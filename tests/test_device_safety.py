# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Safe defaults for a device (ADR-0025 decisions 2, 3, 4 and 6; FINDINGS
ADD-7, ADD-8, ADD-12, ADD-14).

- a tap never turns into a poll, whatever its bridge frames;
- a template written for listening stays listening;
- a new device (created, adopted, an installation) is write-locked, and a
  restored one comes back locked with its commands off;
- Home Assistant discovery follows the device's MQTT output."""
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from multibus.api import create_api
from multibus.bridges import resolve_connection

from tests.test_devices import write_config
from tests.test_devices_api import make_app, needs_tc


# ── ADD-7: a tap never turns into a poll ─────────────────────────────────
def test_a_tap_stays_a_tap_on_a_modbus_tcp_gateway():
    bridges = [{"id": "gw", "type": "modbus_gateway", "host": "192.0.2.61", "ports": [{"port": 502}]}]
    c = resolve_connection({"protocol": "rtu_tap", "bridge": "gw", "bridge_port": 502, "unit_id": 1}, bridges)
    assert c["protocol"] == "rtu_tap" and (c["host"], c["port"]) == ("192.0.2.61", 502)
    polled = resolve_connection({"bridge": "gw", "bridge_port": 502, "unit_id": 1}, bridges)
    assert polled["protocol"] == "tcp"                     # a polled device follows the framing, as before


BRIDGE_YAML = """
bridges:
  - id: ws
    name: Panel
    type: rtu_transparent
    host: 192.0.2.60
    ports: [{{port: 4196}}, {{port: 4197}}]
devices:
  - id: tap1
    enabled: {enabled}
    connection: {{ protocol: rtu_tap, bridge: ws, bridge_port: 4196, unit_id: 1 }}
"""
_GW_EDIT = {"name": "Panel", "type": "modbus_gateway", "host": "192.0.2.60",
            "ports": [{"port": 4196}, {"port": 4197}]}


@needs_tc
def test_a_bridge_type_change_is_refused_while_a_tap_listens_on_it(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=BRIDGE_YAML.format(enabled="true"))
    r = client.put("/api/bridges/ws", json=_GW_EDIT)
    assert r.status_code == 409, r.text
    assert "tap1" in " ".join(r.json()["detail"]["errors"]) and r.json()["detail"]["listeners"] == ["tap1"]
    assert cfg.get_raw_bridge("ws")["type"] == "rtu_transparent"
    assert cfg.get_device("tap1").protocol == "rtu_tap"
    # an edit that keeps the framing is fine
    r = client.put("/api/bridges/ws", json={**_GW_EDIT, "type": "rtu_transparent", "name": "Panel 2"})
    assert r.status_code == 200, r.text


@needs_tc
def test_a_bridge_type_change_goes_through_once_the_tap_is_off(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=BRIDGE_YAML.format(enabled="false"))
    r = client.put("/api/bridges/ws", json=_GW_EDIT)
    assert r.status_code == 200, r.text
    assert cfg.get_device("tap1").protocol == "rtu_tap"    # still listening, never polling


# ── ADD-8: a listening template stays listening ──────────────────────────
@needs_tc
def test_a_listening_template_is_refused_on_a_polled_device(tmp_path):
    _cfg, client = make_app(tmp_path)
    for conn in ({"protocol": "rtu", "serial_port": "/dev/ttyV3", "unit_id": 1},
                 {"protocol": "tcp", "host": "192.0.2.70", "port": 502, "unit_id": 1}):
        r = client.post("/api/devices", json={"id": "bank", "enabled": False,
                                              "template": "seplos_bms_v3_rtu_tap", "connection": conn})
        assert r.status_code == 422, r.text
        assert "connection.protocol" in r.json()["detail"]["fields"], r.json()["detail"]
    r = client.post("/api/devices", json={"id": "bank", "enabled": False, "template": "seplos_bms_v3_rtu_tap",
                                          "connection": {"protocol": "rtu_tap", "serial_port": "/dev/ttyV3",
                                                         "unit_id": 1}})
    assert r.status_code == 200, r.text


def test_a_stored_device_breaking_the_rule_starts_as_a_tap(tmp_path, caplog):
    from multibus.device_runtime import driver_for
    from multibus.device_template import TemplateRegistry
    from multibus.rtu_tap import RtuTapClient
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: bank
    template: seplos_bms_v3_rtu_tap
    connection: { protocol: rtu, serial_port: /dev/ttyV3, unit_id: 1 }
""")
    dev = cfg.get_device("bank")
    regs, groups = cfg.load_device_registers(dev)
    with caplog.at_level("WARNING"):
        drv = driver_for(cfg, TemplateRegistry(), dev, dev.sources[0], regs, groups, allow_nonlan=False)
    assert isinstance(drv, RtuTapClient)
    assert any("listen" in r.getMessage() and "bank" in r.getMessage() for r in caplog.records)


def test_the_import_validator_accepts_listen_only_on_a_modbus_tap_template():
    import pathlib
    from multibus import device_template as dt
    data = json.loads((pathlib.Path(dt.__file__).parent / "device_templates"
                       / "seplos_bms_v3_rtu_tap.json").read_text())
    data["device_template"]["protocol"]["listen_only"] = True
    assert not [e for e in dt.validate_template(data) if "listen_only" in e]
    # listen_only next to a transport that polls is a contradiction
    data["device_template"]["protocol"]["transports"] = ["rtu_tap", "tcp"]
    assert any("listen_only" in e for e in dt.validate_template(data))


def test_a_listening_template_is_known_as_such():
    from multibus.device_template import TemplateRegistry, template_listens
    reg = TemplateRegistry()
    assert template_listens(reg.get("seplos_bms_v3_rtu_tap"))      # transports ["rtu_tap"] only
    assert template_listens(reg.get("seplos_bms_v2_tap"))          # protocol.listen_only
    assert not template_listens(reg.get("eastron_sdm120"))


# ── ADD-12: a new device is write-locked ─────────────────────────────────
@needs_tc
def test_a_created_device_is_write_locked_whatever_the_payload_says(tmp_path):
    cfg, client = make_app(tmp_path)
    r = client.post("/api/devices", json={"id": "m1", "enabled": False, "template": "eastron_sdm120",
                                          "write_locked": False,
                                          "connection": {"protocol": "tcp", "host": "192.0.2.10", "unit_id": 1}})
    assert r.status_code == 200, r.text
    assert r.json()["device"]["write_locked"] is True
    assert cfg.get_raw_device("m1")["write_locked"] is True and cfg.get_device("m1").write_locked is True


@needs_tc
def test_an_adopted_device_is_write_locked(tmp_path):
    from tests.test_mqtt_discovery import TASMOTA
    cfg, client = make_app(tmp_path)
    client.app.state.mqtt_discovery.handle("tasmota/discovery/A4CF12A1B2C3/config", json.dumps(TASMOTA).encode())
    r = client.post("/api/mqtt-discovery/tasmota:a4cf12a1b2c3/adopt", json={})
    assert r.status_code == 200, r.text
    assert cfg.get_device(r.json()["device_id"]).write_locked is True


@needs_tc
def test_a_new_installation_is_write_locked(tmp_path):
    from tests.test_endpoints import ENDPOINT_BODY
    cfg, client = make_app(tmp_path)
    r = client.post("/api/endpoints", json={**ENDPOINT_BODY, "write_locked": False})
    assert r.status_code == 200, r.text
    assert cfg.get_raw_endpoint("sdm-endpoint")["write_locked"] is True
    assert all(d.write_locked for d in cfg.endpoint_devices("sdm-endpoint"))


@needs_tc
def test_a_restored_device_is_write_locked_with_its_commands_off(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml="""
devices:
  - id: inv
    enabled: false
    write_locked: false
    template: fronius_sunspec_inverter
    connection: { protocol: tcp, host: 192.0.2.9, port: 502, unit_id: 1 }
    commands:
      - { name: power_limit, enabled: true }
      - { name: restore, enabled: true }
""")
    assert cfg.get_device("inv").write_locked is False          # stored devices keep theirs
    assert client.delete("/api/devices/inv").status_code == 200
    r = client.post("/api/devices/inv/restore")
    assert r.status_code == 200, r.text
    raw = cfg.get_raw_device("inv")
    assert raw["write_locked"] is True
    assert [(c["name"], c["enabled"]) for c in raw["commands"]] == [("power_limit", False), ("restore", False)]


def test_a_stored_device_without_the_key_is_not_migrated(tmp_path):
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: old
    template: eastron_sdm120
    connection: { protocol: tcp, host: 192.0.2.9, unit_id: 1 }
""")
    assert cfg.get_device("old").write_locked is False


# ── ADD-14: HA discovery follows the MQTT output ─────────────────────────
class _Pub:
    connected = True
    config = SimpleNamespace(enabled=True, topic_prefix="janitza/umg512", ha_discovery_enabled=True,
                             ha_discovery_prefix="homeassistant")

    def __init__(self):
        self.announced, self.cleared = [], []

    def publish_device_discovery(self, device_id, *a, **k):
        self.announced.append(device_id)
        return 1

    def clear_device_discovery(self, device_id, *a, **k):
        self.cleared.append(device_id)
        return 1

    def __getattr__(self, n):
        return lambda *a, **k: None


HA_YAML = """
devices:
  - id: loud
    template: eastron_sdm120
    enabled: false
    connection: { protocol: tcp, host: 192.0.2.9, unit_id: 1 }
  - id: quiet
    template: eastron_sdm120
    enabled: false
    connection: { protocol: tcp, host: 192.0.2.9, unit_id: 2 }
    mqtt: { enabled: false }
"""


@needs_tc
def test_ha_discovery_is_published_only_for_a_device_whose_mqtt_is_on(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=HA_YAML)
    pub = _Pub()
    app, _ = create_api(cfg, None, pub, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/api/devices/loud/outputs", json={"ha_discovery": True}).status_code == 200
    assert "loud" in pub.announced and "quiet" not in pub.announced
    assert "quiet" in pub.cleared and "loud" not in pub.cleared
    # turning MQTT off takes its entities out of Home Assistant
    pub.announced.clear(), pub.cleared.clear()
    assert client.post("/api/devices/loud/outputs", json={"mqtt": False}).status_code == 200
    assert "loud" in pub.cleared and "loud" not in pub.announced
    # …as turning its discovery off does
    client.post("/api/devices/loud/outputs", json={"mqtt": True})
    assert "loud" in pub.announced
    pub.announced.clear(), pub.cleared.clear()
    client.post("/api/devices/loud/outputs", json={"ha_discovery": False})
    assert "loud" in pub.cleared and "loud" not in pub.announced


def test_the_publisher_clears_what_it_announced_for_a_device(tmp_path):
    from multibus.config import MQTTConfig, SelectedRegister
    from multibus.mqtt_publisher import MQTTPublisher
    pub = MQTTPublisher(MQTTConfig(enabled=False, topic_prefix="meters/em24", ha_discovery_enabled=True),
                        [], publish_mode="changed")
    sent = {}
    pub.connected = True
    pub._publish = lambda topic, payload, retain=None: sent.__setitem__(topic, payload) or True
    a = SelectedRegister(address=100, name="V1", label="V1", unit="V", data_type="float", poll_group="normal")
    pub.publish_device_discovery("em24", "EM24", "meters/em24", [a])
    announced = {t for t, p in sent.items() if p}
    assert announced
    sent.clear()
    pub.clear_device_discovery("em24", [a])
    assert announced <= {t for t, p in sent.items() if p == ""}
    sent.clear()
    pub.clear_device_discovery("em24", [a])                     # once: nothing left to clear
    assert sent == {}


def test_a_clear_after_a_restart_touches_only_what_would_have_been_announced(tmp_path):
    """A fresh publisher (a restart) clears from the registers: a sensor per
    published value and the connectivity sensor — no number/select for a
    register that has no write entity, nothing for an unpublished one."""
    from multibus.config import MQTTConfig, SelectedRegister
    from multibus.mqtt_publisher import MQTTPublisher
    pub = MQTTPublisher(MQTTConfig(enabled=False, topic_prefix="meters/em24", ha_discovery_enabled=True),
                        [], publish_mode="changed")
    sent = {}
    pub.connected = True
    pub._publish = lambda topic, payload, retain=None: sent.__setitem__(topic, payload) or True
    a = SelectedRegister(address=100, name="V1", label="V1", unit="V", data_type="float", poll_group="normal")
    b = SelectedRegister(address=102, name="V2", label="V2", unit="V", data_type="float", poll_group="normal",
                         mqtt_enabled=False)
    assert pub.clear_device_discovery("em24", [a, b]) is True
    assert sorted(sent) == ["homeassistant/binary_sensor/mbg_dev_em24/connectivity/config",
                            "homeassistant/sensor/mbg_dev_em24/100_v1/config"]
    assert set(sent.values()) == {""}
    sent.clear()
    pub2 = MQTTPublisher(MQTTConfig(enabled=False, topic_prefix="meters/em24", ha_discovery_enabled=True),
                         [], publish_mode="changed")
    pub2.connected = True
    pub2._publish = lambda topic, payload, retain=None: sent.__setitem__(topic, payload) or True
    pub2.clear_device_discovery("em24", [a], write_keys={100})
    assert {t.split("/")[1] for t in sent} == {"number", "select", "binary_sensor"}   # a write entity, no sensor


@needs_tc
def test_a_device_already_cleared_is_not_cleared_again_after_a_restart(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=HA_YAML)
    pub = _Pub()
    app, _ = create_api(cfg, None, pub, None, devices=[(d, None) for d in cfg.devices])
    TestClient(app, raise_server_exceptions=False).post("/api/devices/loud/outputs", json={"ha_discovery": True})
    assert "quiet" in pub.cleared
    # the next start: the same config, a new publisher
    from multibus.config import Config
    cfg2 = Config(str(tmp_path / "config.yaml"))
    pub2 = _Pub()
    app2, _ = create_api(cfg2, None, pub2, None, devices=[(d, None) for d in cfg2.devices])
    TestClient(app2, raise_server_exceptions=False).post("/api/devices/loud/outputs", json={"ha_discovery": True})
    assert "loud" in pub2.announced and pub2.cleared == []
