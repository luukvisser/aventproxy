"""The bridge's /status as the "Stream path" sensor reads it (bridge_status.py).

The Go side is pkg/rtsp/status.go; its tests cover what the bridge writes.
"""

from bridge_status import (
    PATH_IDLE,
    STATUS_REFRESH_COOLDOWN,
    STREAM_PATHS,
    STREAM_START_CHECK_SECONDS,
    parse_status,
    status_url,
    webrtc_allowed,
)

DOC = {
    "webrtc": True,
    "cameras": [
        {"camera_id": "cam1", "path": "local", "mode": "auto", "since": "2026-11-02T11:20:05+01:00",
         "width": 1920, "height": 1080},
        {"camera_id": "cam2", "path": "idle", "last_fallback": "local stream sent no video, using the cloud",
         "last_fallback_at": "2026-11-02T10:00:00+01:00"},
    ],
}


def test_url_is_the_port_after_rtsp():
    assert status_url("192.0.2.20", 38554) == "http://192.0.2.20:38555/status"


def test_cameras_are_read():
    cams = parse_status(DOC)
    assert cams["cam1"]["path"] == "local"
    assert cams["cam1"]["resolution"] == "1920x1080"
    assert cams["cam1"]["mode"] == "auto"
    assert cams["cam2"]["path"] == "idle"
    assert cams["cam2"]["resolution"] is None
    assert cams["cam2"]["last_fallback"].startswith("local stream sent no video")
    assert webrtc_allowed(DOC) is True


def test_nothing_unexpected_reaches_the_sensor():
    cams = parse_status({"cameras": [
        {"camera_id": "a", "path": "<script>", "width": "1920", "height": 1080, "mode": 5},
        {"path": "local"},          # no id
        "junk",
    ]})
    assert list(cams) == ["a"]
    assert cams["a"]["path"] == PATH_IDLE
    assert cams["a"]["resolution"] is None and cams["a"]["mode"] is None
    for bad in (None, [], "x", {"cameras": "x"}):
        assert parse_status(bad) == {}
    assert webrtc_allowed({"webrtc": "yes"}) is None


def test_paths_match_the_bridge_and_the_translations():
    import json
    from pathlib import Path
    root = Path(__file__).parent.parent.parent
    go = (root / "avent-webrtc-bridge" / "pkg" / "rtsp" / "status.go").read_text()
    for path in STREAM_PATHS:
        assert f'= "{path}"' in go
    for f in ("strings.json", "translations/en.json", "translations/fr.json"):
        states = json.loads((root / "custom_components" / "philips_avent" / f).read_text())["entity"]["sensor"]["stream_path"]["state"]
        assert set(states) == set(STREAM_PATHS)


class TestStreamStartChecks:
    def test_first_look_is_quick(self):
        # A 3.3 camera's start waits for this look; at 1 s it cost 1.05-1.1 s.
        assert STREAM_START_CHECK_SECONDS[0] <= 0.25

    def test_looks_in_order_and_not_folded(self):
        checks = STREAM_START_CHECK_SECONDS
        assert list(checks) == sorted(checks)
        assert STATUS_REFRESH_COOLDOWN < min(b - a for a, b in zip(checks, checks[1:]))


def test_stream_path_sensor_off_by_default():
    """Diagnostic entities: off until enabled, so a new install shows no more than the app's data."""
    from pathlib import Path

    source = (Path(__file__).parent.parent.parent / "custom_components" / "philips_avent" / "sensor.py").read_text()
    for cls in ('AventStreamPathSensor',):
        body = source.split(f"class {cls}(")[1].split("\nclass ")[0]
        assert "_attr_entity_category = EntityCategory.DIAGNOSTIC" in body, cls
        assert "_attr_entity_registry_enabled_default = False" in body, cls
