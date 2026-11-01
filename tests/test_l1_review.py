# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The independent review of lot L1 (ADR-0026): what it found, held.

- M1: a device's Home Assistant node is stored with it at creation, like its
  topic prefix; renaming the gateway, switching the scope or restoring a
  snapshot never renames an existing device's entities. A hand-written
  gateway-scope file keeps one generated id across restarts.
- M2: a default pattern equal to any scope's default is the default — a
  stale Settings form cannot pin the legacy one.
- minors: an unknown protocol is not meter-only; the ETag is no oracle for a
  secret; a scope switch orphans no retained discovery; a restore keeps the
  live identity unless asked; trailing `/` on an aggregate prefix; default
  patterns validated on save; `gateway` reserved; identity saves are admin
  only and audited; a spec inside a source is dropped too.
"""
import json
import re

import pytest
import yaml

from multibus.config import Config, SelectedRegister
from tests.test_devices import write_config
from tests.test_devices_api import make_app, needs_tc


def _file(tmp_path):
    return yaml.safe_load((tmp_path / "config.yaml").read_text())


def _new(client, did, host='192.0.2.9'):
    r = client.post("/api/devices", json={'id': did, 'enabled': False,
                                          'connection': {'protocol': 'tcp', 'host': host, 'unit_id': 1}})
    assert r.status_code == 200, r.text
    return r.json()['device']


def _ident(client, **body):
    r = client.post("/api/config/identity", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ── M1: the node is stored with the device ──────────────────────────────
@needs_tc
def test_a_new_device_under_gateway_scope_stores_its_node_and_legacy_stores_none(tmp_path):
    cfg, client = make_app(tmp_path)
    _new(client, 'old1')
    assert 'ha_node' not in (cfg.get_raw_device('old1').get('mqtt') or {})        # legacy: nothing stored
    assert cfg.get_device('old1').ha_node == 'mbg_dev_old1'
    _ident(client, gateway_id='site-a', identity_scope='gateway')
    _new(client, 'new1', '192.0.2.10')
    assert cfg.get_raw_device('new1')['mqtt']['ha_node'] == 'mbg_site-a_dev_new1'
    assert cfg.get_device('new1').ha_node == 'mbg_site-a_dev_new1'
    assert cfg.get_device('old1').ha_node == 'mbg_dev_old1'                      # the scope switch renamed nothing


@needs_tc
def test_renaming_the_gateway_or_switching_back_renames_no_existing_device(tmp_path):
    cfg, client = make_app(tmp_path)
    _ident(client, gateway_id='site-a', identity_scope='gateway')
    _new(client, 'inv1')
    _ident(client, gateway_id='site-b')
    assert cfg.get_device('inv1').ha_node == 'mbg_site-a_dev_inv1'
    _new(client, 'inv2', '192.0.2.11')
    assert cfg.get_device('inv2').ha_node == 'mbg_site-b_dev_inv2'                # a new device: the new id
    _ident(client, identity_scope='legacy')
    assert cfg.get_device('inv1').ha_node == 'mbg_site-a_dev_inv1'
    assert cfg.get_device('inv2').ha_node == 'mbg_site-b_dev_inv2'
    # a reload reads what is stored
    again = Config(str(tmp_path / "config.yaml"))
    assert again.get_device('inv1').ha_node == 'mbg_site-a_dev_inv1'
    # an edit keeps it, whatever the payload says
    r = client.put("/api/devices/inv1", json={'id': 'inv1', 'name': 'Renamed', 'enabled': False,
                                              'mqtt': {'ha_node': 'mbg_evil'},
                                              'connection': {'protocol': 'tcp', 'host': '192.0.2.9', 'unit_id': 1}})
    assert r.status_code == 200, r.text
    assert cfg.get_device('inv1').ha_node == 'mbg_site-a_dev_inv1'


@needs_tc
def test_the_publisher_announces_the_stored_node(tmp_path):
    from multibus.config import MQTTConfig
    from multibus.mqtt_publisher import MQTTPublisher
    pub = MQTTPublisher(MQTTConfig(enabled=False, ha_discovery_enabled=True), [])
    pub.connected = True
    sent = {}
    pub._publish = lambda t, p, retain=True: sent.__setitem__(t, p) or True
    reg = SelectedRegister(address=52, name="power", label="Power", unit="W", data_type="uint16", poll_group="normal")
    pub.publish_device_discovery("inv1", "Inv", "mbg/site-a/devices/inv1", [reg], ha_node="mbg_site-a_dev_inv1")
    assert all("/mbg_site-a_dev_inv1/" in t for t in sent)
    assert {json.loads(v)['unique_id'] for v in sent.values()} == {
        "mbg_site-a_dev_inv1_52_power", "mbg_site-a_dev_inv1_connectivity"}
    sent.clear()
    pub.publish_device_discovery("old1", "Old", "mbg/devices/old1", [reg])                 # nothing stored
    assert all("/mbg_dev_old1/" in t for t in sent)


@needs_tc
def test_a_component_and_an_installation_unit_store_their_node(tmp_path):
    from tests.test_device_components import GX_YAML
    cfg, client = make_app(tmp_path, extra_yaml=GX_YAML)
    _ident(client, gateway_id='site-a', identity_scope='gateway')
    r = client.post("/api/devices/gx/components", json={"type": "temperature", "instance": "23"})
    assert r.status_code == 200, r.text
    assert cfg.get_device('gx-temperature-23').ha_node == 'mbg_site-a_dev_gx-temperature-23'
    assert cfg.get_device('gx-battery-288').ha_node == 'mbg_dev_gx-battery-288'         # stored before: legacy
    r = client.post("/api/endpoints", json={"id": "plant", "name": "Plant", "template": "eastron_sdm120",
                                            "enabled": False, "units": [1, 2],
                                            "connection": {"protocol": "tcp", "host": "192.0.2.20", "port": 502}})
    assert r.status_code == 200, r.text
    units = cfg.endpoint_devices('plant')
    assert {d.ha_node for d in units} == {f"mbg_site-a_dev_{d.id}" for d in units}
    _ident(client, gateway_id='site-b')
    assert {d.ha_node for d in cfg.endpoint_devices('plant')} == {f"mbg_site-a_dev_{d.id}" for d in units}


def test_a_hand_written_gateway_file_keeps_one_id_across_restarts(tmp_path):
    write_config(tmp_path, extra_yaml="identity_scope: gateway\n")
    first = Config(str(tmp_path / "config.yaml"))
    gid = first.gateway_id
    assert re.match(r"^mbg-[0-9a-f]{6}$", gid)
    assert _file(tmp_path)['gateway'] == {'id': gid}                   # persisted at once
    assert Config(str(tmp_path / "config.yaml")).gateway_id == gid


# ── M2: a stale form cannot pin a scope's default ──────────────────────
@needs_tc
def test_a_stale_settings_form_after_a_scope_switch_keeps_the_scope_default(tmp_path):
    cfg, client = make_app(tmp_path)
    _ident(client, gateway_id='site-a', identity_scope='gateway')
    # the MQTT and InfluxDB tabs were loaded before the switch: they send the legacy defaults
    assert client.post("/api/config/mqtt", json={'default_topic_pattern': 'mbg/devices/{device}'}).status_code == 200
    assert client.post("/api/config/influxdb", json={'default_bucket_pattern': '{device}'}).status_code == 200
    assert cfg.default_topic_prefix('x') == 'mbg/site-a/devices/x'
    assert cfg.default_bucket('x') == 'site-a-x'
    again = Config(str(tmp_path / "config.yaml"))
    assert again.default_topic_prefix('x') == 'mbg/site-a/devices/x' and again.default_bucket('x') == 'site-a-x'
    _ident(client, identity_scope='legacy')
    assert cfg.default_topic_prefix('x') == 'mbg/devices/x'


# ── minor 1: an unknown protocol is not meter-only ─────────────────────
@needs_tc
def test_an_unknown_protocol_reads_unknown_and_gets_the_protocol_error(tmp_path):
    from multibus.connection_spec import conn_of, edit_errors, spec_of
    s = spec_of({'protocol': 'tpc', 'host': 'h'}, [])
    assert s['port'] == 'unknown' and conn_of(s) == {'protocol': 'tpc', 'host': 'h'}
    assert edit_errors(spec_of({'protocol': 'tcp'}, []), s) == []
    cfg, client = make_app(tmp_path)
    _new(client, 'inv1')
    r = client.put("/api/devices/inv1", json={'id': 'inv1', 'connection': {'protocol': 'tpc', 'host': '192.0.2.9'}})
    assert r.status_code == 422
    errs = r.json()['detail']['errors']
    assert any(e.startswith("connection.protocol: must be one of") for e in errs), errs
    assert not any('meters' in e for e in errs), errs


# ── minor 2: the ETag is no oracle for a secret ────────────────────────
@needs_tc
def test_the_etag_cannot_be_recomputed_and_follows_a_password_change(tmp_path):
    import hashlib
    cfg, client = make_app(tmp_path, extra_yaml="""
devices:
  - id: gx
    name: GX
    template: victron_gx_mqtt
    enabled: false
    connection: {protocol: mqtt, broker: 192.0.2.10, port: 1883, topic: 'N/x/#', password: hunter2}
""")
    tag = client.get("/api/devices/gx").headers['etag']
    raw = cfg.get_raw_device('gx')
    plain = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()
    assert plain[:32] not in tag                                         # not a bare hash of the entry
    cfg.load()
    assert client.get("/api/devices/gx").headers['etag'] == tag          # a reload changes nothing
    r = client.put("/api/devices/gx", json={'id': 'gx', 'connection': {'password': 'other'}},
                   headers={'If-Match': tag})
    assert r.status_code == 200, r.text
    assert client.get("/api/devices/gx").headers['etag'] != tag          # only the password changed


# ── minor 3: a scope switch orphans no retained discovery ─────────────
@needs_tc
def test_a_scope_switch_clears_no_existing_devices_discovery(tmp_path):
    from fastapi.testclient import TestClient

    from multibus.api import create_api
    from multibus.mqtt_publisher import MQTTPublisher
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: inv1
    name: Inverter
    template: eastron_sdm630
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.80, unit_id: 1}
""")
    pub = MQTTPublisher(cfg.mqtt, [])
    pub.connected = True
    sent = []
    pub._publish = lambda t, p, retain=True: sent.append((t, p)) or True
    app, _ = create_api(cfg, None, pub, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    before = {t for t, p in sent if p}
    assert any('/mbg_dev_inv1/' in t for t in before)
    sent.clear()
    r = client.post("/api/config/identity", json={'gateway_id': 'site-a', 'identity_scope': 'gateway'})
    assert r.status_code == 200, r.text
    cleared = [t for t, p in sent if p == '' and '/mbg_dev_inv1/' in t]
    assert cleared == [], cleared
    assert all('/mbg_site-a_dev_inv1/' not in t for t, _p in sent)


# ── minor 4: a restore keeps the live identity unless asked ─────────────
@needs_tc
def test_a_snapshot_restore_keeps_the_live_identity_unless_asked(tmp_path):
    cfg, client = make_app(tmp_path)
    sid = client.post("/api/config/snapshots", json={'label': 'legacy'}).json()
    sid = sid.get('id') or sid.get('snapshot', {}).get('id')
    assert sid, sid
    _ident(client, gateway_id='site-a', identity_scope='gateway')
    r = client.post(f"/api/config/snapshots/{sid}/restore?apply=false")
    assert r.status_code == 200, r.text
    assert (cfg.identity_scope, cfg.gateway_id) == ('gateway', 'site-a')
    assert _file(tmp_path)['identity_scope'] == 'gateway'
    r = client.post(f"/api/config/snapshots/{sid}/restore?apply=false&restore_identity=true")
    assert r.status_code == 200, r.text
    assert (cfg.identity_scope, cfg.gateway_id) == ('legacy', '')


# ── minor 7, 9 and the nits ────────────────────────────────────────────
@needs_tc
def test_an_aggregate_prefix_with_a_trailing_slash_is_read_as_the_runtime_reads_it(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/endpoints", json={"id": "plant", "name": "Plant", "template": "eastron_sdm120",
                                            "enabled": False, "units": [1],
                                            "connection": {"protocol": "tcp", "host": "192.0.2.20", "port": 502},
                                            "mqtt": {"aggregate_prefix": "pv/"}})
    assert r.status_code == 200, r.text


@needs_tc
@pytest.mark.parametrize("route,body,field", [
    ("/api/config/mqtt", {'default_topic_pattern': 'mbg/{gateway}/{device}'}, 'default_topic_pattern'),
    ("/api/config/mqtt", {'default_topic_pattern': 'mbg/+/{device}'}, 'default_topic_pattern'),
    ("/api/config/mqtt", {'default_topic_pattern': 'mbg//{device}'}, 'default_topic_pattern'),
    ("/api/config/influxdb", {'default_bucket_pattern': '{gateway}-{device}'}, 'default_bucket_pattern'),
])
def test_a_default_pattern_is_checked_on_save(tmp_path, route, body, field):
    cfg, client = make_app(tmp_path)
    before = (tmp_path / "config.yaml").read_bytes()
    r = client.post(route, json=body)
    assert r.status_code == 422, r.text
    assert field in r.json()['detail']['fields']
    assert (tmp_path / "config.yaml").read_bytes() == before


@needs_tc
def test_the_gateway_id_gateway_is_reserved_and_a_refused_save_changes_nothing(tmp_path):
    cfg, client = make_app(tmp_path)
    r = client.post("/api/config/identity", json={'gateway_id': 'gateway', 'identity_scope': 'gateway'})
    assert r.status_code == 422 and 'gateway_id' in r.json()['detail']['fields']
    assert (cfg.identity_scope, cfg.gateway_id) == ('legacy', '')


@needs_tc
def test_identity_changes_are_admin_only_and_audited(tmp_path):
    from tests.test_operator_role import _build_clients
    c = _build_clients(tmp_path)
    for who in (c.op, c.viewer):
        assert who.post("/api/config/identity", json={'gateway_id': 'site-a'}).status_code == 403
    assert c.admin.post("/api/config/identity", json={'gateway_id': 'site-a'}).status_code == 200
    log = (tmp_path / "audit.jsonl").read_text()
    assert 'identity' in log and 'site-a' in log


def test_a_spec_is_dropped_beside_the_connection_inside_it_and_inside_a_source():
    from multibus.connection_spec import drop_spec_keys
    body = {'id': 'x', 'connection_spec': {}, 'source_specs': [], 'connection': {'protocol': 'tcp', 'spec': {}},
            'sources': [{'id': 'a', 'protocol': 'tcp', 'spec': {}}, 'junk']}
    out = drop_spec_keys(body)
    assert out == {'id': 'x', 'connection': {'protocol': 'tcp'}, 'sources': [{'id': 'a', 'protocol': 'tcp'}, 'junk']}
    assert 'spec' in body['connection']                                  # the caller's payload untouched
