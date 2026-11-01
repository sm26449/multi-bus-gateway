# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""A rule's reassert meets the channel's pace (ADR-0022, RC-1): the drift asks the referee's hold, an emergency
want never waits, the head asks the yield of an urgent want too, the pending
path keeps the emergency exemption, the undo puts the drift back, starvation
is said, and a fast step on a paced channel is refused at the API."""
import pytest

from multibus import rules as _rules
from multibus.rules import STALE, RuleState, parse_rule_def
from tests.test_devices_api import needs_tc
from tests.test_rules import OV
from tests.test_rules_api import _feed
from tests.test_rules_recheck import (EMERG, _busy, _cap, _decide, _drain, _fast, _last, _limits, _plant,  # noqa: F401
                                      _revert)

# (read this way so the file imports on 3.94 too: the fail-before run)
DRIFT_HELD_WARN_S = getattr(_rules, 'DRIFT_HELD_WARN_S', 300.0)
CLOSED = 'power: 20 s to go before the next write (min_interval_s)'


class _Gate:
    """The referee's word, as a test says it: the channel open or closed."""

    def __init__(self, closed=True, why=CLOSED):
        self.closed, self.why, self.asked = closed, why, []

    def __call__(self, want, urgent):
        self.asked.append(urgent)
        if self.closed and not urgent:
            return ('hold', self.why, '')
        return None


def _st(over=None, *, gate=None, pace=True, state='Moderate', value=70, **timing):
    raw = {**OV, 'mode': 'armed', **(over or {})}
    raw['timing'] = {**OV['timing'], 'min_interval_s': 0, 'reassert_s': 10, **timing}
    r = parse_rule_def(raw)
    r.safe_params, r.tolerance = {'value': 100, 'revert_s': 0}, 1.0
    st = RuleState(r)
    st._wall = 1.0e9
    st.state = state
    st.want = {'value': value, 'revert_s': 0, 'ramp_s': 10}
    st.commanded = dict(st.want)
    st.gate, st.pace_reassert = gate, pace
    return st


def _drift(st, now, actual=100):
    return st._act(float(now), None, '', 250.0, actual, actual_age_s=0.0, actual_stale_s=60.0)


# ── D1: the drift asks the channel ───────────────────────────────────────────

def test_a_drift_waits_for_the_channel_and_goes_out_when_it_opens():
    g = _Gate()
    st = _st(gate=g)
    assert _drift(st, 0).action == 'idle'                       # watching
    d = _drift(st, 10)
    assert d.action == 'hold' and d.reason == f'drift seen — {CLOSED}'
    assert st.drift_since == 0.0                                # kept: out the first tick it opens
    assert _drift(st, 12).action == 'hold'
    g.closed = False
    assert _drift(st, 14).action == 'reassert'
    assert st.drift_since is None and st.drift_held_s is None


def test_without_a_pace_the_drift_asks_nothing():
    """3.94's behaviour where the channel has no pace (the OV `power`
    channel): the drift does not ask the hold."""
    st = _st(gate=_Gate(), pace=False)
    _drift(st, 0)
    assert _drift(st, 10).action == 'reassert'


@pytest.mark.parametrize('case', ['fast_step', 'stale_safe', 'rest'])
def test_an_emergency_want_reasserts_through_a_closed_channel(case):
    if case == 'fast_step':
        st = _st(gate=_Gate(), state='Emergency', value=50)
    elif case == 'stale_safe':
        st = _st({'on_stale': 'safe'}, gate=_Gate(), state=STALE, value=100)
    else:
        st = _st(gate=_Gate())
        st._rest_until = 1000.0                                 # resting after max_on_s
    assert st.emergency_want()
    _drift(st, 0, actual=0 if case == 'stale_safe' else 100)
    assert _drift(st, 10, actual=0 if case == 'stale_safe' else 100).action == 'reassert'


def test_a_forced_want_is_not_an_emergency():
    st = _st(gate=_Gate(), state='forced', value=50)
    st.forced = {'params': {'value': 50}, 'until': 2.0e9}
    assert not st.emergency_want()
    _drift(st, 0)
    assert _drift(st, 10).action == 'hold'


def test_the_rules_own_interval_still_holds_an_emergency_reassert():
    """D2: the rule's own min_interval_s applies to every reassert (3.94)."""
    st = _st(gate=_Gate(), state='Emergency', value=50, min_interval_s=30)
    st.last_cmd_ts = 5.0
    _drift(st, 0)
    d = _drift(st, 10)
    assert d.action == 'hold' and d.reason == 'drift seen, rate limit'


def test_a_safety_rules_routine_reassert_meets_the_pace():
    """Q1: the layer orders writers; the pace protects the equipment."""
    st = _st({'layer': 'safety'}, gate=_Gate())
    _drift(st, 0)
    assert _drift(st, 10).action == 'hold'


def test_a_shadow_rule_is_held_as_armed_would_be():
    st = _st({'mode': 'shadow'}, gate=_Gate())
    _drift(st, 0)
    assert _drift(st, 10).action == 'hold'
    st.gate.closed = False
    d = _drift(st, 12)
    assert d.action == 'shadow' and d.reason.startswith('would reassert')


# ── MED 3: the pending path keeps the emergency exemption ────────────────────

def test_a_pending_emergency_want_is_not_held_by_the_channel():
    """An emergency reassert that found the device's last command in flight
    is pending with `_urgent` False; the channel's pace still never holds it."""
    st = _st(gate=_Gate(), state='Emergency', value=50)
    st.pending, st._urgent = True, False
    d = st._act(0.0, None, '', 254.0, 100, actual_age_s=0.0, actual_stale_s=60.0)
    assert d.action == 'run'


def test_a_pending_routine_want_is_still_held_by_the_channel():
    st = _st(gate=_Gate())
    st.pending = True
    d = st._act(0.0, None, '', 251.5, 100, actual_age_s=0.0, actual_stale_s=60.0)
    assert d.action == 'hold' and d.reason == CLOSED


# ── LOW 9: the undo puts the drift back; LOW 8: starvation said ──────────────

def test_a_dropped_reassert_puts_its_drift_back():
    g = _Gate(closed=False)
    st = _st(gate=g)
    _drift(st, 0)
    assert _drift(st, 10).action == 'reassert'
    assert st.drift_since is None
    st.unsend(st._last_undo)
    assert st.drift_since == 0.0                               # the next tick may reassert at once


def test_a_drift_held_for_minutes_is_counted_and_shown():
    st = _st(gate=_Gate())
    _drift(st, 0)
    _drift(st, 10)
    _drift(st, 10 + DRIFT_HELD_WARN_S - 1)
    assert st.snapshot()['drift_held_long'] == 0 and st.snapshot()['drift_held_s'] is not None
    _drift(st, 10 + DRIFT_HELD_WARN_S + 1)
    _drift(st, 10 + DRIFT_HELD_WARN_S + 3)
    snap = st.snapshot()
    assert snap['drift_held_long'] == 1                        # once per drift
    assert snap['drift_held_s'] == pytest.approx(DRIFT_HELD_WARN_S + 3)
    _drift(st, 400, actual=70)                                 # the read-back matches again
    assert st.snapshot()['drift_held_s'] is None and st.snapshot()['drift_held_long'] == 1
    assert 'drift_held_s' not in _st().snapshot()             # never held: 3.94's state, key for key


@pytest.mark.parametrize('leave', ['yield', 'pending', 'paused', 'want_none'])
def test_a_tick_that_leaves_the_drift_branch_ends_the_held_drift(leave):
    """RP-9: a held drift's time stayed in the status (`rules_drift_held`)
    after the tick left the drift branch — a yield, a pending want, a pause."""
    g = _Gate()
    st = _st(gate=g)
    _drift(st, 0)
    _drift(st, 10)
    assert st.drift_held_s is not None
    if leave == 'yield':
        st.gate = lambda want, urgent: ('yield', 'cap drives the channel', 'cap')
    elif leave == 'pending':
        st.pending = True
    elif leave == 'paused':
        st.paused_until = st._wall + 600
    else:
        st.want = None
    _drift(st, 12)
    assert st.drift_held_s is None and 'drift_held_s' not in st.snapshot()


# ── through the runtime: scenario A, the head, the kill switch ───────────────

def _cap_drifting(client, interval_rule=0, reassert_s=10):
    _cap(client, 'cap', when='true', value=40,
         timing={'every_s': 2, 'debounce': 1, 'min_interval_s': interval_rule, 'reassert_s': reassert_s})


def _tick(app, ck, rt, limit, dt=2.0):
    _feed(app, ck, 'pv-u1', {'power_limit_pct': limit})
    out = rt.tick(ck())
    _drain(rt)
    ck.t += dt
    return out


@needs_tc
def test_a_foreign_write_holds_the_rules_reassert_for_the_channels_interval(tmp_path, monkeypatch):
    """Scenario A: a lease revert (or a refresh) writes the channel; the
    rule's read-back drifted — its reassert waits the channel's 60 s, not
    the rule's 10 s."""
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, interval=60, rule=False)
    inv = invs['pv-u1']
    _cap_drifting(client)
    _tick(app, ck, rt, 100)
    assert _limits(inv)[-1] == 4000                           # the cap's 40 %
    ck.t += 70
    _revert(app, 100)                                          # a foreign write, stamped now
    t_stamp = ck.t
    n = len(_limits(inv))
    while ck.t < t_stamp + 59:
        _tick(app, ck, rt, 100)
    assert len(_limits(inv)) == n, 'reasserted inside the channel interval'
    while ck.t < t_stamp + 64:
        _tick(app, ck, rt, 100)
    assert _limits(inv)[-1] == 4000                           # out when the channel opens
    assert any(d['reason'].startswith('drift seen — power:') for d in rt.decisions['cap'])


def _load_ov(rt, **over):
    """OV on a paced channel: the API refuses it (Q5), a file holds it — the
    runtime loads it as it is."""
    raw = dict(OV, mode='armed')
    raw['timing'] = {**OV['timing'], 'debounce': 1}
    raw.update(over)
    rt.store.save(list(rt.store.load()) + [raw])
    rt.load()
    assert 'ov-u1' in rt.rules and 'ov-u1' not in rt.errors


@needs_tc
def test_an_emergency_reassert_passes_a_foreign_stamp_at_the_head(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, interval=30, rule=False)
    inv = invs['pv-u1']
    _load_ov(rt, timing={**OV['timing'], 'debounce': 1, 'reassert_s': 10})
    _decide(app, ck, rt, EMERG)                                # 50 %, urgent
    _drain(rt)
    assert _limits(inv)[-1] == 5000
    ck.t += 40
    _feed(app, ck, 'pv-u1', dict(EMERG, power_limit_pct=100))  # RvrtTms: back at 100
    rt.tick(ck())
    ck.t += 10
    gate = _busy(rt)
    _feed(app, ck, 'pv-u1', dict(EMERG, power_limit_pct=100))
    rt.tick(ck())                                              # the reassert, waiting for the writer
    assert _last(rt)['action'] == 'reassert'
    _revert(app, 100)                                          # a foreign stamp before its head
    gate.set()
    _drain(rt)
    assert not _last(rt).get('dropped'), _last(rt)
    assert _limits(inv)[-1] == 5000


@needs_tc
def test_an_urgent_job_that_lost_the_referee_yields_at_the_head(tmp_path, monkeypatch):
    """MED 2: an emergency write must not overwrite a stricter want of a rule
    that took the channel (combine: min) after the decision."""
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, interval=0)
    _cap(client)                                               # no opinion until forced
    gate = _busy(rt)
    _decide(app, ck, rt, EMERG)                                # ov 50 %, urgent, waits
    assert client.post('/api/rules/cap/force', json={'value': 40, 'seconds': 600}).status_code == 200
    _decide(app, ck, rt, EMERG)                                # cap wants 40: the lowest wins
    gate.set()
    _drain(rt)
    assert next(d for d in rt.decisions['ov-u1'] if d['action'] == 'run').get('dropped') == 'yielded'
    assert 5000 not in _limits(invs['pv-u1'])


# ── Q5: a fast step on a paced channel is refused at the API ─────────────────

@needs_tc
def test_a_fast_step_rule_on_a_paced_channel_is_refused(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, interval=30, rule=False)
    r = client.post('/api/rules', json=dict(OV, mode='armed'))
    assert r.status_code == 422, r.text
    assert 'fast step' in r.text and 'min_interval_s 30' in r.text
    assert client.post('/api/rules', json=dict(OV, mode='shadow')).status_code == 200
    r = client.post('/api/rules/ov-u1/mode', json={'mode': 'armed'})
    assert r.status_code in (409, 422), r.text
    assert rt.rules['ov-u1'].mode == 'shadow'


@needs_tc
def test_a_pace_on_a_channel_a_fast_rule_drives_is_refused(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, interval=0)
    import copy
    raw = copy.deepcopy(cfg.get_raw_endpoint('pv'))
    g = next(x for x in raw['groups'] if x['id'] == 'inverters')
    g['channels'] = {'power': {'min_interval_s': 30}}
    r = client.put('/api/endpoints/pv', json=raw)
    assert r.status_code == 422, r.text
    assert "rule 'ov-u1'" in r.text and 'fast step' in r.text


@needs_tc
def test_an_installation_save_that_keeps_the_stored_pace_is_not_refused(tmp_path, monkeypatch):
    """The boot-allowed pace re-sent with a new max_age_s is no new pace
    (ADR-0006 diff) — the save restarts the units. RP-13: the plant's facade
    had no disconnect(), the save answered 500 and this went unasserted."""
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, interval=30, rule=False)
    _load_ov(rt)
    import copy
    raw = copy.deepcopy(cfg.get_raw_endpoint('pv'))
    g = next(x for x in raw['groups'] if x['id'] == 'inverters')
    g['channels'] = {'power': {'min_interval_s': 30, 'max_age_s': 20}}
    try:
        r = client.put('/api/endpoints/pv', json=raw)
        assert r.status_code == 200, r.text
        g['channels'] = {'power': {'min_interval_s': 45, 'max_age_s': 20}}
        r = client.put('/api/endpoints/pv', json=raw)
        assert r.status_code == 422 and "rule 'ov-u1'" in r.text, r.text
    finally:
        # the save restarted the units with real clients: their pollers end here
        for dev in ('pv-u1', 'pv-u2'):
            cl = app.state.registry.find(dev)[2]
            if cl is not None and hasattr(cl, 'disconnect'):
                cl.disconnect()


@needs_tc
def test_a_device_channel_edit_is_judged_on_what_it_changes(tmp_path):
    """RP-8: a channel a fast rule drives, its pace boot-allowed from the
    file — re-sending that pace with a new max_age_s is no new pace; a
    changed pace is still refused (Q5)."""
    from tests.test_command_binding import _yaml_app
    from tests.test_commands import _Inverter
    from tests.test_rules_api import _Clock, _rt
    yaml_text = """
security: { allow_writes: true }
devices:
  - id: pv-u1
    name: PV
    template: fronius_sunspec_inverter
    connection: { protocol: tcp, host: 192.0.2.9, port: 502, unit_id: 1 }
    channels:
      power: { min_interval_s: 30 }
    commands:
      - { name: power_limit }
"""
    cfg, app, client = _yaml_app(tmp_path, yaml_text, {'pv-u1': _Inverter(sf=-2)})
    rt = _rt(app, _Clock(t=1792465200.0))
    _load_ov(rt)
    assert rt.fast_rule_on('pv-u1', 'power') == 'ov-u1'
    r = client.put('/api/devices/pv-u1/channels/power', json={'min_interval_s': 30, 'max_age_s': 20})
    assert r.status_code == 200, r.text
    r = client.put('/api/devices/pv-u1/channels/power', json={'min_interval_s': 45})
    assert r.status_code == 422 and "rule 'ov-u1'" in r.text, r.text


@needs_tc
def test_a_file_that_holds_both_is_loaded_and_said(tmp_path, monkeypatch, caplog):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, interval=30, rule=False)
    with caplog.at_level('WARNING'):
        _load_ov(rt)
    assert rt.rules['ov-u1'].mode == 'armed'
    assert any('fast step' in m and 'loaded as it is' in m for m in caplog.messages)


# ── LOW 8: a lease shorter than the channel's interval is said ───────────────

@needs_tc
def test_a_lease_shorter_than_the_channels_interval_is_warned(tmp_path):
    from tests.test_command_binding import _yaml_app
    yaml_text = """
security: { allow_writes: true }
devices:
  - id: pv
    name: PV
    template: fronius_sunspec_inverter
    connection: { protocol: tcp, host: 192.0.2.9, port: 502, unit_id: 1 }
    channels:
      power: { min_interval_s: 60 }
    commands:
      - { name: power_limit }
"""
    from tests.test_commands import _Inverter
    cfg, app, client = _yaml_app(tmp_path, yaml_text, {'pv': _Inverter(sf=-2)})
    r = client.put('/api/devices/pv/commands/power_limit', json={'lease_s': 20})
    assert r.status_code == 200, r.text
    assert any('lease_s 20' in w and 'min_interval_s 60' in w for w in r.json().get('warnings') or [])
    r = client.put('/api/devices/pv/commands/power_limit', json={'lease_s': 90})
    assert not r.json().get('warnings')
