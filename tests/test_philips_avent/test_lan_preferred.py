"""LAN preferred: the cloud only when the cache cannot do the job.

With a usable cache the camera starts and runs without a single cloud call:
the fake API below fails the test on any attribute access. The cloud comes in
only without a usable cache, when the camera refuses the cached key (at most
once per KEY_REFRESH_INTERVAL) and on the daily device refresh.
"""

import asyncio
import sys
from datetime import timedelta

import pytest

from .ha_stubs import PKG_NAME, HomeAssistantError
from .test_lan_connect import Recorder, lan  # noqa: F401 - fixture
from .test_offline_start import CACHED, KEY, NEW_KEY, FakeAPI, FakeCache, FakeLANClient, env  # noqa: F401 - fixture


class CloudForbidden:
    """An API client that must not be touched.

    Fails the test on the spot, and also keeps a record, since a call from a
    background task would only fail that task.
    """

    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        pytest.fail(f"LAN preferred called the cloud: api.{name}")


def run(coro):
    return asyncio.run(coro)


class TestNoCloudWithACache:
    def test_start_poll_and_commands_never_touch_the_cloud(self, env):
        api = CloudForbidden()
        coordinator, hass = env(api, FakeCache(CACHED), lan_preferred=True)

        async def scenario():
            await coordinator.async_start()
            lan = FakeLANClient.instances[-1]
            lan.come_up()
            # A manual refresh (update_entity) and a command over the LAN.
            await coordinator.async_refresh()
            await coordinator.async_refresh()
            await coordinator.set_dps({"138": False})
            await hass.settle()
            return lan

        lan = run(scenario())
        assert api.touched == []
        assert coordinator.update_interval is None
        assert coordinator.data["138"] is False
        assert lan.sent == [{"138": False}]
        assert (lan.local_key, lan.ip) == (KEY, "192.168.1.50")
        assert coordinator.last_update_success is True
        assert coordinator.cloud_available is False

    def test_command_without_the_lan_fails_instead_of_using_the_cloud(self, env):
        coordinator, _ = env(CloudForbidden(), FakeCache(CACHED), lan_preferred=True)

        async def scenario():
            await coordinator.async_start()
            with pytest.raises(HomeAssistantError):
                await coordinator.set_dps({"138": True})

        run(scenario())

    def test_the_message_says_why_the_command_failed(self, env):
        # 02-11-2026: a refused handshake (the camera's three LAN sessions in use)
        # was reported as "not reachable on the LAN".
        coordinator, _ = env(CloudForbidden(), FakeCache(CACHED), lan_preferred=True)

        async def scenario():
            await coordinator.async_start()
            messages = []
            with pytest.raises(HomeAssistantError) as gone:
                await coordinator.set_dps({"138": True})
            messages.append(str(gone.value))
            lan = FakeLANClient.instances[-1]
            lan.come_up()
            for failure in ("refused", "busy", "no_answer"):
                lan.fail_with = failure
                with pytest.raises(HomeAssistantError) as err:
                    await coordinator.set_dps({"138": True})
                messages.append(str(err.value))
            return messages

        gone, refused, busy, no_answer = run(scenario())
        assert "not reachable on the LAN" in gone
        assert "refused the LAN connection" in refused and "a running stream takes one" in refused
        assert "busy with an earlier command" in busy
        assert "did not answer" in no_answer and "not reachable" not in no_answer

    def test_availability_follows_the_lan(self, env):
        coordinator, hass = env(CloudForbidden(), FakeCache(CACHED), lan_preferred=True)

        async def scenario():
            await coordinator.async_start()
            assert coordinator.last_update_success is False
            lan = FakeLANClient.instances[-1]
            lan.come_up()
            assert coordinator.last_update_success is True
            lan.go_down()
            assert coordinator.last_update_success is False
            await coordinator.async_refresh()
            assert coordinator.last_update_success is False
            await hass.settle()

        run(scenario())

    def test_daily_device_refresh_is_scheduled(self, env):
        coordinator, hass = env(CloudForbidden(), FakeCache(CACHED), lan_preferred=True)
        run(coordinator.async_start())
        assert timedelta(days=1) in [t[0] for t in hass.timers]


def fallback_timer(hass):
    """The LAN_FALLBACK_DELAY timer the coordinator scheduled last."""
    module = sys.modules[f"{PKG_NAME}.coordinator"]
    timers = [action for delay, action in hass.timers if delay == module.LAN_FALLBACK_DELAY]
    assert timers, "no cloud fallback scheduled"
    return timers[-1]


class TestCloudStandsInForADownLan:
    def test_lan_down_too_long_polls_the_cloud_until_it_is_back(self, env):
        api = FakeAPI()
        coordinator, hass = env(api, FakeCache(CACHED), lan_preferred=True)
        module = sys.modules[f"{PKG_NAME}.coordinator"]

        async def scenario():
            await coordinator.async_start()
            # Never came up: the timer from the start fires.
            fallback_timer(hass)()
            await hass.settle()
            assert api.calls.count("get_device") == 1
            # The normal poll from here on, fast while the LAN is down.
            assert coordinator.update_interval == module.POLL_FAST
            assert coordinator.last_update_success is True
            # Commands use the cloud meanwhile.
            await coordinator.set_dps({"138": True})
            assert "set_dps" in api.calls
            FakeLANClient.instances[-1].come_up()
            # The fallback ends once the LAN has stayed up LAN_STABLE_DELAY.
            assert coordinator.update_interval == module.POLL_FAST
            stable_timer(hass)()
            assert coordinator.update_interval is None
            # A manual refresh with the LAN back does not reach the cloud.
            await coordinator.async_refresh()
            assert api.calls.count("get_device") == 1

        run(scenario())

    def test_a_lan_that_comes_up_in_time_keeps_the_cloud_away(self, env):
        coordinator, hass = env(CloudForbidden(), FakeCache(CACHED), lan_preferred=True)

        async def scenario():
            await coordinator.async_start()
            FakeLANClient.instances[-1].come_up()
            stable_timer(hass)()  # up for LAN_STABLE_DELAY
            fallback_timer(hass)()  # cancelled in Home Assistant; must do nothing if it still fires
            await hass.settle()
            assert coordinator.update_interval is None

        run(scenario())

    def test_going_down_schedules_the_fallback_again(self, env):
        coordinator, hass = env(CloudForbidden(), FakeCache(CACHED), lan_preferred=True)
        module = sys.modules[f"{PKG_NAME}.coordinator"]

        async def scenario():
            await coordinator.async_start()
            lan = FakeLANClient.instances[-1]
            lan.come_up()
            stable_timer(hass)()
            before = len([d for d, _ in hass.timers if d == module.LAN_FALLBACK_DELAY])
            lan.go_down()
            after = len([d for d, _ in hass.timers if d == module.LAN_FALLBACK_DELAY])
            assert after == before + 1

        run(scenario())


def stable_timer(hass):
    module = sys.modules[f"{PKG_NAME}.coordinator"]
    timers = [action for delay, action in hass.timers if delay == module.LAN_STABLE_DELAY]
    assert timers, "no LAN stability timer scheduled"
    return timers[-1]


class TestFlappingLan:
    def test_a_lan_back_for_moments_does_not_keep_the_fallback_away(self, env):
        """Up for less than LAN_STABLE_DELAY at a time: the first drop's timer still starts the fallback."""
        api = FakeAPI()
        coordinator, hass = env(api, FakeCache(CACHED), lan_preferred=True)

        async def scenario():
            await coordinator.async_start()
            lan = FakeLANClient.instances[-1]
            lan.come_up()
            stable_timer(hass)()
            lan.go_down()
            first_drop = fallback_timer(hass)
            for _ in range(3):
                lan.come_up()   # never stays up for LAN_STABLE_DELAY
                lan.go_down()
            assert fallback_timer(hass) is first_drop, "each reconnect restarted the countdown"
            lan.come_up()
            assert coordinator.update_interval is None
            first_drop()
            await hass.settle()
            # The poll runs (the resync after a reconnect also calls the cloud,
            # so the call alone would prove nothing).
            assert coordinator.update_interval is not None, "a flapping LAN kept the cloud fallback away"
            # Stable at last: the fallback ends.
            stable_timer(hass)()
            assert coordinator.update_interval is None

        run(scenario())


class TestResyncAfterReconnect:
    def test_one_cloud_fetch_when_the_lan_comes_back(self, env):
        api = FakeAPI(device={"devId": "cam1", "localKey": KEY, "dps": {"246": "stop"}})
        coordinator, hass = env(api, FakeCache(CACHED), lan_preferred=True)

        async def scenario():
            await coordinator.async_start()
            lan = FakeLANClient.instances[-1]
            lan.come_up()           # the first connect: no resync
            await hass.settle()
            assert "get_device" not in api.calls
            lan.go_down()
            lan.come_up()           # back after a drop: one fetch
            await hass.settle()
            assert api.calls.count("get_device") == 1
            assert coordinator.data["246"] == "stop"
            lan.go_down()
            lan.come_up()           # within LAN_RESYNC_INTERVAL: none
            await hass.settle()
            assert api.calls.count("get_device") == 1

        run(scenario())

    def test_an_unreachable_cloud_is_no_problem(self, env):
        api = FakeAPI(error=TimeoutError())
        coordinator, hass = env(api, FakeCache(CACHED), lan_preferred=True)

        async def scenario():
            await coordinator.async_start()
            lan = FakeLANClient.instances[-1]
            lan.come_up()
            lan.go_down()
            lan.come_up()
            await hass.settle()
            assert coordinator.last_update_success is True

        run(scenario())


class TestCloudSaysOffline:
    def test_an_offline_camera_is_not_shown_with_the_clouds_old_values(self, env):
        api = FakeAPI(device={"devId": "cam1", "localKey": KEY, "isOnline": False, "dps": {"207": 2030}})
        coordinator, hass = env(api, FakeCache(CACHED), lan_preferred=True)

        async def scenario():
            await coordinator.async_start()
            fallback_timer(hass)()  # the LAN never came up
            await hass.settle()
            assert "get_device" in api.calls
            assert coordinator.last_update_success is False

        run(scenario())

    def test_an_offline_camera_does_not_fail_the_setup(self, env):
        """Without a usable cache the start fetches from the cloud; an offline
        camera there must not fail the entry (and with it every other camera)."""
        api = FakeAPI(device={"devId": "cam1", "localKey": KEY, "isOnline": False, "dps": {"207": 2030}})
        coordinator, hass = env(api, FakeCache(), lan_preferred=True)

        async def scenario():
            await coordinator.async_start()   # no exception: the entry sets up
            assert "get_device" in api.calls
            # Unavailable from the start, not five minutes later.
            assert coordinator.last_update_success is False
            # The LAN coming up makes it available, as any LAN session does.
            FakeLANClient.instances[-1].come_up()
            assert coordinator.last_update_success is True

        run(scenario())

    def test_online_or_unknown_is_served(self, env):
        for record in ({"isOnline": True}, {}):
            api = FakeAPI(device={"devId": "cam1", "localKey": KEY, "dps": {"207": 2030}, **record})
            coordinator, hass = env(api, FakeCache(CACHED), lan_preferred=True)

            async def scenario():
                await coordinator.async_start()
                fallback_timer(hass)()
                await hass.settle()
                assert coordinator.last_update_success is True

            run(scenario())


class TestLanConnectionIsReported:
    def test_every_connect_and_drop_updates_the_entities(self, env):
        for preferred in (True, False):
            coordinator, hass = env(FakeAPI(), FakeCache(CACHED), lan_preferred=preferred)

            async def scenario():
                await coordinator.async_start()
                await hass.settle()
                lan = FakeLANClient.instances[-1]
                for change in (lan.come_up, lan.go_down):
                    before = coordinator.listener_updates
                    change()
                    assert coordinator.listener_updates > before, (preferred, change)

            run(scenario())


class TestNothingAfterUnload:
    def test_no_lan_client_or_cache_write_after_stop(self, env):
        cache = FakeCache(CACHED)
        coordinator, hass = env(CloudForbidden(), cache, lan_preferred=True)

        async def scenario():
            await coordinator.async_start()
            await coordinator.stop_lan()
            clients = len(FakeLANClient.instances)
            writes = len(cache.writes)
            await coordinator.start_lan()
            await coordinator._async_update_cache(coordinator.data)
            fallback_timer(hass)()
            await hass.settle()
            assert len(FakeLANClient.instances) == clients
            assert len(cache.writes) == writes
            assert coordinator.update_interval is None

        run(scenario())


class TestCloudWhenTheCacheFallsShort:
    def test_no_cache_fetches_from_the_cloud_once_then_stops_polling(self, env):
        api = FakeAPI()
        cache = FakeCache()
        coordinator, hass = env(api, cache, lan_preferred=True)

        async def scenario():
            await coordinator.async_start()
            FakeLANClient.instances[-1].come_up()
            calls = list(api.calls)
            await coordinator.async_refresh()
            await hass.settle()
            return calls

        calls_at_start = run(scenario())
        assert calls_at_start.count("get_device") == 1
        assert api.calls.count("get_device") == 1
        assert coordinator.update_interval is None
        assert cache.entries["cam1"]["localKey"] == KEY

    @pytest.mark.parametrize("missing", ["lan_ip", "device", "localKey"])
    def test_incomplete_cache_fetches_from_the_cloud(self, env, missing):
        api = FakeAPI()
        partial = {k: v for k, v in CACHED.items() if k != missing}
        coordinator, _ = env(api, FakeCache(partial), lan_preferred=True)
        run(coordinator.async_start())
        assert api.calls.count("get_device") == 1


class TestRefusedKey:
    def test_refused_key_refreshes_once_then_rate_limits(self, env, monkeypatch):
        api = FakeAPI(device={"devId": "cam1", "productId": "pid123", "localKey": NEW_KEY, "dps": {}})
        cache = FakeCache(CACHED)
        coordinator, hass = env(api, cache, lan_preferred=True)
        module = sys.modules[f"{PKG_NAME}.coordinator"]
        clock = [1000.0]
        monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

        async def scenario():
            await coordinator.async_start()
            FakeLANClient.instances[-1].on_key_rejected()
            await hass.settle()
            # The refreshed key is in use and in the cache.
            assert FakeLANClient.instances[-1].local_key == NEW_KEY
            assert cache.entries["cam1"]["localKey"] == NEW_KEY
            # A second refusal within the hour does not ask again.
            clock[0] += 60
            FakeLANClient.instances[-1].on_key_rejected()
            await hass.settle()
            assert api.calls.count("get_device") == 1
            # After the hour it may.
            clock[0] += 3600
            FakeLANClient.instances[-1].on_key_rejected()
            await hass.settle()

        run(scenario())
        assert api.calls == ["get_device", "get_device"]
        assert coordinator.update_interval is None

    def test_unreachable_cloud_keeps_the_cached_key(self, env):
        api = FakeAPI(error=TimeoutError())
        coordinator, hass = env(api, FakeCache(CACHED), lan_preferred=True)

        async def scenario():
            await coordinator.async_start()
            FakeLANClient.instances[-1].on_key_rejected()
            await hass.settle()

        run(scenario())
        assert api.calls == ["get_device"]
        assert FakeLANClient.instances[-1].local_key == KEY

    def test_auto_mode_leaves_a_refused_key_to_the_poll(self, env):
        api = FakeAPI()
        coordinator, hass = env(api, FakeCache(CACHED))

        async def scenario():
            await coordinator.async_start()
            api.calls.clear()
            FakeLANClient.instances[-1].on_key_rejected()
            await hass.settle()

        run(scenario())
        assert api.calls == []


class TestLanClientReportsARefusedKey:
    def test_handshake_error_914_calls_back(self, lan):  # noqa: F811 - fixture
        refused = []
        # A re-paired camera refuses the old key on every version.
        recorder = Recorder(answers={("192.168.1.50", 3.5): 914, ("192.168.1.50", 3.4): 914})
        client = lan(recorder, ip="192.168.1.50", version=3.5, on_key_rejected=lambda: refused.append(True))
        asyncio.run(client._connect())
        assert refused == [True]

    def test_a_914_on_one_version_only_is_not_a_refused_key(self, lan):  # noqa: F811 - fixture
        # Proven 3.5 timed out once; 3.4, which this camera does not speak, answers 914.
        refused = []
        recorder = Recorder(answers={("192.168.1.50", 3.5): 905, ("192.168.1.50", 3.4): 914})
        client = lan(recorder, ip="192.168.1.50", version=3.5, on_key_rejected=lambda: refused.append(True))
        asyncio.run(client._connect())
        assert refused == []

    def test_other_refusals_do_not(self, lan):  # noqa: F811 - fixture
        refused = []
        recorder = Recorder(answers={("192.168.1.50", 3.5): 901})
        client = lan(recorder, ip="192.168.1.50", version=3.5, on_key_rejected=lambda: refused.append(True))
        asyncio.run(client._connect())
        assert refused == []


class TestCloudContactIsReported:
    """Cloud Connection reports the last contact in LAN preferred too, instead of unavailable."""

    def test_unknown_until_the_first_contact(self, env):
        coordinator, hass = env(CloudForbidden(), FakeCache(CACHED), lan_preferred=True)
        run(coordinator.async_start())
        assert coordinator.cloud_reachable is None
        assert coordinator.cloud_checked_at is None

    def test_the_daily_refresh_reports_the_cloud(self, env):
        for error, expected in ((None, True), (TimeoutError(), False)):
            api = FakeAPI(error=error)
            coordinator, hass = env(api, FakeCache(CACHED), lan_preferred=True)

            async def scenario():
                await coordinator.async_start()
                before = coordinator.listener_updates
                await coordinator._async_refresh_device("test")
                assert coordinator.cloud_reachable is expected
                assert coordinator.cloud_checked_at is not None
                assert coordinator.listener_updates > before

            run(scenario())

    def test_a_cloud_start_reports_it(self, env):
        coordinator, hass = env(FakeAPI(), FakeCache(), lan_preferred=True)
        run(coordinator.async_start())
        assert coordinator.cloud_reachable is True

    def test_the_sensor_is_always_available(self):
        from pathlib import Path
        source = (Path(__file__).parent.parent.parent / "custom_components" / "philips_avent" / "binary_sensor.py").read_text()
        block = source.split("class AventCloudConnection")[1].split("\nclass ")[0]
        assert "return self.coordinator.cloud_reachable" in block
        assert "return not self.coordinator.lan_preferred" not in block


class TestCloudContactIsShared:
    """All cameras of an entry share the session: one camera's contact counts for all."""

    def test_another_cameras_contact_answers_for_a_camera_started_from_the_cache(self, env):
        import sys
        policy = sys.modules[f"{PKG_NAME}.cloud_policy"]
        account = policy.AccountCloud()
        coordinator_module = env.module
        hass_a = env(CloudForbidden(), FakeCache(CACHED), lan_preferred=True)[1]
        cached = coordinator_module.PhilipsAventCoordinator(
            hass_a, CloudForbidden(), "cam1", "Nursery", cache=FakeCache(CACHED),
            lan_preferred=True, account_cloud=account)
        fetched = coordinator_module.PhilipsAventCoordinator(
            hass_a, FakeAPI(device={"devId": "cam2", "localKey": KEY, "isOnline": False, "dps": {}}),
            "cam2", "Bedroom", cache=FakeCache(), lan_preferred=True, account_cloud=account)

        async def scenario():
            await cached.async_start()
            assert cached.cloud_reachable is None
            before = cached.listener_updates
            await fetched.async_start()
            assert cached.cloud_reachable is True
            assert cached.cloud_checked_at is not None
            assert cached.listener_updates > before
            # Each camera's own link to Tuya stays its own.
            assert fetched.camera_online is False
            assert cached.camera_online is None

        run(scenario())

    def test_without_an_account_view_it_is_per_camera(self, env):
        coordinator, hass = env(FakeAPI(), FakeCache(), lan_preferred=True)
        run(coordinator.async_start())
        assert coordinator.cloud_reachable is True
