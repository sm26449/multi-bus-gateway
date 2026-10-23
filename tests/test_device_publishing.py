# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A device publishes on its own rhythm when it says so — every reading or on
change, its own MQTT heartbeat, its own InfluxDB interval — and on the
gateway's (Settings → MQTT / InfluxDB) otherwise."""
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from multibus.api import create_api
from multibus.config import Config, InfluxDBConfig, MQTTConfig
from multibus.influxdb_publisher import InfluxDBPublisher
from multibus.mqtt_publisher import MQTTPublisher

from tests.test_devices import write_config
from tests.test_devices_api import make_app, needs_tc


# ── the publishers ───────────────────────────────────────────────────────────

def test_mqtt_follows_the_devices_mode_and_heartbeat_else_the_gateways():
    pub = MQTTPublisher(MQTTConfig(enabled=False), [], publish_mode="changed", heartbeat_interval=0)
    pub.last_values["t"] = 230.0
    pub.last_publish_at["t"] = time.monotonic()
    assert pub._should_publish("t", 230.0) is False                       # the gateway's: on change
    assert pub._should_publish("t", 230.0, mode="all") is True            # this device: every reading
    pub.last_publish_at["t"] = time.monotonic() - 40
    assert pub._should_publish("t", 230.0, heartbeat=30) is True          # its own heartbeat
    assert pub._should_publish("t", 230.0, heartbeat=60) is False
    pub.heartbeat_interval = 30
    assert pub._should_publish("t", 230.0, heartbeat=0) is False          # 0 = no heartbeat, its choice


def test_influxdb_follows_the_devices_mode_and_interval_else_the_gateways():
    cfg = InfluxDBConfig(enabled=False, write_interval=5)
    pub = InfluxDBPublisher(cfg, [], publish_mode="changed")
    pub.last_values[("d", 1)] = 7.0
    pub.last_write_time[("d", 1)] = time.time() - 2
    assert pub._should_write(1, 8.0, "d") is False                        # 5 s not up
    assert pub._should_write(1, 8.0, "d", interval=1) is True             # its own 1 s
    pub.last_write_time[("d", 1)] = time.time() - 10
    assert pub._should_write(1, 7.0, "d") is False                        # unchanged
    assert pub._should_write(1, 7.0, "d", mode="all") is True             # every reading


# ── the configuration ────────────────────────────────────────────────────────

YAML = """
devices:
  - id: em24
    template: janitza_umg512_pro
    connection: { protocol: tcp, host: 192.0.2.9 }
    mqtt: { topic_prefix: "meters/em24", publish_mode: all, heartbeat_interval: 10 }
    influxdb: { bucket: warehouse, publish_mode: changed, write_interval: 1 }
  - id: plain
    connection: { protocol: tcp, host: 192.0.2.10 }
  - id: wrong
    connection: { protocol: tcp, host: 192.0.2.11 }
    mqtt: { publish_mode: sometimes, heartbeat_interval: -3 }
endpoints:
  - id: pv
    connection: { protocol: tcp, host: 192.0.2.9, port: 502 }
    groups:
      - id: inverters
        units: [1, 2]
"""


def test_a_device_carries_its_own_rhythm_or_none(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=YAML)
    em = cfg.get_device("em24")
    assert (em.mqtt_publish_mode, em.mqtt_heartbeat_s, em.influxdb_publish_mode, em.influxdb_write_interval_s) \
        == ("all", 10, "changed", 1.0)
    plain = cfg.get_device("plain")
    assert (plain.mqtt_publish_mode, plain.mqtt_heartbeat_s) == ("", None)          # the gateway's
    wrong = cfg.get_device("wrong")
    assert (wrong.mqtt_publish_mode, wrong.mqtt_heartbeat_s) == ("", None)          # nonsense = the gateway's


def test_the_setter_writes_where_the_choice_lives(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=YAML)
    cfg.set_device_publishing("plain", mqtt={"publish_mode": "all"}, influxdb={"write_interval": 30.0})
    cfg.set_device_publishing("em24", mqtt={"heartbeat_interval": None})             # back to Settings
    cfg.set_device_publishing("pv-u1", influxdb={"publish_mode": "all"})              # its group
    re = Config(str(tmp_path / "config.yaml"))
    assert re.get_device("plain").mqtt_publish_mode == "all" and re.get_device("plain").influxdb_write_interval_s == 30.0
    assert re.get_device("em24").mqtt_heartbeat_s is None and re.get_device("em24").mqtt_publish_mode == "all"
    assert re.get_device("pv-u2").influxdb_publish_mode == "all"
    assert re.get_raw_endpoint("pv")["groups"][0]["influxdb"] == {"publish_mode": "all"}
    try:
        cfg.set_device_publishing("umg512", mqtt={"publish_mode": "all"})
        raise AssertionError("the primary follows Settings")
    except ValueError:
        pass


# ── the API and the poll ─────────────────────────────────────────────────────

class _Cap:
    connected = True
    config = SimpleNamespace(enabled=True, topic_prefix="janitza/umg512", ha_discovery_enabled=False,
                             ha_discovery_prefix="homeassistant")

    def __init__(self):
        self.mqtt, self.influx = [], []

    def publish_register_data(self, poll_group, data, topic_prefix=None, **policy):
        self.mqtt.append(policy)

    def write_register_data(self, poll_group, data, bucket=None, device_tag=None, device_id="",
                            extra_tags=None, **policy):
        self.influx.append(policy)

    def __getattr__(self, n):
        return lambda *a, **k: None


def _app(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=YAML)
    cap = _Cap()
    clients = {d.id: SimpleNamespace(publish_callback=None) for d in cfg.devices}
    app, _ = create_api(cfg, None, cap, cap, devices=[(d, clients[d.id]) for d in cfg.devices])
    return cap, clients, TestClient(app, raise_server_exceptions=False)


def _poll(clients, dev):
    reg = SimpleNamespace(name="_V1", label="L1", unit="V", mqtt_enabled=True, influxdb_enabled=True)
    clients[dev].publish_callback("realtime", {19000: {"value": 230.0, "register": reg}})


@needs_tc
def test_each_reading_goes_out_on_the_devices_rhythm_changed_without_a_restart(tmp_path):
    cap, clients, client = _app(tmp_path)
    _poll(clients, "em24")
    assert cap.mqtt[-1] == {"mode": "all", "heartbeat": 10} and cap.influx[-1] == {"mode": "changed", "interval": 1.0}
    _poll(clients, "plain")
    assert cap.mqtt[-1] == {} and cap.influx[-1] == {}                    # the gateway's
    r = client.post("/api/devices/plain/publishing", json={"mqtt": {"publish_mode": "all", "heartbeat_interval": 0}})
    assert r.status_code == 200, r.text
    _poll(clients, "plain")                                               # the same poller
    assert cap.mqtt[-1] == {"mode": "all", "heartbeat": 0}


@needs_tc
def test_the_device_says_how_it_publishes_and_wrong_values_are_refused(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=YAML)
    dev = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "em24")
    pub = dev["publishing"]
    assert pub["own"]["mqtt"] == {"publish_mode": "all", "heartbeat_interval": 10}
    assert pub["effective"]["influxdb"]["write_interval"] == 1.0 and pub["editable"] is True
    prim = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "umg512")["publishing"]
    assert prim["editable"] is False and prim["effective"] == prim["settings"]
    for bad in ({"mqtt": {"publish_mode": "sometimes"}}, {"mqtt": {"heartbeat_interval": -1}},
                {"influxdb": {"write_interval": "5"}}, {"mqtt": {"retain": True}}, {}):
        assert client.post("/api/devices/em24/publishing", json=bad).status_code == 422, bad
    assert client.post("/api/devices/umg512/publishing", json={"mqtt": {"publish_mode": "all"}}).status_code == 422
    r = client.post("/api/devices/em24/publishing", json={"mqtt": {"publish_mode": None, "heartbeat_interval": None}})
    assert r.json()["publishing"]["own"]["mqtt"] == {"publish_mode": None, "heartbeat_interval": None}
