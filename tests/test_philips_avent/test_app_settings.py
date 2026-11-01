"""Settings the app has: temperature alert thresholds, timers' time left, and their entity names."""

import json
from pathlib import Path

from const import seconds_left, temp_alert_celsius, temp_alert_raw

COMPONENT = Path(__file__).parent.parent.parent / "custom_components" / "philips_avent"


class TestTemperatureThreshold:
    def test_stored_hundredths_of_a_degree(self):
        # as two monitors reported them (01-11-2026): 20.0 / 16.0 and 24.0 / 15.0
        assert temp_alert_celsius(2000) == 20.0
        assert temp_alert_celsius("1500") == 15.0
        assert temp_alert_celsius(None) is None
        assert temp_alert_celsius("x") is None

    def test_written_back_as_whole_hundredths(self):
        assert temp_alert_raw(22.5) == 2250
        assert temp_alert_raw(18.0) == 1800
        assert temp_alert_raw(temp_alert_celsius(2350)) == 2350


class TestTimeLeft:
    def test_running_and_idle(self):
        assert seconds_left(5400) == 5400
        assert seconds_left("90") == 90
        assert seconds_left(-1) == 0      # the monitor's "not running"
        assert seconds_left(0) == 0

    def test_unknown(self):
        assert seconds_left(None) is None
        assert seconds_left("") is None


def test_translations():
    keys = {"switch": ("status_light", "temp_high_alert", "temp_low_alert"),
            "number": ("temp_high_threshold", "temp_low_threshold"),
            "sensor": ("lullaby_time_left", "night_light_time_left")}
    for name in ("strings.json", "translations/en.json", "translations/fr.json"):
        entity = json.loads((COMPONENT / name).read_text())["entity"]
        for platform, wanted in keys.items():
            for key in wanted:
                assert entity[platform][key]["name"], (name, platform, key)
