"""Tests for the offline cache format and the cloud-outage decisions.

The cache lets a camera start on the LAN while the Tuya cloud is away. It
holds the camera's localKey, which controls the camera on the LAN by itself,
so besides the round trip these tests pin down that the key never reaches the
diagnostics dump or a log line.
"""

import asyncio
import json

import aiohttp
from api import TuyaAPIError
from cloud_policy import (
    CLOUD_RETRY_MAX,
    FAILURE_API,
    FAILURE_AUTH,
    FAILURE_UNREACHABLE,
    START_AUTH_FAILED,
    START_LAN_ONLY,
    START_NOT_READY,
    CloudState,
    classify_cloud_error,
    cloud_retry_seconds,
    is_auth_error,
    keep_serving_without_cloud,
    startup_mode,
)
from diagnostics import async_get_config_entry_diagnostics
from offline_cache import (
    CACHE_VERSION,
    DPS_SAVE_INTERVAL,
    build_entry,
    cache_key,
    describe,
    device_info_from_entry,
    dump_cache,
    migrate_cache,
    parse_cache,
    restorable_dps,
    save_due,
    usable_for_fallback,
)
from redact import redact_secrets

KEY = "0123456789abcdef"
CLOUD_DEVICE = {
    "devId": "cam1",
    "name": "Nursery",
    "productId": "pid123",
    "verSw": "1.1.12",
    "localKey": KEY,
    "ip": "203.0.113.7",
    "dps": {"207": 2345},
}


def entry(**overrides):
    args = {
        "local_key": KEY,
        "device_info": CLOUD_DEVICE,
        "dps": {"207": 2345, "138": True},
        "lan_ip": "192.168.1.50",
        "lan_version": 3.5,
    }
    args.update(overrides)
    return build_entry(**args)


class TestEntry:
    def test_keeps_only_the_listed_device_fields(self):
        # The cloud record carries the household's public IP; it stays out.
        assert entry()["device"] == {"productId": "pid123", "name": "Nursery", "verSw": "1.1.12"}

    def test_no_key_means_no_entry(self):
        assert entry(local_key=None) is None
        assert entry(local_key="") is None

    def test_unknown_protocol_version_is_left_out(self):
        assert "lan_version" not in entry(lan_version=9.9)

    def test_address_is_optional(self):
        assert "lan_ip" not in entry(lan_ip=None)

    def test_cache_key_is_per_entry(self):
        assert cache_key("abc") == "philips_avent.abc"


class TestRoundTrip:
    def test_survives_json(self):
        stored = json.loads(json.dumps(dump_cache({"cam1": entry()})))
        assert parse_cache(stored) == {"cam1": entry()}

    def test_malformed_entries_are_dropped_not_fatal(self):
        raw = {"cameras": {"cam1": entry(), "cam2": {"localKey": ""}, "cam3": "junk", "cam4": {"dps": {}}}}
        assert list(parse_cache(raw)) == ["cam1"]

    def test_nothing_stored_is_an_empty_cache(self):
        assert parse_cache(None) == {}
        assert parse_cache({"cameras": []}) == {}

    def test_wrong_typed_fields_are_dropped(self):
        raw = {"cameras": {"cam1": {"localKey": KEY, "device": "x", "dps": [1], "lan_ip": 5}}}
        assert parse_cache(raw)["cam1"] == {"localKey": KEY, "device": {}, "dps": {}}


class TestVersioning:
    def test_same_major_passes_through(self):
        data = dump_cache({"cam1": entry()})
        assert migrate_cache(CACHE_VERSION, 0, data) == data

    def test_unknown_major_is_dropped(self):
        assert migrate_cache(CACHE_VERSION + 1, 1, dump_cache({"cam1": entry()})) == {"cameras": {}}


class TestSaveDue:
    def test_first_entry_is_written(self):
        assert save_due(None, entry(), None, 0.0)

    def test_identical_entry_is_not_written(self):
        assert not save_due(entry(), entry(), 0.0, 10_000.0)

    def test_changed_key_or_address_is_written_at_once(self):
        assert save_due(entry(), entry(local_key="f" * 16), 100.0, 101.0)
        assert save_due(entry(), entry(lan_ip="192.168.1.51"), 100.0, 101.0)

    def test_dps_only_change_waits_for_the_interval(self):
        newer = entry(dps={"207": 2400})
        assert not save_due(entry(), newer, 100.0, 100.0 + DPS_SAVE_INTERVAL - 1)
        assert save_due(entry(), newer, 100.0, 100.0 + DPS_SAVE_INTERVAL)

    def test_nothing_to_write(self):
        assert not save_due(entry(), None, None, 0.0)


class TestRestorableDps:
    def test_settings_and_measurements_are_restored(self):
        cached = entry(dps={"207": 2345, "138": True, "209": 5, "244": 3600})
        assert restorable_dps(cached) == {"207": 2345, "138": True, "209": 5, "244": 3600}

    def test_what_happened_at_snapshot_time_is_not(self):
        # Taken while a lullaby played: after a restart it showed as playing,
        # 90 minutes left, until the camera pushed DPS 246 again.
        cached = entry(dps={"246": "playing", "201": "play", "245": 5400, "242": 600,
                            "253": "1", "212": "{}", "250": "{}", "141": 60, "3": "{}", "209": 5})
        assert restorable_dps(cached) == {"209": 5}

    def test_no_entry_or_no_dps(self):
        assert restorable_dps(None) == {}
        assert restorable_dps({"localKey": KEY}) == {}


class TestTheKeyStaysPrivate:
    def test_redaction_hides_the_cached_key(self):
        assert KEY not in repr(redact_secrets(dump_cache({"cam1": entry()})))

    def test_device_info_from_the_cache_has_no_key(self):
        assert KEY not in repr(device_info_from_entry(entry()))

    def test_log_summary_names_parts_not_values(self):
        text = describe(entry())
        assert KEY not in text
        assert "192.168.1.50" not in text
        assert text == "key, 2 DPS, LAN address, protocol 3.5"

    def test_diagnostics_of_a_camera_started_from_the_cache(self):
        class Coordinator:
            camera_name = "Nursery"
            data = entry()["dps"]
            lan_connected = True
            cloud_available = False
            cloud_failure = FAILURE_UNREACHABLE
            update_interval = "0:02:00"
            rssi = None
            device_info = device_info_from_entry(entry())

        class Entry:
            entry_id = "e1"
            data = {"sid": "s", "cameras": [{"id": "cam1", "name": "Nursery"}]}

        class Hass:
            data = {"philips_avent": {"e1": {"coordinators": {"cam1": Coordinator()}}}}

        diag = asyncio.run(async_get_config_entry_diagnostics(Hass(), Entry()))
        assert KEY not in json.dumps(diag)
        assert diag["devices"]["cam1"]["cloud_connected"] is False


class TestClassify:
    def test_auth_codes(self):
        assert classify_cloud_error(TuyaAPIError("USER_SESSION_LOSS", "x")) == FAILURE_AUTH
        assert classify_cloud_error(TuyaAPIError("SID_INVALID", "x")) == FAILURE_AUTH
        assert is_auth_error(TuyaAPIError("SID_INVALID", "x"))

    def test_other_tuya_answers_are_api_errors(self):
        assert classify_cloud_error(TuyaAPIError("DEVICE_NOT_EXIST", "x")) == FAILURE_API

    def test_network_failures_are_unreachable(self):
        assert classify_cloud_error(aiohttp.ClientConnectionError("dns")) == FAILURE_UNREACHABLE
        assert classify_cloud_error(TimeoutError()) == FAILURE_UNREACHABLE
        assert classify_cloud_error(json.JSONDecodeError("x", "<html>", 0)) == FAILURE_UNREACHABLE

    def test_something_else_is_not_an_outage(self):
        assert classify_cloud_error(KeyError("x")) == FAILURE_API


class TestStartupMode:
    def test_unreachable_with_cache_starts_on_the_lan(self):
        assert startup_mode(FAILURE_UNREACHABLE, True) == START_LAN_ONLY

    def test_auth_error_stays_an_auth_failure_even_with_cache(self):
        assert startup_mode(FAILURE_AUTH, True) == START_AUTH_FAILED

    def test_no_cache_is_not_ready(self):
        assert startup_mode(FAILURE_UNREACHABLE, False) == START_NOT_READY

    def test_api_error_is_not_ready(self):
        assert startup_mode(FAILURE_API, True) == START_NOT_READY

    def test_fallback_needs_a_key(self):
        assert usable_for_fallback(entry())
        assert not usable_for_fallback(None)


class TestRetry:
    def test_starts_at_the_poll_interval_and_doubles(self):
        assert [cloud_retry_seconds(n, 120) for n in (1, 2, 3, 4)] == [120, 240, 480, CLOUD_RETRY_MAX]

    def test_never_beyond_the_cap(self):
        assert cloud_retry_seconds(50, 30) == CLOUD_RETRY_MAX


class TestCloudState:
    def test_transitions_are_reported_once(self):
        state = CloudState()
        assert state.succeeded() is False
        assert state.failed(FAILURE_UNREACHABLE) is True
        assert state.failed(FAILURE_UNREACHABLE) is False
        assert state.failures == 2
        assert state.succeeded() is True
        assert state.failures == 0
        assert state.last_failure is None

    def test_a_failure_before_any_success_counts_as_going_away(self):
        assert CloudState().failed(FAILURE_UNREACHABLE) is True

    def test_serving_without_cloud_needs_lan_and_data(self):
        assert keep_serving_without_cloud(True, True)
        assert not keep_serving_without_cloud(False, True)
        assert not keep_serving_without_cloud(True, False)
