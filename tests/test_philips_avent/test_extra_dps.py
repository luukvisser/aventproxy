"""The camera's remaining Tuya data points as entities (extra_dps.py)."""

import json
import re
from pathlib import Path

import pytest
from extra_dps import BINARY_SENSOR, EXTRA_DPS, SELECT, SENSOR, SWITCH, ExtraDp, is_on, sensor_value

COMPONENT = Path(__file__).parent.parent.parent / "custom_components" / "philips_avent"
DANGEROUS = {"247", "206", "249", "251", "255"}   # power off, firmware, voice pack, unknown effect


def test_dangerous_ones_are_disabled_by_default():
    by_dps = {e.dps: e for e in EXTRA_DPS}
    for dps in DANGEROUS:
        assert by_dps[dps].enabled is False, dps
        assert by_dps[dps].why_disabled


def test_updates_are_read_only():
    # Firmware and voice pack updates are shown, never written from HA.
    by_dps = {e.dps: e for e in EXTRA_DPS}
    assert by_dps["206"].platform == SENSOR
    assert by_dps["249"].platform == SENSOR


def test_every_disabled_one_says_why():
    for e in EXTRA_DPS:
        assert e.enabled or e.why_disabled, e.key


def test_no_data_point_twice_and_none_the_integration_already_covers():
    dps = [e.dps for e in EXTRA_DPS]
    assert len(dps) == len(set(dps))
    covered = set()
    const = (COMPONENT / "const.py").read_text()
    for name, number in re.findall(r'^(DPS_[A-Z0-9_]+)\s*=\s*"(\d+)"', const, re.M):
        used = any(re.search(rf"\b{name}\b", p.read_text()) for p in COMPONENT.glob("*.py") if p.name != "const.py")
        if used:
            covered.add(number)
    covered |= {"202", "248"}   # the lullaby track, read and written by number in select.py
    assert not covered & set(dps), covered & set(dps)


def test_keys_are_unique_and_translated():
    keys = [e.key for e in EXTRA_DPS]
    assert len(keys) == len(set(keys))
    for path in ("strings.json", "translations/en.json", "translations/fr.json"):
        entity = json.loads((COMPONENT / path).read_text(encoding="utf-8"))["entity"]
        for e in EXTRA_DPS:
            assert entity[e.platform][e.key]["name"], (path, e.key)


def test_selects_have_options_and_their_labels():
    strings = json.loads((COMPONENT / "strings.json").read_text())["entity"]["select"]
    for e in EXTRA_DPS:
        if e.platform == SELECT:
            assert e.options, e.key
            states = strings[e.key].get("state")
            if states:
                assert set(states) == set(e.options), e.key


@pytest.mark.parametrize(("raw", "expected"), [(True, True), (False, False), ("1", True), ("0", False), (1, True), ("x", None), (None, None)])
def test_is_on(raw, expected):
    assert is_on(raw) is expected


def test_sensor_value():
    scaled = ExtraDp("208", "t", SENSOR, "mdi:x", scale=2)
    assert sensor_value(scaled, 2350) == 23.5
    assert sensor_value(scaled, "") is None
    plain = ExtraDp("169", "a", SENSOR, "mdi:x")
    assert sensor_value(plain, "x" * 300) == "x" * 255
    assert sensor_value(plain, "abc") == "abc"


def test_platforms_known():
    assert {e.platform for e in EXTRA_DPS} <= {SELECT, SWITCH, SENSOR, BINARY_SENSOR}
    for f in ("select.py", "switch.py", "sensor.py", "binary_sensor.py"):
        assert "add_extra_entities(" in (COMPONENT / f).read_text()
