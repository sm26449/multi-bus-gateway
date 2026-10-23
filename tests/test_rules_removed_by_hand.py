# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""RL-LOAD-R4..R7: a reload never lifts an OV limit. An armed rule gone from
rules.yaml as read (a hand edit, a file half-written or moved away) holds its
target and alerts; only a delete from the API releases. Nothing is written
over a file that cannot be read, a disarm still works (in memory), and what
was saved for a rule waits for it to be repaired."""
import json

import yaml

from tests.test_commands import _Inverter
from tests.test_devices_api import needs_tc
from tests.test_rules_api import _feed, _rule_app

HIGH = {'voltage_l1_n': 251.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231, 'power_limit_pct': 100}
SAFE = [10000, 0, 0, 10, 0]


def _curtailed(app, clock, rt, inv):
    for _ in range(3):
        _feed(app, clock, 'pv-u1', HIGH)
        rt.tick(clock())
        clock.t += 2
    rt._writer('pv-u1').submit(lambda: None).result(10)
    assert inv.writes[-1][1][0] == 7000


def _alerts(rt):
    fired = []
    rt.alert_mgr = type('A', (), {'fire': lambda self, sev, key, src, msg: fired.append(key)})()
    return fired


def _rewrite(tmp_path, fn):
    path = tmp_path / 'rules.yaml'
    doc = yaml.safe_load(path.read_text())
    doc['rules'] = fn(doc['rules'])
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


@needs_tc
def test_an_armed_rule_removed_by_hand_holds_its_target_and_alerts(tmp_path):
    # a read can be wrong (a file half-written, replaced while read): lifting
    # an OV limit on it is the dangerous direction — hold, and say so
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    _curtailed(app, clock, rt, inv)
    fired = _alerts(rt)
    n = len(inv.writes)
    _rewrite(tmp_path, lambda rules: [r for r in rules if r['id'] != 'ov-u1'])
    rt.load()
    assert rt.wait_released(10)
    assert len(inv.writes) == n and 'rules:vanished' in fired


@needs_tc
def test_an_unreadable_file_releases_nothing(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    _curtailed(app, clock, rt, inv)
    n = len(inv.writes)
    (tmp_path / 'rules.yaml').write_text('rules: [ {id: broken')
    rt.load()
    assert rt.wait_released(10)
    assert len(inv.writes) == n                     # the limit holds


@needs_tc
def test_a_rule_broken_by_hand_holds_its_limit(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    _curtailed(app, clock, rt, inv)
    n = len(inv.writes)
    _rewrite(tmp_path, lambda rules: [dict(r, target=True) if r['id'] == 'ov-u1' else r for r in rules])
    rt.load()
    assert 'ov-u1' in rt.errors and rt.wait_released(10)
    assert len(inv.writes) == n


@needs_tc
def test_a_delete_from_the_api_releases_once_and_raises_no_removed_alert(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    _curtailed(app, clock, rt, inv)
    fired = _alerts(rt)
    n = len(inv.writes)
    assert client.delete('/api/rules/ov-u1').status_code == 200
    assert rt.wait_released(10)
    assert [w[1] for w in inv.writes[n:]] == [SAFE]
    assert 'rules:vanished' not in fired


@needs_tc
def test_a_rule_in_error_keeps_its_saved_clamp_until_repaired(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    assert rt.set_clamp('ov-u1', 60, None, who='op') == []
    good = yaml.safe_load((tmp_path / 'rules.yaml').read_text())['rules']
    _rewrite(tmp_path, lambda rules: [dict(r, target=True) if r['id'] == 'ov-u1' else r for r in rules])
    rt.load()
    rt._save_state()                                # any later save (another rule, a tick)
    saved = json.loads((tmp_path / 'rules_state.json').read_text())
    assert saved['ov-u1:pv-u1']['clamp']['max'] == 60
    _rewrite(tmp_path, lambda rules: good)
    rt.load()
    assert rt.states[('ov-u1', 'pv-u1')].clamp['max'] == 60


BROKEN = 'rules: [ {id: broken'


@needs_tc
def test_a_ui_save_over_an_unreadable_file_writes_nothing_and_releases_nothing(tmp_path):
    # review R4-1: the save rebuilt the file from an empty read, the reload
    # then found every armed rule gone and lifted every limit
    from tests.test_rules import OV
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    _curtailed(app, clock, rt, inv)
    n = len(inv.writes)
    (tmp_path / 'rules.yaml').write_text(BROKEN)
    r = client.put('/api/rules/ov-u1', json=dict(OV, mode='armed', label='renamed'))
    assert r.status_code == 422 and 'cannot be read' in r.text
    assert client.delete('/api/rules/ov-u1').status_code == 409
    assert (tmp_path / 'rules.yaml').read_text() == BROKEN
    assert rt.wait_released(10) and len(inv.writes) == n
    assert rt.rules['ov-u1'].mode == 'armed'


@needs_tc
def test_disarming_over_an_unreadable_file_works_in_memory(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    _curtailed(app, clock, rt, inv)
    fired = _alerts(rt)
    (tmp_path / 'rules.yaml').write_text(BROKEN)
    assert client.post('/api/rules/ov-u1/mode', json={'mode': 'shadow'}).status_code == 200
    assert rt.rules['ov-u1'].mode == 'shadow' and 'rules:store' in fired
    assert rt.wait_released(10) and inv.writes[-1][1] == SAFE      # leaving releases, as always
    assert (tmp_path / 'rules.yaml').read_text() == BROKEN
    # and it stays left: the voltage is still high, the rule writes nothing more
    n = len(inv.writes)
    for _ in range(80):                                 # past debounce, min interval and reassert
        _feed(app, clock, 'pv-u1', HIGH)
        rt.tick(clock())
        clock.t += 2
    rt._writer('pv-u1').submit(lambda: None).result(10)
    assert len(inv.writes) == n


@needs_tc
def test_an_empty_or_missing_file_releases_nothing(tmp_path):
    # review R4-2: an editor that truncates first, a file moved away
    for how in ('empty', 'missing'):
        inv = _Inverter(sf=-2)
        d = tmp_path / how
        d.mkdir()
        cfg, app, client, clock, rt = _rule_app(d, {'pv-u1': inv}, mode='armed')
        _curtailed(app, clock, rt, inv)
        fired = _alerts(rt)
        n = len(inv.writes)
        if how == 'empty':
            (d / 'rules.yaml').write_text('\n')
        else:
            (d / 'rules.yaml').unlink()
        rt.load()
        assert rt.wait_released(10) and len(inv.writes) == n, how
        assert 'rules:vanished' in fired, how


@needs_tc
def test_a_mis_indented_rule_is_not_taken_for_a_removed_one(tmp_path):
    # review R4-3: ov-u1 written as a list (or with its id lost) is not gone
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    _curtailed(app, clock, rt, inv)
    fired = _alerts(rt)
    n = len(inv.writes)
    _rewrite(tmp_path, lambda rules: [[r] if r['id'] == 'ov-u1' else r for r in rules])
    rt.load()
    assert rt.wait_released(10) and len(inv.writes) == n and 'rules:vanished' in fired
    # back as it was, then its id lost: still not a removal
    _rewrite(tmp_path, lambda rules: [r[0] if isinstance(r, list) else r for r in rules])
    rt.load()
    assert rt.rules['ov-u1'].mode == 'armed'
    _rewrite(tmp_path, lambda rules: [{k: v for k, v in r.items() if k != 'id'} if r['id'] == 'ov-u1' else r
                                      for r in rules])
    rt.load()
    assert rt.wait_released(10) and len(inv.writes) == n



@needs_tc
def test_a_missing_file_is_not_rewritten_with_one_rule(tmp_path):
    # second review H2: rules.yaml moved away, a delete wrote `rules: []` and
    # every other rule went with it
    from tests.test_rules import OV
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    (tmp_path / 'rules.yaml').unlink()
    assert client.delete('/api/rules/ov-u1').status_code == 409
    r = client.put('/api/rules/ov-u1', json=dict(OV, mode='armed', label='renamed'))
    assert r.status_code == 422 and 'missing' in r.text
    assert not (tmp_path / 'rules.yaml').exists()


@needs_tc
def test_saved_clamps_survive_while_the_whole_file_cannot_be_read(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    assert rt.set_clamp('ov-u1', 60, None, who='op') == []
    good = (tmp_path / 'rules.yaml').read_text()
    (tmp_path / 'rules.yaml').write_text(BROKEN)
    rt.load()
    rt._save_state()
    assert json.loads((tmp_path / 'rules_state.json').read_text())['ov-u1:pv-u1']['clamp']['max'] == 60
    (tmp_path / 'rules.yaml').write_text(good)
    rt.load()
    assert rt.states[('ov-u1', 'pv-u1')].clamp['max'] == 60



@needs_tc
def test_a_clamp_cleared_while_the_file_is_unreadable_does_not_come_back(tmp_path):
    # third review: the carry-over copied back what the operator had ended
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    assert rt.set_clamp('ov-u1', 60, None, who='op') == []
    rt._save_state()
    good = (tmp_path / 'rules.yaml').read_text()
    (tmp_path / 'rules.yaml').unlink()                  # gone between reloads: the states live on
    assert client.delete('/api/rules/ov-u1').status_code == 409   # a refused save has read it missing
    assert rt.clear_clamp('ov-u1', who='op') == []
    rt._save_state()
    saved = json.loads((tmp_path / 'rules_state.json').read_text())
    assert not (saved.get('ov-u1:pv-u1') or {}).get('clamp')
    (tmp_path / 'rules.yaml').write_text(good)
    rt.load()
    assert rt.states[('ov-u1', 'pv-u1')].clamp is None
