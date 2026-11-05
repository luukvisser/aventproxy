"""Number entities for Philips Avent Baby Monitor."""
from __future__ import annotations

from functools import partial

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    DPS_AWAKE_DELAY,
    DPS_BRIGHTNESS,
    DPS_LULLABY_VOLUME,
    DPS_TEMP_HIGH,
    DPS_TEMP_LOW,
    temp_alert_celsius,
    temp_alert_raw,
)
from .coordinator import PhilipsAventCoordinator
from .entity import add_entities_when_ready, build_device_info, dps_present


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for cam_id, coordinator in data["coordinators"].items():
        entities.extend([
            AventNumber(coordinator, cam_id, DPS_BRIGHTNESS, None, "mdi:brightness-6", 1, 100, 1, "%", translation_key="night_light_brightness"),
            AventNumber(coordinator, cam_id, DPS_LULLABY_VOLUME, None, "mdi:volume-medium", 1, 100, 1, "%", translation_key="lullaby_volume"),
        ])
        # SenseIQ: delay before the "baby awake" alert fires (seconds).
        add_entities_when_ready(coordinator, async_add_entities, [
            (dps_present(DPS_AWAKE_DELAY), partial(_awake_delay, coordinator, cam_id)),
        ], entry.async_on_unload)
        # Temperature alert thresholds, as the app sets them.
        add_entities_when_ready(coordinator, async_add_entities, [
            (dps_present(DPS_TEMP_HIGH), partial(_threshold, coordinator, cam_id, DPS_TEMP_HIGH, "temp_high_threshold", "mdi:thermometer-high")),
            (dps_present(DPS_TEMP_LOW), partial(_threshold, coordinator, cam_id, DPS_TEMP_LOW, "temp_low_threshold", "mdi:thermometer-low")),
        ], entry.async_on_unload)
    async_add_entities(entities)


def _awake_delay(coordinator: PhilipsAventCoordinator, cam_id: str) -> list:
    return [
        AventNumber(coordinator, cam_id, DPS_AWAKE_DELAY, None, "mdi:timer-outline", 0, 600, 30, "s", translation_key="awake_alert_delay")
    ]


class AventNumber(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: PhilipsAventCoordinator, cam_id: str,
        dps_id: str, name: str | None, icon: str,
        min_val: float, max_val: float, step: float, unit: str,
        *, translation_key: str | None = None,
    ):
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._dps_id = dps_id
        if translation_key is not None:
            self._attr_translation_key = translation_key
        else:
            self._attr_name = name
        self._attr_icon = icon
        self._attr_native_min_value = min_val
        self._attr_native_max_value = max_val
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_unique_id = f"{cam_id}_{dps_id}"
        self._attr_device_info = build_device_info(coordinator, cam_id)

    @property
    def native_value(self) -> float | None:
        dps = self.coordinator.data
        if dps and self._dps_id in dps:
            return float(dps[self._dps_id])
        return None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.set_dps({self._dps_id: int(value)})


def _threshold(coordinator: PhilipsAventCoordinator, cam_id: str, dps_id: str, key: str, icon: str) -> list:
    return [AventTemperatureThreshold(coordinator, cam_id, dps_id, key, icon)]


class AventTemperatureThreshold(CoordinatorEntity, NumberEntity):
    """A temperature alert threshold; the monitor stores degrees C x 100."""

    _attr_has_entity_name = True
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = 0
    _attr_native_max_value = 40
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PhilipsAventCoordinator, cam_id: str, dps_id: str, key: str, icon: str):
        super().__init__(coordinator)
        self._dps_id = dps_id
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_unique_id = f"{cam_id}_{key}"
        self._attr_device_info = build_device_info(coordinator, cam_id)

    @property
    def native_value(self) -> float | None:
        return temp_alert_celsius((self.coordinator.data or {}).get(self._dps_id))

    async def async_set_native_value(self, value: float) -> None:
        raw = temp_alert_raw(value)
        await self.coordinator.set_dps({self._dps_id: raw})
        self.coordinator.data[self._dps_id] = raw
        self.async_write_ha_state()
