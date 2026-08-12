# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Per-value threshold engine — turns a register's *visual* thresholds
(``dangerLow/warningLow/warningHigh/dangerHigh``, the same ones that colour the
dashboard) into alert **events** with hysteresis, so a value crossing a limit
notifies through the existing :mod:`multibus.alerts` path (and onward to alertd
for Telegram/SMS delivery).

Design goals (why it looks like this):

* **Fast to alarm, slow to clear.** Escalation uses the raw threshold so a
  genuine over-limit is reported immediately; de-escalation toward *normal*
  requires the value to retreat past the boundary by a deadband. This is the
  standard safety-alerting hysteresis and it kills boundary flapping.
* **One band per value.** A value sits in exactly one of five bands, so a
  higher severity inherently suppresses the lower one — no double-firing.
* **Fire only on transitions.** The engine holds the current band per key and
  emits an event only when the band changes; steady state is silent. The
  downstream :meth:`AlertManager.fire` still rate-limits per key as a backstop.

The engine is pure (no I/O, no clock): it takes a value + thresholds and returns
an optional event dict. That makes the state machine exhaustively unit-testable
and keeps it off the poll hot path — the caller (the 5 s event harvester) does
the wiring.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# bands ordered from the low extreme to the high extreme
_BAND_SEVERITY = {
    "danger_low": "error",
    "warning_low": "warn",
    "normal": "info",
    "warning_high": "warn",
    "danger_high": "error",
}


def _num(x: Any) -> Optional[float]:
    """Coerce a threshold/value to float, or None if absent/non-numeric."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


class ThresholdEngine:
    """Stateful band tracker across many register keys.

    ``deadband_pct`` is the clear-side hysteresis as a percent of the boundary
    value (2.0 → a high limit of 253 must fall back below 253 − 5.06 to clear).
    ``alert_on_start`` fires once for a key whose very first evaluation is
    already in an alarm band, so a condition that was true across a restart is
    still surfaced (default on).
    """

    def __init__(self, deadband_pct: float = 2.0, alert_on_start: bool = True):
        self.deadband_pct = max(0.0, float(deadband_pct))
        self.alert_on_start = bool(alert_on_start)
        self._band: Dict[str, str] = {}

    # ── band classification with directional hysteresis ──────────────────────
    def _deadband(self, boundary: float) -> float:
        return abs(boundary) * self.deadband_pct / 100.0

    def _classify(self, v: float, dl, wl, wh, dh, cur: str) -> str:
        """Return the band for value ``v`` given the four bounds and the current
        band ``cur`` (so de-escalation can be made sticky). Any bound may be
        None. Escalation is immediate; clearing requires crossing back by the
        deadband."""
        # ── high side: escalate immediately ──
        if dh is not None and v > dh:
            return "danger_high"
        if wh is not None and v > wh:
            # already in danger_high? stay until we drop a deadband below dh
            if cur == "danger_high" and dh is not None and v > dh - self._deadband(dh):
                return "danger_high"
            return "warning_high"
        # ── low side: escalate immediately ──
        if dl is not None and v < dl:
            return "danger_low"
        if wl is not None and v < wl:
            if cur == "danger_low" and dl is not None and v < dl + self._deadband(dl):
                return "danger_low"
            return "warning_low"
        # ── in the normal span: clear only past the boundary + deadband ──
        if cur in ("warning_high", "danger_high") and wh is not None \
                and v > wh - self._deadband(wh):
            return "warning_high"        # not yet cleared on the high side
        if cur in ("warning_low", "danger_low") and wl is not None \
                and v < wl + self._deadband(wl):
            return "warning_low"         # not yet cleared on the low side
        return "normal"

    # ── evaluation ───────────────────────────────────────────────────────────
    def evaluate(self, key: str, value: Any, thresholds: Optional[Dict],
                 source: str = "", label: str = "", unit: str = "") -> Optional[Dict]:
        """Feed one register reading. Returns an alert event dict
        ``{severity, key, source, message, band}`` when the band changes, else
        None. A disabled/empty threshold set is ignored (and clears any tracked
        state so it can't get stuck)."""
        t = thresholds or {}
        v = _num(value)
        if not t or not t.get("enabled") or v is None:
            self._band.pop(key, None)
            return None
        dl, wl = _num(t.get("dangerLow")), _num(t.get("warningLow"))
        wh, dh = _num(t.get("warningHigh")), _num(t.get("dangerHigh"))
        if dl is None and wl is None and wh is None and dh is None:
            self._band.pop(key, None)
            return None

        first = key not in self._band
        cur = self._band.get(key, "normal")
        new = self._classify(v, dl, wl, wh, dh, cur)
        self._band[key] = new

        if first:
            # baseline: only speak up if we boot straight into an alarm
            if new == "normal" or not self.alert_on_start:
                return None
        elif new == cur:
            return None                  # steady state — silent

        return self._event(key, new, v, source, label, unit, dl, wl, wh, dh)

    def _event(self, key, band, v, source, label, unit, dl, wl, wh, dh) -> Dict:
        name = label or key
        u = f" {unit}" if unit else ""
        if band == "normal":
            msg = f"{name} = {v:g}{u} — back to normal"
        else:
            bound = {"danger_high": dh, "warning_high": wh,
                     "warning_low": wl, "danger_low": dl}[band]
            side = "above" if band.endswith("high") else "below"
            lvl = "danger" if band.startswith("danger") else "warning"
            msg = f"{name} = {v:g}{u} — {side} {lvl} limit {bound:g}{u}"
        return {"severity": _BAND_SEVERITY[band], "key": key,
                "source": source, "message": msg, "band": band}

    def forget(self, key: str) -> None:
        """Drop tracked state for a key (register removed / device deleted)."""
        self._band.pop(key, None)

    def retain(self, keep: set) -> None:
        """Drop every tracked key not in ``keep`` (config reload prunes gone
        registers so their band state can't leak)."""
        for k in [k for k in self._band if k not in keep]:
            self._band.pop(k, None)
