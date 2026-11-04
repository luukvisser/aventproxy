"""What the bridge does with each camera's stream, from its /status endpoint.

The bridge (pkg/rtsp/status.go) serves, on the port after its RTSP port and
on the same address, a JSON document: per camera the path its stream takes
now (local, imm, webrtc or idle), since when, the video size, and the last time it
fell back from one path to the other. Home Assistant polls it for the "Stream
path" sensor; until then it only knew the stream source it asked for.

Kept free of Home Assistant imports so the unit tests can load it.
"""
from __future__ import annotations

PATH_LOCAL = "local"  # LAN signalling, media over the LAN
PATH_IMM = "imm"  # imm over cloud signalling (the add-on's Dev and Beta cards)
PATH_WEBRTC = "webrtc"  # the webrtc cloud session
PATH_IDLE = "idle"
STREAM_PATHS = (PATH_LOCAL, PATH_IMM, PATH_WEBRTC, PATH_IDLE)

# Often enough to see a stream start or fall back while a dashboard is open,
# rare enough to cost nothing: one small local request per entry.
STATUS_POLL_SECONDS = 30
STATUS_TIMEOUT = 5


def status_url(host: str, rtsp_port: int) -> str:
    """The bridge's status endpoint: its RTSP host, the next port."""
    return f"http://{host}:{rtsp_port + 1}/status"


def parse_status(document: object) -> dict[str, dict]:
    """{camera id: status} from the bridge's document; nothing for what it cannot read.

    A camera's status keeps only known fields with the expected types, and an
    unknown path becomes idle, so a newer or broken bridge cannot put
    arbitrary values into the sensor.
    """
    if not isinstance(document, dict) or not isinstance(document.get("cameras"), list):
        return {}
    out: dict[str, dict] = {}
    for cam in document["cameras"]:
        if not isinstance(cam, dict) or not isinstance(cam.get("camera_id"), str):
            continue
        path = cam.get("path")
        width, height = cam.get("width"), cam.get("height")
        out[cam["camera_id"]] = {
            "path": path if path in STREAM_PATHS else PATH_IDLE,
            "mode": cam.get("mode") if isinstance(cam.get("mode"), str) else None,
            "since": cam.get("since") if isinstance(cam.get("since"), str) else None,
            "resolution": f"{width}x{height}" if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0 else None,
            "last_fallback": cam.get("last_fallback") if isinstance(cam.get("last_fallback"), str) else None,
            "last_fallback_at": cam.get("last_fallback_at") if isinstance(cam.get("last_fallback_at"), str) else None,
        }
    return out


def webrtc_allowed(document: object) -> bool | None:
    """Whether the bridge allows WebRTC ("Use WebRTC"), as it reports it."""
    if isinstance(document, dict) and isinstance(document.get("webrtc"), bool):
        return document["webrtc"]
    return None
