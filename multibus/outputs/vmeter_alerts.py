# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Virtual-meter alerts (VM-ALERT-1).

On 2026-11-11 the primary meter delivered nothing for 25 minutes, the virtual
meter the Victron GX reads stopped answering after its hold window, and nothing
alerted — the harvester only copied the meter's last error into the event log.

Three conditions per running virtual meter, each fired once when it has lasted
its limit and cleared once when it ends (same key, ``transition=True`` — the
bridge-health pattern: the edge is the event, the hold-off is the limit):

* ``not_serving`` — consumers get no live answer (server stopped, or nothing
  fresh nor held left) for ``alerts.vmeter.not_serving_s`` (default 30 s);
* ``held`` — at least one row is served from hold for ``held_s`` (20 s);
* ``consumer`` — a meter that had been polled got no request for
  ``consumer_idle_s`` (120 s). Not judged while the meter is not serving (that
  alert already says why the consumer is gone); a meter never polled is not
  judged at all (nobody is wired to it yet).

A limit of 0 switches that condition off. ``alerts.signals.vmeter`` (default on)
switches all three. Pure but for the state it keeps; the clock is the caller's.
"""
from __future__ import annotations

from typing import Callable, Dict, Iterable, Optional, Set, Tuple

DEFAULT_VMETER_THRESHOLDS = {"not_serving_s": 30.0, "held_s": 20.0, "consumer_idle_s": 120.0}
THRESHOLD_KEYS = tuple(DEFAULT_VMETER_THRESHOLDS)

# condition → (severity when it fires, message, message when it clears)
_CONDS = {
    "not_serving": ("error",
                    "not serving — its consumers get no live answer for {age:.0f} s "
                    "(source stale beyond the hold)",
                    "serving again"),
    "held": ("warn",
             "serving HELD values for {age:.0f} s (its source is stale)",
             "serving live values again"),
    "consumer": ("warn",
                 "no request from its consumer for {age:.0f} s — the consumer "
                 "stopped polling (lost its meter?)",
                 "its consumer is polling again"),
}


def thresholds_from(raw: Optional[dict]) -> Dict[str, float]:
    """The effective limits: defaults overlaid with whatever parses (>= 0)."""
    out = dict(DEFAULT_VMETER_THRESHOLDS)
    for k, v in (raw or {}).items() if isinstance(raw, dict) else ():
        if k in out:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f >= 0 and not isinstance(v, bool):
                out[k] = f
    return out


def overrides_from(raw: Optional[dict]) -> Dict[str, float]:
    """A virtual meter's own ``alerts:`` limits, laid over the global
    ``alerts.vmeter`` for that meter alone (e.g. ``consumer_idle_s: 0`` for a
    consumer that sleeps at night). Only the keys it names and that parse."""
    if not isinstance(raw, dict):
        return {}
    full = thresholds_from(raw)
    return {k: full[k] for k in raw if k in full and not validate_thresholds({k: raw[k]})}


def validate_thresholds(raw, where: str = "vmeter") -> list:
    """Errors in words for an ``alerts.vmeter`` mapping."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [f"{where}: a mapping of {', '.join(THRESHOLD_KEYS)}"]
    errs = []
    for k, v in raw.items():
        if k not in THRESHOLD_KEYS:
            errs.append(f"{where}.{k}: unknown (one of {', '.join(THRESHOLD_KEYS)})")
            continue
        try:
            if isinstance(v, bool) or float(v) < 0:
                raise ValueError
        except (TypeError, ValueError):
            errs.append(f"{where}.{k}: seconds, 0 or more (0 = off)")
    return errs


class VMeterAlertTracker:
    """Edge state for the three conditions of every virtual meter."""

    def __init__(self, fire: Callable[..., None]):
        self._fire = fire
        self._since: Dict[Tuple[str, str], float] = {}
        self._alerted: Set[Tuple[str, str]] = set()

    def update(self, sample: dict, thresholds: Dict[str, float], now: float) -> None:
        """Judge one meter's sample (``alert_samples()`` row) at monotonic ``now``."""
        vid = str(sample.get("id") or "")
        if not vid:
            return
        name = f"vMeter {sample.get('name') or vid}"
        mode = sample.get("serving")
        idle = sample.get("idle_s")
        conds: Dict[str, Tuple[bool, float]] = {
            "not_serving": (mode == "not_serving", thresholds["not_serving_s"]),
            "held": (mode == "held", thresholds["held_s"]),
        }
        if mode != "not_serving":
            lim = thresholds["consumer_idle_s"]
            # idle already measures the duration: no second hold-off
            conds["consumer"] = (bool(lim) and idle is not None and idle > lim, 0.0)
        for cond, (on, limit) in conds.items():
            key = (vid, cond)
            if cond != "consumer" and not limit:
                on = False                            # switched off (limit 0)
            if on:
                since = self._since.setdefault(key, now)
                if key not in self._alerted and now - since >= limit:
                    self._alerted.add(key)
                    sev, msg, _ok = _CONDS[cond]
                    age = idle if cond == "consumer" else now - since
                    self._fire(sev, f"vmeter:{vid}:{cond}", name, msg.format(age=age or 0.0),
                               transition=True)
            else:
                self._since.pop(key, None)
                if key in self._alerted:
                    self._alerted.discard(key)
                    self._fire("info", f"vmeter:{vid}:{cond}", name, _CONDS[cond][2],
                               transition=True)

    def retain(self, ids: Iterable[str]) -> None:
        """Forget meters that are gone (removed/disabled): no orphan clear."""
        keep = set(ids)
        for key in [k for k in set(self._since) | self._alerted if k[0] not in keep]:
            self._since.pop(key, None)
            self._alerted.discard(key)

    def reset(self) -> None:
        """Signal off / standby: forget everything (becoming active starts clean)."""
        self._since.clear()
        self._alerted.clear()
