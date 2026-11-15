# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""MQTT-RATE-1: optional per-topic throttling of value topics.

Prod published ~219 msg/s in `changed` mode: a meter polled at 4 Hz has ~25
analog fields whose floats change on every read. ``mqtt.min_interval_s`` caps
each topic's rate without ever losing the final value; ``mqtt.deadband_pct``
suppresses noise. Both off by default = byte-for-byte the old behaviour.
"""
from types import SimpleNamespace

import pytest

import multibus.mqtt_publisher as mp
from multibus.config import MQTTConfig
from multibus.mqtt_publisher import MQTTPublisher


class _Client:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.sent.append((topic, payload))
        return SimpleNamespace(rc=mp.mqtt.MQTT_ERR_SUCCESS)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(mp.time, "monotonic", lambda: now[0])
    return now


def _pub(monkeypatch, **cfg):
    pub = MQTTPublisher(MQTTConfig(enabled=False, **cfg), [], publish_mode="changed")
    pub.client = _Client()
    pub.connected = True
    # no background thread in a unit test: the final-value path is driven by
    # flush_throttled() / the next batch, exactly what the flusher calls
    monkeypatch.setattr(pub, "_ensure_throttle_flusher", lambda mi: None)
    return pub


def _reg(name="voltage_l1_n"):
    return SimpleNamespace(name=name, mqtt_enabled=True, mqtt_topic=None)


def _feed(pub, value, group="realtime", reg=None):
    pub.publish_register_data(group, {1: {"register": reg or _reg(), "value": value}},
                              topic_prefix="mbg/devices/umg512")


def _values(pub):
    return [p for t, p in pub.client.sent if t.endswith("voltage_l1_n")]


def test_defaults_publish_every_change_as_before(monkeypatch, clock):
    pub = _pub(monkeypatch)
    for i in range(8):                       # 4 Hz for 2 s, always changing
        _feed(pub, 230.0 + i * 0.1)
        clock[0] += 0.25
    assert len(_values(pub)) == 8


def test_min_interval_caps_a_4hz_series_and_never_drops_the_last(monkeypatch, clock):
    pub = _pub(monkeypatch, min_interval_s=1.0)
    series = [230.0 + i * 0.1 for i in range(17)]     # 4 s at 4 Hz
    for v in series:
        _feed(pub, v)
        clock[0] += 0.25
    sent = _values(pub)
    assert len(sent) <= 5                    # ≤ 1/s (+ the first)
    # the series stops; the held-back final value still goes out
    clock[0] += 1.0
    pub.flush_throttled()
    assert _values(pub)[-1] == str(round(series[-1], 3))
    assert pub._throttle_pending == {}


def test_held_value_rides_out_on_the_next_batch(monkeypatch, clock):
    pub = _pub(monkeypatch, min_interval_s=1.0)
    _feed(pub, 230.0)
    clock[0] += 0.25
    _feed(pub, 231.0)                        # held back
    assert _values(pub) == ["230.0"]
    clock[0] += 1.0
    _feed(pub, 231.0, reg=_reg("frequency"))  # another topic's batch flushes it
    assert _values(pub) == ["230.0", "231.0"]


def test_a_value_back_at_the_published_one_drops_the_pending(monkeypatch, clock):
    pub = _pub(monkeypatch, min_interval_s=1.0)
    _feed(pub, 230.0)
    clock[0] += 0.25
    _feed(pub, 231.0)                        # held
    clock[0] += 0.25
    _feed(pub, 230.0)                        # back to what the broker holds
    clock[0] += 2.0
    pub.flush_throttled()
    assert _values(pub) == ["230.0"]


def test_poll_interval_equal_to_min_interval_is_not_halved_by_jitter(monkeypatch, clock):
    pub = _pub(monkeypatch, min_interval_s=1.0)
    for i in range(6):
        _feed(pub, 230.0 + i)
        clock[0] += 0.98 if i % 2 else 1.02
    assert len(_values(pub)) == 6


def test_deadband_suppresses_noise_but_not_a_real_change(monkeypatch, clock):
    pub = _pub(monkeypatch, deadband_pct=0.5)          # 0.5 % of 230 V = 1.15 V
    for v in (230.0, 230.2, 229.9, 230.6, 231.0, 232.0):
        _feed(pub, v)
        clock[0] += 0.25
    assert _values(pub) == ["230.0", "232.0"]


def test_throttle_limited_to_the_named_poll_groups(monkeypatch, clock):
    pub = _pub(monkeypatch, min_interval_s=1.0, throttle_poll_groups=["realtime"])
    for i in range(4):
        _feed(pub, 230.0 + i, group="fast")             # not named → unthrottled
        clock[0] += 0.25
    assert len(_values(pub)) == 4


def test_config_round_trips_and_is_absent_by_default(tmp_path):
    from multibus.config import Config
    from tests.test_devices import write_config
    cfg = Config(str(write_config(tmp_path).config_path))
    out = cfg.to_dict()["mqtt"]
    assert "min_interval_s" not in out and "deadband_pct" not in out
    cfg.update_mqtt(min_interval_s=1.0, throttle_poll_groups=["realtime"])
    cfg.save_yaml_config()
    again = Config(str(cfg.config_path))
    assert again.mqtt.min_interval_s == 1.0
    assert again.mqtt.throttle_poll_groups == ["realtime"]
    assert again.mqtt.deadband_pct == 0.0
