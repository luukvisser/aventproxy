"""The cloud poll in states the stubs did not reach before (review of
2026-11-04): pushes must not starve the poll, and a poll switched on must start."""

import asyncio

from .test_offline_start import CACHED, FakeAPI, FakeCache, FakeLANClient, env  # noqa: F401 - fixture


def _up(env, version, lan_preferred=True):  # noqa: F811 - fixture
    coordinator, hass = env(FakeAPI(), FakeCache(CACHED), lan_preferred=lan_preferred)

    async def go():
        await coordinator.async_start()
        lan = FakeLANClient.instances[-1]
        lan.version = version
        lan.come_up()
        await hass.settle()

    asyncio.run(go())
    return coordinator, hass


def test_a_lan_push_does_not_move_the_cloud_poll(env):  # noqa: F811 - fixture
    # Home Assistant's async_set_updated_data plans the next poll a whole
    # interval later; a camera pushing every few seconds was never polled.
    coordinator, _ = _up(env, 3.3)

    def forbidden(_data):
        raise AssertionError("a push must not reschedule the cloud poll")

    coordinator.async_set_updated_data = forbidden
    coordinator._on_lan_dps_update({"101": True})
    assert coordinator.data["101"] is True


def test_a_33_session_starts_the_poll_it_switches_on(env, monkeypatch):  # noqa: F811 - fixture
    coordinator, hass = env(FakeAPI(), FakeCache(CACHED), lan_preferred=True)
    requested = []

    async def go():
        await coordinator.async_start()
        assert coordinator.update_interval is None
        monkeypatch.setattr(coordinator, "async_request_refresh", lambda: _note(requested))
        lan = FakeLANClient.instances[-1]
        lan.version = 3.3
        lan.come_up()
        await hass.settle()

    asyncio.run(go())
    assert coordinator.update_interval is not None
    assert requested, "setting update_interval alone schedules no poll"


async def _note(calls):
    calls.append(1)
