# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""`PATCH /api/devices/{id}` on the primary meter (ADR-0032 §6, FINDINGS L3-F19).

The primary (the gateway's own meter) is not a ``devices:`` entry: its
connection is the flat ``modbus:`` section, its HA flag the gateway's
``mqtt.ha_discovery_enabled``, its outputs the flat ``http_output:``,
``opcua_output:`` and ``rest_push:`` sections, its intervals its register
file. PATCH maps the same sections the dialog sends for any device onto them,
judged by the validators PUT and the sub-routes use; what does not apply to
the primary is refused per field, in the words its edit locks say — never
silently dropped. If-Match is required, a dry run stores nothing, one write,
one audit record, one restart decision. A PATCH that says what a PUT says
writes the same config.yaml, byte for byte."""
import json
import pathlib
from unittest import mock

import pytest

from tests.test_devices_api import make_app, needs_tc

PRIM = 'umg512'


def _tag(client, did=PRIM):
    r = client.get(f"/api/devices/{did}")
    assert r.status_code == 200, r.text
    return r.headers['etag']


def _patch(client, sections, tag=None, query=''):
    headers = {'If-Match': tag} if tag else {}
    return client.patch(f"/api/devices/{PRIM}{query}", json={'sections': sections}, headers=headers)


def _file(cfg) -> pathlib.Path:
    return pathlib.Path(cfg.config_path)


def _sub(tmp_path, name) -> pathlib.Path:
    """A second gateway's directory beside the first."""
    p = tmp_path / name
    p.mkdir()
    return p


def _fields(res):
    return res.json()['detail'].get('fields', {})


# ── If-Match ────────────────────────────────────────────────────────────────

@needs_tc
def test_a_primary_patch_without_if_match_is_428_and_changes_nothing(tmp_path):
    cfg, client = make_app(tmp_path)
    before = _file(cfg).read_bytes()
    res = _patch(client, {'connection': {'host': '192.0.2.9'}})
    assert res.status_code == 428, res.text
    assert 'If-Match' in res.json()['detail']['errors'][0]
    assert res.headers['etag'] == _tag(client)
    assert _file(cfg).read_bytes() == before and cfg.modbus.host == '192.168.1.207'


@needs_tc
def test_a_stale_if_match_on_the_primary_is_412_with_its_entry(tmp_path):
    cfg, client = make_app(tmp_path)
    tag = _tag(client)
    before = _file(cfg).read_bytes()
    res = _patch(client, {'connection': {'host': '192.0.2.9'}}, tag='"stale"')
    assert res.status_code == 412, res.text
    assert res.json()['detail']['current']['id'] == PRIM and res.headers['etag'] == tag
    assert _file(cfg).read_bytes() == before


# ── connection → the flat modbus: section ───────────────────────────────────

@needs_tc
def test_the_connection_maps_onto_the_modbus_section_and_reconnects(tmp_path):
    cfg, client = make_app(tmp_path)
    tag = _tag(client)
    res = _patch(client, {'connection': {'host': '192.0.2.9', 'unit_id': 3}}, tag=tag)
    assert res.status_code == 200, res.text
    d = res.json()
    assert d['status'] == 'updated' and d['restarted'] is True
    got = {(c['section'], c['field']): (c['from'], c['to']) for c in d['changes']}
    assert got[('connection', 'host')] == ('192.168.1.207', '192.0.2.9')
    assert got[('connection', 'unit_id')] == (1, 3)
    assert (cfg.modbus.host, cfg.modbus.port, cfg.modbus.unit_id) == ('192.0.2.9', 502, 3)
    assert 'host: 192.0.2.9' in _file(cfg).read_text()
    assert d['device']['id'] == PRIM and d['device']['connection']['host'] == '192.0.2.9'
    assert res.headers['etag'] == _tag(client) != tag
    assert res.headers['mbg-if-match'] == 'checked'


@needs_tc
@pytest.mark.parametrize('conn', [
    {'protocol': 'tcp', 'host': ''},
    {'port': 'x'},
    {'unit_id': 300},
    {'timeout': 45},
    {'host': '/dev/ttyUSB0'},
    {'retry_attempts': 99},
    # review 3: a boolean or a fraction is not a port or an address
    {'port': True},
    {'unit_id': 2.9},
    {'port': '502.5'},
    {'unit_id': False},
])
def test_the_primary_connection_is_judged_as_its_put_judges_it(tmp_path, conn):
    cfg, client = make_app(tmp_path)
    before = _file(cfg).read_bytes()
    put = client.put(f"/api/devices/{PRIM}?dry_run=true", json={'connection': conn})
    res = _patch(client, {'connection': conn}, tag=_tag(client))
    assert put.status_code == res.status_code == 422, (put.text, res.text)
    assert put.json()['detail']['errors'] == res.json()['detail']['errors']
    assert put.json()['detail'].get('fields') == res.json()['detail'].get('fields')
    assert _file(cfg).read_bytes() == before


@needs_tc
def test_the_primary_stays_modbus_tcp(tmp_path):
    cfg, client = make_app(tmp_path)
    before = _file(cfg).read_bytes()
    res = _patch(client, {'connection': {'protocol': 'rtu'}}, tag=_tag(client))
    assert res.status_code == 422, res.text
    lock = next(lk for lk in client.get(f"/api/devices/{PRIM}").json()['device']['locks']
                if lk['field'] == 'connection.protocol')
    assert _fields(res)['connection.protocol'] == lock['text'] and lock['code'] == 'primary'
    assert _file(cfg).read_bytes() == before


@needs_tc
@pytest.mark.parametrize('key,value', [
    ('serial_port', '/dev/ttyUSB0'), ('baudrate', 19200), ('parity', 'E'), ('url', 'http://192.0.2.1/'),
    ('max_gap', 0), ('bridge', 'b1'), ('stale_after_s', 60),
])
def test_a_connection_key_the_primary_has_not_is_refused_never_ignored(tmp_path, key, value):
    cfg, client = make_app(tmp_path)
    before = _file(cfg).read_bytes()
    res = _patch(client, {'connection': {'host': '192.0.2.9', key: value}}, tag=_tag(client))
    assert res.status_code == 422, res.text
    why = _fields(res)[f'connection.{key}']
    assert 'primary' in why and 'host' in why, why          # says what the primary's connection is
    assert _file(cfg).read_bytes() == before and cfg.modbus.host == '192.168.1.207'


# ── outputs → the flat sections and the gateway's HA flag ───────────────────

OUTPUTS = {'http_output': True,
           'opcua': {'enabled': True, 'fields': ['_G_ULN[0]']},
           'rest_push': {'enabled': True, 'url': 'http://192.0.2.5/in', 'interval_s': 30,
                         'headers': {'Authorization': 'Bearer s3cret'}}}


@needs_tc
def test_the_outputs_apply_live_and_store_what_the_sub_routes_and_put_store(tmp_path):
    cfg, client = make_app(_sub(tmp_path, 'a'))
    res = _patch(client, {'outputs': OUTPUTS}, tag=_tag(client))
    assert res.status_code == 200, res.text
    d = res.json()
    assert d['restarted'] is False and 's3cret' not in res.text
    fields = {c['field'] for c in d['changes'] if c['section'] == 'outputs'}
    assert {'http_output.enabled', 'rest_push.url'} <= fields, d['changes']
    dev = d['device']
    assert dev['http_output_enabled'] is True and dev['ha_discovery_enabled'] is True
    assert dev['opcua_fields'] == ['_G_ULN[0]'] and dev['rest_push_enabled'] is True
    # the same through the old routes, on a second gateway: the same file
    cfg2, c2 = make_app(_sub(tmp_path, 'b'))
    for url, body in [('http-output', {'enabled': True}),
                      ('opcua-output', {'enabled': True, 'fields': ['_G_ULN[0]']}),
                      ('rest-push', OUTPUTS['rest_push'])]:
        r = c2.post(f"/api/devices/{PRIM}/{url}", json=body)
        assert r.status_code == 200, (url, r.text)
    assert _file(cfg).read_bytes() == _file(cfg2).read_bytes()


@needs_tc
@pytest.mark.parametrize('out,field', [
    ({'mqtt': False}, 'outputs.mqtt'),
    ({'influxdb': {'enabled': False}}, 'outputs.influxdb'),
    ({'mqtt': {'publish_mode': 'all'}}, 'outputs.mqtt.publish_mode'),
    ({'influxdb': {'write_interval': 30}}, 'outputs.influxdb.write_interval'),
    # review 1: the gateway-wide switch is not one of the primary's outputs
    ({'ha_discovery': False}, 'outputs.ha_discovery'),
])
def test_an_output_the_primary_cannot_change_is_refused_in_its_locks_words(tmp_path, out, field):
    cfg, client = make_app(tmp_path)
    before = _file(cfg).read_bytes()
    res = _patch(client, {'outputs': out}, tag=_tag(client))
    assert res.status_code == 422, res.text
    locks = {lk['field']: lk for lk in client.get(f"/api/devices/{PRIM}").json()['device']['locks']}
    lock = locks[field.split('.', 2)[0] + '.' + field.split('.', 2)[1]]
    assert lock['code'] == 'primary' and _fields(res)[field] == lock['text']
    assert _file(cfg).read_bytes() == before
    assert cfg.mqtt.ha_discovery_enabled is True
    if field == 'outputs.ha_discovery':
        assert 'Settings → MQTT' in lock['text']


@needs_tc
def test_what_the_primary_already_is_may_be_said_again(tmp_path):
    """A dialog sends the whole section back: a fixed field said as it is is
    no change, never a refusal."""
    cfg, client = make_app(tmp_path)
    before = _file(cfg).read_bytes()
    dev = client.get(f"/api/devices/{PRIM}").json()['device']
    res = _patch(client, {'outputs': {'mqtt': True, 'influxdb': {'enabled': True}, 'ha_discovery': True},
                          'polling': {'enabled': True},
                          'identity': {'name': dev['name'], 'type': dev['type']},
                          'model': {'template': dev['template']},
                          'connection': {'protocol': 'tcp', 'host': dev['connection']['host']}},
                 tag=_tag(client))
    assert res.status_code == 200, res.text
    assert res.json()['status'] == 'unchanged' and res.json()['changes'] == []
    assert _file(cfg).read_bytes() == before


# ── polling, identity, model ────────────────────────────────────────────────

@needs_tc
def test_the_primary_poll_groups_apply_live_as_its_poll_group_route(tmp_path):
    cfg, client = make_app(tmp_path)
    res = _patch(client, {'polling': {'poll_groups': {'realtime': 2}}}, tag=_tag(client))
    assert res.status_code == 200, res.text
    d = res.json()
    assert d['restarted'] is False
    assert {'section': 'polling', 'field': 'poll_groups.realtime', 'from': 1, 'to': 2.0} in d['changes']
    assert client.get(f"/api/devices/{PRIM}/poll-groups").json()['poll_groups']['realtime']['interval'] == 2.0
    assert cfg.poll_groups['realtime'].interval == 2.0
    bad = _patch(client, {'polling': {'poll_groups': {'realtime': 0}}}, tag=_tag(client))
    assert bad.status_code == 422 and 'polling.poll_groups' in bad.text


@needs_tc
@pytest.mark.parametrize('sections,field,lock_field', [
    ({'polling': {'enabled': False}}, 'polling.enabled', 'polling.enabled'),
    ({'identity': {'name': 'Main meter'}}, 'identity.name', 'identity.name'),
    ({'identity': {'type': 'inverter'}}, 'identity.type', 'identity.type'),
    ({'model': {'template': 'eastron_sdm630'}}, 'model.template', 'template'),
])
def test_a_fixed_field_of_the_primary_is_refused_in_its_locks_words(tmp_path, sections, field, lock_field):
    cfg, client = make_app(tmp_path)
    before = _file(cfg).read_bytes()
    res = _patch(client, sections, tag=_tag(client))
    assert res.status_code == 422, res.text
    locks = {lk['field']: lk for lk in client.get(f"/api/devices/{PRIM}").json()['device']['locks']}
    assert locks[lock_field]['code'] == 'primary'
    assert _fields(res)[field] == locks[lock_field]['text']
    assert _file(cfg).read_bytes() == before


@needs_tc
def test_replace_hardware_is_not_offered_for_the_primary(tmp_path):
    cfg, client = make_app(tmp_path)
    before = _file(cfg).read_bytes()
    res = _patch(client, {'connection': {'unit_id': 2}}, tag=_tag(client), query='?intent=replace')
    assert res.status_code == 422 and 'intent' in _fields(res), res.text
    assert _file(cfg).read_bytes() == before


# ── the dry run ─────────────────────────────────────────────────────────────

@needs_tc
def test_a_primary_dry_run_answers_the_diff_and_stores_nothing(tmp_path):
    cfg, client = make_app(tmp_path)
    before = _file(cfg).read_bytes()
    regs_before = (tmp_path / 'selected_registers.json').read_bytes()
    res = _patch(client, {'connection': {'timeout': 5}, 'outputs': {'http_output': True},
                          'polling': {'poll_groups': {'realtime': 3}}}, query='?dry_run=true')
    assert res.status_code == 200, res.text
    d = res.json()
    assert set(d) == {'status', 'would', 'changes', 'restart_required', 'effects', 'locks'}
    assert d['status'] == 'valid' and d['would'] == 'update_restart' and d['restart_required'] is True
    got = {(c['section'], c['field']): (c['from'], c['to']) for c in d['changes']}
    assert got[('connection', 'timeout')] == (3, 5.0)
    assert got[('outputs', 'http_output.enabled')] == (False, True)
    assert got[('polling', 'poll_groups.realtime')] == (1, 3.0)
    assert any('reconnect' in e for e in d['effects']), d['effects']
    assert d['locks'] == client.get(f"/api/devices/{PRIM}").json()['device']['locks']
    # nothing stored, nothing changed in memory
    assert _file(cfg).read_bytes() == before
    assert (tmp_path / 'selected_registers.json').read_bytes() == regs_before
    assert cfg.modbus.timeout == 3 and cfg.http_output_primary_enabled is False
    assert cfg.poll_groups['realtime'].interval == 1


@needs_tc
def test_outputs_and_intervals_on_the_primary_never_restart(tmp_path):
    _cfg, client = make_app(tmp_path)
    d = _patch(client, {'outputs': {'http_output': True, 'opcua': {'enabled': False}},
                        'polling': {'poll_groups': {'realtime': 2}}}, query='?dry_run=true').json()
    assert d['would'] == 'update' and d['restart_required'] is False
    assert any('live' in e for e in d['effects']), d['effects']


# ── PATCH ≡ PUT ─────────────────────────────────────────────────────────────

@needs_tc
@pytest.mark.parametrize('conn,status', [
    ({'protocol': 'tcp', 'host': '192.0.2.9', 'port': 1502, 'unit_id': 3, 'timeout': 5,
      'retry_attempts': 2, 'retry_delay': 0.5}, 'updated'),
    # numbers as text and as whole floats: stored as the numbers they are
    ({'protocol': 'tcp', 'host': '192.0.2.9', 'port': '1502', 'unit_id': 3.0, 'retry_attempts': '4',
      'retry_delay': 2}, 'updated'),
    # the connection said as it is: neither writes anything
    ({'protocol': 'tcp', 'host': '192.168.1.207'}, 'unchanged'),
])
def test_a_patch_that_says_what_a_put_says_writes_the_same_file(tmp_path, conn, status):
    cfg_a, a = make_app(_sub(tmp_path, 'put'))
    original = _file(cfg_a).read_bytes()
    r = a.put(f"/api/devices/{PRIM}", json={'connection': conn}, headers={'If-Match': _tag(a)})
    assert r.status_code == 200 and r.json()['status'] == status, r.text
    cfg_b, b = make_app(_sub(tmp_path, 'patch'))
    r = _patch(b, {'connection': conn}, tag=_tag(b))
    assert r.status_code == 200 and r.json()['status'] == status, r.text
    assert r.json()['restarted'] is (status == 'updated')
    assert _file(cfg_a).read_bytes() == _file(cfg_b).read_bytes()
    if status == 'unchanged':
        assert _file(cfg_b).read_bytes() == original
    assert r.json()['device'] == a.get(f"/api/devices/{PRIM}").json()['device']
    for c in (cfg_a, cfg_b):
        m = c.modbus
        assert type(m.port) is int and type(m.unit_id) is int and type(m.retry_attempts) is int
        assert type(m.retry_delay) is float


@needs_tc
def test_the_primary_put_is_unchanged(tmp_path):
    """Compat: PUT answers as before (its own words, its own shape)."""
    cfg, client = make_app(tmp_path)
    m = cfg.modbus
    same = {'connection': {'protocol': 'tcp', 'host': m.host, 'port': m.port, 'unit_id': m.unit_id,
                           'timeout': m.timeout}}
    assert client.put(f"/api/devices/{PRIM}?dry_run=true", json=same).json() == {
        'status': 'valid', 'would': 'unchanged'}
    r = client.put(f"/api/devices/{PRIM}", json={'connection': {'host': '192.0.2.10'}})
    assert r.status_code == 200 and r.json()['status'] == 'updated' and set(r.json()) == {'status', 'device'}
    assert cfg.modbus.host == '192.0.2.10'


# ── one write, one audit record, one restart decision ───────────────────────

@needs_tc
def test_one_primary_patch_is_one_audit_record(tmp_path):
    cfg, client = make_app(tmp_path)
    audit = _file(cfg).parent / "audit.jsonl"

    def n():
        return sum(1 for ln in (audit.read_text().splitlines() if audit.exists() else [])
                   if f'PATCH /api/devices/{PRIM}' in ln)
    k = n()
    res = _patch(client, {'connection': {'host': '192.0.2.9'}, 'outputs': {'http_output': True},
                          'polling': {'poll_groups': {'realtime': 2}}}, tag=_tag(client))
    assert res.status_code == 200, res.text
    assert n() == k + 1
    line = [json.loads(ln) for ln in audit.read_text().splitlines() if f'PATCH /api/devices/{PRIM}' in ln][-1]
    assert line['status'] == 'ok'


@needs_tc
def test_one_primary_patch_is_one_config_write(tmp_path):
    cfg, client = make_app(tmp_path)
    with mock.patch.object(type(cfg), 'save_yaml_config', autospec=True,
                           side_effect=type(cfg).save_yaml_config) as save:
        res = _patch(client, {'connection': {'host': '192.0.2.9'}, 'outputs': OUTPUTS}, tag=_tag(client))
    assert res.status_code == 200, res.text
    assert save.call_count == 1


def _app_with_meter_client(tmp_path):
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    mc = mock.MagicMock(name='modbus_client')
    app, _ = create_api(cfg, mc, None, None, devices=[(d, mc if d.primary else None) for d in cfg.devices])
    return cfg, TestClient(app, raise_server_exceptions=False), mc


@needs_tc
def test_the_meter_reconnects_once_for_a_connection_and_never_for_an_output(tmp_path):
    cfg, client, mc = _app_with_meter_client(tmp_path)
    mc.reconnect.reset_mock()
    res = _patch(client, {'outputs': {'http_output': True}, 'polling': {'poll_groups': {'realtime': 2}}},
                 tag=_tag(client))
    assert res.status_code == 200 and res.json()['restarted'] is False, res.text
    assert mc.reconnect.call_count == 0
    res = _patch(client, {'connection': {'host': '192.0.2.9'}}, tag=_tag(client))
    assert res.status_code == 200 and res.json()['restarted'] is True, res.text
    assert mc.reconnect.call_count == 1
    assert mc.update_config.call_args[0][0].host == '192.0.2.9'
    # a dry run reconnects nothing
    _patch(client, {'connection': {'host': '192.0.2.10'}}, query='?dry_run=true')
    assert mc.reconnect.call_count == 1


# ── the primary's tag covers what PATCH changes (L3.11-F1) ──────────────────

@needs_tc
def test_an_output_change_on_the_primary_moves_its_etag(tmp_path):
    _cfg, client = make_app(tmp_path)
    tag = _tag(client)
    res = _patch(client, {'outputs': {'http_output': True}}, tag=tag)
    assert res.status_code == 200, res.text
    assert _tag(client) != tag
    # a second tab holding the old tag is told
    assert _patch(client, {'outputs': {'http_output': False}}, tag=tag).status_code == 412
    # the old routes move it too
    tag = _tag(client)
    assert client.post(f"/api/devices/{PRIM}/opcua-output", json={'enabled': False}).status_code == 200
    assert _tag(client) != tag


# ── GET says what the dialog needs ──────────────────────────────────────────

@needs_tc
def test_get_says_the_primarys_sections_values_and_locks(tmp_path):
    _cfg, client = make_app(tmp_path)
    dev = client.get(f"/api/devices/{PRIM}").json()['device']
    assert dev['primary'] is True and dev['summary'] and dev['summary']['text']
    assert {'host', 'port', 'unit_id', 'timeout', 'retry_attempts', 'retry_delay'} <= set(dev['connection'])
    assert 'rest_push' in dev and 'publishing' in dev and 'sinks' in dev
    fields = {lk['field'] for lk in dev['locks'] if lk['code'] == 'primary'}
    assert {'connection.protocol', 'identity.name', 'identity.type', 'template', 'polling.enabled',
            'outputs.mqtt', 'outputs.influxdb'} <= fields, dev['locks']
    # what each section of PATCH takes for this device
    assert dev['edit_sections'] == {
        'connection': ['protocol', 'host', 'port', 'unit_id', 'timeout', 'retry_attempts', 'retry_delay'],
        'polling': ['poll_groups'],
        'identity': [],
        'outputs': ['http_output', 'opcua', 'rest_push'],
        'model': [],
    }
    # an ordinary device takes every section
    _c2, c2 = make_app(_sub(tmp_path, 'o'), extra_yaml="""
devices:
  - id: inv1
    template: eastron_sdm630
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.80, unit_id: 1}
""")
    ordinary = c2.get("/api/devices/inv1").json()['device']['edit_sections']
    assert ordinary['connection'] is None                    # any key its connection has
    assert ordinary['polling'] == ['enabled', 'poll_groups'] and ordinary['model'] == ['template']


# ── review 2026-11-01 ───────────────────────────────────────────────────────

@needs_tc
@pytest.mark.parametrize('method', ['put', 'patch'])
def test_a_whole_float_retry_count_is_stored_as_a_whole_number(tmp_path, method):
    """Review 2: 4.0 was stored as a float — range(4.0) failed every read
    cycle of the meter, and again after a restart."""
    cfg, client = make_app(tmp_path)
    conn = {'retry_attempts': 4.0, 'retry_delay': 2}
    r = (client.put(f"/api/devices/{PRIM}", json={'connection': conn}) if method == 'put'
         else _patch(client, {'connection': conn}, tag=_tag(client)))
    assert r.status_code == 200, r.text
    assert cfg.modbus.retry_attempts == 4 and type(cfg.modbus.retry_attempts) is int
    assert cfg.modbus.retry_delay == 2.0 and type(cfg.modbus.retry_delay) is float
    assert 'retry_attempts: 4\n' in _file(cfg).read_text()


def test_a_float_retry_count_already_stored_heals_at_load(tmp_path):
    from multibus.config import Config
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    text = _file(cfg).read_text().replace("  unit_id: 1\n", "  unit_id: 1\n  retry_attempts: 4.0\n  retry_delay: 2\n", 1)
    _file(cfg).write_text(text)
    again = Config(str(_file(cfg)))
    assert again.modbus.retry_attempts == 4 and type(again.modbus.retry_attempts) is int
    assert again.modbus.retry_delay == 2.0 and type(again.modbus.retry_delay) is float
    # what cannot be read keeps the default, never stops the gateway
    _file(cfg).write_text(text.replace('retry_attempts: 4.0', 'retry_attempts: lots'))
    assert Config(str(_file(cfg))).modbus.retry_attempts == 3


@needs_tc
def test_what_get_serves_comes_back_as_no_change(tmp_path):
    """Review 4: GET's connection (with the keys a Modbus TCP meter does not
    use) sent back whole is no change — never seven refusals."""
    cfg, client = make_app(tmp_path)
    before = _file(cfg).read_bytes()
    dev = client.get(f"/api/devices/{PRIM}").json()['device']
    dry = _patch(client, {'connection': dev['connection']}, query='?dry_run=true')
    assert dry.status_code == 200 and dry.json()['would'] == 'unchanged' and dry.json()['changes'] == [], dry.text
    res = _patch(client, {'connection': dev['connection']}, tag=_tag(client))
    assert res.status_code == 200 and res.json()['status'] == 'unchanged' and res.json()['changes'] == [], res.text
    # a change among them is still refused
    bad = _patch(client, {'connection': dict(dev['connection'], parity='E')}, tag=_tag(client))
    assert bad.status_code == 422 and 'connection.parity' in _fields(bad), bad.text
    assert _file(cfg).read_bytes() == before


@needs_tc
def test_an_output_said_in_its_default_form_is_no_change(tmp_path):
    """Review 5: a disabled push without a URL and OPC UA on with every value
    are what the file keeps as nothing: no change said, no tag moved."""
    cfg, client = make_app(tmp_path)
    before, tag = _file(cfg).read_bytes(), _tag(client)
    res = _patch(client, {'outputs': {'rest_push': {'enabled': False, 'url': ''}, 'opcua': {'enabled': True}}},
                 tag=tag)
    assert res.status_code == 200 and res.json()['status'] == 'unchanged', res.text
    assert res.json()['changes'] == [] and res.headers['etag'] == tag == _tag(client)
    assert _file(cfg).read_bytes() == before
    # the old routes keep memory as the file keeps it too
    assert client.post(f"/api/devices/{PRIM}/rest-push", json={'enabled': False, 'url': ''}).status_code == 200
    assert client.post(f"/api/devices/{PRIM}/opcua-output", json={'enabled': True}).status_code == 200
    assert _tag(client) == tag


@needs_tc
def test_after_a_reload_the_tag_and_the_device_are_what_the_patch_answered(tmp_path):
    cfg, client = make_app(tmp_path)
    res = _patch(client, {'outputs': {'rest_push': {'enabled': False, 'url': 'http://192.0.2.5/in'},
                                      'opcua': {'enabled': False}, 'http_output': True}}, tag=_tag(client))
    assert res.status_code == 200 and res.json()['status'] == 'updated', res.text
    cfg.load()                                      # what the file says, read again
    assert _tag(client) == res.headers['etag']


@needs_tc
def test_a_save_that_fails_puts_everything_back(tmp_path, monkeypatch):
    cfg, client = make_app(tmp_path)
    before, tag = _file(cfg).read_bytes(), _tag(client)

    def boom():
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(cfg, 'save_yaml_config', boom)
    res = _patch(client, {'connection': {'host': '192.0.2.9'}, 'outputs': OUTPUTS}, tag=tag)
    assert res.status_code == 503, res.text
    assert 'nothing was saved' in res.json()['detail']['errors'][0]
    assert cfg.modbus.host == '192.168.1.207' and cfg.http_output_primary_enabled is False
    assert cfg.rest_push_primary == {} and cfg.opcua_primary == {}
    assert _file(cfg).read_bytes() == before and _tag(client) == tag


@needs_tc
def test_intervals_saved_before_a_config_that_fails_are_said(tmp_path, monkeypatch):
    cfg, client = make_app(tmp_path)

    def boom():
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(cfg, 'save_yaml_config', boom)
    res = _patch(client, {'connection': {'host': '192.0.2.9'}, 'polling': {'poll_groups': {'realtime': 2}}},
                 tag=_tag(client))
    assert res.status_code == 503, res.text
    d = res.json()['detail']
    assert d['partial'] == {'saved': ['polling.poll_groups'], 'failed': ['entry']}
    assert cfg.poll_groups['realtime'].interval == 2.0 and cfg.modbus.host == '192.168.1.207'


@needs_tc
@pytest.mark.parametrize('method', ['put', 'patch'])
def test_a_rebuild_that_fails_after_the_save_says_it_was_saved(tmp_path, monkeypatch, method):
    """Review 9: the file is written, the running devices are not rebuilt —
    said so (restart the gateway), never a bare 500."""
    cfg, client = make_app(tmp_path)
    tag = _tag(client)

    def boom():
        raise RuntimeError("rebuild")
    monkeypatch.setattr(cfg, '_build_devices', boom)
    r = (client.put(f"/api/devices/{PRIM}", json={'connection': {'host': '192.0.2.9'}}) if method == 'put'
         else _patch(client, {'connection': {'host': '192.0.2.9'}}, tag=tag))
    assert r.status_code == 503, r.text
    d = r.json()['detail']
    assert 'saved' in d['errors'][0] and 'restart' in d['errors'][0]
    assert d['partial'] == {'saved': ['entry'], 'failed': ['apply']}
    assert 'host: 192.0.2.9' in _file(cfg).read_text()


# ── Settings → Modbus: the same validator, the same write (review 7) ────────

@needs_tc
def test_settings_modbus_is_judged_and_written_as_the_primary(tmp_path):
    cfg, client = make_app(tmp_path)
    before, tag = _file(cfg).read_bytes(), _tag(client)
    bad = client.post("/api/config/modbus", json={'host': '192.0.2.9', 'retry_attempts': 99})
    assert bad.status_code == 422 and 'connection.retry_attempts' in bad.text, bad.text
    assert _file(cfg).read_bytes() == before and cfg.modbus.host == '192.168.1.207'
    ok = client.post("/api/config/modbus", json={'host': '192.0.2.9', 'port': 502, 'unit_id': 1, 'timeout': 3,
                                                 'retry_attempts': 2, 'retry_delay': 1.0})
    assert ok.status_code == 200 and ok.json()['status'] == 'ok', ok.text
    assert cfg.modbus.host == '192.0.2.9' and cfg.modbus.retry_attempts == 2
    assert _tag(client) != tag
    # the devices were rebuilt and the list re-synced: the device says it
    assert client.get(f"/api/devices/{PRIM}").json()['device']['connection']['host'] == '192.0.2.9'
