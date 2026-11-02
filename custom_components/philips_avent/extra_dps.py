"""The camera's remaining Tuya data points, as plain entities.

Everything in the device schema that no other entity covers, taken from the
schemas of an SCD951 (selj2idknqhjnids) and an SCD973-family unit
(kzm54lhabeeucq5a), 02-11-2026. Names follow the schema codes; what several
of them do is not documented, so:

- a data point that can switch the monitor off, start an update, or whose
  effect is unknown is disabled by default (`enabled=False`), and is shown
  read-only where writing it is not needed to see what it holds;
- each entity is created only once the camera reports its data point, so a
  model without it gets nothing.

The raw log data points (bu_logs 19, pu_logs 20) are binary and left out.
Kept free of Home Assistant imports so the unit tests can load it.
"""
from __future__ import annotations

from dataclasses import dataclass

SELECT = "select"
SWITCH = "switch"
SENSOR = "sensor"
BINARY_SENSOR = "binary_sensor"

CONFIG = "config"
DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True)
class ExtraDp:
    dps: str
    key: str            # translation key and unique id suffix
    platform: str
    icon: str
    enabled: bool = True
    category: str = DIAGNOSTIC
    options: tuple[str, ...] = ()   # select values, as the camera takes them
    scale: int = 0                  # sensor value: raw / 10**scale
    why_disabled: str = ""


EXTRA_DPS: tuple[ExtraDp, ...] = (
    ExtraDp("102", "image_flip", SELECT, "mdi:flip-horizontal", category=CONFIG,
            options=("flip_none", "flip_horizontal_mirror", "flip_vertical_mirror", "flip_rotate_180")),
    ExtraDp("168", "motion_area", SWITCH, "mdi:vector-square", category=CONFIG),
    ExtraDp("239", "monitor_sensitivity", SELECT, "mdi:tune-vertical", category=CONFIG,
            options=("0", "1", "2", "3")),
    ExtraDp("252", "parent_unit_talking", BINARY_SENSOR, "mdi:account-voice"),
    ExtraDp("253", "app_talking", BINARY_SENSOR, "mdi:cellphone-sound"),
    ExtraDp("205", "power_status", SENSOR, "mdi:power-plug"),
    ExtraDp("208", "temperature_report", SENSOR, "mdi:thermometer-lines", enabled=False, scale=2,
            why_disabled="meaning unknown"),
    ExtraDp("169", "motion_area_definition", SENSOR, "mdi:vector-polygon", enabled=False,
            why_disabled="raw zone definition"),
    ExtraDp("204", "night_light_colour", SENSOR, "mdi:palette", enabled=False,
            why_disabled="format unknown"),
    ExtraDp("235", "high_temperature_f", SENSOR, "mdi:thermometer-chevron-up", enabled=False,
            why_disabled="the same limit in Fahrenheit"),
    ExtraDp("236", "low_temperature_f", SENSOR, "mdi:thermometer-chevron-down", enabled=False,
            why_disabled="the same limit in Fahrenheit"),
    ExtraDp("21", "extended_functions", SENSOR, "mdi:puzzle", enabled=False,
            why_disabled="meaning unknown"),
    ExtraDp("254", "baby_unit_reset", SENSOR, "mdi:restart-alert", enabled=False,
            why_disabled="meaning unknown"),
    ExtraDp("247", "power_off", SELECT, "mdi:power", enabled=False, category=CONFIG, options=("0", "1"),
            why_disabled="dangerous: switches the camera off"),
    ExtraDp("206", "firmware_update_message", SENSOR, "mdi:update", enabled=False,
            why_disabled="dangerous to write: firmware update; shown read-only"),
    ExtraDp("249", "voice_upgrade", SENSOR, "mdi:account-voice", enabled=False,
            why_disabled="dangerous to write: voice pack update; shown read-only"),
    ExtraDp("251", "background_mode", SWITCH, "mdi:application-cog", enabled=False, category=CONFIG,
            why_disabled="effect unknown"),
    ExtraDp("255", "timer_report", SELECT, "mdi:timer-cog", enabled=False, category=CONFIG, options=("0", "1"),
            why_disabled="effect unknown"),
)


def for_platform(platform: str) -> tuple[ExtraDp, ...]:
    return tuple(e for e in EXTRA_DPS if e.platform == platform)


def is_on(value: object) -> bool | None:
    """A Tuya "0"/"1" enum, or a bool, as on/off; None for anything else."""
    if isinstance(value, bool):
        return value
    if value in ("0", "1", 0, 1):
        return str(value) == "1"
    return None


def sensor_value(extra: ExtraDp, value: object):
    """The sensor state for a raw value: scaled numbers, text as is, '' as nothing."""
    if value is None or value == "":
        return None
    if extra.scale and isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 10 ** extra.scale
    if isinstance(value, str) and len(value) > 255:
        return value[:255]
    return value
