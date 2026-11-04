"""The per-camera "Local stream quality" select, from the choice to each camera's ``lan_clarity``.

The Go side reads it as ``Camera.LanClarity`` (``lan_clarity``) in
``cmd/addon/addon.go``: 4 is HD (1920x1080), 2 is standard (1280x720, what
the Avent app asks for), anything else is HD.
"""

import json
from pathlib import Path

import pytest
from const import DEFAULT_LAN_QUALITY, LAN_QUALITIES, LAN_QUALITY_HD, LAN_QUALITY_SD
from lan_quality import LanQualityChoice, resolve_lan_quality
from payload import build_bridge_config, lan_clarity_field, lan_clarity_for, previous_lan_values

COMPONENT = Path(__file__).parent.parent.parent / "custom_components" / "philips_avent"

BASE = {
    "signing_key": "sk", "sid": "eu166", "ecode": "E", "partner": "P",
    "app_key": "AK", "device_id": "D", "package_name": "pkg",
    "api_host": "a1.tuyaeu.com", "bridge_port": 38554,
    "cameras": [{"deviceId": "abc", "deviceName": "Cam"}, {"deviceId": "ghi", "deviceName": "Two"}],
}


class TestConstants:
    def test_default_is_hd(self):
        assert DEFAULT_LAN_QUALITY == LAN_QUALITY_HD == "hd"
        assert LAN_QUALITY_SD == "sd"
        assert set(LAN_QUALITIES) == {"hd", "sd"}

    @pytest.mark.parametrize(("quality", "clarity"), [("hd", 4), ("sd", 2), (None, 4), ("bogus", 4)])
    def test_clarity_for(self, quality, clarity):
        assert lan_clarity_for(quality) == clarity

    @pytest.mark.parametrize(("value", "field"), [(4, 4), (2, 2), (0, None), (None, None), (3, None), (True, None), ("4", None)])
    def test_clarity_field(self, value, field):
        assert lan_clarity_field(value) == field


class TestCarryOver:
    def test_each_camera_its_own_through_the_file(self):
        """What setup does: read the old file, carry the value over, write the new one."""
        lan = {"abc": {"lan_clarity": 2}}
        text = json.dumps(build_bridge_config(**BASE, lan=lan))
        carried = previous_lan_values(text)
        clarities = {cid: lan_clarity_for(resolve_lan_quality(None, v["lan_clarity"])) for cid, v in carried.items()}
        assert clarities == {"abc": 2, "ghi": 4}
        again = build_bridge_config(**BASE, lan={cid: {"lan_clarity": c} for cid, c in clarities.items()})
        assert json.dumps(again) == text


class TestPrecedence:
    @pytest.mark.parametrize(
        ("restored", "carried", "expected"),
        [
            ("sd", 4, "sd"),             # the restored choice wins
            ("hd", 2, "hd"),
            (None, 2, "sd"),             # then the carried-over bridge value
            ("unknown", 4, "hd"),
            ("unavailable", 2, "sd"),
            (None, 0, "hd"),             # then the default, HD
            (None, 7, "hd"),
            (None, True, "hd"),
            (None, None, "hd"),
            ("bogus", None, "hd"),
        ],
    )
    def test_resolve(self, restored, carried, expected):
        assert resolve_lan_quality(restored, carried) == expected

    def test_defaults(self):
        assert resolve_lan_quality() == "hd"


class TestChoice:
    def test_initial(self):
        assert LanQualityChoice("abc", "sd").quality == "sd"
        assert LanQualityChoice("abc", "bogus").quality == "hd"
        assert LanQualityChoice("abc", "hd").options == ["hd", "sd"]

    def test_restore_different_writes_once(self):
        calls = []
        choice = LanQualityChoice("abc", "hd", calls.append)
        assert choice.restore("sd") == "sd"
        assert calls == ["sd"]

    @pytest.mark.parametrize("stored", [None, "unknown", "unavailable", "bogus"])
    def test_restore_without_a_choice_keeps_initial_and_writes_nothing(self, stored):
        calls = []
        choice = LanQualityChoice("abc", "hd", calls.append)
        assert choice.restore(stored) == "hd"
        assert calls == []

    def test_restore_keeps_a_carried_sd(self):
        calls = []
        choice = LanQualityChoice("abc", "sd", calls.append)
        assert choice.restore(None) == "sd"
        assert calls == []

    def test_select(self):
        calls = []
        choice = LanQualityChoice("abc", "hd", calls.append)
        choice.select("sd")
        choice.select("sd")
        choice.select("hd")
        assert calls == ["sd", "hd"]
        with pytest.raises(ValueError):
            choice.select("4k")
        assert choice.quality == "hd"


class TestWiring:
    def test_not_an_option(self):
        source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
        assert "LAN_QUALITIES" not in source

    def test_integration_carries_over_and_rewrites(self):
        source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
        assert 'resolve_lan_quality(None, seed.get("lan_clarity")), _on_stream_choice' in source
        assert "{cid: lan_clarity_for(choice.quality) for cid, choice in lan_qualities.items()}" in source
        # Only for cameras with a local stream.
        assert "if possible:\n                lan_qualities[cam_id] = LanQualityChoice(" in source

    def test_select_entity(self):
        source = (COMPONENT / "select.py").read_text(encoding="utf-8")
        assert "class AventLanQualitySelect(SelectEntity, RestoreEntity):" in source
        assert '_attr_translation_key = "lan_quality"' in source
        assert 'self._attr_unique_id = f"{cam_id}_lan_quality"' in source
        assert "AventLanQualitySelect(coordinator, cam_id, qualities[cam_id])" in source


class TestTranslations:
    @pytest.mark.parametrize(
        ("path", "name"),
        [
            ("strings.json", "Local stream quality"),
            ("translations/en.json", "Local stream quality"),
            ("translations/fr.json", "Qualité du flux local"),
        ],
    )
    def test_select_translated(self, path, name):
        strings = json.loads((COMPONENT / path).read_text(encoding="utf-8"))
        select = strings["entity"]["select"]["lan_quality"]
        assert select["name"] == name
        assert set(select["state"]) == set(LAN_QUALITIES)
