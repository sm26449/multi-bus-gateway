# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""What a formula reads, judged against what the gateway has (input_check):
said in the rules list, the preview and an import; refused only for a rule
that drives its target — never on the way to shadow or disabled — and never
changing how a rule evaluates (UI audit 2026-10-21, R-M2 and the hyphen trap)."""
import pytest

from multibus import expressions, input_check
from tests.test_commands import _Inverter
from tests.test_endpoints import needs_tc
from tests.test_rules import OV
from tests.test_rules_api import _feed, _rule_app

VOLTS = {'voltage_l1_n': 230, 'voltage_l2_n': 231, 'voltage_l3_n': 232, 'power_limit_pct': 100}


@pytest.fixture
def ids():
    """The gateway's ids, as the API tells the expression engine."""
    known = set()
    expressions.set_device_ids_provider(lambda: known)
    yield known
    expressions.set_device_ids_provider(None)


def test_dashed_unknown_names_only_what_is_neither_a_device_nor_a_subtraction(ids):
    assert expressions.dashed_unknown('battery-bank.soc') == []          # ids unknown: say nothing
    ids |= {'pv-u1', 'meter2'}
    assert expressions.dashed_unknown('battery-bank.soc + 1') == ['battery-bank']
    assert expressions.dashed_unknown('pv-u1.p') == []                   # a real id
    assert expressions.dashed_unknown('grid_power-meter2.p') == []       # a subtraction that resolves
    ids.add('battery-bank')
    assert expressions.dashed_unknown('battery-bank.soc') == []


def test_problems_say_no_device_no_field_with_a_hint_and_nothing_read():
    names = {'pv-u1': {'voltage_l1_n', 'cells'}, 'empty': set(), 'bank': None}

    def fields_of(dev):
        return names.get(dev, False)
    got = input_check.problems(['nosuch.x', 'pv-u1.voltage_l1', 'pv-u1.cells[3]', 'pv-u1.voltage_l1_n',
                                'empty.p', 'bank.soc'], fields_of)
    assert got == [{'ref': 'nosuch.x', 'problem': 'no_device'},
                   {'ref': 'pv-u1.voltage_l1', 'problem': 'no_field', 'hint': 'voltage_l1_n'},
                   {'ref': 'empty.p', 'problem': 'nothing_read'}]
    assert "did you mean 'voltage_l1_n'" in input_check.text(got[1])


@needs_tc
def test_a_rule_says_what_it_reads_that_is_not_here_and_only_an_armed_one_is_refused(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    _feed(app, clock, 'pv-u1', VOLTS)
    rt._fields_of.cache_clear()                # the names are cached a moment
    # a typo: saved, said with the near name
    typo = dict(OV, id='typo', signal='pv-u1.voltage_l1')
    r = client.post("/api/rules", json=typo)
    assert r.status_code == 200, r.text
    assert r.json()['rule']['inputs'] == [{'in': 'signal', 'ref': 'pv-u1.voltage_l1',
                                           'problem': 'no_field', 'hint': 'voltage_l1_n'}]
    # a device that is not here: a shadow rule is saved and says so …
    gone = dict(OV, id='gone', signal='nosuch.voltage')
    r = client.post("/api/rules", json=gone)
    assert r.status_code == 200, r.text
    listed = {x['id']: x for x in client.get("/api/rules").json()['rules']}
    assert listed['gone']['inputs'][0]['problem'] == 'no_device'
    assert listed['ov-u1']['inputs'] == []
    # … but it cannot be armed: it could only ever be stale
    r = client.post("/api/rules/gone/mode", json={'mode': 'armed'})
    assert r.status_code == 422 and "no device or installation 'nosuch'" in r.text and 'shadow' in r.text
    # a hyphenated id that is not here names itself, not "must name its device"
    r = client.post("/api/rules", json=dict(OV, id='dash', signal='nosuch-dev.x'))
    assert r.status_code == 422 and "'nosuch-dev' is not a device or installation" in r.text
    # the preview and an import say it too
    p = client.post("/api/rules/validate", json=gone).json()
    assert p['inputs'][0]['ref'] == 'nosuch.voltage'
    imp = client.post("/api/rules/import", json={'yaml': "rules:\n  - id: imp\n    kind: steps\n"
                                                          "    target: {device: pv-u1, command: power_limit}\n"
                                                          "    signal: other.v\n    steps: [{at: 250, value: 80}]\n"
                                                          "    release_below: 248\n    normal: {value: 100}\n"})
    row = imp.json()['rules'][0]
    assert row['status'] == 'new' and row['inputs'][0]['problem'] == 'no_device'


@needs_tc
def test_an_armed_rule_whose_device_went_away_still_runs_and_can_always_be_disarmed(tmp_path):
    """The safety side: nothing about the inputs changes what an armed rule
    does — it evaluates stale and follows on_stale; going to shadow (alone,
    or in one PUT with an edit) and disabling are never refused."""
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    r = client.post("/api/rules", json=dict(OV, id='m', mode='armed', signal='umg512.v', on_stale='safe'))
    assert r.status_code == 200, r.text
    live_keys = set(client.get("/api/rules/m").json()['live'])
    # umg512 "goes away": the gateway no longer has it
    rt._fields_of = lambda dev: False if dev == 'umg512' else None
    d = client.get("/api/rules/m").json()
    assert d['inputs'][0]['problem'] == 'no_device' and set(d['live']) == live_keys
    clock.t += 5
    rt.tick(clock())
    assert rt.get('m')['live']['state'] != '' and 'm' not in rt.errors      # still evaluated
    # one PUT that disarms AND edits: never refused, and it releases what the
    # rule had sent (as if it had held pv-u1 at 60 %)
    rt.states[('m', 'pv-u1')].commanded = {'value': 60, 'revert_s': 0}
    r = client.put("/api/rules/m", json=dict(OV, id='m', mode='shadow', signal='umg512.v',
                                             on_stale='safe', label='edited', stale_after_s=61))
    assert r.status_code == 200, r.text
    assert rt.rules['m'].mode == 'shadow' and r.json()['releasing'] == ['pv-u1']
    assert rt.wait_released(10)
    # arming it back is refused, in words
    r = client.post("/api/rules/m/mode", json={'mode': 'armed'})
    assert r.status_code == 422 and 'umg512' in r.text
    r = client.post("/api/rules/m/enable", json={'enabled': False})
    assert r.status_code == 200, r.text


@needs_tc
def test_the_names_are_never_looked_up_under_the_rules_lock(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    held = []

    def fields_of(dev):
        held.append(rt._lock._is_owned())
        return None
    rt._fields_of = fields_of
    client.get("/api/rules")
    client.put("/api/rules/ov-u1", json=dict(OV, label='x', stale_after_s=61))
    client.post("/api/rules/validate", json=OV)
    assert held and not any(held)


@needs_tc
def test_a_template_formula_names_a_hyphenated_device_that_is_not_here(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    tpl = {'registers': [{'name': 'p'}]}
    r = client.post("/api/device-templates/check-expression",
                    json={'template': tpl, 'expr': 'battery-bank.pack_average_soc * 2', 'name': 'x'}).json()
    assert r['ok'] is True and 'battery' not in r.get('error', '')
    assert "no 'battery-bank' on this gateway" in r['warning'] and r['refs'] == ['battery-bank.pack_average_soc']
    # a real hyphenated id is a device reference as before
    r = client.post("/api/device-templates/check-expression",
                    json={'template': tpl, 'expr': 'pv-u1.p', 'name': 'x'}).json()
    assert r['ok'] is True and 'no ' not in r['warning'] and r['refs'] == ['pv-u1.p']
    # a device's calculated list: said, never refused — and a subtraction that
    # resolves is left alone
    r = client.post("/api/devices/umg512/calculated", json={'calculated': [
        {'name': 'a', 'expr': 'nosuch-dev.x + 1'}, {'name': 'b', 'expr': 'p-umg512.p'}]})
    assert r.status_code == 200, r.text
    w = r.json()['warnings']
    assert len(w) == 1 and "'nosuch-dev' is not a device" in w[0]


@needs_tc
def test_a_hyphenated_device_that_went_away_reads_as_before_with_the_reason_named(tmp_path):
    """B4, unchanged on purpose: once `pv-u2` is gone, `pv-u2.v` parses as a
    subtraction and the reload puts the rule in error, as it always did (the
    user decides separately whether rules should read it as a device). Only
    the message changes: it names `pv-u2`, not "must name its device"."""
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    r = client.post("/api/rules", json=dict(OV, id='h', signal='pv-u2.voltage_l1_n'))
    assert r.status_code == 200, r.text
    before = expressions._device_ids
    expressions.set_device_ids_provider(lambda: {'umg512', 'pv-u1', 'pv'})
    try:
        rt.load()
        assert "'pv-u2' is not a device or installation" in rt.errors['h']
        assert 'ov-u1' not in rt.errors
    finally:
        expressions.set_device_ids_provider(before)
