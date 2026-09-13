# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""What the installation page is built from: `GET /api/endpoints/{id}` in the
operator's order — the headline, each unit's glance values and source verdicts,
every way the installation is read, and NO whole-installation total when it
holds more than one kind of thing.
"""
import time

import pytest

from tests.test_endpoint_edit_guard import PLANT
from tests.test_endpoints import make_app, needs_tc


def _feed(registry, dev_id, values, age_s=0.0):
    store = registry.ensure_store(dev_id)
    for i, (name, val) in enumerate(values.items(), start=1):
        store[i] = {"name": name, "value": val, "ts": time.time() - age_s,
                    "interval": 5, "unit": ""}


@needs_tc
def test_the_headline_comes_from_the_site_when_there_is_one(tmp_path):
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=PLANT).status_code == 200
    reg = client.app.state.registry
    _feed(reg, "pv-site", {"power_pv": 32700, "energy_today": 96000,
                           "autonomy": 100, "self_consumption": 74.7,
                           "power_load": -24400})
    _feed(reg, "pv-u1", {"power_active_total": 9000, "voltage_ln_avg": 242, "voltage_dc": 636})
    _feed(reg, "pv-u2", {"power_active_total": 7800, "voltage_l1": 241, "voltage_dc": 592})
    d = client.get("/api/endpoints/pv").json()
    assert d["headline"] == {"power_now": 32700, "energy_today": 96000,
                             "autonomy": 100, "self_consumption": 74.7}
    # a grouped installation has no whole-installation total
    assert d["aggregates"] == {}
    units = {u["device_id"]: u for u in d["units"]}
    assert units["pv-u1"]["live"] == {"power_active_total": 9000,
                                      "voltage_ln_avg": 242, "voltage_dc": 636}
    # an inverter without an average falls back to L1, under the same name
    assert units["pv-u2"]["live"]["voltage_ln_avg"] == 241
    assert units["pv-site"]["live"]["power_load"] == -24400
    assert d["fields"]["voltage_dc"]["unit"] == "V"
    assert d["fields"]["power_pv"]["label"]


@needs_tc
def test_stale_values_do_not_reach_a_row(tmp_path):
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=PLANT).status_code == 200
    _feed(client.app.state.registry, "pv-u1", {"power_active_total": 9000}, age_s=3600)
    d = client.get("/api/endpoints/pv").json()
    assert {u["device_id"]: u["live"] for u in d["units"]}["pv-u1"] == {}


@needs_tc
def test_without_a_site_the_headline_is_the_inverters_sum_and_nothing_else(tmp_path):
    cfg, client = make_app(tmp_path)
    body = dict(PLANT, groups=[PLANT["groups"][0]])
    assert client.post("/api/endpoints", json=body).status_code == 200
    reg = client.app.state.registry
    _feed(reg, "pv-u1", {"power_active_total": 9000})
    _feed(reg, "pv-u2", {"power_active_total": 7800})
    h = client.get("/api/endpoints/pv").json()["headline"]
    assert h["power_now"] == 16800
    assert h["energy_today"] is None and h["autonomy"] is None


@needs_tc
def test_read_via_lists_every_way_the_installation_is_read(tmp_path):
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=PLANT).status_code == 200
    d = client.get("/api/endpoints/pv").json()
    via = {(r["id"], r["protocol"]): r for r in d["read_via"]}
    # the inverters' solar_api and the site's solar_api are different URLs →
    # two ways; plus sunspec
    assert len(d["read_via"]) == 3
    inv_api = next(r for r in d["read_via"] if r["id"] == "solar_api" and "DeviceId" in r["address"])
    assert inv_api["interval_s"] == 2 and inv_api["groups"] == ["inverters"]
    assert via[("sunspec", "tcp")]["interval_s"] == 20
    for r in d["read_via"]:
        assert set(r) >= {"status", "units_ok", "units_total", "reads_5m",
                          "failed_5m", "fail_pct_5m", "latency_ms"}
    # a source with no client behind it (endpoint disabled in tests) is idle
    assert all(r["status"] == "idle" for r in d["read_via"])
    # per-source verdicts travel with each unit
    assert all("sources" in u for u in d["units"])


def test_the_five_minute_window_is_a_difference_not_a_counter():
    from multibus.multi_source import MultiSourceClient
    c = MultiSourceClient("u", [])
    t = 1000.0
    assert c._window("s", 100, 10, now=t) == (0, 0)          # first sample
    assert c._window("s", 130, 12, now=t + 60) == (32, 2)    # 30 ok + 2 failed since
    # the baseline is the newest sample at least a window old — t+60 here —
    # so the difference spans the whole window, never a counter since boot
    assert c._window("s", 1000, 50, now=t + 400) == (1000 - 130 + 50 - 12, 38)
    assert c._window("s", 1010, 50, now=t + 720) == (10, 0)  # baseline moved to t+400
    assert c._window("s", None, None) == (None, None)
