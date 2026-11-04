"""Timing policy for the persistent LAN connection.

Kept free of Home Assistant imports so the decisions can be unit-tested without
an HA runtime; `lan.py` owns the sockets and calls into here.
"""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable

# The monitor closes an idle LAN socket after roughly 30-45 seconds, which shows
# up as "Unexpected Payload from Device" followed by a reconnect every ~30s
# (issue #62). tinytuya leaves keep-alive to the caller on persistent sockets, so
# a ping well inside that window keeps the session up. Receives block for up to
# SOCKET_TIMEOUT seconds, so the effective ping interval is this value plus that.
HEARTBEAT_INTERVAL = 8.0

# Force a reconnect when nothing at all has been heard for this long. A working
# connection answers every heartbeat, so this only fires on a wedged socket.
DATA_TIMEOUT = 600.0

# Delay before retrying a connection, from the second consecutive failure on.
RECONNECT_DELAY = 15.0

# tinytuya error codes (see tinytuya.core.error_helper). Only the connection
# ones mean the socket is gone; 904 says a frame could not be parsed, which the
# monitor also produces for stray keep-alive acks, and dropping the connection
# over one of those is what kept the camera entity churning (issue #62).
LAN_ERR_CONNECT = "901"
LAN_ERR_PAYLOAD = "904"
LAN_ERR_OFFLINE = "905"
CONNECTION_ERRORS = frozenset({LAN_ERR_CONNECT, LAN_ERR_OFFLINE})
LAN_ERR_TIMEOUT = "902"
LAN_ERR_KEY_OR_VERSION = "914"

# tinytuya hands a failed write back as an error dict rather than raising.
# 905 and 914 come from opening the socket, before anything is sent: nothing to
# connect to, or the session key refused (3.4 and up). tinytuya can also return
# them when a send broke off half-way and the reconnect then failed, which is
# rare enough to accept; a step is never re-sent after a slow reply, because
# set_dps does not wait for one. 901 and 902 can come after the send: a connection error
# while reading the reply, or no reply at all. 904, an unreadable reply, means
# the monitor answered, so the command landed.
WRITE_NEVER_SENT = frozenset({LAN_ERR_OFFLINE, LAN_ERR_KEY_OR_VERSION})
WRITE_MAYBE_SENT = frozenset({LAN_ERR_CONNECT, LAN_ERR_TIMEOUT})

# Reconnect once this many unparseable frames arrive in a row.
MAX_PAYLOAD_ERRORS = 3


def is_connection_error(err_code: str | None) -> bool:
    """True when a tinytuya error code means the LAN socket is unusable."""
    return str(err_code or "") in CONNECTION_ERRORS


def lan_write_delivered(result: object, *, repeatable: bool) -> bool:
    """Whether a tinytuya `set_value` result means the command reached the monitor.

    Before this, every write that did not raise counted as sent, and the cloud
    mirror hid the ones that never left: harmless while every command also went
    through the cloud, a lost command once it no longer does.

    `repeatable` says whether sending the command twice is harmless. An
    absolute value is, so anything short of an answer sends it through the cloud
    as well. A relative lullaby step is not (issue #91): it goes to the cloud
    only when it never left, and a step lost to an ambiguous error is preferred
    to one taken twice.
    """
    if not isinstance(result, dict):
        return True
    code = str(result.get("Err") or "")
    if code in WRITE_NEVER_SENT:
        return False
    if code in WRITE_MAYBE_SENT:
        return not repeatable
    return True


def should_reconnect(err_code: str | None, consecutive_payload_errors: int) -> bool:
    """Whether a device error frame justifies dropping the connection.

    A connection error always does. An unparseable frame only does once several
    arrive in a row, so a single odd frame no longer costs the user a reconnect
    cycle and, with it, a spell of the camera showing as unavailable.
    """
    if is_connection_error(err_code):
        return True
    return consecutive_payload_errors >= MAX_PAYLOAD_ERRORS


def heartbeat_due(now: float, last_heartbeat: float, interval: float = HEARTBEAT_INTERVAL) -> bool:
    """True when the next keep-alive ping is due."""
    return now - last_heartbeat >= interval


def heartbeat_outcome(result: object) -> tuple[bool, dict | None]:
    """What a keep-alive got back: whether the socket lives, and any state pushed in its place.

    tinytuya's `heartbeat(nowait=False)` skips the empty ack and returns the next
    frame that has a payload, from the same socket the pushes arrive on. A push
    landing in that window comes back here rather than in the receive loop, and
    dropping it lost about half of a run of alarms on an SCD973/26 (fork
    8c4fc03), so the caller publishes it. Only a connection error counts as
    failure: an ack we cannot parse still proves the socket is alive.
    """
    if not isinstance(result, dict):
        return True, None
    if is_connection_error(result.get("Err")):
        return False, None
    dps = result.get("dps")
    return True, (dps if isinstance(dps, dict) and dps else None)


def data_stale(now: float, last_data: float, timeout: float = DATA_TIMEOUT) -> bool:
    """True when nothing has been received for `timeout` seconds."""
    return now - last_data > timeout


def reconnect_delay(consecutive_failures: int, delay: float = RECONNECT_DELAY) -> float:
    """Seconds to wait before the next connection attempt.

    The first failure retries immediately: a device that closed the socket is
    usually ready again straight away, and waiting only widens the window where
    the integration falls back to cloud polling. Repeated failures back off so a
    monitor that is really gone is not hammered.
    """
    if consecutive_failures <= 1:
        return 0.0
    return delay

# Protocol version for the local Tuya session. 3.3 is what the integration has
# always used, and what an SCD973 on firmware 1.4.0 speaks. An SCD973/26 announces
# 3.5 in its discovery broadcast (capture on #51), and a session negotiated at the
# wrong version cannot read frames the camera sends, which is the likely source
# of the "Unexpected Payload from Device" frames on #62. Try what the camera
# announces, keep 3.3 as the fallback so local control cannot regress.
PROTOCOL_VERSION_DEFAULT = 3.3
SUPPORTED_PROTOCOL_VERSIONS = (3.1, 3.2, 3.3, 3.4, 3.5)

# From 3.4 up the session is keyed, and on such a session an SCD973/26 answered
# DP_QUERY with all 37 keys and pushed its alarm record in DPS 212 within 0.2 to
# 2.4 seconds (fork c3d0b83 and 619981b, measured at 3.5). A 3.3 session does
# neither: the query goes unanswered and 212 never arrives. The maintainer's
# SCD973 on firmware 1.4.0 speaks 3.3, so everything gated here leaves it as it
# was.
KEYED_PROTOCOL_MIN_VERSION = 3.4


def keyed_protocol(version: float | None) -> bool:
    """Whether a session at `version` carries the monitor's state on its own."""
    return version is not None and version >= KEYED_PROTOCOL_MIN_VERSION


def seeded_session(last_ip: str | None, last_protocol: str) -> tuple[str | None, float | None]:
    """The address and protocol a new client may dial before its first scan, or (None, None).

    Seeded from the pair last published to the bridge, so a restart need not wait
    for a broadcast. Only a keyed protocol qualifies: its handshake proves the
    address and the version together, so a stale pair fails and the client scans.
    A 3.3 pair is a bare TCP connect that proves neither, so those clients scan
    first, as they always did.
    """
    version = parse_protocol_version(last_protocol)
    if last_ip and keyed_protocol(version):
        return last_ip, version
    return None, None


def session_candidates(announced: float | None, last_session: float | None, *, scanned: bool) -> list[float]:
    """Protocol versions to try at an address, best guess first.

    The announced version comes before the last session's. A 3.3 fallback on a
    monitor that announces 3.5 is a TCP connect that reads nothing (see
    `confirmed_protocol`), and starting every reconnect from it kept such a
    monitor on 3.3 until Home Assistant restarted.

    At an address that did not just come from a scan, a keyed version gets no
    3.3 fallback: only its handshake proves the monitor is the one answering
    there, and a failure sends the client back to the scan instead.
    """
    preferred = announced if announced is not None else last_session
    if not scanned and keyed_protocol(preferred):
        return [preferred]
    return version_candidates(preferred)


def baseline_dps(result: object) -> dict | None:
    """The state in an answer to DP_QUERY, or None when it carries none.

    An error frame, an empty answer or a timeout all give None: the session goes
    on as before and the cloud poll keeps the baseline it already provided.
    """
    if not isinstance(result, dict) or "Err" in result:
        return None
    dps = result.get("dps")
    return dps if isinstance(dps, dict) and dps else None


def parse_protocol_version(value: object) -> float | None:
    """Read the protocol version out of a discovery broadcast.

    tinytuya reports it as a string such as "3.5". Anything unrecognised gives
    None, so the caller falls back to the default rather than handing tinytuya a
    version it cannot speak.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        version = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            version = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return version if version in SUPPORTED_PROTOCOL_VERSIONS else None


def version_candidates(
    announced: float | None, default: float = PROTOCOL_VERSION_DEFAULT
) -> list[float]:
    """Protocol versions to try for a local session, best guess first.

    The announced version comes first because the camera should know what it
    speaks. The default follows as a fallback, since 3.4 and up negotiate a
    session key and a firmware that refuses can still be reached the old way.
    """
    candidates = []
    if announced is not None:
        candidates.append(announced)
    if default not in candidates:
        candidates.append(default)
    return candidates


def confirmed_protocol(version: float, announced: float | None) -> str | None:
    """The protocol to publish for a session at `version`, or None when it proves nothing.

    Below 3.4 tinytuya does no handshake, so the 3.3 fallback on a monitor that
    announces 3.5 is only a TCP connect, often after a Wi-Fi blip. Publishing it
    would restart the bridge and keep every stream off the LAN until Home
    Assistant restarts, so only a version at or above the announced one counts.
    """
    if announced is None or version >= announced:
        return str(version)
    return None


class AddressPublisher:
    """Report the monitor's LAN address and protocol when the pair becomes new and complete.

    The LAN client clears its address on every failed connect and refills it after
    rediscovery. Reporting those blips would rewrite the bridge JSON, and restart
    the bridge, twice per hiccup (issue #73), so losing the address says nothing
    and a return to the last reported pair says nothing either. The protocol rides
    along because the bridge's LAN path speaks only 3.5.
    """

    def __init__(
        self, callback: Callable[[str], None] | None, last: str | None = None, protocol: str = ""
    ) -> None:
        self._callback = callback
        # Seeded from the bridge JSON on start, so rediscovery at the same address stays silent.
        self._last = last or None
        self._protocol = protocol or ""

    @property
    def last(self) -> str | None:
        """The last address reported (or seeded): what the bridge JSON carries."""
        return self._last

    @property
    def protocol(self) -> str:
        """The protocol reported (or seeded) with that address, such as "3.3"; "" when unknown."""
        return self._protocol

    def update(self, ip: str | None, protocol: str | None) -> None:
        if not ip or not protocol or (ip, protocol) == (self._last, self._protocol):
            return
        self._last, self._protocol = ip, protocol
        if self._callback:
            self._callback(ip)


# A DNS label: letters, digits and inner hyphens, at most 63 characters.
_HOST_LABEL = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)")


def lan_host(value: str | None) -> str | None:
    """The camera address from the options, or None when it is empty or not an address.

    Accepts an IPv4/IPv6 address or a host name; no port, since the Tuya LAN
    protocol always uses 6668.
    """
    value = (value or "").strip()
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    labels = value.rstrip(".").split(".")
    if len(value) <= 253 and all(_HOST_LABEL.fullmatch(label) for label in labels) and not labels[-1].isdigit():
        return value.rstrip(".")
    return None


# Protocol versions whose handshake proves the local key: from 3.4 the client and
# the camera negotiate a session key with it, which fails for another camera.
# On 3.3 a TCP connect succeeds whatever the key, so a configured address tried
# on 3.3 would connect one camera's client to another camera of the account.
VERIFIED_VERSIONS = (3.5, KEYED_PROTOCOL_MIN_VERSION)


def verified_versions(known: float | None) -> list[float]:
    """Protocol versions to try on a configured address: only those that check the key, the known one first."""
    candidates = [known] if known is not None and known >= min(VERIFIED_VERSIONS) else []
    candidates += [v for v in VERIFIED_VERSIONS if v not in candidates]
    return candidates


def lan_hosts(value: str | None) -> list[str] | None:
    """The addresses from the option, separated by commas or spaces, without duplicates.

    An empty list when the option is empty; None when one entry is not an address.
    """
    hosts = [lan_host(part) for part in re.split(r"[,\s]+", value or "") if part]
    if None in hosts:
        return None
    return list(dict.fromkeys(hosts))


# A configured address that refused this camera's key on every key-checking
# protocol is another camera's. Skipping it for this long keeps a camera that
# is offline from handshaking with the others' cameras on every reconnect.
FOREIGN_ADDRESS_HOLD = 600.0


class AddressClaims:
    """Configured addresses that another camera of the same entry is connected on.

    One per config entry, shared by its cameras' LAN clients. A camera that is
    connected claims its address; the others skip it entirely while the claim
    stands, instead of handshaking with that camera using the wrong key. Such a
    handshake is refused, and on 3.4 it holds one of the camera's three LAN
    sessions for about 10 s (measured 02-11-2026), long enough to make a
    command of the rightful camera fail. The claim stays while its camera is
    disconnected (a reconnect is exactly when the others would knock, and each
    knock takes a session the parent unit may need); it ends only when that
    camera connects at another address, its own address refuses its key (the
    address went to another camera), or its client stops. ForeignAddresses
    then limits the attempts again.
    """

    def __init__(self) -> None:
        self._owner: dict[str, str] = {}

    def claim(self, ip: str, camera: str) -> None:
        self.release(camera)
        self._owner[ip] = camera

    def release(self, camera: str) -> None:
        for ip in [ip for ip, owner in self._owner.items() if owner == camera]:
            del self._owner[ip]

    def held_by_other(self, ip: str, camera: str) -> bool:
        owner = self._owner.get(ip)
        return owner is not None and owner != camera

    def owned(self) -> dict[str, str]:
        """{address: camera} of the current claims."""
        return dict(self._owner)


def ordered_hosts(configured: list[str], own: str | None, others: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """A camera's addresses to try: its own verified one first, then the configured
    ones that are not verified for another camera of the entry."""
    hosts = [own] if own else []
    for ip in configured:
        if ip not in hosts and ip not in others:
            hosts.append(ip)
    return hosts


class ForeignAddresses:
    """Configured addresses that turned out to be another camera's, for a while."""

    def __init__(self, hold: float = FOREIGN_ADDRESS_HOLD) -> None:
        self._hold = hold
        self._until: dict[str, float] = {}

    def note(self, ip: str, now: float) -> None:
        self._until[ip] = now + self._hold

    def skipped(self, ip: str, now: float) -> bool:
        until = self._until.get(ip)
        if until is None:
            return False
        if now >= until:
            del self._until[ip]
            return False
        return True
