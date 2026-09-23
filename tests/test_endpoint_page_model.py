# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""What the installation page is built from: `GET /api/endpoints/{id}` in the
operator's order — the headline, each unit's glance values and source verdicts,
every way the installation is read, and NO whole-installation total when it
holds more than one kind of thing.
"""
import time


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


@needs_tc
def test_a_unit_is_described_by_the_sources_that_read_it(tmp_path):
    """The unit page showed a Modbus form with an empty host for a unit read
    over HTTP every two seconds. The entry now carries how it is read."""
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=PLANT).status_code == 200
    _feed(client.app.state.registry, "pv-u1", {"power_active_total": 9000, "voltage_dc": 636})
    d = {x["id"]: x for x in client.get("/api/devices").json()["devices"]}
    u = d["pv-u1"]
    assert u["endpoint_name"] == "PV installation" and u["role"] == "inverter"
    assert [r["id"] for r in u["read_via"]] == ["solar_api", "sunspec"]
    api, sun = u["read_via"]
    assert api["protocol"] == "http" and "DeviceId=1" in api["address"]
    assert api["interval_s"] == 2 and api["stale_after_s"] == 0
    assert sun["protocol"] == "tcp" and sun["address"] == "192.0.2.9:502 · unit 1"
    assert sun["interval_s"] == 20 and sun["timeout_s"]
    for r in u["read_via"]:
        assert set(r) >= {"template", "registers", "provides", "status", "latency_ms",
                          "reads_5m", "failed_5m", "fail_pct_5m"}
    assert u["live"] == {"power_active_total": 9000, "voltage_dc": 636}
    assert u["fields"]["voltage_dc"]["unit"] == "V"
    # a standalone device is untouched: no read_via, its own connection block
    assert "read_via" not in d["umg512"] and d["umg512"]["connection"]["host"]


def test_every_driver_reports_when_it_last_succeeded():
    """`sources[].last_success_ts` was null for HTTP and MQTT sources while
    `successful_reads` climbed (UI audit 6.1) — an integrator could not tell
    a fresh source from a dead one without the gateway's own verdict."""
    from multibus.http_client import HttpClient
    from multibus.mqtt_input import MqttInputClient
    h = HttpClient({"url": "http://127.0.0.1:1/x"}, registers=[], poll_groups={})
    assert "last_success_ts" in h.get_stats() and h.get_stats()["last_success_ts"] is None
    h.last_success_ts = 1_700_000_000.0
    assert h.get_stats()["last_success_ts"] == 1_700_000_000.0
    m = MqttInputClient.__new__(MqttInputClient)
    m.connected, m.messages, m.updates, m.last_msg_ts = False, 0, 0, 1_700_000_001.0
    m.broker, m.port, m.base_topic = "b", 1883, "t"
    m._subscriptions = lambda: ["t/#"]
    assert m.get_stats()["last_success_ts"] == 1_700_000_001.0
