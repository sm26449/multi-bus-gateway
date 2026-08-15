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

    __slots__ = ("reset_confirm", "noise", "_last", "_regressions",
                 "_reset_first", "just_reset")

    def __init__(self, reset_confirm: int = 3, noise: float = 0.0):
        # at least 1 confirming read; 1 = accept every downward step immediately
        self.reset_confirm = max(1, int(reset_confirm))
        self.noise = abs(float(noise))
        self._last: Optional[float] = None
        self._regressions = 0
        self._reset_first: Optional[float] = None
        # set for exactly one feed() when a reset was ADOPTED — the caller
        # logs it with register context (an adopted reset used to be the one
        # transition that left no trace at all)
        self.just_reset = False

    def feed(self, value):
        """Return the value to publish, or ``None`` to drop it (hold last)."""
        self.just_reset = False
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
            self._reset_first = None
            return value

        # Below baseline — glitch until proven a reset. A REAL reset produces
        # a COHERENT low sequence: the post-reset counter grows by tiny
        # increments compared to the size of the drop itself, so the SPREAD of
        # the confirming reads must stay small relative to that drop (5%, or
        # the noise band). A burst of unrelated corrupt reads — even an
        # ascending one — spreads on the same order as the drop and never
        # confirms (audit DP-10: three random garbage values could previously
        # be adopted as a baseline, the exact phantom-delta this filter
        # exists to stop).
        if self._regressions == 0 or self._reset_first is None:
            self._reset_first = value
            self._regressions = 1
        else:
            spread = value - self._reset_first
            allowed = max(self.noise, 0.05 * (self._last - self._reset_first))
            if -self.noise <= spread <= allowed:
                self._regressions += 1             # coherent continuation
            else:
                self._reset_first = value          # incoherent → restart the count
                self._regressions = 1
        if self._regressions >= self.reset_confirm:
            self._last = value                     # sustained + coherent → adopt
            self._regressions = 0
            self._reset_first = None
            self.just_reset = True
            return value
        return None                                # transient → drop, keep last-good
