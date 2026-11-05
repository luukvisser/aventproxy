"""Select entities for Philips Avent Baby Monitor."""
from __future__ import annotations

import json
import logging
from functools import partial

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoredExtraData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    DPS_LIGHT_TIMER,
    DPS_LIGHT_TIMER_SWITCH,
    DPS_LULLABY_CONTROL,
    DPS_LULLABY_MODE,
    DPS_LULLABY_TIMER,
    DPS_LULLABY_TIMER_SWITCH,
    DPS_MOTION_SENSITIVITY,
    DPS_SOUND_SENSITIVITY,
    LULLABY_TRACKS,
    SENSITIVITY_VALUES,
    TIMER_OPTIONS,
    TIMER_SECONDS_TO_LABEL,
    learn_lullaby_block,
    lullaby_block,
    lullaby_catalogue_known,
    lullaby_track_id,
    lullaby_track_name,
    parse_lullaby_record,
    sensitivity_option,
)
from .coordinator import PhilipsAventCoordinator
from .entity import add_entities_when_ready, bridge_streaming, build_device_info, dps_present
from .extra_entities import add_extra_entities
from .lan_quality import LanQualityChoice
from .stream_source import StreamSourceChoice

_LOGGER = logging.getLogger(__name__)

PLAY_MODES = ["loop", "loop1", "shuffle"]
PLAY_MODE_LABELS = {"loop": "Loop All", "loop1": "Repeat One", "shuffle": "Shuffle"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    choices = data.get("stream_sources", {})
    qualities = data.get("lan_qualities", {})
    entities = []
    for cam_id, coordinator in data["coordinators"].items():
        if cam_id in choices and choices[cam_id].streamable:
            entities.append(AventStreamSourceSelect(coordinator, cam_id, choices[cam_id]))
        if cam_id in qualities:
            entities.append(AventLanQualitySelect(coordinator, cam_id, qualities[cam_id]))
        add_extra_entities("select", coordinator, cam_id, async_add_entities, entry.async_on_unload)
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
        # Alert sensitivities, once the monitor reports them (both families do).
        add_entities_when_ready(coordinator, async_add_entities, [
            (dps_present(DPS_MOTION_SENSITIVITY), partial(
                _sensitivity, coordinator, cam_id, DPS_MOTION_SENSITIVITY, "motion_sensitivity", "mdi:motion-sensor")),
            (dps_present(DPS_SOUND_SENSITIVITY), partial(
                _sensitivity, coordinator, cam_id, DPS_SOUND_SENSITIVITY, "sound_sensitivity", "mdi:microphone-outline")),
        ], entry.async_on_unload)
    async_add_entities(entities)


def _sensitivity(coordinator, cam_id: str, dps_id: str, key: str, icon: str) -> list:
    return [AventSensitivitySelect(coordinator, cam_id, dps_id, key, icon)]


class AventSensitivitySelect(CoordinatorEntity, SelectEntity):
    """Motion or sound alert sensitivity: low, medium, high (the app's settings).

    Written over the LAN like the alert switches; the monitor confirms it with
    a push. A change made in the Philips app arrives through the cloud poll.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PhilipsAventCoordinator, cam_id: str, dps_id: str, key: str, icon: str):
        super().__init__(coordinator)
        self._attr_options = list(SENSITIVITY_VALUES)
        self._dps_id = dps_id
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_unique_id = f"{cam_id}_{key}"
        self._attr_device_info = build_device_info(coordinator, cam_id)

    @property
    def current_option(self) -> str | None:
        return sensitivity_option((self.coordinator.data or {}).get(self._dps_id))

    async def async_select_option(self, option: str) -> None:
        value = SENSITIVITY_VALUES[option]
        await self.coordinator.set_dps({self._dps_id: value})
        self.coordinator.data[self._dps_id] = value
        self.async_write_ha_state()


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
        if not lullaby_catalogue_known(current, self._learned):
            raise HomeAssistantError(
                f"{self.coordinator.camera_name} reports lullaby id {current}, outside every known "
                "catalogue block; pick a track with Next or Prev once (past the last track or the "
                "first), which shows the block, and picking by name works from then on"
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


class AventStreamSourceSelect(SelectEntity, RestoreEntity):
    """Where the camera entity takes its stream from.

    The choice is restored from Home Assistant's own state store rather than
    kept in the config entry options: an options change reloads the entry,
    which rewrites the bridge config and restarts the bridge, and this choice
    needs neither, since the bridge serves every mode on its own path (only a
    move to or from "WebRTC only" rewrites the config, StreamSourceChoice).
    The choice made is stored next to the state (``chosen``), so one that
    WebRTC off set aside comes back when WebRTC is turned on again.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "stream_source"
    _attr_icon = "mdi:video-switch"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PhilipsAventCoordinator, cam_id: str, choice: StreamSourceChoice):
        self._cam_id = cam_id
        self._choice = choice
        self._attr_options = list(choice.options)
        self._attr_unique_id = f"{cam_id}_stream_source"
        self._coordinator = coordinator
        self._attr_device_info = build_device_info(coordinator, cam_id)

    @property
    def available(self) -> bool:
        """Like the camera entity: not while Home Assistant has no data from the
        camera, unless the bridge is streaming it. The choice is Home
        Assistant's own and needs no data, but stays with the picture it is for.
        """
        return self._coordinator.last_update_success or bridge_streaming(self.hass, self._coordinator, self._cam_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._coordinator.async_add_listener(self.async_write_ha_state))
        last = await self.async_get_last_state()
        extra = await self.async_get_last_extra_data()
        self._choice.restore(
            last.state if last else None,
            (extra.as_dict() or {}).get("chosen") if extra else None,
        )

    @property
    def extra_restore_state_data(self) -> RestoredExtraData:
        return RestoredExtraData({"chosen": self._choice.chosen})

    @property
    def current_option(self) -> str | None:
        return self._choice.source

    async def async_select_option(self, option: str) -> None:
        self._choice.select(option)
        self.async_write_ha_state()


class AventLanQualitySelect(SelectEntity, RestoreEntity):
    """The resolution this camera's local stream asks for: HD or standard.

    Per camera, because one account can hold different models and not every
    one may send HD. Restored from Home Assistant's state store, like the
    stream source, so a change does not go through the options flow (which
    reloads the entry). A change writes this camera's ``lan_clarity`` into
    the bridge config, and the add-on then restarts the bridge: the running
    streams of all cameras drop for a moment.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "lan_quality"
    _attr_icon = "mdi:high-definition"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PhilipsAventCoordinator, cam_id: str, choice: LanQualityChoice):
        self._cam_id = cam_id
        self._choice = choice
        self._attr_options = list(choice.options)
        self._attr_unique_id = f"{cam_id}_lan_quality"
        self._coordinator = coordinator
        self._attr_device_info = build_device_info(coordinator, cam_id)

    @property
    def available(self) -> bool:
        """Like the camera entity: not while Home Assistant has no data from the
        camera, unless the bridge is streaming it. The choice is Home
        Assistant's own and needs no data, but stays with the picture it is for.
        """
        return self._coordinator.last_update_success or bridge_streaming(self.hass, self._coordinator, self._cam_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._coordinator.async_add_listener(self.async_write_ha_state))
        last = await self.async_get_last_state()
        self._choice.restore(last.state if last else None)

    @property
    def current_option(self) -> str | None:
        return self._choice.quality

    async def async_select_option(self, option: str) -> None:
        self._choice.select(option)
        self.async_write_ha_state()
