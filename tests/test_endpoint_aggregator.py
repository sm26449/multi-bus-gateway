# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Endpoint aggregates: an endpoint publishes its own output — sums for
powers/currents, averages for voltages/frequency/temperatures, COUNTERS from
last-known values with a complete census, power factor derived as
Σ active / Σ apparent, and a census/status that publishes even when nothing
is fresh."""
import time

import pytest

from multibus.endpoint_aggregator import (EndpointAggregator,
                                       compute_endpoint_aggregates, _rule_for,
                                       endpoint_bucket)


class _Dev:
    def __init__(self, id, enabled=True):
        self.id = id
        self.enabled = enabled


class _Cfg:
    def __init__(self, units, endpoints=None):
        self._units = [u if isinstance(u, _Dev) else _Dev(u) for u in units]
        self.endpoints = (endpoints if endpoints is not None
                       else [{"id": "p", "name": "P", "enabled": True}])

    def endpoint_devices(self, pid):
        return self._units


class _Reg:
    def __init__(self, stores):
        self._stores = stores

    def store_for(self, did):
        return self._stores.get(did)


class _Mqtt:
    connected = True

    def __init__(self):
        self.sent = {}

    def publish_if_changed(self, topic, value):
        self.sent[topic] = value
        return True


class _Influx:
    connected = True
    publish_mode = "changed"

    def __init__(self):
        self.points = []

    def write_point(self, point, ts=None, bucket=None):
        self.points.append((point, bucket))


def _entry(name, value, ts=None, interval=15):
    return {"name": name, "value": value,
            "ts": ts if ts is not None else time.time(), "interval": interval}


# ── rules ────────────────────────────────────────────────────────────────────

def test_rules():
    assert _rule_for("power_active_total") == "sum"
    assert _rule_for("current_l1") == "sum"
    assert _rule_for("power_dc_mppt1") == "sum"
    assert _rule_for("voltage_l1_n") == "avg"
    assert _rule_for("frequency") == "avg"
    assert _rule_for("temperature_cabinet") == "avg"
    # counters are their own class — last-known, never freshness-gated
    assert _rule_for("energy_active_generated") == "counter"
    assert _rule_for("energy_dc_mppt1") == "counter"
    # a ratio is neither summable nor averageable — PF is DERIVED instead
    assert _rule_for("power_factor_total") is None
    assert _rule_for("power_factor_l1") is None
    assert _rule_for("operating_state") is None      # nonsense to combine
    assert _rule_for("manufacturer") is None
    assert _rule_for("event_flags_1") is None


# ── instantaneous quantities ─────────────────────────────────────────────────

def test_sum_and_avg_across_units():
    cfg = _Cfg(["u1", "u2", "u3"])
    reg = _Reg({
        "u1": {1: _entry("power_active_total", 10000), 2: _entry("voltage_l1_n", 230)},
        "u2": {1: _entry("power_active_total", 12000), 2: _entry("voltage_l1_n", 232)},
        "u3": {1: _entry("power_active_total", 8000), 2: _entry("voltage_l1_n", 234)},
    })
    agg = compute_endpoint_aggregates(cfg, reg, "p")
    assert agg["power_active_total"] == 30000
    assert agg["voltage_l1_n"] == 232
    assert agg["units_online"] == 3 and agg["units_total"] == 3


def test_stale_unit_drops_out():
    now = time.time()
    cfg = _Cfg(["u1", "u2"])
    reg = _Reg({
        "u1": {1: _entry("power_active_total", 10000, ts=now)},
        # stale: older than 4x interval
        "u2": {1: _entry("power_active_total", 12000, ts=now - 120, interval=15)},
    })
    agg = compute_endpoint_aggregates(cfg, reg, "p", now=now)
    assert agg["power_active_total"] == 10000     # only the fresh unit
    assert agg["units_online"] == 1 and agg["units_total"] == 2


def test_non_numeric_and_unknown_names_ignored():
    cfg = _Cfg(["u1"])
    reg = _Reg({"u1": {
        1: _entry("power_active_total", 5000),
        2: _entry("manufacturer", "Fronius"),          # string → skip
        3: _entry("operating_state", 4),               # skip-rule
        4: _entry("a_sf", -2),                         # no rule → skip
    }})
    agg = compute_endpoint_aggregates(cfg, reg, "p")
    assert set(agg) == {"power_active_total", "units_online", "units_total",
                        "status"}


def test_empty_endpoint():
    agg = compute_endpoint_aggregates(_Cfg([]), _Reg({}), "p")
    assert agg == {"units_online": 0, "units_total": 0}


# ── counters ─────────────────────────────────────────────────────────────────

def test_counters_use_last_known_values_and_survive_the_night():
    night = time.time() - 3600
    cfg = _Cfg(["u1", "u2"])
    reg = _Reg({
        "u1": {1: _entry("energy_active_generated", 1000, ts=night)},
        "u2": {1: _entry("energy_active_generated", 2000, ts=night)},
    })
    agg = compute_endpoint_aggregates(cfg, reg, "p")
    assert agg["energy_active_generated"] == 3000    # complete, despite stale
    assert agg["units_online"] == 0 and agg["status"] == "offline"


def test_counter_never_jumps_backwards_as_units_go_dark():
    """The 2026-09-11 regression: four fresh inverters summed ~178 MWh; three
    went dark and the endpoint counter dropped to the one that was left."""
    now = time.time()
    vals = [22_925_800, 22_096_800, 20_059_310, 112_903_704]
    cfg = _Cfg([f"u{i}" for i in range(1, 5)])
    reg = _Reg({f"u{i + 1}": {1: _entry("energy_active_generated", v, ts=now)}
                for i, v in enumerate(vals)})
    day = compute_endpoint_aggregates(cfg, reg, "p", now=now)
    night = compute_endpoint_aggregates(cfg, reg, "p", now=now + 7200)
    assert day["energy_active_generated"] == sum(vals)
    assert night["energy_active_generated"] == day["energy_active_generated"]
    assert night["units_online"] == 0


def test_counter_withheld_when_a_unit_has_none():
    cfg = _Cfg(["u1", "u2"])
    reg = _Reg({
        "u1": {1: _entry("energy_active_generated", 1000)},
        "u2": {1: _entry("power_active_total", 5)},      # no counter of its own
    })
    agg = compute_endpoint_aggregates(cfg, reg, "p")
    assert "energy_active_generated" not in agg        # a partial sum is a lie
    assert agg["power_active_total"] == 5


# ── power factor ─────────────────────────────────────────────────────────────

def test_power_factor_is_a_ratio_of_sums_not_an_average():
    cfg = _Cfg(["u1", "u2"])
    reg = _Reg({
        "u1": {1: _entry("power_active_total", 1000),
               2: _entry("power_apparent_total", 1000),      # this unit: PF 1.0
               3: _entry("power_factor_total", 100)},
        "u2": {1: _entry("power_active_total", 0),
               2: _entry("power_apparent_total", 1000),      # this unit: PF 0.0
               3: _entry("power_factor_total", 0)},
    })
    agg = compute_endpoint_aggregates(cfg, reg, "p")
    assert agg["power_factor_total"] == 0.5      # 1000/2000, not avg(100, 0)


def test_power_factor_absent_without_apparent_power():
    cfg = _Cfg(["u1"])
    reg = _Reg({"u1": {1: _entry("power_active_total", 1000)}})
    assert "power_factor_total" not in compute_endpoint_aggregates(cfg, reg, "p")


def test_power_factor_absent_at_standstill():
    cfg = _Cfg(["u1"])
    reg = _Reg({"u1": {1: _entry("power_active_total", 0),
                       2: _entry("power_apparent_total", 0)}})
    assert "power_factor_total" not in compute_endpoint_aggregates(cfg, reg, "p")


# ── census / status ──────────────────────────────────────────────────────────

def test_endpoint_status_online_partial_offline():
    cfg = _Cfg(["u1", "u2"])
    fresh = {1: _entry("power_active_total", 100)}
    stale = {1: _entry("power_active_total", 100, ts=time.time() - 3600)}
    assert compute_endpoint_aggregates(cfg, _Reg({"u1": fresh, "u2": dict(fresh)}),
                                    "p")["status"] == "online"
    assert compute_endpoint_aggregates(cfg, _Reg({"u1": fresh, "u2": stale}),
                                    "p")["status"] == "partial"
    assert compute_endpoint_aggregates(cfg, _Reg({"u1": stale, "u2": stale}),
                                    "p")["status"] == "offline"


def test_disabled_unit_is_not_expected_to_contribute():
    cfg = _Cfg([_Dev("u1"), _Dev("u2", enabled=False)])
    reg = _Reg({"u1": {1: _entry("energy_active_generated", 500),
                       2: _entry("power_active_total", 10)}})
    agg = compute_endpoint_aggregates(cfg, reg, "p")
    assert agg["units_total"] == 1 and agg["units_online"] == 1
    assert agg["status"] == "online"      # reachable despite the disabled unit
    assert agg["energy_active_generated"] == 500   # not held hostage by it


# ── publishing ───────────────────────────────────────────────────────────────

def test_census_publishes_even_when_nothing_is_fresh():
    stale = {1: _entry("power_active_total", 100, ts=time.time() - 3600)}
    cfg = _Cfg(["u1", "u2"])
    mq = _Mqtt()
    EndpointAggregator(cfg, _Reg({"u1": stale, "u2": dict(stale)}),
                    lambda: mq, lambda: None)._publish_all()
    assert mq.sent["mbg/endpoints/p/status"] == "offline"
    assert mq.sent["mbg/endpoints/p/units_online"] == 0
    assert mq.sent["mbg/endpoints/p/units_total"] == 2
    assert "mbg/endpoints/p/power/active/total" not in mq.sent


def test_mqtt_leaves_use_canonical_topics():
    cfg = _Cfg(["u1"])
    reg = _Reg({"u1": {1: _entry("power_active_total", 1500),
                       2: _entry("energy_active_generated", 90)}})
    mq = _Mqtt()
    EndpointAggregator(cfg, reg, lambda: mq, lambda: None)._publish_all()
    assert mq.sent["mbg/endpoints/p/power/active/total"] == 1500
    assert mq.sent["mbg/endpoints/p/energy/active/generated"] == 90


def test_aggregates_false_opts_the_endpoint_out():
    cfg = _Cfg(["u1"], endpoints=[{"id": "p", "enabled": True, "aggregates": False}])
    reg = _Reg({"u1": {1: _entry("power_active_total", 1500)}})
    mq = _Mqtt()
    EndpointAggregator(cfg, reg, lambda: mq, lambda: None)._publish_all()
    assert mq.sent == {}


def test_endpoint_bucket_resolves_every_placeholder_to_the_endpoint():
    assert endpoint_bucket({"influxdb": {"bucket": "fronius_${unit_id}"}},
                        "fronius") == "fronius_fronius"
    assert endpoint_bucket({"influxdb": {"bucket": "b_${endpoint_id}"}},
                        "fronius") == "b_fronius"
    assert endpoint_bucket({"influxdb": {"bucket": "flat"}}, "fronius") == "flat"
    assert endpoint_bucket({}, "fronius") is None


def test_influx_writes_the_resolved_bucket_and_only_on_change():
    pytest.importorskip("influxdb_client")
    cfg = _Cfg(["u1"], endpoints=[{"id": "p", "enabled": True,
                                "influxdb": {"bucket": "b_${endpoint_id}"}}])
    reg = _Reg({"u1": {1: _entry("power_active_total", 100)}})
    inf = _Influx()
    ag = EndpointAggregator(cfg, reg, lambda: None, lambda: inf)
    ag._publish_all()
    written = len(inf.points)
    assert written and all(b == "b_p" for _pt, b in inf.points)
    ag._publish_all()                    # nothing changed → nothing written
    assert len(inf.points) == written
