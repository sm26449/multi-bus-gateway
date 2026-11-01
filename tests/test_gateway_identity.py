# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Gateway-scoped identity for new installs (ADR-0026 §3, FINDINGS ADD-11).

An existing install (a file without `identity_scope`) is `legacy`: its new
devices are named as today, its HA unique_ids stay `mbg_dev_<id>_…`, and the
next save writes `identity_scope: legacy` out. A first boot (no file) is
`gateway`, with a generated `gateway.id` that a save keeps."""
import json
import re

import yaml

from multibus.config import Config, MQTTConfig, SelectedRegister
from multibus.mqtt_publisher import MQTTPublisher
from tests.test_devices import write_config
from tests.test_devices_api import make_app, needs_tc

GW_RE = re.compile(r"^mbg-[0-9a-f]{6}$")


def _file(tmp_path):
    return yaml.safe_load((tmp_path / "config.yaml").read_text())


# ── the config ───────────────────────────────────────────────────────────
def test_a_file_without_the_key_is_legacy_and_names_new_devices_as_today(tmp_path):
    cfg = write_config(tmp_path)
    assert cfg.identity_scope == 'legacy' and cfg.gateway_id == ''
    assert cfg.default_topic_prefix('inv1') == 'mbg/devices/inv1'
    assert cfg.default_bucket('inv1') == 'inv1'
    cfg.save_yaml_config()
    data = _file(tmp_path)
    assert data['identity_scope'] == 'legacy'                  # written out (ADR-0024 style)
    assert 'gateway' not in data
    again = Config(str(tmp_path / "config.yaml"))
    assert again.identity_scope == 'legacy' and again.default_topic_prefix('x') == 'mbg/devices/x'


def test_a_first_boot_is_gateway_scoped_with_a_generated_id_stable_once_saved(tmp_path):
    cfg = Config(str(tmp_path / "config.yaml"))              # no file at all
    assert cfg.identity_scope == 'gateway'
    gid = cfg.gateway_id
    assert GW_RE.match(gid), gid
    assert cfg.default_topic_prefix('inv1') == f'mbg/{gid}/devices/inv1'
    assert cfg.default_bucket('inv1') == f'{gid}-inv1'
    cfg.save_yaml_config()
    data = _file(tmp_path)
    assert data['identity_scope'] == 'gateway' and data['gateway'] == {'id': gid}
    again = Config(str(tmp_path / "config.yaml"))
    assert again.gateway_id == gid and again.identity_scope == 'gateway'
    assert again.default_topic_prefix('inv1') == f'mbg/{gid}/devices/inv1'
    # a save of the fresh file writes no default pattern: the scope's stays the default
    assert 'default_topic_pattern' not in data['mqtt']
    assert 'default_bucket_pattern' not in data['influxdb']


def test_an_explicit_pattern_wins_and_gateway_is_a_placeholder_under_either_scope(tmp_path):
    cfg = write_config(tmp_path, extra_yaml="""
identity_scope: legacy
gateway: {id: site-a}
""")
    assert cfg.gateway_id == 'site-a' and cfg.identity_scope == 'legacy'
    assert cfg.default_topic_prefix('x') == 'mbg/devices/x'                    # the id alone changes nothing
    cfg.update_mqtt(default_topic_pattern='plant/{gateway}/{device}')
    assert cfg.default_topic_prefix('x') == 'plant/site-a/x'
    cfg.save_yaml_config()
    assert _file(tmp_path)['mqtt']['default_topic_pattern'] == 'plant/{gateway}/{device}'
    assert _file(tmp_path)['gateway'] == {'id': 'site-a'}
    cfg2 = write_config(tmp_path, extra_yaml="""
identity_scope: gateway
gateway: {id: site-b}
""")
    cfg2.update_influxdb(default_bucket_pattern='{device}-data')     # explicit
    assert cfg2.default_topic_prefix('x') == 'mbg/site-b/devices/x'
    assert cfg2.default_bucket('x') == 'x-data'
    cfg2.update_influxdb(default_bucket_pattern='{device}')          # a scope's default: the default (review M2)
    assert cfg2.default_bucket('x') == 'site-b-x'


def test_a_legacy_default_written_out_by_an_older_save_follows_a_scope_change(tmp_path):
    # every save before the scope existed wrote the default patterns out:
    # in a legacy file they are the default, not a choice — moving to
    # `gateway` (Settings) changes how new devices are named, and stays so
    raw = (tmp_path / "config.yaml")
    write_config(tmp_path)
    text = raw.read_text().replace(
        "  topic_prefix: janitza/umg512\n",
        "  topic_prefix: janitza/umg512\n  default_topic_pattern: mbg/devices/{device}\n")
    raw.write_text(text.replace("  bucket: janitza\n", "  bucket: janitza\n  default_bucket_pattern: '{device}'\n"))
    cfg = Config(str(raw))
    assert cfg.default_topic_prefix('x') == 'mbg/devices/x'
    cfg.set_identity(scope='gateway', gateway_id='site-c')
    assert cfg.default_topic_prefix('x') == 'mbg/site-c/devices/x'
    assert cfg.default_bucket('x') == 'site-c-x'
    cfg.save_yaml_config()
    again = Config(str(raw))
    assert again.default_topic_prefix('x') == 'mbg/site-c/devices/x' and again.default_bucket('x') == 'site-c-x'
    # and an explicit choice under `gateway` survives a reload
    again.update_influxdb(default_bucket_pattern='{gateway}_{device}')
    again.save_yaml_config()
    assert Config(str(raw)).default_bucket('x') == 'site-c_x'


def test_a_legacy_save_keeps_writing_its_default_patterns(tmp_path):
    cfg = write_config(tmp_path)
    cfg.save_yaml_config()
    data = _file(tmp_path)
    assert data['mqtt']['default_topic_pattern'] == 'mbg/devices/{device}'      # as every save before
    assert data['influxdb']['default_bucket_pattern'] == '{device}'


def test_a_bad_scope_or_id_in_the_file_is_legacy_and_ignored(tmp_path, caplog):
    cfg = write_config(tmp_path, extra_yaml="""
identity_scope: planet
gateway: {id: 'Not A Slug!'}
""")
    assert cfg.identity_scope == 'legacy' and cfg.gateway_id == ''
    assert 'identity_scope' in caplog.text and 'gateway.id' in caplog.text


def test_a_gateway_scope_without_an_id_gets_one(tmp_path):
    cfg = write_config(tmp_path, extra_yaml="identity_scope: gateway\n")
    assert GW_RE.match(cfg.gateway_id)


# ── Home Assistant ───────────────────────────────────────────────────────
def _pub(**cfg):
    p = MQTTPublisher(MQTTConfig(enabled=False, ha_discovery_enabled=True, **cfg), [])
    p.connected = True
    p._captured = {}
    p._publish = lambda t, payload, retain=True: p._captured.__setitem__(t, payload) or True
    return p


def _reg():
    return SelectedRegister(address=52, name="power", label="Power", unit="W",
                            data_type="uint16", poll_group="normal")


def test_ha_unique_ids_follow_the_stored_node():
    legacy = _pub()
    legacy.publish_device_discovery("inv1", "Inverter", "mbg/devices/inv1", [_reg()])
    topics = sorted(legacy._captured)
    assert any(t.startswith("homeassistant/sensor/mbg_dev_inv1/") for t in topics), topics
    uids = {json.loads(v)['unique_id'] for v in legacy._captured.values()}
    assert uids == {"mbg_dev_inv1_52_power", "mbg_dev_inv1_connectivity"}
    ids = {tuple(json.loads(v)['device']['identifiers']) for v in legacy._captured.values()}
    assert ids == {("mbg_dev_inv1",)}

    gw = _pub()
    gw.publish_device_discovery("inv1", "Inverter", "mbg/mbg-a1b2c3/devices/inv1", [_reg()],
                                ha_node="mbg_mbg-a1b2c3_dev_inv1")
    topics = sorted(gw._captured)
    assert all("/mbg_mbg-a1b2c3_dev_inv1/" in t for t in topics), topics
    uids = {json.loads(v)['unique_id'] for v in gw._captured.values()}
    assert uids == {"mbg_mbg-a1b2c3_dev_inv1_52_power", "mbg_mbg-a1b2c3_dev_inv1_connectivity"}
    ids = {tuple(json.loads(v)['device']['identifiers']) for v in gw._captured.values()}
    assert ids == {("mbg_mbg-a1b2c3_dev_inv1",)}
    # taking them back clears the scope's own topics
    gw._captured.clear()
    gw.clear_device_discovery("inv1", [_reg()], ha_node="mbg_mbg-a1b2c3_dev_inv1")
    assert gw._captured and all("/mbg_mbg-a1b2c3_dev_inv1/" in t for t in gw._captured)


def test_a_device_without_a_stored_node_has_the_legacy_one(tmp_path):
    cfg = write_config(tmp_path, extra_yaml="""
identity_scope: gateway
gateway: {id: site-a}
devices:
  - {id: old1, name: Old, template: '', enabled: false, connection: {protocol: tcp, host: 192.0.2.1}}
  - {id: new1, name: New, template: '', enabled: false, connection: {protocol: tcp, host: 192.0.2.2},
     mqtt: {ha_node: mbg_site-a_dev_new1}}
""")
    assert cfg.get_device('old1').ha_node == 'mbg_dev_old1'          # the scope names NEW devices only
    assert cfg.get_device('new1').ha_node == 'mbg_site-a_dev_new1'


# ── the API ──────────────────────────────────────────────────────────────
@needs_tc
def test_settings_read_and_write_the_identity(tmp_path):
    cfg, client = make_app(tmp_path)
    got = client.get("/api/config/identity").json()
    assert got['identity_scope'] == 'legacy' and got['gateway_id'] == ''
    assert got['defaults']['legacy']['topic'] == 'mbg/devices/{device}'
    assert got['defaults']['gateway']['topic'] == 'mbg/{gateway}/devices/{device}'
    for bad in ({'gateway_id': 'Bad Id'}, {'gateway_id': 'x'}, {'identity_scope': 'planet'},
                {'identity_scope': 'gateway', 'gateway_id': ''}):
        r = client.post("/api/config/identity", json=bad)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()['detail']['fields'], bad
    r = client.post("/api/config/identity", json={'gateway_id': 'site-a', 'identity_scope': 'gateway'})
    assert r.status_code == 200, r.text
    assert cfg.gateway_id == 'site-a' and cfg.identity_scope == 'gateway'
    data = _file(tmp_path)
    assert data['gateway'] == {'id': 'site-a'} and data['identity_scope'] == 'gateway'
    r = client.post("/api/devices", json={'id': 'inv9', 'enabled': False,
                                          'connection': {'protocol': 'tcp', 'host': '192.0.2.9', 'unit_id': 1}})
    assert r.status_code == 200, r.text
    d = r.json()['device']
    assert d['mqtt_topic_prefix'] == 'mbg/site-a/devices/inv9' and d['influxdb_bucket'] == 'site-a-inv9'


@needs_tc
def test_an_existing_install_creates_devices_byte_identically(tmp_path):
    cfg, client = make_app(tmp_path)
    r = client.post("/api/devices", json={'id': 'inv8', 'enabled': False,
                                          'connection': {'protocol': 'tcp', 'host': '192.0.2.8', 'unit_id': 1}})
    assert r.status_code == 200, r.text
    raw = cfg.get_raw_device('inv8')
    assert raw['mqtt']['topic_prefix'] == 'mbg/devices/inv8' and raw['influxdb']['bucket'] == 'inv8'
    assert _file(tmp_path)['identity_scope'] == 'legacy'


def test_the_gateway_placeholder_needs_an_id():
    from multibus.identity import pattern_error
    assert pattern_error('mbg/{gateway}/{device}', '') is not None
    assert pattern_error('mbg/{gateway}/{device}', 'site-a') is None
    assert pattern_error('mbg/devices/{device}', '') is None
