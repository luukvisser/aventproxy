"""The per-camera "Local stream quality" choice, shared by the select and setup.

Kept free of Home Assistant imports so the unit tests can load it.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

try:
    from .const import DEFAULT_LAN_QUALITY, LAN_QUALITIES
    from .payload import LAN_QUALITY_BY_CLARITY
except ImportError:  # loaded standalone by unit tests, without the HA package
    from const import DEFAULT_LAN_QUALITY, LAN_QUALITIES
    from payload import LAN_QUALITY_BY_CLARITY

_LOGGER = logging.getLogger(__name__)


def resolve_lan_quality(restored=None, carried_clarity=None) -> str:
    """One camera's local stream quality, "hd" or "sd".

    Precedence: the choice the camera's select restored > the ``lan_clarity``
    carried over from the previous bridge config > the default, HD.
    Anything unusable at one level falls through to the next, so a setting
    never silently changes.
    """
    if restored in LAN_QUALITIES:
        return restored
    carried = LAN_QUALITY_BY_CLARITY.get(carried_clarity) if not isinstance(carried_clarity, bool) else None
    if carried:
        return carried
    return DEFAULT_LAN_QUALITY


class LanQualityChoice:
    """One camera's local stream quality: what the select shows, what the bridge gets.

    Setup builds it with the quality it wrote into the bridge config (from
    the carried-over value or the old option); the select then restores its
    stored choice (``restore``) and sets new ones (``select``). Each change
    calls ``on_change(quality)``, which writes the camera's ``lan_clarity``
    into the bridge config in place; the add-on then restarts the bridge.
    """

    def __init__(self, cam_id: str, initial: str, on_change: Callable[[str], None] | None = None) -> None:
        self.cam_id = cam_id
        self.options = list(LAN_QUALITIES)
        self._quality = initial if initial in LAN_QUALITIES else DEFAULT_LAN_QUALITY
        self._on_change = on_change

    @property
    def quality(self) -> str:
        return self._quality

    def restore(self, stored: str | None) -> str:
        """Take the choice stored before a restart; keep the initial one without it."""
        self._set(stored if stored in LAN_QUALITIES else self._quality)
        return self._quality

    def select(self, option: str) -> None:
        if option not in self.options:
            raise ValueError(f"local stream quality {option!r} is not available for camera {self.cam_id}")
        self._set(option)

    def _set(self, quality: str) -> None:
        if quality == self._quality:
            return
        self._quality = quality
        if self._on_change is not None:
            self._on_change(quality)
