"""The "LAN connection while streaming" option: separate or shared.

A camera accepts three LAN (6668) sessions at most (an SCD951, measured
02-11-2026), and its parent unit may need one. Separate (the default): the
integration keeps its own session, and the bridge opens one more while it
streams the camera. Shared: while the bridge streams it, the integration hands
its control traffic over to the bridge's session (the bridge's
pkg/rtsp/share.go) and closes its own, so this house holds one session to the
camera; when the stream ends it takes its own session back.

No push may get lost on the way, so the order is fixed (ShareHandover):

- taking over: subscribe to the bridge's push stream, wait for its "hello",
  which carries a fresh query of all points and so proves the bridge's
  session delivers data, and only then close the own session;
- giving back: open the own session and wait until it is connected, and only
  then drop the subscription. The bridge keeps an ended stream's session open
  until the subscriber has left (at most 15 s).

During either overlap both deliver: alarm records are de-duplicated by their
timestamp, and lullaby and settings pushes are the same value twice. Commands
wait while a handover runs. Anything that goes wrong ends in the own session.

A camera that serves one LAN session at a time (protocol 3.3: it accepted the
bridge's connection while ours was open and answered nothing on it,
02-11-2026) cannot overlap, so there the order is reversed: when the bridge
wants to start a direct stream it says so in /status (wants_session); the
integration closes its own session and confirms (POST /cameras/<id>/release),
the bridge connects, and the integration subscribes once the stream runs. At
the end the bridge closes first ("ending" with exclusive) and the integration
reconnects. If the bridge does not take the session within RELEASE_RETURN_
SECONDS, or reports no direct stream any more, the own session comes back.
In the gap, commands wait for either session (COMMAND_WAIT_SECONDS) and then
go through the cloud; this camera's alerts come from the cloud poll anyway.

Kept free of Home Assistant imports so the unit tests can load it.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

if __package__:
    from .command_echo import EchoWatch, send_with_echo
else:  # loaded on its own, as the unit tests do
    from command_echo import EchoWatch, send_with_echo

_LOGGER = logging.getLogger(__name__)

LAN_SHARE_SEPARATE = "separate"
LAN_SHARE_SHARED = "shared"
LAN_SHARE_MODES = (LAN_SHARE_SEPARATE, LAN_SHARE_SHARED)
DEFAULT_LAN_SHARE = LAN_SHARE_SEPARATE

# The bridge pings an idle event stream every 10 s; three missed pings is a
# stall, and the own session comes back.
EVENT_STALL_SECONDS = 30
# How long the bridge may take to answer the subscription with its hello.
HELLO_TIMEOUT_SECONDS = 10
# How long giving back waits for the own session before it lets go anyway.
OWN_CONNECT_TIMEOUT_SECONDS = 20
# How long a command waits for a running handover.
COMMAND_WAIT_SECONDS = 15
COMMAND_TIMEOUT_SECONDS = 10
# How long a session released for the bridge may stay unused before it comes
# back: the bridge waits 6 s for the release, then needs a few seconds for its
# handshake, offer and first video (12 s at most, lan_exclusive.go), and the
# status poll runs every 2 s.
RELEASE_RETURN_SECONDS = 25
# After a release the bridge stops asking and connects; until it streams it
# shows no local path. Ours comes back before RELEASE_RETURN_SECONDS only when
# the bridge reports WebRTC (it gave up on the direct start) or after this
# grace, longer than the bridge's 5 s handshake: at 2 s our session came back
# during the bridge's attempt and could take the one-session camera from it
# (02-11-2026, 21:51).
RELEASE_GRACE_SECONDS = 10
# However long the bridge still says its direct start is connecting, our session
# comes back this long after the release: the bridge's own start gives up after
# 18 s at most, so a status still asking after that is a fault on its side.
RELEASE_MAX_WAIT_SECONDS = 90

PATH_OWN = "own"
PATH_VIA_BRIDGE = "via_bridge"
PATH_NONE = "none"
CONNECTION_PATHS = (PATH_OWN, PATH_VIA_BRIDGE, PATH_NONE)


def share_base_url(host: str, rtsp_port: int) -> str:
    """The bridge's status port: its RTSP host, the next port."""
    return f"http://{host}:{rtsp_port + 1}"


def events_url(host: str, rtsp_port: int, camera_id: str) -> str:
    return f"{share_base_url(host, rtsp_port)}/cameras/{camera_id}/events"


def dps_url(host: str, rtsp_port: int, camera_id: str) -> str:
    return f"{share_base_url(host, rtsp_port)}/cameras/{camera_id}/dps"


def release_url(host: str, rtsp_port: int, camera_id: str) -> str:
    return f"{share_base_url(host, rtsp_port)}/cameras/{camera_id}/release"


async def post_release(session: Any, url: str, token: str) -> bool:
    """Tell the bridge our own session is closed; whether a start was waiting for it."""
    import aiohttp

    try:
        async with session.post(
            url, headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=COMMAND_TIMEOUT_SECONDS),
        ) as response:
            return response.status == 204
    except (aiohttp.ClientError, TimeoutError):
        return False


def lan_share_mode(value: object) -> str:
    return value if value in LAN_SHARE_MODES else DEFAULT_LAN_SHARE


class SSEParser:
    """Server-Sent Events, fed in chunks: complete events come out as dicts.

    Comment lines (the bridge's pings) yield nothing but still count as
    traffic for the stall check, which the caller does per chunk.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._event = ""
        self._data: list[str] = []

    def feed(self, chunk: str) -> list[dict[str, Any]]:
        self._buffer += chunk
        out: list[dict[str, Any]] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                if self._data:
                    try:
                        doc = json.loads("\n".join(self._data))
                    except ValueError:
                        doc = None
                    if isinstance(doc, dict):
                        doc.setdefault("type", self._event or "message")
                        out.append(doc)
                self._event, self._data = "", []
            elif line.startswith(":"):
                continue
            elif line.startswith("event:"):
                self._event = line[6:].strip()
            elif line.startswith("data:"):
                self._data.append(line[5:].lstrip())
        if len(self._buffer) > 1 << 20:
            # A line that never ends: drop it rather than grow without bound.
            self._buffer = ""
        return out


class ShareHandover:
    """The order of a handover, with every step injected.

    ``subscribe`` returns once the bridge's hello arrived (and raises if it
    did not); ``close_own`` and ``open_own`` stop and start the own session,
    ``open_own`` returning only once it is connected (or raising);
    ``unsubscribe`` drops the bridge subscription.
    """

    def __init__(
        self,
        subscribe: Callable[[], Awaitable[None]],
        unsubscribe: Callable[[], Awaitable[None]],
        close_own: Callable[[], Awaitable[None]],
        open_own: Callable[[], Awaitable[None]],
    ) -> None:
        self._subscribe = subscribe
        self._unsubscribe = unsubscribe
        self._close_own = close_own
        self._open_own = open_own
        self.lock = asyncio.Lock()
        self.shared = False
        self._failure_logged = False

    async def take(self) -> bool:
        """Subscribe, then close the own session. False leaves the own session as it was."""
        async with self.lock:
            if self.shared:
                return True
            try:
                await self._subscribe()
            except Exception as err:  # noqa: BLE001 - any failure keeps the own session
                self._log_once("the bridge's session could not take over (%s); keeping our own", err)
                await self._quietly(self._unsubscribe)
                return False
            try:
                await self._close_own()
            except Exception as err:  # noqa: BLE001
                self._log_once("closing our own session failed (%s); keeping it", err)
                await self._quietly(self._unsubscribe)
                return False
            self.shared = True
            return True

    async def give_back(self) -> None:
        """Open the own session, then drop the subscription."""
        async with self.lock:
            if not self.shared:
                return
            try:
                await self._open_own()
            except Exception as err:  # noqa: BLE001 - let go anyway; the own session keeps retrying
                self._log_once("our own session did not come back in time (%s); it keeps trying", err)
            await self._quietly(self._unsubscribe)
            self.shared = False

    async def _quietly(self, step: Callable[[], Awaitable[None]]) -> None:
        try:
            await step()
        except Exception as err:  # noqa: BLE001 - cleanup must not raise
            _LOGGER.debug("Ignoring an error while dropping the bridge subscription: %s", err)

    def _log_once(self, message: str, err: Exception) -> None:
        if self._failure_logged:
            _LOGGER.debug(message, err)
            return
        self._failure_logged = True
        _LOGGER.warning(message, err)


class BridgeLink:
    """The bridge's push stream and command endpoint for one camera.

    ``session`` is an aiohttp ClientSession. The token travels only in the
    Authorization header and never reaches a log line. Commands go one at a
    time, each once the camera has settled after the previous one, and one
    without an echo in the push stream goes once more (command_echo.py).
    """

    def __init__(
        self,
        session: Any,
        events: str,
        commands: str,
        token: str,
        on_dps: Callable[[dict[str, Any]], None],
        on_end: Callable[[str], None],
    ) -> None:
        self._session = session
        self._events = events
        self._commands = commands
        self._headers = {"Authorization": f"Bearer {token}"}
        self._on_dps = on_dps
        self._on_end = on_end
        self._task: asyncio.Task | None = None
        self._closing = False
        self._command_lock = asyncio.Lock()
        self._echoes = EchoWatch()

    async def open(self) -> dict[str, Any]:
        """Subscribe and return the hello's data points; raises without a hello."""
        hello: asyncio.Future = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._read(hello))
        try:
            return await asyncio.wait_for(asyncio.shield(hello), HELLO_TIMEOUT_SECONDS)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        self._closing = True
        self._echoes.close()
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _read(self, hello: asyncio.Future) -> None:
        import aiohttp

        reason = "the bridge closed the event stream"
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
            async with self._session.get(self._events, headers=self._headers, timeout=timeout) as response:
                if response.status != 200:
                    raise ConnectionError(f"the bridge answered {response.status}")
                parser = SSEParser()
                while True:
                    chunk = await asyncio.wait_for(response.content.readany(), EVENT_STALL_SECONDS)
                    if not chunk:
                        break
                    for event in parser.feed(chunk.decode("utf-8", "replace")):
                        kind = event.get("type")
                        dps = event.get("dps") if isinstance(event.get("dps"), dict) else None
                        if kind == "hello" and not hello.done():
                            hello.set_result(dps or {})
                        elif kind == "dps" and dps:
                            self._echoes.seen(dps)
                            self._on_dps(dps)
                        elif kind == "ending":
                            reason = "the bridge ended the stream"
                            self._on_end(reason)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            reason = f"no word from the bridge for {EVENT_STALL_SECONDS} s"
        except Exception as err:  # noqa: BLE001 - any failure ends the link
            reason = f"the event stream failed ({type(err).__name__})"
            if not hello.done():
                hello.set_exception(err)
                return
        finally:
            # No more pushes: no command waits for its echo any more.
            self._echoes.close()
        if not hello.done():
            hello.set_exception(ConnectionError(reason))
            return
        if not self._closing:
            self._on_end(reason)

    async def set_dps(self, dps: dict[str, Any], *, repeatable: bool = True) -> None:
        """Send a command through the bridge's session; raises when it did not arrive.

        Once the camera has settled after the previous command; sent once
        more in the background without an echo when `repeatable` (a lullaby
        step is not).
        """
        async with self._command_lock:
            await send_with_echo(self._post, dps, self._echoes, repeatable=repeatable)

    async def _post(self, dps: dict[str, Any]) -> None:
        import aiohttp

        async with self._session.post(
            self._commands, headers=self._headers, json={"dps": dps},
            timeout=aiohttp.ClientTimeout(total=COMMAND_TIMEOUT_SECONDS),
        ) as response:
            try:
                body = await response.json(content_type=None)
            except ValueError:
                body = {}
            if response.status != 200 or not (isinstance(body, dict) and body.get("ok")):
                error = body.get("error") if isinstance(body, dict) else None
                raise ConnectionError(f"the bridge could not send it ({response.status}{': ' + str(error) if error else ''})")
