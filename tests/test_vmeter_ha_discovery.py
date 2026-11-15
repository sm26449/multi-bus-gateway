# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""HA-DISC-1: the virtual meters' Home Assistant discovery configs are retained,
so they are announced on (re)connect and on a config change — never on a timer
(prod republished 18 configs every 5 minutes, 259 times in 21 h)."""
from types import SimpleNamespace

import yaml

from multibus.virtual_meter_manager import VirtualMeterManager


class _Pub:
    def __init__(self):
        self.connected = True
        self.connects = 1
        self.config = SimpleNamespace(ha_discovery_enabled=True)
        self.discovery_calls = []

    def publish_vmeter_discovery(self, meters):
        self.discovery_calls.append([m["id"] for m in meters])
        return 9 * len(meters)

    def publish_state(self, *_a, **_k):
        return True


def _mgr(tmp_path, pub):
    cfg = tmp_path / "vm.yaml"
    cfg.write_text(yaml.safe_dump({"instances": [
        {"template": "em24", "port": 25550, "enabled": True},
        {"template": "fronius_ts", "port": 25551, "enabled": True}]}))
    return VirtualMeterManager({}, config_path=str(cfg),
                               templates_dir=str(tmp_path / "templates"), mqtt_publisher=pub)


def test_discovery_is_not_republished_on_a_timer(tmp_path):
    pub = _Pub()
    mgr = _mgr(tmp_path, pub)
    for _ in range(100):                       # ~17 min of 10 s ticks
        mgr._state_tick()
    assert pub.discovery_calls == [["em24", "fronius_ts"]]


def test_discovery_republished_after_a_reconnect(tmp_path):
    pub = _Pub()
    mgr = _mgr(tmp_path, pub)
    mgr._state_tick()
    pub.connects += 1                          # the broker came back
    mgr._state_tick()
    mgr._state_tick()
    assert len(pub.discovery_calls) == 2


def test_discovery_republished_after_a_config_change(tmp_path):
    pub = _Pub()
    mgr = _mgr(tmp_path, pub)
    mgr._state_tick()
    mgr.set_enabled("fronius_ts", False)       # a mutation through the API path
    mgr._state_tick()
    mgr._state_tick()
    assert pub.discovery_calls == [["em24", "fronius_ts"], ["em24"]]


def test_discovery_waits_for_a_connection(tmp_path):
    pub = _Pub()
    pub.connected = False
    mgr = _mgr(tmp_path, pub)
    mgr._state_tick()
    assert pub.discovery_calls == []
    pub.connected = True
    mgr._state_tick()
    mgr._state_tick()
    assert len(pub.discovery_calls) == 1


def test_explicit_publish_counts_as_announced(tmp_path):
    """main.py announces right after the first connect; the loop must not repeat it."""
    pub = _Pub()
    mgr = _mgr(tmp_path, pub)
    assert mgr.publish_ha_discovery() == 18
    mgr._state_tick()
    assert len(pub.discovery_calls) == 1
