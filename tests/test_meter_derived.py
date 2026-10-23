# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""What an input and an output meter give a device (a UPS): its input and
output power and energy, its loss, its efficiency and its load against its
rating. Computed when a METER reports — a UPS with no communication of its
own is measured by its meters alone (a meter-only device). Loss and
efficiency only when honest (both readings fresh, measured close in time,
the input steady, the output no larger than the input beyond the meters'
error; the efficiency above a minimum load), never clamped to 100 %; an
absent value leaves the live store (rules and /api/values see none) and its
retained topic is cleared; the device view shows the LAST evaluation's
verdict. The clock is the readings' own `mono` stamps, set back by the
test."""
import json
import time
from datetime import datetime

import pytest

from tests.test_devices_api import make_app, needs_tc
from tests.test_linked_meter import _row
from tests.test_meter_links import IN, OUT, _devices, _stored

DERIVED = {"power_in", "power_out", "power_loss", "efficiency_pct", "load_pct", "energy_in"}


def _files(tmp_path, did, rows, groups=None):
    f = tmp_path / "devices" / did / "selected_registers.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"version": "1.0", "poll_groups": groups or {}, "registers": rows}))


def _ups(tmp_path, client, *, out_unit="W", options=None, sign_in=None, meters=None):
    _devices(tmp_path, client)
    if out_unit != "W":
        _files(tmp_path, "em-out", [_row(0, "power_active_total", out_unit)])
    body = {"meters": meters or [{**IN, **({"sign": sign_in} if sign_in else {})}, OUT]}
    if options is not None:
        body["options"] = options
    r = client.put("/api/devices/ups/meters", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _read(client, *, pin, pout, age_in=0.0, age_out=0.0, interval=5):
    """A reading in each meter's store, measured `age` seconds ago."""
    reg = client.app.state.registry
    now, mono = datetime.now().isoformat(), time.monotonic()
    for did, v, age in (("em-in", pin, age_in), ("em-out", pout, age_out)):
        st = reg.ensure_store(did)
        st.pop(0, None)
        if v is not None:
            st[0] = {"name": "power_active_total", "value": v, "timestamp": now,
                     "mono": mono - age, "interval": interval}


def _run(client, gone=None):
    """What the meters' report computes — the store is NOT cleared between
    runs: an absent value must leave it by itself."""
    app = client.app
    ups = app.state.registry.ensure_store("ups")
    # the devices here are stored disabled (no client dials a test address);
    # linked values are computed for a device that runs
    app.state.calc_engine.config.get_device("ups").enabled = True
    app.state.calc_engine.run_linked("ups", ups, force=True, gone=gone, topic_prefix="mbg/devices/ups",
                                     bucket=None, device_tag=None, device_id="ups",
                                     mqtt_on=False, influx_on=False)
    return {v.get("name"): v.get("value") for v in ups.values() if v.get("calculated")}


def _steady(client, gone=None, **kw):
    """Two readings of the same load, a moment apart (the input is steady)."""
    pin, pout = kw.pop("pin"), kw.pop("pout")
    _read(client, pin=pin, pout=pout, **{**kw, "age_in": kw.get("age_in", 0.0) + 1.0,
                                          "age_out": kw.get("age_out", 0.0) + 1.0})
    _run(client, gone)
    _read(client, pin=pin, pout=pout, **kw)
    return _run(client, gone)


def _status(client):
    view = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "ups")
    return view["meters_status"], view


@needs_tc
def test_an_input_and_an_output_meter_give_loss_and_efficiency(tmp_path):
    _cfg, client = make_app(tmp_path)
    _ups(tmp_path, client)
    assert {e["_reg"].name for e in client.app.state.calc_engine.store["ups"]} >= DERIVED
    st, view = _status(client)
    assert st["efficiency_pct"] == {"state": "waiting", "reason": "not_evaluated"}   # no meter has reported
    v = _steady(client, pin=1000.0, pout=920.0)
    assert v["power_in"] == 1000 and v["power_out"] == 920
    assert v["power_loss"] == 80 and v["efficiency_pct"] == 92.0
    st, view = _status(client)
    assert st["efficiency_pct"] == {"state": "published"} and st["power_loss"] == {"state": "published"}
    assert st["age_s"] is not None and st["thresholds"]["max_skew_s"] == 5.0   # the meters' interval
    assert view["meter_options"] == {"min_load_w": 100.0, "tolerance_pct": 2.0, "max_skew_s": None}


@needs_tc
@pytest.mark.parametrize("reading,reason", [
    (dict(pin=50.0, pout=40.0), "below_min_load"),                      # 40 W out: noise
    (dict(pin=1000.0, pout=1050.0), "out_exceeds_in"),                  # +5 %: meters disagree
    (dict(pin=1000.0, pout=920.0, age_in=0.0, age_out=5.5), "skew"),    # a reading missed
    (dict(pin=1000.0, pout=920.0, age_in=16.0, age_out=16.0), "stale"),  # > 3 intervals
    (dict(pin=None, pout=920.0), "no_reading"),
])
def test_an_absent_value_leaves_the_store_and_the_view_says_why(tmp_path, reading, reason):
    _cfg, client = make_app(tmp_path)
    _ups(tmp_path, client)
    assert "efficiency_pct" in _steady(client, pin=1000.0, pout=920.0)
    gone = []
    v = _steady(client, gone, **reading)                # a steady reading of that kind
    assert "efficiency_pct" not in v, v                  # not the last good value either
    assert "efficiency_pct" in {r.name for r in gone}    # its retained topic is cleared
    st, _ = _status(client)
    assert st["efficiency_pct"] == {"state": "absent", "reason": reason}
    # once cleared, it is not cleared again every round
    gone2 = []
    _run(client, gone2)
    assert "efficiency_pct" not in {r.name for r in gone2}


@needs_tc
def test_a_load_that_steps_between_two_readings_is_not_judged(tmp_path):
    _cfg, client = make_app(tmp_path)
    _ups(tmp_path, client)
    _steady(client, pin=1000.0, pout=920.0)
    _read(client, pin=1500.0, pout=920.0)               # the input jumped 50 %
    v = _run(client)
    assert "efficiency_pct" not in v and "power_loss" not in v
    assert _status(client)[0]["efficiency_pct"]["reason"] == "unsteady"
    assert v["power_in"] == 1500                          # the readings themselves are published


@needs_tc
def test_the_first_reading_has_no_previous_one(tmp_path):
    _cfg, client = make_app(tmp_path)
    _ups(tmp_path, client)
    _read(client, pin=1000.0, pout=920.0)
    assert "efficiency_pct" not in _run(client)
    assert _status(client)[0]["efficiency_pct"]["reason"] == "unsteady"


@needs_tc
def test_within_the_meters_error_the_value_is_published_unclamped(tmp_path):
    _cfg, client = make_app(tmp_path)
    _ups(tmp_path, client)
    v = _steady(client, pin=1000.0, pout=1015.0)        # +1.5 %: inside 2 %
    assert v["efficiency_pct"] == 101.5                  # as measured, never 100
    assert v["power_loss"] == -15


@needs_tc
def test_the_standby_loss_is_published(tmp_path):
    """A UPS idling: 30 W in, nothing out. Its loss is what an operator wants
    (an absolute difference has no ratio noise); its efficiency means nothing."""
    _cfg, client = make_app(tmp_path)
    _ups(tmp_path, client)
    v = _steady(client, pin=30.0, pout=0.0)
    assert v["power_loss"] == 30
    assert "efficiency_pct" not in v
    assert _status(client)[0]["efficiency_pct"]["reason"] == "below_min_load"


@needs_tc
def test_the_skew_limit_follows_the_slower_meter(tmp_path):
    """Input read every 1 s, output every 5 s: up to 5 s apart is two
    pollers' phase; more means a reading was missed."""
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client)
    _files(tmp_path, "em-in", [{**_row(0, "power_active_total", "W"), "poll_group": "fast"},
                               _row(2, "energy_active_import", "kWh")],
           {"fast": {"interval": 1}})
    _files(tmp_path, "em-out", [{**_row(0, "power_active_total", "W"), "poll_group": "normal"}],
           {"normal": {"interval": 5}})
    assert client.put("/api/devices/ups/meters", json={"meters": [IN, OUT]}).status_code == 200
    assert client.app.state.calc_engine.pairs["ups"]["max_skew_s"] == 5.0
    v = _steady(client, pin=1000.0, pout=920.0, age_in=0.0, age_out=4.0)
    assert v["efficiency_pct"] == 92.0
    _read(client, pin=1000.0, pout=920.0, age_in=0.0, age_out=5.6)
    assert "efficiency_pct" not in _run(client)
    assert _status(client)[0]["efficiency_pct"]["reason"] == "skew"


@needs_tc
def test_the_thresholds_are_the_devices_own(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = _ups(tmp_path, client, options={"min_load_w": 10, "tolerance_pct": 6, "max_skew_s": 40})
    assert r["options"] == {"min_load_w": 10.0, "tolerance_pct": 6.0, "max_skew_s": 40.0}
    assert _stored(tmp_path, "ups")["meter_options"] == {"min_load_w": 10.0, "tolerance_pct": 6.0,
                                                          "max_skew_s": 40.0}
    assert _steady(client, pin=50.0, pout=40.0, age_out=10.0)["efficiency_pct"] == 80.0   # 10 s apart: under 40
    assert _steady(client, pin=1000.0, pout=1050.0)["efficiency_pct"] == 105.0
    put = lambda b: client.put("/api/devices/ups/meters", json={"meters": [IN, OUT], **b})  # noqa: E731
    # absent keeps them; the same answers unchanged; a default sent is kept as sent
    assert put({}).json()["status"] == "unchanged"
    assert put({"options": {"min_load_w": 100}}).json()["status"] == "updated"
    assert _stored(tmp_path, "ups")["meter_options"] == {"min_load_w": 100.0}
    # {} goes back to the defaults; a dry run says what it would store
    r = client.put("/api/devices/ups/meters?dry_run=true", json={"meters": [IN, OUT], "options": {"tolerance_pct": 3}})
    assert r.json()["options"]["tolerance_pct"] == 3.0
    assert put({"options": {}}).json()["status"] == "updated"
    assert "meter_options" not in _stored(tmp_path, "ups")


@needs_tc
@pytest.mark.parametrize("opts,key", [
    (None, "options"),
    ({"min_load_w": None}, "options.min_load_w"),
    ({"min_load_w": -1}, "options.min_load_w"),
    ({"tolerance_pct": 50}, "options.tolerance_pct"),
    ({"max_skew_s": 0}, "options.max_skew_s"),
    ({"max_skew_s": "10"}, "options.max_skew_s"),
    ({"clamp": True}, "options.clamp"),
])
def test_a_wrong_threshold_is_refused_at_its_field(tmp_path, opts, key):
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client)
    r = client.put("/api/devices/ups/meters", json={"meters": [IN, OUT], "options": opts})
    assert r.status_code == 422 and key in r.json()["detail"]["fields"], r.text
    assert "meters" not in _stored(tmp_path, "ups")


@needs_tc
def test_thresholds_belong_to_a_pair(tmp_path):
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client)
    r = client.put("/api/devices/ups/meters", json={"meters": [IN], "options": {"min_load_w": 5}})
    assert r.status_code == 422 and "options" in r.json()["detail"]["fields"], r.text
    assert client.put("/api/devices/ups/meters",
                      json={"meters": [IN, OUT], "options": {"min_load_w": 5}}).status_code == 200
    # the pair goes: its thresholds go with it
    assert client.put("/api/devices/ups/meters", json={"meters": [IN]}).status_code == 200
    assert "meter_options" not in _stored(tmp_path, "ups")


@needs_tc
def test_input_and_output_are_never_one_field(tmp_path):
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client)
    r = client.put("/api/devices/ups/meters", json={"meters": [IN, {**OUT, "device": "em-in"}]})
    assert r.status_code == 422 and "meters[1].power" in r.json()["detail"]["fields"], r.text
    # one meter, two channels: allowed
    _files(tmp_path, "em-in", [_row(0, "power_active_total", "W"), _row(2, "energy_active_import", "kWh"),
                               _row(4, "power_active_l2", "W")])
    r = client.put("/api/devices/ups/meters", json={"meters": [IN, {**OUT, "device": "em-in",
                                                                    "power": "power_active_l2"}]})
    assert r.status_code == 200, r.text


@needs_tc
def test_a_meter_read_in_kw_or_wired_the_other_way(tmp_path):
    _cfg, client = make_app(tmp_path)
    _ups(tmp_path, client, out_unit="kW", sign_in="invert")
    v = _steady(client, pin=-1000.0, pout=0.95)         # input reversed, output in kW
    assert v["power_in"] == 1000 and v["power_out"] == 950
    assert v["efficiency_pct"] == 95.0


@needs_tc
def test_auto_sign_reads_a_reversed_meter_positive(tmp_path):
    _cfg, client = make_app(tmp_path)
    _ups(tmp_path, client)
    v = _steady(client, pin=-1000.0, pout=-900.0)
    assert v["power_in"] == 1000 and v["efficiency_pct"] == 90.0


@needs_tc
def test_load_needs_a_rating_and_is_never_negative(tmp_path):
    _cfg, client = make_app(tmp_path)
    _ups(tmp_path, client, meters=[IN, {**OUT, "sign": "as_is"}])
    assert "load_pct" not in _steady(client, pin=1000.0, pout=900.0)
    assert _status(client)[0]["load_pct"] == {"state": "absent", "reason": "no_rating"}
    r = client.put("/api/attributes/devices/ups/rating", json={"rated_ac_kw": 3})
    assert r.status_code == 200, r.text
    assert _steady(client, pin=1000.0, pout=900.0)["load_pct"] == 30.0
    assert _status(client)[0]["load_pct"] == {"state": "published"}
    _read(client, pin=1000.0, pout=-50.0)               # a meter read as-is, backwards
    assert "load_pct" not in _run(client)
    assert _status(client)[0]["load_pct"] == {"state": "absent", "reason": "negative"}


@needs_tc
def test_the_meters_counters_are_the_devices_too(tmp_path):
    _cfg, client = make_app(tmp_path)
    _ups(tmp_path, client)
    reg = client.app.state.registry
    _steady(client, pin=1000.0, pout=920.0)
    reg.ensure_store("em-in")[2] = {"name": "energy_active_import", "value": 4321.0,
                                    "timestamp": datetime.now().isoformat(), "mono": time.monotonic(),
                                    "interval": 5}
    v = _run(client)
    assert v["energy_in"] == 4321.0 and "energy_out" not in v      # OUT names no counter
    unit = next(e["_reg"].unit for e in client.app.state.calc_engine.store["ups"] if e["_reg"].name == "energy_in")
    assert unit == "kWh"


@needs_tc
def test_the_values_and_their_thresholds_never_restart_the_device(tmp_path):
    _cfg, client = make_app(tmp_path)
    _ups(tmp_path, client)
    reg = client.app.state.registry
    before = reg.find("ups")[2]
    assert client.put("/api/devices/ups/meters",
                      json={"meters": [IN, OUT], "options": {"min_load_w": 5}}).json()["status"] == "updated"
    assert reg.find("ups")[2] is before
    st = _stored(tmp_path, "ups")
    body = {k: v for k, v in st.items() if k not in ("meters", "meter_options")}
    assert client.put("/api/devices/ups", json={**body, "name": "UPS hall"}).status_code == 200
    assert _stored(tmp_path, "ups")["meter_options"] == {"min_load_w": 5.0}


@needs_tc
def test_an_option_save_keeps_what_the_devices_own_formulas_remember(tmp_path):
    _cfg, client = make_app(tmp_path)
    _ups(tmp_path, client)
    ce = client.app.state.calc_engine
    own = next(e for e in ce.store["ups"] if e["_reg"].name == "power_in")
    own["_state"]["marker"] = 1
    assert client.put("/api/devices/ups/meters",
                      json={"meters": [IN, OUT], "options": {"min_load_w": 5}}).status_code == 200
    again = next(e for e in ce.store["ups"] if e["_reg"].name == "power_in")
    assert again["_state"].get("marker") == 1


@needs_tc
def test_a_meter_edited_reloads_what_it_gives(tmp_path):
    """The output meter's power moves to kW: the UPS reads it in kW at once,
    not after a restart."""
    _cfg, client = make_app(tmp_path)
    _ups(tmp_path, client)
    ce = client.app.state.calc_engine
    assert ce.pairs["ups"]["out"]["unit"] == "W"
    _files(tmp_path, "em-out", [_row(0, "power_active_total", "kW")])
    ce.load("em-out")                                   # what a device edit / re-select does
    assert ce.pairs["ups"]["out"]["unit"] == "kW"


@needs_tc
def test_a_hand_edited_threshold_never_takes_the_device_off(tmp_path):
    from fastapi.testclient import TestClient

    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - {id: ups, enabled: false, connection: {protocol: tcp, host: 192.0.2.9}, meter_options: {min_load_w: lots, tolerance_pct: 3}}
  - {id: ups2, enabled: false, connection: {protocol: tcp, host: 192.0.2.8}, meter_options: [1, 2]}
""")
    assert {d.id for d in cfg.devices} >= {"ups", "ups2"}
    ups = next(d for d in cfg.devices if d.id == "ups")
    assert ups.meter_options == {"tolerance_pct": 3.0}
    assert "ups.meter_options.min_load_w" in cfg.ignored_blocks and "ups2.meter_options" in cfg.ignored_blocks
    app, _ = create_api(cfg, None, None, None)
    assert TestClient(app).get("/api/devices").status_code == 200


@needs_tc
def test_the_derived_names_are_never_a_second_value(tmp_path):
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client)
    _files(tmp_path, "ups", [_row(0, "efficiency_pct", "%")])
    r = client.put("/api/devices/ups/meters", json={"meters": [IN, OUT]})
    assert r.status_code == 422 and "efficiency_pct" in r.text, r.text


@needs_tc
def test_a_meter_value_has_a_stable_ha_identity(tmp_path):
    _cfg, client = make_app(tmp_path)
    _ups(tmp_path, client)
    keys = {e["_reg"].name: getattr(e["_reg"], "discovery_key", "")
            for e in client.app.state.calc_engine.store["ups"]}
    assert keys["efficiency_pct"] == "linked_efficiency_pct"


@needs_tc
def test_the_device_list_reads_no_meter_file(tmp_path, monkeypatch):
    cfg, client = make_app(tmp_path)
    _ups(tmp_path, client)
    reads = []
    real = cfg.load_device_registers

    def counting(dev, *a, **kw):
        reads.append(dev.id)
        return real(dev, *a, **kw)
    monkeypatch.setattr(cfg, "load_device_registers", counting)
    client.get("/api/devices")
    with_pair = sorted(reads)
    reads.clear()
    assert client.put("/api/devices/ups/meters", json={"meters": []}).status_code == 200
    reads.clear()
    client.get("/api/devices")
    assert with_pair == sorted(reads)                   # the pair added no read


@needs_tc
def test_the_consumption_link_gives_exactly_what_it_gave(tmp_path):
    """The earlier single link: the same two entries, the same formulas."""
    from multibus.linked_meter import linked_calcs
    cfg, client = make_app(tmp_path)
    _devices(tmp_path, client, ids=("hp-1", "meter-a"))
    assert client.put("/api/devices/hp-1/energy-meter",
                      json={"device": "meter-a", "power": "power_active_total",
                            "energy": "energy_active_import"}).status_code == 200
    got = linked_calcs(cfg, cfg.get_device("hp-1"))
    assert [(c["name"], c["expr"], c["unit"], c["category"], c["poll_group"]) for c in got] == [
        ("power_electric", "abs(meter-a.power_active_total)", "W", "energy", "normal"),
        ("energy_electric", "meter-a.energy_active_import", "kWh", "energy", "normal")]


# ── a meter-only device: a UPS with no communication of its own ─────────────

def _meter_only(tmp_path, client, **extra):
    _devices(tmp_path, client, ids=("em-in", "em-out"))
    body = {"id": "ups", "name": "UPS", "connection": {"protocol": "none"}, "kind": "ups",
            "meters": [IN, OUT], **extra}
    return client.post("/api/devices", json=body)


@needs_tc
def test_a_meter_only_ups_is_measured_by_its_meters_alone(tmp_path):
    cfg, client = make_app(tmp_path)
    r = _meter_only(tmp_path, client)
    assert r.status_code in (200, 201), r.text
    st = _stored(tmp_path, "ups")
    assert st["connection"] == {"protocol": "none"} and st["kind"] == "ups" and "template" not in st or not st.get("template")
    app = client.app
    _i, dev, ups_client = app.state.registry.find("ups")
    from multibus.meter_only import MeterOnlyClient
    assert isinstance(ups_client, MeterOnlyClient)
    assert "ups" in app.state.calc_engine.dependents("em-in")
    # the meters report: the UPS's runner computes, with no poll of its own
    _read(client, pin=1000.0, pout=920.0, age_in=1.0, age_out=1.0)
    app.state.calc_runners["ups"](force=True)
    _read(client, pin=1000.0, pout=920.0)
    app.state.calc_runners["ups"](force=True)
    vals = {v.get("name"): v.get("value") for v in app.state.registry.ensure_store("ups").values()}
    assert vals["efficiency_pct"] == 92.0
    view = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "ups")
    assert view["meters_status"]["efficiency_pct"] == {"state": "published"}


@needs_tc
def test_a_meters_report_computes_the_ups_through_the_real_callback(tmp_path, monkeypatch):
    """No shortcut: em-in's and em-out's own poll callbacks run, and the
    meter-only UPS gets its values from them."""
    from fastapi.testclient import TestClient

    import multibus.calc_engine as ce_mod
    from multibus.api import create_api
    from multibus.config import SelectedRegister
    from multibus.meter_only import MeterOnlyClient
    from tests.test_devices import write_config
    for did in ("em-in", "em-out"):
        _files(tmp_path, did, [_row(0, "power_active_total", "W")])
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - {id: em-in, enabled: true, connection: {protocol: tcp, host: 192.0.2.10}}
  - {id: em-out, enabled: true, connection: {protocol: tcp, host: 192.0.2.11}}
  - id: ups
    kind: ups
    connection: {protocol: none}
    meters:
      - {role: input, device: em-in, power: power_active_total}
      - {role: output, device: em-out, power: power_active_total}
""")

    class _Meter:
        publish_callback = None
        connected = True

        def data_health(self):
            return {"status": "ok", "last_success_ts": time.time()}

        def get_stats(self):
            return {"connected": True}
    meters = {"em-in": _Meter(), "em-out": _Meter()}
    pairs = [(d, meters.get(d.id) or (MeterOnlyClient(cfg, d.id) if d.id == "ups" else None))
             for d in cfg.devices]
    from unittest.mock import MagicMock
    mqtt = MagicMock()
    mqtt._build_topic.side_effect = lambda r, prefix: f"{prefix}/{r.name}"
    app, _ = create_api(cfg, None, mqtt, None, devices=pairs)
    client = TestClient(app, raise_server_exceptions=False)
    reg = SelectedRegister(address=0, name="power_active_total", label="P", unit="W",
                           data_type="float", poll_group="normal")

    def report(did, value):
        meters[did].publish_callback("normal", {0: {"value": value, "register": reg, "ts": time.time(),
                                                    "mono": time.monotonic(), "interval": 5}})
    monkeypatch.setattr(ce_mod, "LINKED_MIN_GAP_S", 0.0)   # every report evaluates, for the test
    for _ in range(2):                                   # two readings: the input is steady
        report("em-in", 1000.0)
        report("em-out", 920.0)
        time.sleep(0.01)
    vals = {v.get("name"): v.get("value") for v in app.state.registry.ensure_store("ups").values()}
    assert vals["efficiency_pct"] == 92.0 and vals["power_loss"] == 80
    view = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "ups")
    assert view["meters_status"]["efficiency_pct"] == {"state": "published"}
    # published under the UPS's own topic
    sent = [c.args for c in mqtt.publish_register_data.call_args_list
            if c.kwargs.get("topic_prefix") == "mbg/devices/ups"]
    assert any(it["register"].name == "efficiency_pct" for _g, batch in sent for it in batch.values())
    # the load drops under the minimum: absent everywhere, its retained topic cleared once
    mqtt.clear_topics.reset_mock()
    for _ in range(2):
        report("em-in", 50.0)
        report("em-out", 40.0)
        time.sleep(0.01)
    vals = {v.get("name") for v in app.state.registry.ensure_store("ups").values()}
    assert "efficiency_pct" not in vals
    cleared = set().union(*[set(c.args[0]) for c in mqtt.clear_topics.call_args_list])
    assert "mbg/devices/ups/efficiency_pct" in cleared
    assert mqtt.clear_topics.call_count == 1


@needs_tc
def test_a_meter_only_device_is_online_when_its_meters_are(tmp_path, monkeypatch):
    from multibus import meter_only
    from multibus.device_registry import client_is_live
    _cfg, client = make_app(tmp_path)
    assert _meter_only(tmp_path, client).status_code in (200, 201)
    ups = client.app.state.registry.find("ups")[2]

    class _C:
        def __init__(self, status):
            self.status, self.connected = status, status != "down"

        def data_health(self):
            return {"status": self.status, "last_success_ts": time.time() if self.status != "down" else None}
    live = {"em-in": _C("ok"), "em-out": _C("ok")}
    monkeypatch.setattr(meter_only, "find_client", lambda mid: live.get(mid))
    assert ups.data_health()["status"] == "ok" and client_is_live(ups)
    live["em-out"] = _C("down")
    assert ups.data_health()["status"] == "degraded" and client_is_live(ups)
    live["em-in"] = _C("down")
    assert ups.data_health()["status"] == "down" and not client_is_live(ups)
    assert ups.write_value(0, "holding", "uint16", 1)[0] is False


@needs_tc
@pytest.mark.parametrize("extra,field", [
    ({"meters": None}, "meters"),
    ({"meters": []}, "meters"),
    ({"kind": None}, "kind"),
    ({"kind": "toaster"}, "kind"),
    ({"template": "eastron_sdm630"}, "template"),
    ({"connection": {"protocol": "none", "host": "192.0.2.1"}}, "connection"),
    ({"meters": [{**IN, "device": "nope"}]}, "meters[0].device"),
])
def test_a_meter_only_device_is_created_whole_or_not_at_all(tmp_path, extra, field):
    _cfg, client = make_app(tmp_path)
    r = _meter_only(tmp_path, client, **extra)
    assert r.status_code == 422 and field in r.json()["detail"]["fields"], r.text
    assert not client.app.state.registry.has("ups")


@needs_tc
def test_a_meter_only_device_keeps_its_meters(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert _meter_only(tmp_path, client).status_code in (200, 201)
    r = client.put("/api/devices/ups/meters", json={"meters": []})
    assert r.status_code == 422 and "meters" in r.json()["detail"]["fields"], r.text
    # its meters cannot be deleted while it names them
    assert client.delete("/api/devices/em-in").status_code == 409
    # a dry run checks and creates nothing
    r = client.post("/api/devices?dry_run=true", json={"id": "ups2", "connection": {"protocol": "none"},
                                                       "kind": "charger", "meters": [IN]})
    assert r.status_code == 200 and r.json()["would"] == "create", r.text
    assert not client.app.state.registry.has("ups2")
