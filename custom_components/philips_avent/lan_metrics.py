"""Cheap measures of the LAN connection, to compare "separate" and "shared".

Counted from what the integration does anyway; nothing here sends anything to
the camera. Kept free of Home Assistant imports so the unit tests can load it.

- reconnects: own LAN sessions that came back after the first one;
- refused handshakes: handshakes the camera refused (914) at its own address,
  which is what a full set of three sessions looks like;
- command latency: from a command's start to the camera's answer;
- push delay: an alarm record's arrival minus its own timestamp. The camera's
  clock may be off (it syncs only through the cloud); a negative or very large
  delay is flagged as clock skew instead of reported.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

# Beyond this an alarm record's delay says more about the camera's clock than
# about the push.
CLOCK_SKEW_SECONDS = 120


@dataclass
class LanMetrics:
    reconnects: int = 0
    last_reconnect: datetime | None = None
    refused_handshakes: int = 0
    command_latency: float | None = None
    command_path: str | None = None
    push_delay: float | None = None
    clock_skew: bool = False

    def note_connected(self, was_up_before: bool, now: float) -> None:
        if was_up_before:
            self.reconnects += 1
            self.last_reconnect = datetime.fromtimestamp(now, UTC)

    def note_refused(self, count: int = 1) -> None:
        self.refused_handshakes += max(0, count)

    def note_command(self, seconds: float, path: str) -> None:
        self.command_latency = round(max(0.0, seconds), 3)
        self.command_path = path

    def note_alarm(self, record_time: float | None, received: float) -> None:
        if record_time is None:
            return
        delay = received - record_time
        if delay < -2 or delay > CLOCK_SKEW_SECONDS:
            self.clock_skew = True
            return
        self.clock_skew = False
        self.push_delay = round(max(0.0, delay), 2)
