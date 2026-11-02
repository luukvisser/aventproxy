"""Camera entity for Philips Avent Baby Monitor."""
from __future__ import annotations

import logging
import time

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import async_get_image as ffmpeg_get_image
from homeassistant.components.stream import CONF_RTSP_TRANSPORT, CONF_USE_WALLCLOCK_AS_TIMESTAMPS
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_BRIDGE_HOST,
    CONF_BRIDGE_PORT,
    CONF_TALKBACK,
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    DEFAULT_TALKBACK,
    DOMAIN,
    build_rtsp_url,
)
from .coordinator import PhilipsAventCoordinator
from .entity import build_device_info

try:  # Home Assistant 2026.11 and later
    from homeassistant.components.camera import CameraEntityCapabilities
except ImportError:  # an older core has no two-way audio to declare
    CameraEntityCapabilities = None

_LOGGER = logging.getLogger(__name__)

# A snapshot pulled through the bridge starts the camera's stream when none
# is running, and the bridge keeps it for half a minute after: Home Assistant
# and its frontend asked about once a second on 02-11-2026 (205 times in three
# minutes, most for a camera whose stream could not start), which would keep
# the camera streaming next to the parent unit. So a snapshot comes from Home
# Assistant's own running stream, else the last picture is reused, and a new
# one through the bridge is taken at most this often per camera.
SNAPSHOT_MIN_INTERVAL = 600


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    bridge_port = entry.options.get(CONF_BRIDGE_PORT, DEFAULT_BRIDGE_PORT)
    bridge_host = entry.options.get(CONF_BRIDGE_HOST, DEFAULT_BRIDGE_HOST)
    talkback = entry.options.get(CONF_TALKBACK, DEFAULT_TALKBACK)
    entities = []
    for cam_id, coordinator in data["coordinators"].items():
        entities.append(AventCamera(coordinator, cam_id, bridge_port, bridge_host, talkback))
    async_add_entities(entities)


class AventCamera(Camera):
    """Camera entity pointing to the WebRTC bridge RTSP stream."""

    _attr_has_entity_name = True
    _attr_translation_key = "camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: PhilipsAventCoordinator,
        cam_id: str,
        bridge_port: int = DEFAULT_BRIDGE_PORT,
        bridge_host: str = DEFAULT_BRIDGE_HOST,
        talkback: bool = DEFAULT_TALKBACK,
    ):
        super().__init__()
        # The bridge's RTSP over TCP: over UDP the first packets with the
        # stream's parameter sets could be lost, and Home Assistant's stream
        # worker gave up within its probe time and reconnected in a loop, the
        # card spinning on its placeholder after the app came back from the
        # background (measured 02-11-2026: UDP 176 frames in 8 s with
        # "non-existing PPS", TCP 216 without). Wall-clock timestamps, because
        # ffmpeg finds the bridge's packet timestamps not monotonic.
        self.stream_options[CONF_RTSP_TRANSPORT] = "tcp"
        self.stream_options[CONF_USE_WALLCLOCK_AS_TIMESTAMPS] = True
        self.coordinator = coordinator
        self._cam_id = cam_id
        self._attr_unique_id = f"{cam_id}_camera"
        self._stream_url = build_rtsp_url(
            bridge_host, bridge_port, coordinator.camera_name, cam_id
        )
        self._attr_device_info = build_device_info(coordinator, cam_id)
        if talkback and CameraEntityCapabilities is not None:
            # Home Assistant offers its microphone (a Labs preview, over WebRTC
            # only) to a camera that declares two-way audio. go2rtc sends it on
            # the bridge's RTSP backchannel, which the bridge advertises while
            # talkback is on.
            self._attr_camera_entity_capabilities = CameraEntityCapabilities(two_way_audio=True)
        self._last_image: bytes | None = None
        self._last_grab: float | None = None

    async def stream_source(self) -> str:
        return self._stream_url

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """A JPEG of the camera, without starting a stream for it.

        Required by the `camera.snapshot` service. From Home Assistant's own
        running stream when there is one (no extra connection to the bridge);
        otherwise the last picture, and a new one through the bridge (ffmpeg,
        over TCP like the stream) at most once per SNAPSHOT_MIN_INTERVAL.
        """
        stream = self.stream
        if stream is not None and stream.available:
            image = await stream.async_get_image(width, height)
            if image:
                self._last_image = image
                return image
        now = time.monotonic()
        if self._last_grab is not None and now - self._last_grab < SNAPSHOT_MIN_INTERVAL:
            return self._last_image
        self._last_grab = now
        try:
            image = await ffmpeg_get_image(
                self.hass, f"-rtsp_transport tcp -i {self._stream_url}", width=width, height=height
            )
        except Exception:
            _LOGGER.exception("ffmpeg snapshot failed for %s", self._stream_url)
            return self._last_image
        if image:
            self._last_image = image
        return image or self._last_image
