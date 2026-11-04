"""The offline cache on disk, through Home Assistant's private storage helper."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .offline_cache import (
    CACHE_MINOR_VERSION,
    CACHE_VERSION,
    cache_key,
    dump_cache,
    migrate_cache,
    parse_cache,
    save_due,
)


class _CacheStore(Store):
    async def _async_migrate_func(self, old_major_version: int, old_minor_version: int, old_data: Any) -> dict:
        return migrate_cache(old_major_version, old_minor_version, old_data)


class OfflineCache:
    """Per-entry cache of what a camera needs to start on the LAN alone.

    private=True writes the file owner-only: it holds the cameras' localKeys.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._entry_id = entry_id
        self._store = _CacheStore(
            hass, CACHE_VERSION, cache_key(entry_id), private=True, minor_version=CACHE_MINOR_VERSION
        )
        self._cameras: dict[str, dict] = {}
        self._saved_at: dict[str, float] = {}
        # Once the entry is removed, a write still on its way must not bring
        # the file, and the keys in it, back: the lock lets a save in flight
        # finish before the file goes, and none starts after.
        self._removed = False
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        self._cameras = parse_cache(await self._store.async_load())

    def get(self, camera_id: str) -> dict | None:
        return self._cameras.get(camera_id)

    async def async_update(self, camera_id: str, entry: dict | None) -> None:
        """Write the camera's entry when save_due says it changed enough."""
        async with self._lock:
            if self._removed:
                return
            now = time.monotonic()
            if not save_due(self._cameras.get(camera_id), entry, self._saved_at.get(camera_id), now):
                return
            self._cameras[camera_id] = entry
            self._saved_at[camera_id] = now
            await self._store.async_save(dump_cache(self._cameras))

    async def async_remove(self) -> None:
        async with self._lock:
            self._removed = True
            await self._store.async_remove()
        _CACHES.pop(self._entry_id, None)


# One cache per entry for the life of Home Assistant, across reloads, so the
# removal of an entry goes through the instance its coordinators write with.
_CACHES: dict[str, OfflineCache] = {}


def offline_cache(hass: HomeAssistant, entry_id: str) -> OfflineCache:
    """This entry's cache, the same instance for setup and removal."""
    cache = _CACHES.get(entry_id)
    if cache is None:
        cache = _CACHES[entry_id] = OfflineCache(hass, entry_id)
    return cache
