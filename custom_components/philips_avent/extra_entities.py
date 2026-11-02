"""Entities for the camera's remaining Tuya data points (extra_dps.py)."""
from __future__ import annotations

from functools import partial

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import PhilipsAventCoordinator
from .entity import add_entities_when_ready, build_device_info, dps_present
from .extra_dps import CONFIG, ExtraDp, for_platform, is_on, sensor_value


class _ExtraEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PhilipsAventCoordinator, cam_id: str, extra: ExtraDp):
        super().__init__(coordinator)
        self._extra = extra
        self._attr_translation_key = extra.key
        self._attr_icon = extra.icon
        self._attr_unique_id = f"{cam_id}_{extra.key}"
        self._attr_device_info = build_device_info(coordinator, cam_id)
        self._attr_entity_category = EntityCategory.CONFIG if extra.category == CONFIG else EntityCategory.DIAGNOSTIC
        self._attr_entity_registry_enabled_default = extra.enabled

    @property
    def _raw(self):
        return (self.coordinator.data or {}).get(self._extra.dps)

    async def _write(self, value) -> None:
        await self.coordinator.set_dps({self._extra.dps: value})
        self.coordinator.data[self._extra.dps] = value
        self.async_write_ha_state()


class AventExtraSelect(_ExtraEntity, SelectEntity):
    def __init__(self, coordinator, cam_id, extra):
        super().__init__(coordinator, cam_id, extra)
        self._attr_options = list(extra.options)

    @property
    def current_option(self) -> str | None:
        raw = self._raw
        return str(raw) if raw is not None and str(raw) in self._extra.options else None

    async def async_select_option(self, option: str) -> None:
        await self._write(option)


class AventExtraSwitch(_ExtraEntity, SwitchEntity):
    @property
    def is_on(self) -> bool | None:
        return is_on(self._raw)

    async def async_turn_on(self, **kwargs) -> None:
        await self._write(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._write(False)


class AventExtraSensor(_ExtraEntity, SensorEntity):
    @property
    def native_value(self):
        return sensor_value(self._extra, self._raw)


class AventExtraBinarySensor(_ExtraEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool | None:
        return is_on(self._raw)


_CLASSES = {
    "select": AventExtraSelect,
    "switch": AventExtraSwitch,
    "sensor": AventExtraSensor,
    "binary_sensor": AventExtraBinarySensor,
}


def _build(cls, coordinator, cam_id, extra) -> list:
    return [cls(coordinator, cam_id, extra)]


def add_extra_entities(platform: str, coordinator, cam_id: str, async_add_entities, on_unload) -> None:
    """Add this platform's extra entities, each once its data point is reported."""
    cls = _CLASSES[platform]
    add_entities_when_ready(coordinator, async_add_entities, [
        (dps_present(extra.dps), partial(_build, cls, coordinator, cam_id, extra))
        for extra in for_platform(platform)
    ], on_unload)
