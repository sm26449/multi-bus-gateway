# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""`PATCH /api/devices/{id}` (ADR-0031 §4–§5): a device edited in sections.

If-Match is required (428 without it, 412 when stale); every section is judged
by the validator its own route uses, so PATCH and PUT / the sub-routes store
the same thing for the same request; one config write, one audit record, one
restart decision — outputs and the name apply live. A dry run answers the
diff, whether acquisition restarts, the effects and the locks, and stores
nothing. `?intent=replace` writes an energy-counter reset marker (L3-F17)."""
import json
import pathlib

import pytest

from tests.test_devices_api import make_app, needs_tc

SETUP = """
devices:
  - id: inv1
    name: Inverter
    template: eastron_sdm630
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.80, unit_id: 1}
  - id: em2
    name: Meter
    template: eastron_sdm120
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.81, unit_id: 2}
endpoints:
  - id: plant
    name: Plant
    template: eastron_sdm120
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.20, port: 502}
    units: [1, 2]
"""


def _app(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml="primary_device: false\n" + SETUP)
    return cfg, client


def _tag(client, did):
    r = client.get(f"/api/devices/{did}")
    assert r.status_code == 200, r.text
    return r.headers['etag']


def _patch(client, did, sections, tag=None, query=''):
    headers = {'If-Match': tag} if tag else {}
    return client.patch(f"/api/devices/{did}{query}", json={'sections': sections}, headers=headers)


def _raw(cfg, did):
    return cfg.get_raw_device(did)


# ── If-Match ────────────────────────────────────────────────────────────────

@needs_tc
def test_a_patch_without_if_match_is_428_and_changes_nothing(tmp_path):
    cfg, client = _app(tmp_path)
    path = pathlib.Path(cfg.config_path)
    before = path.read_bytes()
    res = _patch(client, 'inv1', {'identity': {'name': 'Renamed'}})
    assert res.status_code == 428, res.text
    assert 'If-Match' in res.json()['detail']['errors'][0]
    assert path.read_bytes() == before


@needs_tc
def test_a_stale_if_match_is_412_with_the_current_entry(tmp_path):
    cfg, client = _app(tmp_path)
    tag = _tag(client, 'inv1')
    path = pathlib.Path(cfg.config_path)
    before = path.read_bytes()
    res = _patch(client, 'inv1', {'identity': {'name': 'Renamed'}}, tag='"stale"')
    assert res.status_code == 412, res.text
    assert res.json()['detail']['current']['id'] == 'inv1' and res.headers['etag'] == tag
    assert path.read_bytes() == before


@needs_tc
def test_a_current_if_match_goes_through_and_answers_the_new_tag(tmp_path):
    cfg, client = _app(tmp_path)
    tag = _tag(client, 'inv1')
    res = _patch(client, 'inv1', {'identity': {'name': 'Renamed'}}, tag=tag)
    assert res.status_code == 200, res.text
    d = res.json()
    assert d['status'] == 'updated' and d['restarted'] is False
    assert d['changes'] == [{'section': 'identity', 'field': 'name', 'from': 'Inverter', 'to': 'Renamed'}]
    assert d['device']['name'] == 'Renamed' and _raw(cfg, 'inv1')['name'] == 'Renamed'
    assert res.headers['etag'] == _tag(client, 'inv1') != tag
    assert res.headers['mbg-if-match'] == 'checked'


# ── each section ────────────────────────────────────────────────────────────

@needs_tc
def test_the_connection_section_merges_with_what_is_stored_and_restarts(tmp_path):
    cfg, client = _app(tmp_path)
    res = _patch(client, 'inv1', {'connection': {'host': '192.0.2.90'}}, tag=_tag(client, 'inv1'))
    assert res.status_code == 200, res.text
    d = res.json()
    assert d['restarted'] is True
    assert {'section': 'connection', 'field': 'host', 'from': '192.0.2.80', 'to': '192.0.2.90'} in d['changes']
    conn = _raw(cfg, 'inv1')['connection']
    assert conn['host'] == '192.0.2.90' and conn['unit_id'] == 1 and conn['protocol'] == 'tcp'


@needs_tc
def test_the_connection_is_judged_as_put_judges_it(tmp_path):
    cfg, client = _app(tmp_path)
    bad = {'protocol': 'tcp', 'host': '', 'unit_id': 1}
    put = client.put("/api/devices/inv1?dry_run=true", json={'id': 'inv1', 'connection': bad})
    res = _patch(client, 'inv1', {'connection': bad}, tag=_tag(client, 'inv1'))
    assert put.status_code == res.status_code == 422
    assert put.json()['detail']['errors'] == res.json()['detail']['errors']
    assert res.json()['detail']['fields']['connection.host']


@needs_tc
def test_the_outputs_section_applies_live_and_stores_what_the_sub_routes_store(tmp_path):
    cfg, client = _app(tmp_path)
    out = {'mqtt': False, 'ha_discovery': False,
           'influxdb': {'enabled': False, 'publish_mode': 'all', 'heartbeat_interval': 120},
           'http_output': True, 'opcua': {'enabled': True, 'fields': ['power_active_total']},
           'rest_push': {'enabled': True, 'url': 'http://192.0.2.5/in', 'interval_s': 30}}
    res = _patch(client, 'inv1', {'outputs': out}, tag=_tag(client, 'inv1'))
    assert res.status_code == 200, res.text
    assert res.json()['restarted'] is False
    patched = _raw(cfg, 'inv1')
    # the same through the sub-routes, on em2 — the stored blocks are the same
    for url, body in [('outputs', {'mqtt': False, 'ha_discovery': False, 'influxdb': False}),
                      ('publishing', {'influxdb': {'publish_mode': 'all', 'heartbeat_interval': 120}}),
                      ('http-output', {'enabled': True}),
                      ('opcua-output', {'enabled': True, 'fields': ['power_active_total']}),
                      ('rest-push', {'enabled': True, 'url': 'http://192.0.2.5/in', 'interval_s': 30})]:
        r = client.post(f"/api/devices/em2/{url}", json=body)
        assert r.status_code == 200, (url, r.text)
    routed = _raw(cfg, 'em2')
    for k in ('mqtt', 'influxdb', 'http_output', 'opcua', 'rest_push'):
        a, b = dict(patched.get(k) or {}), dict(routed.get(k) or {})
        for x in (a, b):
            x.pop('topic_prefix', None), x.pop('bucket', None), x.pop('device_tag', None), x.pop('ha_node', None)
        assert a == b, k


@needs_tc
def test_an_output_refused_names_its_field(tmp_path):
    cfg, client = _app(tmp_path)
    path = pathlib.Path(cfg.config_path)
    before = path.read_bytes()
    res = _patch(client, 'inv1', {'outputs': {'rest_push': {'enabled': True, 'url': ''},
                                              'influxdb': {'publish_mode': 'sometimes'}}},
                 tag=_tag(client, 'inv1'))
    assert res.status_code == 422, res.text
    errs = res.json()['detail']['errors']
    assert any(e.startswith('outputs.rest_push.url: required') for e in errs), errs
    assert any(e.startswith('outputs.influxdb.publish_mode') for e in errs), errs
    assert path.read_bytes() == before


@needs_tc
def test_the_polling_section_sets_intervals_live_and_enabled_restarts(tmp_path):
    cfg, client = _app(tmp_path)
    res = _patch(client, 'inv1', {'polling': {'poll_groups': {'normal': 7}}}, tag=_tag(client, 'inv1'))
    assert res.status_code == 200, res.text
    assert res.json()['restarted'] is False
    assert any(c['section'] == 'polling' and c['field'] == 'poll_groups.normal' and c['to'] == 7.0
               for c in res.json()['changes'])
    assert client.get("/api/devices/inv1/poll-groups").json()['poll_groups']['normal']['interval'] == 7.0
    dry = _patch(client, 'inv1', {'polling': {'enabled': True}}, query='?dry_run=true')
    assert dry.status_code == 200 and dry.json()['restart_required'] is True
    bad = _patch(client, 'inv1', {'polling': {'poll_groups': {'normal': 0}}}, tag=_tag(client, 'inv1'))
    assert bad.status_code == 422 and 'polling.poll_groups' in bad.text


@needs_tc
def test_the_identity_type_follows_the_organise_rule(tmp_path):
    cfg, client = _app(tmp_path)
    res = _patch(client, 'inv1', {'identity': {'type': 'meter'}}, tag=_tag(client, 'inv1'))
    assert res.status_code == 200, res.text
    assert _raw(cfg, 'inv1')['kind'] == 'meter' and res.json()['restarted'] is False
    bad = _patch(client, 'inv1', {'identity': {'type': 'no such type'}}, tag=_tag(client, 'inv1'))
    org = client.patch("/api/devices/em2/organise", json={'type': 'no such type'})
    assert bad.status_code == org.status_code == 422
    assert bad.json()['detail']['errors'][0].split(': ', 1)[1] == org.json()['detail']['errors'][0].split(': ', 1)[1]


@needs_tc
def test_the_model_section_changes_the_template_and_restarts(tmp_path):
    cfg, client = _app(tmp_path)
    dry = _patch(client, 'inv1', {'model': {'template': 'eastron_sdm120'}}, query='?dry_run=true')
    assert dry.status_code == 200, dry.text
    d = dry.json()
    assert d['would'] == 'update_restart' and d['restart_required'] is True
    assert {'section': 'model', 'field': 'template', 'from': 'eastron_sdm630', 'to': 'eastron_sdm120'} in d['changes']
    nope = _patch(client, 'inv1', {'model': {'template': 'no_such'}}, tag=_tag(client, 'inv1'))
    assert nope.status_code == 422 and 'template' in nope.text


# ── the dry run ─────────────────────────────────────────────────────────────

@needs_tc
def test_a_dry_run_answers_the_diff_and_stores_nothing(tmp_path):
    cfg, client = _app(tmp_path)
    path = pathlib.Path(cfg.config_path)
    before = path.read_bytes()
    res = _patch(client, 'inv1', {'connection': {'unit_id': 4}, 'identity': {'name': 'Four'},
                                  'outputs': {'mqtt': False}}, query='?dry_run=true')
    assert res.status_code == 200, res.text
    d = res.json()
    assert d['status'] == 'valid' and d['would'] == 'update_restart' and d['restart_required'] is True
    got = {(c['section'], c['field']): (c['from'], c['to']) for c in d['changes']}
    assert got[('connection', 'unit_id')] == (1, 4)
    assert got[('identity', 'name')] == ('Inverter', 'Four')
    assert got[('outputs', 'mqtt.enabled')] == (None, False)
    assert any('restarts' in e for e in d['effects'])
    # the same locks GET /api/devices/{id} says (device_summary.locks)
    assert d['locks'] == client.get("/api/devices/inv1").json()['device']['locks']
    assert any(lk['field'] == 'connection.protocol' for lk in d['locks']), d['locks']
    assert path.read_bytes() == before


@needs_tc
def test_outputs_and_the_name_never_restart(tmp_path):
    _cfg, client = _app(tmp_path)
    d = _patch(client, 'inv1', {'identity': {'name': 'X'}, 'outputs': {'influxdb': False, 'http_output': True}},
               query='?dry_run=true').json()
    assert d['would'] == 'update' and d['restart_required'] is False
    assert any('live' in e for e in d['effects'])


@needs_tc
def test_a_secret_changed_is_said_but_never_shown(tmp_path):
    _cfg, client = _app(tmp_path)
    d = _patch(client, 'inv1', {'outputs': {'rest_push': {'enabled': True, 'url': 'http://192.0.2.5/in',
                                                          'headers': {'Authorization': 'Bearer s3cret'}}}},
               query='?dry_run=true').json()
    assert 's3cret' not in json.dumps(d)
    assert any(c['field'] == 'rest_push.headers.Authorization' and c['to'] == '******' for c in d['changes'])


@needs_tc
def test_a_renamed_header_with_its_masked_value_is_refused_never_stored_as_the_mask(tmp_path):
    """A header's value comes back masked; renaming the header in the form
    sent the mask under a name the gateway does not know — it stored
    the mask as the secret. Refused at that header, nothing stored."""
    cfg, client = _app(tmp_path)
    rp = {'enabled': True, 'url': 'http://192.0.2.5/in', 'headers': {'Authorization': 'Bearer s3cret'}}
    assert _patch(client, 'inv1', {'outputs': {'rest_push': rp}}, tag=_tag(client, 'inv1')).status_code == 200
    mask = client.get('/api/devices/inv1').json()['device']['rest_push']['headers']['Authorization']   # as the page holds it
    renamed = dict(rp, headers={'X-Auth': mask})
    res = _patch(client, 'inv1', {'outputs': {'rest_push': renamed}}, tag=_tag(client, 'inv1'))
    assert res.status_code == 422, res.text
    assert 'outputs.rest_push.headers.X-Auth' in res.json()['detail'].get('fields', {}), res.text
    assert _raw(cfg, 'inv1')['rest_push']['headers'] == {'Authorization': 'Bearer s3cret'}
    # the old route judges it the same way
    res = client.post('/api/devices/inv1/rest-push', json=renamed)
    assert res.status_code in (400, 422), res.text
    assert _raw(cfg, 'inv1')['rest_push']['headers'] == {'Authorization': 'Bearer s3cret'}
    # the same name with its mask keeps the stored value
    assert _patch(client, 'inv1', {'outputs': {'rest_push': dict(rp, headers={'Authorization': mask})}},
                  tag=_tag(client, 'inv1')).status_code == 200
    assert _raw(cfg, 'inv1')['rest_push']['headers'] == {'Authorization': 'Bearer s3cret'}


@needs_tc
def test_an_unchanged_patch_writes_nothing(tmp_path):
    cfg, client = _app(tmp_path)
    path = pathlib.Path(cfg.config_path)
    before = path.read_bytes()
    res = _patch(client, 'inv1', {'identity': {'name': 'Inverter'}, 'connection': {'host': '192.0.2.80'}},
                 tag=_tag(client, 'inv1'))
    assert res.status_code == 200 and res.json()['status'] == 'unchanged', res.text
    assert path.read_bytes() == before


# ── what PATCH refuses ──────────────────────────────────────────────────────

@needs_tc
@pytest.mark.parametrize("body,needle", [
    ({'sections': {'write_locked': {'locked': False}}}, 'write-lock'),
    ({'sections': {'connection': {'write_locked': False}}}, 'write-lock'),
    ({'sections': {'polling': {'write_locked': False}}}, 'write-lock'),
    ({'sections': {'colour': {}}}, 'not a section'),
    ({'sections': {}}, 'at least one'),
    ({'name': 'x'}, 'sections'),
    ({'sections': {'identity': {'id': 'other'}}}, 'fixed after creation'),
])
def test_what_is_not_a_section_is_refused(tmp_path, body, needle):
    cfg, client = _app(tmp_path)
    path = pathlib.Path(cfg.config_path)
    before = path.read_bytes()
    res = client.patch("/api/devices/inv1", json=body, headers={'If-Match': _tag(client, 'inv1')})
    assert res.status_code == 422, res.text
    assert needle in res.text
    assert path.read_bytes() == before


@needs_tc
def test_a_unit_of_an_installation_is_edited_on_its_installation(tmp_path):
    _cfg, client = _app(tmp_path)
    res = _patch(client, 'plant-u1', {'identity': {'name': 'x'}}, tag=_tag(client, 'plant-u1'))
    assert res.status_code == 422 and 'edit the endpoint' in res.text


@needs_tc
def test_one_patch_is_one_audit_record(tmp_path):
    cfg, client = _app(tmp_path)
    audit = pathlib.Path(cfg.config_path).parent / "audit.jsonl"

    def n():
        return sum(1 for ln in (audit.read_text().splitlines() if audit.exists() else [])
                   if 'PATCH /api/devices/inv1' in ln)
    k = n()
    res = _patch(client, 'inv1', {'identity': {'name': 'A'}, 'outputs': {'mqtt': False},
                                  'polling': {'poll_groups': {'normal': 9}}}, tag=_tag(client, 'inv1'))
    assert res.status_code == 200, res.text
    assert n() == k + 1


# ── Replace hardware: the counter reset marker (L3-F17, groundwork) ─────────

@needs_tc
def test_intent_replace_writes_a_counter_reset_marker_that_put_keeps(tmp_path):
    cfg, client = _app(tmp_path)
    dry = _patch(client, 'inv1', {'connection': {'unit_id': 9}}, query='?dry_run=true&intent=replace')
    assert any('reset marker' in e for e in dry.json()['effects'])
    assert 'energy_counter_resets' not in _raw(cfg, 'inv1')
    res = _patch(client, 'inv1', {'connection': {'unit_id': 9}}, tag=_tag(client, 'inv1'), query='?intent=replace')
    assert res.status_code == 200, res.text
    marks = _raw(cfg, 'inv1')['energy_counter_resets']
    assert len(marks) == 1 and marks[0]['reason'] == 'replace' and marks[0]['at'] and marks[0]['ts'] > 0
    assert res.json()['device']['energy_counter_resets'] == marks
    assert client.get("/api/devices/inv1").json()['device']['energy_counter_resets'] == marks
    # a PUT of the connection keeps it (a device's own key)
    assert client.put("/api/devices/inv1", json={'id': 'inv1', 'name': 'Inverter 2'}).status_code == 200
    assert _raw(cfg, 'inv1')['energy_counter_resets'] == marks
    bad = _patch(client, 'inv1', {'identity': {'name': 'x'}}, tag=_tag(client, 'inv1'), query='?intent=swap')
    assert bad.status_code == 422 and 'intent' in bad.text
