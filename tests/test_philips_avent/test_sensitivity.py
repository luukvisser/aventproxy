"""Alert sensitivity: the app's low/medium/high as the Tuya enum the monitor stores."""

import json
from pathlib import Path

from const import SENSITIVITY_VALUES, sensitivity_option

COMPONENT = Path(__file__).parent.parent.parent / "custom_components" / "philips_avent"


def test_values_round_trip():
    for option, raw in SENSITIVITY_VALUES.items():
        assert sensitivity_option(raw) == option


def test_measured_values():
    # "high" set in the app arrived as "2"; the default "1" is the middle (01-11-2026)
    assert sensitivity_option("2") == "high"
    assert sensitivity_option("1") == "medium"
    assert sensitivity_option(0) == "low"


def test_unknown_values():
    for bad in (None, "", "3", "high", 1.5):
        assert sensitivity_option(bad) is None, bad


def test_translations_cover_every_option():
    for name in ("strings.json", "translations/en.json", "translations/fr.json"):
        selects = json.loads((COMPONENT / name).read_text())["entity"]["select"]
        for key in ("motion_sensitivity", "sound_sensitivity"):
            assert set(selects[key]["state"]) == set(SENSITIVITY_VALUES), (name, key)
