"""Tests for the bridge-config camera payload builder.

Imports the real ``build_cameras_payload`` so a contract drift (e.g. someone
changing it to omit product_id) is caught here, not just in the Go integration
tests.

The import below uses the leaf form ``from payload import ...`` to match the
project convention (see ``conftest.py``): the full path
``custom_components.philips_avent.payload`` cannot be used because it triggers
``custom_components/philips_avent/__init__.py``, which imports ``homeassistant``
— a runtime dependency we deliberately keep out of unit tests.
"""

import json

from payload import (
    bridge_config_filename,
    build_bridge_config,
    build_cameras_payload,
    dps_delta,
    orphan_bridge_configs,
    truncated_dps,
)


class TestBuildCamerasPayloadShape:
    def test_product_id_propagated_for_scd951(self):
        cameras = [
            {"deviceId": "abc123", "deviceName": "Baby", "productId": "selj2idknqhjnids"},
        ]
        payload = build_cameras_payload(cameras)
        assert len(payload) == 1
        assert payload[0] == {
            "camera_id": "abc123",
            "camera_name": "Baby",
            "product_id": "selj2idknqhjnids",
            "local_key": "",
            "p2p_password": "",
            "lan_ip": "",
            "lan_protocol": "",
            "lan_hosts": [],
            "lan_clarity": 4,
            "stream_source": "",
        }

    def test_product_id_empty_for_scd973(self):
        """No regression for devices without a productId — empty string passes through."""
        cameras = [{"deviceId": "def456", "deviceName": "Cam"}]
        assert build_cameras_payload(cameras)[0]["product_id"] == ""

    def test_mixed_cameras(self):
        cameras = [
            {"deviceId": "abc123", "deviceName": "Baby SCD951", "productId": "selj2idknqhjnids"},
            {"deviceId": "def456", "deviceName": "Baby SCD973", "productId": ""},
        ]
        payload = build_cameras_payload(cameras)
        assert payload[0]["product_id"] == "selj2idknqhjnids"
        assert payload[1]["product_id"] == ""

    def test_devid_fallback_key(self):
        """Raw Tuya discovery dicts use 'devId' not 'deviceId'."""
        cameras = [{"devId": "xyz789", "deviceName": "Cam", "productId": "someproduct"}]
        assert build_cameras_payload(cameras)[0]["camera_id"] == "xyz789"

    def test_product_key_fallback(self):
        """Some Tuya endpoints return 'productKey' instead of 'productId'."""
        cameras = [{"deviceId": "abc", "deviceName": "Cam", "productKey": "alt-product-key"}]
        assert build_cameras_payload(cameras)[0]["product_id"] == "alt-product-key"

    def test_snake_case_product_id_fallback(self):
        """A stored-entry dict (pre-conversion) uses 'product_id'."""
        cameras = [{"id": "abc", "name": "Cam", "product_id": "stored-pid"}]
        payload = build_cameras_payload(cameras)
        assert payload[0]["camera_id"] == "abc"
        assert payload[0]["camera_name"] == "Cam"
        assert payload[0]["product_id"] == "stored-pid"

    def test_name_fallback_to_default(self):
        """A camera with no name at all gets the literal default 'camera'."""
        cameras = [{"deviceId": "abc"}]
        assert build_cameras_payload(cameras)[0]["camera_name"] == "camera"

    def test_empty_input(self):
        assert build_cameras_payload([]) == []


class TestBuildCamerasPayloadPrecedence:
    """When multiple keys are present, the documented precedence applies."""

    def test_device_id_wins_over_devid(self):
        cameras = [{"deviceId": "first", "devId": "second"}]
        assert build_cameras_payload(cameras)[0]["camera_id"] == "first"

    def test_device_name_wins_over_name(self):
        cameras = [{"deviceId": "abc", "deviceName": "first", "name": "second"}]
        assert build_cameras_payload(cameras)[0]["camera_name"] == "first"

    def test_product_id_wins_over_product_key(self):
        cameras = [
            {"deviceId": "abc", "deviceName": "Cam", "productId": "id-form", "productKey": "key-form"},
        ]
        assert build_cameras_payload(cameras)[0]["product_id"] == "id-form"


class TestBuildBridgeConfig:
    """The JSON contract with ``cmd/addon/addon.go::BridgeConfig``."""

    BASE = {
        "signing_key": "sk",
        "sid": "az1661958",
        "ecode": "E",
        "partner": "P",
        "app_key": "AK",
        "device_id": "D",
        "package_name": "com.philips.ph.babymonitorplus",
        "api_host": "a1.tuyaus.com",
        "bridge_port": 38554,
        "cameras": [{"deviceId": "abc123", "deviceName": "Erik", "productId": "p1"}],
    }

    def test_api_host_is_written_for_the_bridge(self):
        # Without this the add-on would talk to the EU data center whatever the
        # account's region is (issues #44, #58).
        config = build_bridge_config(**self.BASE)
        assert config["api_host"] == "a1.tuyaus.com"

    def test_keys_match_the_go_struct(self):
        config = build_bridge_config(**self.BASE)
        assert set(config) == {
            "signing_key", "sid", "ecode", "partner", "app_key", "device_id",
            "package_name", "api_host", "talkback", "uid", "lan_streaming", "webrtc",
            "webrtc_lan_only", "max_stream_minutes", "aac_audio", "bridge_port", "cameras",
        }

    def test_max_stream_minutes(self):
        # A stream ends this long after it started; 0, the default, is no limit.
        assert build_bridge_config(**self.BASE)["max_stream_minutes"] == 0
        assert build_bridge_config(**self.BASE, max_stream_minutes=0)["max_stream_minutes"] == 0
        assert build_bridge_config(**self.BASE, max_stream_minutes=-3)["max_stream_minutes"] == 0
        assert build_bridge_config(**self.BASE, max_stream_minutes=25)["max_stream_minutes"] == 25

    def test_aac_audio(self):
        # On by default; off must reach the bridge as false, since the bridge
        # reads a missing key as on.
        assert build_bridge_config(**self.BASE)["aac_audio"] is True
        assert build_bridge_config(**self.BASE, aac_audio=False)["aac_audio"] is False

    def test_setup_and_options_carry_aac_audio(self):
        from pathlib import Path
        component = Path(__file__).parent.parent.parent / "custom_components" / "philips_avent"
        init = (component / "__init__.py").read_text()
        assert "aac_audio=entry.options.get(CONF_AAC_AUDIO, DEFAULT_AAC_AUDIO)" in init
        flow = (component / "config_flow.py").read_text()
        assert "vol.Optional(CONF_AAC_AUDIO, default=current_aac_audio): bool" in flow
        import json
        for name in ("strings.json", "translations/en.json", "translations/fr.json"):
            data = json.loads((component / name).read_text())["options"]["step"]["init"]
            assert "aac_audio" in data["data"] and "aac_audio" in data["data_description"], name

    def test_setup_and_options_carry_max_stream_minutes(self):
        from pathlib import Path
        component = Path(__file__).parent.parent.parent / "custom_components" / "philips_avent"
        init = (component / "__init__.py").read_text()
        assert "max_stream_minutes=entry.options.get(CONF_MAX_STREAM_MINUTES, DEFAULT_MAX_STREAM_MINUTES)" in init
        flow = (component / "config_flow.py").read_text()
        assert "vol.Optional(CONF_MAX_STREAM_MINUTES, default=current_max_stream_minutes)" in flow
        import json
        for name in ("strings.json", "translations/en.json", "translations/fr.json"):
            data = json.loads((component / name).read_text())["options"]["step"]["init"]
            assert "max_stream_minutes" in data["data"] and "max_stream_minutes" in data["data_description"], name

    def test_cameras_use_the_shared_payload_builder(self):
        config = build_bridge_config(**self.BASE)
        assert config["cameras"] == [
            {
                "camera_id": "abc123", "camera_name": "Erik", "product_id": "p1",
                "local_key": "", "p2p_password": "", "lan_ip": "", "lan_protocol": "",
                "lan_hosts": [], "lan_clarity": 4, "stream_source": "",
            },
        ]

    def test_cameras_carry_lan_fields(self):
        lan = {"abc123": {"local_key": "lk", "p2p_password": "pw", "lan_ip": "192.168.1.9", "lan_protocol": "3.5"}}
        cameras = [*self.BASE["cameras"], {"deviceId": "other", "deviceName": "Two"}]
        config = build_bridge_config(**{**self.BASE, "cameras": cameras}, uid="u1", lan=lan)
        assert config["uid"] == "u1"
        assert config["cameras"][0]["local_key"] == "lk"
        assert config["cameras"][0]["p2p_password"] == "pw"
        assert config["cameras"][0]["lan_ip"] == "192.168.1.9"
        assert config["cameras"][0]["lan_protocol"] == "3.5"
        keys = ("local_key", "p2p_password", "lan_ip", "lan_protocol")
        assert [config["cameras"][1][k] for k in keys] == ["", "", "", ""]

    def test_lan_streaming_defaults_on(self):
        assert build_bridge_config(**self.BASE)["lan_streaming"] is True

    def test_lan_streaming_written_when_off(self):
        assert build_bridge_config(**self.BASE, lan_streaming=False)["lan_streaming"] is False

    def test_lan_secrets_stay_out_while_lan_streaming_is_off(self):
        lan = {"abc123": {"local_key": "lk", "p2p_password": "pw", "lan_ip": "192.168.1.9", "lan_protocol": "3.5"}}
        config = build_bridge_config(**self.BASE, lan_streaming=False, lan=lan)
        camera = config["cameras"][0]
        assert (camera["local_key"], camera["p2p_password"]) == ("", "")
        # Not credentials, and the seed for the next setup's LAN client.
        assert (camera["lan_ip"], camera["lan_protocol"]) == ("192.168.1.9", "3.5")
        assert lan["abc123"]["local_key"] == "lk"

    def test_lan_off_gives_identical_json_across_restarts(self):
        # run.sh restarts the bridge on any md5 change (issue #73).
        lan = {"abc123": {"local_key": "lk", "p2p_password": "", "lan_ip": "192.168.1.9", "lan_protocol": "3.3"}}
        again = {"abc123": {"local_key": "lk", "p2p_password": "pw", "lan_ip": "192.168.1.9", "lan_protocol": "3.3"}}
        first = json.dumps(build_bridge_config(**self.BASE, lan_streaming=False, lan=lan), indent=2)
        second = json.dumps(build_bridge_config(**self.BASE, lan_streaming=False, lan=again), indent=2)
        assert first == second

    def test_unchanged_inputs_give_identical_json(self):
        # run.sh restarts the bridge on any md5 change (issue #73).
        lan = {"abc123": {"local_key": "lk", "p2p_password": "pw", "lan_ip": "192.168.1.9", "lan_protocol": "3.3"}}
        first = json.dumps(build_bridge_config(**self.BASE, uid="u1", lan=lan), indent=2)
        second = json.dumps(build_bridge_config(**self.BASE, uid="u1", lan=lan), indent=2)
        assert first == second

    def test_config_is_json_serialisable(self):
        assert json.loads(json.dumps(build_bridge_config(**self.BASE))) == build_bridge_config(**self.BASE)


class TestOrphanBridgeConfigs:
    """Leftover per-entry config files (issue #52).

    Deleting and re-adding the integration mints a new entry id, so the old file
    stayed in the config directory. The add-on then had two to choose from and
    could keep using the dead entry's session and camera id, which Tuya answers
    with "No access".
    """

    LIVE = "01ABCLIVEENTRY"
    DEAD = "01XYZDEADENTRY"

    def test_filename_is_named_after_the_entry(self):
        assert bridge_config_filename(self.LIVE) == f"philips_avent_bridge_{self.LIVE}.json"

    def test_file_of_a_missing_entry_is_an_orphan(self):
        names = [bridge_config_filename(self.LIVE), bridge_config_filename(self.DEAD)]
        assert orphan_bridge_configs(names, {self.LIVE}) == [bridge_config_filename(self.DEAD)]

    def test_live_entries_are_kept(self):
        names = [bridge_config_filename(self.LIVE), bridge_config_filename(self.DEAD)]
        assert orphan_bridge_configs(names, {self.LIVE, self.DEAD}) == []

    def test_multiple_accounts_keep_their_own_files(self):
        second = "01SECONDACCOUNT"
        names = [bridge_config_filename(x) for x in (self.LIVE, second, self.DEAD)]
        assert orphan_bridge_configs(names, {self.LIVE, second}) == [bridge_config_filename(self.DEAD)]

    def test_unrelated_files_are_never_touched(self):
        names = [
            "philips_avent_bridge.json",       # the legacy single-entry file
            "configuration.yaml",
            "philips_avent_bridge_.json",      # no entry id in the name
            "something_philips_avent_bridge_x.json",
            bridge_config_filename(self.DEAD),
        ]
        assert orphan_bridge_configs(names, {self.LIVE}) == [bridge_config_filename(self.DEAD)]

    def test_no_entries_at_all_means_every_file_is_an_orphan(self):
        names = [bridge_config_filename(self.LIVE), bridge_config_filename(self.DEAD)]
        assert sorted(orphan_bridge_configs(names, set())) == sorted(names)

    def test_empty_directory(self):
        assert orphan_bridge_configs([], {self.LIVE}) == []


class TestDpsDelta:
    """The debug line that says what a poll changed (issues #42, #61)."""

    def test_only_changed_and_new_keys(self):
        old = {"207": 2310, "141": "", "212": "same"}
        new = {"207": 2600, "141": "", "212": "same", "250": "motion_detection"}
        assert dps_delta(old, new) == {"207": 2600, "250": "motion_detection"}

    def test_no_previous_state_reports_everything(self):
        assert dps_delta(None, {"207": 2310}) == {"207": 2310}
        assert dps_delta({}, {"207": 2310}) == {"207": 2310}

    def test_nothing_changed_is_empty(self):
        assert dps_delta({"207": 2310}, {"207": 2310}) == {}
        assert dps_delta({"207": 2310}, {}) == {}
        assert dps_delta({"207": 2310}, None) == {}

    def test_a_value_going_empty_still_counts(self):
        # An alert DPS clearing is a change worth seeing in the log.
        assert dps_delta({"141": "decibel_upload"}, {"141": ""}) == {"141": ""}

    def test_long_alarm_record_is_cut_but_readable(self):
        record = "e" * 900
        out = dps_delta({}, {"212": record})["212"]
        assert out.startswith("e" * 300)
        assert "+600 chars" in out
        assert len(out) < 400

    def test_short_values_are_untouched(self):
        assert dps_delta({}, {"201": "play"}) == {"201": "play"}

    def test_truncated_dps_keeps_repeated_pushes(self):
        # A push repeating a value is still an event worth logging.
        assert truncated_dps({"141": "decibel_upload"}) == {"141": "decibel_upload"}
        assert truncated_dps(None) == {}


class TestTalkbackOption:
    """Two-way audio must be opt-in (issue #72)."""

    BASE = {
        "signing_key": "sk", "sid": "eu166", "ecode": "E", "partner": "P",
        "app_key": "AK", "device_id": "D", "package_name": "pkg",
        "api_host": "a1.tuyaeu.com", "bridge_port": 38554,
        "cameras": [{"deviceId": "abc", "deviceName": "Erik"}],
    }

    def test_default_is_off(self):
        # Watching the camera must not claim the speaker.
        assert build_bridge_config(**self.BASE)["talkback"] is False

    def test_can_be_turned_on(self):
        assert build_bridge_config(**self.BASE, talkback=True)["talkback"] is True

    def test_key_is_part_of_the_go_contract(self):
        assert "talkback" in build_bridge_config(**self.BASE)


class _Coordinator:
    def __init__(self, device_info, stored_key="", lan_ip=None, lan_protocol=None):
        self.device_info = device_info
        self.local_key = stored_key
        self.lan_ip = lan_ip
        self.lan_protocol = lan_protocol


class _Api:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error

    async def get_rtc_config(self, dev_id):
        if self._error:
            raise self._error
        return self._result


class TestLanCredentials:
    def test_lan_credentials_prefer_fresh_key(self):
        from lan_credentials import lan_credentials

        coordinators = {
            "cam": _Coordinator({"localKey": "new"}, stored_key="old", lan_ip="10.0.0.5", lan_protocol="3.4"),
        }
        out = lan_credentials(coordinators, {"cam": "pw"})
        assert out == {"cam": {
            "local_key": "new", "p2p_password": "pw", "lan_ip": "10.0.0.5", "lan_protocol": "3.4",
            "lan_hosts": [], "lan_clarity": 4, "stream_source": "",
        }}

    def test_a_33_camera_streams_locally_only_on_its_bound_address(self):
        # 3.3 proves no key in its handshake: the bridge tries it only on the
        # one address bound to the camera, which lan_hosts then carries; a 3.3
        # camera without one gets no secrets (they would never be used).
        from lan_credentials import lan_credentials, local_stream_possible

        coordinators = {"cam": _Coordinator({"localKey": "k"}, lan_ip="10.0.0.5", lan_protocol="3.3")}
        configured = {"cam": ["10.0.0.5", "10.0.0.9"]}
        out = lan_credentials(coordinators, {"cam": "pw"}, configured, bound={"cam": "10.0.0.5"})["cam"]
        assert (out["local_key"], out["p2p_password"], out["lan_hosts"]) == ("k", "pw", ["10.0.0.5"])
        out = lan_credentials(coordinators, {"cam": "pw"}, configured)["cam"]
        assert (out["local_key"], out["p2p_password"]) == ("", "")
        # Its addresses still go along, for a LAN-only WebRTC stream's candidates.
        assert out["lan_hosts"] == ["10.0.0.9"]
        assert local_stream_possible(True, "k", "3.3", "10.0.0.5")
        assert not local_stream_possible(True, "k", "3.3")
        assert local_stream_possible(True, "k", "3.4")
        assert local_stream_possible(True, "k", "")

    def test_only_strings_never_none(self):
        from lan_credentials import lan_credentials

        out = lan_credentials({"cam": _Coordinator({}, stored_key=None)}, {"cam": None})
        assert out["cam"] == {
            "local_key": "", "p2p_password": "", "lan_ip": "", "lan_protocol": "",
            "lan_hosts": [], "lan_clarity": 4, "stream_source": "",
        }

    def test_runtime_rewrite_takes_no_api(self):
        import inspect

        from lan_credentials import lan_credentials

        assert list(inspect.signature(lan_credentials).parameters) == [
            "coordinators", "passwords", "configured", "clarities", "sources", "bound",
        ]

    def test_hosts_leave_out_the_published_and_the_others_addresses(self):
        from lan_credentials import lan_credentials

        coordinators = {
            "a": _Coordinator({"localKey": "ka"}, lan_ip="192.0.2.1"),
            "b": _Coordinator({"localKey": "kb"}, lan_ip="192.0.2.2"),
        }
        shared = ["192.0.2.1", "192.0.2.2", "192.0.2.3"]
        out = lan_credentials(coordinators, {}, {"a": ["192.0.2.9", *shared], "b": shared})
        assert out["a"]["lan_hosts"] == ["192.0.2.9", "192.0.2.3"]
        assert out["b"]["lan_hosts"] == ["192.0.2.3"]

    def test_quality_and_source_go_along(self):
        from lan_credentials import lan_credentials

        out = lan_credentials({"cam": _Coordinator({"localKey": "k"})}, {"cam": "pw"}, None, {"cam": 2}, {"cam": "local"})
        assert (out["cam"]["lan_clarity"], out["cam"]["stream_source"]) == (2, "local")

    def test_webrtc_only_gets_no_secrets(self):
        from lan_credentials import lan_credentials

        out = lan_credentials({"cam": _Coordinator({"localKey": "k"}, lan_ip="192.0.2.1")}, {"cam": "pw"}, sources={"cam": "webrtc"})
        assert (out["cam"]["local_key"], out["cam"]["p2p_password"]) == ("", "")
        assert (out["cam"]["lan_ip"], out["cam"]["stream_source"]) == ("192.0.2.1", "webrtc")

    def test_stored_key_used_when_cloud_has_none(self):
        from lan_credentials import lan_credentials

        out = lan_credentials({"cam": _Coordinator({}, stored_key="old")}, {"cam": "pw"})
        assert out["cam"]["local_key"] == "old"


class TestFetchPasswords:
    def test_fetched_once_per_camera(self):
        import asyncio

        from lan_credentials import fetch_p2p_passwords

        out = asyncio.run(fetch_p2p_passwords(_Api({"password": "pw"}), ["cam"], {"cam": "old"}))
        assert out == {"cam": "pw"}

    def test_failure_keeps_previous_value(self):
        import asyncio

        from lan_credentials import fetch_p2p_passwords

        out = asyncio.run(fetch_p2p_passwords(_Api(error=RuntimeError("boom")), ["cam", "b"], {"cam": "pw"}))
        assert out == {"cam": "pw", "b": ""}

    def test_empty_password_keeps_previous_value(self):
        import asyncio

        from lan_credentials import fetch_p2p_passwords

        out = asyncio.run(fetch_p2p_passwords(_Api({"password": None}), ["cam"], {"cam": "pw"}))
        assert out == {"cam": "pw"}


class _CountingApi(_Api):
    def __init__(self, result=None, error=None, delay=0.0):
        super().__init__(result, error)
        self.calls = []
        self._delay = delay

    async def get_rtc_config(self, dev_id):
        import asyncio

        self.calls.append(dev_id)
        if self._delay:
            await asyncio.sleep(self._delay)
        return await super().get_rtc_config(dev_id)


class TestFetchMissingPasswords:
    """A setup that could not fetch the P2P password tries again on a later rewrite."""

    def test_nothing_missing_means_no_call(self):
        import asyncio

        from lan_credentials import fetch_missing_passwords

        api = _CountingApi({"password": "pw"})
        assert asyncio.run(fetch_missing_passwords(api, ["cam"], {"cam": "pw"})) == {}
        assert api.calls == []

    def test_only_missing_cameras_are_fetched(self):
        import asyncio

        from lan_credentials import fetch_missing_passwords

        api = _CountingApi({"password": "fresh"})
        out = asyncio.run(fetch_missing_passwords(api, ["a", "b"], {"a": "pw", "b": ""}))
        assert out == {"b": "fresh"}
        assert api.calls == ["b"]

    def test_a_failure_returns_nothing_to_store(self):
        import asyncio

        from lan_credentials import fetch_missing_passwords

        out = asyncio.run(fetch_missing_passwords(_CountingApi(error=RuntimeError("down")), ["cam"], {}))
        assert out == {}

    def test_a_hanging_cloud_is_cut_short(self):
        import asyncio

        from lan_credentials import fetch_missing_passwords

        api = _CountingApi({"password": "late"}, delay=5)
        assert asyncio.run(fetch_missing_passwords(api, ["cam"], {}, timeout=0.01)) == {}


class TestPreviousLanValues:
    def test_reads_ip_protocol_and_password_per_camera(self):
        from payload import previous_lan_values

        text = json.dumps({"cameras": [
            {"camera_id": "a", "lan_ip": "10.0.0.2", "lan_protocol": "3.5", "p2p_password": "pw"},
            {"camera_id": "b"},
        ]})
        assert previous_lan_values(text) == {
            "a": {"lan_ip": "10.0.0.2", "lan_protocol": "3.5", "p2p_password": "pw", "local_key": "",
                  "lan_clarity": None, "stream_source": ""},
            "b": {"lan_ip": "", "lan_protocol": "", "p2p_password": "", "local_key": "",
                  "lan_clarity": None, "stream_source": ""},
        }

    def test_reads_clarity_source_and_key(self):
        from payload import previous_lan_values

        text = json.dumps({"cameras": [
            {"camera_id": "a", "local_key": "k", "lan_clarity": 2, "stream_source": "webrtc"},
            {"camera_id": "b", "lan_clarity": True},
        ]})
        out = previous_lan_values(text)
        assert (out["a"]["local_key"], out["a"]["lan_clarity"], out["a"]["stream_source"]) == ("k", 2, "webrtc")
        assert out["b"]["lan_clarity"] is None

    def test_missing_or_corrupt_input_gives_empty(self):
        from payload import previous_lan_values

        for text in ("", "{not json", "[]", '{"cameras": [{"no_id": 1}]}', '{"cameras": 5}'):
            assert previous_lan_values(text) == {}


class TestPasswordFetchIds:
    PREVIOUS = {"a": {"p2p_password": "pw", "local_key": "ka"}, "b": {"p2p_password": "", "local_key": "kb"}}

    def test_every_camera_by_default(self):
        from lan_credentials import password_fetch_ids

        assert password_fetch_ids(["a", "b"], self.PREVIOUS, {"a": "ka", "b": "kb"}) == ["a", "b"]

    def test_lan_preferred_keeps_a_password_for_the_same_key(self):
        from lan_credentials import password_fetch_ids

        assert password_fetch_ids(["a", "b"], self.PREVIOUS, {"a": "ka", "b": "kb"}, lan_preferred=True) == ["b"]

    def test_a_new_key_fetches_again(self):
        from lan_credentials import password_fetch_ids

        assert password_fetch_ids(["a"], self.PREVIOUS, {"a": "new"}, lan_preferred=True) == ["a"]

    def test_webrtc_only_is_skipped(self):
        from lan_credentials import password_fetch_ids

        assert password_fetch_ids(["a", "b"], self.PREVIOUS, {}, skip={"b"}) == ["a"]


class TestLocalStreamPossible:
    def test_needs_lan_streaming_and_a_key(self):
        from lan_credentials import local_stream_possible

        assert local_stream_possible(True, "k", "")
        assert not local_stream_possible(False, "k", "")
        assert not local_stream_possible(True, None, "3.5")

    def test_protocol_from_3_4(self):
        from lan_credentials import local_stream_possible

        assert local_stream_possible(True, "k", "3.4")
        assert local_stream_possible(True, "k", "3.5")
        assert not local_stream_possible(True, "k", "3.3")


class TestWebRTCAndLimit:
    BASE = {
        "signing_key": "sk", "sid": "S", "ecode": "E", "partner": "P", "app_key": "AK",
        "device_id": "D", "package_name": "pkg", "api_host": "a1.tuyaeu.com", "bridge_port": 38554,
        "cameras": [{"deviceId": "abc123", "deviceName": "Cam"}],
    }

    def test_defaults(self):
        config = build_bridge_config(**self.BASE)
        assert (config["webrtc"], config["max_stream_minutes"]) == (True, 0)

    def test_talkback_needs_webrtc(self):
        assert build_bridge_config(**self.BASE, talkback=True)["talkback"] is True
        assert build_bridge_config(**self.BASE, talkback=True, webrtc=False)["talkback"] is False

    def test_limit_is_never_negative(self):
        assert build_bridge_config(**self.BASE, max_stream_minutes=-3)["max_stream_minutes"] == 0
        assert build_bridge_config(**self.BASE, max_stream_minutes=10)["max_stream_minutes"] == 10

    def test_clarity_falls_back_to_hd(self):
        lan = {"abc123": {"lan_clarity": 7}}
        assert build_bridge_config(**self.BASE, lan=lan)["cameras"][0]["lan_clarity"] == 4
        lan = {"abc123": {"lan_clarity": 2, "lan_hosts": ["192.0.2.5"]}}
        cam = build_bridge_config(**self.BASE, lan=lan)["cameras"][0]
        assert (cam["lan_clarity"], cam["lan_hosts"]) == (2, ["192.0.2.5"])
