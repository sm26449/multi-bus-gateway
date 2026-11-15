# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Virtual BMS lifecycle wiring (ADR-0037): read_value reads fresh store values, load_profile
resolves a template's rows, and build_vbms_manager ties them to live data."""
import json
import pathlib

import pytest

from multibus.outputs.vbms_manager import VbmsManager
from multibus.outputs.vbms_wiring import (VbmsCommandBridge, VbmsStatePublisher,
                                          build_vbms_manager, make_load_profile, make_read_value)

_ROWS = json.loads((pathlib.Path(__file__).resolve().parent.parent / "multibus" / "device_templates"
                    / "pylontech_can_inverter.json").read_text())["device_template"]["registers"]


class FakeRegistry:
    def __init__(self, stores):
        self.stores = stores
    def store_for(self, dev):
        return self.stores.get(dev, {})


class FakeTemplate:
    def __init__(self, rows):
        self.registers = rows


class FakeTemplateRegistry:
    def __init__(self, by_name):
        self._by = by_name
    def get(self, name):
        return self._by.get(name)


def _entry(name, value, mono, interval=5):
    return {"name": name, "value": value, "mono": mono, "interval": interval}


def test_read_value_returns_fresh_value():
    now = 1000.0
    reg = FakeRegistry({"p1": {"r0": _entry("soc", 90, mono=now - 2)}})   # 2s old, bound 2.5*5=12.5
    rv = make_read_value(reg, clock=lambda: now)
    assert rv("p1", "soc") == 90
    assert rv("p1", "voltage") is None          # not in store
    assert rv("p2", "soc") is None              # no store for p2


def test_read_value_drops_stale_and_untimed():
    now = 1000.0
    reg = FakeRegistry({"p1": {"r0": _entry("soc", 90, mono=now - 100),     # stale (>12.5s)
                               "r1": _entry("voltage", 53.0, mono=None)}})  # no mono → fail closed
    rv = make_read_value(reg, clock=lambda: now)
    assert rv("p1", "soc") is None and rv("p1", "voltage") is None


def test_load_profile_resolves_rows_and_errors():
    lp = make_load_profile(FakeTemplateRegistry({"pylontech_can_inverter": FakeTemplate(_ROWS)}))
    assert lp("pylontech_can_inverter") == _ROWS
    with pytest.raises(ValueError, match="not found"):
        lp("nope")


def test_build_manager_reads_live_through_the_adapters():
    now = 500.0
    reg = FakeRegistry({
        "seplos-adr1": {"a": _entry("soc", 90, now), "b": _entry("pack_voltage", 53.5, now),
                        "c": _entry("battery_current", 10.0, now), "d": _entry("total_capacity", 280, now)},
        "seplos-adr2": {"a": _entry("soc", 70, now), "b": _entry("pack_voltage", 53.4, now),
                        "c": _entry("battery_current", 8.0, now), "d": _entry("total_capacity", 280, now)},
    })
    cfg = {"virtual_bms": [{"id": "vb", "profile": "pylontech_can_inverter",
                            "packs": ["seplos-adr1", "seplos-adr2"],
                            "floor": {"charge_current_limit": 20}}]}
    # inject the clock through the read_value adapter by building it ourselves
    from multibus.outputs.vbms_manager import VbmsManager
    m = VbmsManager(cfg, make_read_value(reg, clock=lambda: now),
                    make_load_profile(FakeTemplateRegistry({"pylontech_can_inverter": FakeTemplate(_ROWS)})))
    vals = m.values(m.instances["vb"])
    assert vals["soc"] == 80 and vals["battery_current"] == 18.0     # live aggregation
    assert vals["charge_current_limit"] == 20                        # floor


def test_build_vbms_manager_factory_smoke():
    reg = FakeRegistry({})
    m = build_vbms_manager({"virtual_bms": []}, reg,
                           FakeTemplateRegistry({"pylontech_can_inverter": FakeTemplate(_ROWS)}))
    assert m.status() == {}                                          # no instances → nothing runs


# ── VbmsCommandBridge: EMS command topics → the clamped control plane (ADR-0037 D4) ──

class FakePublisher:
    """Mimics the MQTTPublisher command seam: register/unregister a topic handler and let a test
    deliver a payload to it."""
    def __init__(self):
        self.handlers = {}
        self.unregistered = []
        self.published = {}                        # topic -> last value (publish_if_changed)
    def register_command(self, topic, fn):
        self.handlers[topic] = fn
    def unregister_command(self, topic):
        self.unregistered.append(topic)
        self.handlers.pop(topic, None)
    def deliver(self, topic, payload):
        return self.handlers[topic](payload)
    def publish_if_changed(self, topic, value, retain=True, heartbeat=None):
        changed = self.published.get(topic) != value
        self.published[topic] = value
        return changed


def _cmd_manager():
    cfg = {"virtual_bms": [{"id": "vb", "profile": "pylontech_can_inverter", "packs": [],
                            "bounds": {"charge_current_limit": [0, 80]},
                            "floor": {"charge_current_limit": 20}}]}
    return VbmsManager(cfg, lambda d, f: None,
                       make_load_profile(FakeTemplateRegistry({"pylontech_can_inverter": FakeTemplate(_ROWS)})))


def test_command_bridge_subscribes_and_routes_clamped():
    m = _cmd_manager()
    pub = FakePublisher()
    topics = VbmsCommandBridge(m, lambda: pub).sync()
    assert "mbg/devices/vb/cmd/charge_current_limit" in topics
    assert set(topics) == set(pub.handlers)                          # exactly those were subscribed
    pub.deliver("mbg/devices/vb/cmd/charge_current_limit", "200")    # EMS over MQTT
    # the control plane holds the clamped value (what is SENT also depends on the bank being
    # vouched for — with no live pack it is 0, WK7-SAFE-2; tests/test_vbms_gate.py)
    assert m.instances["vb"].control.resolve()["charge_current_limit"] == 80


def test_command_bridge_sync_is_idempotent():
    m = _cmd_manager()
    pub = FakePublisher()
    bridge = VbmsCommandBridge(m, lambda: pub)
    first = bridge.sync()
    pub.unregistered.clear()
    second = bridge.sync()
    assert first == second                                          # same topic set
    assert set(pub.unregistered) == set(first)                      # re-sync drops its own first
    assert set(pub.handlers) == set(second)                         # then re-registers them


def test_command_bridge_honours_the_topic_pattern():
    m = _cmd_manager()
    pub = FakePublisher()
    VbmsCommandBridge(m, lambda: pub, topic_pattern="site7/bms/{device}").sync()
    assert "site7/bms/vb/cmd/charge_current_limit" in pub.handlers
    assert not any(t.startswith("mbg/devices/") for t in pub.handlers)


def test_command_bridge_no_publisher_is_a_noop():
    m = _cmd_manager()
    assert VbmsCommandBridge(m, lambda: None).sync() == []           # broker off → nothing subscribed


# ── VbmsStatePublisher: telemetry of what each instance sends (ADR-0037) ──

def _state_manager():
    """A manager with one instance sourcing two live packs, so the aggregation is non-empty."""
    now = 1000.0
    reg = FakeRegistry({
        "seplos-adr1": {"a": _entry("soc", 90, now), "b": _entry("pack_voltage", 53.5, now),
                        "c": _entry("battery_current", 10.0, now), "d": _entry("total_capacity", 280, now)},
        "seplos-adr2": {"a": _entry("soc", 70, now), "b": _entry("pack_voltage", 53.4, now),
                        "c": _entry("battery_current", 8.0, now), "d": _entry("total_capacity", 280, now)},
    })
    cfg = {"virtual_bms": [{"id": "vb", "profile": "pylontech_can_inverter",
                            "packs": ["seplos-adr1", "seplos-adr2"],
                            # a field is commandable only inside configured bounds (WK7-SAFE-3)
                            "bounds": {"charge_current_limit": [0, 80]},
                            "floor": {"charge_current_limit": 20}}]}
    return VbmsManager(cfg, make_read_value(reg, clock=lambda: now),
                       make_load_profile(FakeTemplateRegistry({"pylontech_can_inverter": FakeTemplate(_ROWS)})))


def test_state_publisher_emits_per_field_plus_status():
    m = _state_manager()
    pub = FakePublisher()
    VbmsStatePublisher(m, lambda: pub).publish_once()
    assert pub.published["mbg/devices/vb/soc"] == 80                 # live aggregation
    assert pub.published["mbg/devices/vb/charge_current_limit"] == 20  # control floor
    assert pub.published["mbg/devices/vb/armed"] == 0               # disarmed → 0
    status = pub.published["mbg/devices/vb/status"]
    assert status["armed"] is False and status["packs_live"] == 2 and status["packs_total"] == 2


def test_state_publisher_reflects_arm_and_commands():
    m = _state_manager()
    pub = FakePublisher()
    sp = VbmsStatePublisher(m, lambda: pub)
    m.command("vb", "charge_current_limit", "15")                   # EMS stages a limit
    m.instances["vb"].vbms.arm()                                    # arm (no executor thread here)
    sp.publish_once()
    assert pub.published["mbg/devices/vb/charge_current_limit"] == 15
    assert pub.published["mbg/devices/vb/armed"] == 1
    assert pub.published["mbg/devices/vb/status"]["armed"] is True


def test_state_publisher_honours_pattern_and_noop_without_publisher():
    m = _state_manager()
    pub = FakePublisher()
    VbmsStatePublisher(m, lambda: pub, topic_pattern="site7/bms/{device}").publish_once()
    assert "site7/bms/vb/soc" in pub.published
    assert not any(t.startswith("mbg/devices/") for t in pub.published)
    assert VbmsStatePublisher(m, lambda: None).publish_once() == 0   # broker off → nothing sent
