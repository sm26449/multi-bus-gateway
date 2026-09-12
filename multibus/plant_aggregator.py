# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Plant-level aggregates — a plant is a real entity, so it publishes its own
output like any device.

For every enabled plant, its units' values are combined by canonical-name rule
and published:

- MQTT under ``mbg/plants/<plant_id>/<canonical topic>`` (the entity-typed
  namespace next to ``mbg/devices/…``), plus ``units_online`` / ``units_total``
  / ``status``;
- InfluxDB into the plant's bucket, SAME canonical measurements/fields as the
  units, tagged ``device=<plant_id>`` + ``aggregate=plant`` — a dashboard reads
  plant totals exactly like it reads a device, filtered by tag.

Three rules, because three kinds of quantity behave differently:

* **instantaneous SUM** — ``power_*`` (bar power factor) and ``current_*``.
  Freshness-gated: a unit contributes only values younger than 4× their poll
  cadence (60 s floor). A stalled unit silently drops out instead of freezing
  the total; its production genuinely IS unknown, and ``units_online`` says how
  many are contributing so a consumer can tell a partial total apart.
* **instantaneous AVG** — ``voltage_*``, ``frequency``, ``temperature_*``. Same
  freshness gate.
* **COUNTERS** — ``energy_*``. Last-known value at ANY age, and published only
  when EVERY expected unit has one. A sleeping inverter still holds its lifetime
  energy: dropping it from the sum makes the plant counter jump BACKWARDS, which
  poisons every ``increase()`` / derivative downstream (an evening of 4 units
  reporting 178 MWh became 113 MWh the moment three of them went dark). An
  incomplete sum is a lie, so a missing unit withholds the leaf entirely and the
  retained topic keeps the last COMPLETE total.

``power_factor_total`` is never an average of ratios — averaging would weight a
1 kW inverter like a 20 kW one. It is derived as Σ active / Σ apparent, the only
definition that survives units of different size. (Until the Fronius templates
normalize PF to a fraction, a unit's own ``power_factor/total`` is still the raw
SunSpec ±100 while the plant's is a true ±1 — see
``docs/fronius-migration-plan.md``, phase P3.)

``units_online`` / ``units_total`` / ``status`` publish on EVERY cycle,
including the one where nothing is fresh: a plant that never says ``offline``
leaves its consumers unable to tell a dark plant from a dead gateway.

Opt-out per plant with ``aggregates: false`` in the ``plants:`` entry.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from .canonical_fields import mqtt_topic_for

logger = logging.getLogger(__name__)

# Monotonic counters — combined from last-known values, never freshness-gated.
_COUNTER_PREFIXES = ("energy_",)
_SUM_PREFIXES = ("power_", "current_")
_AVG_PREFIXES = ("voltage_", "frequency", "temperature_")
# Names that would be nonsense to combine: identity strings, status codes, event
# bitfields, plumbing — plus power factor, which is DERIVED from the summed
# powers instead (a ratio is neither summable nor meaningfully averageable).
_SKIP_PREFIXES = ("operating_state", "vendor_state", "event_", "vendor_event_",
                  "manufacturer", "model", "serial", "mppt_modules",
                  "power_factor")

# Census/status keys — not measurements: they bypass the canonical topic map and
# (for the text one) the InfluxDB path.
_META = ("units_online", "units_total", "status")


def _rule_for(name: str) -> Optional[str]:
    """``"sum"`` | ``"avg"`` | ``"counter"`` | None (do not aggregate)."""
    n = name.lower()
    # SKIP first: power_factor_* is more specific than the power_* SUM prefix
    if n.startswith(_SKIP_PREFIXES):
        return None
    if n.startswith(_COUNTER_PREFIXES):
        return "counter"
    if n.startswith(_AVG_PREFIXES):
        return "avg"
    if n.startswith(_SUM_PREFIXES):
        return "sum"
    return None


def aggregation_rule(name: str) -> Optional[str]:
    """How a canonical field combines across a plant's units: ``sum`` | ``avg``
    | ``counter`` | None (not aggregated). Public because the history picker
    needs to know which of a unit's series a plant actually republishes."""
    return _rule_for(name)


def _derive_power_factor(out: Dict[str, Any]) -> Optional[float]:
    """A plant's power factor is Σ active / Σ apparent. None when the plant has
    no apparent power (register not selected) or stands still (Σ apparent 0)."""
    p, s = out.get("power_active_total"), out.get("power_apparent_total")
    if not isinstance(p, (int, float)) or isinstance(p, bool):
        return None
    if not isinstance(s, (int, float)) or isinstance(s, bool) or not s:
        return None
    return round(max(-1.0, min(1.0, p / s)), 4)


def compute_plant_aggregates(config, registry, plant_id: str,
                             now: Optional[float] = None) -> Dict[str, Any]:
    """Pure computation: ``{name: value}`` for one plant from its units' live
    stores, plus ``units_online`` / ``units_total`` / ``status``. Shared by the
    publisher thread and the ``/api/plants`` endpoint.

    ``units_total`` counts the units EXPECTED to contribute (the enabled ones) —
    the denominator of "how many are producing", not the configuration census
    the UI lists. A disabled unit is excluded everywhere: it must not hold the
    plant's counters hostage, nor make ``status`` unreachable from ``online``.
    """
    now = now if now is not None else time.time()
    expected = [d for d in config.plant_devices(plant_id)
                if getattr(d, "enabled", True)]
    fresh: Dict[str, List[float]] = {}
    counters: Dict[str, List[float]] = {}
    online = 0
    for dev in expected:
        store = registry.store_for(dev.id) or {}
        fresh_any = False
        for entry in list(store.values()):
            name = entry.get("name") or ""
            val = entry.get("value")
            ts = entry.get("ts")
            if not name or not isinstance(val, (int, float)) or isinstance(val, bool):
                continue
            interval = entry.get("interval") or 30
            is_fresh = (ts is not None
                        and (now - ts) <= max(4 * float(interval), 60.0))
            fresh_any = fresh_any or is_fresh
            rule = _rule_for(name)
            if rule == "counter":
                counters.setdefault(name, []).append(float(val))
            elif rule and is_fresh:
                fresh.setdefault(name, []).append(float(val))
        if fresh_any:
            online += 1

    out: Dict[str, Any] = {}
    for name, vals in fresh.items():
        out[name] = round(sum(vals) if _rule_for(name) == "sum"
                          else sum(vals) / len(vals), 3)
    n_expected = len(expected)
    for name, vals in counters.items():
        if n_expected and len(vals) == n_expected:
            out[name] = round(sum(vals), 3)
        # else: withheld on purpose — see the module docstring
    pf = _derive_power_factor(out)
    if pf is not None:
        out["power_factor_total"] = pf
    out["units_online"] = online
    out["units_total"] = n_expected
    if expected:
        out["status"] = ("online" if online == n_expected else
                         "offline" if online == 0 else "partial")
    return out


def plant_bucket(plant: Dict, plant_id: str) -> Optional[str]:
    """The InfluxDB bucket for a plant's OWN points. The plant's units resolve
    ``${unit_id}`` / ``${device_id}`` per unit; the aggregate belongs to no unit,
    so every placeholder resolves to the plant itself — a legal, findable bucket
    name instead of a literal ``fronius_${unit_id}`` on the wire."""
    bucket = (plant.get("influxdb") or {}).get("bucket") or None
    if not bucket:
        return None
    return (str(bucket).replace("${plant_id}", plant_id)
                       .replace("${device_id}", plant_id)
                       .replace("${unit_id}", plant_id))


class PlantAggregator(threading.Thread):
    """Periodic publisher of plant aggregates (daemon thread, one per app)."""

    def __init__(self, config, registry, get_mqtt, get_influx,
                 interval_s: float = 10.0):
        super().__init__(daemon=True, name="Plant-Aggregator")
        self._config = config
        self._registry = registry
        self._get_mqtt = get_mqtt
        self._get_influx = get_influx
        self._interval = interval_s
        self._stop = threading.Event()
        # per-plant {name: last written value} — InfluxDB honours the same
        # change-detection contract as every other sink, so a plant standing
        # still overnight does not write 8 640 identical points per field
        self._influx_last: Dict[str, Dict[str, Any]] = {}

    def stop(self):
        self._stop.set()

    def run(self):
        logger.info("plant aggregator started (interval %.0fs)", self._interval)
        # small startup delay so the first cycle sees warmed-up stores
        self._stop.wait(self._interval)
        n = 0
        while not self._stop.is_set():
            try:
                self._publish_all(log=(n % 60 == 0))
            except Exception as e:  # noqa: BLE001 — the loop must survive anything
                logger.warning("plant aggregator cycle failed: %s", e, exc_info=True)
            n += 1
            self._stop.wait(self._interval)

    def _publish_all(self, log: bool = False):
        for p in self._config.plants:
            pid = p.get("id")
            if not pid or not bool(p.get("enabled", True)):
                continue
            if not bool(p.get("aggregates", True)):
                continue
            agg = compute_plant_aggregates(self._config, self._registry, pid)
            if log:
                fields = sum(1 for k in agg if k not in _META)
                energy = sum(1 for k in agg if k.startswith("energy_"))
                logger.info("plant %s aggregates: %s online=%s/%s fields=%d "
                            "energy_leaves=%d (power_active_total=%s)", pid,
                            agg.get("status", "—"), agg.get("units_online"),
                            agg.get("units_total"), fields, energy,
                            agg.get("power_active_total"))
            # Publishes UNCONDITIONALLY, including a cycle with nothing fresh:
            # that cycle still carries status=offline and units_online=0, which
            # is precisely the information a consumer needs at nightfall.
            self._publish_mqtt(pid, agg)
            self._publish_influx(p, pid, agg)

    def _publish_mqtt(self, pid: str, agg: Dict[str, Any]):
        mqtt = self._get_mqtt()
        if mqtt is None or not getattr(mqtt, "connected", False):
            return
        base = f"mbg/plants/{pid}"
        for name, val in agg.items():
            leaf = name if name in _META else (mqtt_topic_for(name) or name)
            # publish_if_changed keeps the change-detection semantics every
            # other topic has (heartbeat republish included)
            mqtt.publish_if_changed(f"{base}/{leaf}", val)

    def _publish_influx(self, plant: Dict, pid: str, agg: Dict[str, Any]):
        influx = self._get_influx()
        if influx is None or not getattr(influx, "connected", False):
            return
        from .canonical_fields import measurement_for
        try:
            from influxdb_client import Point, WritePrecision
        except Exception:  # noqa: BLE001 — influx client optional in tests
            return
        bucket = plant_bucket(plant, pid)
        changed_only = getattr(influx, "publish_mode", "changed") == "changed"
        last = self._influx_last.setdefault(pid, {})
        ts = time.time()
        for name, val in agg.items():
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                continue                     # status text is MQTT-only
            if changed_only and last.get(name) == val:
                continue
            meas = "plant" if name in _META else (measurement_for(name) or "plant")
            point = (Point(meas)
                     .tag("device", pid).tag("aggregate", "plant")
                     .field(name, float(val))
                     .time(int(ts * 1e9), WritePrecision.NS))
            influx.write_point(point, ts=ts, bucket=bucket)
            last[name] = val
