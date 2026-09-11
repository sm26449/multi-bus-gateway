# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Plant-level aggregates — a plant is a real entity, so it publishes its own
output like any device.

For every enabled plant, the fresh values of its units are combined by
canonical-name rules (powers/currents/energies SUM; voltages/frequency/
power-factor/temperatures AVERAGE) and published:

- MQTT under ``mbg/plants/<plant_id>/<canonical topic>`` (the entity-typed
  namespace next to ``mbg/devices/…``), plus ``units_online``/``units_total``;
- InfluxDB into the plant's bucket, SAME canonical measurements/fields as the
  units, tagged ``device=<plant_id>`` + ``aggregate=plant`` — a dashboard
  reads plant totals exactly like it reads a device, filtered by tag.

Freshness: a unit contributes only values younger than 4× their poll cadence
(minimum 60 s) — a stalled unit silently drops out of the aggregate instead
of freezing it (its production IS unknown), and ``units_online`` says how
many are contributing, so a consumer can tell a partial total apart.

Opt-out per plant with ``aggregates: false`` in the ``plants:`` entry.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from .canonical_fields import mqtt_topic_for

logger = logging.getLogger(__name__)

_SUM_PREFIXES = ("power_", "current_", "energy_")
_AVG_PREFIXES = ("voltage_", "frequency", "power_factor_", "temperature_")
# names that would be nonsense to combine (identity strings, status codes,
# event bitfields, plumbing)
_SKIP_PREFIXES = ("operating_state", "vendor_state", "event_", "vendor_event_",
                  "manufacturer", "model", "serial", "mppt_modules")


def _rule_for(name: str) -> Optional[str]:
    n = name.lower()
    if n.startswith(_SKIP_PREFIXES):
        return None
    # AVG first: power_factor_* is more specific than the power_* SUM prefix
    if n.startswith(_AVG_PREFIXES) or n == "frequency":
        return "avg"
    if n.startswith(_SUM_PREFIXES):
        return "sum"
    return None


def compute_plant_aggregates(config, registry, plant_id: str,
                             now: Optional[float] = None) -> Dict[str, Any]:
    """Pure computation: {name: value} for one plant from its units' live
    stores, plus ``units_online``/``units_total``. Shared by the publisher
    thread and the /api/plants endpoint."""
    now = now if now is not None else time.time()
    units = config.plant_devices(plant_id)
    per_name: Dict[str, list] = {}
    online = 0
    for dev in units:
        store = registry.store_for(dev.id) or {}
        fresh_any = False
        for entry in list(store.values()):
            name = entry.get("name") or ""
            val = entry.get("value")
            ts = entry.get("ts")
            if not name or not isinstance(val, (int, float)) or isinstance(val, bool):
                continue
            interval = entry.get("interval") or 30
            if ts is None or (now - ts) > max(4 * float(interval), 60.0):
                continue
            fresh_any = True
            rule = _rule_for(name)
            if rule:
                per_name.setdefault(name, []).append(float(val))
        if fresh_any:
            online += 1
    out: Dict[str, Any] = {}
    for name, vals in per_name.items():
        rule = _rule_for(name)
        agg = sum(vals) if rule == "sum" else sum(vals) / len(vals)
        out[name] = round(agg, 3)
    out["units_online"] = online
    out["units_total"] = len(units)
    return out


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
                logger.info("plant %s aggregates: online=%s/%s fields=%d "
                            "(power_active_total=%s)", pid,
                            agg.get("units_online"), agg.get("units_total"),
                            len(agg) - 2, agg.get("power_active_total"))
            if agg.get("units_online", 0) == 0:
                continue                       # nothing fresh — publish nothing
            self._publish_mqtt(pid, agg)
            self._publish_influx(p, pid, agg)

    def _publish_mqtt(self, pid: str, agg: Dict[str, Any]):
        mqtt = self._get_mqtt()
        if mqtt is None or not getattr(mqtt, "connected", False):
            return
        base = f"mbg/plants/{pid}"
        for name, val in agg.items():
            leaf = (mqtt_topic_for(name) or name) if name not in (
                "units_online", "units_total") else name
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
        bucket = (plant.get("influxdb") or {}).get("bucket") or None
        ts = time.time()
        for name, val in agg.items():
            if name in ("units_online", "units_total"):
                meas = "plant"
            else:
                meas = measurement_for(name) or "plant"
            point = (Point(meas)
                     .tag("device", pid).tag("aggregate", "plant")
                     .field(name, float(val))
                     .time(int(ts * 1e9), WritePrecision.NS))
            influx.write_point(point, ts=ts, bucket=bucket)
