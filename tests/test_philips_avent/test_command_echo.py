"""Keeping a camera's commands apart and checking their echo (command_echo.py).

The camera ignores a command that arrives while it is still busy with the
previous one, until its follow-up report about a second later. What must
hold: commands sent back to back all land; a single command returns as soon
as the camera answered; a missing echo leads to exactly one resend, and none
for a command that must not be sent twice; a session that ends during a wait
ends the wait, without a resend; nothing waits for ever.
"""

import asyncio

import pytest
from command_echo import ECHOED, LOST, NO_ECHO, EchoWatch, same_value, send_with_echo


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """The real timings divided by ten."""
    monkeypatch.setattr(EchoWatch, "echo_wait", 0.15)
    monkeypatch.setattr(EchoWatch, "settle_min", 0.05)
    monkeypatch.setattr(EchoWatch, "settle_max", 0.15)
    monkeypatch.setattr(EchoWatch, "follow_up_limit", 1.5)


def run(coro):
    return asyncio.run(coro)


class Camera:
    """Takes a command when not busy, echoes it after `echo_delay`, and is busy until its
    follow-up report `busy` later; a command that comes while it is busy is answered and ignored."""

    def __init__(self, watch, *, busy=0.1, echo_delay=0.002):
        self.watch, self.busy, self.echo_delay = watch, busy, echo_delay
        self.state: dict = {}
        self.received: list[dict] = []
        self._free_at = 0.0

    async def send(self, dps):
        loop = asyncio.get_running_loop()
        now = loop.time()
        self.received.append(dict(dps))
        if now >= self._free_at:
            self.state.update(dps)
            self._free_at = now + self.busy
            loop.call_later(self.echo_delay, self.watch.seen, dict(dps))
            loop.call_later(self.busy, self.watch.seen, dict(self.state))  # the follow-up report
        return None  # the frame itself is answered either way


async def settled(watch):
    await watch.follow_up_done()


class TestSameValue:
    @pytest.mark.parametrize(("reported", "written"), [(40, 40), ("40", 40), (40.0, 40), (True, True), ("Play", "play")])
    def test_same(self, reported, written):
        assert same_value(reported, written)

    @pytest.mark.parametrize(("reported", "written"), [(True, 1), (False, 0), (50, 40), ("1", True), (None, False)])
    def test_not_the_same(self, reported, written):
        assert not same_value(reported, written)


class TestEchoWatch:
    def test_the_written_values_end_the_wait(self):
        async def go():
            watch = EchoWatch()
            echo = watch.expect({"158": 40, 138: True})
            watch.seen({"158": 40})
            assert not echo.future.done()
            watch.seen({"138": True, "207": 2500})
            return await watch.wait(echo, 1), watch.waiting
        assert run(go()) == (ECHOED, 0)

    def test_a_report_of_the_old_value_is_no_echo(self):
        async def go():
            watch = EchoWatch()
            echo = watch.expect({"158": 40})
            watch.seen({"158": 50})
            return await watch.wait(echo, 0.02), watch.waiting
        assert run(go()) == (NO_ECHO, 0)

    def test_lost_ends_every_wait(self):
        async def go():
            watch = EchoWatch()
            echoes = [watch.expect({"1": True}), watch.expect({"2": True})]
            asyncio.get_running_loop().call_later(0.01, watch.lost)
            return [await watch.wait(e, 5) for e in echoes], watch.waiting
        assert run(go()) == ([LOST, LOST], 0)

    def test_the_echo_with_the_answer_does_not_count_as_settled(self):
        async def go():
            watch = EchoWatch()
            loop = asyncio.get_running_loop()
            await watch.settle()
            watch.seen({"158": 40})  # at once: the echo, not the follow-up report
            started = loop.time()
            await watch.settle()
            return loop.time() - started
        assert run(go()) >= 0.14  # waited the whole settle_max

    def test_a_later_report_settles_the_camera(self):
        async def go():
            watch = EchoWatch()
            loop = asyncio.get_running_loop()
            await watch.settle()
            loop.call_later(0.07, watch.seen, {"207": 2500})
            started = loop.time()
            await watch.settle()
            return loop.time() - started
        assert 0.05 <= run(go()) < 0.12


class TestSendWithEcho:
    def test_commands_back_to_back_all_land(self):
        async def go():
            watch = EchoWatch()
            camera = Camera(watch)
            # Straight after each other the second is lost: the camera model is right.
            await camera.send({"158": 40})
            await camera.send({"138": False})
            assert camera.state == {"158": 40}
            results = []
            for echo_delay in (0.002, 0.1):  # the echo with the answer, or with the follow-up report
                watch = EchoWatch()
                camera = Camera(watch, echo_delay=echo_delay)
                for dps in ({"158": 40}, {"138": False}, {"158": 70}):
                    await send_with_echo(camera.send, dps, watch, repeatable=True)
                await settled(watch)
                results.append((camera.state, len(camera.received)))
            return results
        assert run(go()) == [({"158": 70, "138": False}, 3), ({"158": 70, "138": False}, 3)]

    def test_a_camera_busy_longer_than_the_settle_wait_still_takes_all(self):
        async def go():
            watch = EchoWatch()
            # Busy longer than settle_max (no report in time): the resend, after the echo wait, brings it.
            camera = Camera(watch, busy=0.25)
            for dps in ({"158": 40}, {"138": False}):
                await send_with_echo(camera.send, dps, watch, repeatable=True)
            await settled(watch)
            return camera.state
        assert run(go()) == {"158": 40, "138": False}

    def test_a_single_command_returns_as_soon_as_it_is_answered(self):
        async def go():
            watch = EchoWatch()
            loop = asyncio.get_running_loop()

            async def silent(dps):
                return "answered"
            started = loop.time()
            result = await send_with_echo(silent, {"138": True}, watch, repeatable=True)
            return result, loop.time() - started
        result, took = run(go())
        assert result == "answered" and took < 0.05

    def test_a_missing_echo_means_exactly_one_resend(self):
        sent = []

        async def go():
            watch = EchoWatch()

            async def silent(dps):
                sent.append(dict(dps))
            await send_with_echo(silent, {"138": True}, watch, repeatable=True)
            await settled(watch)
            return watch.waiting
        assert run(go()) == 0
        assert sent == [{"138": True}, {"138": True}]

    def test_only_the_missing_values_go_again(self):
        sent = []

        async def go():
            watch = EchoWatch()

            async def half(dps):
                sent.append(dict(dps))
                watch.seen({"161": 1})
            await send_with_echo(half, {"161": 1, "162": 600}, watch, repeatable=True)
            await settled(watch)
        run(go())
        assert sent == [{"161": 1, "162": 600}, {"162": 600}]

    def test_a_step_is_never_sent_twice(self):
        sent = []

        async def go():
            watch = EchoWatch()

            async def silent(dps):
                sent.append(dict(dps))
            await send_with_echo(silent, {"201": "next"}, watch, repeatable=False)
            await settled(watch)
        run(go())
        assert sent == [{"201": "next"}]

    def test_the_next_command_waits_for_the_follow_up(self):
        sent = []

        async def go():
            watch = EchoWatch()

            async def silent(dps):
                sent.append(dict(dps))
            await send_with_echo(silent, {"138": True}, watch, repeatable=True)
            await send_with_echo(silent, {"158": 40}, watch, repeatable=True)
            await settled(watch)
        run(go())
        # The first one's resend before the second, never after it.
        assert sent == [{"138": True}, {"138": True}, {"158": 40}, {"158": 40}]

    def test_an_undelivered_command_is_returned_at_once(self):
        sent = []
        refused = ([("138", True)], {"Err": "905"})

        async def go():
            watch = EchoWatch()

            async def refuse(dps):
                sent.append(dps)
                return refused
            result = await send_with_echo(refuse, {"138": True}, watch, repeatable=True, delivered=lambda f: f is None)
            await settled(watch)
            return result, watch.waiting
        assert run(go()) == (refused, 0)
        assert len(sent) == 1

    def test_a_failed_resend_changes_nothing(self):
        calls = []

        async def go():
            watch = EchoWatch()

            async def flaky(dps):
                calls.append(dps)
                if len(calls) > 1:
                    raise ConnectionError("gone")
                return "answered"
            result = await send_with_echo(flaky, {"138": True}, watch, repeatable=True)
            await settled(watch)
            return result, watch.waiting
        assert run(go()) == ("answered", 0)
        assert len(calls) == 2

    def test_a_send_that_raises_leaves_nothing_waiting(self):
        async def go():
            watch = EchoWatch()

            async def broken(dps):
                raise ConnectionError("gone")
            with pytest.raises(ConnectionError):
                await send_with_echo(broken, {"138": True}, watch, repeatable=True)
            return watch.waiting
        assert run(go()) == 0

    def test_no_deadlock_and_no_resend_when_the_session_drops_during_the_wait(self, monkeypatch):
        monkeypatch.setattr(EchoWatch, "echo_wait", 5)
        monkeypatch.setattr(EchoWatch, "settle_max", 5)
        sent = []

        async def go():
            watch = EchoWatch()

            async def silent(dps):
                sent.append(dps)
            await send_with_echo(silent, {"138": True}, watch, repeatable=True)
            asyncio.get_running_loop().call_later(0.01, watch.lost)
            # The next command neither waits for the echo nor for the camera to settle.
            await asyncio.wait_for(send_with_echo(silent, {"158": 40}, watch, repeatable=False), 1)
            watch.lost()  # this one's session is gone as well
            await asyncio.wait_for(settled(watch), 1)
            return watch.waiting
        assert run(go()) == 0
        assert sent == [{"138": True}, {"158": 40}]

    def test_a_stuck_follow_up_does_not_block_the_camera(self, monkeypatch):
        monkeypatch.setattr(EchoWatch, "follow_up_limit", 0.2)
        sent = []

        async def go():
            watch = EchoWatch()

            async def hanging(dps):
                sent.append(dps)
                if len(sent) == 2:  # the resend never returns
                    await asyncio.sleep(3600)
            await send_with_echo(hanging, {"138": True}, watch, repeatable=True)
            await asyncio.wait_for(send_with_echo(hanging, {"158": 40}, watch, repeatable=False), 1)
            watch.close()
        run(go())
        assert sent == [{"138": True}, {"138": True}, {"158": 40}]

    def test_after_close_nothing_waits(self, monkeypatch):
        monkeypatch.setattr(EchoWatch, "echo_wait", 5)
        monkeypatch.setattr(EchoWatch, "settle_max", 5)
        sent = []

        async def go():
            watch = EchoWatch()
            watch.close()

            async def silent(dps):
                sent.append(dps)
            for dps in ({"138": True}, {"158": 40}):
                await asyncio.wait_for(send_with_echo(silent, dps, watch, repeatable=True), 1)
            await asyncio.wait_for(settled(watch), 1)
            return watch.waiting
        assert run(go()) == 0
        assert sent == [{"138": True}, {"158": 40}]
