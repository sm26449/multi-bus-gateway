# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Monotonic-counter hygiene for cumulative energy registers.

A cumulative energy counter (Wh/kWh/varh…) only ever grows. A single corrupt
Modbus read that decodes *lower* than the last good value looks to every
downstream consumer — the Home Assistant Energy Dashboard, Victron's own
accounting, an InfluxDB `difference()` — like a **counter reset**, which
silently injects a huge phantom delta and poisons the day's totals.

`MonotonicFilter` sits on the poll path for registers flagged ``monotonic: true``
and lets a *lower* value through only when it is a **genuine** reset (meter
replaced / rebooted), not a one-read glitch:

* value ≥ last accepted  → accept (normal growth; flat is fine).
* value < last, transient → **drop** (hold the last value; the read is treated
  as missing, so the cache keeps serving the last-good counter).
* value < last, sustained for ``reset_confirm`` consecutive reads → accept it as
  a real reset and adopt the new, lower baseline.

The guard is per register and lives on the poller, so it resets (re-seeds on the
first read) whenever the device is reloaded — no persistence, no surprises.
Non-numeric values pass through untouched.
"""
from __future__ import annotations

from typing import Optional


class MonotonicFilter:
    """Stateful reject-on-regression guard for one cumulative counter.

    ``reset_confirm`` consecutive below-baseline reads are required before a
    downward step is believed (a real meter reset), so a lone glitch never
    propagates. ``noise`` absorbs sub-unit float jitter from scale division so a
    counter that is momentarily flat is not mistaken for a regression.
    """

    __slots__ = ("reset_confirm", "noise", "_last", "_regressions")

    def __init__(self, reset_confirm: int = 3, noise: float = 0.0):
        # at least 1 confirming read; 1 = accept every downward step immediately
        self.reset_confirm = max(1, int(reset_confirm))
        self.noise = abs(float(noise))
        self._last: Optional[float] = None
        self._regressions = 0

    def feed(self, value):
        """Return the value to publish, or ``None`` to drop it (hold last)."""
        # bool is an int subclass — exclude it; non-numerics are not counters
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return value

        if self._last is None:                     # first sample seeds the baseline
            self._last = value
            return value

        if value >= self._last - self.noise:       # grew, flat, or within noise
            if value > self._last:
                self._last = value                 # never let the baseline slip down
            self._regressions = 0
            return value

        # value is genuinely below the baseline — glitch until proven a reset
        self._regressions += 1
        if self._regressions >= self.reset_confirm:
            self._last = value                     # sustained → real reset, adopt it
            self._regressions = 0
            return value
        return None                                # transient → drop, keep last-good
