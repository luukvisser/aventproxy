"""The per-camera stream source choice, shared by the select and the camera.

Kept free of Home Assistant imports so the unit tests can load it.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

try:
    from .const import (
        STREAM_SOURCE_AUTO,
        STREAM_SOURCE_LOCAL,
        STREAM_SOURCE_WEBRTC,
        STREAM_SOURCE_WEBRTC_FIRST,
    )
except ImportError:  # loaded standalone by unit tests, without the HA package
    from const import (
        STREAM_SOURCE_AUTO,
        STREAM_SOURCE_LOCAL,
        STREAM_SOURCE_WEBRTC,
        STREAM_SOURCE_WEBRTC_FIRST,
    )

_LOGGER = logging.getLogger(__name__)


ALL_SOURCES = (STREAM_SOURCE_AUTO, STREAM_SOURCE_WEBRTC_FIRST, STREAM_SOURCE_LOCAL, STREAM_SOURCE_WEBRTC)


def stream_source_options(lan_available: bool, webrtc: bool = True) -> list[str]:
    """The choices a camera offers.

    Without a local stream (LAN streaming off, no local key, or a protocol the
    bridge's local path does not speak) "local" would always fail and "WebRTC
    first" would be the same as "WebRTC only", so both are left out, and
    "auto" streams from the cloud. Without WebRTC only "local" is left, and a
    camera without a local stream has no stream.
    """
    if not webrtc:
        return [STREAM_SOURCE_LOCAL] if lan_available else []
    if lan_available:
        return [STREAM_SOURCE_AUTO, STREAM_SOURCE_WEBRTC_FIRST, STREAM_SOURCE_LOCAL, STREAM_SOURCE_WEBRTC]
    return [STREAM_SOURCE_AUTO, STREAM_SOURCE_WEBRTC]


class StreamSourceChoice:
    """One camera's stream source: what the select shows, what the camera uses.

    Setup builds it with the choice carried over in the bridge config
    (``initial``); the select then restores its stored choice (``restore``)
    and sets new ones (``select``). The camera reads ``source`` and listens
    for changes to move its running stream. Every mode is a path the bridge
    always serves, so only a move to or from "WebRTC only", which decides
    whether the bridge gets the camera's LAN secrets, calls ``on_change``
    (a bridge config rewrite).
    """

    def __init__(
        self,
        cam_id: str,
        lan_available: bool,
        webrtc: bool = True,
        initial: str | None = None,
        on_change: Callable[[str | None], None] | None = None,
    ) -> None:
        self.cam_id = cam_id
        self.lan_available = lan_available
        self.webrtc = webrtc
        self.options = stream_source_options(lan_available, webrtc)
        self._source = initial if initial in self.options else self.default
        self._chosen: str | None = None
        self._listeners: list[Callable[[str], None]] = []
        self._on_change = on_change

    @property
    def default(self) -> str | None:
        """Auto where it is offered, else the only choice left, else None."""
        if STREAM_SOURCE_AUTO in self.options:
            return STREAM_SOURCE_AUTO
        return self.options[0] if self.options else None

    @property
    def streamable(self) -> bool:
        """Whether the camera has any stream (not without WebRTC and LAN)."""
        return bool(self.options)

    @property
    def source(self) -> str | None:
        return self._source

    @property
    def chosen(self) -> str | None:
        """The last choice made, also while it is not offered.

        The select stores it next to its state, so a choice that WebRTC off
        or removed LAN addresses set aside comes back once it is offered again.
        """
        return self._chosen

    def restore(self, stored: str | None, chosen: str | None = None) -> str | None:
        """Take the choice made before a restart, or the default if it is not offered.

        ``chosen`` is what the select stored next to its state; ``stored`` is
        the state itself, which is all an older version left. A choice that is
        not offered now is kept as ``chosen`` and logged once, here: a
        "local" whose LAN addresses were removed, or a WebRTC mode (auto
        included) while WebRTC is off.
        """
        wanted = chosen if chosen in ALL_SOURCES else stored
        if wanted in self.options:
            self._chosen = wanted
            self._set(wanted)
        elif wanted in ALL_SOURCES:
            self._chosen = wanted
            if self.webrtc:
                _LOGGER.warning(
                    "Stream source %r of camera %s is not available (no local stream), using %r",
                    wanted, self.cam_id, self.default,
                )
            else:
                _LOGGER.info(
                    "Stream source %r of camera %s needs WebRTC, which is off, using %r",
                    wanted, self.cam_id, self.default,
                )
            self._set(self.default)
        return self._source

    def select(self, option: str) -> None:
        if option not in self.options:
            raise ValueError(f"stream source {option!r} is not available for camera {self.cam_id}")
        self._chosen = option
        self._set(option)

    def add_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _set(self, source: str | None) -> None:
        if source == self._source:
            return
        secrets_changed = (source == STREAM_SOURCE_WEBRTC) != (self._source == STREAM_SOURCE_WEBRTC)
        self._source = source
        for listener in list(self._listeners):
            listener(source)
        if secrets_changed and self._on_change is not None:
            self._on_change(source)


def offers_two_way_audio(talkback: bool, source: str | None) -> bool:
    """Whether a camera on this stream source can talk: the bridge offers a
    return channel with talkback on, on every path but the local stream's,
    which uses neither MQTT nor WebRTC (pkg/rtsp's offersTalkback)."""
    return talkback and source != STREAM_SOURCE_LOCAL


def move_stream(stream, url: str) -> bool:
    """Point a Home Assistant stream at a new URL.

    ``Stream.update_source`` (homeassistant/components/stream, checked in
    2026.10.4) stores the new source and restarts the worker with it at once,
    so it is used only while the worker runs. On a stream whose worker is not
    running it leaves the fast-restart flag set, which ``start()`` does not
    clear: the next ``stop()`` then restarts the worker instead of ending it
    and waits for it for good under the start/stop lock, and every later
    start of that stream hangs (05-11-2026: the dashboard's stream request
    never answered after a stream source change). A stopped stream only needs
    the new source for its next start. Without a stream there is nothing to
    move: the next one is built from ``stream_source``. Returns whether the
    stream was pointed at the new URL.
    """
    if stream is None or getattr(stream, "source", None) == url:
        return False
    worker = getattr(stream, "_thread", None)
    if worker is not None and worker.is_alive():
        stream.update_source(url)
    else:
        stream.source = url
    return True
