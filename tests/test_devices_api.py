# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Tier 2 Phase B: devices CRUD API, template library API, per-device
registers catalog/selection routing."""
import json
import pathlib

import pytest

from multibus.api import create_api
from multibus.config import Config

try:
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False

needs_tc = pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")


def make_app(tmp_path, extra_yaml=""):
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml=extra_yaml)
    devices = [(d, None) for d in cfg.devices]      # no live clients in tests
    app, _ = create_api(cfg, None, None, None, devices=devices)
    return cfg, TestClient(app, raise_server_exceptions=False)


@needs_tc
def test_canonical_fields_endpoint(tmp_path):
    """The register editor's canonical-guidance source: the endpoint mirrors the
    dictionary module exactly (name -> measurement + hierarchical MQTT topic)."""
    from multibus.canonical_fields import CANONICAL_FIELDS
    _cfg, client = make_app(tmp_path)
    fields = client.get("/api/canonical-fields").json()["fields"]
    assert len(fields) == len(CANONICAL_FIELDS)
    assert fields["voltage_l1_n"]["mqtt_topic"] == "voltage/l1_n"
    assert fields["voltage_l1_n"]["measurement"] == "voltage"
    assert fields["power_active_total"]["mqtt_topic"] == "power/active/total"
    # the fields added during the vendor-map roll-out are exposed too
    assert "energy_active_total" in fields and "current_avg" in fields


@needs_tc
def test_canonical_guess_endpoint(tmp_path):
    """The 'auto-canonicalize' HTTP path: cryptic vendor names in, canonical
    names out (null where unsure), aligned by index."""
    _cfg, client = make_app(tmp_path)
    regs = [
        {"name": "V_L1", "label": "Voltage L1-N", "unit": "V"},
        {"name": "P_total", "label": "Active power total", "unit": "W"},
        {"name": "Import_kWh", "label": "Active energy import", "unit": "kWh"},
        {"name": "V", "label": "Voltage", "unit": "V"},          # bare → ambiguous → None
        {"name": "Xowef", "label": "Some vendor thing", "unit": ""},  # unclassifiable → None
    ]
    got = client.post("/api/canonical-fields/guess", json={"registers": regs}).json()["guesses"]
    assert got == ["voltage_l1_n", "power_active_total", "energy_active_import", None, None]
    # a non-dict item yields None (never dropped) so guesses stay index-aligned
    aligned = client.post("/api/canonical-fields/guess", json={"registers": [
        {"name": "V_L1", "label": "Voltage L1-N", "unit": "V"}, "junk"]}).json()["guesses"]
    assert aligned == ["voltage_l1_n", None]


@needs_tc
def test_devices_crud_roundtrip(tmp_path):
    cfg, client = make_app(tmp_path)

    # list: primary present
    devices = client.get("/api/devices").json()["devices"]
    assert [d["id"] for d in devices] == ["umg512"]
    assert devices[0]["primary"] and devices[0]["selected_registers"] == 1

    # create (disabled → no client thread in tests)
    payload = {"id": "em24-hala", "name": "Warehouse EM24", "enabled": False,
               "connection": {"protocol": "tcp", "host": "192.0.2.10", "port": 1502,
                              "unit_id": 5},
               "mqtt": {"topic_prefix": "meters/em24-hala"},
               "influxdb": {"bucket": "warehouse"}}
    r = client.post("/api/devices", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["device"]["id"] == "em24-hala"

    # persisted to config.yaml and reloadable
    reloaded = Config(str(tmp_path / "config.yaml"))
    dev = reloaded.get_device("em24-hala")
    assert dev and dev.influxdb_bucket == "warehouse"
    assert dev.mqtt_topic_prefix == "meters/em24-hala"

    # update
    payload["name"] = "EM24 Hala 2"
    r = client.put("/api/devices/em24-hala", json=payload)
    assert r.status_code == 200 and r.json()["device"]["name"] == "EM24 Hala 2"

    # duplicate id rejected; primary can't be deleted
    r = client.post("/api/devices", json=payload)
    assert r.status_code == 422 and "already exists" in json.dumps(r.json())
    assert client.delete("/api/devices/umg512").status_code == 422

    # primary IS editable now: its connection updates, routing identity fixed
    r = client.put("/api/devices/umg512", json={"connection": {"protocol": "tcp",
                   "host": "10.9.9.9", "port": 5020, "unit_id": 9}})
    assert r.status_code == 200, r.text
    d1 = r.json()["device"]
    assert d1["connection"]["host"] == "10.9.9.9" and d1["connection"]["port"] == 5020
    assert d1["mqtt_topic_prefix"] == "janitza/umg512"      # routing identity unchanged
    assert d1["influxdb_bucket"] == "janitza"
    assert d1["influxdb_device_tag"] == "janitza_umg512"

    # delete
    assert client.delete("/api/devices/em24-hala").json()["status"] == "deleted"
    assert [d["id"] for d in client.get("/api/devices").json()["devices"]] == ["umg512"]
    assert Config(str(tmp_path / "config.yaml")).get_device("em24-hala") is None


@needs_tc
def test_device_validation_errors(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/devices", json={
        "id": "Bad Id!", "template": "nope",
        "connection": {"protocol": "tcp", "host": "", "port": 99999, "unit_id": 999},
    })
    assert r.status_code == 422
    errors = json.dumps(r.json())
    for frag in ("id:", "template:", "connection.host", "connection.port",
                 "connection.unit_id"):
        assert frag in errors


@needs_tc
def test_template_library_endpoints(tmp_path):
    _cfg, client = make_app(tmp_path)
    templates = client.get("/api/device-templates").json()["templates"]
    ids = [t["id"] for t in templates]
    assert "janitza_umg512_pro" in ids
    full = client.get("/api/device-templates/janitza_umg512_pro").json()
    assert full["device_template"]["builtin"] is True
    assert len(full["device_template"]["registers"]) > 4000
    assert client.get("/api/device-templates/nope").status_code == 404


@needs_tc
def test_per_device_catalog_and_selection(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml="""
devices:
  - id: em24
    template: janitza_umg512_pro
    enabled: false
    connection: { protocol: tcp, host: 192.0.2.10 }
""")
    # catalog for a non-primary device comes from its TEMPLATE, same shape
    cat = client.get("/api/registers/all?device=em24").json()
    assert "measurements" in cat
    total = sum(len(c.get("entries", [])) for c in cat["measurements"].values())
    assert total > 4000
    assert cat["device_template"]["id"] == "janitza_umg512_pro"
    # Tier 2: the primary now ALSO draws its catalog from its device template
    # (uniform model), not the fixed modbus_data.json — same shape as any device.
    primary = client.get("/api/registers/all?device=umg512").json()
    assert primary["device_template"]["id"] == "janitza_umg512_pro"

    # selection: save to the device's own file, primary untouched
    sel = [{"address": 19000, "name": "_G_ULN[0]", "label": "L1", "unit": "V",
            "data_type": "float", "poll_group": "realtime",
            "mqtt_enabled": True, "mqtt_topic": "voltage/l1_n",
            "influxdb_enabled": True, "influxdb_measurement": "voltage",
            "influxdb_tags": {}, "ui_show_on_dashboard": True,
            "ui_widget": "value", "ui_config": {}, "thresholds": None}]
    r = client.post("/api/registers/selected?device=em24", json=sel)
    assert r.status_code == 200 and r.json()["device"] == "em24"
    per_dev = tmp_path / "devices" / "em24" / "selected_registers.json"
    assert per_dev.exists()
    got = client.get("/api/registers/selected?device=em24").json()
    assert len(got["registers"]) == 1
    # legacy selection unchanged (still the 1 register from write_config)
    legacy_sel = client.get("/api/registers/selected").json()
    assert len(legacy_sel["registers"]) == 1
    assert legacy_sel["registers"][0]["mqtt_topic"] == "voltage/l1_n"


@needs_tc
def test_template_save_upload_export_delete(tmp_path, monkeypatch):
    # user templates land in a relative config/ dir — isolate it
    import multibus.device_template as dt
    monkeypatch.setattr(dt, 'USER_DIR', tmp_path / 'user_templates')
    _cfg, client = make_app(tmp_path)

    tpl = {"device_template": {
        "schema_version": 1, "id": "cg_em24", "name": "Carlo Gavazzi EM24",
        "poll_groups": {"normal": {"interval": 5}},
        "categories": {"basic": {"label": "Basic", "order": 1}},
        "registers": [{"address": 0, "name": "V_L1", "unit": "V",
                       "data_type": "int32", "category": "basic"}]}}
    # save (create)
    r = client.post("/api/device-templates", json=tpl)
    assert r.status_code == 200, r.text
    assert r.json()["template"]["id"] == "cg_em24"
    # appears in the library, not builtin
    lib = {t["id"]: t for t in client.get("/api/device-templates").json()["templates"]}
    assert lib["cg_em24"]["builtin"] is False and lib["cg_em24"]["used_by"] == []
    # validation errors are row-level
    bad = {"device_template": {**tpl["device_template"],
                               "registers": [{"address": 99999, "name": "X",
                                              "data_type": "nope", "category": "basic"}]}}
    r = client.post("/api/device-templates", json=bad)
    assert r.status_code == 422 and "0..65535" in json.dumps(r.json())
    # builtin ids shielded
    shield = {"device_template": {**tpl["device_template"], "id": "janitza_umg512_pro"}}
    assert client.post("/api/device-templates", json=shield).status_code == 422
    # upload conflict → 409, overwrite works
    r = client.post("/api/device-templates/upload", json={"template": tpl})
    assert r.status_code == 409
    r = client.post("/api/device-templates/upload", json={"template": tpl, "overwrite": True})
    assert r.status_code == 200
    # export round-trips
    r = client.get("/api/device-templates/cg_em24/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert r.json()["device_template"]["id"] == "cg_em24"
    # delete guard: assign to a device → blocked; unassign → ok
    dev = {"id": "em24-x", "template": "cg_em24", "enabled": False,
           "connection": {"protocol": "tcp", "host": "192.0.2.9"}}
    assert client.post("/api/devices", json=dev).status_code == 200
    r = client.delete("/api/device-templates/cg_em24")
    assert r.status_code == 422 and "in use" in json.dumps(r.json())
    assert client.delete("/api/devices/em24-x").status_code == 200
    assert client.delete("/api/device-templates/cg_em24").json()["status"] == "deleted"
    assert client.delete("/api/device-templates/janitza_umg512_pro").status_code == 422


@needs_tc
def test_config_export_import_roundtrip(tmp_path, monkeypatch):
    import io
    import zipfile
    import multibus.device_template as dt
    monkeypatch.setattr(dt, 'USER_DIR', tmp_path / 'device_templates')
    cfg, client = make_app(tmp_path, extra_yaml="""
devices:
  - id: em24
    template: janitza_umg512_pro
    enabled: false
    connection: { protocol: tcp, host: 192.0.2.9 }
    influxdb: { bucket: warehouse }
""")
    # export (secrets stripped by default)
    r = client.get("/api/config/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "config.yaml" in names and "manifest.json" in names
    import yaml as y
    exported = y.safe_load(zf.read("config.yaml"))
    assert "devices" in exported and exported["devices"][0]["id"] == "em24"
    assert "password" not in exported.get("mqtt", {})     # secret stripped

    # mutate: delete the extra device, then restore from the backup
    assert client.delete("/api/devices/em24").status_code == 200
    assert cfg.get_device("em24") is None
    r = client.post("/api/config/import?apply=false",
                    content=r.content, headers={"Content-Type": "application/zip"})
    assert r.status_code == 200, r.text
    assert r.json()["config"] is True
    # em24 came back
    assert cfg.get_device("em24") is not None
    assert cfg.get_device("em24").influxdb_bucket == "warehouse"

    # traversal guard
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil.yaml", "x: 1")
    r = client.post("/api/config/import", content=bad.getvalue(),
                    headers={"Content-Type": "application/zip"})
    assert r.status_code == 422 and "unsafe path" in json.dumps(r.json())


@needs_tc
def test_non_primary_ha_discovery_namespaced(tmp_path, monkeypatch):
    import multibus.device_template as dt
    monkeypatch.setattr(dt, 'USER_DIR', tmp_path / 'device_templates')
    from multibus.mqtt_publisher import MQTTPublisher
    from multibus.config import MQTTConfig, SelectedRegister
    pub = MQTTPublisher(MQTTConfig(enabled=False, topic_prefix="janitza/umg512",
                                   ha_discovery_enabled=True), [], publish_mode="changed")
    published = {}
    pub.connected = True
    pub._publish = lambda topic, payload, retain=None: published.__setitem__(topic, payload) or True
    reg = SelectedRegister(address=100, name="_V1", label="Voltage", unit="V",
                           data_type="float", poll_group="normal")
    n = pub.publish_device_discovery("em24", "Warehouse EM24", "meters/em24", [reg], model="cg_em24")
    assert n == 2                                       # the register sensor + a connectivity binary_sensor
    import json as _j
    topic = next(t for t in published if "/sensor/" in t)
    cfg = _j.loads(published[topic])
    # namespaced so it never collides with device #1
    assert "mbg_dev_em24" in topic
    assert cfg["unique_id"] == "mbg_dev_em24_100__v1"
    assert cfg["state_topic"] == "meters/em24/_v1"
    assert cfg["device"]["identifiers"] == ["mbg_dev_em24"]
    assert cfg["device"]["via_device"] == "janitza_umg512"
    assert cfg["availability_topic"] == "janitza/umg512/status"
    # and the per-device connectivity binary_sensor
    bs_topic = next(t for t in published if "/binary_sensor/" in t)
    assert _j.loads(published[bs_topic])["device_class"] == "connectivity"


@needs_tc
def test_ha_discovery_clears_removed_registers(tmp_path):
    """Re-publishing discovery after a register is removed must clear its
    retained config (empty payload) so HA doesn't keep a ghost sensor."""
    from multibus.mqtt_publisher import MQTTPublisher
    from multibus.config import MQTTConfig, SelectedRegister
    pub = MQTTPublisher(MQTTConfig(enabled=False, topic_prefix="meters/em24",
                                   ha_discovery_enabled=True), [], publish_mode="changed")
    published = {}
    pub.connected = True
    pub._publish = lambda topic, payload, retain=None: published.__setitem__(topic, payload) or True
    a = SelectedRegister(address=100, name="V1", label="V1", unit="V",
                         data_type="float", poll_group="normal")
    b = SelectedRegister(address=102, name="V2", label="V2", unit="V",
                         data_type="float", poll_group="normal")
    pub.publish_device_discovery("em24", "EM24", "meters/em24", [a, b])
    b_topic = next(t for t in published if t.endswith("102_v2/config"))
    assert published[b_topic] != ""                       # b's config was set
    published.clear()
    # b removed → its retained config must be cleared with an empty payload
    pub.publish_device_discovery("em24", "EM24", "meters/em24", [a])
    assert published.get(b_topic) == ""                   # ghost sensor deleted
    assert not any(t.endswith("100_v1/config") and p == "" for t, p in published.items())  # a kept


@needs_tc
def test_values_per_device(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml="""
devices:
  - id: em24
    template: janitza_umg512_pro
    enabled: false
    connection: { protocol: tcp, host: 192.0.2.9 }
""")
    r = client.get("/api/values?device=em24").json()
    assert r["device"] == "em24" and r["values"] == {}   # not polling in tests
    r2 = client.get("/api/values").json()
    assert r2["device"] == "umg512"


@needs_tc
def test_health_http_codes(tmp_path):
    """/health returns 503 ONLY when an enabled virtual meter is down; a stale
    Modbus source degrades the body but stays HTTP 200 (never restart-loop on an
    unreachable meter)."""
    from multibus.api import create_api
    from tests.test_devices import write_config

    class FakeModbus:
        def __init__(self, status): self._s = status
        def data_health(self, threshold=30): return {"status": self._s}
        def get_stats(self): return {"connected": self._s == "ok"}
        def data_health_status(self): return self._s

    def app_with(modbus_status, vmeter_status):
        cfg = write_config(tmp_path)
        app, _ = create_api(cfg, FakeModbus(modbus_status), None, None,
                            devices=[(d, None) for d in cfg.devices])
        app.state.vmeter_manager = type("M", (), {
            "health": staticmethod(lambda: {"status": vmeter_status,
                                            "enabled_meters": 1, "meters": []})})()
        return TestClient(app, raise_server_exceptions=False)

    # all good → 200 ok
    r = app_with("ok", "ok").get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    # modbus stale/down → body degrades but HTTP stays 200
    r = app_with("down", "ok").get("/health")
    assert r.status_code == 200 and r.json()["status"] == "down"
    assert r.json()["modbus"]["status"] == "down"
    # a vmeter down → 503
    r = app_with("ok", "down").get("/health")
    assert r.status_code == 503 and r.json()["status"] == "down"


@needs_tc
def test_device_routing_defaults_and_ha_flag(tmp_path):
    cfg, client = make_app(tmp_path)
    # create with NO topic/bucket → defaults from {device} patterns
    r = client.post("/api/devices", json={"id": "meter-x", "enabled": False,
                    "connection": {"protocol": "tcp", "host": "192.0.2.20"},
                    "ha_discovery_enabled": False})
    assert r.status_code == 200, r.text
    d = r.json()["device"]
    assert d["mqtt_topic_prefix"] == "meters/meter-x"     # default_topic_pattern
    assert d["influxdb_bucket"] == "meter-x"              # default_bucket_pattern
    assert d["ha_discovery_enabled"] is False
    # config endpoints expose the patterns
    m = client.get("/api/config/mqtt").json()
    assert m["default_topic_pattern"] == "meters/{device}"
    i = client.get("/api/config/influxdb").json()
    assert i["default_bucket_pattern"] == "{device}"
    # reload persists the ha flag
    dev = Config(str(tmp_path / "config.yaml")).get_device("meter-x")
    assert dev.ha_discovery_enabled is False


@needs_tc
def test_per_device_sink_toggles(tmp_path):
    cfg, client = make_app(tmp_path)
    # a device that logs to InfluxDB only (no MQTT sink)
    r = client.post("/api/devices", json={"id": "logger", "enabled": False,
                    "connection": {"protocol": "tcp", "host": "192.0.2.30"},
                    "mqtt": {"enabled": False}, "influxdb": {"enabled": True}})
    assert r.status_code == 200, r.text
    d = r.json()["device"]
    assert d["mqtt_enabled"] is False and d["influxdb_enabled"] is True
    # sink status block is exposed per device
    assert d["sinks"]["mqtt"]["enabled"] is False
    assert d["sinks"]["influxdb"]["enabled"] is True
    # flags persist across reload
    dev = Config(str(tmp_path / "config.yaml")).get_device("logger")
    assert dev.mqtt_enabled is False and dev.influxdb_enabled is True
    # the primary is always locked on
    prim = next(x for x in client.get("/api/devices").json()["devices"] if x["primary"])
    assert prim["mqtt_enabled"] is True and prim["influxdb_enabled"] is True


@needs_tc
def test_device_poll_group_intervals(tmp_path):
    cfg, client = make_app(tmp_path)
    r = client.post("/api/devices", json={"id": "m3", "enabled": False,
                    "connection": {"protocol": "tcp", "host": "192.0.2.40"}})
    assert r.status_code == 200, r.text
    # set intervals
    r = client.post("/api/devices/m3/poll-groups",
                    json={"poll_groups": {"realtime": {"interval": 7}, "slow": {"interval": 120}}})
    assert r.status_code == 200, r.text
    # read back
    g = client.get("/api/devices/m3/poll-groups").json()["poll_groups"]
    assert g["realtime"]["interval"] == 7 and g["slow"]["interval"] == 120
    # bad interval rejected
    assert client.post("/api/devices/m3/poll-groups",
                       json={"poll_groups": {"realtime": {"interval": -1}}}).status_code == 400
    # persists across reload
    dev = Config(str(tmp_path / "config.yaml")).get_device("m3")
    _regs, groups = Config(str(tmp_path / "config.yaml")).load_device_registers(dev)
    assert groups["realtime"].interval == 7


@needs_tc
def test_autoselect_uses_curated_defaults(tmp_path):
    """Creating a device with a curated template auto-selects only the
    registers marked with `defaults` (58 for the Janitza map, not 4126)."""
    cfg, client = make_app(tmp_path)
    r = client.post("/api/devices", json={"id": "j2", "enabled": False,
                    "template": "janitza_umg512_pro",
                    "connection": {"protocol": "tcp", "host": "192.0.2.50"}})
    assert r.status_code == 200, r.text
    assert r.json()["device"]["selected_registers"] == 58
    # intervals seeded from the template's poll groups
    g = client.get("/api/devices/j2/poll-groups").json()["poll_groups"]
    assert set(g) >= {"realtime", "normal", "slow"}


@needs_tc
def test_autoselect_applies_canonical_topics_and_measurement(tmp_path):
    """A canonical template seeds each register with its hierarchical MQTT topic
    + canonical InfluxDB measurement from the canonical dictionary, so a new
    device is uniform with every other device out of the box — not the flat
    register name / unit-derived measurement the seed used to hardcode."""
    cfg, client = make_app(tmp_path)
    r = client.post("/api/devices", json={"id": "fr1", "enabled": False,
                    "template": "fronius_smart_meter_65a",
                    "connection": {"protocol": "rtu-tcp", "host": "192.0.2.60",
                                   "port": 502}})
    assert r.status_code == 200, r.text
    regs = client.get("/api/registers/selected?device=fr1").json()["registers"]
    by = {x["name"]: x for x in regs}
    # hierarchical MQTT topic + canonical measurement, not the flat name
    assert by["voltage_l1_n"]["mqtt_topic"] == "voltage/l1_n"
    assert by["voltage_l1_n"]["influxdb_measurement"] == "voltage"
    assert by["power_active_total"]["mqtt_topic"] == "power/active/total"
    assert by["power_active_total"]["influxdb_measurement"] == "power_active"
    # a unitless field the unit-fallback would misfile under 'janitza'
    assert by["power_factor_total"]["influxdb_measurement"] == "power_factor"
    # energy: canonical measurement, not the template's 'energy' category
    assert by["energy_active_import"]["influxdb_measurement"] == "energy_active"
    assert by["serial"]["mqtt_topic"] == "diagnostic/serial"


@needs_tc
def test_scale_roundtrip_and_ssrf_and_csrf(tmp_path):
    cfg, client = make_app(tmp_path)
    # scale survives the selected-registers round-trip
    sel = [{"address": 100, "name": "_X", "label": "X", "unit": "V",
            "data_type": "int16", "poll_group": "normal", "scale": 100,
            "mqtt_enabled": True, "influxdb_enabled": True}]
    assert client.post("/api/registers/selected?device=umg512", json=sel).status_code == 200
    got = client.get("/api/registers/selected?device=umg512").json()["registers"]
    assert got[0]["scale"] == 100
    # SSRF: a public host is rejected (must be private LAN)
    r = client.get("/api/fronius/discover?host=1.1.1.1")
    assert r.status_code == 400 and "LAN" in r.json()["detail"]
    # cloud metadata blocked
    assert client.get("/api/fronius/discover?host=169.254.169.254").status_code == 400
    # CSRF: a cross-site browser POST is blocked
    r = client.post("/api/registers/selected?device=umg512", json=sel,
                    headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


@needs_tc
def test_primary_catalog_comes_from_template_byte_equivalent(tmp_path):
    # Tier 2: the primary's register catalog is now sourced from its device
    # template (janitza_umg512_pro), uniform with every other device. Prove the
    # available-register set is byte-equivalent to the legacy modbus_data.json,
    # so nothing is lost/added. (Polling reads selected_registers.json, which is
    # untouched, so the MQTT/InfluxDB output is byte-identical by construction.)
    _cfg, client = make_app(tmp_path)
    r = client.get("/api/registers/all?device=umg512")
    assert r.status_code == 200
    cat = r.json()
    assert cat.get("device_template", {}).get("id") == "janitza_umg512_pro"
    tmpl_addrs = {e["address"] for c in cat["measurements"].values() for e in c["entries"]}

    md = json.loads(pathlib.Path("docs/modbus_data.json").read_text())
    md_addrs = set()
    for c in md["measurements"].values():
        subs = c.get("subtypes")
        entries = ([e for s in subs.values() for e in s.get("entries", [])]
                   if subs else c.get("entries", []))
        md_addrs |= {e["address"] for e in entries}

    assert tmpl_addrs == md_addrs      # same 4126 addresses — catalog byte-equivalent


@needs_tc
def test_nonprimary_poll_routes_to_its_own_sinks(tmp_path):
    # END-TO-END: a non-primary device's poll must publish to ITS OWN MQTT topic
    # prefix and InfluxDB bucket/tag (the core Tier 2 "no single global sink"
    # promise). create_api wires make_data_callback(dev) onto the device's client,
    # so we invoke that callback and capture where each sink was routed.
    from types import SimpleNamespace
    from multibus.api import create_api
    from tests.test_devices import write_config

    class _CapMQTT:
        connected = True
        config = SimpleNamespace(enabled=True, topic_prefix="janitza/umg512",
                                 ha_discovery_enabled=False)
        def __init__(self): self.routed = []
        def publish_register_data(self, poll_group, data, topic_prefix=None):
            self.routed.append(topic_prefix)
        def __getattr__(self, n): return lambda *a, **k: None

    class _CapInflux:
        config = SimpleNamespace(enabled=True)
        def __init__(self): self.routed = []
        def write_register_data(self, poll_group, data, bucket=None,
                                device_tag=None, device_id=""):
            self.routed.append((bucket, device_tag, device_id))
        def __getattr__(self, n): return lambda *a, **k: None

    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: em24
    template: janitza_umg512_pro
    enabled: true
    connection: { protocol: tcp, host: 192.0.2.9 }
    mqtt: { topic_prefix: "meters/em24" }
    influxdb: { bucket: "warehouse", device_tag: "em24tag" }
""")
    mq, ix = _CapMQTT(), _CapInflux()
    clients = {d.id: SimpleNamespace(publish_callback=None) for d in cfg.devices}
    create_api(cfg, None, mq, ix, devices=[(d, clients[d.id]) for d in cfg.devices])
    reg = SimpleNamespace(name="_V1", label="L1", unit="V",
                          mqtt_enabled=True, influxdb_enabled=True)

    # non-primary → routed to its OWN sinks
    clients["em24"].publish_callback("realtime", {19000: {"value": 230.0, "register": reg}})
    assert mq.routed == ["meters/em24"]                       # MQTT → device's own prefix
    assert ix.routed == [("warehouse", "em24tag", "em24")]    # Influx → device's bucket+tag+id

    # primary → legacy routing (None): publishers fall back to their own global
    # config, so device #1's topics/bucket/tag stay byte-identical to today
    mq.routed.clear(); ix.routed.clear()
    clients["umg512"].publish_callback("realtime", {19000: {"value": 231.0, "register": reg}})
    assert mq.routed == [None]
    assert ix.routed == [(None, None, "")]


@needs_tc
def test_store_stamps_measurement_time_not_callback_time(tmp_path):
    """P0: the value store must carry the driver's measurement time (item['ts']),
    not callback time — the vmeter freshness watchdog reads it, so a slow/retried
    read must not look fresher than it is."""
    from datetime import datetime
    from types import SimpleNamespace
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    clients = {d.id: SimpleNamespace(publish_callback=None) for d in cfg.devices}
    app, _ = create_api(cfg, None, None, None,
                        devices=[(d, clients[d.id]) for d in cfg.devices])
    store = app.state.current_values      # primary device's store (the alias)
    reg = SimpleNamespace(name="_V1", label="L1", unit="V",
                          mqtt_enabled=False, influxdb_enabled=False)
    measured = datetime(2020, 1, 2, 3, 4, 5).timestamp()      # a clearly-old read
    clients["umg512"].publish_callback("realtime",
                                       {19000: {"value": 230.0, "register": reg, "ts": measured}})
    stored = store[19000]["timestamp"]
    assert datetime.fromisoformat(stored).timestamp() == measured   # NOT now()
    # missing ts → falls back to now() (never crashes)
    clients["umg512"].publish_callback("realtime", {19002: {"value": 1.0, "register": reg}})
    assert store[19002]["timestamp"]                                  # present, some ISO


@needs_tc
def test_device_rejects_template_protocol_mismatch(tmp_path):
    # A template's map is transport-specific: a Modbus map on an HTTP device (or
    # vice versa) must be refused, else every read silently resolves to nothing.
    _cfg, client = make_app(tmp_path)
    bad = client.post("/api/devices", json={
        "id": "bad", "enabled": False, "template": "janitza_umg512_pro",
        "connection": {"protocol": "http", "url": "http://192.168.1.5/data"}})
    assert bad.status_code == 422
    assert "MODBUS" in json.dumps(bad.json())        # transport-mismatch message
    ok = client.post("/api/devices", json={
        "id": "ok", "enabled": False, "template": "janitza_umg512_pro",
        "connection": {"protocol": "tcp", "host": "192.168.1.6"}})
    assert ok.status_code == 200, ok.text            # Modbus template on a TCP device is fine


def test_template_transport_classifier():
    from types import SimpleNamespace
    from multibus.device_template import template_transport
    def reg(**k): return SimpleNamespace(json_path=k.get("json_path", ""), address=k.get("address", 0))
    modbus = SimpleNamespace(protocol={"byte_order": "big"}, registers=[reg(address=100)])
    http_declared = SimpleNamespace(protocol={"transports": ["http"]}, registers=[reg(address=0)])
    http_inferred = SimpleNamespace(protocol={}, registers=[reg(json_path="a.b"), reg(json_path="c.d")])
    janitza = SimpleNamespace(protocol={"transports": ["tcp", "rtu"]}, registers=[reg(address=1)])
    assert template_transport(modbus) == "modbus"
    assert template_transport(http_declared) == "http"
    assert template_transport(http_inferred) == "http"
    assert template_transport(janitza) == "modbus"


@needs_tc
def test_nonprimary_routing_locked_on_update(tmp_path):
    # topic/bucket/tag are fixed after creation — an update (even a raw API call
    # that changes them) must keep the stored routing, so history/HA don't orphan.
    _cfg, client = make_app(tmp_path)
    client.post("/api/devices", json={"id": "em24", "enabled": False,
        "connection": {"protocol": "tcp", "host": "192.168.1.9"},
        "mqtt": {"topic_prefix": "meters/em24"},
        "influxdb": {"bucket": "warehouse", "device_tag": "em24tag"}})
    r = client.put("/api/devices/em24", json={"id": "em24", "enabled": False,
        "connection": {"protocol": "tcp", "host": "192.168.1.9"},
        "mqtt": {"topic_prefix": "HACKED/topic"},
        "influxdb": {"bucket": "HACKED", "device_tag": "HACKEDtag"}})
    assert r.status_code == 200, r.text
    d = r.json()["device"]
    assert d["mqtt_topic_prefix"] == "meters/em24"        # unchanged
    assert d["influxdb_bucket"] == "warehouse"
    assert d["influxdb_device_tag"] == "em24tag"


@needs_tc
def test_energy_fields_autodetect_and_select(tmp_path):
    _cfg, client = make_app(tmp_path)
    client.post("/api/devices", json={"id": "em24", "enabled": False,
        "connection": {"protocol": "tcp", "host": "192.168.1.9"}})
    reg = lambda a, n, l, u, pg: {"address": a, "name": n, "label": l, "unit": u,
        "data_type": "uint32", "poll_group": pg, "mqtt_enabled": False, "mqtt_topic": "",
        "influxdb_enabled": True, "influxdb_measurement": "e", "influxdb_tags": {},
        "ui_show_on_dashboard": False, "ui_widget": "value", "ui_config": {}, "thresholds": None}
    client.post("/api/registers/selected?device=em24", json=[
        reg(100, "_imp", "Import", "Wh", "slow"), reg(200, "_v", "Voltage", "V", "realtime")])
    r = client.get("/api/energy/fields?device=em24").json()
    names = [c["name"] for c in r["candidates"]]
    assert "_imp" in names and "_v" not in names          # only the cumulative energy counter
    imp = next(c for c in r["candidates"] if c["name"] == "_imp")
    assert imp["unit"] == "kWh" and imp["div"] == 1000     # Wh presented as kWh
    assert r["selected"] == []                             # nothing picked yet
    client.post("/api/energy/fields?device=em24",
                json={"fields": [{"name": "_imp", "label": "Import", "unit": "kWh", "div": 1000}]})
    assert [f["name"] for f in client.get("/api/energy/fields?device=em24").json()["selected"]] == ["_imp"]


@needs_tc
def test_primary_edit_validates_connection(tmp_path):
    _cfg, client = make_app(tmp_path)
    # a bad port must be rejected (was silently persisted, breaking the primary)
    r = client.put("/api/devices/umg512", json={"connection": {"protocol": "tcp",
                   "host": "10.0.0.9", "port": "abc", "unit_id": 1}})
    assert r.status_code == 422 and "port" in json.dumps(r.json())
    # out-of-range unit id rejected too
    r = client.put("/api/devices/umg512", json={"connection": {"protocol": "tcp",
                   "host": "10.0.0.9", "port": 502, "unit_id": 999}})
    assert r.status_code == 422 and "unit_id" in json.dumps(r.json())
    # a valid edit still works
    r = client.put("/api/devices/umg512", json={"connection": {"protocol": "tcp",
                   "host": "10.0.0.9", "port": 5020, "unit_id": 9}})
    assert r.status_code == 200 and r.json()["device"]["connection"]["port"] == 5020


@needs_tc
def test_status_devices_carry_poll_rate(tmp_path):
    # Regression: devices[] lacked poll_rate → the Status page showed 0.00/s per
    # device while the pipeline header (top-level modbus.poll_rate) said 4.2/s.
    from types import SimpleNamespace
    from tests.test_devices import write_config
    from multibus.api import create_api

    class _Client(SimpleNamespace):
        def get_stats(self):
            return {"connected": True, "poll_rate": 4.22,
                    "successful_reads": 10, "failed_reads": 0,
                    "staleness_age_s": 0.2, "last_latency_ms": 6}
        def data_health(self, *a):
            return {"status": "ok"}

    cfg = write_config(tmp_path)
    fake = _Client(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None,
                        devices=[(d, fake) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    dev = client.get("/api/status").json()["devices"][0]
    assert dev["poll_rate"] == 4.22


# ── P1: upsert_raw_device is transactional (rollback on failed edit) ──────────

def test_upsert_rollback_preserves_device_on_build_failure(tmp_path):
    from multibus.config import Config
    from tests.test_devices import write_config
    cfg_path = write_config(tmp_path, extra_yaml="""
devices:
  - id: keepme
    template: janitza_umg512_pro
    enabled: false
    connection: { protocol: tcp, host: 192.0.2.5 }
""")
    cfg = Config(str(cfg_path.config_path))
    assert cfg.get_device("keepme") is not None
    before = [d.get("id") for d in cfg._raw_devices]
    # force a build failure mid-upsert; the raw list (and the device) must be
    # restored to the pre-edit state, not left with the old entry deleted
    import pytest
    orig_build = cfg._build_devices
    calls = {"n": 0}
    def boom():
        calls["n"] += 1
        if calls["n"] == 1:                        # fail the FIRST build (the edit)
            raise RuntimeError("simulated build failure")
        return orig_build()
    cfg._build_devices = boom
    with pytest.raises(RuntimeError):
        cfg.upsert_raw_device({"id": "keepme",
                               "connection": {"protocol": "tcp", "host": "10.0.0.9"}})
    cfg._build_devices = orig_build
    assert cfg.get_device("keepme") is not None     # NOT deleted (rolled back)
    assert [d.get("id") for d in cfg._raw_devices] == before


@needs_tc
def test_ui_security_validate_then_commit(tmp_path):
    _cfg, client = make_app(tmp_path)
    # enabling auth without a hashed password is rejected AND leaves auth off
    r = client.post("/api/config/ui-security", json={"auth_enabled": True})
    assert r.status_code == 422
    assert client.get("/api/auth/status").json()["enabled"] is False   # not half-applied


# ── ad-hoc probe LAN guard (audit 2026-08-14, L1) ──────────────────────────────

@needs_tc
def test_adhoc_probe_blocks_nonlan_host(tmp_path):
    """/api/devices/test must apply the same LAN-egress policy as the
    /api/discover/* routes — without it the probe is an internal TCP
    port-scanner for any operator (or anyone, on an auth-off box)."""
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/devices/test", json={
        "connection": {"protocol": "tcp", "host": "8.8.8.8", "port": 502},
        "unit_id": 1})
    body = r.json()
    assert body.get("ok") is False
    assert "blocked" in body.get("message", "")


@needs_tc
def test_adhoc_probe_allows_lan_host(tmp_path):
    """A private-LAN host passes the guard (the probe then legitimately
    fails to connect in the test env — but NOT with the 'blocked' message)."""
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/devices/test", json={
        "connection": {"protocol": "tcp", "host": "192.0.2.1", "port": 502,
                       "timeout": 0.2},
        "unit_id": 1})
    body = r.json()
    assert "blocked" not in body.get("message", "")


# ── audit DP-9: identity-collision validation on register save ───────────────

def test_register_identity_hard_rejections():
    """Duplicate (register_type, address) and duplicate names are rejected;
    span overlaps only warn (live maps legitimately read overlapping
    windows — fronius_rtu serves int32@10 alongside uint16@11)."""
    import pytest as _pytest
    from multibus.config import validate_register_identity as v

    base = {'data_type': 'uint16', 'register_type': 'holding'}
    # same (type, address) → reject
    with _pytest.raises(ValueError, match='duplicate address'):
        v([{**base, 'address': 5, 'name': 'a'},
           {**base, 'address': 5, 'name': 'b'}])
    # same address, DIFFERENT register space → allowed
    v([{**base, 'address': 5, 'name': 'a'},
       {**base, 'address': 5, 'name': 'b', 'register_type': 'input'}])
    # duplicate name → reject (vmeter binding + MQTT topic + Influx series)
    with _pytest.raises(ValueError, match='duplicate register name'):
        v([{**base, 'address': 1, 'name': 'power'},
           {**base, 'address': 2, 'name': 'power'}])
    # span overlap → warn only, never raise
    v([{'address': 10, 'name': 'v', 'data_type': 'int32', 'register_type': 'holding'},
       {'address': 11, 'name': 'm', 'data_type': 'uint16', 'register_type': 'holding'}])


def test_canonical_unit_contract_warns_not_rejects(caplog):
    """A canonical energy_* name with a non-canonical unit (kWh) is a WARNING
    (loosening-only rule) — the selection still saves, but the operator is
    told to fix the scale before the 1000x error reaches a vmeter/twin."""
    import logging
    from multibus.config import validate_register_identity as v
    rows = [{'address': 52, 'name': 'energy_active_import', 'unit': 'kWh',
             'data_type': 'int32', 'register_type': 'holding'}]
    with caplog.at_level(logging.WARNING, logger='multibus.config'):
        v(rows)                                        # must NOT raise
    assert any('canonical unit' in r.message for r in caplog.records)
    # matching unit → silent
    caplog.clear()
    rows[0]['unit'] = 'Wh'
    with caplog.at_level(logging.WARNING, logger='multibus.config'):
        v(rows)
    assert not any('canonical unit' in r.message for r in caplog.records)


def test_canonical_unit_and_cumulative_helpers():
    from multibus.canonical_fields import canonical_unit_for, is_cumulative_field
    assert canonical_unit_for('energy_active_import') == 'Wh'
    assert canonical_unit_for('energy_reactive_import') == 'varh'
    assert canonical_unit_for('power_active_total') == 'W'
    assert canonical_unit_for('power_factor_total') is None    # unit-less
    assert canonical_unit_for('not_canonical_name') is None
    assert is_cumulative_field('energy_active_import_l2')
    assert not is_cumulative_field('power_active_total')
    assert not is_cumulative_field('voltage_l1_n')


@needs_tc
def test_ui_config_never_overrides_the_explicit_dashboard_flag(tmp_path):
    """Found while hiding demo dashboard cards: the save path spread
    ``**ui_config`` LAST, so its stale round-tripped copy of
    show_on_dashboard silently overrode the field the caller actually set
    — the flag kept reverting on every save. Explicit fields must win."""
    _cfg, client = make_app(tmp_path)
    sel = client.get("/api/registers/selected").json()["registers"]
    assert sel, "fixture has a selected register"
    reg = sel[0]
    reg["ui_show_on_dashboard"] = False
    reg["ui_config"] = {"show_on_dashboard": True, "widget": "value",
                        "color": "teal"}          # stale copy + a real extra
    assert client.post("/api/registers/selected", json=sel).status_code == 200
    got = client.get("/api/registers/selected").json()["registers"][0]
    assert got["ui_show_on_dashboard"] is False    # the explicit field won
    assert got["ui_config"].get("color") == "teal"  # extras survive


def test_modbus_discovery_guards_allow_loopback_http_guards_do_not():
    """Loopback is legitimate for PURE-MODBUS probes (local simulators,
    probing the gateway's own virtual meters on 127.0.0.1:1502) — a Modbus
    frame cannot exploit an HTTP service. The HTTP-fetch guards keep
    rejecting it: there loopback is SSRF into local services."""
    from multibus import discovery
    from multibus.http_client import _classify_lan

    # pure-Modbus guards: loopback OK, public/link-local still refused
    assert discovery.lan_host_error("127.0.0.1") is None
    assert discovery.lan_host_error("8.8.8.8") is not None
    assert discovery.lan_host_error("169.254.169.254") is not None
    assert discovery.hosts_from_cidr("127.0.0.1/32") == ["127.0.0.1"]
    import pytest as _pytest
    with _pytest.raises(ValueError):
        discovery.hosts_from_cidr("8.8.8.0/30")

    # HTTP guard: loopback still rejected (SSRF-to-self)
    assert _classify_lan(["127.0.0.1"]) is not None
    assert _classify_lan(["192.168.1.50"]) is None
