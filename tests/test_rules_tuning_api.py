# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The five opt-in tuning keys (ADR-0012) through the rules API, as the rule
editor uses it: what GET hands the editor, a PUT of it back keeps exactly
(the production ov-u1..u4 shape), an out-of-range value is refused with the
key named in ``fields`` so the editor can say it next to its box, and a key
absent stays absent (never written as 0)."""
import json

import pytest
import yaml

from tests.test_commands import _Inverter
from tests.test_endpoints import needs_tc
from tests.test_rules import OV
from tests.test_rules_api import _rule_app

# the keys as production rules.yaml holds them on ov-u1..u4
PROD_TUNING = {'resume_on_boot': True, 'step_down_margin': 0.5}
PROD_TIMING = {'boot_wait_s': 60, 'step_down_hold_s': 90, 'urgent_retries': 3}


def _tuned():
    return dict(OV, **PROD_TUNING, timing={**OV['timing'], **PROD_TIMING})


def _raw(client, rid='ov-u1'):
    return next(r for r in client.get('/api/rules').json()['rules'] if r['id'] == rid)['raw']


def _stored(tmp_path, rid='ov-u1'):
    return next(r for r in yaml.safe_load((tmp_path / 'rules.yaml').read_text())['rules'] if r['id'] == rid)


@needs_tc
def test_get_then_put_keeps_all_five_keys_exactly(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    r = client.put('/api/rules/ov-u1', json=_tuned())
    assert r.status_code == 200, r.text
    raw = _raw(client)
    assert {k: raw.get(k) for k in PROD_TUNING} == PROD_TUNING
    assert {k: raw['timing'].get(k) for k in PROD_TIMING} == PROD_TIMING
    # the editor sends back what it got: nothing changes, nothing is dropped
    back = json.loads(json.dumps(raw))
    r = client.put('/api/rules/ov-u1', json=back)
    assert r.status_code == 200, r.text
    after = _stored(tmp_path)
    assert {k: after.get(k) for k in PROD_TUNING} == PROD_TUNING
    assert {k: after['timing'].get(k) for k in PROD_TIMING} == PROD_TIMING
    assert rt.rules['ov-u1'].step_down_margin == 0.5 and rt.rules['ov-u1'].timing.step_down_hold_s == 90
    assert rt.rules['ov-u1'].resume_on_boot is True and rt.rules['ov-u1'].timing.urgent_retries == 3


@needs_tc
def test_a_put_that_changes_one_key_keeps_the_others(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    assert client.put('/api/rules/ov-u1', json=_tuned()).status_code == 200
    body = _raw(client)
    body['timing']['step_down_hold_s'] = 120
    assert client.put('/api/rules/ov-u1', json=body).status_code == 200, body
    after = _stored(tmp_path)
    assert after['timing']['step_down_hold_s'] == 120 and after['step_down_margin'] == 0.5
    assert after['timing']['boot_wait_s'] == 60 and after['resume_on_boot'] is True


@needs_tc
def test_an_absent_key_stays_absent_and_a_typed_zero_is_kept(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    raw = _raw(client)
    assert not any(k in raw for k in PROD_TUNING)
    assert not any(k in raw['timing'] for k in PROD_TIMING)
    # removing a key (the editor's empty box) removes it
    assert client.put('/api/rules/ov-u1', json=_tuned()).status_code == 200
    body = _raw(client)
    del body['step_down_margin']
    del body['timing']['urgent_retries']
    assert client.put('/api/rules/ov-u1', json=body).status_code == 200
    after = _stored(tmp_path)
    assert 'step_down_margin' not in after and 'urgent_retries' not in after['timing']
    # a typed 0 is a value: stored as 0 (and means off)
    body = _raw(client)
    body['timing']['step_down_hold_s'] = 0
    assert client.put('/api/rules/ov-u1', json=body).status_code == 200
    assert _stored(tmp_path)['timing']['step_down_hold_s'] == 0
    assert rt.rules['ov-u1'].timing.step_down_hold_s == 0


@needs_tc
@pytest.mark.parametrize('where,key,value,word', [
    ('timing', 'boot_wait_s', 601, 'timing.boot_wait_s'),
    ('timing', 'boot_wait_s', -1, 'timing.boot_wait_s'),
    ('timing', 'urgent_retries', 6, 'timing.urgent_retries'),
    ('timing', 'urgent_retries', 1.5, 'timing.urgent_retries'),
    ('timing', 'step_down_hold_s', 3601, 'timing.step_down_hold_s'),
    ('rule', 'step_down_margin', 5.1, 'step_down_margin'),      # 253 - 248 = 5
    ('rule', 'step_down_margin', -0.1, 'step_down_margin'),
    ('rule', 'resume_on_boot', 'yes', 'resume_on_boot'),
])
def test_out_of_range_is_refused_with_the_key_named(tmp_path, where, key, value, word):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    body = _raw(client)
    if where == 'timing':
        body['timing'][key] = value
    else:
        body[key] = value
    r = client.put('/api/rules/ov-u1', json=body)
    assert r.status_code == 422, r.text
    detail = r.json()['detail']
    assert any(e.startswith(word + ':') for e in detail['errors']), detail
    assert word in detail['fields'], detail
    # nothing stored
    assert key not in (_stored(tmp_path).get('timing', {}) if where == 'timing' else _stored(tmp_path))


@needs_tc
def test_the_margin_bound_follows_the_steps_and_release(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    body = _raw(client)
    body['step_down_margin'] = 5.0
    assert client.put('/api/rules/ov-u1', json=body).status_code == 200
    r = client.put('/api/rules/ov-u1', json=dict(body, step_down_margin=5.5))
    assert r.status_code == 422 and 'from 0 to 5' in r.json()['detail']['fields']['step_down_margin']


@needs_tc
def test_a_non_steps_rule_is_refused_the_keys(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cond = {'id': 'cond-1', 'kind': 'condition', 'mode': 'shadow',
            'target': {'device': 'pv-u1', 'command': 'power_limit'},
            'when': 'pv-u1.voltage_l1_n > 250', 'then': {'params': {'value': 80}},
            'step_down_margin': 0.5, 'timing': {'step_down_hold_s': 30}}
    r = client.post('/api/rules', json=cond)
    assert r.status_code == 422
    f = r.json()['detail']['fields']
    assert 'only a steps rule' in f['step_down_margin'] and 'only a steps rule' in f['timing.step_down_hold_s']
