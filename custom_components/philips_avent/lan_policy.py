"""Timing policy for the persistent LAN connection.

Kept free of Home Assistant imports so the decisions can be unit-tested without
an HA runtime; `lan.py` owns the sockets and calls into here.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import select
import socket
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

# The monitor closes an idle LAN socket after roughly 30-45 seconds, which shows
# up as "Unexpected Payload from Device" followed by a reconnect every ~30s
# (issue #62). tinytuya leaves keep-alive to the caller on persistent sockets, so
# a ping well inside that window keeps the session up. Receives block for up to
# SOCKET_TIMEOUT seconds, so the effective ping interval is this value plus that.
HEARTBEAT_INTERVAL = 8.0

# Force a reconnect when nothing at all has been heard for this long. A working
# connection answers every heartbeat, so this only fires on a wedged socket.
DATA_TIMEOUT = 600.0

# Delay before retrying a connection, from the second consecutive failure on;
# it doubles with each further failure up to RECONNECT_DELAY_MAX. The cap keeps
# a camera back from a Wi-Fi drop found within a minute, while one that is
# away for good (unplugged, not on Wi-Fi) is tried once a minute instead of
# every few seconds.
RECONNECT_DELAY = 15.0
RECONNECT_DELAY_MAX = 60.0

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

# The results that say the camera did not answer a request: no connection, no
# reply within tinytuya's retries (902, which tinytuya returns after closing
# and reopening the socket itself), or a refused handshake on that reopen
# (914). Anything else came from the camera, an unreadable ack (904) included.
NO_ANSWER_ERRORS = CONNECTION_ERRORS | {LAN_ERR_TIMEOUT, LAN_ERR_KEY_OR_VERSION}

# tinytuya retries a request this many times, reopening the socket each time,
# before it reports a failure; its own default is 5 with a 5 s pause, which
# took about a minute to notice a camera that is gone and kept a refused
# command waiting as long. One retry still rides out a single dropped packet.
SOCKET_RETRY_LIMIT = 1
SOCKET_RETRY_DELAY = 1

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

# An SCD951 accepts at most three LAN sessions at once (measured 02-11-2026).
# Commands used to open a short-lived session each, next to the persistent
# listener: with a local stream through the bridge holding another, two
# commands at once (a timer set and play pressed together, or a press repeated
# while the first was still being retried) left one refused at the handshake
# with 914. Commands now ride the listener's session (OwnSession below), go
# out one at a time per camera (CommandQueue), and one that was not delivered is tried
# again after a growing pause, as long as COMMAND_DEADLINE allows, so a button
# press never hangs for long. A queued command waits up to COMMAND_QUEUE_WAIT
# for the one before it.
COMMAND_RETRY_DELAYS = (1.0, 2.0, 4.0)
COMMAND_ATTEMPTS = len(COMMAND_RETRY_DELAYS) + 1
COMMAND_DEADLINE = 10.0
COMMAND_QUEUE_WAIT = 15.0

# Why a command did not reach the camera, for the message the user sees.
COMMAND_REFUSED = "refused"        # handshake refused or no connection, after the retries
COMMAND_BUSY = "busy"              # an earlier command still held the queue
COMMAND_NO_ANSWER = "no_answer"    # sent, but no answer (902, or 901 on a step): may have been applied


def cloud_may_repeat(failure: str | None, idempotent: bool) -> bool:
    """Whether a command the LAN did not deliver may go out over the cloud.

    A command without an answer may have been applied: the LAN does not send
    it again, and neither may the cloud when applying it twice changes the
    result (a lullaby Next or Previous step).
    """
    return failure != COMMAND_NO_ANSWER or idempotent


# Reconnect once this many unparseable frames arrive in a row.
MAX_PAYLOAD_ERRORS = 3


def is_connection_error(err_code: str | None) -> bool:
    """True when a tinytuya error code means the LAN socket is unusable."""
    return str(err_code or "") in CONNECTION_ERRORS


def device_answered(result: object) -> bool:
    """Whether a tinytuya request result shows the camera answered.

    tinytuya does not raise when a request fails: it returns an error dict.
    A heartbeat that timed out came back as {"Err": "902"}, which counted as
    alive, so a session whose camera stopped answering stayed "connected", and
    every such heartbeat also reset the stale-data timer. None is an ack
    without payload, which is an answer.
    """
    if not isinstance(result, dict):
        return True
    err = result.get("Err")
    return err is None or str(err) not in NO_ANSWER_ERRORS


def command_not_delivered(result: object, *, repeatable: bool = True) -> bool:
    """Whether a failed set_value result shows the command never reached the camera.

    905 and 914 come from opening the socket, before anything is sent
    (WRITE_NEVER_SENT): retrying those cannot apply a command twice. A 901 can
    come after the send, the connection breaking while the reply was read: a
    repeatable command (an absolute value) is retried, a relative lullaby step
    counts as maybe sent and is not. A timeout (902) may come after the camera
    took the command, so it is never retried: "next lullaby" would skip two
    tracks.
    """
    if not isinstance(result, dict):
        return False
    code = str(result.get("Err") or "")
    if code in WRITE_NEVER_SENT:
        return True
    return repeatable and code == LAN_ERR_CONNECT


@dataclass(frozen=True)
class CommandOutcome:
    """How a LAN command ended: delivered, or the first value that was not and why."""

    delivered: bool
    failure: str | None = None
    key: str | None = None
    result: Any = None


# send(items) delivers the values in order over one connection and returns None
# when all were answered, or (the values from the first unanswered one on, its
# tinytuya result) when not.
CommandSend = Callable[[list[tuple[str, Any]]], Awaitable[tuple[list[tuple[str, Any]], Any] | None]]


class CommandQueue:
    """Sends a camera's LAN commands one at a time, retrying the undelivered ones.

    Kept free of Home Assistant and tinytuya so the order and timing can be
    tested; lan.py supplies the sending, sleeping (stop-aware) and clock.
    """

    def __init__(
        self,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        delays: Sequence[float] = COMMAND_RETRY_DELAYS,
        deadline: float = COMMAND_DEADLINE,
        queue_wait: float = COMMAND_QUEUE_WAIT,
    ) -> None:
        self._lock = asyncio.Lock()
        self._sleep = sleep
        self._clock = clock
        self._delays = tuple(delays)
        self._deadline = deadline
        self._queue_wait = queue_wait

    async def run(self, send: CommandSend, items: list[tuple[str, Any]], *, repeatable: bool = True) -> CommandOutcome:
        """Deliver `items`; `repeatable` says whether sending them twice is harmless (command_not_delivered)."""
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=self._queue_wait)
        except TimeoutError:
            return CommandOutcome(False, COMMAND_BUSY, items[0][0] if items else None)
        try:
            return await self._deliver(send, items, repeatable)
        finally:
            self._lock.release()

    async def _deliver(self, send: CommandSend, items: list[tuple[str, Any]], repeatable: bool) -> CommandOutcome:
        start = self._clock()
        for delay in (*self._delays, None):
            failed = await send(items)
            if failed is None:
                return CommandOutcome(True)
            # Only what was not delivered, and only when it certainly was not:
            # the values before it went through already.
            items, result = failed
            if not command_not_delivered(result, repeatable=repeatable):
                return CommandOutcome(False, COMMAND_NO_ANSWER, items[0][0], result)
            if delay is None or self._clock() - start + delay > self._deadline:
                return CommandOutcome(False, COMMAND_REFUSED, items[0][0], result)
            await self._sleep(delay)
        raise AssertionError("unreachable")


# Our LAN sessions per camera: never more than one from this integration. An
# SCD951 accepts at most three LAN sessions at once (measured 02-11-2026), and
# the parent unit must always find one free: whether it needs one of these
# three is unknown, so it is treated as if it does. The bridge holds one while
# it streams locally (one per camera, pkg/rtsp acquire); the integration holds
# the other: its persistent listener, which also carries the commands, or,
# only while the listener is down, one short command session, never next to a
# reconnect. Two of three at most, whatever runs.
SESSION_COMMAND_WAIT = 12.0   # how long a command waits for the listener to send it


class OwnSession:
    """Routes a camera's commands so the integration never holds two LAN sessions.

    While the persistent session is up a command is handed to it (send_on_session);
    otherwise it may open a short session of its own (send_short), but only under
    the same lock the listener holds while it connects, so a reconnect and a
    command session never overlap. Kept free of Home Assistant and tinytuya.
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()

    async def send(
        self,
        items: list[tuple[str, Any]],
        connected: Callable[[], bool],
        send_on_session: CommandSend,
        send_short: CommandSend,
    ) -> tuple[list[tuple[str, Any]], Any] | None:
        if connected():
            return await send_on_session(items)
        async with self.lock:
            # The listener may have reconnected while this waited for the lock.
            if connected():
                return await send_on_session(items)
            return await send_short(items)


class SessionCommands:
    """Commands waiting for the persistent session's listener loop to send them.

    The listener owns the socket: it sends these between two receives, so the
    socket is never used by two threads at once. A command that cannot be sent
    because the session went down is answered with a connection error, which
    CommandQueue retries (then over a short session, see OwnSession).
    """

    def __init__(self, on_submit: Callable[[], None] | None = None) -> None:
        self._pending: list[tuple[list[tuple[str, Any]], asyncio.Future]] = []
        # Wakes the listener out of its wait on the socket (LoopWaker.wake), so
        # a command goes out at once instead of after the current receive.
        self.on_submit = on_submit

    @property
    def pending(self) -> bool:
        return bool(self._pending)

    async def submit(
        self, items: list[tuple[str, Any]], timeout: float = SESSION_COMMAND_WAIT
    ) -> tuple[list[tuple[str, Any]], Any] | None:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = (items, future)
        self._pending.append(entry)
        if self.on_submit is not None:
            self.on_submit()
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except TimeoutError:
            if entry in self._pending:
                self._pending.remove(entry)
            return items, {"Error": "listener did not send it", "Err": LAN_ERR_CONNECT, "Payload": None}

    async def drain(self, send_now: CommandSend) -> bool:
        """Send what is waiting, in order. Returns False if the session broke doing so."""
        while self._pending:
            items, future = self._pending.pop(0)
            try:
                failed = await send_now(items)
            except Exception as ex:  # noqa: BLE001 - a broken socket fails this command, not the loop
                failed = items, {"Error": str(ex), "Err": LAN_ERR_CONNECT, "Payload": None}
            if not future.done():
                future.set_result(failed)
            if failed is not None and is_connection_error(failed[1].get("Err") if isinstance(failed[1], dict) else None):
                self.fail_all()
                return False
        return True

    def fail_all(self) -> None:
        """Answer every waiting command with a connection error (the session is gone)."""
        pending, self._pending = self._pending, []
        for items, future in pending:
            if not future.done():
                future.set_result((items, {"Error": "LAN session closed", "Err": LAN_ERR_CONNECT, "Payload": None}))


LOOP_DATA = "data"          # the camera's socket has something to read
LOOP_WAKE = "wake"          # a command was queued for the listener
LOOP_TIMEOUT = "timeout"    # neither, within the wait


class LoopWaker:
    """Lets the listener wait for the camera's socket *or* a queued command.

    The listener used to sit in a blocking receive (up to RECEIVE_POLL) and only
    then look at queued commands, so a command waited up to that long before it
    went out. Now it waits in select() on the camera's socket and one end of a
    socketpair; SessionCommands.submit writes a byte to the other end, which
    ends the wait at once. Nothing is read from the camera's socket here: when
    both are ready the wake wins, the loop sends the command, and the camera's
    data is still in the socket for the next receive (it is never lost).
    """

    def __init__(self) -> None:
        self._r, self._w = socket.socketpair()
        self._r.setblocking(False)
        self._w.setblocking(False)

    def wake(self) -> None:
        with contextlib.suppress(OSError):  # full or closed: a wake is already pending, or we stop
            self._w.send(b"\0")

    def wait(self, sock: Any, timeout: float) -> str:
        readers: list[Any] = [self._r]
        if sock is not None:
            readers.append(sock)
        try:
            ready, _, _ = select.select(readers, [], [], timeout)
        except (OSError, ValueError):  # the camera's socket was closed under us
            return LOOP_DATA if sock is not None else LOOP_TIMEOUT
        if self._r in ready:
            self._drain()
            return LOOP_WAKE
        return LOOP_DATA if ready else LOOP_TIMEOUT

    def _drain(self) -> None:
        with contextlib.suppress(OSError):
            while self._r.recv(64):
                pass

    def close(self) -> None:
        for s in (self._r, self._w):
            with contextlib.suppress(OSError):
                s.close()


def is_key_error(err_code: object) -> bool:
    """True when a refused handshake points at the local key (tinytuya 914)."""
    return str(err_code or "") == LAN_ERR_KEY_OR_VERSION


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
    8c4fc03), so the caller publishes it. A heartbeat the camera did not answer
    (`device_answered`: no connection, a timeout or a refused handshake) is a
    failure; an ack we cannot parse still proves the socket is alive.
    """
    if not device_answered(result):
        return False, None
    if not isinstance(result, dict):
        return True, None
    dps = result.get("dps")
    return True, (dps if isinstance(dps, dict) and dps else None)


def data_stale(now: float, last_data: float, timeout: float = DATA_TIMEOUT) -> bool:
    """True when nothing has been received for `timeout` seconds."""
    return now - last_data > timeout


def reconnect_delay(
    consecutive_failures: int, delay: float = RECONNECT_DELAY, maximum: float = RECONNECT_DELAY_MAX
) -> float:
    """Seconds to wait before the next connection attempt.

    The first failure retries immediately: a device that closed the socket is
    usually ready again straight away, and waiting only widens the window where
    the integration falls back to cloud polling. Repeated failures back off,
    doubling up to `maximum`, so a monitor that is really gone is not hammered.
    """
    if consecutive_failures <= 1:
        return 0.0
    return min(delay * 2 ** (consecutive_failures - 2), maximum)

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

    Once a session with this camera negotiated 3.4 or later, no address gets a
    3.3 fallback, scanned or not: a 3.3 session would check no key and miss the
    alarm DPS, and stay on 3.3 from then on (an SCD951 that speaks only 3.4
    ended up there after one failed attempt). That version comes first, as the
    one that worked, then the other keyed versions, the announced one first, so
    a firmware update that moves the camera from 3.4 to 3.5 does not lock it
    out of the LAN.
    """
    if keyed_protocol(last_session):
        others = [v for v in (announced, *VERIFIED_VERSIONS) if keyed_protocol(v)]
        return list(dict.fromkeys([last_session, *others]))
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


def parse_lan_hosts(value: str | None) -> tuple[list[str], dict[str, str]] | None:
    """The option's shared addresses and its per-camera bindings, or None when an entry is invalid.

    Entries are separated by commas (or semicolons or new lines). `address` is
    shared: every camera of the entry may try it, on a key-checked protocol
    only. `camera=address` binds the address to one camera (its device id, the
    first six or more characters of the id, or its name): only that camera
    tries it, and it may speak protocol 3.3 there (see bound_versions). Without
    an "=", an entry may also hold several addresses separated by spaces, as
    the option has always accepted.
    """
    shared: list[str] = []
    bound: dict[str, str] = {}
    for raw in re.split(r"[,;\n]+", value or ""):
        entry = raw.strip()
        if not entry:
            continue
        if "=" in entry:
            key, _, address = entry.partition("=")
            host = lan_host(address)
            key = key.strip()
            if not key or host is None:
                return None
            bound[key] = host
            continue
        for part in entry.split():
            host = lan_host(part)
            if host is None:
                return None
            shared.append(host)
    return list(dict.fromkeys(shared)), bound


def lan_hosts(value: str | None) -> list[str] | None:
    """The shared addresses from the option, without duplicates and without the bound ones.

    An empty list when the option is empty; None when one entry is invalid.
    """
    parsed = parse_lan_hosts(value)
    return None if parsed is None else parsed[0]


def normalized_lan_hosts(value: str | None) -> str | None:
    """The option as stored: shared addresses first, then the bindings; None when invalid."""
    parsed = parse_lan_hosts(value)
    if parsed is None:
        return None
    shared, bound = parsed
    return ", ".join([*shared, *(f"{key}={address}" for key, address in bound.items())])


# The shortest part of a device id that names a camera in a binding: the six
# characters Home Assistant shows after the name of two same-named cameras.
BINDING_ID_PREFIX = 6


def resolve_lan_bindings(
    bound: dict[str, str], cameras: dict[str, list[str]]
) -> tuple[dict[str, str], list[str]]:
    """({camera id: address}, the keys that name no camera or more than one).

    cameras: {device id: its names} (the cloud name, Home Assistant's name and
    the name the user gave it). A key matches a camera's id, the start of its id
    (at least BINDING_ID_PREFIX characters) or one of its names, ignoring case.
    """
    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    for key, address in bound.items():
        wanted = key.strip().casefold()
        matches = [
            camera for camera, names in cameras.items()
            if wanted == camera.casefold()
            or (len(wanted) >= BINDING_ID_PREFIX and camera.casefold().startswith(wanted))
            or any(wanted == name.strip().casefold() for name in names if name)
        ]
        if len(matches) == 1:
            resolved[matches[0]] = address
        else:
            unresolved.append(key)
    return resolved, unresolved


def shared_hosts_for(shared: list[str], resolved: dict[str, str], camera: str) -> list[str]:
    """The shared addresses one camera may try: none that is bound to another camera."""
    others = {address for owner, address in resolved.items() if owner != camera}
    return [address for address in shared if address not in others]


def bound_versions(known: float | None, proven: float | None) -> list[float]:
    """Protocol versions to try on a camera's own bound address.

    The user said this address is this camera's, so 3.3, which proves no key in
    its handshake, is allowed here and nowhere else; the session then has to
    prove the camera itself (SessionIdentity). A camera that negotiated 3.4 or
    later once keeps to the keyed versions (session_candidates). With nothing
    known, the keyed versions come first and 3.3 last.
    """
    if keyed_protocol(proven):
        return session_candidates(known, proven, scanned=False)
    if known is not None:
        return version_candidates(known)
    return [*VERIFIED_VERSIONS, PROTOCOL_VERSION_DEFAULT]


# tinytuya's codes for a payload it could not decrypt into JSON: what every
# message of a 3.3 camera reads as when it was encrypted with another key.
UNREADABLE_PAYLOAD_ERRORS = frozenset({"900", "904"})
# Consecutive unreadable messages, without one readable in between, before a
# 3.3 session on a bound address counts as another camera's.
IDENTITY_UNREADABLE_LIMIT = 2
# A bound address that answered with another camera's key is not tried again
# for this long; the log says to check the binding.
BOUND_MISMATCH_HOLD = 3600.0

IDENTITY_OK = "ok"
IDENTITY_PENDING = "pending"
IDENTITY_WRONG = "wrong"


class SessionIdentity:
    """Whether a LAN session is really with this camera.

    From protocol 3.4 the handshake proves the local key, so a session is this
    camera's from the start. A 3.3 handshake proves nothing: any Tuya device at
    the address accepts it. Its messages, though, are AES-encrypted with the
    sender's local key and tinytuya decrypts them with this camera's: another
    camera's push comes out as garbage (error 900 or 904), this camera's as JSON
    with its dps, and a 3.3 status push names the sender in devId. So the first
    readable message settles it, and a sender id that is not this camera's, or
    messages that keep failing to decrypt, mean the session is another device's.
    No message has to be asked for: the camera pushes its state by itself.
    """

    def __init__(self, device_id: str, required: bool) -> None:
        self._device_id = device_id
        self.verified = not required
        self._unreadable = 0

    def check(self, data: object) -> str:
        if self.verified:
            return IDENTITY_OK
        if not isinstance(data, dict):
            return IDENTITY_PENDING
        if "Err" in data or "Error" in data:
            if str(data.get("Err")) in UNREADABLE_PAYLOAD_ERRORS:
                self._unreadable += 1
                if self._unreadable >= IDENTITY_UNREADABLE_LIMIT:
                    return IDENTITY_WRONG
            return IDENTITY_PENDING
        sender = next((data[k] for k in ("devId", "gwId", "devid") if isinstance(data.get(k), str)), None)
        if sender is not None and sender != self._device_id:
            return IDENTITY_WRONG
        if sender == self._device_id or isinstance(data.get("dps"), dict):
            self.verified = True
            self._unreadable = 0
            return IDENTITY_OK
        return IDENTITY_PENDING

    def proven_by_key(self) -> None:
        """The camera answered in its own key without naming itself or sending dps.

        An SCD923 on 3.3 answers the status query with the text "json obj data
        unvalid" (tinytuya's device22 sign), and pushes nothing while nothing
        changes. That text could only be read after decrypting with this
        camera's local key, which no other device holds: the session is this
        camera's (07-11-2026: without it, a start without the cloud left the
        camera's entities unavailable until it happened to push).
        """
        self.verified = True
        self._unreadable = 0


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

    def seed(self, addresses: dict[str, str]) -> None:
        """Claim each camera's last verified address before any client connects.

        From the offline cache, so right after a restart, before the rightful
        camera's session is up, the others do not knock on its address either.
        An address cached for two cameras is left unclaimed: one of the two is
        stale, and the handshake will tell which.
        """
        counts: dict[str, int] = {}
        for ip in addresses.values():
            counts[ip] = counts.get(ip, 0) + 1
        for camera, ip in addresses.items():
            if ip and counts[ip] == 1:
                self.claim(ip, camera)

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

    def note(self, ip: str, now: float, hold: float | None = None) -> None:
        self._until[ip] = now + (self._hold if hold is None else hold)

    def skipped(self, ip: str, now: float) -> bool:
        until = self._until.get(ip)
        if until is None:
            return False
        if now >= until:
            del self._until[ip]
            return False
        return True


def lan_command_failure_message(camera: str, connected: bool, failure: str | None) -> str:
    """What the user is told when LAN preferred keeps a failed command off the cloud.

    `failure` is the LAN client's `last_command_failure` (pr/lan-liveness):
    "refused" when the camera refused the command's connection after the
    retries (an SCD951 accepts three LAN sessions at once, and a running
    stream takes one), "busy" when an earlier command still held the queue,
    None or "no_answer" otherwise.
    """
    if not connected:
        return f"{camera} is not reachable on the LAN, and LAN preferred keeps commands off the cloud"
    if failure == "refused":
        return (
            f"{camera} refused the LAN connection for this command: it allows only a few at once, "
            "and a running stream takes one. Try again in a moment"
        )
    if failure == "busy":
        return f"{camera} is still busy with an earlier command. Try again in a moment"
    return (
        f"{camera} did not answer the command on the LAN (it may have taken it anyway), "
        "and LAN preferred keeps commands off the cloud"
    )
