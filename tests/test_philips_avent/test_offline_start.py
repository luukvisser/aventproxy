"""The coordinator starting and running without the Tuya cloud.

Loads coordinator.py against the stand-ins in ha_stubs.py, with a fake cloud
API and a fake LAN client, and walks through the startup decision (unreachable
cloud with a cache starts on the LAN, an auth error stays an auth failure, no
cache stays not-ready) and the way back once the cloud answers.
"""

import asyncio
import logging
import sys

import aiohttp
import pytest

from .ha_stubs import PKG_NAME, ConfigEntryAuthFailed, ConfigEntryNotReady, FakeHass, load

KEY = "0123456789abcdef"
NEW_KEY = "fedcba9876543210"
CACHED = {
    "localKey": KEY,
    "device": {"productId": "pid123", "name": "Nursery"},
    "dps": {"207": 2345, "138": True},
    "lan_ip": "192.168.1.50",
    "lan_version": 3.5,
}


class FakeCache:
    def __init__(self, entry=None):
        self.entries = {"cam1": entry} if entry else {}
        self.writes = []

    def get(self, camera_id):
        return self.entries.get(camera_id)

    async def async_update(self, camera_id, entry):
        self.entries[camera_id] = entry
        self.writes.append(entry)


class FakeAPI:
    """The cloud: answers with `device`, or raises `error` when set."""

    def __init__(self, error=None, device=None):
        self.error = error
        self.device = device or {"devId": "cam1", "productId": "pid123", "localKey": KEY, "dps": {"207": 2400}}
        self.calls = []

    async def get_device(self, dev_id):
        self.calls.append("get_device")
        if self.error:
            raise self.error
        return dict(self.device)

    async def get_rssi(self, dev_id):
        self.calls.append("get_rssi")
        return {"value": -55}

    async def get_sleep_day(self, dev_id, date):
        self.calls.append("get_sleep_day")
        return None

    async def set_dps(self, dev_id, dps):
        self.calls.append("set_dps")
        return {"success": True}


class FakeLANClient:
    instances = []

    def __init__(self, hass, device_id, local_key, on_dps_update, on_address_change=None, last_ip=None,
                 last_protocol="", *, on_session_up=None, claims=None, ip=None, version=None,
                 on_connection_change=None, on_key_rejected=None, hosts=None, bound=None):
        self.local_key = local_key
        self.ip = ip
        self.version = version
        self.on_session_up = on_session_up
        self.protocol = ""
        self.bound = bound
        self.hosts = hosts
        self.on_connection_change = on_connection_change
        self.on_key_rejected = on_key_rejected
        self.sent = []
        self.connected = False
        self.started = False
        # Set to make the next commands fail as the real client reports it.
        self.fail_with = None
        self.last_command_failure = None
        # Whether the session proves it is this camera; a 3.3 one does that
        # only with its first readable message.
        self.identity_proven = True
        FakeLANClient.instances.append(self)

    @property
    def verified(self):
        return self.connected and self.identity_proven

    # The real client's names for the same (upstream's `proven`; `session_ip` for the cache).
    @property
    def proven(self):
        return self.verified

    @property
    def session_ip(self):
        return self.ip

    @property
    def session_version(self):
        return self.version if self.connected else None

    def set_local_key(self, key):
        self.local_key = key

    async def start(self):
        self.started = True

    async def stop(self):
        self.started = False

    async def set_dps(self, dps, *, repeatable=True):
        if not self.connected:
            return None
        if self.fail_with:
            self.last_command_failure = self.fail_with
            return None
        self.sent.append(dps)
        return {"success": True}

    def come_up(self):
        self.connected = True
        self.on_connection_change(True)

    def go_down(self):
        self.connected = False
        self.on_connection_change(False)


@pytest.fixture
def env(monkeypatch):
    module = load(monkeypatch, "coordinator")
    api_module = sys.modules[f"{PKG_NAME}.api"]
    FakeLANClient.instances = []
    monkeypatch.setattr(module, "TuyaLANClient", FakeLANClient)

    def make(api, cache, lan_preferred=False):
        hass = FakeHass()
        coordinator = module.PhilipsAventCoordinator(
            hass, api, "cam1", "Nursery", cache=cache, lan_preferred=lan_preferred
        )
        return coordinator, hass

    make.TuyaAPIError = api_module.TuyaAPIError
    make.module = module
    return make


def run(coro):
    return asyncio.run(coro)


def unreachable():
    return aiohttp.ClientConnectionError("Cannot connect to host")


class TestStartupDecision:
    def test_unreachable_cloud_with_cache_starts_on_the_lan(self, env, caplog):
        caplog.set_level(logging.DEBUG)
        coordinator, _ = env(FakeAPI(error=unreachable()), FakeCache(CACHED))

        run(coordinator.async_start())

        assert coordinator.data == CACHED["dps"]
        assert coordinator.device_info == CACHED["device"]
        assert coordinator.cloud_available is False
        lan = FakeLANClient.instances[-1]
        assert (lan.local_key, lan.ip, lan.version) == (KEY, "192.168.1.50", 3.5)
        # Nothing current to show until the LAN session is up.
        assert coordinator.last_update_success is False
        assert KEY not in caplog.text
        assert "runs on the LAN" in caplog.text

    def test_a_lullaby_playing_in_the_snapshot_is_not_restored(self, env):
        cached = {**CACHED, "dps": {**CACHED["dps"], "246": "playing", "245": 5400}}
        coordinator, _ = env(FakeAPI(error=unreachable()), FakeCache(cached))

        run(coordinator.async_start())

        assert coordinator.data == CACHED["dps"]

    def test_timeout_counts_as_unreachable(self, env):
        coordinator, _ = env(FakeAPI(error=TimeoutError()), FakeCache(CACHED))
        run(coordinator.async_start())
        assert coordinator.data == CACHED["dps"]

    def test_auth_error_raises_auth_failed_even_with_cache(self, env):
        api = FakeAPI()
        coordinator, _ = env(api, FakeCache(CACHED))
        api.error = env.TuyaAPIError("USER_SESSION_LOSS", "session lost")
        with pytest.raises(ConfigEntryAuthFailed):
            run(coordinator.async_start())
        assert FakeLANClient.instances == []

    def test_no_cache_stays_not_ready(self, env):
        coordinator, _ = env(FakeAPI(error=unreachable()), FakeCache())
        with pytest.raises(ConfigEntryNotReady):
            run(coordinator.async_start())
        assert FakeLANClient.instances == []

    def test_tuya_error_answer_stays_not_ready(self, env):
        api = FakeAPI()
        coordinator, _ = env(api, FakeCache(CACHED))
        api.error = env.TuyaAPIError("SOMETHING_ELSE", "no")
        with pytest.raises(ConfigEntryNotReady):
            run(coordinator.async_start())

    def test_cloud_start_uses_the_cached_lan_address(self, env):
        coordinator, _ = env(FakeAPI(), FakeCache(CACHED))
        run(coordinator.async_start())
        assert coordinator.cloud_available is True
        assert FakeLANClient.instances[-1].ip == "192.168.1.50"

    def test_cloud_fetch_fills_the_cache(self, env):
        cache = FakeCache()
        coordinator, _ = env(FakeAPI(), cache)
        run(coordinator.async_start())
        assert cache.entries["cam1"]["localKey"] == KEY
        assert cache.entries["cam1"]["device"] == {"productId": "pid123"}
        assert cache.entries["cam1"]["dps"] == {"207": 2400}


class TestRunningOnTheLan:
    def test_availability_follows_the_lan_while_the_cloud_is_away(self, env):
        coordinator, hass = env(FakeAPI(error=unreachable()), FakeCache(CACHED))

        async def scenario():
            await coordinator.async_start()
            lan = FakeLANClient.instances[-1]
            lan.come_up()
            assert coordinator.last_update_success is True
            lan.go_down()
            assert coordinator.last_update_success is False
            await hass.settle()

        run(scenario())

    def test_failed_poll_keeps_serving_while_the_lan_is_up(self, env):
        coordinator, hass = env(FakeAPI(error=unreachable()), FakeCache(CACHED))

        async def scenario():
            await coordinator.async_start()
            FakeLANClient.instances[-1].come_up()
            await coordinator.async_refresh()
            assert coordinator.last_update_success is True
            assert coordinator.data == CACHED["dps"]
            await hass.settle()

        run(scenario())

    def test_failed_poll_without_lan_marks_unavailable(self, env):
        coordinator, _ = env(FakeAPI(error=unreachable()), FakeCache(CACHED))

        async def scenario():
            await coordinator.async_start()
            await coordinator.async_refresh()
            assert coordinator.last_update_success is False

        run(scenario())

    def test_retry_backs_off(self, env):
        coordinator, _ = env(FakeAPI(error=unreachable()), FakeCache(CACHED))

        async def scenario():
            await coordinator.async_start()
            seen = []
            for _ in range(4):
                await coordinator.async_refresh()
                seen.append(coordinator.update_interval.total_seconds())
            return seen

        seen = run(scenario())
        assert seen == sorted(seen)
        assert seen[-1] > seen[0]

    def test_lan_address_reaches_the_cache_when_the_session_comes_up(self, env):
        cache = FakeCache(CACHED)
        coordinator, hass = env(FakeAPI(error=unreachable()), cache)

        async def scenario():
            await coordinator.async_start()
            lan = FakeLANClient.instances[-1]
            lan.ip = "192.168.1.60"
            lan.come_up()
            await hass.settle()

        run(scenario())
        assert cache.entries["cam1"]["lan_ip"] == "192.168.1.60"

    def test_an_unproven_33_session_reaches_the_cache_only_once_proven(self, env):
        # A 3.3 handshake succeeds with any Tuya device: until the session's
        # first readable message it may be another device's address.
        cache = FakeCache(CACHED)
        coordinator, hass = env(FakeAPI(error=unreachable()), cache)

        async def scenario():
            await coordinator.async_start()
            lan = FakeLANClient.instances[-1]
            lan.ip, lan.version, lan.identity_proven = "192.168.1.61", 3.3, False
            lan.come_up()
            await hass.settle()
            before = dict(cache.entries.get("cam1") or {})
            lan.identity_proven = True
            coordinator._on_lan_dps_update({"101": True})
            await hass.settle()
            return before

        before = run(scenario())
        assert before.get("lan_ip") != "192.168.1.61"
        assert cache.entries["cam1"]["lan_ip"] == "192.168.1.61"
        assert cache.entries["cam1"]["lan_version"] == 3.3


class TestRecovery:
    def test_cloud_comes_back_by_itself(self, env, caplog):
        caplog.set_level(logging.INFO)
        api = FakeAPI(error=unreachable())
        coordinator, hass = env(api, FakeCache(CACHED))

        async def scenario():
            await coordinator.async_start()
            FakeLANClient.instances[-1].come_up()
            await coordinator.async_refresh()
            api.error = None
            await coordinator.async_refresh()
            await hass.settle()

        run(scenario())
        assert coordinator.cloud_available is True
        assert coordinator.data["207"] == 2400
        assert coordinator.update_interval.total_seconds() in (30, 120)
        assert "answers again" in caplog.text
        assert caplog.text.count("runs on the LAN") + caplog.text.count("running on the LAN") == 1

    def test_new_key_from_the_cloud_reconnects_the_lan(self, env):
        api = FakeAPI(error=unreachable())
        coordinator, hass = env(api, FakeCache(CACHED))

        async def scenario():
            await coordinator.async_start()
            api.error = None
            api.device = {**api.device, "localKey": NEW_KEY}
            await coordinator.async_refresh()
            await hass.settle()

        run(scenario())
        # Handed to the running client (upstream 8fd01cd), which reconnects with
        # it at its next turn; no second client.
        assert len(FakeLANClient.instances) == 1
        assert FakeLANClient.instances[0].local_key == NEW_KEY
        assert FakeLANClient.instances[0].started


class TestStoreRemoval:
    def test_setup_and_removal_share_one_instance(self, monkeypatch):
        store = load(monkeypatch, "store")
        assert store.offline_cache(FakeHass(), "entry1") is store.offline_cache(FakeHass(), "entry1")

    def test_a_save_in_flight_finishes_before_the_file_goes_and_none_follows(self, monkeypatch):
        store = load(monkeypatch, "store")
        cache = store.offline_cache(FakeHass(), "entry2")
        backing = cache._store
        entry = {"localKey": KEY, "device": {}, "dps": {}}

        async def scenario():
            backing.gate = asyncio.Event()
            saving = asyncio.create_task(cache.async_update("cam1", entry))
            await asyncio.sleep(0)
            removing = asyncio.create_task(store.offline_cache(FakeHass(), "entry2").async_remove())
            await asyncio.sleep(0)
            backing.gate.set()
            await asyncio.gather(saving, removing)
            backing.gate = None
            await cache.async_update("cam1", {**entry, "lan_ip": "192.168.1.50"})

        asyncio.run(scenario())
        assert backing.events == ["save started", "saved", "removed"]
        assert backing.data is None
