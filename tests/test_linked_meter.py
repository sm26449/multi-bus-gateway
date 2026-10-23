# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A device whose electricity another device measures: naming the meter
gives it power_electric (W, positive) and energy_electric as its own — its
Energy tab works and its template can compute with them (a heat pump's COP).
Device ids with hyphens work in calculated fields, as in rules."""
import json

from tests.test_devices_api import make_app, needs_tc


def _row(addr, name, unit):
    return {"address": addr, "name": name, "label": name, "unit": unit, "data_type": "float",
            "poll_group": "normal", "mqtt": {"enabled": True, "topic": ""},
            "influxdb": {"enabled": True, "measurement": "", "tags": {}}, "ui": {}}


def _two_devices(tmp_path, client, power_unit="kW"):
    for did in ("hp-1", "meter-a"):
        r = client.post("/api/devices", json={"id": did, "enabled": False,
                                              "connection": {"protocol": "tcp", "host": "192.168.1.9"}})
        assert r.status_code in (200, 201), r.text
    f = tmp_path / "devices" / "meter-a" / "selected_registers.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"version": "1.0", "poll_groups": {}, "registers": [
        _row(0, "power_active_total", power_unit), _row(2, "energy_active_import", "kWh")]}))


@needs_tc
def test_a_named_meter_gives_the_device_its_electricity(tmp_path):
    cfg, client = make_app(tmp_path)
    _two_devices(tmp_path, client)
    r = client.put("/api/devices/hp-1/energy-meter",
                   json={"device": "meter-a", "power": "power_active_total", "energy": "energy_active_import"})
    assert r.status_code == 200, r.text
    dev = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "hp-1")
    assert dev["energy_meter"]["device"] == "meter-a" and dev["energy_fields"] >= 1
    from multibus.linked_meter import linked_calcs
    hp = next(d for d in cfg.devices if d.id == "hp-1")
    calcs = {c["name"]: c for c in linked_calcs(cfg, hp)}
    assert calcs["power_electric"]["expr"] == "abs(meter-a.power_active_total) * 1000"   # kW → W
    assert calcs["energy_electric"]["unit"] == "kWh"
    cands = client.get("/api/energy/fields?device=hp-1").json()["candidates"]
    assert any(c["name"] == "energy_electric" for c in cands)
    # an edit from the device form keeps the link
    r = client.put("/api/devices/hp-1", json={"name": "Heat pump", "enabled": False,
                                             "connection": {"protocol": "tcp", "host": "192.168.1.9"}})
    assert r.status_code == 200, r.text
    assert next(d for d in cfg.devices if d.id == "hp-1").energy_meter["device"] == "meter-a"
    # and {} unlinks
    assert client.put("/api/devices/hp-1/energy-meter", json={}).status_code == 200
    assert not next(d for d in cfg.devices if d.id == "hp-1").energy_meter


@needs_tc
def test_a_bad_link_is_named(tmp_path):
    _cfg, client = make_app(tmp_path)
    _two_devices(tmp_path, client)
    for body, frag in (({"device": "nope", "power": "x"}, "no device 'nope'"),
                       ({"device": "hp-1", "power": "x"}, "its own meter"),
                       ({"device": "meter-a", "power": "voltage"}, "'voltage' is not a measurement of meter-a"),
                       ({"device": "meter-a"}, "power field, its energy counter")):
        r = client.put("/api/devices/hp-1/energy-meter", json=body)
        assert r.status_code == 422 and frag in json.dumps(r.json()), (body, r.text)


def test_calculated_fields_read_devices_with_hyphens():
    from multibus import expressions as ex
    ok, err, refs = ex.validate_expression("samsung-dvm.heat / (meter-a.power / 1000)")
    assert ok and refs == ["meter-a.power", "samsung-dvm.heat"]
    vals = {"samsung-dvm.heat": 9.0, "meter-a.power": 3000.0}
    assert ex.evaluate_tree(ex.compile_expression("samsung-dvm.heat / (meter-a.power / 1000)"),
                            lambda n: vals[ex.unprepare_ref(n)]) == 3.0
    assert ex.evaluate("a - b", lambda n: {"a": 5, "b": 2}[n]) == 3        # a minus is still a minus


def test_the_heat_pump_cop_uses_the_linked_power():
    from multibus import expressions as ex
    import json as _j
    t = _j.load(open("multibus/device_templates/samsung_mim_b19n.json"))["device_template"]
    cop = next(c for c in t["calculated"] if c["name"] == "cop_estimated")["expr"]
    vals = {"heat_output_estimated": 9.0, "power_electric": 2500.0}
    assert round(ex.evaluate(cop, vals.get), 2) == 3.6
    assert ex.evaluate(cop, {"heat_output_estimated": 9.0, "power_electric": 50.0}.get) == 0   # standby


@needs_tc
def test_the_linked_fields_are_computed_from_the_meter_live(tmp_path):
    import time
    cfg, client = make_app(tmp_path)
    _two_devices(tmp_path, client)
    assert client.put("/api/devices/hp-1/energy-meter",
                      json={"device": "meter-a", "power": "power_active_total",
                            "energy": "energy_active_import"}).status_code == 200
    app = client.app
    ce, reg = app.state.calc_engine, app.state.registry
    assert {e["_reg"].name for e in ce.store["hp-1"]} >= {"power_electric", "energy_electric"}
    meter = reg.ensure_store("meter-a")
    from datetime import datetime
    now, mono = datetime.now().isoformat(), time.monotonic()
    meter[0] = {"name": "power_active_total", "value": -2.4, "timestamp": now, "mono": mono, "interval": 5}
    meter[2] = {"name": "energy_active_import", "value": 1234.5, "timestamp": now, "mono": mono, "interval": 5}
    hp = reg.ensure_store("hp-1")
    # the device's own poll no longer computes them: the meter's report does
    ce.run("hp-1", "normal", hp, topic_prefix="mbg/devices/hp-1", bucket=None, device_tag=None,
           device_id="hp-1", mqtt_on=False, influx_on=False)
    assert "power_electric" not in {v.get("name") for v in hp.values()}
    cfg.get_device("hp-1").enabled = True            # stored disabled: no client dials a test address
    batch = ce.run_linked("hp-1", hp, force=True, topic_prefix="mbg/devices/hp-1", bucket=None,
                          device_tag=None, device_id="hp-1", mqtt_on=False, influx_on=False)
    vals = {v.get("name"): v.get("value") for v in hp.values()}
    assert vals["power_electric"] == 2400            # kW → W, a reversed meter read positive
    assert vals["energy_electric"] == 1234.5
    # published with the device's own group, as when its poll computed them
    assert {it["register"].poll_group for it in batch.values()} == {"normal"}
    assert "hp-1" in ce.dependents("meter-a")


def test_first_takes_the_meter_when_there_is_one_else_the_estimate():
    from multibus import expressions as ex
    assert ex.validate_expression("first(power_electric, power_electric_estimated)")[0]
    assert ex.validate_expression("first(x)")[1] == "first() takes at least 2 arguments, got 1"
    assert ex.evaluate("first(a, b)", {"b": 2}.get) == 2
    assert ex.evaluate("first(a, b)", {"a": 1, "b": 2}.get) == 1
    import pytest
    with pytest.raises(ex.MissingValue):
        ex.evaluate("first(a, c)", {"b": 2}.get)


def test_the_heat_pump_cop_falls_back_to_the_current_estimate():
    from multibus import expressions as ex
    import json as _j
    t = _j.load(open("multibus/device_templates/samsung_mim_b19n.json"))["device_template"]
    c = {x["name"]: x["expr"] for x in t["calculated"]}
    v = {"compressor_current": 13.2, "compressor_frequency": 110, "water_flow_assumed": 30, "iu0_water_delta_t": 12.0}
    v["power_electric_estimated"] = ex.evaluate(c["power_electric_estimated"], v.get)
    v["heat_output_estimated"] = ex.evaluate(c["heat_output_estimated"], v.get)
    assert round(v["power_electric_estimated"]) == 8688                  # √3 × 400 × 13.2 × 0.95
    assert round(ex.evaluate(c["cop_estimated"], v.get), 2) == 2.89     # no meter: the estimate
    v["power_electric"] = 9000
    assert round(ex.evaluate(c["cop_estimated"], v.get), 2) == 2.79     # a meter wins
