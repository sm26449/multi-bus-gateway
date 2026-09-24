# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Endpoint-level aggregates — an endpoint is a real entity, so it publishes its own
output like any device.

For every enabled endpoint, its units' values are combined by canonical-name rule
and published:

- MQTT under ``mbg/endpoints/<endpoint_id>/<canonical topic>`` (the entity-typed
  namespace next to ``mbg/devices/…``), plus ``units_online`` / ``units_total``
  / ``status``;
- InfluxDB into the endpoint's bucket, SAME canonical measurements/fields as the
  units, tagged ``device=<endpoint_id>`` + ``aggregate=endpoint`` — a dashboard reads
  endpoint totals exactly like it reads a device, filtered by tag.

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
  energy: dropping it from the sum makes the endpoint counter jump BACKWARDS, which
  poisons every ``increase()`` / derivative downstream (an evening of 4 units
  reporting 178 MWh became 113 MWh the moment three of them went dark). An
  incomplete sum is a lie, so a missing unit withholds the leaf entirely and the
  retained topic keeps the last COMPLETE total.

``power_factor_total`` is never an average of ratios — averaging would weight a
1 kW inverter like a 20 kW one. It is derived as Σ active / Σ apparent, the only
definition that survives units of different size. (Until the Fronius templates
normalize PF to a fraction, a unit's own ``power_factor/total`` is still the raw
SunSpec ±100 while the endpoint's is a true ±1 — see
the Fronius migration plan (operator notes), phase P3.)

``units_online`` / ``units_total`` / ``status`` publish on EVERY cycle,
including the one where nothing is fresh: an endpoint that never says ``offline``
leaves its consumers unable to tell a dark endpoint from a dead gateway.

Opt-out per endpoint with ``aggregates: false`` in the ``endpoints:`` entry.
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
# Site ratios (autonomy, self-consumption) are averaged rather than summed: two
# installations that are each 100 % autonomous are not 200 % autonomous. A site
# group holds ONE unit, where averaging is simply passthrough — but the rule has
# to be right for the day someone groups two sites together, and "no rule at
# all" silently dropped them, which is how they went missing the first time.
_AVG_PREFIXES = ("voltage_", "frequency", "temperature_",
                 "autonomy", "self_consumption")
# Names that would be nonsense to combine: identity strings, status codes, event
# bitfields, plumbing — plus power factor, which is DERIVED from the summed
# powers instead (a ratio is neither summable nor meaningfully averageable).
_SKIP_PREFIXES = ("operating_state", "vendor_state", "event_", "vendor_event_",
                  "manufacturer", "model", "serial", "mppt_modules",
                  "power_factor",
                  # what an inverter is TOLD (model 123) is a setting per unit:
                  # summing two 100 % limits into "200 %" describes nothing
                  "power_limit_", "controls_")

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
    """How a canonical field combines across an endpoint's units: ``sum`` | ``avg``
    | ``counter`` | None (not aggregated). Public because the history picker
    needs to know which of a unit's series an endpoint actually republishes."""
    return _rule_for(name)


def _derive_power_factor(out: Dict[str, Any]) -> Optional[float]:
    """An endpoint's power factor is Σ active / Σ apparent. None when the endpoint has
    no apparent power (register not selected) or stands still (Σ apparent 0)."""
    p, s = out.get("power_active_total"), out.get("power_apparent_total")
    if not isinstance(p, (int, float)) or isinstance(p, bool):
        return None
    if not isinstance(s, (int, float)) or isinstance(s, bool) or not s:
        return None
    return round(max(-1.0, min(1.0, p / s)), 4)


def compute_endpoint_aggregates(config, registry, endpoint_id: str,
                             now: Optional[float] = None,
                             group_id: Optional[str] = None) -> Dict[str, Any]:
    """Pure computation: ``{name: value}`` for one endpoint from its units' live
    stores, plus ``units_online`` / ``units_total`` / ``status``. Shared by the
    publisher thread and the ``/api/endpoints`` endpoint.

    ``units_total`` counts the units EXPECTED to contribute (the enabled ones) —
    the denominator of "how many are producing", not the configuration census
    the UI lists. A disabled unit is excluded everywhere: it must not hold the
    endpoint's counters hostage, nor make ``status`` unreachable from ``online``.

    ``group_id`` restricts the sum to ONE group of the endpoint, and it must be
    used whenever an endpoint holds more than one kind of thing. A PV plant
    holds inverters and the meter at its grid connection: their active powers
    have opposite meanings — generation against import/export — and adding them
    would produce a number that describes nothing. Groups aggregate separately
    or not at all.
    """
    now = now if now is not None else time.time()
    now_mono = time.monotonic()
    expected = [d for d in config.endpoint_devices(endpoint_id)
                if getattr(d, "enabled", True)
                and (group_id is None
                     or (getattr(d, "group_id", "") or "units") == group_id)]
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
            # freshness on the store's monotonic stamp when it has one — an
            # NTP step must not mark every unit offline for an interval
            # (F-58, 3.83.0); the wall-clock stamp is the fallback
            mono = entry.get("mono")
            age = (now_mono - mono) if isinstance(mono, (int, float)) else (
                (now - ts) if ts is not None else None)
            is_fresh = age is not None and age <= max(4 * float(interval), 60.0)
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


def aggregate_topic(endpoint: Dict, endpoint_id: str, group_id: str = "",
                    first: bool = False) -> str:
    """Where one group's totals are published.

    The default keeps every topic that exists today byte-identical:
    ``mbg/endpoints/<id>`` for the first group, with later groups under their own
    name. An installation may override the root — one gateway can front several
    SITES, and `pv/` on two of them would be two installations writing the same
    topics. ``${endpoint_id}`` and ``${group_id}`` substitute, so one pattern can
    serve them all.

    A trailing slash is trimmed and an empty override falls back, because a root
    of "" would publish to a topic starting with "/" that no broker layout wants.
    """
    mqtt = (endpoint.get('mqtt') or {})
    pat = str(mqtt.get('aggregate_prefix', '') or '').strip().strip('/')
    if not pat:
        # Default only: the first group keeps the bare path so every topic that
        # predates groups is byte-identical. That rule exists to protect
        # EXISTING consumers — it must not silently rewrite a pattern the
        # operator wrote themselves, which is how `pv/${group_id}` published the
        # inverters' totals to a bare `pv/`.
        base = f"mbg/endpoints/{endpoint_id}"
        return f"{base}/{group_id}" if group_id and not first else base
    out = (pat.replace('${endpoint_id}', endpoint_id)
              .replace('${group_id}', group_id or '')
              .replace('${id}', endpoint_id))
    if '${group_id}' not in pat and group_id and not first:
        out = f"{out}/{group_id}"
    # The first group has no name, so a pattern that places ${group_id} mid-path
    # collapses to an empty segment. "pv//summary" is a DIFFERENT topic from
    # "pv/summary" to a broker, and subscribers to the latter would never see it.
    return '/'.join(seg for seg in out.split('/') if seg)


def _safe_bucket(name: str) -> str:
    """A bucket name that cannot collide with another device's by accident."""
    out = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(name))
    return out or "mbg"


def endpoint_bucket(endpoint: Dict, endpoint_id: str) -> Optional[str]:
    """The InfluxDB bucket for an endpoint's OWN points. The endpoint's units resolve
    ``${unit_id}`` / ``${device_id}`` per unit; the aggregate belongs to no unit,
    so every placeholder resolves to the endpoint itself — a legal, findable bucket
    name instead of a literal ``fronius_${unit_id}`` on the wire."""
    bucket = (endpoint.get("influxdb") or {}).get("bucket") or None
    if not bucket:
        # No bucket declared. Returning None hands the write to the publisher's
        # global default, which is whatever the PRIMARY device uses — so an
        # installation that forgot to name a bucket would pour its totals into
        # an unrelated device's series. Name it after the installation instead:
        # findable, obviously separate, and wrong in a way nobody can miss.
        return _safe_bucket(endpoint_id)
    return (str(bucket).replace("${endpoint_id}", endpoint_id)
                       .replace("${device_id}", endpoint_id)
                       .replace("${unit_id}", endpoint_id))


class EndpointAggregator(threading.Thread):
    """Periodic publisher of endpoint aggregates (daemon thread, one per app)."""

    def __init__(self, config, registry, get_mqtt, get_influx,
                 interval_s: float = 10.0):
        super().__init__(daemon=True, name="Endpoint-Aggregator")
        self._config = config
        self._registry = registry
        self._get_mqtt = get_mqtt
        self._get_influx = get_influx
        self._interval = interval_s
        self._stop = threading.Event()
        # per-endpoint {name: last written value} — InfluxDB honours the same
        # change-detection contract as every other sink, so an endpoint standing
        # still overnight does not write 8 640 identical points per field
        self._influx_last: Dict[str, Dict[str, Any]] = {}
        self._ensured: set = set()

    def stop(self):
        self._stop.set()

    def run(self):
        logger.info("endpoint aggregator started (interval %.0fs)", self._interval)
        # small startup delay so the first cycle sees warmed-up stores
        self._stop.wait(self._interval)
        n = 0
        while not self._stop.is_set():
            try:
                self._publish_all(log=(n % 60 == 0))
            except Exception as e:  # noqa: BLE001 — the loop must survive anything
                logger.warning("endpoint aggregator cycle failed: %s", e, exc_info=True)
            n += 1
            self._stop.wait(self._interval)

    def _publish_all(self, log: bool = False):
        for p in self._config.endpoints:
            pid = p.get("id")
            if not pid or not bool(p.get("enabled", True)):
                continue
            if not bool(p.get("aggregates", True)):
                continue
            # One total per GROUP. A plant's inverters and the meter at its
            # grid connection measure opposite things — generation against
            # import/export — so one sum over both would describe nothing.
            # A config stub in a test (or an older one) may not know about
            # groups; one implicit group is the right answer for it, and the
            # same answer groups-unaware endpoints get anyway.
            _gids = getattr(self._config, 'endpoint_group_ids', None)
            groups = (_gids(pid) if callable(_gids) else None) or ['units']
            for gi, gid in enumerate(groups):
                agg = compute_endpoint_aggregates(self._config, self._registry,
                                                  pid, group_id=gid)
                # The FIRST group keeps the bare endpoint path, so every topic
                # and Influx series that existed before groups is byte-identical.
                # The real group id always travels; whether the FIRST group is
                # published bare is a property of the default pattern, decided
                # inside aggregate_topic, not guessed here.
                sub, first = gid, (gi == 0)
                if int(agg.get("units_total") or 0) < 2:
                    # A total of one unit is that unit, published twice under
                    # another name. The unit's own topics already carry it.
                    continue
                if log:
                    fields = sum(1 for k in agg if k not in _META)
                    energy = sum(1 for k in agg if k.startswith("energy_"))
                    logger.info("endpoint %s/%s aggregates: %s online=%s/%s "
                                "fields=%d energy_leaves=%d "
                                "(power_active_total=%s)", pid, gid,
                                agg.get("status", "—"), agg.get("units_online"),
                                agg.get("units_total"), fields, energy,
                                agg.get("power_active_total"))
                # Publishes UNCONDITIONALLY, including a cycle with nothing
                # fresh: that cycle still carries status=offline and
                # units_online=0, which is precisely what a consumer needs at
                # nightfall.
                self._publish_mqtt(pid, agg, sub, p, first)
                self._publish_influx(p, pid, agg, sub if not first else '')

    def _publish_mqtt(self, pid: str, agg: Dict[str, Any], sub: str = "",
                      endpoint: Optional[Dict] = None, first: bool = False):
        mqtt = self._get_mqtt()
        if mqtt is None or not getattr(mqtt, "connected", False):
            return
        base = aggregate_topic(endpoint or {}, pid, sub, first)
        for name, val in agg.items():
            leaf = name if name in _META else (mqtt_topic_for(name) or name)
            # publish_if_changed keeps the change-detection semantics every
            # other topic has (heartbeat republish included)
            mqtt.publish_if_changed(f"{base}/{leaf}", val)

    def _publish_influx(self, endpoint: Dict, pid: str, agg: Dict[str, Any],
                        sub: str = ""):
        influx = self._get_influx()
        if influx is None or not getattr(influx, "connected", False):
            return
        ix = endpoint.get("influxdb") or {}
        # An installation that has switched InfluxDB off means it: the units
        # stopped writing, and its totals must stop too. They did not, and with
        # no bucket of their own they landed in the GLOBAL default — which on
        # this system is the Janitza's bucket, so a Fronius test endpoint was
        # quietly writing into the grid meter's series.
        if not bool(ix.get("enabled", True)):
            return
        from .canonical_fields import measurement_for
        try:
            from influxdb_client import Point, WritePrecision
        except Exception:  # noqa: BLE001 — influx client optional in tests
            return
        bucket = endpoint_bucket(endpoint, pid)
        # A derived name may be a bucket nobody has created, and a write to a
        # missing bucket is simply lost. Ensure it once, then remember — this
        # runs on every aggregate cycle.
        if bucket and bucket not in self._ensured:
            try:
                fn = getattr(influx, 'ensure_bucket', None)
                if callable(fn):
                    fn(bucket)
            except Exception as e:  # noqa: BLE001 — a totals write must not
                logger.debug("ensure_bucket(%s): %s", bucket, e)
            self._ensured.add(bucket)
        changed_only = getattr(influx, "publish_mode", "changed") == "changed"
        # change detection is per GROUP: two groups carry the same field names
        # and one shared memo would hide the second group's every value
        last = self._influx_last.setdefault(f"{pid}/{sub}" if sub else pid, {})
        ts = time.time()
        for name, val in agg.items():
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                continue                     # status text is MQTT-only
            if changed_only and last.get(name) == val:
                continue
            meas = "endpoint" if name in _META else (measurement_for(name) or "endpoint")
            point = (Point(meas)
                     .tag("device", pid).tag("aggregate", "endpoint")
                     .field(name, float(val))
                     .time(int(ts * 1e9), WritePrecision.NS))
            # A tag CHANGES series identity, so the first group must not gain
            # one: its series has to stay byte-identical to what it wrote
            # before groups existed. Later groups are tagged, which is also what
            # keeps a plant's meter from overwriting its inverters' totals.
            if sub:
                point = point.tag("group", sub)
            influx.write_point(point, ts=ts, bucket=bucket)
            last[name] = val
