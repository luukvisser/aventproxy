"""Camera entity for Philips Avent Baby Monitor."""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import async_get_image as ffmpeg_get_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .bridge_status import STREAM_START_CHECK_SECONDS
from .const import (
    CONF_BRIDGE_HOST,
    CONF_BRIDGE_PORT,
    CONF_TALKBACK,
    CONF_WEBRTC,
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    DEFAULT_TALKBACK,
    DEFAULT_WEBRTC,
    DOMAIN,
    build_rtsp_url,
)
from .coordinator import PhilipsAventCoordinator
from .entity import bridge_streaming, build_device_info
from .stream_source import StreamSourceChoice, move_stream, offers_two_way_audio

try:  # Home Assistant 2026.11 and later
    from homeassistant.components.camera import CameraEntityCapabilities
except ImportError:  # an older core has no two-way audio to declare
    CameraEntityCapabilities = None

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    bridge_port = entry.options.get(CONF_BRIDGE_PORT, DEFAULT_BRIDGE_PORT)
    bridge_host = entry.options.get(CONF_BRIDGE_HOST, DEFAULT_BRIDGE_HOST)
    # Two-way audio rides WebRTC only: with Use WebRTC off the bridge has none.
    talkback = entry.options.get(CONF_TALKBACK, DEFAULT_TALKBACK) and entry.options.get(
        CONF_WEBRTC, DEFAULT_WEBRTC
    )
    entities = []
    choices = data.get("stream_sources", {})
    for cam_id, coordinator in data["coordinators"].items():
        entities.append(AventCamera(
            coordinator, cam_id, bridge_port, bridge_host, choices.get(cam_id),
            talkback=talkback,
        ))
    async_add_entities(entities)


class AventCamera(Camera):
    """Camera entity pointing to the WebRTC bridge RTSP stream.

    It streams from the path of the camera's "Stream source" select. Each
    still image (a dashboard card refreshes one every few seconds) opens that
    path through ffmpeg, which starts a bridge stream when none is running.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: PhilipsAventCoordinator,
        cam_id: str,
        bridge_port: int = DEFAULT_BRIDGE_PORT,
        bridge_host: str = DEFAULT_BRIDGE_HOST,
        choice: StreamSourceChoice | None = None,
        *,
        talkback: bool = DEFAULT_TALKBACK,
    ):
        super().__init__()
        self.coordinator = coordinator
        self._cam_id = cam_id
        self._attr_unique_id = f"{cam_id}_camera"
        self._bridge_host = bridge_host
        self._bridge_port = bridge_port
        self._choice = choice
        self._talkback = talkback
        self._two_way_declared = False
        self._attr_device_info = build_device_info(coordinator, cam_id)
        self._declare_two_way_audio()

    @property
    def available(self) -> bool:
        """Not without a stream (WebRTC off and no LAN path), and not while Home
        Assistant has no data from the camera: an offline one showed "idle".

        A stream the bridge is running counts as data: its LAN session is not
        the one the coordinator's data comes from, so a drop of that one (in
        LAN preferred before any cloud contact) must not take the picture away.
        """
        streamable = self._choice is None or self._choice.streamable
        return streamable and (self.coordinator.last_update_success or self._bridge_streaming)

    @property
    def _bridge_streaming(self) -> bool:
        return bridge_streaming(self.hass, self.coordinator, self._cam_id)

    @property
    def _source(self) -> str | None:
        return self._choice.source if self._choice else None

    @property
    def _stream_url(self) -> str:
        return build_rtsp_url(
            self._bridge_host, self._bridge_port, self.coordinator.camera_name,
            self._cam_id, stream_source=self._source,
        )

    def _declare_two_way_audio(self) -> None:
        """Home Assistant offers its microphone (a Labs preview, over WebRTC
        only) to a camera that declares two-way audio. go2rtc sends it on the
        bridge's RTSP backchannel, which the bridge advertises while talkback is
        on, on every path but the local stream's."""
        two_way = offers_two_way_audio(self._talkback, self._source)
        if CameraEntityCapabilities is None or not (two_way or self._two_way_declared):
            return  # as Home Assistant's default until it was declared once
        self._two_way_declared = True
        self._attr_camera_entity_capabilities = CameraEntityCapabilities(two_way_audio=two_way)

    @property
    def extra_state_attributes(self) -> dict | None:
        return {"stream_source": self._source} if self._choice else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._was_available = self.available
        self.async_on_remove(self.coordinator.async_add_listener(self._availability_changed))
        if self._choice:
            self.async_on_remove(self._choice.add_listener(self._source_changed))

    @callback
    def _availability_changed(self) -> None:
        """Write the state, and when the camera came or went, let Home Assistant
        pick its WebRTC provider again.

        Home Assistant asks stream_source() which provider (its built-in
        go2rtc) can serve a camera when the entity is added, and not again by
        itself. A camera that was unavailable at that moment, as during a
        reload, gave None and was left with HLS only: on 03-11-2026 its
        dashboard card took 8.6-10.5 s to start (HLS waits for a whole 6 s
        segment) and had no sound (HLS keeps only AAC and MP3 audio, the camera
        sends G.711 mu-law), where WebRTC through go2rtc carries the mu-law as
        it is.
        """
        self.async_write_ha_state()
        if self.available == self._was_available:
            return
        self._was_available = self.available
        # Since Home Assistant 2024.11, with the WebRTC providers.
        if refresh := getattr(self, "async_refresh_providers", None):
            self.hass.async_create_task(refresh())

    @callback
    def _source_changed(self, _source: str) -> None:
        """Move a running stream to the new path; a later one starts there."""
        if move_stream(self.stream, self._stream_url):
            _LOGGER.debug("Stream of %s moved to %s", self.entity_id, self._stream_url)
        self._declare_two_way_audio()
        # A choice can make the camera streamable or not (WebRTC turned off).
        self._availability_changed()

    async def stream_source(self) -> str | None:
        if self.available:
            # Home Assistant is starting (or keeping) a stream: check the bridge
            # soon, so a shared LAN session moves over in seconds rather than at
            # the next poll. Repeated requests are debounced by the coordinator.
            for delay in STREAM_START_CHECK_SECONDS:
                async_call_later(self.hass, delay, self._check_bridge)
        return self._stream_url if self.available else None

    @callback
    def _check_bridge(self, _now) -> None:
        entry = self.coordinator.config_entry
        status = self.hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("bridge_status") if entry else None
        if status is not None:
            self.hass.async_create_task(status.async_request_refresh())

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Pull a single JPEG frame from the live RTSP stream.

        Required by the `camera.snapshot` service. Without this override the
        base class raises NotImplementedError. The helper runs ffmpeg under
        the hood; it shares the running bridge stream so it adds no extra
        load on the camera itself.
        """
        try:
            return await ffmpeg_get_image(
                self.hass, self._stream_url, width=width, height=height
            )
        except Exception:
            _LOGGER.exception("ffmpeg snapshot failed for %s", self._stream_url)
            return None
