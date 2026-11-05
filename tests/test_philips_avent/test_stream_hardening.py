"""The bridge's RTSP password and status token, as the integration uses them.

Without the password anything that reached the bridge's port could watch the
camera; the password goes into the stream URL Home Assistant pulls, so that
URL must never reach a log line, the diagnostics or an attribute.
"""

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from const import RTSP_USER, build_rtsp_url, redact_rtsp_url
from payload import build_bridge_config
from redact import REDACTED, redact_secrets

COMPONENT = Path(__file__).parent.parent.parent / "custom_components" / "philips_avent"
PASSWORD = "Zr4-x_secret/with@odd:chars"


def _config(**kwargs) -> dict:
    return build_bridge_config(
        signing_key="k", sid="s", ecode="e", partner="p", app_key="a", device_id="d",
        package_name="n", api_host="h", bridge_port=38554, cameras=[], **kwargs,
    )


class TestStreamURL:
    def test_password_in_the_url(self):
        url = build_rtsp_url("bridge", 38554, "Baby Cam", "id", password=PASSWORD)
        parts = urlsplit(url)
        assert parts.username == RTSP_USER == "homeassistant"
        assert unquote(parts.password) == PASSWORD
        assert parts.hostname == "bridge" and parts.port == 38554
        assert parts.path == "/Baby_Cam"

    def test_with_a_stream_source(self):
        url = build_rtsp_url("bridge", 38554, "Cam", "id", stream_source="local", password=PASSWORD)
        assert url.endswith("@bridge:38554/Cam/local")

    def test_without_a_password_as_before(self):
        assert build_rtsp_url("bridge", 38554, "Cam", "id") == "rtsp://bridge:38554/Cam"

    def test_redacted(self):
        url = build_rtsp_url("bridge", 38554, "Cam", "id", stream_source="local", password=PASSWORD)
        shown = redact_rtsp_url(url)
        assert shown == "rtsp://***@bridge:38554/Cam/local"
        assert "secret" not in shown and RTSP_USER not in shown
        assert redact_rtsp_url("rtsp://bridge:38554/Cam") == "rtsp://bridge:38554/Cam"


class TestBridgeConfig:
    def test_password_written_when_set(self):
        assert _config(rtsp_password=PASSWORD)["rtsp_password"] == PASSWORD

    def test_absent_without_one(self):
        assert "rtsp_password" not in _config()

    def test_redacted_in_diagnostics(self):
        shown = json.dumps(redact_secrets({"data": {"bridge_rtsp_password": PASSWORD}, "bridge": _config(rtsp_password=PASSWORD)}))
        assert PASSWORD not in shown and REDACTED in shown


class TestNeverLogged:
    """The entity modules need Home Assistant; check their wiring as text."""

    def test_camera_logs_only_the_redacted_url(self):
        camera = (COMPONENT / "camera.py").read_text(encoding="utf-8")
        for call in re.findall(r"_LOGGER\.\w+\((.*?)\)\n", camera, re.S):
            if "_stream_url" in call:
                assert "redact_rtsp_url(self._stream_url)" in call, call
        assert "password=self._rtsp_password" in camera
        assert "extra_state_attributes" in camera and "_rtsp_password" not in camera.split("def extra_state_attributes")[1].split("def ")[0]

    def test_password_made_like_the_token(self):
        init = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
        assert "secrets.token_urlsafe(32)" in init
        # Made before setup writes the bridge config.
        assert init.index("_bridge_rtsp_password(hass, entry)\n") < init.index("await _rewrite_bridge_config(refetch=False)")
        assert "rtsp_password=entry.data.get(CONF_BRIDGE_RTSP_PASSWORD)" in init

    def test_status_poll_sends_the_token(self):
        init = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
        poll = init.split("async def _bridge_status")[1].split("\nasync def ")[0]
        assert '{"Authorization": f"Bearer {token}"}' in poll
        assert "session.get(url, headers=headers)" in poll


def test_requirements_pinned():
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["requirements"]
    for requirement in manifest["requirements"]:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*", requirement), requirement
