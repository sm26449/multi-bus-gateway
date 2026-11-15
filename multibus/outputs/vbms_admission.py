# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Virtual BMS — which packs a bank is allowed to count on, and how fast its limits may
rise (design §4.1, §4.6, §4.9; R2D-VBMS-3).

Three pure pieces, each with an injected ``now`` so they are tested without a clock:

* ``PackAdmission`` — the per-pack return state machine. A drop is immediate; a pack that
  dropped while this process ran comes back only after ``return_after_s`` of eligibility
  without a break; ``flap_drops`` drops inside ``flap_window_s`` hold it out until an
  operator clears it. A pack never seen since the instance was built is admitted on its
  first eligible sample (no history to distrust — design §4.1 "after a gateway start");
  the bank then enters through the ramp below.
* ``derate`` — the current limits a bank of the ADMITTED packs can carry: from the per-pack
  capability when it is known (``pack_limits`` and/or a live per-pack reading), otherwise
  the configured bank limit scaled by admitted / configured. It only ever lowers a limit.
* ``LimitRamp`` — an increase of CCL/DCL slews at ``slew_a_per_s`` from 0 after a (re)start,
  a re-arm or a return from a data failure; a decrease is immediate.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Optional

# defaults are the design's (§3.3 policy); every one may only be tightened by validation
RECOVERY_DEFAULTS = {"return_after_s": 60.0, "flap_drops": 3, "flap_window_s": 600.0,
                     "slew_a_per_s": 2.0}
SHARE_FACTOR_DEFAULT = 0.9

# the bank's current limits, and the per-pack reading key (via ``field_map``) for each
CURRENT_LIMITS = (("charge_current_limit", "ccl"), ("discharge_current_limit", "dcl"))

NEW, IN, OUT, RETURNING, HELD = "new", "in", "out", "returning", "held"


class PackAdmission:
    """The per-pack return state machine (design §4.1).

    ``new`` (never seen) → ``in`` on the first eligible sample.
    ``in`` → ``out`` the moment a sample is not eligible (a drop, counted).
    ``out`` → ``returning`` on an eligible sample; ``returning`` → ``in`` once it has stayed
    eligible for ``return_after_s``; a break sends it back to ``out`` (counted as a drop).
    ``flap_drops`` drops inside ``flap_window_s`` → ``held`` until ``clear`` (→ ``out``).

    "Never seen" means never seen within the START GRACE: a pack first eligible more than
    ``grace_s`` (= ``return_after_s``) after ``started`` is a pack that was dead at the start
    and is treated as a return. The grace is ``return_after_s`` because that is the delay the
    operator already accepts for a return; a shorter one (a few periods) would turn every
    start with a slow first poll (V2 at 5 s, design §4.1 budget ≤ 40 s) into a return.
    The drops and the held flag survive a rebuild through ``export`` / ``restore``.
    """

    def __init__(self, packs: Iterable[str], *, return_after_s: float, flap_drops: int,
                 flap_window_s: float):
        self.packs: List[str] = list(packs)
        self.return_after_s = float(return_after_s)
        self.flap_drops = int(flap_drops)
        self.flap_window_s = float(flap_window_s)
        self.state: Dict[str, str] = {p: NEW for p in self.packs}
        self._since: Dict[str, float] = {}
        self._drops: Dict[str, Deque[float]] = {p: deque(maxlen=max(1, self.flap_drops))
                                                 for p in self.packs}
        self.started: Optional[float] = None      # set at build; else the first observation
        self.grace_s = self.return_after_s
        self.dirty = False                        # a drop, a hold or a clear not yet persisted

    def _drop(self, pack: str, now: float) -> None:
        drops = self._drops[pack]
        drops.append(now)
        while drops and now - drops[0] > self.flap_window_s:
            drops.popleft()
        self._since.pop(pack, None)
        self.state[pack] = HELD if len(drops) >= self.flap_drops else OUT
        self.dirty = True

    def observe(self, eligible: Iterable[str], now: float) -> List[str]:
        """Advance every pack on one observation; return the admitted packs (config order)."""
        ok = set(eligible)
        if self.started is None:
            self.started = now
        for p in self.packs:
            st = self.state[p]
            if st == HELD:
                continue
            if p not in ok:
                if st in (IN, RETURNING):
                    self._drop(p, now)
                continue
            if st == NEW and now - self.started <= self.grace_s:
                self.state[p] = IN
            elif st in (NEW, OUT):
                self.state[p] = RETURNING
                self._since[p] = now
            if self.state[p] == RETURNING and now - self._since[p] >= self.return_after_s:
                self.state[p] = IN
                self._since.pop(p, None)
        return [p for p in self.packs if self.state[p] == IN]

    def clear(self, pack: str) -> bool:
        """An operator readmits a pack held out for flapping. It still has to serve its
        ``return_after_s`` and comes back through the ramp. Returns whether it was held."""
        if self.state.get(pack) != HELD:
            return False
        self._drops[pack].clear()
        self.state[pack] = OUT
        self.dirty = True
        return True

    def export(self, now: float, wall: float) -> Dict[str, Dict[str, Any]]:
        """The state worth keeping across a rebuild: each pack's recent drops (wall-clock
        seconds) and whether it is held. Packs with neither are left out (bounded)."""
        out: Dict[str, Dict[str, Any]] = {}
        for p in self.packs:
            drops = [round(wall - (now - d), 3) for d in self._drops[p]
                     if now - d <= self.flap_window_s]
            if drops or self.state[p] == HELD:
                out[p] = {"held": self.state[p] == HELD, "drops": drops}
        return out

    def restore(self, saved: Any, now: float, wall: float) -> None:
        """Take back what ``export`` kept. Anything malformed is ignored, pack by pack —
        a damaged file never raises and never holds a pack back by itself."""
        if not isinstance(saved, dict):
            return
        for p in self.packs:
            row = saved.get(p)
            if not isinstance(row, dict):
                continue
            drops = row.get("drops") if isinstance(row.get("drops"), list) else []
            for w in drops:
                if isinstance(w, (int, float)) and not isinstance(w, bool) and math.isfinite(w):
                    age = wall - w
                    if 0 <= age <= self.flap_window_s:
                        self._drops[p].append(now - age)
            if row.get("held") is True:
                self.state[p] = HELD

    def snapshot(self, now: float) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for p in self.packs:
            st = self.state[p]
            row: Dict[str, Any] = {"state": st, "drops": len(self._drops[p])}
            if st == RETURNING:
                row["return_in_s"] = round(max(0.0, self.return_after_s - (now - self._since[p])), 1)
            out[p] = row
        return out


def _reading(x: Any) -> Optional[float]:
    """A per-pack limit reading as a number. Present but not a finite, non-negative number
    reads as 0 — a value we cannot trust never raises a limit."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) and v >= 0 else 0.0


def _down(a: float) -> float:
    """Round DOWN to 0.1 A — the wire's usual resolution — so rounding never adds current."""
    return math.floor(a * 10 + 1e-9) / 10


def derate(control: Dict[str, Any], admitted: List[Dict[str, Any]], n_configured: int,
           pack_limits: Dict[str, Any]) -> Dict[str, Any]:
    """Cap the resolved CCL/DCL to what the ADMITTED packs can carry (design §4.6, static).

    Per pack i and side: ``L_i = min(live reading_i, pack_limits[side])`` (whichever exist).
    When every admitted pack has an ``L_i``: ``cap = min(n × min_i L_i × k, Σ L_i)``.
    Otherwise the per-pack capability is unknown and the configured bank limit is scaled by
    ``n / N`` — the safe reading of a bank limit set for N packs. The result is
    ``min(resolved, cap)``: never higher than what was resolved. Mutates and returns
    ``control``."""
    n = len(admitted)
    k = float(pack_limits.get("share_factor", SHARE_FACTOR_DEFAULT))
    for field, key in CURRENT_LIMITS:
        target = control.get(field)
        if target is None:
            continue
        per: List[Optional[float]] = []
        for p in admitted:
            vals = [v for v in (_reading(p.get(key)), _reading(pack_limits.get(field)))
                    if v is not None]
            per.append(min(vals) if vals else None)
        known = [v for v in per if v is not None]
        if n and len(known) == n:
            cap = min(n * min(known) * k, sum(known))
        else:
            cap = float(target) * n / max(1, n_configured)
            if known:                         # what IS known still bounds it: a known 0 is 0
                cap = min(cap, n * min(known) * k)
        if cap < float(target):                 # only ever lower; an untouched value keeps its type
            control[field] = _down(max(0.0, cap))
    return control


class LimitRamp:
    """Slew-limit increases of the current limits; decreases pass at once (design §4.9)."""

    def __init__(self, slew_a_per_s: float, max_dt_s: float):
        self.slew = float(slew_a_per_s)
        self.max_dt_s = float(max_dt_s)
        self._last: Dict[str, float] = {}
        self._t: Optional[float] = None

    def reset(self) -> None:
        """The next ``apply`` starts from 0 (a restart, a re-arm, a return from failure)."""
        self._last.clear()
        self._t = None

    def apply(self, values: Dict[str, Any], now: float) -> Dict[str, Any]:
        """The limits to send now; remembers them. The elapsed time is capped at
        ``max_dt_s`` so a gap between calls is never a jump."""
        dt = 0.0 if self._t is None else min(max(0.0, now - self._t), self.max_dt_s)
        out = dict(values)
        for field, _key in CURRENT_LIMITS:
            target = out.get(field)
            if target is None:
                continue
            prev = self._last.get(field)
            sent = 0.0 if prev is None else prev + self.slew * dt
            if sent < float(target):
                out[field] = _down(sent)
            self._last[field] = float(out[field])
        self._t = now
        return out

    def view(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """``values`` with the limits no higher than the last ones sent — for telemetry;
        does not advance the ramp. Without a ramp in progress it is ``values``."""
        if not self._last:
            return values
        out = dict(values)
        for field, _key in CURRENT_LIMITS:
            if out.get(field) is not None and self._last.get(field, math.inf) < float(out[field]):
                out[field] = self._last[field]
        return out


def flag_bits(value: Any, bits: int) -> int:
    """An alarm/protection bit field as an int — FAIL SAFE: a value that is not a finite,
    non-negative whole number reads as every bit set (an alarm we cannot decode is an
    alarm), masked to the field's ``bits`` so it always fits the wire. Never raises."""
    full = (1 << bits) - 1
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value if 0 <= value <= full else full
    if isinstance(value, float) and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value) if value <= full else full
    return full
