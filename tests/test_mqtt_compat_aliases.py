# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Compat topic aliases (Migration B): every publish under `from` is ALSO
published under `to` with per-leaf renames, so not-yet-migrated consumers keep
receiving byte-identical old topics during a prefix migration."""
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt

from multibus.config import MQTTConfig
from multibus.mqtt_publisher import MQTTPublisher


ALIASES = [{"from": "meters/umg512", "to": "janitza/umg512",
            "leaves": {"energy/active/import": "energy/active/consumed"}}]


def _pub(aliases=None):
    p = MQTTPublisher(MQTTConfig(enabled=False, topic_prefix="meters/umg512",
                                 compat_aliases=aliases or []), [])
    p.connected = True
    p.client = MagicMock()
    p.client.publish.return_value = MagicMock(rc=mqtt.MQTT_ERR_SUCCESS)
    return p


def _topics(p):
    return [c.args[0] for c in p.client.publish.call_args_list]


def test_alias_republishes_under_old_prefix():
    p = _pub(ALIASES)
    assert p._publish("meters/umg512/voltage/l1_n", "230.1", retain=True)
    assert _topics(p) == ["meters/umg512/voltage/l1_n",
                          "janitza/umg512/voltage/l1_n"]
    # same payload + retain on both
    for c in p.client.publish.call_args_list:
        assert c.args[1] == "230.1" and c.kwargs["retain"] is True
    assert p.messages_aliased == 1
    assert p.messages_published == 1          # alias never double-counts


def test_alias_applies_leaf_renames():
    p = _pub(ALIASES)
    p._publish("meters/umg512/energy/active/import", "160321")
    assert _topics(p)[1] == "janitza/umg512/energy/active/consumed"


def test_non_matching_topics_not_aliased():
    p = _pub(ALIASES)
    p._publish("meters/fronius_rtu/power/active/total", "512")
    p._publish("homeassistant/sensor/x/config", "{}")
    assert all(not t.startswith("janitza/") for t in _topics(p))
    assert p.messages_aliased == 0


def test_no_aliases_configured_is_a_noop():
    p = _pub()
    p._publish("meters/umg512/frequency", "50.0")
    assert _topics(p) == ["meters/umg512/frequency"]


def test_state_and_alert_paths_flow_through_the_alias():
    """publish_state (vmeter state / data_health) funnels through _publish, so
    the retained state topics old consumers (alertd) watch keep updating."""
    p = _pub(ALIASES)
    p.publish_state("vmeter/em24_av53/state", '{"state":"ok"}')
    assert _topics(p) == ["meters/umg512/vmeter/em24_av53/state",
                          "janitza/umg512/vmeter/em24_av53/state"]


def test_alias_survives_a_config_roundtrip(tmp_path):
    """compat_aliases must survive load→save (a UI save must not drop the
    migration config — the exact trap the version stamp exists for)."""
    from multibus.config import Config
    import yaml
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "mqtt": {"topic_prefix": "meters/umg512", "compat_aliases": ALIASES}}))
    cfg = Config(str(cfg_file))
    assert cfg.mqtt.compat_aliases == ALIASES
    cfg.save_yaml_config()
    saved = yaml.safe_load(cfg_file.read_text())
    assert saved["mqtt"]["compat_aliases"] == ALIASES
