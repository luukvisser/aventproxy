"""Settings the app has: temperature alert thresholds, timers' time left, and their entity names."""

import json
from pathlib import Path

from const import Countdown, lullaby_running, night_light_running, seconds_left, temp_alert_celsius, temp_alert_raw

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


class TestCountdown:
    """The monitor holds the set length until the timer ends; the countdown is ours."""

    def test_counts_down_from_the_first_report(self):
        c = Countdown()
        c.seen(1800, now=1000.0)
        assert c.left(1000.0) == 1800
        assert c.left(1000.0 + 22 * 60) == 480        # the case of 02-11: 1800 still reported
        c.seen(1800, now=1000.0 + 22 * 60)            # the same length again keeps the end
        assert c.left(1000.0 + 22 * 60) == 480
        assert c.left(1000.0 + 1800) == 0
        assert c.left(1000.0 + 5000) == 0

    def test_rounds_up(self):
        c = Countdown()
        c.seen(300, now=0.0)
        assert c.left(299.2) == 1

    def test_end_and_new_timer(self):
        c = Countdown()
        c.seen(300, now=0.0)
        c.seen(-1, now=100.0)                         # ended (or switched off): 0, no end
        assert c.left(100.0) == 0 and c.ends_at is None
        c.seen(300, now=200.0)                        # the same length again after an end: a new timer
        assert c.left(200.0) == 300

    def test_another_length_restarts(self):
        c = Countdown()
        c.seen(5400, now=0.0)
        c.seen(1800, now=60.0)
        assert c.left(60.0) == 1800

    def test_restored_end_kept_while_the_length_is_the_same(self):
        c = Countdown(1800, 1800.0)                   # saved before a restart
        c.seen(1800, now=900.0)
        assert c.left(900.0) == 900
        c = Countdown(1800, 1800.0)
        c.seen(5400, now=900.0)                       # changed while Home Assistant was down
        assert c.left(900.0) == 5400

    def test_a_start_with_the_same_length_counts_down(self):
        # 02-11-2026: 245 read 5400 with the lullaby stopped, the saved end of
        # an earlier run had passed, and a start at the same length showed 0.
        c = Countdown(5400, 1000.0)                   # restored, the end passed
        c.seen(5400, now=2000.0, running=False)       # reported while stopped
        assert c.left(2000.0) == 0 and c.ends_at is None
        c.seen(5400, now=3000.0, running=True)        # 246 "playing" (245 a moment later)
        assert c.left(3000.0) == 5400
        c.seen(5400, now=3000.2, running=True)        # the length again: same end
        assert c.left(3600.0) == 4800

    def test_a_stop_ends_the_countdown(self):
        c = Countdown()
        c.seen(1800, now=0.0, running=True)
        c.seen(1800, now=600.0, running=False)        # stopped before the timer ran out
        assert c.left(600.0) == 0
        c.seen(None, now=900.0, running=True)         # on again: a new run of the length
        assert c.left(900.0) == 1800

    def test_restored_end_kept_while_still_running(self):
        c = Countdown(1800, 1800.0)
        c.seen(1800, now=900.0, running=True)
        assert c.left(900.0) == 900

    def test_unknown_running_keeps_the_old_rule(self):
        c = Countdown(1800, 1800.0)
        c.seen(1800, now=900.0, running=None)         # paused, or 246 not reported
        assert c.left(900.0) == 900

    def test_running_from_the_data_points(self):
        assert lullaby_running({"246": "playing"}) is True
        assert lullaby_running({"246": "stopping"}) is False
        assert lullaby_running({"246": "paused"}) is None
        assert lullaby_running({}) is None and lullaby_running(None) is None
        assert night_light_running({"138": True}) is True
        assert night_light_running({"138": False}) is False
        assert night_light_running({"138": "1"}) is None

    def test_unknown_before_any_report(self):
        c = Countdown()
        assert c.left(0.0) is None
        c.seen(None, now=0.0)
        c.seen("", now=0.0)
        assert c.left(0.0) is None
