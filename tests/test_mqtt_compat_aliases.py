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


# ── audit DP-22/23/25: alias truth, config identity, retry-able clears ───────

def test_alias_counters_split_on_rc():
    p = _pub(ALIASES)
    p._publish("meters/umg512/frequency", "50.0")     # primary+alias both ok
    assert p.messages_aliased == 1 and p.aliases_failed == 0
    p.client.publish.return_value = MagicMock(rc=mqtt.MQTT_ERR_NO_CONN)
    # primary fails → no alias attempt either; simulate primary-ok/alias-fail
    calls = {"n": 0}
    def _pub_side(topic, payload, qos=0, retain=None):
        calls["n"] += 1
        rc = mqtt.MQTT_ERR_SUCCESS if calls["n"] % 2 == 1 else mqtt.MQTT_ERR_NO_CONN
        return MagicMock(rc=rc)
    p.client.publish.side_effect = _pub_side
    p._publish("meters/umg512/frequency", "50.1")
    assert p.aliases_failed == 1                       # failure counted as such


def test_update_config_rebuilds_aliases_and_reconnects_on_identity_change():
    p = _pub()
    assert p._compat_aliases == []
    recon = []
    p.reconnect = lambda: recon.append(1)
    import dataclasses
    # same identity → no reconnect; aliases rebuilt
    cfg2 = dataclasses.replace(p.config, compat_aliases=ALIASES)
    p.update_config(cfg2)
    assert len(p._compat_aliases) == 1 and recon == []
    # prefix change = identity change → reconnect (re-arms the LWT)
    cfg3 = dataclasses.replace(cfg2, topic_prefix="meters/renamed", enabled=True)
    p.update_config(cfg3)
    assert recon == [1]


def test_failed_discovery_clear_is_retried():
    from multibus.config import SelectedRegister
    p = _pub()
    reg = SelectedRegister(address=1, name="power", label="P", unit="W",
                           data_type="uint16", poll_group="normal")
    p.publish_device_discovery("dev1", "Dev", "meters/dev1", [reg])
    ghosts = set(p._device_discovery_topics["dev1"])
    # next republish drops the register; the CLEAR publishes fail
    p._publish = lambda t, pl, retain=None: bool(pl)   # empty payload → fail
    p.publish_device_discovery("dev1", "Dev", "meters/dev1", [])
    # the un-cleared ghost stays tracked → retried next time
    assert any(g in p._device_discovery_topics["dev1"] for g in ghosts)


def test_outage_drops_are_counted():
    from multibus.config import SelectedRegister
    p = _pub()
    p.connected = False
    reg = SelectedRegister(address=1, name="power", label="P", unit="W",
                           data_type="uint16", poll_group="normal")
    p.publish_register_data("normal", {1: {"register": reg, "value": 5}})
    assert p.publish_drops == 1
    assert p.get_stats()["publish_drops"] == 1


def test_a_devices_entities_follow_its_own_availability_topic():
    """An inverter asleep at night must be unavailable in HA even while the
    gateway and its primary meter are alive: value entities point at the
    device's availability (publish_device_availability), the connectivity
    binary_sensor at the gateway's status."""
    import json
    from multibus.config import SelectedRegister
    p = _pub()
    p.config.ha_discovery_enabled = True
    reg = SelectedRegister(address=40083, name="power_active_total", label="Active power", unit="W",
                           data_type="int16", poll_group="normal")
    p.publish_device_discovery("pv-u1", "Inverter 1", "pv/inverters/1", [reg])
    cfgs = {c.args[0]: json.loads(c.args[1]) for c in p.client.publish.call_args_list if c.args[0].startswith("homeassistant/")}
    ent = cfgs["homeassistant/sensor/mbg_dev_pv-u1/40083_power_active_total/config"]
    assert ent["availability_topic"] == "pv/inverters/1/availability" and ent["state_topic"].startswith("pv/inverters/1/")
    conn = cfgs["homeassistant/binary_sensor/mbg_dev_pv-u1/connectivity/config"]
    assert conn["state_topic"] == "pv/inverters/1/availability" and conn["availability_topic"] == "meters/umg512/status"

