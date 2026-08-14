# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""HA write-entities: a writable register is published as a number/select and
its command topic is subscribed + routed to the gated write handler. Off by
default (no write_rules → plain sensors, no subscriptions)."""
import json
import queue
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from multibus.config import MQTTConfig, SelectedRegister
from multibus.mqtt_publisher import MQTTPublisher


def _pub(**cfg):
    p = MQTTPublisher(MQTTConfig(enabled=False, ha_discovery_enabled=True, **cfg), [])
    p.connected = True
    p.client = MagicMock()
    p._captured = {}

    def _cap(topic, payload, retain=True):
        # empty payload = HA "remove entity"; a plain string (e.g. "online") is a
        # non-JSON state — record raw instead of choking on json.loads
        if not payload:
            p._captured[topic] = None
        else:
            try:
                p._captured[topic] = json.loads(payload)
            except (ValueError, TypeError):
                p._captured[topic] = payload
        return True
    p._publish = _cap
    return p


def _reg(address=52, name="power_limit", unit="W", enum=None):
    return SelectedRegister(address=address, name=name, label=name.title(),
                            unit=unit, data_type="uint16", poll_group="normal",
                            enum=enum)


def _rule(**kw):
    base = dict(writable=True, write_min=0, write_max=5000, write_safe=None,
                data_type="uint16", scale=1.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _disc(pub, component):
    return next((t for t in pub._captured if f"/{component}/" in t), None)


# ── off by default: no write_rules → sensors, no subscriptions ────────────────

def test_no_write_rules_publishes_plain_sensors():
    pub = _pub(allow_write_entities=True)
    pub.publish_device_discovery("dev1", "Dev", "meters/dev1", [_reg()])
    assert _disc(pub, "sensor") and _disc(pub, "number") is None
    assert pub._command_map == {}
    pub.client.subscribe.assert_not_called()


# ── numeric writable → number entity + command topic ─────────────────────────

def test_writable_numeric_becomes_number_with_bounds():
    pub = _pub(allow_write_entities=True)
    reg = _reg()
    pub.publish_device_discovery("dev1", "Dev", "meters/dev1", [reg],
                                 write_rules={52: _rule()})
    topic = _disc(pub, "number")
    assert topic is not None
    cfg = pub._captured[topic]
    assert cfg["command_topic"] == "meters/dev1/power_limit/set"
    assert cfg["min"] == 0 and cfg["max"] == 5000 and cfg["mode"] == "box"
    assert "state_class" not in cfg                 # invalid on a number
    assert pub._command_map["meters/dev1/power_limit/set"] == ("dev1", reg)
    pub.client.subscribe.assert_any_call("meters/dev1/power_limit/set")


# ── enum writable → select entity with options ───────────────────────────────

def test_writable_enum_becomes_select_with_options():
    pub = _pub(allow_write_entities=True)
    reg = _reg(name="mode", unit="", enum={"0": "Off", "1": "On", "2": "Eco"})
    pub.publish_device_discovery("dev1", "Dev", "meters/dev1", [reg],
                                 write_rules={52: _rule(write_min=None, write_max=None)})
    topic = _disc(pub, "select")
    assert topic is not None
    cfg = pub._captured[topic]
    assert cfg["options"] == ["Off", "On", "Eco"]
    assert cfg["command_topic"] == "meters/dev1/mode/set"
    assert "min" not in cfg and "state_class" not in cfg


# ── command routing: on_message → worker → handler with decoded payload ──────

def test_on_message_routes_to_handler():
    pub = _pub(allow_write_entities=True)
    calls = []
    pub.set_command_write_handler(lambda d, r, p: calls.append((d, r.address, p)))
    reg = _reg()
    pub.publish_device_discovery("dev1", "Dev", "meters/dev1", [reg],
                                 write_rules={52: _rule()})
    msg = SimpleNamespace(topic="meters/dev1/power_limit/set", payload=b" 1500 ")
    pub._on_message(pub.client, None, msg)
    pub._command_queue.join()                        # worker drained the command
    assert calls == [("dev1", 52, "1500")]           # trimmed, decoded


# ── M4: the blocking write runs on the command worker, never the paho loop ────

def test_command_executes_off_the_calling_thread():
    pub = _pub(allow_write_entities=True)
    seen = []
    pub.set_command_write_handler(lambda d, r, p: seen.append(threading.current_thread()))
    reg = _reg()
    pub.publish_device_discovery("dev1", "Dev", "meters/dev1", [reg],
                                 write_rules={52: _rule()})
    pub._on_message(pub.client, None,
                    SimpleNamespace(topic="meters/dev1/power_limit/set", payload=b"1"))
    pub._command_queue.join()
    assert seen and seen[0] is not threading.current_thread()
    assert seen[0].name == "mqtt-command-worker"


def test_full_command_queue_drops_without_raising():
    pub = _pub(allow_write_entities=True)
    pub.set_command_write_handler(lambda *a: None)
    # park the worker so the queue can actually fill
    pub._stop_commands.set()
    pub._command_worker.join(timeout=2)
    pub._command_queue = queue.Queue(maxsize=1)
    reg = _reg()
    pub.publish_device_discovery("dev1", "Dev", "meters/dev1", [reg],
                                 write_rules={52: _rule()})
    msg = SimpleNamespace(topic="meters/dev1/power_limit/set", payload=b"1")
    pub._on_message(pub.client, None, msg)           # fills the queue
    pub._on_message(pub.client, None, msg)           # dropped, no exception
    assert pub._command_queue.qsize() == 1


def test_disconnect_stops_the_command_worker():
    pub = _pub(allow_write_entities=True)
    pub.set_command_write_handler(lambda *a: None)
    worker = pub._command_worker
    assert worker.is_alive()
    pub.disconnect()
    assert not worker.is_alive()


def test_on_message_ignores_unknown_topic_and_missing_handler():
    pub = _pub(allow_write_entities=True)
    # no handler set → silently ignored
    pub._on_message(pub.client, None, SimpleNamespace(topic="x/y/set", payload=b"1"))
    calls = []
    pub.set_command_write_handler(lambda *a: calls.append(a))
    # handler set, but topic isn't a known command
    pub._on_message(pub.client, None, SimpleNamespace(topic="unknown/set", payload=b"1"))
    assert calls == []


# ── rebuild drops stale command subscriptions ────────────────────────────────

def test_rebuild_unsubscribes_removed_commands():
    pub = _pub(allow_write_entities=True)
    reg = _reg()
    pub.publish_device_discovery("dev1", "Dev", "meters/dev1", [reg],
                                 write_rules={52: _rule()})
    assert "meters/dev1/power_limit/set" in pub._command_map
    # re-publish with the register no longer writable → command gone + unsubscribed
    pub.publish_device_discovery("dev1", "Dev", "meters/dev1", [reg], write_rules={})
    assert pub._command_map == {}
    pub.client.unsubscribe.assert_any_call("meters/dev1/power_limit/set")


# ── per-device connectivity binary_sensor + availability ─────────────────────

def test_discovery_includes_connectivity_binary_sensor():
    pub = _pub()
    pub.publish_device_discovery("dev1", "Dev", "meters/dev1", [_reg()])
    topic = next((t for t in pub._captured if "/binary_sensor/" in t), None)
    assert topic is not None
    cfg = pub._captured[topic]
    assert cfg["device_class"] == "connectivity"
    assert cfg["state_topic"] == "meters/dev1/availability"


def test_availability_published_only_on_change():
    pub = _pub()
    pub.publish_device_availability("meters/dev1", True)
    assert pub._captured["meters/dev1/availability"] == "online"
    pub._captured.clear()
    pub.publish_device_availability("meters/dev1", True)      # unchanged
    assert pub._captured == {}                                # no re-publish
    pub.publish_device_availability("meters/dev1", False)     # changed
    assert pub._captured["meters/dev1/availability"] == "offline"


# ── the gated command handler (the security boundary) ────────────────────────
import pytest  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: F401
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False


def _ctrl_tpl():
    from multibus.device_template import parse_template
    return parse_template({"device_template": {"schema_version": 1, "id": "ctrl_tpl",
        "name": "Ctrl", "protocol": {}, "categories": {}, "registers": [
            {"address": 100, "name": "limit", "data_type": "uint16",
             "register_type": "holding", "writable": True, "write_min": 0, "write_max": 100},
            {"address": 300, "name": "mode", "data_type": "uint16",
             "register_type": "holding", "writable": True,
             "write_min": 0, "write_max": 1, "enum": {"0": "Off", "1": "On"}},
        ]}}, builtin=True)


class _FakeClient:
    def __init__(self): self.reg = {}
    def write_value(self, address, register_type, data_type, value, scale=1.0, prefer_fc6=False):
        self.reg[address] = value
        return True, None, [int(float(value))]
    def read_register(self, address, data_type, register_type):
        return self.reg.get(address)                     # raw == engineering (scale 1)


def _handler_and_client(tmp_path):
    from tests.test_devices import write_config
    from multibus.api import create_api
    from multibus.config import DeviceConfig
    cfg = write_config(tmp_path)
    cfg.security.allow_writes = True
    cfg.mqtt.allow_write_entities = True
    dev = DeviceConfig(id="ctrl", name="ctrl", template="ctrl_tpl", protocol="tcp")
    fake = _FakeClient()
    mock_mqtt = MagicMock()
    app, _ = create_api(cfg, None, mock_mqtt, None,
                        devices=[(d, None) for d in cfg.devices] + [(dev, fake)])
    app.state.template_registry._templates["ctrl_tpl"] = _ctrl_tpl()
    handler = mock_mqtt.set_command_write_handler.call_args[0][0]
    fake.app = app                                       # for store-refresh assertions
    return handler, fake, cfg


@pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")
def test_handler_writes_valid_number(tmp_path):
    handler, fake, _ = _handler_and_client(tmp_path)
    handler("ctrl", _reg(address=100, name="limit"), "42")
    assert fake.reg[100] == 42


@pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")
def test_handler_rejects_out_of_bounds(tmp_path):
    handler, fake, _ = _handler_and_client(tmp_path)
    handler("ctrl", _reg(address=100, name="limit"), "250")   # > write_max 100
    assert 100 not in fake.reg


@pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")
def test_handler_maps_select_label_to_code(tmp_path):
    handler, fake, _ = _handler_and_client(tmp_path)
    handler("ctrl", _reg(address=300, name="mode", enum={"0": "Off", "1": "On"}), "On")
    assert fake.reg[300] == 1
    handler("ctrl", _reg(address=300, name="mode", enum={"0": "Off", "1": "On"}), "Nope")
    assert fake.reg[300] == 1                          # unknown option ignored, no change


@pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")
def test_handler_blocked_when_allow_writes_off(tmp_path):
    handler, fake, cfg = _handler_and_client(tmp_path)
    cfg.security.allow_writes = False                 # global gate down
    handler("ctrl", _reg(address=100, name="limit"), "42")
    assert 100 not in fake.reg


@pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")
def test_handler_blocked_when_write_entities_off(tmp_path):
    handler, fake, cfg = _handler_and_client(tmp_path)
    cfg.mqtt.allow_write_entities = False             # feature gate down
    handler("ctrl", _reg(address=100, name="limit"), "42")
    assert 100 not in fake.reg


@pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")
def test_handler_rejects_undeclared_register(tmp_path):
    handler, fake, _ = _handler_and_client(tmp_path)
    handler("ctrl", _reg(address=999, name="ghost"), "5")   # not in the template
    assert fake.reg == {}


@pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")
def test_write_then_refresh_updates_the_live_store(tmp_path):
    handler, fake, _ = _handler_and_client(tmp_path)
    store = fake.app.state.registry.ensure_store("ctrl")
    store[100] = {"value": 0, "ts": 0, "mono": 0, "interval": 5}   # a prior poll
    handler("ctrl", _reg(address=100, name="limit"), "42")
    assert fake.reg[100] == 42                            # written
    assert store[100]["value"] == 42                      # AND reflected at once
    assert store[100]["ts"] > 0
