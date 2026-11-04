"""A camera that serves one LAN session at a time (protocol 3.3, bridge_share.py).

An SCD973-family camera accepted the bridge's connection while the
integration's 3.3 session was open but answered nothing on it (02-11-2026).
So for such a camera the order is reversed: the integration closes its own
session when the bridge asks for it, confirms, and subscribes once the
bridge streams; if the bridge does not use it, the own session comes back.
What must hold: never two sessions from this house, the release before the
confirmation, the own session back on every path that does not end in the
bridge's session, and a 3.4 camera untouched.
"""

import asyncio

import pytest

from bridge_share import LAN_SHARE_SEPARATE, PATH_NONE, PATH_OWN, PATH_VIA_BRIDGE, post_release, release_url
from bridge_status import parse_status

from .test_lan_share import TOKEN, FakeLink, run, shared_coordinator
from .test_offline_start import FakeLANClient, env  # noqa: F401 - fixture


class FakePost:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def post(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        outer = self

        class Response:
            status = outer.status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False
        return Response()


def test_status_reports_wants_session():
    doc = {"cameras": [
        {"camera_id": "a", "path": "idle", "wants_session": True},
        {"camera_id": "b", "path": "idle", "wants_session": "yes"},
        {"camera_id": "c", "path": "idle"},
    ]}
    status = parse_status(doc)
    assert status["a"]["wants_session"] is True
    assert status["b"]["wants_session"] is False
    assert status["c"]["wants_session"] is False


def test_release_url_and_post():
    assert release_url("192.0.2.10", 38554, "cam") == "http://192.0.2.10:38555/cameras/cam/release"
    ok = FakePost(204)
    assert run(post_release(ok, "u", TOKEN)) is True
    assert ok.calls[0][1] == {"Authorization": f"Bearer {TOKEN}"}
    assert run(post_release(FakePost(409), "u", TOKEN)) is False


def single_session(env, monkeypatch, version=3.3, post_status=204):  # noqa: F811 - fixture
    coordinator, hass = shared_coordinator(env, monkeypatch)
    coordinator.configure_lan_share(
        coordinator._share_mode, object(), "events", "commands", TOKEN, release="release",
    )
    coordinator._lan_version = version
    timers = []
    monkeypatch.setattr(env.module, "async_call_later", lambda _h, _d, cb: timers.append(cb) or (lambda: None))
    order = []

    async def fake_post(session, url, token):
        # The own session must be closed by the time the bridge is told.
        order.append(("post", coordinator._lan_client is None))
        return post_status == 204
    monkeypatch.setattr(env.module, "post_release", fake_post)
    return coordinator, hass, timers, order


def sessions(coordinator):
    own = 1 if coordinator._lan_client is not None and coordinator._lan_client.started else 0
    bridge = 1 if coordinator._share_link is not None else 0
    return own + bridge


async def settle(hass):
    for _ in range(3):
        await hass.settle()
        await asyncio.sleep(0)


def test_release_before_the_bridge_connects_then_shared(env, monkeypatch):  # noqa: F811 - fixture
    coordinator, hass, _timers, order = single_session(env, monkeypatch)

    async def go():
        await coordinator.start_lan()
        own = FakeLANClient.instances[-1]
        own.come_up()
        coordinator.bridge_stream_changed("idle", wants_session=True)
        await settle(hass)
        assert order == [("post", True)]
        assert not own.started and coordinator._lan_client is None
        assert coordinator.connection_path == PATH_NONE
        assert sessions(coordinator) == 0
        # A reconnect attempt elsewhere (a key refresh) waits while released.
        await coordinator.start_lan()
        assert FakeLANClient.instances[-1] is own

        coordinator.bridge_stream_changed("local")
        await settle(hass)
        assert coordinator.lan_shared and coordinator.connection_path == PATH_VIA_BRIDGE
        assert not coordinator._released_for_bridge
        assert sessions(coordinator) == 1
    run(go())


def test_the_bridge_closing_first_brings_the_own_session_back(env, monkeypatch):  # noqa: F811 - fixture
    coordinator, hass, _timers, _order = single_session(env, monkeypatch)

    async def go():
        await coordinator.start_lan()
        FakeLANClient.instances[-1].come_up()
        coordinator.bridge_stream_changed("idle", wants_session=True)
        await settle(hass)
        coordinator.bridge_stream_changed("local")
        await settle(hass)
        link = FakeLink.instances[-1]

        async def bring_up_soon():
            while not (coordinator._lan_client and coordinator._lan_client.started):
                await asyncio.sleep(0.01)
            coordinator._lan_client.come_up()
        helper = asyncio.create_task(bring_up_soon())
        link.on_end("the bridge ended the stream")
        await settle(hass)
        await helper
        await settle(hass)
        assert not coordinator.lan_shared and link.closed
        assert coordinator.connection_path == PATH_OWN
    run(go())


@pytest.mark.parametrize("cloud_path", ["webrtc", "imm"])
def test_own_session_back_when_the_bridge_stops_asking(env, monkeypatch, cloud_path):  # noqa: F811 - fixture
    coordinator, hass, _timers, _order = single_session(env, monkeypatch)

    async def go():
        await coordinator.start_lan()
        FakeLANClient.instances[-1].come_up()
        coordinator.bridge_stream_changed("idle", wants_session=True)
        await settle(hass)
        # The bridge streams over cloud signalling instead: it no longer asks.
        coordinator.bridge_stream_changed(cloud_path, wants_session=False)
        await settle(hass)
        assert not coordinator._released_for_bridge
        assert coordinator._lan_client is not None and coordinator._lan_client.started
        assert len(FakeLANClient.instances) == 2
    run(go())


def test_released_session_waits_while_the_bridge_connects(env, monkeypatch):  # noqa: F811 - fixture
    # The bridge stops asking once it has the release and connects; until it
    # streams its path stays idle. Ours must not come back at the next poll
    # (it did after 2 s, 02-11-2026, during the bridge's 5 s handshake).
    coordinator, hass, _timers, _order = single_session(env, monkeypatch)

    async def go():
        await coordinator.start_lan()
        FakeLANClient.instances[-1].come_up()
        coordinator.bridge_stream_changed("idle", wants_session=True)
        await settle(hass)
        assert coordinator._released_for_bridge
        coordinator.bridge_stream_changed("idle", wants_session=False)
        await settle(hass)
        assert coordinator._released_for_bridge
        assert len(FakeLANClient.instances) == 1
        # After the grace without a direct stream, ours comes back.
        coordinator._released_at -= 11
        coordinator.bridge_stream_changed("idle", wants_session=False)
        await settle(hass)
        assert not coordinator._released_for_bridge
        assert len(FakeLANClient.instances) == 2
    run(go())


def test_own_session_back_after_the_timeout(env, monkeypatch):  # noqa: F811 - fixture
    coordinator, hass, timers, _order = single_session(env, monkeypatch)

    async def go():
        await coordinator.start_lan()
        FakeLANClient.instances[-1].come_up()
        coordinator.bridge_stream_changed("idle", wants_session=True)
        await settle(hass)
        assert timers, "no timeout was set for the release"
        # The bridge no longer asks, and nothing streams: the timeout brings ours back.
        coordinator.bridge_stream_changed("idle", wants_session=False)
        timers[-1](None)
        await settle(hass)
        assert not coordinator._released_for_bridge
        assert coordinator._lan_client is not None and coordinator._lan_client.started
    run(go())


def test_a_slow_direct_start_keeps_our_session_released(env, monkeypatch):  # noqa: F811 - fixture
    # A 3.3 start can take 18 s (the version-header retry); the bridge keeps
    # asking until it ends. Neither the grace nor the timeout may take the
    # camera's one session from it meanwhile.
    coordinator, hass, timers, _order = single_session(env, monkeypatch)

    async def go():
        await coordinator.start_lan()
        FakeLANClient.instances[-1].come_up()
        coordinator.bridge_stream_changed("idle", wants_session=True)
        await settle(hass)
        coordinator._released_at -= 30
        coordinator.bridge_stream_changed("idle", wants_session=True)
        await settle(hass)
        assert coordinator._released_for_bridge
        before = len(timers)
        timers[-1](None)
        await settle(hass)
        assert coordinator._released_for_bridge
        assert len(timers) == before + 1, "the timeout looks again later"
        assert len(FakeLANClient.instances) == 1
    run(go())


def test_a_status_asking_far_too_long_gets_our_session_back(env, monkeypatch):  # noqa: F811 - fixture
    coordinator, hass, timers, _order = single_session(env, monkeypatch)

    async def go():
        await coordinator.start_lan()
        FakeLANClient.instances[-1].come_up()
        coordinator.bridge_stream_changed("idle", wants_session=True)
        await settle(hass)
        coordinator._released_at -= 91
        timers[-1](None)
        await settle(hass)
        assert not coordinator._released_for_bridge
        assert coordinator._lan_client is not None and coordinator._lan_client.started
    run(go())


def test_own_session_back_when_the_bridge_no_longer_waits(env, monkeypatch):  # noqa: F811 - fixture
    coordinator, hass, _timers, _order = single_session(env, monkeypatch, post_status=409)

    async def go():
        await coordinator.start_lan()
        FakeLANClient.instances[-1].come_up()
        coordinator.bridge_stream_changed("idle", wants_session=True)
        await settle(hass)
        assert not coordinator._released_for_bridge
        assert coordinator._lan_client is not None and coordinator._lan_client.started
    run(go())


def test_a_34_camera_never_releases(env, monkeypatch):  # noqa: F811 - fixture
    coordinator, hass, _timers, order = single_session(env, monkeypatch, version=3.4)

    async def go():
        await coordinator.start_lan()
        own = FakeLANClient.instances[-1]
        own.come_up()
        coordinator.bridge_stream_changed("idle", wants_session=True)
        await settle(hass)
        assert order == []
        assert own.started and coordinator.connection_path == PATH_OWN
    run(go())


def test_separate_mode_never_releases(env, monkeypatch):  # noqa: F811 - fixture
    coordinator, hass, _timers, order = single_session(env, monkeypatch)
    coordinator.configure_lan_share(LAN_SHARE_SEPARATE, object(), "e", "c", TOKEN, release="release")

    async def go():
        await coordinator.start_lan()
        FakeLANClient.instances[-1].come_up()
        coordinator.bridge_stream_changed("idle", wants_session=True)
        await settle(hass)
        assert order == []
        assert coordinator.connection_path == PATH_OWN
    run(go())


def test_a_command_in_the_gap_waits_for_the_bridge(env, monkeypatch):  # noqa: F811 - fixture
    coordinator, hass, _timers, _order = single_session(env, monkeypatch)

    async def go():
        await coordinator.start_lan()
        FakeLANClient.instances[-1].come_up()
        coordinator.bridge_stream_changed("idle", wants_session=True)
        await settle(hass)
        command = asyncio.create_task(coordinator.set_dps({"201": "stop"}))
        await asyncio.sleep(0.05)
        assert not command.done()
        coordinator.bridge_stream_changed("local")
        await settle(hass)
        await asyncio.wait_for(command, 2)
        assert FakeLink.instances[-1].sent == [{"201": "stop"}]
    run(go())
