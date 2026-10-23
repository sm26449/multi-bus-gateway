"""The components of a device: kinds of
things a template says the device holds, each added by instance and read
through the parent's connection."""
import json
from types import SimpleNamespace

import pytest

from multibus import mqtt_input as mi
from multibus import mqtt_link
from multibus.device_template import TemplateRegistry, validate_template, is_component_id

PORTAL = "c0ffee000001"


# ── the template ────────────────────────────────────────────────────────────

def _tpl(**comp):
    c = {"name": "Battery", "kind": "battery",
         "registers": [{"address": 1, "name": "soc", "unit": "%", "data_type": "float",
                        "topic": "N/{portal}/battery/{instance}/Soc", "json_path": "value"}]}
    c.update(comp)
    return {"device_template": {
        "id": "gx_test", "name": "GX", "protocol": {"transports": ["mqtt"]},
        "registers": [{"address": 1, "name": "soc", "data_type": "float",
                       "topic": "N/{portal}/system/0/Soc", "json_path": "value"}],
        "components": {"battery": c}}}


def test_a_component_must_say_instance_in_every_topic():
    assert validate_template(_tpl()) == []
    bad = _tpl()
    bad["device_template"]["components"]["battery"]["registers"][0]["topic"] = "N/{portal}/battery/288/Soc"
    assert any("{instance}" in e for e in validate_template(bad))


def test_a_component_is_read_only_and_found_by_one_level():
    errs = validate_template(_tpl(commands={"x": {}}))
    assert any("commands" in e and "read-only" in e for e in errs)
    errs = validate_template(_tpl(discover={"topic": "N/{portal}/+/+/ProductName"}))
    assert any("exactly one +" in e for e in errs)
    assert validate_template(_tpl(discover={"topic": "N/{portal}/battery/+/ProductName",
                                            "name_topic": "N/{portal}/battery/{instance}/CustomName"})) == []
    modbus = _tpl()
    modbus["device_template"]["protocol"] = {"transports": ["tcp"]}
    assert any("only an MQTT template" in e for e in validate_template(modbus))


def test_the_registry_holds_components_under_the_parent(tmp_path):
    user = tmp_path / "user"
    reg = TemplateRegistry(builtin_dir=tmp_path / "none", user_dir=user)
    t = reg.save_user(_tpl())
    assert reg.get("gx_test:battery").component_of == "gx_test"
    assert [x.id for x in reg.list()] == ["gx_test"]
    assert t.to_dict()["device_template"]["components"]["battery"]["name"] == "Battery"
    # saved again without the component: it goes
    d = _tpl()
    del d["device_template"]["components"]
    reg.save_user(d)
    assert reg.get("gx_test:battery") is None
    reg.save_user(_tpl())
    with pytest.raises(ValueError):
        reg.delete_user("gx_test:battery")             # never the parent's file
    assert (user / "gx_test.json").exists()
    reg.delete_user("gx_test")
    assert reg.get("gx_test:battery") is None and is_component_id("gx_test:battery")


# ── the configuration ───────────────────────────────────────────────────────

GX_YAML = f"""
devices:
  - id: gx
    name: Victron GX
    template: victron_gx_mqtt
    enabled: false
    connection: {{protocol: mqtt, broker: 192.0.2.10, port: 8883, tls: true, username: u, topic: 'N/{PORTAL}/#',
                  password: secret, vars: {{portal: {PORTAL}, vebus: '274'}}}}
    mqtt: {{topic_prefix: mbg/devices/gx, ha_discovery: false}}
    influxdb: {{bucket: victron-gx, device_tag: gx}}
    components:
      - {{type: battery, instance: 288, name: SmartShunt}}
      - {{type: battery, instance: '512', enabled: false}}
      - {{type: temperature, instance: '22', id: ruuvi-in, mqtt: {{topic_prefix: mbg/devices/ruuvi-in}}}}
      - {{type: battery, instance: '288'}}
      - {{type: battery, instance: 'a/b'}}
"""


def _cfg(tmp_path, extra=GX_YAML):
    from tests.test_devices import write_config
    return write_config(tmp_path, extra_yaml=extra)


def test_components_materialize_as_devices_reading_through_the_parent(tmp_path):
    cfg = _cfg(tmp_path)
    comps = {d.id: d for d in cfg.component_devices("gx")}
    assert sorted(comps) == ["gx-battery-288", "gx-battery-512", "ruuvi-in"]   # the repeat and a/b skipped
    b = comps["gx-battery-288"]
    assert (b.template, b.name, b.component_type, b.component_instance) == \
        ("victron_gx_mqtt:battery", "SmartShunt", "battery", "288")
    assert b.mqtt_in["vars"] == {"portal": PORTAL, "vebus": "274", "instance": "288"}
    assert b.mqtt_in["broker"] == "192.0.2.10" and b.mqtt_in["password"] == "secret"
    assert b.mqtt_topic_prefix == "mbg/devices/gx-battery-288"
    assert (b.influxdb_bucket, b.influxdb_device_tag) == ("victron-gx", "gx-battery-288")
    assert b.ha_discovery_enabled is False and b.write_locked is True
    assert b.enabled is False                                          # the parent is off
    assert comps["ruuvi-in"].mqtt_topic_prefix == "mbg/devices/ruuvi-in"
    assert b.summary()["component_of"] == "gx"


def test_a_component_is_managed_through_its_parent(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match="component of"):
        cfg.upsert_raw_device({"id": "gx-battery-288", "template": "x",
                               "connection": {"protocol": "mqtt", "broker": "h"}})
    # its own settings land in its entry, and survive a rebuild
    cfg.set_opcua_output("gx-battery-288", False)
    cfg.set_device_sinks("gx-battery-288", influxdb=False)
    d = cfg.get_device("gx-battery-288")
    assert d.opcua_enabled is False and d.influxdb_enabled is False
    assert cfg.get_device("gx").influxdb_enabled is True
    added = cfg.add_component("gx", {"type": "temperature", "instance": "23", "name": "Ruuvi out"})
    assert added.id == "gx-temperature-23" and added.component_of == "gx"
    cfg.update_component("gx", "gx-temperature-23", {"name": "Outside", "type": "battery"})
    d = cfg.get_device("gx-temperature-23")
    assert d.name == "Outside" and d.component_type == "temperature"     # the type never changes
    cfg.remove_component("gx", "gx-temperature-23")
    assert cfg.get_device("gx-temperature-23") is None
    # the file holds them
    from tests.test_devices import write_config  # noqa: F401
    import yaml
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
    gx = next(d for d in raw["devices"] if d["id"] == "gx")
    assert [c.get("instance") for c in gx["components"]][:3] == [288, "512", "22"]


def test_components_need_an_mqtt_parent(tmp_path):
    cfg = _cfg(tmp_path, GX_YAML.replace("protocol: mqtt", "protocol: tcp, host: 192.0.2.10"))
    assert cfg.component_devices("gx") == []


# ── one connection ──────────────────────────────────────────────────────────

class _Paho:
    def __init__(self):
        self.sent, self.subs, self.unsubs = [], [], []

    def publish(self, topic, payload, qos=0, retain=False):
        self.sent.append((topic, payload))
        return SimpleNamespace(rc=0)

    def subscribe(self, t):
        self.subs.append(t)

    def unsubscribe(self, t):
        self.unsubs.append(t)


@pytest.fixture
def fake_links(monkeypatch):
    def start(self):
        self.client = _Paho()
        return True
    monkeypatch.setattr(mqtt_link.MqttLink, "start", start)
    mqtt_link._pool.clear()
    yield
    mqtt_link._pool.clear()


KA = {"topic": "R/{portal}/keepalive", "first_payload": "", "payload": "k", "every_s": 30}


def _member(link, *topics, keepalive=KA, password="p", **vars_):
    from multibus.config import SelectedRegister
    regs = [SelectedRegister(address=i + 1, name=f"f{i}", label="", unit="", data_type="float",
                             poll_group="normal", json_path="value", topic=t) for i, t in enumerate(topics)]
    m = mi.MqttInputClient({"broker": "gx", "port": 8883, "username": "u", "password": password,
                            "link": link, "vars": {"portal": PORTAL, **vars_}, "keepalive": keepalive}, regs)
    got = []
    m.publish_callback = lambda _pg, data: got.extend(e["value"] for e in data.values())
    return m, got


def _msg(topic, value):
    return SimpleNamespace(topic=topic, payload=json.dumps({"value": value}).encode(), retain=False)


def test_a_parent_and_its_components_share_one_connection(fake_links):
    gx, _ = _member("gx", "N/{portal}/system/0/Soc")
    bat, got = _member("gx", "N/{portal}/battery/{instance}/Soc", instance="288")
    other, _ = _member("other", "N/{portal}/battery/{instance}/Soc", instance="288")
    for m in (gx, bat, other):
        assert m.connect()
    assert gx._link is bat._link and other._link is not gx._link
    link = gx._link
    link._on_connect(link.client, None, None, 0)
    assert gx.connected and bat.connected
    assert sorted(link.client.subs) == [f"N/{PORTAL}/battery/288/Soc", f"N/{PORTAL}/system/0/Soc"]
    # one republish for both
    assert [s for s in link.client.sent if s[0].startswith("R/")] == [(f"R/{PORTAL}/keepalive", "")]
    # a message goes only to whom it is for: the parent never looks alive
    # through its component's traffic
    link._on_message(link.client, None, _msg(f"N/{PORTAL}/battery/288/Soc", 85.0))
    assert got == [85.0] and gx.last_msg_mono is None and bat.last_msg_mono is not None


def test_a_late_member_asks_for_everything_and_topics_are_counted(fake_links):
    gx, _ = _member("gx", "N/{portal}/system/0/Soc", "N/{portal}/shared")
    gx.connect()
    link = gx._link
    link._on_connect(link.client, None, None, 0)
    n = len(link.client.sent)
    bat, _ = _member("gx", "N/{portal}/battery/{instance}/Soc", "N/{portal}/shared", instance="512")
    bat.connect()
    assert bat.connected and link.client.sent[n:] == [(f"R/{PORTAL}/keepalive", "")]
    assert link.client.subs.count(f"N/{PORTAL}/shared") == 1
    bat.disconnect()
    assert link.client.unsubs == [f"N/{PORTAL}/battery/512/Soc"]          # the shared topic stays
    assert mqtt_link.links() == [link]
    gx.disconnect()
    assert mqtt_link.links() == [] and link.client is None


def test_a_live_register_change_goes_through_the_count(fake_links):
    gx, _ = _member("gx", "N/{portal}/a")
    bat, _ = _member("gx", "N/{portal}/a", "N/{portal}/b")
    gx.connect(); bat.connect()
    link = gx._link
    link._on_connect(link.client, None, None, 0)
    n = len(link.client.subs)
    bat.update_registers(gx.registers)                     # bat drops b, keeps a
    assert link.client.unsubs == [f"N/{PORTAL}/b"]
    assert len(link.client.subs) == n                      # nothing subscribed again
    # a command goes out on the shared connection
    assert gx.publish("W/x/a", "1") == (True, "") and link.client.sent[-1] == ("W/x/a", "1")


def test_another_password_is_another_connection(fake_links):
    a, _ = _member("gx", "N/{portal}/a")
    b, _ = _member("gx", "N/{portal}/a", password="new")
    a.connect(); b.connect()
    assert a._link is not b._link


def test_the_link_keeps_the_keepalive(fake_links):
    gx, _ = _member("gx", "N/{portal}/a")
    bat, _ = _member("gx", "N/{portal}/b")
    gx.connect(); bat.connect()
    link = gx._link
    link._on_connect(link.client, None, None, 0)
    link._ka[f"R/{PORTAL}/keepalive"]["last"] = 0.0
    assert link._send(f"R/{PORTAL}/keepalive")
    assert link.client.sent[-1] == (f"R/{PORTAL}/keepalive", "k")
    assert gx.keepalives_sent == bat.keepalives_sent == 2         # one republish + one ask


# ── the API ─────────────────────────────────────────────────────────────────

try:
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False
needs_tc = pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")


def _app(tmp_path, extra=GX_YAML):
    from multibus.api import create_api
    cfg = _cfg(tmp_path, extra)
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    return cfg, TestClient(app, raise_server_exceptions=False)


@needs_tc
def test_listing_adding_renaming_and_removing_components(tmp_path):
    cfg, c = _app(tmp_path)
    r = c.get("/api/devices/gx/components").json()
    assert [t["type"] for t in r["types"]] == ["battery", "temperature"] and r["types"][0]["discover"]
    assert {x["id"] for x in r["components"]} == {"gx-battery-288", "gx-battery-512", "ruuvi-in"}
    gx = next(d for d in c.get("/api/devices").json()["devices"] if d["id"] == "gx")
    assert gx["components"] == 3
    # refusals
    for body, word in [({"type": "pv", "instance": "1"}, "type"),
                       ({"type": "battery", "instance": "a/b"}, "instance"),
                       ({"type": "battery", "instance": "288"}, "already"),
                       ({"type": "battery", "instance": "9", "id": "gx"}, "already exists")]:
        res = c.post("/api/devices/gx/components", json=body)
        assert res.status_code == 422 and word in json.dumps(res.json()), body
    assert c.post("/api/devices/umg512/components", json={"type": "battery", "instance": "1"}).status_code == 422
    assert c.post("/api/devices/nope/components", json={}).status_code == 404
    # add
    res = c.post("/api/devices/gx/components", json={"type": "temperature", "instance": "23"})
    assert res.status_code == 200, res.text
    v = res.json()["component"]
    assert v["id"] == "gx-temperature-23" and v["name"] == "Temperature sensor 23"
    assert v["mqtt_topic_prefix"].endswith("gx-temperature-23")
    regs, _g = cfg.load_device_registers(cfg.get_device("gx-temperature-23"))
    assert regs and all("{instance}" in r.topic for r in regs)          # seeded from the component
    # managed only here
    assert c.put("/api/devices/gx-temperature-23", json={"name": "x"}).status_code == 422
    assert c.delete("/api/devices/gx-temperature-23").status_code == 422
    # rename, switch off
    res = c.patch("/api/devices/gx/components/gx-temperature-23", json={"name": "Outside", "enabled": False})
    assert res.status_code == 200 and res.json()["component"]["name"] == "Outside"
    assert c.patch("/api/devices/gx/components/ruuvi-x", json={}).status_code == 404
    # remove
    assert c.delete("/api/devices/gx/components/gx-temperature-23").status_code == 200
    assert cfg.get_device("gx-temperature-23") is None


@needs_tc
def test_a_parent_edit_keeps_its_components_and_its_kind(tmp_path):
    cfg, c = _app(tmp_path)
    body = {"id": "gx", "name": "GX 2", "template": "victron_gx_mqtt", "enabled": False,
            "connection": {"protocol": "mqtt", "broker": "192.0.2.10", "port": 8883, "topic": f"N/{PORTAL}/#",
                           "vars": {"portal": PORTAL, "vebus": "274"}}}
    res = c.put("/api/devices/gx", json=body)
    assert res.status_code == 200, res.text
    assert len(cfg.component_devices("gx")) == 3
    assert cfg.get_device("gx-battery-288").mqtt_in["password"] == "secret"   # kept by the edit
    res = c.put("/api/devices/gx", json={**body, "connection": {"protocol": "tcp", "host": "192.0.2.10"},
                                          "template": ""})
    assert res.status_code == 422 and "component" in res.text


@needs_tc
def test_deleting_the_parent_takes_its_components_and_a_restore_brings_them(tmp_path):
    cfg, c = _app(tmp_path)
    assert c.delete("/api/devices/gx").status_code == 200
    assert cfg.component_devices("gx") == [] and cfg.get_device("ruuvi-in") is None
    res = c.post("/api/devices/gx/restore")
    assert res.status_code == 200, res.text
    assert {d.id for d in cfg.component_devices("gx")} == {"gx-battery-288", "gx-battery-512", "ruuvi-in"}
    assert cfg.get_device("gx").influxdb_bucket == "victron-gx"


@needs_tc
def test_a_component_template_is_not_a_device_template(tmp_path):
    _cfg2, c = _app(tmp_path)
    res = c.post("/api/devices", json={"id": "lone", "template": "victron_gx_mqtt:battery", "enabled": False,
                                       "connection": {"protocol": "mqtt", "broker": "192.0.2.10"}})
    assert res.status_code == 422 and "component" in res.text
    assert c.get("/api/device-templates/victron_gx_mqtt:battery/export").status_code == 422
    ids = [t["id"] for t in c.get("/api/device-templates").json()["templates"]]
    assert "victron_gx_mqtt" in ids and not [i for i in ids if ":" in i]


@needs_tc
def test_finding_components_listens_through_the_parents_connection(tmp_path, fake_links, monkeypatch):
    _cfg2, c = _app(tmp_path)
    assert c.post("/api/devices/gx/components/discover").status_code == 409     # not connected
    link = mqtt_link.MqttLink(("gx",), SimpleNamespace(broker="h", port=1))
    link.client, link.connected = _Paho(), True
    link._ka["R/" + PORTAL + "/keepalive"] = {"payload": "k", "first_payload": "", "every_s": 30,
                                               "last": 0.0, "owners": []}
    mqtt_link._pool[("gx",)] = link

    def speak(_s):
        for t, v in [(f"N/{PORTAL}/battery/288/ProductName", "SmartShunt 1000A/50mV"),
                     (f"N/{PORTAL}/battery/512/ProductName", "BMS16S200A-SP112"),
                     (f"N/{PORTAL}/battery/512/CustomName", "SEPLOS V4"),
                     (f"N/{PORTAL}/battery/700/ProductName", None),
                     (f"N/{PORTAL}/temperature/24/ProductName", "RuuviTag")]:
            link._on_message(link.client, None, _msg(t, v))
    monkeypatch.setattr("time.sleep", speak)
    res = c.post("/api/devices/gx/components/discover")
    assert res.status_code == 200, res.text
    found = res.json()["found"]
    assert [(f["type"], f["instance"]) for f in found] == [("battery", "288"), ("battery", "512"), ("temperature", "24")]
    assert found[1]["name"] == "SEPLOS V4" and found[0]["added"] and not found[2]["added"]
    assert link._taps == [] and not link._subs                           # and stopped listening


@needs_tc
def test_a_put_carrying_components_never_drops_them(tmp_path):
    cfg, c = _app(tmp_path)
    body = {"id": "gx", "name": "GX", "template": "victron_gx_mqtt", "enabled": False, "components": 3,
            "connection": {"protocol": "mqtt", "broker": "192.0.2.10", "port": 8883, "topic": f"N/{PORTAL}/#",
                           "vars": {"portal": PORTAL, "vebus": "274"}}}
    assert c.put("/api/devices/gx", json=body).status_code == 200
    assert len(cfg.component_devices("gx")) == 3


@needs_tc
def test_a_restore_brings_back_what_validation_does_not_rebuild_and_nothing_else(tmp_path):
    cfg, c = _app(tmp_path)
    raw = cfg.get_raw_device("gx")
    assert c.delete("/api/devices/gx").status_code == 200
    planted = dict(raw, write_locked=True, sources=[{"id": "x", "protocol": "http", "url": "http://169.254.169.254/"}])
    cfg._tombstones.write("gx", planted)
    assert c.post("/api/devices/gx/restore").status_code == 200
    back = cfg.get_raw_device("gx")
    assert back["write_locked"] is True and "sources" not in back and len(back["components"]) == 5
