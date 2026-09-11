# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Plant aggregates: a plant publishes its own output — sums for
powers/currents/energies, averages for voltages/frequency/PF/temperatures,
freshness-aware unit census."""
import time

from multibus.plant_aggregator import compute_plant_aggregates, _rule_for


class _Dev:
    def __init__(self, id):
        self.id = id


class _Cfg:
    def __init__(self, units):
        self._units = [_Dev(u) for u in units]

    def plant_devices(self, pid):
        return self._units


class _Reg:
    def __init__(self, stores):
        self._stores = stores

    def store_for(self, did):
        return self._stores.get(did)


def _entry(name, value, ts=None, interval=15):
    return {"name": name, "value": value,
            "ts": ts if ts is not None else time.time(), "interval": interval}


def test_rules():
    assert _rule_for("power_active_total") == "sum"
    assert _rule_for("current_l1") == "sum"
    assert _rule_for("energy_active_generated") == "sum"
    assert _rule_for("voltage_l1_n") == "avg"
    assert _rule_for("frequency") == "avg"
    assert _rule_for("power_factor_total") == "avg"
    assert _rule_for("temperature_cabinet") == "avg"
    assert _rule_for("operating_state") is None      # nonsense to combine
    assert _rule_for("manufacturer") is None
    assert _rule_for("event_flags_1") is None


def test_sum_and_avg_across_units():
    cfg = _Cfg(["u1", "u2", "u3"])
    reg = _Reg({
        "u1": {1: _entry("power_active_total", 10000), 2: _entry("voltage_l1_n", 230)},
        "u2": {1: _entry("power_active_total", 12000), 2: _entry("voltage_l1_n", 232)},
        "u3": {1: _entry("power_active_total", 8000), 2: _entry("voltage_l1_n", 234)},
    })
    agg = compute_plant_aggregates(cfg, reg, "p")
    assert agg["power_active_total"] == 30000
    assert agg["voltage_l1_n"] == 232
    assert agg["units_online"] == 3 and agg["units_total"] == 3


def test_stale_unit_drops_out():
    now = time.time()
    cfg = _Cfg(["u1", "u2"])
    reg = _Reg({
        "u1": {1: _entry("power_active_total", 10000, ts=now)},
        # stale: older than 3x interval
        "u2": {1: _entry("power_active_total", 12000, ts=now - 120, interval=15)},
    })
    agg = compute_plant_aggregates(cfg, reg, "p", now=now)
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
    agg = compute_plant_aggregates(cfg, reg, "p")
    assert set(agg) == {"power_active_total", "units_online", "units_total",
                        "status"}


def test_empty_plant():
    agg = compute_plant_aggregates(_Cfg([]), _Reg({}), "p")
    assert agg == {"units_online": 0, "units_total": 0}


def test_plant_status_online_partial_offline():
    cfg = _Cfg(["u1", "u2"])
    fresh = {1: _entry("power_active_total", 100)}
    stale = {1: _entry("power_active_total", 100, ts=time.time() - 3600)}
    assert compute_plant_aggregates(cfg, _Reg({"u1": fresh, "u2": dict(fresh)}),
                                    "p")["status"] == "online"
    assert compute_plant_aggregates(cfg, _Reg({"u1": fresh, "u2": stale}),
                                    "p")["status"] == "partial"
    assert compute_plant_aggregates(cfg, _Reg({"u1": stale, "u2": stale}),
                                    "p")["status"] == "offline"
