"""The "Use WebRTC" option: off leaves only the local stream, on brings every choice back.

The Go side reads it as ``BridgeConfig.WebRTC`` (``webrtc``) in
``cmd/addon/addon.go``; absent there means on.
"""

import json
import logging
from pathlib import Path

import pytest
from const import (
    CONF_WEBRTC,
    DEFAULT_WEBRTC,
    STREAM_SOURCE_AUTO,
    STREAM_SOURCE_LOCAL,
    STREAM_SOURCE_WEBRTC,
    STREAM_SOURCE_WEBRTC_FIRST,
)
from payload import build_bridge_config
from stream_source import StreamSourceChoice, stream_source_options

COMPONENT = Path(__file__).parent.parent.parent / "custom_components" / "philips_avent"
BASE = {
    "signing_key": "sk", "sid": "eu166", "ecode": "E", "partner": "P",
    "app_key": "AK", "device_id": "D", "package_name": "pkg",
    "api_host": "a1.tuyaeu.com", "bridge_port": 38554,
    "cameras": [{"deviceId": "abc", "deviceName": "Cam"}],
}


class TestDefault:
    def test_on_by_default(self):
        assert DEFAULT_WEBRTC is True
        assert CONF_WEBRTC == "webrtc"
        assert build_bridge_config(**BASE)["webrtc"] is True


class TestBridgeConfig:
    def test_off_is_written(self):
        assert build_bridge_config(**BASE, webrtc=False)["webrtc"] is False

    def test_talkback_needs_webrtc(self):
        assert build_bridge_config(**BASE, talkback=True)["talkback"] is True
        assert build_bridge_config(**BASE, talkback=True, webrtc=False)["talkback"] is False

    def test_integration_writes_it(self):
        source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
        assert "webrtc=entry.options.get(CONF_WEBRTC, DEFAULT_WEBRTC)" in source
        assert 'cam_id, possible, webrtc, seed.get("stream_source"), _on_stream_choice' in source


class TestOptionsOffered:
    def test_only_local_without_webrtc(self):
        assert stream_source_options(True, webrtc=False) == [STREAM_SOURCE_LOCAL]

    def test_nothing_without_webrtc_and_lan(self):
        assert stream_source_options(False, webrtc=False) == []
        choice = StreamSourceChoice("cam", False, webrtc=False)
        assert not choice.streamable
        assert choice.source is None
        assert choice.restore(STREAM_SOURCE_AUTO) is None

    def test_default_is_local_without_webrtc(self):
        assert StreamSourceChoice("cam", True, webrtc=False).source == STREAM_SOURCE_LOCAL

    @pytest.mark.parametrize("option", [STREAM_SOURCE_AUTO, STREAM_SOURCE_WEBRTC, STREAM_SOURCE_WEBRTC_FIRST])
    def test_webrtc_modes_refused(self, option):
        with pytest.raises(ValueError):
            StreamSourceChoice("cam", True, webrtc=False).select(option)


class TestChoiceSurvivesTheToggle:
    @pytest.mark.parametrize("stored", [STREAM_SOURCE_WEBRTC, STREAM_SOURCE_WEBRTC_FIRST, STREAM_SOURCE_AUTO])
    def test_set_aside_while_off(self, stored, caplog):
        choice = StreamSourceChoice("cam", True, webrtc=False)
        with caplog.at_level(logging.INFO):
            assert choice.restore(stored) == STREAM_SOURCE_LOCAL
        assert choice.chosen == stored
        assert "needs WebRTC, which is off" in caplog.text

    def test_comes_back_when_on_again(self):
        off = StreamSourceChoice("cam", True, webrtc=False)
        off.restore(STREAM_SOURCE_LOCAL, STREAM_SOURCE_WEBRTC)
        # What the select stores at the next restart: state local, chosen webrtc.
        on = StreamSourceChoice("cam", True)
        assert on.restore(off.source, off.chosen) == STREAM_SOURCE_WEBRTC

    def test_a_choice_made_while_off_counts(self):
        off = StreamSourceChoice("cam", True, webrtc=False)
        off.restore(STREAM_SOURCE_LOCAL, STREAM_SOURCE_WEBRTC)
        off.select(STREAM_SOURCE_LOCAL)
        on = StreamSourceChoice("cam", True)
        assert on.restore(off.source, off.chosen) == STREAM_SOURCE_LOCAL

    def test_older_versions_stored_only_the_state(self):
        assert StreamSourceChoice("cam", True).restore(STREAM_SOURCE_WEBRTC_FIRST, None) == STREAM_SOURCE_WEBRTC_FIRST

    @pytest.mark.parametrize("junk", [None, "unknown", "unavailable", "bogus"])
    def test_junk_chosen_falls_back_to_the_state(self, junk):
        assert StreamSourceChoice("cam", True).restore(STREAM_SOURCE_LOCAL, junk) == STREAM_SOURCE_LOCAL


class TestOptionsFlow:
    SOURCE = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")

    def test_toggle_on_the_first_step(self):
        assert "vol.Optional(CONF_WEBRTC, default=current_webrtc): bool" in self.SOURCE

    def test_webrtc_step_only_when_on(self):
        assert "if self._options.get(CONF_WEBRTC, DEFAULT_WEBRTC):\n                    return await self.async_step_webrtc()" in self.SOURCE
        assert 'step_id="webrtc"' in self.SOURCE

    def test_stored_options_are_kept(self):
        # Saving with WebRTC off keeps talkback as it was.
        assert "**self.config_entry.options, **user_input" in self.SOURCE

    @pytest.mark.parametrize("path", ["strings.json", "translations/en.json", "translations/fr.json"])
    def test_translated(self, path):
        steps = json.loads((COMPONENT / path).read_text(encoding="utf-8"))["options"]["step"]
        assert steps["init"]["data"]["webrtc"] and steps["init"]["data_description"]["webrtc"]
        assert set(steps["webrtc"]["data"]) == {"talkback"}
        assert "talkback" not in steps["init"]["data"]
