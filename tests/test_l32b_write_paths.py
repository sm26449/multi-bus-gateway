# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""L3.2b — the server write paths beside PATCH (ADR-0031 §5):

- `POST /api/devices?dry_run=true&only=connection` judges the connection alone
  (no id, no name, no template, no probe device — L3-F3); the full dry run
  answers the would-be entry;
- advanced connection keys are judged on save, each in its range (L3-F7);
- components are created with their parent, all or nothing, and found before
  saving through a short-lived, subscribe-only link;
- `PATCH /api/endpoints/{id}/units/{unit}` renames one unit (L3-F12);
- the Fronius prefill names a template that exists (L3-F2) and the Solar API
  paths come from the templates (L3-F6);
- the read preview's budget is written into config.yaml on first load."""
import json
import logging
import pathlib
import re
import time
from types import SimpleNamespace

import pytest
import yaml

from multibus import mqtt_link
from multibus.config import Config
from tests.test_device_components import PORTAL, _Paho, _msg
from tests.test_devices import write_config
from tests.test_devices_api import make_app, needs_tc

ROOT = pathlib.Path(__file__).resolve().parent.parent

SETUP = """
devices:
  - id: tap
    name: Listens on ttyS9
    template: seplos_bms_v3_rtu_tap
    enabled: true
    connection: {protocol: rtu_tap, serial_port: /dev/ttyS9, baudrate: 19200, unit_id: 2}
  - id: inv1
    name: Inverter
    template: eastron_sdm630
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.80, unit_id: 1, retry_attempts: 99}
endpoints:
  - id: plant
    name: Plant
    template: eastron_sdm120
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.20, port: 502}
    units: [1, {unit_id: 2, name: Second}]
"""

GX_CONN = {'protocol': 'mqtt', 'broker': '192.0.2.10', 'port': 8883, 'tls': True, 'topic': f'N/{PORTAL}/#',
           'vars': {'portal': PORTAL, 'vebus': '274'}}


def _app(tmp_path):
    return make_app(tmp_path, extra_yaml="primary_device: false\n" + SETUP)


def _ids(cfg):
    return sorted(d.id for d in cfg.devices)


# ── only=connection (L3-F3) ─────────────────────────────────────────────────

@needs_tc
def test_only_connection_needs_no_id_and_stores_nothing(tmp_path):
    cfg, client = _app(tmp_path)
    path = pathlib.Path(cfg.config_path)
    before, ids = path.read_bytes(), _ids(cfg)
    res = client.post("/api/devices?dry_run=true&only=connection",
                      json={'connection': {'protocol': 'tcp', 'host': '192.0.2.81', 'unit_id': 1}})
    assert res.status_code == 200, res.text
    d = res.json()
    assert d['status'] == 'valid' and d['only'] == 'connection'
    assert d['bus'] == {'key': '192.0.2.81:502', 'where': '192.0.2.81:502', 'role': 'master',
                        'pollers': [], 'listeners': []}
    assert path.read_bytes() == before and _ids(cfg) == ids


@needs_tc
@pytest.mark.parametrize("conn,field,needle", [
    ({'protocol': 'tcp', 'host': '', 'unit_id': 1}, 'connection.host', 'required'),
    ({'protocol': 'rtu', 'serial_port': '/dev/ttyS9', 'baudrate': 19200, 'unit_id': 7}, None, 'tap'),
    ({'protocol': 'seplos_v2', 'serial_port': '/dev/ttyS8', 'unit_id': 0}, 'connection.mode', 'listen'),
    ({'protocol': 'tcp', 'host': '192.0.2.81', 'unit_id': 1, 'max_gap': 500}, 'connection.max_gap', '0..125'),
])
def test_only_connection_answers_the_connections_refusals_only(tmp_path, conn, field, needle):
    cfg, client = _app(tmp_path)
    path = pathlib.Path(cfg.config_path)
    before = path.read_bytes()
    res = client.post("/api/devices?dry_run=true&only=connection", json={'connection': conn})
    assert res.status_code == 422, res.text
    det = res.json()['detail']
    assert any(needle in e for e in det['errors']), det
    if field:
        assert field in det['fields'], det
    assert not [e for e in det['errors'] if re.match(r'^(id|name|template)\b', e)], det
    assert path.read_bytes() == before


@needs_tc
def test_only_goes_with_a_dry_run_and_only_for_the_connection(tmp_path):
    cfg, client = _app(tmp_path)
    ids = _ids(cfg)
    conn = {'protocol': 'tcp', 'host': '192.0.2.81'}
    assert client.post("/api/devices?only=connection", json={'connection': conn}).status_code == 422
    assert client.post("/api/devices?dry_run=true&only=meters", json={'connection': conn}).status_code == 422
    assert client.post("/api/devices?dry_run=true&only=connection", json={}).status_code == 422
    assert _ids(cfg) == ids


@needs_tc
def test_the_full_dry_run_answers_the_would_be_entry(tmp_path):
    cfg, client = _app(tmp_path)
    path = pathlib.Path(cfg.config_path)
    before = path.read_bytes()
    res = client.post("/api/devices?dry_run=true", json={
        'id': 'pack3', 'name': 'Pack 3', 'template': 'seplos_bms_v3_rtu_tap', 'enabled': False,
        'connection': {'protocol': 'rtu_tap', 'serial_port': '/dev/ttyS9', 'baudrate': 19200, 'unit_id': 3}})
    assert res.status_code == 200, res.text
    d = res.json()
    assert d['status'] == 'valid' and d['would'] == 'create'
    e = d['entry']
    assert e['id'] == 'pack3' and e['write_locked'] is True and e['opcua_enabled'] is False
    assert e['topic_prefix'] and '${' not in e['topic_prefix'] and e['bucket'] and '${' not in e['bucket']
    assert e['ha_node'].startswith('mbg_') and e['setup'] in ('none', 'pending')
    assert e['wire'] and e['protocol'] == 'rtu_tap'
    assert e['bus']['role'] == 'listen' and e['bus']['listeners'] == ['tap'] and e['bus']['pollers'] == []
    assert path.read_bytes() == before


# ── advanced connection keys (L3-F7) ────────────────────────────────────────

ADV_BAD = [('retry_attempts', 11), ('retry_attempts', 1.5), ('retry_delay', -1), ('stale_after_s', 'x'),
           ('max_gap', 126), ('illegal_registers', [70000]), ('illegal_registers', 'x'),
           ('startup_jitter_s', 4000), ('endpoint_min_gap_s', 61), ('connect_settle_s', 31),
           ('max_connections', 0), ('endpoint_wait_s', 0), ('share_transport', 'yes'), ('drop_all_zero', 1)]
ADV_OK = [('retry_attempts', 0), ('retry_delay', 0.5), ('stale_after_s', 0), ('max_gap', 0),
          ('illegal_registers', [50, '0x32']), ('startup_jitter_s', None), ('endpoint_min_gap_s', 0.2),
          ('connect_settle_s', None), ('max_connections', 3), ('endpoint_wait_s', 30),
          ('serialize_endpoint', False), ('drop_all_zero', True)]


@needs_tc
@pytest.mark.parametrize("key,value", ADV_BAD, ids=[f"{k}={v!r}" for k, v in ADV_BAD])
def test_an_advanced_key_out_of_range_is_refused_at_its_field(tmp_path, key, value):
    _cfg, client = _app(tmp_path)
    res = client.post("/api/devices", json={'id': 'n1', 'name': 'n', 'enabled': False, 'connection': {
        'protocol': 'tcp', 'host': '192.0.2.81', 'unit_id': 1, key: value}})
    assert res.status_code == 422, res.text
    assert f"connection.{key}" in res.json()['detail']['fields'], res.text


@needs_tc
@pytest.mark.parametrize("key,value", ADV_OK, ids=[f"{k}={v!r}" for k, v in ADV_OK])
def test_an_advanced_key_in_range_is_taken(tmp_path, key, value):
    _cfg, client = _app(tmp_path)
    res = client.post("/api/devices", json={'id': 'n1', 'name': 'n', 'enabled': False, 'connection': {
        'protocol': 'tcp', 'host': '192.0.2.81', 'unit_id': 1, key: value}})
    assert res.status_code == 200, res.text


@needs_tc
def test_a_stored_value_out_of_range_never_blocks_an_edit_of_something_else(tmp_path):
    cfg, client = _app(tmp_path)        # inv1 holds retry_attempts: 99, written by hand
    res = client.put("/api/devices/inv1", json={'id': 'inv1', 'name': 'Renamed', 'connection': {
        'protocol': 'tcp', 'host': '192.0.2.80', 'unit_id': 1, 'retry_attempts': 99}})
    assert res.status_code == 200, res.text
    res = client.put("/api/devices/inv1", json={'id': 'inv1', 'connection': {
        'protocol': 'tcp', 'host': '192.0.2.80', 'unit_id': 1, 'retry_attempts': 98}})
    assert res.status_code == 422 and 'connection.retry_attempts' in res.json()['detail']['fields']


def test_the_advanced_defaults_are_what_the_config_reads():
    from multibus.config import ModbusConfig
    from multibus.conn_advanced import ADVANCED, DEFAULTS, described
    m = ModbusConfig()
    for k, d in DEFAULTS.items():
        if d is None:
            continue
        assert getattr(m, k) == d, k
    assert {x['key'] for x in described()} == set(ADVANCED)


def test_the_served_defaults_are_the_checked_ones():
    """One source: /api/protocols' `defaults` (connection_matrix, L3.2a) say
    the defaults and ranges conn_advanced checks a save against."""
    from multibus.conn_advanced import ADVANCED, DEFAULTS
    from multibus.connection_matrix import defaults
    served = defaults()['modbus']
    for k, (kind, lo, hi, d, _w) in ADVANCED.items():
        assert k in served, k
        if d is not None:
            assert served[k]['value'] == d, k
        if lo is not None:
            assert (served[k]['min'], served[k]['max']) == (lo, hi), k
    assert served['startup_jitter_s']['source'] == 'gateway' and served['connect_settle_s']['source'] == 'template'
    assert DEFAULTS['startup_jitter_s'] is None and DEFAULTS['connect_settle_s'] is None


# ── components on create ────────────────────────────────────────────────────

@needs_tc
def test_components_are_created_with_their_parent(tmp_path):
    cfg, client = _app(tmp_path)
    res = client.post("/api/devices", json={
        'id': 'gx2', 'name': 'GX', 'template': 'victron_gx_mqtt', 'enabled': False, 'connection': GX_CONN,
        'components': [{'type': 'battery', 'instance': '288', 'name': 'Shunt'},
                       {'type': 'temperature', 'instance': 24}]})
    assert res.status_code == 200, res.text
    made = {d.id: d for d in cfg.component_devices('gx2')}
    assert sorted(made) == ['gx2-battery-288', 'gx2-temperature-24']
    assert made['gx2-battery-288'].name == 'Shunt' and made['gx2-battery-288'].write_locked is True
    assert sorted(c['id'] for c in res.json()['components']) == sorted(made)
    stored = cfg.get_raw_device('gx2')['components']
    assert all(c['mqtt']['topic_prefix'] == f"mbg/devices/{c['id']}" for c in stored)


@needs_tc
@pytest.mark.parametrize("comps,needle", [
    ([{'type': 'battery', 'instance': '288'}, {'type': 'nope', 'instance': '1'}], 'components[1].type'),
    ([{'type': 'battery', 'instance': '288'}, {'type': 'battery', 'instance': '288'}], 'already a component'),
    ([{'type': 'battery', 'instance': 'a/b'}], 'components[0].instance'),
    ([{'type': 'battery', 'instance': '1', 'id': 'inv1'}], 'already exists'),
    ('battery', 'components: a list'),
])
def test_components_on_create_are_all_or_nothing(tmp_path, comps, needle):
    cfg, client = _app(tmp_path)
    path = pathlib.Path(cfg.config_path)
    before, ids = path.read_bytes(), _ids(cfg)
    body = {'id': 'gx3', 'name': 'GX', 'template': 'victron_gx_mqtt', 'enabled': False, 'connection': GX_CONN,
            'components': comps}
    for q in ('?dry_run=true', ''):
        res = client.post(f"/api/devices{q}", json=body)
        assert res.status_code == 422, res.text
        assert needle in res.text, res.text
    assert path.read_bytes() == before and _ids(cfg) == ids


@needs_tc
def test_components_need_an_mqtt_parent(tmp_path):
    cfg, client = _app(tmp_path)
    res = client.post("/api/devices", json={'id': 'n2', 'name': 'n', 'enabled': False, 'template': 'eastron_sdm630',
                                            'connection': {'protocol': 'tcp', 'host': '192.0.2.82'},
                                            'components': [{'type': 'battery', 'instance': '1'}]})
    assert res.status_code == 422 and 'MQTT only' in res.text
    assert cfg.get_device('n2') is None


@needs_tc
def test_the_dry_run_lists_the_components_that_would_come(tmp_path):
    cfg, client = _app(tmp_path)
    res = client.post("/api/devices?dry_run=true", json={
        'id': 'gx4', 'name': 'GX', 'template': 'victron_gx_mqtt', 'enabled': False, 'connection': GX_CONN,
        'components': [{'type': 'battery', 'instance': '512', 'name': 'SEPLOS'}]})
    assert res.status_code == 200, res.text
    assert res.json()['entry']['components'] == [
        {'id': 'gx4-battery-512', 'type': 'battery', 'instance': '512', 'name': 'SEPLOS'}]
    assert cfg.get_device('gx4') is None


# ── finding components before saving ───────────────────────────────────────

@needs_tc
def test_components_are_found_before_saving_through_a_short_lived_link(tmp_path, monkeypatch):
    from multibus import component_discover as cd
    _cfg, client = _app(tmp_path)
    link = mqtt_link.MqttLink(("@discover",), SimpleNamespace(broker="h", port=1))
    link.client, link.connected = _Paho(), True
    closed = []
    opened = []

    def fake_open(conn):
        opened.append(dict(conn))
        return link
    monkeypatch.setattr(cd, "open_link", fake_open)
    monkeypatch.setattr(cd, "close", lambda ln: closed.append(ln))
    real_sleep = time.sleep

    def speak(s):
        if s != 2.0:
            return real_sleep(s)
        for t, v in [(f"N/{PORTAL}/battery/288/ProductName", "SmartShunt"),
                     (f"N/{PORTAL}/temperature/24/ProductName", "RuuviTag")]:
            link._on_message(link.client, None, _msg(t, v))
    monkeypatch.setattr("time.sleep", speak)
    res = client.post("/api/devices/components/discover",
                      json={'template': 'victron_gx_mqtt', 'connection': GX_CONN, 'seconds': 2})
    assert res.status_code == 200, res.text
    assert [(f['type'], f['instance'], f['product']) for f in res.json()['found']] == [
        ('battery', '288', 'SmartShunt'), ('temperature', '24', 'RuuviTag')]
    assert opened[0]['broker'] == '192.0.2.10' and closed == [link]
    assert link._taps == [] and not link._subs
    assert link.client.sent == []                     # subscribe only: nothing published


@needs_tc
@pytest.mark.parametrize("body,code,needle", [
    ({'template': 'eastron_sdm630', 'connection': GX_CONN}, 422, 'find its components'),
    ({'template': 'victron_gx_mqtt', 'connection': {'protocol': 'tcp', 'host': '192.0.2.1'}}, 422, 'MQTT only'),
    ({'template': 'victron_gx_mqtt', 'connection': {'protocol': 'mqtt'}}, 422, 'connection.broker'),
    ({'template': 'nope', 'connection': GX_CONN}, 422, 'not found'),
])
def test_finding_components_before_saving_is_refused_clearly(tmp_path, body, code, needle):
    _cfg, client = _app(tmp_path)
    res = client.post("/api/devices/components/discover", json=body)
    assert res.status_code == code and needle in res.text, res.text


@needs_tc
def test_a_broker_that_cannot_be_reached_is_409(tmp_path, monkeypatch):
    from multibus import component_discover as cd
    _cfg, client = _app(tmp_path)
    monkeypatch.setattr(cd, "open_link", lambda conn: None)
    res = client.post("/api/devices/components/discover", json={'template': 'victron_gx_mqtt', 'connection': GX_CONN})
    assert res.status_code == 409 and 'could not be reached' in res.text


def test_the_listening_time_is_bounded():
    from multibus.component_discover import seconds_of
    assert seconds_of(None) == 4.0 and seconds_of(0) == 1.0 and seconds_of(300) == 10.0 and seconds_of('x') == 4.0


# ── a unit's name (L3-F12) ──────────────────────────────────────────────────

@needs_tc
def test_a_units_name_is_patched_alone_and_checked_against_its_tag(tmp_path):
    cfg, client = _app(tmp_path)
    tag = client.get("/api/devices/plant-u1").headers['etag']
    stale = client.patch("/api/endpoints/plant/units/plant-u1", json={'name': 'X'}, headers={'If-Match': '"old"'})
    assert stale.status_code == 412 and stale.headers['etag'] == tag
    res = client.patch("/api/endpoints/plant/units/plant-u1", json={'name': 'First'}, headers={'If-Match': tag})
    assert res.status_code == 200, res.text
    d = res.json()
    assert d['device']['name'] == 'First' and d['restarted'] == []
    assert res.headers['etag'] == client.get("/api/devices/plant-u1").headers['etag'] != tag
    assert res.headers['mbg-if-match'] == 'checked'
    units = cfg.get_raw_endpoint('plant')['units']
    assert units == [{'unit_id': 1, 'name': 'First'}, {'unit_id': 2, 'name': 'Second'}]
    # by its unit address too, and '' gives the installation's naming back
    res = client.patch("/api/endpoints/plant/units/2", json={'name': ''})
    assert res.status_code == 200, res.text
    assert cfg.get_raw_endpoint('plant')['units'][1] == {'unit_id': 2}


@needs_tc
@pytest.mark.parametrize("url,body,code", [
    ("/api/endpoints/plant/units/plant-u9", {'name': 'x'}, 404),
    ("/api/endpoints/nope/units/1", {'name': 'x'}, 404),
    ("/api/endpoints/plant/units/1", {'name': 'x' * 81}, 422),
    ("/api/endpoints/plant/units/1", {'name': 'x', 'unit_id': 3}, 422),
    ("/api/endpoints/plant/units/1", {}, 422),
])
def test_a_unit_rename_refused(tmp_path, url, body, code):
    cfg, client = _app(tmp_path)
    path = pathlib.Path(cfg.config_path)
    before = path.read_bytes()
    assert client.patch(url, json=body).status_code == code
    assert path.read_bytes() == before


def test_the_unit_page_renames_through_the_unit_route_and_the_store():
    # L3.11b: a unit's name is edited in the Edit device modal (its unit save)
    js = (ROOT / "ui" / "js" / "app-device-edit.js").read_text(encoding="utf-8")
    body = js[js.index("async _deSaveUnit("):]
    body = body[:body.index("\n    },\n")]
    assert "DeviceStore.devWrite(" in body and "'PATCH'" in body and "/units/" in body
    assert "method: 'PUT'" not in body and "_endpointPutBody" not in body


# ── the UI probes and the Fronius paths (L3-F2, F3, F6) ─────────────────────

def test_no_probe_device_is_made_or_deleted_by_the_ui():
    for f in ("app-add-device.js", "app-meters.js", "adddev-model.js"):
        js = (ROOT / "ui" / "js" / f).read_text(encoding="utf-8")
        assert "/api/devices/x-dry-run" not in js, f
    assert "dry_run=true&only=connection" in (ROOT / "ui" / "js" / "app-add-device.js").read_text(encoding="utf-8")


def test_the_fronius_prefill_names_templates_that_exist():
    js = (ROOT / "ui" / "js" / "app-devices.js").read_text(encoding="utf-8")
    fn = js[js.index("_froniusDiscoverTemplate(cls"):]
    fn = fn[:fn.index("\n    },\n")]
    ids = set(re.findall(r"'(fronius_[a-z0-9_]+)'", fn))
    assert ids == {'fronius_solar_api_inverter', 'fronius_solar_api_inverter_3p', 'fronius_solar_api_meter'}
    for tid in ids:
        assert (ROOT / "multibus" / "device_templates" / f"{tid}.json").exists(), tid
    assert "template: 'fronius_solar_api'," not in js


# the calls the installation wizard typed by hand before L3-F6
OLD_SOLAR_API = {
    'fronius_solar_api_inverter': '/solar_api/v1/GetInverterRealtimeData.cgi?Scope=Device&DeviceId=${unit_id}&DataCollection=CommonInverterData',
    'fronius_solar_api_site': '/solar_api/v1/GetPowerFlowRealtimeData.fcgi',
    'fronius_solar_api_meter': '/solar_api/v1/GetMeterRealtimeData.cgi?Scope=Device&DeviceId=${unit_id}',
    'fronius_solar_api_inverter_3p': '/solar_api/v1/GetInverterRealtimeData.cgi?Scope=Device&DeviceId=${unit_id}&DataCollection=3PInverterData',
}


def test_the_solar_api_paths_come_from_the_templates_unchanged():
    from multibus.device_template import validate_template
    for tid, path in OLD_SOLAR_API.items():
        t = json.loads((ROOT / "multibus" / "device_templates" / f"{tid}.json").read_text(encoding="utf-8"))
        assert t['device_template']['protocol']['http']['path'] == path, tid
        assert validate_template(t) == [], tid
    js = (ROOT / "ui" / "js" / "app-plant-wizard.js").read_text(encoding="utf-8")
    assert "_SOLAR_API" not in js and not any(p.split("?")[0] in js for p in OLD_SOLAR_API.values())
    assert ".http_path" in js                       # the template list's key (L3.2a)


def test_a_template_http_path_is_checked():
    from multibus.device_template import http_path_errors
    assert http_path_errors({'http': {'path': '/a?x=${unit_id}&d=${device_id}'}}) == []
    assert http_path_errors({}) == []
    for bad in ({'http': {'path': 'no-slash'}}, {'http': {'path': '//host/x'}}, {'http': {'path': '/a b'}},
                {'http': {'path': '/a?${port}'}}, {'http': 'x'}, {'http': {'url': '/x'}}):
        assert http_path_errors(bad), bad


# ── the read preview's budget, written out (its own `preview:` section) ────

def test_a_file_without_the_preview_budget_gets_it_written_once(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="multibus.config")
    cfg = write_config(tmp_path)
    modbus_before = yaml.safe_load((tmp_path / "config.yaml").read_text()).get('modbus')
    assert (cfg.preview_max_requests, cfg.preview_listen_s) == (4, 10)
    doc = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert doc['preview'] == {'max_requests': 4, 'listen_s': 10}
    assert doc.get('modbus') == modbus_before
    assert not any(k.startswith('preview') for k in (doc.get('modbus') or {}))
    lines = [r for r in caplog.records if "preview" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    caplog.clear()
    before = (tmp_path / "config.yaml").read_bytes()
    again = Config(str(tmp_path / "config.yaml"))
    assert again.preview_max_requests == 4
    assert (tmp_path / "config.yaml").read_bytes() == before
    assert not [r for r in caplog.records if "preview" in r.getMessage()]


def test_writing_the_budget_touches_nothing_else(tmp_path):
    (tmp_path / "config.yaml").write_text("mqtt:\n  enabled: false\n", encoding="utf-8")
    cfg = Config(str(tmp_path / "config.yaml"))
    assert cfg.primary_device is None
    doc = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert doc['preview'] == {'max_requests': 4, 'listen_s': 10}
    assert 'modbus' not in doc and 'primary_device' not in doc
    assert Config(str(tmp_path / "config.yaml")).primary_device is None


def test_a_stored_budget_is_what_runs_and_a_bad_one_the_start(tmp_path, caplog):
    (tmp_path / "config.yaml").write_text(
        "mqtt:\n  enabled: false\npreview:\n  max_requests: 7\n  listen_s: 99\n", encoding="utf-8")
    cfg = Config(str(tmp_path / "config.yaml"))
    assert (cfg.preview_max_requests, cfg.preview_listen_s) == (7, 10)
    cfg.save_yaml_config()
    doc = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert doc['preview'] == {'max_requests': 7, 'listen_s': 10}
    assert not any(k.startswith('preview') for k in (doc.get('modbus') or {}))


def test_the_example_file_has_the_budget():
    doc = yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text())
    assert doc['preview'] == {'max_requests': 4, 'listen_s': 10}
    assert not any(k.startswith('preview') for k in (doc.get('modbus') or {}))


@needs_tc
def test_the_budget_is_read_and_set_in_settings(tmp_path):
    cfg, client = _app(tmp_path)
    assert client.get("/api/config/preview").json() == {'max_requests': 4, 'listen_s': 10}
    assert client.get("/api/config").json()['preview'] == {'max_requests': 4, 'listen_s': 10}
    assert 'preview_max_requests' not in client.get("/api/config/modbus").json()
    for bad in ({'max_requests': 0}, {'max_requests': 21}, {'listen_s': 61}, {'listen_s': 2.5},
                {'max_requests': True}, {'listen_s': '5'}, {'other': 1}):
        assert client.post("/api/config/preview", json=bad).status_code == 422, bad
    res = client.post("/api/config/preview", json={'max_requests': 6, 'listen_s': 20})
    assert res.status_code == 200 and res.json() == {'max_requests': 6, 'listen_s': 20}, res.text
    assert (cfg.preview_max_requests, cfg.preview_listen_s) == (6, 20)
    doc = yaml.safe_load(pathlib.Path(cfg.config_path).read_text())
    assert doc['preview'] == {'max_requests': 6, 'listen_s': 20}


def test_the_settings_page_shows_the_budget_with_its_help():
    html = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'data-cfgtab="modbus"' in html and 'id="cfgPreviewMaxRequests"' in html and 'id="cfgPreviewListen"' in html
    for lang in ("en", "ro"):
        tr = json.loads((ROOT / "ui" / "languages" / f"{lang}.json").read_text(encoding="utf-8"))
        for k in ("preview.budget.title", "preview.budget.requestsHint", "preview.budget.listenHint"):
            assert tr.get(k), (lang, k)
