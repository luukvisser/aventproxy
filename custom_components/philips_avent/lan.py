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
    LAN_ERR_CONNECT,
    LOOP_TIMEOUT,
    LOOP_WAKE,
    PROTOCOL_VERSION_DEFAULT,
    SOCKET_RETRY_DELAY,
    SOCKET_RETRY_LIMIT,
    AddressPublisher,
    CommandQueue,
    LoopWaker,
    OwnSession,
    SessionCommands,
    baseline_dps,
    confirmed_protocol,
    data_stale,
    device_answered,
    heartbeat_due,
    heartbeat_outcome,
    keyed_protocol,
    parse_protocol_version,
    reconnect_delay,
    seeded_session,
    session_candidates,
    should_reconnect,
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
    ) -> None:
        self._hass = hass
        self._device_id = device_id
        self._local_key = local_key
        self._on_dps_update = on_dps_update
        self._on_session_up = on_session_up
        self._device: tinytuya.Device | None = None
        self._address = AddressPublisher(on_address_change, last_ip, last_protocol)
        # A keyed pair from the last run is dialled before any scan; see seeded_session.
        self._ip, self._announced_version = seeded_session(last_ip, last_protocol)
        self._version: float | None = None
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._rekeyed = False
        # Set once the live session has decrypted a frame; see `proven`.
        self._exchanged = False
        self.connected: bool = False
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
        """Whether the live session has shown that it holds the right key.

        From 3.4 up the handshake proves it. Below, a connect is bare TCP, so it
        takes a frame that decrypted: until then a 3.5 monitor on an unconfirmed
        3.3 fallback, or a 3.3 monitor straight after a key rotation, would look
        live while delivering nothing.
        """
        return self.connected and (keyed_protocol(self._version) or self._exchanged)

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

    def _disconnect(self) -> None:
        if self._device:
            try:
                self._device.close()
            except Exception as ex:  # noqa: BLE001 - teardown must not raise, whatever tinytuya throws
                _LOGGER.debug("Ignoring error while closing the LAN socket: %s", ex)
            self._device = None
        self._session_commands.fail_all()
        self._echoes.lost()
        self.connected = False
        self._exchanged = False

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
            d.close()
        except Exception as ex:  # noqa: BLE001 - tinytuya raises socket, decode and key errors here
            _LOGGER.debug("Direct LAN connection to %s at protocol %s failed: %s", ip, version, ex)
        return None

    async def _connect_at_any_version(self, ip: str, *, scanned: bool) -> tinytuya.Device | None:
        """Connect to a known IP, trying the announced protocol then the default."""
        for version in session_candidates(self._announced_version, self._version, scanned=scanned):
            device = await self._hass.async_add_executor_job(
                self._try_direct_connect, ip, version
            )
            if device:
                if version != self._version:
                    _LOGGER.info(
                        "LAN session with %s speaks protocol %s", self._device_id, version
                    )
                self._version = version
                # On the event loop, like the rest of _connect. An unconfirmed fallback keeps the last pair.
                self._address.update(ip, confirmed_protocol(version, self._announced_version))
                return device
        return None

    async def _connect(self) -> bool:
        # Try the cached or seeded IP first (direct TCP, no broadcast needed)
        if self._ip:
            _LOGGER.debug("Trying direct LAN connection to %s at %s", self._device_id, self._ip)
            device = await self._connect_at_any_version(self._ip, scanned=False)
            if device:
                self._device = device
                self.connected = True
                self._missing_reported = False
                _LOGGER.info("LAN reconnected to %s at %s", self._device_id, self._ip)
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
            self._device = device
            self.connected = True
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
