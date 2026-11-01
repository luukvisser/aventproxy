"""Select entities for Philips Avent Baby Monitor."""
from __future__ import annotations

import json
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    DPS_LIGHT_TIMER,
    DPS_LIGHT_TIMER_SWITCH,
    DPS_LULLABY_CONTROL,
    DPS_LULLABY_MODE,
    DPS_LULLABY_TIMER,
    DPS_LULLABY_TIMER_SWITCH,
    LULLABY_TRACKS,
    TIMER_OPTIONS,
    TIMER_SECONDS_TO_LABEL,
    learn_lullaby_block,
    lullaby_block,
    lullaby_track_id,
    lullaby_track_name,
    parse_lullaby_record,
)
from .coordinator import PhilipsAventCoordinator
from .entity import build_device_info

_LOGGER = logging.getLogger(__name__)

PLAY_MODES = ["loop", "loop1", "shuffle"]
PLAY_MODE_LABELS = {"loop": "Loop All", "loop1": "Repeat One", "shuffle": "Shuffle"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for cam_id, coordinator in data["coordinators"].items():
        entities.append(AventLullabySelect(coordinator, cam_id))
        entities.append(AventPlayModeSelect(coordinator, cam_id))
        entities.append(AventTimerSelect(
            coordinator, cam_id,
            None, "mdi:timer-music-outline",
            DPS_LULLABY_TIMER, DPS_LULLABY_TIMER_SWITCH, translation_key="lullaby_timer",
        ))
        entities.append(AventTimerSelect(
            coordinator, cam_id,
            None, "mdi:timer-outline",
            DPS_LIGHT_TIMER, DPS_LIGHT_TIMER_SWITCH, translation_key="night_light_timer",
        ))
    async_add_entities(entities)


class AventLullabySelect(CoordinatorEntity, SelectEntity, RestoreEntity):
    """The lullaby track, by name.

    The camera reports the playing track as an id in DPS 248, and units hold
    the catalogue under different id blocks (const.LULLABY_BLOCK_STARTS). A
    block not in that list is learned when the camera wraps from the last
    track to the first or back (learn_lullaby_block), and kept across restarts.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "lullaby_track"
    _attr_icon = "mdi:music-note"
    _attr_options = LULLABY_TRACKS

    def __init__(self, coordinator: PhilipsAventCoordinator, cam_id: str):
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._attr_unique_id = f"{cam_id}_lullaby_track"
        self._attr_device_info = build_device_info(coordinator, cam_id)
        self._learned: tuple[int, ...] = ()
        self._last_id: int | None = None
        self._warned_unknown = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        blocks = (last.attributes.get("learned_blocks") if last else None) or []
        self._learned = tuple(b for b in blocks if isinstance(b, int))
        self._last_id = self._current_track_id()

    def _current_track_id(self) -> int | None:
        """The track id the camera last reported in DPS 248, if any."""
        dps = self.coordinator.data
        if not dps or "248" not in dps:
            return None
        return parse_lullaby_record(dps["248"])

    @callback
    def _handle_coordinator_update(self) -> None:
        current = self._current_track_id()
        block = learn_lullaby_block(self._last_id, current)
        if block is not None and lullaby_block(block, self._learned) is None:
            self._learned = (*self._learned, block)
            _LOGGER.info("Lullaby catalogue of %s learned: block starting at %s", self.coordinator.camera_name, block)
        if current is not None:
            self._last_id = current
        super()._handle_coordinator_update()

    @property
    def current_option(self) -> str | None:
        return lullaby_track_name(self._current_track_id(), self._learned)

    @property
    def extra_state_attributes(self) -> dict:
        current = self._current_track_id()
        return {
            "track_id": current,
            "catalogue_block": lullaby_block(current, self._learned),
            "learned_blocks": list(self._learned),
        }

    async def async_select_option(self, option: str) -> None:
        current = self._current_track_id()
        if current is not None and lullaby_block(current, self._learned) is None and not self._warned_unknown:
            self._warned_unknown = True
            _LOGGER.warning(
                "%s reports lullaby id %s, outside every known block; picking a track by name may not "
                "work until a Next past the last track (or Prev past the first) shows its block",
                self.coordinator.camera_name, current,
            )
        track_id = lullaby_track_id(option, current, self._learned)
        if track_id is None:
            return
        # The monitor echoes the chosen track in DPS 248, not in the 202 written.
        await self.coordinator.set_dps(
            {
                "202": json.dumps({"bizcode": "phi-no-bm", "id": track_id}),
                DPS_LULLABY_CONTROL: "play",
            },
            optimistic={"248": json.dumps({"bizcode": "phi-no-bm", "id": track_id, "errcode": 0})},
        )
        self._last_id = track_id
        self.async_write_ha_state()


class AventPlayModeSelect(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "play_mode"
    _attr_icon = "mdi:repeat"

    def __init__(self, coordinator: PhilipsAventCoordinator, cam_id: str):
        super().__init__(coordinator)
        self._attr_options = list(PLAY_MODE_LABELS.values())
        self._cam_id = cam_id
        self._attr_unique_id = f"{cam_id}_play_mode"
        self._attr_device_info = build_device_info(coordinator, cam_id)

    @property
    def current_option(self) -> str | None:
        dps = self.coordinator.data
        if dps and DPS_LULLABY_MODE in dps:
            return PLAY_MODE_LABELS.get(dps[DPS_LULLABY_MODE])
        return None

    async def async_select_option(self, option: str) -> None:
        mode = next((k for k, v in PLAY_MODE_LABELS.items() if v == option), None)
        if mode is None:
            return
        await self.coordinator.set_dps({DPS_LULLABY_MODE: mode})


class AventTimerSelect(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: PhilipsAventCoordinator, cam_id: str,
        name: str | None, icon: str, dps_timer: str, dps_switch: str,
        *, translation_key: str | None = None,
    ):
        super().__init__(coordinator)
        self._attr_options = list(TIMER_OPTIONS.keys())
        self._cam_id = cam_id
        self._dps_timer = dps_timer
        self._dps_switch = dps_switch
        if translation_key is not None:
            self._attr_translation_key = translation_key
        else:
            self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{cam_id}_{dps_timer}_timer"
        self._attr_device_info = build_device_info(coordinator, cam_id)

    @property
    def current_option(self) -> str | None:
        dps = self.coordinator.data
        if not dps:
            return None
        switch_on = dps.get(self._dps_switch)
        if not switch_on:
            return "Off"
        seconds = dps.get(self._dps_timer, 0)
        return TIMER_SECONDS_TO_LABEL.get(seconds, f"{seconds // 60} min")

    async def async_select_option(self, option: str) -> None:
        seconds = TIMER_OPTIONS.get(option, 0)
        if seconds == 0:
            await self.coordinator.set_dps({self._dps_switch: False})
        else:
            await self.coordinator.set_dps({self._dps_switch: True, self._dps_timer: seconds})
