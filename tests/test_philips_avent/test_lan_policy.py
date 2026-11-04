"""Tests for the LAN keep-alive and reconnect policy (issue #62)."""

import asyncio

from lan_policy import (
    COMMAND_ATTEMPTS,
    COMMAND_BUSY,
    COMMAND_DEADLINE,
    COMMAND_NO_ANSWER,
    COMMAND_REFUSED,
    COMMAND_RETRY_DELAYS,
    CommandQueue,
    cloud_may_repeat,
    DATA_TIMEOUT,
    HEARTBEAT_INTERVAL,
    KEYED_PROTOCOL_MIN_VERSION,
    LAN_ERR_CONNECT,
    LAN_ERR_OFFLINE,
    LAN_ERR_PAYLOAD,
    SOCKET_RETRY_LIMIT,
    MAX_PAYLOAD_ERRORS,
    PROTOCOL_VERSION_DEFAULT,
    RECONNECT_DELAY,
    RECONNECT_DELAY_MAX,
    baseline_dps,
    command_not_delivered,
    data_stale,
    device_answered,
    heartbeat_due,
    heartbeat_outcome,
    is_connection_error,
    keyed_protocol,
    parse_protocol_version,
    reconnect_delay,
    seeded_session,
    session_candidates,
    should_reconnect,
    version_candidates,
)

# tinytuya blocks in receive() for up to this long, so a heartbeat can be late
# by that much. Mirrors SOCKET_TIMEOUT in lan.py.
SOCKET_TIMEOUT = 5

# The monitor closes an idle socket after this long at the earliest.
DEVICE_IDLE_CLOSE = 30


class TestHeartbeatInterval:
    def test_ping_lands_well_inside_the_device_idle_window(self):
        # Worst case: a heartbeat becomes due right after a receive started.
        assert HEARTBEAT_INTERVAL + SOCKET_TIMEOUT < DEVICE_IDLE_CLOSE

    def test_due_only_after_the_interval(self):
        assert not heartbeat_due(now=100.0, last_heartbeat=100.0)
        assert not heartbeat_due(now=100.0 + HEARTBEAT_INTERVAL - 0.1, last_heartbeat=100.0)
        assert heartbeat_due(now=100.0 + HEARTBEAT_INTERVAL, last_heartbeat=100.0)
        assert heartbeat_due(now=200.0, last_heartbeat=100.0)

    def test_interval_is_overridable(self):
        assert heartbeat_due(now=5.0, last_heartbeat=0.0, interval=4.0)
        assert not heartbeat_due(now=5.0, last_heartbeat=0.0, interval=6.0)


class TestDataWatchdog:
    def test_stale_only_after_the_timeout(self):
        assert not data_stale(now=100.0, last_data=100.0)
        assert not data_stale(now=100.0 + DATA_TIMEOUT, last_data=100.0)
        assert data_stale(now=100.0 + DATA_TIMEOUT + 0.1, last_data=100.0)

    def test_answered_heartbeats_keep_the_watchdog_quiet(self):
        # Heartbeats refresh last_data, so on a healthy connection the watchdog
        # never fires even when the monitor pushes no DPS updates.
        last_data = 0.0
        for tick in range(1, 500):
            now = tick * HEARTBEAT_INTERVAL
            assert not data_stale(now, last_data)
            last_data = now


class TestReconnectDelay:
    def test_first_failure_retries_immediately(self):
        # A device that just closed the socket is usually ready again at once,
        # and waiting keeps the camera on cloud polling for longer than needed.
        assert reconnect_delay(1) == 0.0

    def test_later_failures_back_off(self):
        assert reconnect_delay(2) == RECONNECT_DELAY
        assert reconnect_delay(3) == 2 * RECONNECT_DELAY
        assert reconnect_delay(4) == RECONNECT_DELAY_MAX
        assert reconnect_delay(7) == RECONNECT_DELAY_MAX
        assert reconnect_delay(1000) == RECONNECT_DELAY_MAX

    def test_backoff_stays_within_a_minute(self):
        assert RECONNECT_DELAY_MAX == 60.0
        assert max(reconnect_delay(n) for n in range(50)) == RECONNECT_DELAY_MAX

    def test_zero_or_negative_is_treated_as_first(self):
        assert reconnect_delay(0) == 0.0


class TestErrorClassification:
    def test_connection_codes(self):
        assert is_connection_error(LAN_ERR_CONNECT)
        assert is_connection_error(LAN_ERR_OFFLINE)

    def test_payload_code_is_not_a_connection_error(self):
        # "Unexpected Payload from Device" (904) is what the monitor emitted
        # every 30-45s in issue #62; it says nothing about the socket.
        assert not is_connection_error(LAN_ERR_PAYLOAD)

    def test_missing_or_unknown_codes(self):
        assert not is_connection_error(None)
        assert not is_connection_error("")
        assert not is_connection_error("999")

    def test_codes_accepted_as_int_or_str(self):
        assert is_connection_error(int(LAN_ERR_OFFLINE))


class TestShouldReconnect:
    def test_connection_error_reconnects_at_once(self):
        assert should_reconnect(LAN_ERR_OFFLINE, 1)
        assert should_reconnect(LAN_ERR_CONNECT, 1)

    def test_single_unreadable_frame_does_not_reconnect(self):
        assert not should_reconnect(LAN_ERR_PAYLOAD, 1)

    def test_repeated_unreadable_frames_do_reconnect(self):
        assert not should_reconnect(LAN_ERR_PAYLOAD, MAX_PAYLOAD_ERRORS - 1)
        assert should_reconnect(LAN_ERR_PAYLOAD, MAX_PAYLOAD_ERRORS)

    def test_error_without_a_code_is_treated_as_unreadable(self):
        assert not should_reconnect(None, 1)
        assert should_reconnect(None, MAX_PAYLOAD_ERRORS)


class TestProtocolVersion:
    """Which local protocol version to speak (#51, #62).

    The SCD973/26 announces 3.5 in its discovery broadcast while the client always
    built a 3.3 session, and a session at the wrong version cannot read the
    frames the camera sends.
    """

    def test_announced_version_is_read(self):
        assert parse_protocol_version("3.5") == 3.5
        assert parse_protocol_version("3.3") == 3.3
        assert parse_protocol_version(3.4) == 3.4

    def test_unknown_or_junk_values_fall_back(self):
        for value in ("", "  ", "junk", "9.9", None, True, [], {}):
            assert parse_protocol_version(value) is None

    def test_announced_version_is_tried_first(self):
        assert version_candidates(3.5) == [3.5, PROTOCOL_VERSION_DEFAULT]

    def test_default_alone_when_nothing_announced(self):
        assert version_candidates(None) == [PROTOCOL_VERSION_DEFAULT]

    def test_no_duplicate_when_the_camera_announces_the_default(self):
        assert version_candidates(PROTOCOL_VERSION_DEFAULT) == [PROTOCOL_VERSION_DEFAULT]

    def test_fallback_is_always_available(self):
        # Local control must not regress on firmware that refuses the newer
        # session-key negotiation.
        for announced in (3.1, 3.4, 3.5, None):
            assert PROTOCOL_VERSION_DEFAULT in version_candidates(announced)


class TestHeartbeatOutcome:
    """The keep-alive reads the next frame with a payload, which can be a push (fork 8c4fc03)."""

    def test_an_alert_read_in_place_of_the_ack_is_handed_back(self):
        record = {"dps": {"212": "eyJjbWQiOiJpcGNfbW90aW9uIn0="}, "t": 1}
        assert heartbeat_outcome(record) == (True, {"212": "eyJjbWQiOiJpcGNfbW90aW9uIn0="})

    def test_a_plain_ack_carries_nothing(self):
        assert heartbeat_outcome(None) == (True, None)
        assert heartbeat_outcome({}) == (True, None)
        assert heartbeat_outcome({"dps": {}}) == (True, None)

    def test_a_connection_error_means_the_socket_is_gone(self):
        assert heartbeat_outcome({"Error": "Network Error", "Err": LAN_ERR_CONNECT}) == (False, None)
        assert heartbeat_outcome({"Error": "Offline", "Err": LAN_ERR_OFFLINE}) == (False, None)

    def test_an_unreadable_ack_still_proves_the_socket_is_alive(self):
        assert heartbeat_outcome({"Error": "Unexpected Payload", "Err": LAN_ERR_PAYLOAD}) == (True, None)

    def test_a_heartbeat_without_an_answer_is_a_failure(self):
        # A timeout (902) counted as alive and reset the stale-data timer, so a
        # session whose camera stopped answering stayed "connected" for good.
        assert heartbeat_outcome({"Error": "Timeout Waiting for Device", "Err": "902"}) == (False, None)
        assert heartbeat_outcome({"Error": "Check device key or version", "Err": "914"}) == (False, None)

    def test_junk_dps_is_not_published(self):
        assert heartbeat_outcome({"dps": "not a dict"}) == (True, None)


class TestDeviceAnswered:
    """tinytuya returns failures as dicts instead of raising (tinytuya 1.20.0,
    XenonDevice._send_receive); these are the shapes it returns."""

    @staticmethod
    def err(code):
        return {"Error": "tinytuya error", "Err": code, "Payload": None}

    def test_an_ack_and_a_reply_are_answers(self):
        assert device_answered(None)
        assert device_answered({"dps": {"101": True}, "t": 1})
        assert device_answered({})

    def test_an_unreadable_ack_is_still_an_answer(self):
        assert device_answered(self.err("904"))
        assert device_answered(self.err("900"))

    def test_timeout_is_no_answer(self):
        assert not device_answered(self.err("902"))

    def test_no_connection_or_refused_handshake_is_no_answer(self):
        for code in ("901", "905", "914", 901, 902):
            assert not device_answered(self.err(code)), code


class TestQuickerGiveUp:
    def test_one_tinytuya_retry_on_every_socket(self):
        # tinytuya's default is five retries 5 s apart: a minute to notice a camera that is gone.
        from pathlib import Path
        source = (Path(__file__).parent.parent.parent / "custom_components" / "philips_avent" / "lan.py").read_text()
        assert SOCKET_RETRY_LIMIT == 1
        assert source.count("set_socketRetryLimit(SOCKET_RETRY_LIMIT)") == source.count("tinytuya.Device(")


class TestCommandNotDelivered:
    """Which failed writes certainly never reached the camera, so a retry cannot apply them twice."""

    def test_a_reply_or_no_reply_is_no_failure(self):
        for repeatable in (True, False):
            assert not command_not_delivered(None, repeatable=repeatable)
            assert not command_not_delivered({"dps": {"138": True}, "t": 1}, repeatable=repeatable)

    def test_a_socket_that_never_opened_means_the_command_never_left(self):
        for repeatable in (True, False):
            assert command_not_delivered(
                {"Error": "Network Error: Device Unreachable", "Err": LAN_ERR_OFFLINE}, repeatable=repeatable
            )
            assert command_not_delivered({"Error": "Check device key or version", "Err": "914"}, repeatable=repeatable)

    def test_a_broken_connection_retries_an_absolute_value(self):
        # Sending the night light state twice is harmless; losing it is not.
        assert command_not_delivered({"Error": "Network Error: Unable to Connect", "Err": LAN_ERR_CONNECT}, repeatable=True)

    def test_a_broken_connection_never_repeats_a_lullaby_step(self):
        # A 901 can come after the send: a step taken twice skips a track (issue #91).
        assert not command_not_delivered({"Error": "Network Error: Unable to Connect", "Err": LAN_ERR_CONNECT}, repeatable=False)

    def test_a_timeout_is_never_retried(self):
        for repeatable in (True, False):
            assert not command_not_delivered({"Error": "Timeout Waiting for Device", "Err": "902"}, repeatable=repeatable)

    def test_an_unreadable_reply_means_the_monitor_answered(self):
        for repeatable in (True, False):
            assert not command_not_delivered(
                {"Error": "Unexpected Payload from Device", "Err": LAN_ERR_PAYLOAD}, repeatable=repeatable
            )


class TestKeyedProtocol:
    """What a session carries on its own depends on the protocol it negotiated (fork c3d0b83, 619981b)."""

    def test_3_4_and_up_are_keyed(self):
        assert KEYED_PROTOCOL_MIN_VERSION == 3.4
        assert keyed_protocol(3.4)
        assert keyed_protocol(3.5)

    def test_3_3_keeps_todays_behaviour(self):
        # The maintainer's SCD973 on firmware 1.4.0 speaks 3.3.
        assert not keyed_protocol(3.3)
        assert not keyed_protocol(PROTOCOL_VERSION_DEFAULT)
        assert not keyed_protocol(3.1)

    def test_no_session_is_not_keyed(self):
        assert not keyed_protocol(None)


class TestBaselineDps:
    """A DP_QUERY answer becomes the baseline; anything else leaves the session alone."""

    def test_the_answer_is_the_state(self):
        assert baseline_dps({"dps": {"207": 2570, "158": 70}, "t": 1}) == {"207": 2570, "158": 70}

    def test_an_error_frame_carries_no_state(self):
        assert baseline_dps({"Error": "Timeout Waiting for Device", "Err": "902"}) is None
        assert baseline_dps({"Error": "x", "Err": LAN_ERR_PAYLOAD, "dps": {"207": 1}}) is None

    def test_nothing_or_junk_carries_no_state(self):
        for result in (None, {}, {"dps": {}}, {"dps": "x"}, "x"):
            assert baseline_dps(result) is None


class TestSeededSession:
    """A restart may dial the last published pair before scanning, when a handshake can prove it."""

    def test_a_keyed_pair_is_dialled_first(self):
        assert seeded_session("192.168.1.9", "3.5") == ("192.168.1.9", 3.5)
        assert seeded_session("192.168.1.9", "3.4") == ("192.168.1.9", 3.4)

    def test_a_3_3_pair_still_scans_first(self):
        # A 3.3 connect is bare TCP and proves neither the address nor the version.
        assert seeded_session("192.168.1.9", "3.3") == (None, None)

    def test_nothing_published_means_a_scan(self):
        assert seeded_session(None, "") == (None, None)
        assert seeded_session("", "3.5") == (None, None)
        assert seeded_session("192.168.1.9", "") == (None, None)
        assert seeded_session("192.168.1.9", "junk") == (None, None)


class TestSessionCandidates:
    """Which versions a (re)connect tries, and in what order."""

    def test_a_3_3_monitor_is_unchanged(self):
        # The maintainer's SCD973 on firmware 1.4.0: only ever 3.3.
        for scanned in (True, False):
            assert session_candidates(3.3, None, scanned=scanned) == [3.3]
            assert session_candidates(3.3, 3.3, scanned=scanned) == [3.3]
            assert session_candidates(None, 3.3, scanned=scanned) == [3.3]
            assert session_candidates(None, None, scanned=scanned) == [PROTOCOL_VERSION_DEFAULT]

    def test_after_a_scan_the_fallback_stays(self):
        assert session_candidates(3.5, None, scanned=True) == [3.5, PROTOCOL_VERSION_DEFAULT]

    def test_a_fallback_session_does_not_pin_the_monitor_to_3_3(self):
        # One unconfirmed 3.3 session used to make every later reconnect start at 3.3.
        assert session_candidates(3.5, 3.3, scanned=True) == [3.5, PROTOCOL_VERSION_DEFAULT]
        assert session_candidates(3.5, 3.3, scanned=False) == [3.5]

    def test_a_cached_address_is_trusted_only_through_a_handshake(self):
        # A failed handshake there means a rescan, not a bare TCP session with
        # whatever now answers at that address.
        assert session_candidates(3.5, 3.5, scanned=False) == [3.5]
        assert session_candidates(3.4, None, scanned=False) == [3.4]


class TestCommandRetry:
    @staticmethod
    def err(code):
        return {"Error": "tinytuya error", "Err": code, "Payload": None}

    def test_not_delivered_is_retried(self):
        for code in ("901", "905", "914", 914):
            assert command_not_delivered(self.err(code)), code

    def test_a_timeout_or_an_answer_is_not(self):
        # 902 may come after the camera took the command: a retry could apply it twice.
        assert not command_not_delivered(self.err("902"))
        assert not command_not_delivered(self.err("904"))
        assert not command_not_delivered(None)
        assert not command_not_delivered({"dps": {"138": True}})

    def test_retries_fit_in_the_deadline(self):
        assert COMMAND_ATTEMPTS == len(COMMAND_RETRY_DELAYS) + 1 >= 3
        assert sum(COMMAND_RETRY_DELAYS) < COMMAND_DEADLINE <= 10.0


def err(code):
    return {"Error": "tinytuya error", "Err": code, "Payload": None}


class FakeTime:
    """A clock that only moves when the queue sleeps, or when a send takes time."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class TestCommandQueue:
    def run(self, coro):
        return asyncio.run(coro)

    def test_refused_handshake_is_retried_with_growing_pauses(self):
        # The 02-11-2026 case: 914 while a stream and the listener held two of three sessions.
        t = FakeTime()
        calls = []

        async def send(items):
            calls.append(list(items))
            return None if len(calls) == 3 else (items, err("914"))

        q = CommandQueue(sleep=t.sleep, clock=t.clock)
        outcome = self.run(q.run(send, [("201", "play")]))
        assert outcome.delivered
        assert t.slept == [1.0, 2.0]
        assert len(calls) == 3

    def test_gives_up_refused_within_the_deadline(self):
        t = FakeTime()

        async def send(items):
            return items, err("914")

        outcome = self.run(CommandQueue(sleep=t.sleep, clock=t.clock).run(send, [("201", "play")]))
        assert not outcome.delivered and outcome.failure == COMMAND_REFUSED and outcome.key == "201"
        assert t.now <= COMMAND_DEADLINE

    def test_slow_failures_stop_at_the_deadline(self):
        # A connection error after a 5 s connect timeout: no third try past 10 s.
        t = FakeTime()
        calls = []

        async def send(items):
            calls.append(1)
            t.now += 5.0
            return items, err("901")

        outcome = self.run(CommandQueue(sleep=t.sleep, clock=t.clock).run(send, [("201", "play")]))
        assert outcome.failure == COMMAND_REFUSED
        assert len(calls) == 2 and t.slept == [1.0]

    def test_a_timeout_is_never_retried(self):
        # 902 may come after the camera took "next": a retry would skip two tracks.
        t = FakeTime()
        calls = []

        async def send(items):
            calls.append(1)
            return items, err("902")

        outcome = self.run(CommandQueue(sleep=t.sleep, clock=t.clock).run(send, [("202", "next")]))
        assert outcome.failure == COMMAND_NO_ANSWER and len(calls) == 1 and t.slept == []

    def test_only_what_was_not_delivered_is_sent_again(self):
        t = FakeTime()
        sent = []

        async def send(items):
            sent.append([k for k, _ in items])
            return None if len(sent) > 1 else (items[1:], err("914"))

        self.run(CommandQueue(sleep=t.sleep, clock=t.clock).run(send, [("243", True), ("201", "play")]))
        assert sent == [["243", "201"], ["201"]]

    def test_commands_never_overlap(self):
        # Timer and play pressed together: our own two sessions must not meet at the camera.
        active = []
        peak = []

        async def scenario():
            q = CommandQueue(sleep=asyncio.sleep)

            async def send(items):
                active.append(1)
                peak.append(len(active))
                await asyncio.sleep(0.01)
                active.pop()
                return None

            return await asyncio.gather(q.run(send, [("243", True)]), q.run(send, [("201", "play")]))

        outcomes = self.run(scenario())
        assert all(o.delivered for o in outcomes)
        assert max(peak) == 1

    def test_a_queued_command_waits_but_not_for_ever(self):
        async def scenario():
            q = CommandQueue(sleep=asyncio.sleep, queue_wait=0.05)
            release = asyncio.Event()

            async def slow(items):
                await release.wait()
                return None

            async def quick(items):
                return None

            first = asyncio.create_task(q.run(slow, [("243", True)]))
            await asyncio.sleep(0)
            second = await q.run(quick, [("201", "play")])
            release.set()
            return await first, second

        first, second = self.run(scenario())
        assert first.delivered
        assert not second.delivered and second.failure == COMMAND_BUSY

    def test_lan_client_sends_through_the_queue(self):
        from pathlib import Path
        source = (Path(__file__).parent.parent.parent / "custom_components" / "philips_avent" / "lan.py").read_text()
        assert "outcome = await self._commands.run(send, list(dps.items()), repeatable=repeatable)" in source
        assert "return (items[index:], result), pushes" in source
        assert "self.last_command_failure = outcome.failure" in source


class TestOwnSessionBudget:
    """The integration never holds two LAN sessions to one camera (02-11-2026).

    An SCD951 takes three at most; with the bridge's one while it streams, the
    integration's one leaves the third for the parent unit.
    """

    def run(self, coro):
        return asyncio.run(coro)

    def test_commands_ride_the_session_while_it_is_up(self):
        from lan_policy import OwnSession

        used = []

        async def on_session(items):
            used.append("session")

        async def short(items):
            used.append("short")

        self.run(OwnSession().send([("201", "play")], lambda: True, on_session, short))
        assert used == ["session"]

    def test_short_session_only_while_down_and_never_next_to_a_reconnect(self):
        from lan_policy import OwnSession

        own = OwnSession()
        open_sessions = 0
        peak = 0
        state = {"connected": False}

        async def hold(name, seconds):
            nonlocal open_sessions, peak
            open_sessions += 1
            peak = max(peak, open_sessions)
            await asyncio.sleep(seconds)
            open_sessions -= 1

        async def reconnect():
            async with own.lock:
                await hold("reconnect", 0.05)
                state["connected"] = True

        async def on_session(items):
            return None

        async def short(items):
            await hold("short", 0.05)

        async def scenario():
            # The listener starts reconnecting; a command arrives meanwhile.
            task = asyncio.create_task(reconnect())
            await asyncio.sleep(0)
            await own.send([("201", "play")], lambda: state["connected"], on_session, short)
            await task

        self.run(scenario())
        assert peak == 1

    def test_command_after_reconnect_goes_on_the_session(self):
        from lan_policy import OwnSession

        own = OwnSession()
        state = {"connected": False}
        used = []

        async def on_session(items):
            used.append("session")

        async def short(items):
            used.append("short")

        async def scenario():
            async with own.lock:
                task = asyncio.create_task(own.send([("201", "play")], lambda: state["connected"], on_session, short))
                await asyncio.sleep(0)
                state["connected"] = True
            await task

        self.run(scenario())
        assert used == ["session"]


class TestSessionCommands:
    def run(self, coro):
        return asyncio.run(coro)

    def test_the_listener_sends_and_answers(self):
        from lan_policy import SessionCommands

        commands = SessionCommands()
        sent = []

        async def send_now(items):
            sent.append(items)

        async def scenario():
            task = asyncio.create_task(commands.submit([("201", "play")]))
            await asyncio.sleep(0)
            assert commands.pending
            assert await commands.drain(send_now)
            return await task

        assert self.run(scenario()) is None
        assert sent == [[("201", "play")]]

    def test_a_closed_session_fails_waiting_commands_for_a_retry(self):
        from lan_policy import SessionCommands

        commands = SessionCommands()

        async def scenario():
            task = asyncio.create_task(commands.submit([("201", "play")]))
            await asyncio.sleep(0)
            commands.fail_all()
            return await task

        items, result = self.run(scenario())
        assert items == [("201", "play")]
        assert command_not_delivered(result)

    def test_a_broken_socket_stops_the_drain(self):
        from lan_policy import SessionCommands

        commands = SessionCommands()

        async def send_now(items):
            return items, err("901")

        async def scenario():
            first = asyncio.create_task(commands.submit([("201", "play")]))
            second = asyncio.create_task(commands.submit([("202", 3219040)]))
            await asyncio.sleep(0)
            ok = await commands.drain(send_now)
            return ok, await first, await second

        ok, first, second = self.run(scenario())
        assert not ok
        assert command_not_delivered(first[1]) and command_not_delivered(second[1])

    def test_listener_that_never_sends_times_out_as_not_delivered(self):
        from lan_policy import SessionCommands

        items, result = self.run(SessionCommands().submit([("201", "play")], timeout=0.01))
        assert command_not_delivered(result)

    def test_lan_client_routes_through_the_budget(self):
        from pathlib import Path
        source = (Path(__file__).parent.parent.parent / "custom_components" / "philips_avent" / "lan.py").read_text()
        assert "async with self._own_session.lock:\n                    connected = await self._connect()" in source
        assert "return await self._own_session.send(" in source
        assert "await self._session_commands.drain(self._send_on_session)" in source
        assert "self._session_commands.fail_all()" in source


class TestCommandsGoOutAtOnce:
    """A queued command must not wait for the listener's receive or heartbeat.

    Measured on 02-11-2026: commands over the persistent session took 0.4-0.9 s
    at best and 2-6 s for "play", while the camera answered the same commands in
    under 0.25 s through the bridge's session. The listener sat in a blocking
    receive (RECEIVE_POLL) and, every heartbeat, in tinytuya's read for a second
    message after the empty ack (up to SOCKET_TIMEOUT).
    """

    def test_a_queued_command_is_sent_within_100_ms(self):
        import socket
        import time

        from lan_policy import LOOP_WAKE, LoopWaker, SessionCommands

        camera, _other_end = socket.socketpair()  # a camera that sends nothing
        waker = LoopWaker()
        commands = SessionCommands(on_submit=waker.wake)
        sent_at = []

        async def send_now(items):
            sent_at.append(time.monotonic())

        async def listener():
            loop = asyncio.get_running_loop()
            while not sent_at:
                # The listener's wait: up to a second for the camera, like RECEIVE_POLL.
                woke = await loop.run_in_executor(None, waker.wait, camera, 1.0)
                if woke == LOOP_WAKE or commands.pending:
                    await commands.drain(send_now)

        async def scenario():
            task = asyncio.create_task(listener())
            await asyncio.sleep(0.2)  # the listener is now waiting on the socket
            queued_at = time.monotonic()
            await asyncio.wait_for(commands.submit([("201", "play")]), 2)
            await asyncio.wait_for(task, 2)
            return sent_at[0] - queued_at

        try:
            assert asyncio.run(scenario()) < 0.1
        finally:
            camera.close()
            _other_end.close()
            waker.close()

    def test_camera_data_ends_the_wait_and_stays_in_the_socket(self):
        import socket

        from lan_policy import LOOP_DATA, LOOP_TIMEOUT, LOOP_WAKE, LoopWaker

        camera, peer = socket.socketpair()
        waker = LoopWaker()
        try:
            assert waker.wait(camera, 0.05) == LOOP_TIMEOUT
            peer.sendall(b"push")
            assert waker.wait(camera, 1.0) == LOOP_DATA
            # Both ready: the command goes first, the push waits in the socket.
            waker.wake()
            assert waker.wait(camera, 1.0) == LOOP_WAKE
            assert waker.wait(camera, 1.0) == LOOP_DATA
            assert camera.recv(16) == b"push"
        finally:
            for s in (camera, peer):
                s.close()
            waker.close()

    def test_a_wait_without_a_socket_still_wakes(self):
        from lan_policy import LOOP_TIMEOUT, LOOP_WAKE, LoopWaker

        waker = LoopWaker()
        try:
            assert waker.wait(None, 0.05) == LOOP_TIMEOUT
            waker.wake()
            waker.wake()  # several wakes are one
            assert waker.wait(None, 1.0) == LOOP_WAKE
            assert waker.wait(None, 0.05) == LOOP_TIMEOUT
        finally:
            waker.close()

    def test_the_persistent_session_returns_at_the_first_message(self):
        from pathlib import Path

        source = (Path(__file__).parent.parent.parent / "custom_components" / "philips_avent" / "lan.py").read_text()
        # tinytuya would otherwise read on after an empty ack until its socket timeout.
        assert "d.retry = False" in source
        assert "self._waker.wait" in source
        assert "SessionCommands(on_submit=self._waker.wake)" in source



def test_cloud_does_not_repeat_an_unanswered_step():
    # The camera may have applied a command it did not answer: a value can
    # safely go out again through the cloud, a relative lullaby step cannot.
    assert cloud_may_repeat(COMMAND_NO_ANSWER, idempotent=True)
    assert not cloud_may_repeat(COMMAND_NO_ANSWER, idempotent=False)
    for failure in (COMMAND_REFUSED, COMMAND_BUSY, None):
        assert cloud_may_repeat(failure, idempotent=False)


def test_a_broken_connection_after_a_step_is_not_retried():
    # A 901 can come after the send; a lullaby step must not be taken twice.
    t = FakeTime()
    sent = []

    async def send(items):
        sent.append(items)
        return items, err("901")

    queue = CommandQueue(sleep=t.sleep, clock=t.clock)
    outcome = asyncio.run(queue.run(send, [("201", "next")], repeatable=False))
    assert not outcome.delivered
    assert outcome.failure == COMMAND_NO_ANSWER
    assert len(sent) == 1 and t.slept == []
