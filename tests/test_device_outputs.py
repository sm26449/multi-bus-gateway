# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A device's MQTT and InfluxDB outputs switch at once from its Outputs tab:
persisted, applied at the next reading without restarting the poller, and
refused where the device does not own the choice (the primary, an
endpoint's units)."""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from multibus.api import create_api
from multibus.config import Config

from tests.test_devices import write_config
from tests.test_devices_api import needs_tc

YAML = """
devices:
  - id: em24
    template: janitza_umg512_pro
    enabled: true
    connection: { protocol: tcp, host: 192.0.2.9 }
    mqtt: { topic_prefix: "meters/em24" }
    influxdb: { bucket: "warehouse" }
endpoints:
  - id: pv
    connection: { protocol: tcp, host: 192.0.2.9, port: 502 }
    units: [1]
"""


class _Cap:
    connected = True
    config = SimpleNamespace(enabled=True, topic_prefix="janitza/umg512", ha_discovery_enabled=False,
                             ha_discovery_prefix="homeassistant")

    def __init__(self):
        self.mqtt, self.influx = [], []

    def publish_register_data(self, poll_group, data, topic_prefix=None):
        self.mqtt.append(topic_prefix)

    def write_register_data(self, poll_group, data, bucket=None, device_tag=None, device_id="", extra_tags=None):
        self.influx.append(bucket)

    def __getattr__(self, n):
        return lambda *a, **k: None


def _app(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=YAML)
    cap = _Cap()
    clients = {d.id: SimpleNamespace(publish_callback=None) for d in cfg.devices}
    app, _ = create_api(cfg, None, cap, cap, devices=[(d, clients[d.id]) for d in cfg.devices])
    return cfg, cap, clients, TestClient(app, raise_server_exceptions=False)


def _poll(clients):
    reg = SimpleNamespace(name="_V1", label="L1", unit="V", mqtt_enabled=True, influxdb_enabled=True)
    clients["em24"].publish_callback("realtime", {19000: {"value": 230.0, "register": reg}})


@needs_tc
def test_switching_an_output_applies_at_the_next_reading_and_persists(tmp_path):
    _cfg, cap, clients, client = _app(tmp_path)
    _poll(clients)
    assert cap.mqtt == ["meters/em24"] and cap.influx == ["warehouse"]

    r = client.post("/api/devices/em24/outputs", json={"mqtt": False})
    assert r.status_code == 200 and r.json()["mqtt_enabled"] is False and r.json()["influxdb_enabled"] is True
    cap.mqtt.clear(); cap.influx.clear()
    _poll(clients)                                   # the same poller, no restart
    assert cap.mqtt == [] and cap.influx == ["warehouse"]

    client.post("/api/devices/em24/outputs", json={"influxdb": False, "ha_discovery": False})
    re = Config(str(tmp_path / "config.yaml")).get_device("em24")
    assert (re.mqtt_enabled, re.influxdb_enabled, re.ha_discovery_enabled) == (False, False, False)
    assert re.mqtt_topic_prefix == "meters/em24"     # the routing stays


@needs_tc
def test_where_the_device_does_not_own_the_choice_it_is_refused(tmp_path):
    _cfg, _cap, _clients, client = _app(tmp_path)
    assert client.post("/api/devices/umg512/outputs", json={"mqtt": False}).status_code == 422
    r = client.post("/api/devices/pv-u1/outputs", json={"influxdb": False})
    assert r.status_code == 422 and "installation" in r.text
    assert client.post("/api/devices/em24/outputs", json={}).status_code == 422
    assert client.post("/api/devices/em24/outputs", json={"mqtt": "no"}).status_code == 422
    assert client.post("/api/devices/nope/outputs", json={"mqtt": False}).status_code == 404
