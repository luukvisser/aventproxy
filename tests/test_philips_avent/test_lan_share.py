"""Share the LAN connection while streaming (bridge_share.py, lan_metrics.py).

The camera accepts at most three LAN sessions and its parent unit may need
one. Shared mode hands the integration's control traffic to the bridge's
session while it streams. What must hold: the handover order (subscribe before
the own session closes; the own session back before the subscription goes), no
push lost across a handover, never more than two sessions from this house, a
fallback to the own session on any failure, and the token kept out of logs.
"""

import asyncio
import json
import logging
import random
from typing import ClassVar

import pytest
from bridge_share import (
    LAN_SHARE_SEPARATE,
    LAN_SHARE_SHARED,
    PATH_NONE,
    PATH_OWN,
    PATH_VIA_BRIDGE,
    BridgeLink,
    ShareHandover,
    SSEParser,
    dps_url,
    events_url,
)
from command_echo import EchoWatch
from lan_metrics import CLOCK_SKEW_SECONDS, LanMetrics
from payload import build_bridge_config
from redact import REDACTED, redact_secrets

from .test_offline_start import KEY, FakeLANClient, env  # noqa: F401 - fixture


def run(coro):
    return asyncio.run(coro)


class TestSSEParser:
    def test_events_split_across_chunks(self):
        p = SSEParser()
        assert p.feed('event: hello\ndata: {"seq": 3, "dps"') == []
        assert p.feed(': {"1": true}}\n') == []
        (hello,) = p.feed("\n")
        assert hello == {"type": "hello", "seq": 3, "dps": {"1": True}}

    def test_pings_and_garbage_yield_nothing(self):
        p = SSEParser()
        assert p.feed(": ping\n\n") == []
        assert p.feed("data: not json\n\n") == []
        (ev,) = p.feed('event: dps\r\ndata: {"seq": 4}\r\n\r\n')
        assert ev["type"] == "dps"


class Steps:
    """Records the order of handover steps; fails on request."""

    def __init__(self, fail=()):
        self.log = []
        self.fail = set(fail)

    def step(self, name):
        async def run_step():
            self.log.append(name)
            await asyncio.sleep(0)
            if name in self.fail:
                raise ConnectionError(name)
        return run_step

    def handover(self):
        return ShareHandover(self.step("subscribe"), self.step("unsubscribe"),
                             self.step("close_own"), self.step("open_own"))


class TestHandoverOrder:
    def test_subscribe_before_close_and_open_before_unsubscribe(self):
        steps = Steps()
        h = steps.handover()

        async def go():
            assert await h.take()
            assert h.shared
            await h.give_back()
            assert not h.shared
        run(go())
        assert steps.log == ["subscribe", "close_own", "open_own", "unsubscribe"]

    def test_failed_subscription_keeps_the_own_session(self):
        steps = Steps(fail={"subscribe"})
        h = steps.handover()
        assert run(h.take()) is False
        assert not h.shared
        assert "close_own" not in steps.log
        assert steps.log[-1] == "unsubscribe"

    def test_failed_close_keeps_the_own_session(self):
        steps = Steps(fail={"close_own"})
        h = steps.handover()
        assert run(h.take()) is False
        assert not h.shared
        assert steps.log == ["subscribe", "close_own", "unsubscribe"]

    def test_own_session_late_still_lets_go(self, caplog):
        steps = Steps(fail={"open_own"})
        h = steps.handover()

        async def go():
            await h.take()
            with caplog.at_level(logging.WARNING):
                await h.give_back()
        run(go())
        assert not h.shared
        assert steps.log[-2:] == ["open_own", "unsubscribe"]


class Timeline:
    """The own session and the bridge subscription as delivery paths, with a
    session count. The bridge's own session is open for the whole stream."""

    def __init__(self):
        self.own_open = True
        self.subscribed = False
        self.bridge_session_open = True
        self.received = set()
        self.max_sessions = 0

    def count(self):
        n = int(self.own_open) + int(self.bridge_session_open)
        self.max_sessions = max(self.max_sessions, n)

    def push(self, n):
        if self.own_open or self.subscribed:
            self.received.add(n)

    def handover(self):
        async def subscribe():
            await asyncio.sleep(random.uniform(0, 0.003))
            self.subscribed = True  # the hello arrived
        async def unsubscribe():
            self.subscribed = False
        async def close_own():
            await asyncio.sleep(random.uniform(0, 0.003))
            self.own_open = False
            self.count()
        async def open_own():
            await asyncio.sleep(random.uniform(0, 0.003))
            self.own_open = True
            self.count()
        return ShareHandover(subscribe, unsubscribe, close_own, open_own)


def test_no_push_lost_and_never_three_sessions():
    random.seed(7)
    t = Timeline()
    h = t.handover()

    async def pusher():
        for n in range(400):
            t.push(n)
            await asyncio.sleep(random.uniform(0, 0.0005))

    async def switcher():
        for _ in range(25):
            await h.take()
            await asyncio.sleep(random.uniform(0, 0.002))
            await h.give_back()

    async def go():
        t.count()
        await asyncio.gather(pusher(), switcher())
    run(go())
    assert t.received == set(range(400))
    assert t.max_sessions <= 2


class FakeContent:
    def __init__(self, chunks, stall=False):
        self._chunks = list(chunks)
        self._stall = stall

    async def readany(self):
        if self._chunks:
            await asyncio.sleep(0)
            return self._chunks.pop(0)
        if self._stall:
            await asyncio.sleep(3600)
        return b""


class FakeResponse:
    def __init__(self, status, content=None, body=None):
        self.status = status
        self.content = content
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._body


class FakeSession:
    def __init__(self, get_response, post_response=None):
        self._get = get_response
        self._post = post_response
        self.headers = []
        self.posted = []

    def get(self, url, headers=None, timeout=None):
        self.headers.append(headers)
        return self._get

    def post(self, url, headers=None, json=None, timeout=None):
        self.headers.append(headers)
        self.posted.append(json)
        return self._post


def sse(kind, doc):
    return f"event: {kind}\ndata: {json.dumps(doc)}\n\n".encode()


TOKEN = "test-token-not-for-logs-0123456789"


class TestBridgeLink:
    def test_hello_then_pushes_then_end(self, caplog):
        got, ended = [], []
        content = FakeContent([sse("hello", {"seq": 1, "dps": {"246": "playing"}}),
                               sse("dps", {"seq": 2, "dps": {"212": "rec"}}),
                               sse("ending", {"seq": 3})])
        session = FakeSession(FakeResponse(200, content))

        async def go():
            link = BridgeLink(session, "http://b/events", "http://b/dps", TOKEN, got.append, ended.append)
            with caplog.at_level(logging.DEBUG):
                hello = await link.open()
                await asyncio.sleep(0.05)
            await link.close()
            return hello
        hello = run(go())
        assert hello == {"246": "playing"}
        assert got == [{"212": "rec"}]
        assert ended and "ended the stream" in ended[0]
        assert session.headers[0] == {"Authorization": f"Bearer {TOKEN}"}
        assert TOKEN not in caplog.text

    def test_refused_subscription_raises(self):
        session = FakeSession(FakeResponse(409, FakeContent([])))

        async def go():
            link = BridgeLink(session, "e", "c", TOKEN, lambda d: None, lambda r: None)
            await link.open()
        with pytest.raises(ConnectionError):
            run(go())

    def test_stall_ends_the_link(self, monkeypatch):
        import bridge_share
        monkeypatch.setattr(bridge_share, "EVENT_STALL_SECONDS", 0.05)
        ended = []
        content = FakeContent([sse("hello", {"seq": 1, "dps": {"1": True}})], stall=True)

        async def go():
            link = BridgeLink(FakeSession(FakeResponse(200, content)), "e", "c", TOKEN, lambda d: None, ended.append)
            await link.open()
            await asyncio.sleep(0.2)
            await link.close()
        run(go())
        assert ended and "no word" in ended[0]

    @pytest.mark.usefixtures("quick_echo")
    def test_command_answer(self):
        ok = FakeSession(None, FakeResponse(200, body={"ok": True}))
        bad = FakeSession(None, FakeResponse(502, body={"ok": False, "error": "timeout"}))

        async def go(session):
            link = BridgeLink(session, "e", "c", TOKEN, lambda d: None, lambda r: None)
            await link.set_dps({"201": "stop"})
            assert len(session.posted) == 1  # returned once the bridge answered
            await link._echoes.follow_up_done()
        run(go(ok))
        # No push stream here, so no echo: sent once more, in the background.
        assert ok.posted == [{"dps": {"201": "stop"}}] * 2
        with pytest.raises(ConnectionError, match="502"):
            run(go(bad))
        assert len(bad.posted) == 1


@pytest.fixture
def quick_echo(monkeypatch):
    """command_echo's waits divided by ten."""
    monkeypatch.setattr(EchoWatch, "echo_wait", 0.15)
    monkeypatch.setattr(EchoWatch, "settle_min", 0.05)
    monkeypatch.setattr(EchoWatch, "settle_max", 0.15)
    monkeypatch.setattr(EchoWatch, "follow_up_limit", 1.5)


class StreamContent:
    """An event stream fed while the link reads it."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()

    async def readany(self):
        return await self.queue.get()

    def push(self, kind, doc):
        self.queue.put_nowait(sse(kind, doc))

    def end(self):
        self.queue.put_nowait(b"")


class BridgeCamera:
    """The bridge and its camera: a command that comes while the camera is busy with the
    previous one is answered and ignored (no echo); it reports what it took at once and
    everything again `busy` later, when it is free."""

    def __init__(self, content, busy=0.1):
        self.content, self.busy = content, busy
        self.state: dict = {}
        self.posted: list[dict] = []
        self.headers: list = []
        self._free_at = 0.0
        self._seq = 1

    def get(self, url, headers=None, timeout=None):
        self.headers.append(headers)
        return FakeResponse(200, self.content)

    def _report(self, dps):
        self._seq += 1
        self.content.push("dps", {"seq": self._seq, "dps": dps})

    def post(self, url, headers=None, json=None, timeout=None):
        self.headers.append(headers)
        loop = asyncio.get_running_loop()
        now = loop.time()
        self.posted.append(json["dps"])
        if now >= self._free_at:
            self.state.update(json["dps"])
            self._free_at = now + self.busy
            loop.call_later(0.002, self._report, dict(json["dps"]))
            loop.call_later(self.busy, self._report, dict(self.state))
        return FakeResponse(200, body={"ok": True})


@pytest.mark.usefixtures("quick_echo")
class TestBridgeLinkEcho:
    """The camera ignores a command that comes while it is busy with the previous one."""

    def _link(self, camera, got=None, ended=None):
        return BridgeLink(camera, "e", "c", TOKEN, (got if got is not None else []).append,
                          (ended if ended is not None else []).append)

    @pytest.mark.parametrize(("busy", "posts"), [(0.1, 2), (0.25, 3)])
    def test_two_commands_back_to_back_both_land(self, busy, posts):
        # Busy until its report: the second waits for it. Busy longer than the
        # settle wait: the second is ignored once, and the resend brings it.
        async def go():
            content = StreamContent()
            camera = BridgeCamera(content, busy)
            got = []
            link = self._link(camera, got)
            content.push("hello", {"seq": 1, "dps": {"158": 50, "138": True}})
            await link.open()
            await link.set_dps({"158": 40})
            await link.set_dps({"138": False})
            await link._echoes.follow_up_done()
            await link.close()
            return camera, got
        camera, got = run(go())
        assert camera.state == {"158": 40, "138": False}
        assert camera.posted == [{"158": 40}, {"138": False}, {"138": False}][:posts]
        assert {"138": False} in got  # the echoes still reach the coordinator

    def test_commands_from_two_callers_go_one_at_a_time(self):
        async def go():
            content = StreamContent()
            camera = BridgeCamera(content)
            link = self._link(camera)
            content.push("hello", {"seq": 1, "dps": {}})
            await link.open()
            await asyncio.gather(link.set_dps({"158": 40}), link.set_dps({"138": False}))
            await link._echoes.follow_up_done()
            await link.close()
            return camera
        camera = run(go())
        assert camera.state == {"158": 40, "138": False}
        assert len(camera.posted) == 2  # each once the camera was free: none ignored

    def test_a_missing_echo_means_exactly_one_resend(self):
        async def go():
            content = StreamContent()
            camera = BridgeCamera(content, busy=10)  # ignores everything after the first
            link = self._link(camera)
            content.push("hello", {"seq": 1, "dps": {}})
            await link.open()
            for dps, repeatable in (({"158": 40}, True), ({"138": False}, True), ({"201": "next"}, False)):
                await link.set_dps(dps, repeatable=repeatable)
            await link._echoes.follow_up_done()
            await link.close()
            return camera
        camera = run(go())
        assert camera.posted == [{"158": 40}, {"138": False}, {"138": False}, {"201": "next"}]

    def test_no_deadlock_when_the_stream_drops_during_the_wait(self, monkeypatch):
        monkeypatch.setattr(EchoWatch, "echo_wait", 5)
        monkeypatch.setattr(EchoWatch, "settle_max", 5)

        async def go():
            content = StreamContent()
            camera = BridgeCamera(content, busy=10)
            ended = []
            link = self._link(camera, ended=ended)
            content.push("hello", {"seq": 1, "dps": {}})
            await link.open()
            await link.set_dps({"158": 40})  # taken and echoed; the camera stays busy
            # The next waits for the camera to settle; the stream ends during that wait.
            asyncio.get_running_loop().call_later(0.05, content.end)
            await asyncio.wait_for(link.set_dps({"138": False}), 1)
            assert ended
            # A command after the end does not wait either.
            await asyncio.wait_for(link.set_dps({"139": True}), 1)
            await asyncio.wait_for(link._echoes.follow_up_done(), 1)
            await link.close()
            return camera, link._echoes.waiting
        camera, waiting = run(go())
        # Not sent again into a link that is gone.
        assert camera.posted == [{"158": 40}, {"138": False}, {"139": True}]
        assert waiting == 0


class TestMetrics:
    def test_reconnects_count_after_the_first(self):
        m = LanMetrics()
        m.note_connected(False, 100.0)
        assert m.reconnects == 0 and m.last_reconnect is None
        m.note_connected(True, 200.0)
        assert m.reconnects == 1 and m.last_reconnect.timestamp() == 200.0

    def test_push_delay_and_clock_skew(self):
        m = LanMetrics()
        m.note_alarm(1000.0, 1001.5)
        assert m.push_delay == 1.5 and not m.clock_skew
        m.note_alarm(1000.0, 1000.0 + CLOCK_SKEW_SECONDS + 5)
        assert m.clock_skew and m.push_delay == 1.5  # kept, not overwritten
        m.note_alarm(1000.0, 990.0)
        assert m.clock_skew
        m.note_alarm(None, 5.0)
        assert m.push_delay == 1.5

    def test_command_latency(self):
        m = LanMetrics()
        m.note_command(0.1234, "own")
        assert m.command_latency == 0.123 and m.command_path == "own"


def test_urls_and_config_and_redaction():
    assert events_url("bridge", 38554, "cam1") == "http://bridge:38555/cameras/cam1/events"
    assert dps_url("bridge", 38554, "cam1") == "http://bridge:38555/cameras/cam1/dps"
    base = dict(signing_key="s", sid="s", ecode="", partner="", app_key="a", device_id="d",
                package_name="p", api_host="h", bridge_port=38554, cameras=[])
    assert "api_token" not in build_bridge_config(**base)
    config = build_bridge_config(**base, api_token=TOKEN)
    assert config["api_token"] == TOKEN
    assert redact_secrets({"api_token": TOKEN, "bridge_api_token": TOKEN}) == {
        "api_token": REDACTED, "bridge_api_token": REDACTED}


class FakeLink:
    """Stands in for BridgeLink in the coordinator."""

    instances: ClassVar[list] = []

    def __init__(self, session, events, commands, token, on_dps, on_end):
        self.on_dps, self.on_end = on_dps, on_end
        self.closed = False
        self.sent = []
        self.repeatable = []
        FakeLink.instances.append(self)

    async def open(self):
        return {"246": "stopping"}

    async def close(self):
        self.closed = True

    async def set_dps(self, dps, *, repeatable=True):
        self.sent.append(dps)
        self.repeatable.append(repeatable)


class ReportingLANClient(FakeLANClient):
    """Like the real client, stop() reports the session going down."""

    async def stop(self):
        await super().stop()
        if self.connected:
            self.go_down()


def shared_coordinator(env, monkeypatch, mode=LAN_SHARE_SHARED):  # noqa: F811 - fixture
    from .test_offline_start import FakeAPI, FakeCache
    coordinator, hass = env(FakeAPI(), FakeCache(), lan_preferred=True)
    coordinator._local_key = KEY
    coordinator.data = {"1": True}
    FakeLink.instances = []
    monkeypatch.setattr(env.module, "BridgeLink", FakeLink)
    monkeypatch.setattr(env.module, "TuyaLANClient", ReportingLANClient)
    monkeypatch.setattr(env.module, "OWN_CONNECT_TIMEOUT_SECONDS", 2)
    coordinator.configure_lan_share(mode, object(), "events", "commands", TOKEN)
    return coordinator, hass


def test_coordinator_hands_over_and_back(env, monkeypatch):  # noqa: F811 - fixture
    coordinator, hass = shared_coordinator(env, monkeypatch)

    async def go():
        await coordinator.start_lan()
        own = FakeLANClient.instances[-1]
        own.come_up()
        assert coordinator.connection_path == PATH_OWN

        coordinator.bridge_stream_changed("local")
        await hass.settle()
        await asyncio.sleep(0)
        await hass.settle()
        link = FakeLink.instances[-1]
        assert coordinator.lan_shared and coordinator.connection_path == PATH_VIA_BRIDGE
        assert coordinator.lan_connected
        assert not own.started  # the own session closed
        assert coordinator._lan_client is None
        assert coordinator._cloud_fallback_unsub is None  # no fallback for a deliberate close
        # Without an own client the poll decision still has an answer (it
        # raised AttributeError and made every entity unavailable, 04-11-2026).
        assert coordinator.lan_alerts_local is True
        assert isinstance(coordinator._cloud_poll_off, bool)

        # Pushes through the bridge reach the entities as LAN pushes.
        before = coordinator.lan_update_sequence
        link.on_dps({"212": "record"})
        assert coordinator.lan_update_sequence == before + 1
        # Commands go through the bridge, not a new session.
        await coordinator.set_dps({"201": "stop"})
        assert link.sent == [{"201": "stop"}]
        assert coordinator.metrics.command_path == PATH_VIA_BRIDGE
        # A relative lullaby step must not go twice: the link is told so.
        await coordinator.set_dps({"201": "next"})
        assert link.repeatable == [True, False]

        # The stream ends: the own session comes back before the link closes.
        async def bring_up_soon():
            while len(FakeLANClient.instances) < 2:
                await asyncio.sleep(0.01)
            assert not link.closed  # still subscribed while the own one connects
            FakeLANClient.instances[-1].come_up()
        helper = asyncio.create_task(bring_up_soon())
        coordinator.bridge_stream_changed("idle")
        await hass.settle()
        await helper
        await asyncio.sleep(0)
        await hass.settle()
        assert link.closed
        assert not coordinator.lan_shared
        assert coordinator.connection_path == PATH_OWN
    run(go())


def test_the_bridges_session_counts_as_ours_for_availability_and_the_poll(env, monkeypatch):  # noqa: F811 - fixture
    # Without an own client, lan_live and lan_serves_state read the own one
    # only: a push through the bridge left a failed poll's verdict standing,
    # and a 3.4+ camera's cloud poll resumed for as long as the stream ran.
    coordinator, hass = shared_coordinator(env, monkeypatch)
    coordinator.data = {"1": True, "212": ""}
    coordinator._lan_version = 3.4

    async def go():
        await coordinator.start_lan()
        FakeLANClient.instances[-1].come_up()
        coordinator.bridge_stream_changed("local")
        await hass.settle()
        await asyncio.sleep(0)
        await hass.settle()
        assert coordinator.lan_shared and coordinator._lan_client is None
        assert coordinator.lan_live
        assert coordinator.cloud_poll_pausable
        coordinator.last_update_success = False
        FakeLink.instances[-1].on_dps({"1": False})
        assert coordinator.last_update_success
        coordinator._lan_version = 3.3
        assert not coordinator.lan_serves_state
    run(go())


def test_separate_mode_never_hands_over(env, monkeypatch):  # noqa: F811 - fixture
    coordinator, hass = shared_coordinator(env, monkeypatch, mode=LAN_SHARE_SEPARATE)

    async def go():
        await coordinator.start_lan()
        FakeLANClient.instances[-1].come_up()
        coordinator.bridge_stream_changed("local")
        await hass.settle()
        assert not FakeLink.instances
        assert coordinator.connection_path == PATH_OWN
    run(go())


def test_no_token_means_separate(env, monkeypatch):  # noqa: F811 - fixture
    coordinator, hass = shared_coordinator(env, monkeypatch)
    coordinator.configure_lan_share(LAN_SHARE_SHARED, object(), "e", "c", None)

    async def go():
        coordinator.bridge_stream_changed("local")
        await hass.settle()
        assert not coordinator.lan_shared
        assert coordinator.connection_path == PATH_NONE
    run(go())


def test_shared_is_the_default():
    """On by default: through the bridge commands were confirmed 5-20x faster, with the same video."""
    from bridge_share import DEFAULT_LAN_SHARE as DEFAULT_MODE
    from bridge_share import lan_share_mode
    from const import DEFAULT_LAN_SHARE

    assert DEFAULT_LAN_SHARE is True
    assert DEFAULT_MODE == LAN_SHARE_SHARED
    assert lan_share_mode(None) == LAN_SHARE_SHARED
    assert lan_share_mode(LAN_SHARE_SEPARATE) == LAN_SHARE_SEPARATE


def test_an_entry_without_the_option_gets_the_default_and_an_explicit_choice_stays():
    from pathlib import Path

    root = Path(__file__).parent.parent.parent / "custom_components" / "philips_avent"
    # Both read an absent key as the default and keep a stored value.
    assert "entry.options.get(CONF_LAN_SHARE, DEFAULT_LAN_SHARE)" in (root / "__init__.py").read_text()
    assert "self.config_entry.options.get(CONF_LAN_SHARE, DEFAULT_LAN_SHARE)" in (root / "config_flow.py").read_text()


def test_a_handover_waits_for_a_command_on_the_own_session(env, monkeypatch):  # noqa: F811 - fixture
    # A command already on its way over our own session when a handover began
    # had the session closed under it, and it (or its retry) then opened one of
    # its own next to the bridge's, on a camera that serves one at a time.
    coordinator, hass = shared_coordinator(env, monkeypatch)

    async def go():
        await coordinator.start_lan()
        own = FakeLANClient.instances[-1]
        own.come_up()
        gate = asyncio.Event()
        plain = own.set_dps

        async def slow_set_dps(dps, *, repeatable=True):
            await gate.wait()
            return await plain(dps, repeatable=repeatable)

        own.set_dps = slow_set_dps
        command = asyncio.create_task(coordinator.set_dps({"201": "stop"}))
        await asyncio.sleep(0.05)  # the command is waiting for the camera
        coordinator.bridge_stream_changed("local")
        await asyncio.sleep(0.05)
        assert own.started and not coordinator.lan_shared  # the handover waits
        gate.set()
        assert await command == {"success": True}
        assert own.sent == [{"201": "stop"}]
        await hass.settle()
        await asyncio.sleep(0)
        await hass.settle()
        assert coordinator.lan_shared and not own.started  # then it runs
    run(go())
