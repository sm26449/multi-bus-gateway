# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""In 'changed' mode a steady value is republished after `heartbeat_interval`
seconds so it keeps a fresh timestamp (HA doesn't grey the entity out). Off (0)
by default — unchanged values are never republished.
"""
import multibus.mqtt_publisher as mp
from multibus.config import MQTTConfig
from multibus.mqtt_publisher import MQTTPublisher


def _pub(heartbeat=0):
    return MQTTPublisher(MQTTConfig(enabled=False), [], publish_mode="changed",
                         heartbeat_interval=heartbeat)


def test_heartbeat_republishes_unchanged_after_interval(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(mp.time, "time", lambda: now[0])
    pub = _pub(heartbeat=10)
    t = "meters/x/voltage/l1_n"

    assert pub._should_publish(t, 230.0) is True     # first time → publish
    pub._confirm_publish(t, 230.0)

    now[0] = 1005.0                                   # unchanged, within interval
    assert pub._should_publish(t, 230.0) is False

    now[0] = 1011.0                                   # unchanged, past interval → heartbeat
    assert pub._should_publish(t, 230.0) is True
    pub._confirm_publish(t, 230.0)

    now[0] = 1012.0                                   # a real change always publishes
    assert pub._should_publish(t, 231.0) is True


def test_heartbeat_off_by_default(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(mp.time, "time", lambda: now[0])
    pub = _pub(heartbeat=0)                            # default: no heartbeat
    t = "meters/x/voltage/l1_n"
    assert pub._should_publish(t, 230.0) is True
    pub._confirm_publish(t, 230.0)
    now[0] = 1_000_000.0                               # a long time later, still unchanged
    assert pub._should_publish(t, 230.0) is False      # never republished on time alone


def test_heartbeat_config_round_trips(tmp_path):
    from multibus.config import Config
    from tests.test_devices import write_config
    cfg = Config(str(write_config(tmp_path).config_path))
    cfg.update_mqtt(heartbeat_interval=45)
    assert cfg.mqtt.heartbeat_interval == 45
    assert cfg.to_dict()["mqtt"]["heartbeat_interval"] == 45
