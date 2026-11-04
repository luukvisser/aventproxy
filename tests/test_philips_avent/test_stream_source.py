"""The per-camera "Stream source" choice and the URL each choice streams from."""

import json
import logging
from pathlib import Path

import pytest

from const import (
    STREAM_SOURCE_AUTO,
    STREAM_SOURCE_WEBRTC,
    STREAM_SOURCE_WEBRTC_FIRST,
    STREAM_SOURCE_LOCAL,
    STREAM_SOURCE_PATHS,
    build_rtsp_url,
)
from stream_source import StreamSourceChoice, move_stream, offers_two_way_audio, stream_source_options

COMPONENT = Path(__file__).parent.parent.parent / "custom_components" / "philips_avent"
ALL = [STREAM_SOURCE_AUTO, STREAM_SOURCE_WEBRTC_FIRST, STREAM_SOURCE_LOCAL, STREAM_SOURCE_WEBRTC]


class TestOptions:
    def test_all_four_with_lan(self):
        assert stream_source_options(True) == ALL

    def test_no_lan_modes_without_lan(self):
        assert stream_source_options(False) == [STREAM_SOURCE_AUTO, STREAM_SOURCE_WEBRTC]

    def test_default_is_auto(self):
        assert StreamSourceChoice("cam", True).source == STREAM_SOURCE_AUTO
        assert StreamSourceChoice("cam", False).source == STREAM_SOURCE_AUTO


class TestCarriedOver:
    def test_initial_choice_from_the_bridge_config(self):
        assert StreamSourceChoice("cam", True, initial=STREAM_SOURCE_WEBRTC).source == STREAM_SOURCE_WEBRTC

    @pytest.mark.parametrize("initial", [None, "", "bogus", STREAM_SOURCE_LOCAL])
    def test_unusable_initial_is_the_default(self, initial):
        # Local is not offered without a local stream.
        assert StreamSourceChoice("cam", False, initial=initial).source == STREAM_SOURCE_AUTO


class TestSecretsFollowWebRTCOnly:
    """Only a move to or from "WebRTC only" changes the bridge config (its LAN secrets)."""

    def test_moves_that_rewrite(self):
        calls = []
        choice = StreamSourceChoice("cam", True, on_change=calls.append)
        choice.select(STREAM_SOURCE_LOCAL)
        choice.select(STREAM_SOURCE_WEBRTC)
        choice.select(STREAM_SOURCE_WEBRTC_FIRST)
        choice.select(STREAM_SOURCE_AUTO)
        assert calls == [STREAM_SOURCE_WEBRTC, STREAM_SOURCE_WEBRTC_FIRST]

    def test_restoring_the_carried_choice_writes_nothing(self):
        calls = []
        choice = StreamSourceChoice("cam", True, initial=STREAM_SOURCE_WEBRTC, on_change=calls.append)
        assert choice.restore(STREAM_SOURCE_WEBRTC) == STREAM_SOURCE_WEBRTC
        assert calls == []

    def test_restoring_another_choice_writes_once(self):
        calls = []
        choice = StreamSourceChoice("cam", True, on_change=calls.append)
        choice.restore(STREAM_SOURCE_WEBRTC)
        assert calls == [STREAM_SOURCE_WEBRTC]


class TestUrls:
    def test_auto_is_the_plain_path(self):
        assert build_rtsp_url("localhost", 38554, "Baby Cam", "id") == "rtsp://localhost:38554/Baby_Cam"
        assert build_rtsp_url("localhost", 38554, "Baby Cam", "id", stream_source=STREAM_SOURCE_AUTO) == "rtsp://localhost:38554/Baby_Cam"

    @pytest.mark.parametrize(("source", "suffix"), [
        (STREAM_SOURCE_LOCAL, "/local"),
        (STREAM_SOURCE_WEBRTC, "/webrtc"),
        (STREAM_SOURCE_WEBRTC_FIRST, "/webrtc-first"),
    ])
    def test_mode_suffix_after_the_camera_path(self, source, suffix):
        url = build_rtsp_url("10.0.0.2", 8554, "Baby Cam", "id", stream_source=source)
        assert url == "rtsp://10.0.0.2:8554/Baby_Cam" + suffix

    def test_every_source_has_a_path(self):
        assert set(STREAM_SOURCE_PATHS) == set(ALL)

    def test_suffixes_match_the_bridge(self):
        go = (COMPONENT.parent.parent / "avent-webrtc-bridge" / "pkg" / "rtsp" / "stream_mode.go").read_text()
        for suffix in filter(None, STREAM_SOURCE_PATHS.values()):
            assert f'StreamMode = "{suffix}"' in go


class TestRestore:
    def test_stored_choice_comes_back(self):
        choice = StreamSourceChoice("cam", True)
        assert choice.restore(STREAM_SOURCE_LOCAL) == STREAM_SOURCE_LOCAL
        assert choice.source == STREAM_SOURCE_LOCAL

    @pytest.mark.parametrize("stored", [STREAM_SOURCE_LOCAL, STREAM_SOURCE_WEBRTC_FIRST])
    def test_lan_choice_falls_back_to_auto_without_lan(self, stored, caplog):
        choice = StreamSourceChoice("cam", False)
        with caplog.at_level(logging.WARNING):
            assert choice.restore(stored) == STREAM_SOURCE_AUTO
        assert len(caplog.records) == 1
        assert "not available (no local stream)" in caplog.text

    @pytest.mark.parametrize("stored", [None, "unknown", "unavailable"])
    def test_nothing_stored_is_auto_and_quiet(self, stored, caplog):
        choice = StreamSourceChoice("cam", True)
        with caplog.at_level(logging.WARNING):
            assert choice.restore(stored) == STREAM_SOURCE_AUTO
        assert caplog.records == []

    def test_select_refuses_an_option_not_offered(self):
        choice = StreamSourceChoice("cam", False)
        with pytest.raises(ValueError):
            choice.select(STREAM_SOURCE_LOCAL)
        assert choice.source == STREAM_SOURCE_AUTO


class FakeWorker:
    def __init__(self, alive):
        self.alive = alive

    def is_alive(self):
        return self.alive


class FakeStream:
    """Home Assistant's Stream as move_stream sees it: a source, update_source,
    and the worker thread (None before the first start)."""

    def __init__(self, source, running=True):
        self.source = source
        self.updates = []
        self._thread = FakeWorker(True) if running else None

    def update_source(self, url):
        self.updates.append(url)
        self.source = url


class TestSwitching:
    def test_running_stream_moves_to_the_new_path(self):
        stream = FakeStream("rtsp://localhost:38554/Cam")
        assert move_stream(stream, "rtsp://localhost:38554/Cam/local")
        assert stream.updates == ["rtsp://localhost:38554/Cam/local"]

    def test_same_path_is_left_alone(self):
        stream = FakeStream("rtsp://localhost:38554/Cam")
        assert not move_stream(stream, "rtsp://localhost:38554/Cam")
        assert stream.updates == []

    def test_no_stream_yet(self):
        assert not move_stream(None, "rtsp://localhost:38554/Cam/webrtc")

    @pytest.mark.parametrize("worker", [None, FakeWorker(False)])
    def test_stopped_stream_gets_the_source_without_a_restart(self, worker):
        # update_source on a stopped stream would leave its fast-restart flag
        # set, and the next stop would then never end the worker.
        stream = FakeStream("rtsp://localhost:38554/Cam")
        stream._thread = worker
        assert move_stream(stream, "rtsp://localhost:38554/Cam/webrtc")
        assert stream.updates == []
        assert stream.source == "rtsp://localhost:38554/Cam/webrtc"

    def test_select_notifies_the_camera(self):
        """What the camera entity does: listen, rebuild its URL, move the stream."""
        choice = StreamSourceChoice("cam", True)
        stream = FakeStream(build_rtsp_url("localhost", 38554, "Cam", "cam"))
        remove = choice.add_listener(
            lambda source: move_stream(stream, build_rtsp_url("localhost", 38554, "Cam", "cam", stream_source=source))
        )
        choice.select(STREAM_SOURCE_WEBRTC)
        choice.select(STREAM_SOURCE_WEBRTC)  # unchanged: no second restart
        remove()
        choice.select(STREAM_SOURCE_LOCAL)
        assert stream.updates == ["rtsp://localhost:38554/Cam/webrtc"]

    def test_restore_notifies_too(self):
        choice = StreamSourceChoice("cam", True)
        seen = []
        choice.add_listener(seen.append)
        choice.restore(STREAM_SOURCE_WEBRTC_FIRST)
        assert seen == [STREAM_SOURCE_WEBRTC_FIRST]


class TestTranslations:
    @pytest.mark.parametrize("path", ["strings.json", "translations/en.json", "translations/fr.json"])
    def test_select_and_every_option_translated(self, path):
        data = json.loads((COMPONENT / path).read_text(encoding="utf-8"))
        entry = data["entity"]["select"]["stream_source"]
        assert entry["name"]
        assert set(entry["state"]) == set(ALL)
        assert all(entry["state"].values())

    def test_english_names(self):
        data = json.loads((COMPONENT / "translations/en.json").read_text(encoding="utf-8"))
        entry = data["entity"]["select"]["stream_source"]
        assert entry["name"] == "Stream source"
        assert entry["state"][STREAM_SOURCE_AUTO] == "Automatic: local first"

    def test_camera_translation_key_unchanged(self):
        for path in ["strings.json", "translations/en.json", "translations/fr.json"]:
            data = json.loads((COMPONENT / path).read_text(encoding="utf-8"))
            assert list(data["entity"]["camera"]) == ["camera"]


class TestEntitySource:
    """The entity modules need Home Assistant; check their wiring as text."""

    def test_unique_ids(self):
        select = (COMPONENT / "select.py").read_text()
        camera = (COMPONENT / "camera.py").read_text()
        assert 'f"{cam_id}_stream_source"' in select
        assert "EntityCategory.CONFIG" in select
        assert 'f"{cam_id}_camera"' in camera
        assert '_attr_translation_key = "camera"' in camera
        assert "self._stream_url, width=width" in camera


class TestFollowsTheCameraAvailability:
    """An offline camera (no data for Home Assistant) showed its camera entity
    as "idle" and both stream selects as set; they now follow the coordinator,
    and a stream the bridge runs keeps all three through a drop of the data
    session (before, the selects went unavailable next to a running picture)."""

    def test_camera_and_selects_follow_the_coordinator(self):
        camera = (COMPONENT / "camera.py").read_text(encoding="utf-8")
        select = (COMPONENT / "select.py").read_text(encoding="utf-8")
        assert "return streamable and (self.coordinator.last_update_success or self._bridge_streaming)" in camera
        assert "return bridge_streaming(self.hass, self.coordinator, self._cam_id)" in camera
        assert "self.coordinator.async_add_listener(self._availability_changed)" in camera
        assert select.count(
            "return self._coordinator.last_update_success or bridge_streaming(self.hass, self._coordinator, self._cam_id)"
        ) == 2
        assert select.count("self._coordinator.async_add_listener(self.async_write_ha_state)") == 2

    def test_bridge_streaming_reads_the_polled_status(self):
        from types import SimpleNamespace

        from const import DOMAIN
        from entity import bridge_streaming

        coordinator = SimpleNamespace(config_entry=SimpleNamespace(entry_id="e1"))

        def hass_with(cameras):
            status = SimpleNamespace(data={"cameras": cameras})
            return SimpleNamespace(data={DOMAIN: {"e1": {"bridge_status": status}}})

        assert bridge_streaming(hass_with({"cam": {"path": "local"}}), coordinator, "cam")
        assert bridge_streaming(hass_with({"cam": {"path": "webrtc"}}), coordinator, "cam")
        assert not bridge_streaming(hass_with({"cam": {"path": "idle"}}), coordinator, "cam")
        assert not bridge_streaming(hass_with({"other": {"path": "local"}}), coordinator, "cam")
        # Before the first poll, without the status poller, or without an entry.
        assert not bridge_streaming(SimpleNamespace(data={}), coordinator, "cam")
        status = SimpleNamespace(data=None)
        assert not bridge_streaming(SimpleNamespace(data={DOMAIN: {"e1": {"bridge_status": status}}}), coordinator, "cam")
        assert not bridge_streaming(hass_with({"cam": {"path": "local"}}), SimpleNamespace(config_entry=None), "cam")

    def test_webrtc_provider_follows_the_availability(self):
        """An unavailable camera has no stream source, so Home Assistant gives it
        no WebRTC provider; it must ask again once the camera is back, or the
        dashboard stays on HLS (slow start, no sound)."""
        camera = (COMPONENT / "camera.py").read_text(encoding="utf-8")
        handler = camera.split("def _availability_changed", 1)[1].split("\n    @", 1)[0]
        assert "return self._stream_url if self.available else None" in camera
        assert handler.index("self.async_write_ha_state()") < handler.index("async_refresh_providers")
        assert "if self.available == self._was_available:" in handler
        assert "self.hass.async_create_task(refresh())" in handler
        source_changed = camera.split("def _source_changed", 1)[1].split("\n    async def", 1)[0]
        assert "self._availability_changed()" in source_changed


class TestTwoWayAudio:
    """Home Assistant 2026.11 offers its microphone to a camera that declares
    two-way audio; the bridge's local stream has no return channel."""

    @pytest.mark.parametrize("source", [None, STREAM_SOURCE_AUTO, STREAM_SOURCE_WEBRTC_FIRST, STREAM_SOURCE_WEBRTC])
    def test_every_source_but_local_talks(self, source):
        assert offers_two_way_audio(True, source)
        assert not offers_two_way_audio(False, source)

    def test_local_stream_does_not_talk(self):
        assert not offers_two_way_audio(True, STREAM_SOURCE_LOCAL)

    def test_camera_declares_it_and_follows_the_source(self):
        camera = (COMPONENT / "camera.py").read_text(encoding="utf-8")
        setup = camera.split("async def async_setup_entry", 1)[1].split("\nclass ", 1)[0]
        assert "CONF_WEBRTC, DEFAULT_WEBRTC" in setup  # no two-way audio without WebRTC
        init = camera.split("def __init__", 1)[1].split("\n    @", 1)[0]
        assert "self._declare_two_way_audio()" in init
        source_changed = camera.split("def _source_changed", 1)[1].split("\n    async def", 1)[0]
        assert source_changed.index("self._declare_two_way_audio()") < source_changed.index("self._availability_changed()")
