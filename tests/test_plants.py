# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Plants (F0.3): one template + one endpoint + N unit ids → N materialized
devices, each with its own socket, managed THROUGH the plant.

Covers: config expansion (ids, substitutions, invalid entries), persistence
(plants stay in `plants:`, never leak into `devices:`), the CRUD guards on
materialized devices, the /api/plants routes, and boot-time template seeding.
"""
import json

import pytest

from multibus.config import Config

from tests.test_devices import write_config

try:
    from fastapi.testclient import TestClient
    HAVE_TC = True
except Exception:  # pragma: no cover
    HAVE_TC = False

needs_tc = pytest.mark.skipif(not HAVE_TC, reason="fastapi testclient unavailable")


PLANT_YAML = """
plants:
  - id: fronius
    name: Fronius PV
    connection: { protocol: tcp, host: 192.0.2.9, port: 502 }
    units: [1, 2, {unit_id: 3, id: inv3, name: East roof}]
    mqtt: { topic_prefix: "mbg/fronius/inverter/${unit_id}", ha_discovery: false }
    influxdb: { bucket: fronius_shadow, device_tag: "inverter_${unit_id}" }
"""


# ── config-level expansion ───────────────────────────────────────────────────

def test_plant_expands_to_one_device_per_unit(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    made = cfg.plant_devices("fronius")
    assert [d.id for d in made] == ["fronius-u1", "fronius-u2", "inv3"]
    assert [d.connection.unit_id for d in made] == [1, 2, 3]
    assert all(d.plant_id == "fronius" for d in made)
    # every unit gets its OWN connection (socket-per-device is the point)
    assert made[0].connection is not made[1].connection
    assert made[0].connection.host == "192.0.2.9"


def test_plant_substitutions_and_defaults(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    u1, _u2, inv3 = cfg.plant_devices("fronius")
    assert u1.mqtt_topic_prefix == "mbg/fronius/inverter/1"
    assert u1.influxdb_bucket == "fronius_shadow"
    assert u1.influxdb_device_tag == "inverter_1"
    assert u1.name == "Fronius PV unit 1"
    assert u1.ha_discovery_enabled is False
    # per-unit overrides win
    assert inv3.id == "inv3" and inv3.name == "East roof"
    assert inv3.mqtt_topic_prefix == "mbg/fronius/inverter/3"


def test_plant_invalid_units_are_skipped_not_fatal(tmp_path):
    cfg = write_config(tmp_path, extra_yaml="""
plants:
  - id: p1
    connection: { protocol: tcp, host: 192.0.2.9 }
    units: [1, 1, "x", 999, 2]
""")
    # duplicate 1, non-numeric, out-of-range are skipped; 1 and 2 survive
    assert [d.connection.unit_id for d in cfg.plant_devices("p1")] == [1, 2]


def test_disabled_plant_materializes_disabled_units(tmp_path):
    cfg = write_config(tmp_path, extra_yaml="""
plants:
  - id: p1
    enabled: false
    connection: { protocol: tcp, host: 192.0.2.9 }
    units: [1, 2]
""")
    made = cfg.plant_devices("p1")
    assert len(made) == 2 and all(not d.enabled for d in made)


def test_plant_persistence_roundtrip_never_leaks_into_devices(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    cfg.save_yaml_config()
    import yaml as _yaml
    doc = _yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert len(doc["plants"]) == 1
    assert "devices" not in doc          # materialized units are derived
    cfg2 = Config(str(tmp_path / "config.yaml"))
    assert [d.id for d in cfg2.plant_devices("fronius")] == \
        ["fronius-u1", "fronius-u2", "inv3"]


def test_device_crud_refuses_plant_materialized_ids(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    with pytest.raises(ValueError, match="materialized from plant"):
        cfg.upsert_raw_device({"id": "inv3",
                               "connection": {"protocol": "tcp", "host": "x"}})


def test_delete_plant_removes_all_units(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    removed = cfg.delete_plant("fronius")
    assert removed == ["fronius-u1", "fronius-u2", "inv3"]
    assert cfg.plant_devices("fronius") == []
    assert cfg.get_raw_plant("fronius") is None
    with pytest.raises(ValueError):
        cfg.delete_plant("fronius")


def test_plant_id_collision_with_device_is_skipped(tmp_path):
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: inv3
    connection: { protocol: tcp, host: 192.0.2.10 }
    enabled: false
plants:
  - id: p1
    connection: { protocol: tcp, host: 192.0.2.9 }
    units: [1, {unit_id: 3, id: inv3}]
""")
    # the explicit devices[] entry wins; the colliding unit is skipped
    assert cfg.get_device("inv3").plant_id == ""
    assert [d.id for d in cfg.plant_devices("p1")] == ["p1-u1"]


# ── API routes ───────────────────────────────────────────────────────────────

def make_app(tmp_path, extra_yaml=""):
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=extra_yaml)
    devices = [(d, None) for d in cfg.devices]
    app, _ = create_api(cfg, None, None, None, devices=devices)
    return cfg, TestClient(app, raise_server_exceptions=False)


PLANT_BODY = {
    "id": "sdm-plant", "name": "SDM bank",
    "template": "eastron_sdm120",
    "enabled": False,                      # no live clients in tests
    "connection": {"protocol": "tcp", "host": "192.0.2.20", "port": 502},
    "units": [1, 2],
    "influxdb": {"bucket": "sdm_shadow", "device_tag": "sdm_${unit_id}"},
}


@needs_tc
def test_create_plant_materializes_seeds_and_lists(tmp_path):
    cfg, client = make_app(tmp_path)
    r = client.post("/api/plants", json=PLANT_BODY)
    assert r.status_code == 200, r.text
    out = r.json()
    assert [d["id"] for d in out["devices"]] == ["sdm-plant-u1", "sdm-plant-u2"]
    # template seeding ran per unit (bundled sdm120 has curated defaults)
    for did in ("sdm-plant-u1", "sdm-plant-u2"):
        f = tmp_path / "devices" / did / "selected_registers.json"
        assert f.exists()
        assert len(json.loads(f.read_text())["registers"]) > 0
    # list + detail
    plants = client.get("/api/plants").json()["plants"]
    assert len(plants) == 1 and plants[0]["total_units"] == 2
    got = client.get("/api/plants/sdm-plant").json()
    assert [u["device_id"] for u in got["units"]] == \
        ["sdm-plant-u1", "sdm-plant-u2"]
    # materialized units appear in the device list, tagged with their plant
    devs = {d["id"]: d for d in client.get("/api/devices").json()["devices"]}
    assert devs["sdm-plant-u1"]["plant_id"] == "sdm-plant"


@needs_tc
def test_plant_validation_errors(tmp_path):
    _cfg, client = make_app(tmp_path)
    bad = dict(PLANT_BODY, connection={"protocol": "tcp"})    # no host
    assert client.post("/api/plants", json=bad).status_code == 422
    bad = dict(PLANT_BODY, units=[])
    assert client.post("/api/plants", json=bad).status_code == 422
    bad = dict(PLANT_BODY, template="no_such_template")
    assert client.post("/api/plants", json=bad).status_code == 422
    bad = dict(PLANT_BODY, connection={"protocol": "rtu", "serial_port": "/dev/ttyUSB0"})
    assert client.post("/api/plants", json=bad).status_code == 422
    # duplicate id after a successful create
    assert client.post("/api/plants", json=PLANT_BODY).status_code == 200
    assert client.post("/api/plants", json=PLANT_BODY).status_code == 422


@needs_tc
def test_device_routes_refuse_plant_units(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert client.post("/api/plants", json=PLANT_BODY).status_code == 200
    r = client.delete("/api/devices/sdm-plant-u1")
    assert r.status_code == 422
    assert "plant" in r.text
    r = client.put("/api/devices/sdm-plant-u1",
                   json={"connection": {"protocol": "tcp", "host": "192.0.2.30"}})
    assert r.status_code == 422
    assert "plant" in r.text


@needs_tc
def test_update_plant_swaps_units(tmp_path):
    cfg, client = make_app(tmp_path)
    assert client.post("/api/plants", json=PLANT_BODY).status_code == 200
    body = dict(PLANT_BODY, units=[2, 3])
    r = client.put("/api/plants/sdm-plant", json=body)
    assert r.status_code == 200, r.text
    ids = [d.id for d in cfg.plant_devices("sdm-plant")]
    assert ids == ["sdm-plant-u2", "sdm-plant-u3"]
    devs = {d["id"] for d in client.get("/api/devices").json()["devices"]}
    assert "sdm-plant-u1" not in devs and "sdm-plant-u3" in devs


@needs_tc
def test_delete_plant_removes_units_keeps_register_files(tmp_path):
    cfg, client = make_app(tmp_path)
    assert client.post("/api/plants", json=PLANT_BODY).status_code == 200
    r = client.delete("/api/plants/sdm-plant")
    assert r.status_code == 200
    assert r.json()["removed_devices"] == ["sdm-plant-u1", "sdm-plant-u2"]
    assert client.get("/api/plants").json()["plants"] == []
    devs = {d["id"] for d in client.get("/api/devices").json()["devices"]}
    assert not any(d.startswith("sdm-plant") for d in devs)
    # register files stay on disk → a re-add restores the selection
    assert (tmp_path / "devices" / "sdm-plant-u1" / "selected_registers.json").exists()
    assert client.delete("/api/plants/sdm-plant").status_code == 404


# ── boot-time seeding (main.py path) ─────────────────────────────────────────

def test_boot_seeding_for_plant_devices(tmp_path):
    """A plant written straight into config.yaml (no API involved) seeds its
    units' register selections from the template at boot."""
    from multibus.device_seed import autoselect_template_registers
    from multibus.device_template import TemplateRegistry
    cfg = write_config(tmp_path, extra_yaml="""
plants:
  - id: p1
    template: eastron_sdm120
    connection: { protocol: tcp, host: 192.0.2.9 }
    units: [1, 2]
""")
    reg = TemplateRegistry(user_dir=tmp_path / "device_templates")
    for dev in cfg.plant_devices("p1"):
        autoselect_template_registers(cfg, reg, dev)
        regs, _g = cfg.load_device_registers(dev)
        assert regs, dev.id
    # idempotent: a second boot keeps the file (no re-seed churn)
    before = (tmp_path / "devices" / "p1-u1" / "selected_registers.json").read_text()
    autoselect_template_registers(cfg, reg, cfg.plant_devices("p1")[0])
    assert (tmp_path / "devices" / "p1-u1" /
            "selected_registers.json").read_text() == before
