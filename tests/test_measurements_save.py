# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Saving from the Measurements page keeps what that page does not edit, and
a value nobody publishes any more does not stay on the broker.

Found on a heat pump (2026-10-08): unticking a few rows from Measurements
dropped every row's template section, so temperatures and pressures fell
into InfluxDB's catch-all measurement, and the unticked rows kept their last
value retained on the broker, read by every subscriber as live."""
import json

from tests.test_devices_api import needs_tc


def _app(tmp_path):
    from unittest.mock import MagicMock

    from multibus.api import create_api
    from multibus.config import MQTTConfig
    from multibus.mqtt_publisher import MQTTPublisher
    from tests.test_devices import write_config
    from fastapi.testclient import TestClient
    cfg = write_config(tmp_path)
    pub = MQTTPublisher(MQTTConfig(enabled=False), [])
    pub.connected, pub.client, pub.sent = True, MagicMock(), {}

    def _cap(topic, payload, retain=True):
        pub.sent[topic] = payload
        return True
    pub._publish = _cap
    app, _ = create_api(cfg, None, pub, None, devices=[(d, None) for d in cfg.devices])
    return cfg, pub, TestClient(app, raise_server_exceptions=False)


ROWS = [
    {"address": 6, "name": "outdoor_temp", "label": "Outdoor", "unit": "°C", "data_type": "int16",
     "poll_group": "normal", "scale": 10, "category": "outdoor", "daily": True,
     "aggregates": {"site_outdoor": "avg"}, "mqtt": {"enabled": True, "topic": ""},
     "influxdb": {"enabled": True, "measurement": "", "tags": {}}, "ui": {}},
    {"address": 86, "name": "water_flow", "label": "Flow", "unit": "l/min", "data_type": "uint16",
     "poll_group": "normal", "category": "hydro", "mqtt": {"enabled": True, "topic": ""},
     "influxdb": {"enabled": True, "measurement": "", "tags": {}}, "ui": {}},
]


def _device(cfg, client, tmp_path, template=""):
    r = client.post("/api/devices", json={"id": "hp", "enabled": False, "template": template,
                                          "connection": {"protocol": "tcp", "host": "192.168.1.9"}})
    assert r.status_code in (200, 201), r.text
    f = tmp_path / "devices" / "hp" / "selected_registers.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"version": "1.0", "registers": ROWS, "poll_groups": {}}))
    return f


@needs_tc
def test_unticking_a_row_keeps_the_others_whole_and_clears_its_topic(tmp_path):
    cfg, pub, client = _app(tmp_path)
    f = _device(cfg, client, tmp_path)
    shown = client.get("/api/registers/selected?device=hp").json()["registers"]
    keep = [r for r in shown if r["name"] == "outdoor_temp"]
    pub.sent.clear()
    assert client.post("/api/registers/selected?device=hp", json=keep).status_code == 200
    row = json.loads(f.read_text())["registers"]
    assert [r["name"] for r in row] == ["outdoor_temp"]
    assert row[0]["category"] == "outdoor"                       # the template section
    assert row[0]["aggregates"] == {"site_outdoor": "avg"}       # installation totals
    assert row[0]["daily"] is True
    cleared = [t for t, p in pub.sent.items() if p == ""]
    assert len(cleared) == 1 and cleared[0].endswith("/water_flow"), pub.sent
    # the measurement it lands in follows the kept section
    from multibus.influxdb_publisher import get_measurement
    dev = next(d for d in cfg.devices if d.id == "hp")
    reg = next(r for r in cfg.load_device_registers(dev)[0] if r.name == "outdoor_temp")
    assert get_measurement(reg) == "outdoor"


@needs_tc
def test_a_row_ticked_for_the_first_time_takes_its_section_from_the_template(tmp_path):
    cfg, pub, client = _app(tmp_path)
    f = _device(cfg, client, tmp_path, template="samsung_mim_b19n")
    shown = client.get("/api/registers/selected?device=hp").json()["registers"]
    new = dict(shown[0], address=7, name="pressure_high", label="High pressure", unit="kgf/cm²")
    assert client.post("/api/registers/selected?device=hp", json=shown + [new]).status_code == 200
    rows = {r["name"]: r for r in json.loads(f.read_text())["registers"]}
    assert rows["pressure_high"]["category"] == "compressor"
    assert "aggregates" not in rows["pressure_high"]             # nothing borrowed from a neighbour


@needs_tc
def test_a_deleted_device_takes_its_retained_values_with_it(tmp_path):
    cfg, pub, client = _app(tmp_path)
    _device(cfg, client, tmp_path)
    pub.sent.clear()
    assert client.delete("/api/devices/hp").status_code == 200
    cleared = sorted(t.rsplit("/", 1)[-1] for t, p in pub.sent.items() if p == "" and "/config" not in t)
    assert cleared == ["meta", "outdoor_temp", "water_flow"], pub.sent      # its attributes go too


def test_clearing_a_topic_forgets_its_last_value():
    from unittest.mock import MagicMock

    from multibus.config import MQTTConfig
    from multibus.mqtt_publisher import MQTTPublisher
    pub = MQTTPublisher(MQTTConfig(enabled=False), [])
    pub.connected, pub.client = True, MagicMock()
    sent = []
    pub._publish = lambda t, p, retain=True: sent.append((t, p, retain)) or True
    pub._confirm_publish("x/flow", 0.0)
    assert not pub._should_publish("x/flow", 0.0)
    assert pub.clear_topics({"x/flow"}) == 1 and sent == [("x/flow", "", True)]
    assert pub._should_publish("x/flow", 0.0)                    # ticked again → published at once


@needs_tc
def test_a_unit_of_an_installation_keeps_its_totals_when_saved_without_a_source(tmp_path):
    """The multi-source path (an installation's unit, saved from its page):
    a row's aggregates feed the installation's totals — losing them on a
    dashboard tweak would silently break the bank's sums."""
    from tests.test_unit_dashboard_registers import make_plant_app
    cfg, _app, client = make_plant_app(tmp_path)
    p = cfg.source_registers_path('pv-u1', 'solar_api')
    data = json.loads(p.read_text())
    for r in data["registers"]:
        r.update(category="power", aggregates={"site_power": "sum"})
    p.write_text(json.dumps(data))
    rows = client.get("/api/registers/selected?device=pv-u1").json()["registers"]
    for r in rows:
        r["ui_show_on_dashboard"] = False
    assert client.post("/api/registers/selected?device=pv-u1", json=rows).status_code == 200
    saved = {r["name"]: r for r in json.loads(p.read_text())["registers"]}
    assert saved["power_active_total"]["ui"]["show_on_dashboard"] is False      # the edit landed
    assert saved["power_active_total"]["aggregates"] == {"site_power": "sum"}   # the totals stayed
    assert saved["power_active_total"]["category"] == "power"


@needs_tc
def test_a_save_keeps_the_calculated_fields_energy_fields_and_tuned_intervals(tmp_path):
    """The register file holds more than the selection. A save from
    Measurements rewrote it with the selection only: the heat pump lost its
    water delta T, and a device's tuned poll intervals went back to the
    template's."""
    cfg, pub, client = _app(tmp_path)
    f = _device(cfg, client, tmp_path, template="samsung_mim_b19n")
    data = json.loads(f.read_text())
    data.update(calculated=[{"name": "delta_t", "expr": "outdoor_temp - 1"}],
                energy_fields=[{"name": "e", "label": "E", "unit": "kWh", "div": 1000}],
                poll_groups={"normal": {"interval": 42, "description": "tuned"}})
    f.write_text(json.dumps(data))
    shown = client.get("/api/registers/selected?device=hp").json()["registers"]
    assert client.post("/api/registers/selected?device=hp", json=shown[:1]).status_code == 200
    after = json.loads(f.read_text())
    assert [r["name"] for r in after["registers"]] == [shown[0]["name"]]
    assert after["calculated"] == data["calculated"]
    assert after["energy_fields"] == data["energy_fields"]
    assert after["poll_groups"]["normal"]["interval"] == 42


def test_the_primary_file_keeps_its_other_sections(tmp_path):
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    p = cfg.registers_path
    data = json.loads(p.read_text())
    data["calculated"] = [{"name": "x", "expr": "1"}]
    p.write_text(json.dumps(data))
    cfg.save_selected_registers(data["registers"])
    assert json.loads(p.read_text())["calculated"] == [{"name": "x", "expr": "1"}]
