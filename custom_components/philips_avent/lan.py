"""Tuya LAN protocol client for real-time DPS push updates."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

import tinytuya
from homeassistant.core import HomeAssistant

from .command_echo import EchoWatch, send_with_echo
from .lan_policy import (
    BOUND_MISMATCH_HOLD,
    IDENTITY_OK,
    IDENTITY_WRONG,
    LAN_ERR_CONNECT,
    LOOP_TIMEOUT,
    LOOP_WAKE,
    PROTOCOL_VERSION_DEFAULT,
    SOCKET_RETRY_DELAY,
    SOCKET_RETRY_LIMIT,
    AddressClaims,
    AddressPublisher,
    CommandQueue,
    ForeignAddresses,
    LoopWaker,
    OwnSession,
    SessionCommands,
    SessionIdentity,
    baseline_dps,
    bound_versions,
    confirmed_protocol,
    data_stale,
    device_answered,
    heartbeat_due,
    heartbeat_outcome,
    is_key_error,
    keyed_protocol,
    lan_hosts,
    parse_protocol_version,
    reconnect_delay,
    seeded_session,
    session_candidates,
    should_reconnect,
    verified_versions,
)

_LOGGER = logging.getLogger(__name__)

SOCKET_TIMEOUT = 5
# How long the listener waits for the camera's socket before it looks at the
# heartbeat; a queued command ends the wait at once (LoopWaker). A heartbeat, a
# query or a command waits up to SOCKET_TIMEOUT for its answer.
RECEIVE_POLL = 1
SCAN_MAXRETRY = 5


class TuyaLANClient:
    """Persistent LAN connection to a Tuya device for real-time DPS push."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        local_key: str,
        on_dps_update: Callable[[dict[str, Any], bool], None],
        on_address_change: Callable[[str], None] | None = None,
        last_ip: str | None = None,
        last_protocol: str = "",
        *,
        on_session_up: Callable[[], None] | None = None,
        hosts: str | list[str] | None = None,
        claims: AddressClaims | None = None,
        ip: str | None = None,
        version: float | None = None,
        on_connection_change: Callable[[bool], None] | None = None,
        on_key_rejected: Callable[[], None] | None = None,
        bound: str | None = None,
    ) -> None:
        self._hass = hass
        self._device_id = device_id
        self._local_key = local_key
        self._on_dps_update = on_dps_update
        self._on_session_up = on_session_up
        self._on_connection_change = on_connection_change
        self._on_key_rejected = on_key_rejected
        self._device: tinytuya.Device | None = None
        # The address the options bind to this camera ("camera=address"): its
        # own, so tried even on protocol 3.3, which proves no key in the
        # handshake; the session then proves the camera (SessionIdentity).
        self._bound: str | None = bound
        # Configured shared addresses, tried before every scan (see _connect).
        shared = lan_hosts(hosts) if hosts is None or isinstance(hosts, str) else list(hosts)
        self._hosts = [a for a in shared or [] if a != bound]
        # Whether the current session is really this camera's; settled at
        # connect for 3.4 and later, by the first readable message below that.
        self._identity = SessionIdentity(device_id, required=False)
        # A 3.3 session's address and protocol, published once its identity is
        # proven rather than at the bare TCP connect (see _publish_when_proven).
        self._pending_publish: tuple[str, str] | None = None
        self._foreign = ForeignAddresses()
        # Shared with the entry's other cameras: an address one of them is
        # connected on is not tried here (lan_policy.AddressClaims).
        self._claims = claims or AddressClaims()
        # The address of the last session that worked, kept when a failed
        # round clears _ip: it is this camera's, never another camera's.
        self._own_ip: str | None = None
        # The refusal of the last handshake attempt, and whether every attempt
        # on the last address was refused for the key.
        self._last_refusal: str | None = None
        self._key_refused = False
        self._address = AddressPublisher(on_address_change, last_ip, last_protocol)
        # A keyed pair from the last run is dialled before any scan; see seeded_session.
        self._ip, self._announced_version = seeded_session(last_ip, last_protocol)
        self._version: float | None = None
        if ip:
            # The address and protocol of the last verified session, from the
            # offline cache: tried before the broadcast scan, which never
            # reaches Home Assistant in a container on a bridge network.
            self._ip = self._own_ip = ip
            self._version = version
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._rekeyed = False
        # Set once the live session has decrypted a frame; see `proven`.
        self._exchanged = False
        self.connected: bool = False
        # Handshakes the camera refused (914) at its own address: what a
        # camera with all three LAN sessions taken answers (lan_metrics.py).
        self.refused_handshakes = 0
        # Whether "not found on LAN" was logged since the camera was last
        # connected: once at warning, then at debug, not every round.
        self._missing_reported = False
        # One command at a time, and never a second session of ours next to the
        # listener: the camera allows three, and the parent unit must keep one
        # (lan_policy.OwnSession). Commands ride the listener's session.
        self._commands = CommandQueue(sleep=self._interruptible_sleep)
        self._own_session = OwnSession()
        self._waker = LoopWaker()
        self._session_commands = SessionCommands(on_submit=self._waker.wake)
        # The camera ignores a command that comes while it is still busy with
        # the previous one: they wait for it to settle, and one without an
        # echo goes once more (command_echo.py).
        self._echoes = EchoWatch()
        # Why the last command failed (lan_policy.COMMAND_*), for the user's message.
        self.last_command_failure: str | None = None

    @property
    def ip(self) -> str | None:
        """The last published address, not the live one: `_ip` is cleared on every failed connect."""
        return self._address.last

    @property
    def protocol(self) -> str:
        """The protocol published with `ip`, such as "3.3"; "" when unknown."""
        return self._address.protocol

    @property
    def proven(self) -> bool:
        """Whether the live session has shown that it holds the right key, and is this camera.

        From 3.4 up the handshake proves it. Below, a connect is bare TCP, so it
        takes a frame that decrypted: until then a 3.5 monitor on an unconfirmed
        3.3 fallback, or a 3.3 monitor straight after a key rotation, would look
        live while delivering nothing. And it has to be this camera's frame
        (SessionIdentity): on a bound address a 3.3 handshake succeeds with any
        Tuya device.
        """
        return self.connected and (keyed_protocol(self._version) or self._exchanged) and self._identity.verified

    @property
    def verified(self) -> bool:
        """The same as `proven`: the session is this camera's, so its address may be cached."""
        return self.proven

    @property
    def session_ip(self) -> str | None:
        """Address of the current or last working session."""
        return self._ip

    @property
    def version(self) -> float | None:
        """Protocol version of the current or last working session."""
        return self._version

    def _set_connected(self, connected: bool) -> None:
        if connected == self.connected:
            return
        self.connected = connected
        if self._on_connection_change:
            try:
                self._on_connection_change(connected)
            except Exception:
                _LOGGER.exception("Error in LAN connection callback")

    @property
    def session_version(self) -> float | None:
        """The protocol the live session negotiated, or None without a session.

        Unlike `protocol`, this follows the session: an unconfirmed 3.3 fallback
        on a monitor that announces 3.5 reads 3.3 here.
        """
        return self._version if self.connected else None

    def set_local_key(self, local_key: str) -> None:
        """Use a key the cloud rotated, for every connection from now on.

        A session that is up was opened with the old key, so the run loop drops
        it at its next turn and reconnects with this one. Called on the event
        loop, like the run loop itself, so nothing else is using the socket.
        """
        self._local_key = local_key
        self._rekeyed = True

    def _drop_session_if_rekeyed(self) -> None:
        """Close a session opened with a key that has since been replaced."""
        if not self._rekeyed:
            return
        self._rekeyed = False
        if self._device:
            _LOGGER.info("Local key for %s changed, reconnecting the LAN session", self._device_id)
            self._disconnect()

    async def start(self) -> None:
        self._stop_event.clear()
        self._task = self._hass.async_create_background_task(
            self._run(), "tuya_lan_listener"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        self._waker.wake()  # out of a wait on the socket at once
        self._echoes.close()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._disconnect()
        self._claims.release(self._device_id)

    def _new_identity(self) -> SessionIdentity:
        """A session below 3.4 has to prove it is this camera with its first readable message."""
        return SessionIdentity(self._device_id, required=not keyed_protocol(self._version))

    def _not_this_camera(self) -> None:
        """The session answered with another key or another sender: drop it and the address.

        A 3.3 handshake succeeds with any Tuya device, so a wrong or stale
        address (a camera bound to another camera's address, or a DHCP lease
        that moved) would otherwise hold one of that device's LAN sessions.
        """
        ip = self._ip
        _LOGGER.warning(
            "The LAN session of %s at %s answers with another device's key or id: not this camera. "
            "Check its address in the integration's options; it is not tried again for an hour",
            self._device_id, ip,
        )
        if ip:
            self._foreign.note(ip, time.monotonic(), BOUND_MISMATCH_HOLD)
        if self._own_ip == ip:
            self._own_ip = None
        self._ip = None
        self._pending_publish = None
        # A 3.3 "session" with another device proved nothing about this camera.
        if not keyed_protocol(self._version):
            self._version = None
        self._claims.release(self._device_id)
        self._disconnect()

    def _publish_when_proven(self) -> None:
        """Publish a 3.3 session's address once its identity is proven (review 11a)."""
        if self._pending_publish and self._identity.verified:
            self._address.update(*self._pending_publish)
            self._pending_publish = None

    def _disconnect(self) -> None:
        if self._device:
            try:
                self._device.close()
            except Exception as ex:  # noqa: BLE001 - teardown must not raise, whatever tinytuya throws
                _LOGGER.debug("Ignoring error while closing the LAN socket: %s", ex)
            self._device = None
        self._session_commands.fail_all()
        self._echoes.lost()
        self._set_connected(False)
        self._exchanged = False
        # The claim stays through a disconnect: while this camera reconnects,
        # the others must not knock on its address (AddressClaims).

    async def _discover_device(self) -> tuple[str | None, float | None]:
        """Find the monitor on the LAN, keeping the protocol version it announces."""
        def _scan():
            devices = tinytuya.deviceScan(maxretry=SCAN_MAXRETRY)
            for ip, info in devices.items():
                if info.get("gwId") == self._device_id:
                    return ip, parse_protocol_version(info.get("version"))
            return None, None

        return await self._hass.async_add_executor_job(_scan)

    def _try_direct_connect(self, ip: str, version: float) -> tinytuya.Device | None:
        """Try connecting directly to a known IP via TCP handshake only.

        Don't send updatedps() — it crashes this firmware. DP_QUERY goes out
        once the session is up, and only on 3.4 and up; see `_query_baseline`.

        `_get_socket` answers True or a tinytuya error code, and from 3.4 up that
        covers the session-key negotiation, so the caller learns whether this
        protocol version actually works on this firmware.
        """
        self._last_refusal = None
        try:
            d = tinytuya.Device(self._device_id, ip, self._local_key, version=version)
            d.set_socketPersistent(True)
            d.set_socketTimeout(SOCKET_TIMEOUT)
            d.set_socketRetryLimit(SOCKET_RETRY_LIMIT)
            d.set_socketRetryDelay(SOCKET_RETRY_DELAY)
            result = d._get_socket(False)
            if result is True:
                # Receives wait briefly from here on (RECEIVE_POLL).
                d.set_socketTimeout(RECEIVE_POLL)
                # Return at the first message after a heartbeat: the camera's
                # ack has no payload, and with retries on tinytuya then read on
                # for another message until the socket timeout (5 s), holding up
                # the listener.
                d.retry = False
                return d
            _LOGGER.debug(
                "LAN connection to %s at protocol %s refused: %s", ip, version, result
            )
            self._last_refusal = str(result)
            d.close()
        except Exception as ex:  # noqa: BLE001 - tinytuya raises socket, decode and key errors here
            _LOGGER.debug("Direct LAN connection to %s at protocol %s failed: %s", ip, version, ex)
        return None

    async def _connect_at_any_version(
        self, ip: str, *, scanned: bool, verified: bool = False, report_key: bool = True, bound: bool = False
    ) -> tinytuya.Device | None:
        """Connect to a known IP, trying the announced protocol then the default.

        verified: only protocols whose handshake proves this camera's key, for a
        configured address, which may be another camera's.
        report_key: whether a refused key means "the key changed" (the camera's
        own last address) rather than "not this camera" (another configured one).
        bound: the address the options bind to this camera, where 3.3 is allowed
        as well (lan_policy.bound_versions).
        """
        if bound:
            candidates = bound_versions(self._announced_version or self._version, self._version)
        elif verified:
            candidates = verified_versions(self._announced_version or self._version)
        else:
            candidates = session_candidates(self._announced_version, self._version, scanned=scanned)
        self._key_refused = True
        for version in candidates:
            self._last_refusal = None
            device = await self._hass.async_add_executor_job(
                self._try_direct_connect, ip, version
            )
            if not is_key_error(self._last_refusal):
                self._key_refused = False
            elif report_key:
                self.refused_handshakes += 1
            if device:
                if version != self._version:
                    _LOGGER.info(
                        "LAN session with %s speaks protocol %s", self._device_id, version
                    )
                self._version = version
                # On the event loop, like the rest of _connect. An unconfirmed fallback keeps the last pair,
                # and a 3.3 session waits until it has proven it is this camera.
                protocol = confirmed_protocol(version, self._announced_version)
                if keyed_protocol(version):
                    self._address.update(ip, protocol)
                else:
                    self._pending_publish = (ip, protocol) if protocol else None
                return device
        # Reported only when every version tried refused the key: with two
        # candidates, a 914 on the one the camera does not speak would
        # otherwise ask the cloud for a key that has not changed.
        if report_key and self._key_refused and self._on_key_rejected:
            _LOGGER.debug("%s at %s refused the local key", self._device_id, ip)
            try:
                self._on_key_rejected()
            except Exception:
                _LOGGER.exception("Error in LAN key callback")
        return None

    async def _connect(self) -> bool:
        # Addresses known without a broadcast: the current (or seeded) one, the
        # last one that worked (the current one is cleared after a failed round,
        # and a camera rebooting for longer than that came back to it), then the
        # configured ones. A configured address may be another camera's, and a
        # lost address may have gone to another device meanwhile, so those
        # only count on a protocol whose handshake proves this camera's key.
        for ip in dict.fromkeys(a for a in (self._ip, self._own_ip, self._bound, *self._hosts) if a):
            own = ip in (self._ip, self._own_ip, self._bound)
            other = ip in self._hosts and not own
            if other and self._claims.held_by_other(ip, self._device_id):
                continue
            if self._foreign.skipped(ip, time.monotonic()):
                continue
            _LOGGER.debug("Trying direct LAN connection to %s at %s", self._device_id, ip)
            device = await self._connect_at_any_version(
                ip,
                scanned=False,
                verified=(ip in self._hosts or ip != self._ip) and ip != self._bound,
                report_key=own,
                bound=ip == self._bound,
            )
            if not device and other and self._key_refused:
                _LOGGER.debug("%s is another camera's address; skipping it for a while", ip)
                self._foreign.note(ip, time.monotonic())
            elif not device and own and self._key_refused:
                # Our own address now answers with another key: it went to
                # another camera (two cameras that swapped leases). Our claim
                # would keep that camera off its new address while its claim
                # on ours keeps us off, for good; release it.
                _LOGGER.debug("%s no longer takes the key of %s; releasing it", ip, self._device_id)
                self._claims.release(self._device_id)
                if ip == self._own_ip:
                    self._own_ip = None
            if device:
                self._ip = self._own_ip = ip
                self._device = device
                self._identity = self._new_identity()
                self._set_connected(True)
                self._claims.claim(ip, self._device_id)
                _LOGGER.info("LAN connected to %s at %s", self._device_id, ip)
                self._missing_reported = False
                return True
        self._ip = None

        # Scan for device IP
        _LOGGER.debug("Scanning LAN for device %s", self._device_id)
        self._ip, announced = await self._discover_device()
        if announced is not None and announced != self._announced_version:
            _LOGGER.debug(
                "Device %s announces protocol %s", self._device_id, announced
            )
            self._announced_version = announced
        if not self._ip:
            if self._missing_reported:
                _LOGGER.debug("Device %s still not found on LAN", self._device_id)
            else:
                self._missing_reported = True
                _LOGGER.warning(
                    "Device %s not found on LAN; retrying up to once a minute, logged at debug from here on",
                    self._device_id,
                )
            return False

        device = await self._connect_at_any_version(self._ip, scanned=True)
        if device:
            self._own_ip = self._ip
            self._device = device
            self._identity = self._new_identity()
            self._set_connected(True)
            self._claims.claim(self._ip, self._device_id)
            self._missing_reported = False
            _LOGGER.info("LAN connected to %s at %s", self._device_id, self._ip)
            return True

        _LOGGER.debug("LAN connection failed for %s at %s", self._device_id, self._ip)
        self._ip = None
        return False

    def _query_baseline(self) -> dict[str, Any] | None:
        """Read the monitor's whole DPS set on the session that just came up.

        A read and only a read: DP_QUERY (command 10) carries no data points, so
        it cannot change a setting. Sent only on a keyed session, where the
        monitor answers it (`keyed_protocol`); a 3.3 session leaves it
        unanswered, which would cost a socket timeout on every connect.

        tinytuya's status() returns the first frame with a payload whatever its
        command, so a push landing between the query and its answer (a SenseIQ
        status comes every few seconds) is taken for the baseline, and the
        answer then arrives as an ordinary push. That swap is harmless only
        because the alert keys are absent from the answer, as both the #51
        capture and the fork's SCD973/26 show: the answer arriving as a push
        fires nothing. A DPS 212 alarm taken for the baseline still fires, since
        the sensors judge 212 by its own timestamp; a 250 or 141 marker would be
        lost, and on 3.4+ those stayed empty. Telling the frames apart would
        mean reading tinytuya's raw messages for their command.
        """
        try:
            self._device.set_socketTimeout(SOCKET_TIMEOUT)
            try:
                result = self._device.status()
            finally:
                self._device.set_socketTimeout(RECEIVE_POLL)
        except Exception as ex:  # noqa: BLE001 - tinytuya raises socket and decode errors alike
            _LOGGER.debug("LAN baseline query failed: %s", ex)
            return None
        dps = baseline_dps(result)
        if dps is None:
            _LOGGER.debug(
                "LAN baseline query returned no state: %s",
                result.get("Error") if isinstance(result, dict) else result,
            )
        return dps

    async def _load_baseline(self) -> None:
        """Publish the full local state, so it need not wait for the cloud."""
        dps = await self._hass.async_add_executor_job(self._query_baseline)
        if dps:
            _LOGGER.debug("LAN baseline for %s carries %d keys", self._device_id, len(dps))
            self._publish(dps, baseline=True)

    def _announce_session(self) -> None:
        """Tell the coordinator a session is up, on the event loop."""
        if self._on_session_up is None:
            return
        try:
            self._on_session_up()
        except Exception:
            _LOGGER.exception("Error in LAN session callback")

    def _publish(self, dps: dict[str, Any], *, baseline: bool = False) -> None:
        """Hand state to the coordinator, on the event loop.

        A baseline is the answer to the query sent when the session came up:
        state, not news, so the coordinator does not treat it as an event.
        Every report also settles the commands waiting for their echo.
        """
        self._echoes.seen(dps)
        try:
            self._on_dps_update(dps, baseline)
        except Exception:
            _LOGGER.exception("Error in DPS %s callback", "baseline" if baseline else "update")

    def _send_heartbeat(self) -> tuple[bool, dict[str, Any] | None]:
        """Ping the device on the persistent socket.

        Returns whether the socket is still usable, and any state that came back
        in place of the ack (see `heartbeat_outcome`), for the caller to publish
        on the event loop.

        tinytuya does not keep persistent sockets alive by itself, so without
        this the monitor closes the connection after 30-45 seconds of silence
        (issue #62). Unlike status() and updatedps(), which this firmware either
        ignores or crashes on, a heartbeat is the plain keep-alive command.

        The response is consumed here (`nowait=False`) so a stray ack cannot turn
        up in the next receive() and look like a device error. A heartbeat the
        camera did not answer is a failure (`heartbeat_outcome`).
        """
        try:
            self._device.set_socketTimeout(SOCKET_TIMEOUT)
            try:
                result = self._device.heartbeat(nowait=False)
            finally:
                self._device.set_socketTimeout(RECEIVE_POLL)
        except Exception as ex:  # noqa: BLE001 - any failure here means the socket is unusable
            _LOGGER.debug("LAN heartbeat failed (%s)", ex)
            return False, None

        alive, pushed = heartbeat_outcome(result)
        if not alive:
            _LOGGER.debug("LAN heartbeat not answered: %s", result.get("Error", result.get("Err")))
        return alive, pushed

    async def _run(self) -> None:
        """The listener, restarted after anything unexpected instead of ending.

        The task is held, so an exception that escaped would end the camera's
        LAN session for good, with nothing logged until the client is dropped.
        """
        while not self._stop_event.is_set():
            try:
                await self._listen()
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # logged in full; the session must come back
                _LOGGER.exception("LAN listener of %s failed; reconnecting", self._device_id)
                self._disconnect()
                await self._interruptible_sleep(reconnect_delay(2))

    async def _listen(self) -> None:
        last_data = time.monotonic()
        last_heartbeat = last_data
        failures = 0
        payload_errors = 0

        while not self._stop_event.is_set():
            self._drop_session_if_rekeyed()
            if not self._device:
                # Under the lock a short command session takes too: a reconnect
                # and a command session of ours never overlap (OwnSession).
                async with self._own_session.lock:
                    connected = await self._connect()
                if not connected:
                    failures += 1
                    await self._interruptible_sleep(reconnect_delay(failures))
                    continue
                self._announce_session()
                # Before the receive loop, so the query never shares the socket
                # with a keep-alive.
                if keyed_protocol(self._version):
                    await self._load_baseline()
                last_data = time.monotonic()
                last_heartbeat = last_data
                payload_errors = 0

            if data_stale(time.monotonic(), last_data):
                _LOGGER.debug("No LAN data within the timeout, reconnecting")
                self._disconnect()
                continue

            # Commands handed to this session go out between two receives.
            if self._session_commands.pending:
                if not await self._session_commands.drain(self._send_on_session):
                    _LOGGER.debug("LAN session broke while sending a command, reconnecting")
                    self._disconnect()
                    failures += 1
                    await self._interruptible_sleep(reconnect_delay(failures))
                    continue
                last_data = time.monotonic()

            try:
                # Wait for the camera or a queued command, whichever comes
                # first; read only when the camera has sent something.
                sock = getattr(self._device, "socket", None)
                if sock is None:
                    data = await self._hass.async_add_executor_job(self._device.receive)
                else:
                    woke = await self._hass.async_add_executor_job(self._waker.wait, sock, RECEIVE_POLL)
                    if woke == LOOP_WAKE:
                        continue
                    data = await self._hass.async_add_executor_job(self._device.receive) if woke != LOOP_TIMEOUT else None
            except Exception as ex:  # noqa: BLE001 - tinytuya raises socket and decode errors alike
                _LOGGER.debug("LAN receive error (%s), reconnecting", ex)
                self._disconnect()
                failures += 1
                await self._interruptible_sleep(reconnect_delay(failures))
                continue

            if data and isinstance(data, dict):
                identity = self._identity.check(data)
                if identity == IDENTITY_WRONG:
                    self._not_this_camera()
                    failures += 1
                    await self._interruptible_sleep(reconnect_delay(failures))
                    continue
                if identity == IDENTITY_OK:
                    self._publish_when_proven()
                last_data = time.monotonic()

                if "Error" in data or "Err" in data:
                    payload_errors += 1
                    err_code = data.get("Err")
                    if should_reconnect(err_code, payload_errors):
                        _LOGGER.debug(
                            "LAN device error: %s, reconnecting",
                            data.get("Error", err_code),
                        )
                        self._disconnect()
                        failures += 1
                        await self._interruptible_sleep(reconnect_delay(failures))
                        continue
                    _LOGGER.debug(
                        "Ignoring LAN frame we could not read (%s), connection still up",
                        data.get("Error", err_code),
                    )
                else:
                    # A real exchange: the connection is healthy, and on 3.3
                    # a frame that decrypted proves the key as well.
                    failures = 0
                    payload_errors = 0
                    self._exchanged = True

                    if data.get("dps"):
                        self._publish(data["dps"])

            # Keep-alive. Runs after the receive so the two never share the
            # socket at the same time.
            now = time.monotonic()
            if heartbeat_due(now, last_heartbeat):
                alive, pushed = await self._hass.async_add_executor_job(self._send_heartbeat)
                if not alive:
                    self._disconnect()
                    failures += 1
                    await self._interruptible_sleep(reconnect_delay(failures))
                    continue
                if pushed:
                    self._exchanged = True
                    # A push read in place of the ack decrypted under our key: it settles a 3.3 session's identity too.
                    if self._identity.check({"dps": pushed}) == IDENTITY_OK:
                        self._publish_when_proven()
                    self._publish(pushed)
                last_heartbeat = now
                last_data = now
                failures = 0

    def _send_values(
        self, device: tinytuya.Device, items: list, *, nowait: bool = False
    ) -> tuple[tuple[list, Any] | None, list[dict]]:
        """Send each value on `device`, in order; (what failed or None, the pushes read meanwhile).

        Each value goes alone: the device ignores batched commands for certain
        codes (e.g. 201 + 202). On the persistent session the answer read may be
        a push the camera sent meanwhile rather than the ack: its values are
        returned so the caller publishes them, nothing is lost. With `nowait`
        nothing is read back.
        """
        pushes = []
        for index, (key, value) in enumerate(items):
            result = device.set_value(key, value, nowait=nowait)
            if not device_answered(result):
                return (items[index:], result), pushes
            if isinstance(result, dict) and isinstance(result.get("dps"), dict):
                pushes.append(result["dps"])
        return None, pushes

    def _send_on_session_job(self, items: list) -> tuple[tuple[list, Any] | None, list[dict]]:
        device = self._device
        if device is None:
            return (items, {"Error": "LAN session closed", "Err": LAN_ERR_CONNECT, "Payload": None}), []
        device.set_socketTimeout(SOCKET_TIMEOUT)
        try:
            return self._send_values(device, items)
        finally:
            device.set_socketTimeout(RECEIVE_POLL)

    async def _send_on_session(self, items: list) -> tuple[list, Any] | None:
        """Send on the persistent session; only ever called by the listener loop."""
        failed, pushes = await self._hass.async_add_executor_job(self._send_on_session_job, items)
        for dps in pushes:
            self._exchanged = True
            self._publish(dps)
        return failed

    async def set_dps(self, dps: dict, *, repeatable: bool = True) -> dict | None:
        """Send a DPS command, without ever holding two LAN sessions of ours.

        Over the persistent session while it is up (the listener sends it
        between two receives, without tinytuya retries); only while it is down
        over a short session of its own, never next to a reconnect
        (lan_policy.OwnSession). Sends each value individually, in order — the
        device ignores batched commands for certain codes (e.g. 201 + 202).

        One command at a time per camera, each value only once the camera has
        settled after the previous one, and sent once more in the background
        when its echo does not come (command_echo.py: the camera ignores a
        command that comes while it is busy); one that certainly did not reach
        the camera is tried again after a growing pause (lan_policy.CommandQueue).
        `repeatable` says whether sending it twice is harmless: a command that
        is not (a lullaby step) is never retried after it may have gone out
        (command_not_delivered), and on a short session it goes out without
        waiting for the reply, since tinytuya sends the frame again whenever
        the reply is slower than the socket timeout (#91).

        None when the command was not delivered, with the reason in
        `last_command_failure`, so the caller can decide about the cloud.
        """
        if not self._ip:
            return None

        def _send_short(items):
            d = tinytuya.Device(
                self._device_id,
                self._ip,
                self._local_key,
                version=self._version or PROTOCOL_VERSION_DEFAULT,
            )
            d.set_socketTimeout(SOCKET_TIMEOUT)
            d.set_socketRetryLimit(SOCKET_RETRY_LIMIT)
            d.set_socketRetryDelay(SOCKET_RETRY_DELAY)
            try:
                return self._send_values(d, items, nowait=not repeatable)
            finally:
                d.close()

        async def send_short(items):
            if self._stop_event.is_set():
                # Stopped (unloaded, or the session handed over): no session
                # of ours any more, not even a short one for a retry.
                return items, {"Error": "LAN client stopped", "Err": LAN_ERR_CONNECT, "Payload": None}
            failed, pushes = await self._hass.async_add_executor_job(_send_short, items)
            # Not published: only the listener's session hands state on. The
            # answer still shows whether the camera took the value.
            for pushed in pushes:
                self._echoes.seen(pushed)
            return failed

        async def send_one(part):
            return await self._own_session.send(
                list(part.items()),
                lambda: self._device is not None and self.connected,
                self._session_commands.submit,
                send_short,
            )

        async def send(items):
            for index, (key, value) in enumerate(items):
                failed = await send_with_echo(
                    send_one, {key: value}, self._echoes,
                    repeatable=repeatable, delivered=lambda f: f is None,
                )
                if failed is not None:
                    return [*failed[0], *items[index + 1:]], failed[1]
            return None

        try:
            outcome = await self._commands.run(send, list(dps.items()), repeatable=repeatable)
        except Exception as ex:  # noqa: BLE001 - a failed command falls back to the cloud path
            _LOGGER.warning("LAN set_dps failed: %s", ex)
            self.last_command_failure = None
            return None
        if not outcome.delivered:
            result = outcome.result if isinstance(outcome.result, dict) else {}
            _LOGGER.warning(
                "LAN command for DPS %s of %s was not delivered (%s: %s)",
                outcome.key, self._device_id, outcome.failure, result.get("Error", result.get("Err")),
            )
            self.last_command_failure = outcome.failure
            return None
        self.last_command_failure = None
        return {"success": True}

    async def _interruptible_sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
