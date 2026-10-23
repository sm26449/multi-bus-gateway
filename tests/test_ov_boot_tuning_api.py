# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The opt-in OV tuning keys through the runtime and the API (ADR-0012):
judged on a save (never at load, never on the way out of armed), tried with
?dry_run, kept by an unchanged save, and what they do on the wire — the
2026-10-20 13:04 restart traced from the read-back to the rule's state."""
import json

import yaml

from tests.test_commands import _Inverter
from tests.test_commands_api import _Mqtt
from tests.test_devices_api import needs_tc
from tests.test_rules import OV
from tests.test_rules_api import _feed, _rule_app

TUNED = {**OV['timing'], 'boot_wait_s': 60, 'urgent_retries': 3}
LOW = {'voltage_l1_n': 240.0, 'voltage_l2_n': 230, 'voltage_l3_n': 231}


def _ticks(app, clock, rt, n, values=None, dt=2.0):
    for _ in range(n):
        if values is not None:
            _feed(app, clock, 'pv-u1', values)
        rt.tick(clock())
        clock.t += dt


@needs_tc
def test_a_bad_key_is_refused_by_field_and_a_dry_run_says_what_a_save_would_do(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    bad = dict(OV, id='ov-x', timing={**OV['timing'], 'boot_wait_s': 9999}, resume_on_boot='yes')
    r = client.post('/api/rules', json=bad)
    assert r.status_code == 422
    f = r.json()['detail']['fields']
    assert 'timing.boot_wait_s' in f and 'resume_on_boot' in f
    r = client.post('/api/rules?dry_run=true', json=dict(OV, id='ov-y', timing=TUNED))
    assert r.json() == {'status': 'valid', 'would': 'create'} and 'ov-y' not in rt.rules
    r = client.put('/api/rules/ov-u1?dry_run=true', json=dict(OV, timing=TUNED))
    assert r.json()['would'] == 'update' and 'boot_wait_s' not in rt.raw['ov-u1']['timing']
    r = client.put('/api/rules/ov-u1?dry_run=true', json=OV)
    assert r.json()['would'] == 'unchanged'


@needs_tc
def test_tuning_keys_are_saved_kept_unchanged_and_keep_the_live_step(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    _ticks(app, clock, rt, 3, {**LOW, 'voltage_l1_n': 251.6, 'power_limit_pct': 100})
    st = rt.states[('ov-u1', 'pv-u1')]
    assert st.state == 'Moderate'
    r = client.put('/api/rules/ov-u1', json=dict(OV, timing=TUNED, resume_on_boot=True))
    assert r.status_code == 200, r.text
    assert rt.states[('ov-u1', 'pv-u1')] is st and st.state == 'Moderate'   # the live step stays
    stored = yaml.safe_load((tmp_path / 'rules.yaml').read_text())
    rule = next(x for x in (stored['rules'] if isinstance(stored, dict) else stored) if x['id'] == 'ov-u1')
    assert rule['timing']['boot_wait_s'] == 60 and rule['resume_on_boot'] is True
    r = client.put('/api/rules/ov-u1?dry_run=true', json=dict(OV, timing=TUNED, resume_on_boot=True))
    assert r.json()['would'] == 'unchanged'
    # a key present but OFF still differs from absent: a save can remove it
    r = client.put('/api/rules/ov-u1?dry_run=true', json=dict(OV, timing={**TUNED, 'boot_wait_s': 0}, resume_on_boot=True))
    assert r.json()['would'] == 'update'


@needs_tc
def test_leaving_armed_is_never_refused_for_a_bad_key(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)}, mode='armed')
    raw = dict(OV, mode='shadow', timing={**OV['timing'], 'boot_wait_s': 'soon'})
    r = client.put('/api/rules/ov-u1', json=raw)
    assert r.status_code == 200, r.text
    assert rt.rules['ov-u1'].mode == 'shadow'


@needs_tc
def test_a_hand_edited_rules_yaml_with_odd_keys_never_takes_the_armed_rules_down(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)}, mode='armed')
    good = dict(OV, mode='armed')
    odd = [dict(OV, id='odd-1', timing=['not', 'a', 'mapping']),
           dict(OV, id='odd-2', timing={**OV['timing'], 'boot_wait_s': {'x': 1}, 'urgent_retries': 'many'},
                resume_on_boot=[1]),
           dict(OV, id='odd-3', timing='soon')]
    (tmp_path / 'rules.yaml').write_text(yaml.safe_dump({'rules': [good] + odd}))
    rt.load()
    assert 'ov-u1' in rt.rules and 'ov-u1' not in rt.errors
    assert rt.rules['odd-2'].timing.boot_wait_s == 0 and rt.rules['odd-2'].resume_on_boot is False
    assert 'odd-1' in rt.errors and 'timing' in rt.errors['odd-1']


@needs_tc
def test_the_1304_restart_traced_from_the_read_back_to_the_state(tmp_path):
    """2026-10-20 13:04: a restart under release while the read-back was not
    in yet sent 100 % blind. With boot_wait_s the rule waits for the
    read-back, sees the inverter at 100 and writes nothing — and says so
    the same way on the API, the decision log and the retained state."""
    inv = _Inverter(sf=-2)                               # holds 100 %
    mq = _Mqtt()
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mqtt=mq, mode='armed')
    assert client.put('/api/rules/ov-u1', json=dict(OV, mode='armed', timing=TUNED)).status_code == 200
    rt.states.clear()                                   # the restart: every state starts fresh
    _ticks(app, clock, rt, 5, LOW)                      # 10 s, no power_limit read-back yet
    assert inv.writes == []
    live = client.get('/api/rules/ov-u1').json()['live']
    reason = live['units']['pv-u1'].get('last_reason') or live['last']['reason']
    assert 'waiting up to 60 s for the read-back' in reason
    st = json.loads([x for x in mq.sent if x[0] == 'mbg/rules/ov-u1/state'][-1][1])
    assert st['decision'] == 'sweep' and 'waiting up to 60 s for the read-back' in st['reason']
    _ticks(app, clock, rt, 3, {**LOW, 'power_limit_pct': 100})   # the read-back lands: 100
    assert inv.writes == []
    st = rt.states[('ov-u1', 'pv-u1')]
    assert st.commanded['value'] == 100 and st.confirmed
    assert any('already matches' in d['reason'] for d in rt.decisions['ov-u1'])


@needs_tc
def test_without_boot_wait_the_same_restart_writes_100_blind(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    rt.states.clear()
    _ticks(app, clock, rt, 5, LOW)
    assert inv.writes and inv.writes[-1][1][0] == 10000           # 100 % went out


@needs_tc
def test_a_failed_emergency_write_is_sent_again_in_seconds_not_at_the_rate_limit(tmp_path):
    def run(timing):
        inv = _Inverter(sf=-2)
        d = tmp_path / str(len(timing))
        d.mkdir()
        cfg, app, client, clock, rt = _rule_app(d, {'pv-u1': inv}, mode='armed')
        assert client.put('/api/rules/ov-u1', json=dict(OV, mode='armed', timing=timing)).status_code == 200
        real = inv.write
        state = {'fail': True}

        def write(address, register_type='holding', values=None, **kw):
            if state['fail']:
                inv.writes.append((address, list(values)))
                state['fail'] = False                    # the first one fails, the next go through
                return False, 'timeout'
            return real(address, register_type, values, **kw)
        inv.write = write
        t0 = clock.t
        sent = []
        for _ in range(25):
            _feed(app, clock, 'pv-u1', {**LOW, 'voltage_l1_n': 253.4, 'power_limit_pct': 100})
            n = len(inv.writes)
            rt.tick(clock())
            rt.wait_released(5)
            for w_ in list(rt.__dict__.get('_writers', {}).values()):
                w_.submit(lambda: None).result()
            if len(inv.writes) > n:
                sent.append(clock.t - t0)
            clock.t += 1.0
        return sent
    plain = run(dict(OV['timing']))
    tuned = run({**OV['timing'], 'urgent_retries': 3})
    assert len(plain) >= 1 and len(tuned) >= 2
    assert tuned[1] - tuned[0] <= 8 and (len(plain) < 2 or plain[1] - plain[0] >= 29)


@needs_tc
def test_a_dry_run_without_mode_judges_the_save_the_api_would_make(tmp_path):
    # review lot C F1: a real PUT without mode/enabled keeps the stored ones;
    # the dry run judged the bare payload (shadow) and answered for another save
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)}, mode='armed')
    body = {k: v for k, v in OV.items() if k not in ('mode', 'enabled')}
    r = client.put('/api/rules/ov-u1?dry_run=true', json=body)
    assert r.status_code == 200 and r.json()['would'] == 'unchanged', r.text
    assert client.put('/api/rules/ov-u1', json=body).json()['status'] == 'updated'
    assert rt.rules['ov-u1'].mode == 'armed'
