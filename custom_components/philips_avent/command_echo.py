"""A camera's echo of a LAN command, and keeping its commands apart.

An SCD923/SCD973 takes one command at a time: one that arrives while it is
still busy with the previous one is answered and then ignored, with no echo
and the old value in its status. Measured 05-11-2026 on the LAN, on its own
session and through the bridge's: the night light's brightness and then the
light off, 0.1 s apart and the off sent again 0.3 s later, left the light on;
the status light switched off 0.85 s after the previous send, before the
camera's next report, was ignored. The camera reports a command it takes, its
echo, either with the answer or about 1 to 1.2 s later, and sends a full
report about 1 s after each command (once 2 s); a command sent after that
report lands.

So for each camera:

- the next command waits until the camera has settled: a report that comes
  at least SETTLE_MIN after the previous send (the echo that comes with the
  answer does not count), or SETTLE_MAX after it at most;
- a command returns to its caller once the camera answered the frame, as
  before; its echo is awaited in the background, ECHO_WAIT at most, and
  without one the missing values are sent once more (when the camera has
  settled), but only when sending them twice is harmless (an absolute value;
  a relative lullaby step would skip a track);
- the next command waits for that follow-up to end (FOLLOW_UP_LIMIT at most).

A single command costs nothing more; a burst goes out about a second per
command. A value the camera reports differently from how it was written
(clamped) costs one harmless resend.

When the session ends (`EchoWatch.lost`) nothing is sent again here: the
command was answered, and the session's owner deals with the connection.

Kept free of Home Assistant so the unit tests can load it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)

# How long the background follow-up waits for a command's echo.
ECHO_WAIT = 1.5
# A report this long after a send means the camera is done with that command.
SETTLE_MIN = 0.5
# The longest a command waits for the camera to settle after the previous send.
SETTLE_MAX = 1.5
# The longest a command waits for the previous one's follow-up (a resend can
# wait for its own connection), so a stuck follow-up never blocks the camera.
FOLLOW_UP_LIMIT = 15.0

ECHOED = "echoed"
NO_ECHO = "no_echo"
LOST = "lost"


def same_value(reported: Any, written: Any) -> bool:
    """Whether a reported value is the written one (the camera may report 40 for "40")."""
    if isinstance(reported, bool) or isinstance(written, bool):
        return reported is written
    if reported == written:
        return True
    try:
        return float(reported) == float(written)
    except (TypeError, ValueError):
        return str(reported).strip().lower() == str(written).strip().lower()


class Echo:
    """The values of one command still waiting for the camera's report."""

    __slots__ = ("future", "missing")

    def __init__(self, dps: dict[str, Any], future: asyncio.Future) -> None:
        self.missing = {str(k): v for k, v in dps.items()}
        self.future = future


class EchoWatch:
    """One camera's commands: their echoes, the camera's settling, the follow-up.

    `seen` is fed every report the session delivers (pushes, and state read
    back in place of an ack); `lost` when the session ends, and `close` when
    no report will ever come again (a link that is gone for good). All on the
    event loop. The timings are class attributes so tests can shorten them.
    """

    echo_wait = ECHO_WAIT
    settle_min = SETTLE_MIN
    settle_max = SETTLE_MAX
    follow_up_limit = FOLLOW_UP_LIMIT

    def __init__(self) -> None:
        self._waiting: list[Echo] = []
        self._last_send: float | None = None
        self._settled: asyncio.Event | None = None
        self._follow_up: asyncio.Task | None = None
        self._closed = False

    # Echoes

    def expect(self, dps: dict[str, Any]) -> Echo:
        """Start waiting for `dps`; call before sending, the echo can come with the answer."""
        echo = Echo(dps, asyncio.get_running_loop().create_future())
        if self._closed:
            echo.future.set_result(LOST)
        elif not echo.missing:
            echo.future.set_result(ECHOED)
        else:
            self._waiting.append(echo)
        return echo

    def seen(self, dps: dict[str, Any]) -> None:
        if (
            self._settled is not None
            and self._last_send is not None
            and asyncio.get_running_loop().time() - self._last_send >= self.settle_min
        ):
            self._settled.set()
        for echo in list(self._waiting):
            for raw_key, value in dps.items():
                key = str(raw_key)
                if key in echo.missing and same_value(value, echo.missing[key]):
                    del echo.missing[key]
            if not echo.missing:
                self._finish(echo, ECHOED)

    def lost(self) -> None:
        """The session ended: nobody waits for an echo, or a report, that cannot come."""
        for echo in list(self._waiting):
            self._finish(echo, LOST)
        if self._settled is not None:
            self._settled.set()

    def close(self) -> None:
        """No reports from here on: every wait, now and later, ends at once."""
        self._closed = True
        self.lost()
        if self._follow_up is not None and not self._follow_up.done():
            self._follow_up.cancel()

    def discard(self, echo: Echo) -> None:
        if echo in self._waiting:
            self._waiting.remove(echo)

    @property
    def waiting(self) -> int:
        return len(self._waiting)

    async def wait(self, echo: Echo, timeout: float) -> str:
        """ECHOED, LOST, or NO_ECHO after `timeout`; the echo is no longer watched afterwards."""
        try:
            if not echo.future.done():
                await asyncio.wait_for(asyncio.shield(echo.future), timeout)
        except TimeoutError:
            pass
        finally:
            self.discard(echo)
        return echo.future.result() if echo.future.done() else NO_ECHO

    def _finish(self, echo: Echo, outcome: str) -> None:
        self.discard(echo)
        if not echo.future.done():
            echo.future.set_result(outcome)

    # Keeping commands apart

    async def settle(self) -> None:
        """Wait until the camera is done with the previous send, then count this one as sent now."""
        loop = asyncio.get_running_loop()
        if self._last_send is not None and not self._closed:
            remaining = self._last_send + self.settle_max - loop.time()
            if remaining > 0 and self._settled is not None and not self._settled.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._settled.wait(), remaining)
        self._last_send = loop.time()
        self._settled = asyncio.Event()

    async def follow_up_done(self) -> None:
        """Wait for the previous command's follow-up (its echo, or its resend)."""
        task = self._follow_up
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=self.follow_up_limit)

    def follow(self, coro: Awaitable[None]) -> None:
        self._follow_up = asyncio.ensure_future(coro)


async def send_with_echo(
    send: Callable[[dict[str, Any]], Awaitable[Any]],
    dps: dict[str, Any],
    watch: EchoWatch,
    *,
    repeatable: bool,
    delivered: Callable[[Any], bool] = lambda _result: True,
) -> Any:
    """Send `dps` once the camera is ready for it; its echo and any resend follow in the background.

    `send` raises or returns a result that `delivered` judges; a command that
    was not delivered is returned at once, for the caller's own retry rules.
    The first send's result is returned: a resend only makes sure, and its
    failure changes nothing about a command the camera already answered.
    """
    await watch.follow_up_done()
    await watch.settle()
    echo = watch.expect(dps)
    try:
        result = await send(dps)
    except BaseException:
        watch.discard(echo)
        raise
    if not delivered(result):
        watch.discard(echo)
        return result
    watch.follow(_follow_up(send, echo, watch, repeatable, delivered))
    return result


async def _follow_up(
    send: Callable[[dict[str, Any]], Awaitable[Any]],
    echo: Echo,
    watch: EchoWatch,
    repeatable: bool,
    delivered: Callable[[Any], bool],
) -> None:
    outcome = await watch.wait(echo, watch.echo_wait)
    if outcome != NO_ECHO:
        return
    if not repeatable:
        _LOGGER.debug("No echo of DPS %s within %.1f s; not sent again (not repeatable)", list(echo.missing), watch.echo_wait)
        return
    again = dict(echo.missing)
    _LOGGER.debug("No echo of DPS %s within %.1f s; sending it once more", list(again), watch.echo_wait)
    await watch.settle()
    second = watch.expect(again)
    try:
        if delivered(await send(again)) and await watch.wait(second, watch.echo_wait) == NO_ECHO:
            _LOGGER.debug("Still no echo of DPS %s after the resend", list(second.missing))
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001 - the first send was answered; the resend only makes sure
        _LOGGER.debug("Resending DPS %s failed (%s)", list(again), err)
    finally:
        watch.discard(second)
