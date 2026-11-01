# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""A rule's write meets the dispatcher's checks under the bus lock
(ADR-0021): the head re-check (paused,
yielded, held), the undo bound to its send, the stamp inside the lock, RD-3,
RD-4, OV-F4-R3, RL-LOAD-R7. The kill switch `rules.recheck` was removed in
3.97: the re-check is always on."""
import json
import threading
import time

import pytest

import multibus.rules_runtime as m
from multibus import clock as mclock
from multibus.arbiter import ChannelState
from multibus.rules import RuleState, parse_rule_def
from tests.test_command_binding import _yaml_app
from tests.test_commands import _Inverter
from tests.test_devices_api import needs_tc
from tests.test_rules import OV
from tests.test_rules_api import _Clock, _feed, _rt

ALWAYS = 'between("00:00", "24:00")'
WARN = {'voltage_l1_n': 251.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231}        # Moderate: 70 %
EMERG = {'voltage_l1_n': 253.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231}       # Emergency (fast): 50 %


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    # the Fronius recipe settles 1 s; a tick waits up to 5 s for what it sent
    from multibus.commands import run_command
    monkeypatch.setitem(run_command.__kwdefaults__, 'sleep', lambda s: None)
    monkeypatch.setattr(m, 'COMMAND_WAIT_S', 0.3)
    mclock.set_timezone('UTC')
    yield
    mclock.set_now(None)


def _plant(tmp_path, monkeypatch, units=('pv-u1',), interval=30, rule=True, **ov):
    """The Fronius plant with the `power` channel at `interval` s, the OV rule
    on pv-u1 armed (debounce 1), the rules' clock ours."""
    import tests.test_commands_api as tca
    yaml_text = tca.PLANT_YAML.replace(
        "        commands:\n          - { name: power_limit }",
        f"        channels:\n          power: {{ min_interval_s: {interval} }}\n"
        "        commands:\n          - { name: power_limit }")
    invs = {d: _Inverter(sf=-2) for d in units}
    cfg, app, client = _yaml_app(tmp_path, yaml_text, invs)
    ck = _Clock(t=1792465200.0)
    rt = _rt(app, ck)
    if rule:
        raw = dict(OV, mode='armed', timing={**OV['timing'], 'debounce': 1}, **ov)
        if interval:
            # a fast step on a paced channel: the API refuses it (ADR-0022,
            # Q5), a rules.yaml holds it — loaded as the file is
            rt.store.save(list(rt.store.load()) + [raw])
            rt.load()
            assert 'ov-u1' in rt.rules and 'ov-u1' not in rt.errors
        else:
            r = client.post('/api/rules', json=raw)
            assert r.status_code == 200, r.text
    return cfg, app, client, ck, rt, invs


def _unit(app, dev='pv-u1'):
    _i, cfg, cl = app.state.registry.find(dev)
    return cfg, cl


def _limits(inv):
    return [w[1][0] for w in inv.writes]


def _busy(rt, dev='pv-u1'):
    """Hold the device's rules writer: what a rule sends now waits behind it."""
    gate = threading.Event()
    rt._writer(dev).submit(gate.wait, 10)
    return gate


def _drain(rt, dev='pv-u1'):
    rt._writer(dev).submit(lambda: None).result(10)
    rt._settle()


def _decide(app, ck, rt, values=WARN, dev='pv-u1'):
    _feed(app, ck, dev, dict(values, power_limit_pct=100))
    out = rt.tick(ck())
    ck.t += 2
    return out


def _last(rt, rid='ov-u1'):
    return next(d for d in rt.decisions[rid] if d['action'] in ('run', 'reassert'))


def _revert(app, value=100, dev='pv-u1'):
    """A lease revert: a writer the channel's owner neither refuses nor is
    paused by (outside is False) — it runs through the lane."""
    cfg, cl = _unit(app, dev)
    return app.state.run_command(cfg, cl, 'power_limit', {'value': value, 'revert_s': 0}, who='lease', ip='-',
                                 via='lease-revert', is_current=lambda: True)


def _lock(app, dev='pv-u1'):
    cfg, cl = _unit(app, dev)
    return app.state.cmd_lock_for(cl, cfg)


class _Barrier:
    """An inverter whose write of `value` (raw) blocks — holding the bus
    lock — until released."""

    def __init__(self, inv, raw):
        self.inv, self.raw = inv, raw
        self.entered, self.go = threading.Event(), threading.Event()
        real = inv.write

        def write(address, register_type='holding', values=None, **kw):
            if values and values[0] == raw and not self.entered.is_set():
                self.entered.set()
                self.go.wait(10)
            return real(address, register_type, values, **kw)
        inv.write = write


def _bg(fn, *a, **kw):
    out = {}

    def run():
        out['r'] = fn(*a, **kw)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


# ── CMD-Q-8, forward: a lane write lands between the rule's decision and its head ──

@needs_tc
def test_forward_a_lane_write_before_the_rules_head_holds_it(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    inv = invs['pv-u1']
    gate = _busy(rt)
    _decide(app, ck, rt)                                   # 70 % decided, waits for the writer
    assert _last(rt)['result'] == 'sent'
    code, res = _revert(app)                               # a lane write meanwhile: 100 %, stamped
    assert code == 200, res
    gate.set()
    _drain(rt)
    assert _limits(inv) == [10000]                         # the rule's 70 % never went out
    d = _last(rt)
    assert d['result'] == 'cancelled' and d['dropped'] == 'held' and d['result_reason'].startswith('held:')
    st = rt.states[('ov-u1', 'pv-u1')]
    assert st.pending is True and st.commanded is None and st.last_cmd_ts is None   # undone: never sent


@needs_tc
def test_forward_barrier_the_rule_waits_at_the_bus_lock(tmp_path, monkeypatch):
    """A real barrier transport: the lane write holds the bus lock while the
    rule decides and reaches the lock; it writes and stamps; the rule's head
    sees the channel moved and held."""
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    inv = invs['pv-u1']
    bar = _Barrier(inv, 10000)
    t, out = _bg(_revert, app)
    assert bar.entered.wait(5)                             # the lane write is on the wire, under the bus lock
    _decide(app, ck, rt)                                   # the rule decides 70 % and waits for the bus
    time.sleep(0.2)
    bar.go.set()
    t.join(5)
    _drain(rt)
    assert out['r'][0] == 200 and _limits(inv) == [10000]
    assert _last(rt)['dropped'] == 'held'


@needs_tc
def test_forward_an_urgent_want_is_written_anyway(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    inv = invs['pv-u1']
    bar = _Barrier(inv, 10000)
    t, out = _bg(_revert, app)
    assert bar.entered.wait(5)
    _decide(app, ck, rt, EMERG)                            # Emergency: fast, urgent
    time.sleep(0.2)
    bar.go.set()
    t.join(5)
    _drain(rt)
    assert _limits(inv) == [10000, 5000]                   # never held, never yielded
    assert _last(rt)['result'] == 'success'


# ── CMD-Q-8, mirror: the stamp inside the lock ───────────────────────────────

@needs_tc
def test_mirror_a_rules_stamp_is_inside_the_lock(tmp_path, monkeypatch):
    """A rule's write released the bus lock before it stamped the channel: a
    lane job re-checking the hold in that gap passed and wrote (D5)."""
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, rule=False)
    inv = invs['pv-u1']
    dev, cl = _unit(app)
    bar = _Barrier(inv, 7000)
    real_note = rt.channel_note

    def slow_note(*a, **k):                                # widen the gap the stamp used to leave
        time.sleep(0.3)
        return real_note(*a, **k)
    rt.channel_note = slow_note
    tr, rout = _bg(app.state.run_command, dev, cl, 'power_limit', {'value': 70, 'revert_s': 0},
                   who='rule x', ip='-', via='rule:x')
    assert bar.entered.wait(5)
    # admitted while the channel is open (not stamped yet); waits for the bus in its lane
    to, oout = _bg(client.post, '/api/devices/pv-u1/commands/power_limit', json={'value': 60, 'revert_s': 0})
    time.sleep(0.3)
    bar.go.set()
    tr.join(5)
    to.join(10)
    assert rout['r'][0] == 200
    r = oout['r']
    assert r.status_code == 409 and r.json()['held'] is True, r.text
    assert _limits(inv) == [7000]


# ── the head sees what changed while the job waited (real async writer) ──────

@needs_tc
@pytest.mark.parametrize('values', [WARN, EMERG], ids=['normal', 'urgent'])
def test_an_override_while_the_job_waits_drops_it_paused(tmp_path, monkeypatch, values):
    """Decided 2026-10-25: a pause that came after the decision drops the
    want, urgent included — someone took the channel to set it."""
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    gate = _busy(rt)
    _decide(app, ck, rt, values)
    assert client.post('/api/rules/ov-u1/override', json={'seconds': 600}).status_code == 200
    gate.set()
    _drain(rt)
    assert invs['pv-u1'].writes == []
    d = _last(rt)
    assert d['result'] == 'cancelled' and d['dropped'] == 'paused'
    assert rt.states[('ov-u1', 'pv-u1')].failures == 0           # not a failure


def _cap(client, rid='cap', when='false', value=40, **over):
    body = {'id': rid, 'mode': 'armed', 'target': {'device': 'pv-u1', 'command': 'power_limit'},
            'kind': 'condition', 'when': when, 'then': {'params': {'value': value}}, 'else': None,
            'params': {'revert_s': 0}, 'timing': {'every_s': 2, 'debounce': 1, 'min_interval_s': 0, 'reassert_s': 0}}
    body.update(over)
    r = client.post('/api/rules', json=body)
    assert r.status_code == 200, r.text


@needs_tc
def test_a_force_while_the_job_waits_drops_it_yielded(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    _cap(client)                                           # no opinion until forced
    gate = _busy(rt)
    _decide(app, ck, rt)                                   # ov 70 % waits
    assert client.post('/api/rules/cap/force', json={'value': 40, 'seconds': 600}).status_code == 200
    _decide(app, ck, rt)                                   # cap now wants 40 (the lowest wins: min)
    gate.set()
    _drain(rt)
    assert _last(rt)['dropped'] == 'yielded'
    assert 70 * 100 not in _limits(invs['pv-u1'])


@needs_tc
def test_a_higher_rules_want_while_the_job_waits_drops_it_yielded(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    _cap(client, 'hot', when='pv-u1.voltage_l2_n > 240', value=30, layer='safety')
    gate = _busy(rt)
    _decide(app, ck, rt)                                   # ov 70 % waits
    _decide(app, ck, rt, dict(WARN, voltage_l2_n=241))     # the safety rule now wants 30
    gate.set()
    _drain(rt)
    assert _last(rt)['dropped'] == 'yielded'
    assert 7000 not in _limits(invs['pv-u1'])


@needs_tc
def test_an_urgent_want_yields_at_the_head_to_a_stricter_one(tmp_path, monkeypatch):
    """3.94 wrote it (an urgent want was never re-yielded); ADR-0022 (review
    MED 2): an emergency write must not overwrite the stricter want of a rule
    that took the channel since — it is still never held."""
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    _cap(client)
    gate = _busy(rt)
    _decide(app, ck, rt, EMERG)                            # ov 50 % urgent waits
    assert client.post('/api/rules/cap/force', json={'value': 40, 'seconds': 600}).status_code == 200
    _decide(app, ck, rt, EMERG)
    gate.set()
    _drain(rt)
    assert _last(rt)['dropped'] == 'yielded'
    assert 5000 not in _limits(invs['pv-u1'])


@needs_tc
def test_the_synchronous_writer_sees_none_of_them(tmp_path, monkeypatch):
    """diff_day's path: the job runs at submit, the head sees what the tick
    saw — a force before the tick yields at the decision, never at the head."""
    from concurrent.futures import Future

    class _Sync:
        def submit(self, fn, *a, **kw):
            f = Future()
            f.set_result(fn(*a, **kw))
            return f
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    _cap(client, layer='manual')                           # decides first in a tick
    rt._writer = lambda dev: _Sync()
    assert client.post('/api/rules/cap/force', json={'value': 40, 'seconds': 600}).status_code == 200
    for _ in range(3):
        _decide(app, ck, rt)
    rt._settle()
    assert not any(d.get('dropped') for rid in ('ov-u1', 'cap') for d in rt.decisions[rid])
    assert _limits(invs['pv-u1']) == [4000]


# ── D4: the undo is bound to its send ────────────────────────────────────────

@needs_tc
def test_a_newer_send_owns_the_state_only_pending_is_set(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    gate = _busy(rt)
    _decide(app, ck, rt)                                   # send 1: 70 %, waits
    st = rt.states[('ov-u1', 'pv-u1')]
    n1 = st.send_no
    _decide(app, ck, rt, EMERG)                            # send 2: 50 % urgent, queued behind it
    assert st.send_no == n1 + 1 and _last(rt)['result'] == 'queued'
    sent2 = st.last_cmd_ts
    assert client.post('/api/rules/ov-u1/override', json={'seconds': 600}).status_code == 200
    gate.set()
    _drain(rt)
    assert [d['result'] for d in rt.decisions['ov-u1'] if d['action'] == 'run'][-1] == 'cancelled'
    # the drop of send 1 does not touch what send 2 set
    assert st.pending is True and st.last_cmd_ts == sent2 and st._sent_want['value'] == 50


def _onoff_state():
    raw = dict(OV, mode='armed')
    r = parse_rule_def(raw)
    r.safe_params, r.tolerance = {'value': 100, 'revert_s': 0}, 1.0
    st = RuleState(r)
    st._wall = 86400.0 * 20000 + 10
    st.day = mclock.day_key(st._wall)
    st.want = {'value': 70, 'revert_s': 0, 'ramp_s': 10}
    return st


def test_the_exact_start_is_taken_back():
    st = _onoff_state()
    st._starts.extend([100.0, 200.0])
    st.starts_today = 2
    st._send(300.0, 'run', '', None, None, False)
    undo, n = st._last_undo, st.send_no
    assert list(st._starts) == [100.0, 200.0, 300.0] and st.starts_today == 3 and st.on_since == 300.0
    st.unsend(undo)
    assert list(st._starts) == [100.0, 200.0] and st.starts_today == 2
    assert st.on_since is None and st.commanded is None and st.last_cmd_ts is None
    assert st.pending is True and st.send_no == n            # the counter never goes back


def test_a_midnight_roll_leaves_starts_today_alone():
    st = _onoff_state()
    st._send(300.0, 'run', '', None, None, False)
    undo = st._last_undo
    st.day, st.starts_today = 'tomorrow', 1                  # the day rolled; one start counted today
    st.unsend(undo)
    assert st.starts_today == 1 and list(st._starts) == []


def test_on_since_is_restored_only_where_this_send_set_it():
    st = _onoff_state()
    st.off_since = 50.0
    st._send(300.0, 'run', '', None, None, False)
    undo = st._last_undo
    assert st.on_since == 300.0 and st.off_since is None
    st.on_since = 400.0                                      # something later moved it
    st.unsend(undo)
    assert st.on_since == 400.0 and st.off_since == 50.0


# ── D2: one write sequence ───────────────────────────────────────────────────

@needs_tc
def test_another_rules_release_moves_the_seq_and_holds(tmp_path, monkeypatch):
    # ov has no opinion at rest (normal: none), so cap's release is not left to it
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, normal=None)
    inv = invs['pv-u1']
    _cap(client, when=ALWAYS, value=90)
    _decide(app, ck, rt, {'voltage_l1_n': 230, 'voltage_l2_n': 230, 'voltage_l3_n': 230})   # cap writes 90
    _drain(rt)
    assert _limits(inv) == [9000]
    ck.t += 60                                             # the channel is open again
    gate = _busy(rt)
    assert client.post('/api/rules/cap/mode', json={'mode': 'shadow'}).status_code == 200   # its release queued
    _decide(app, ck, rt)                                   # ov 70 % waits behind it
    gate.set()
    assert rt.wait_released(10)
    _drain(rt)
    assert _limits(inv) == [9000, 10000]                   # the release went out; ov held (30 s)
    assert _last(rt)['dropped'] == 'held'


@needs_tc
def test_a_replaced_channel_state_asks_the_hold(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    gate = _busy(rt)
    _decide(app, ck, rt)
    # an HA import replaces the channel: written 5 s ago, no sequence number
    rt.import_mirror({'channels': {'pv-u1\tpower': {'on': None, 'last_write': 5.0, 'starts': []}}})
    assert rt.channels[('pv-u1', 'power')].seq is None
    gate.set()
    _drain(rt)
    assert _last(rt)['dropped'] == 'held' and invs['pv-u1'].writes == []


def test_every_stamp_takes_the_next_number_runtime_wide(monkeypatch):
    monkeypatch.setattr(m, 'turns_on', lambda cdef, params: True if params else None)
    c1, c2 = ChannelState(), ChannelState()
    assert c1.seq is None
    rt = m.RulesRuntime.__new__(m.RulesRuntime)
    rt._chan = lambda cfg, command: ('power', {}, type('D', (), {'writes': [], 'name': command, 'on': None})())
    rt._mono = lambda: 1.0
    rt.channels = {('a', 'power'): c1, ('b', 'power'): c2}
    cfg, other = type('C', (), {'id': 'a'})(), type('C', (), {'id': 'b'})()
    rt.channel_note(cfg, 'power_limit', {})
    rt.channel_reconcile(other, 'power_limit', {'value': 1})     # on learnt: the channel moved
    learnt = c2.seq
    rt.channel_note(cfg, 'power_limit', {})
    assert learnt is not None and c1.seq > learnt
    rt.channel_reconcile(other, 'power_limit', {'value': 1})     # nothing learnt: no number (RC-4)
    assert c2.seq == learnt


# ── locks ────────────────────────────────────────────────────────────────────

@needs_tc
def test_the_recheck_never_waits_for_the_rules_lock(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    held, done = threading.Event(), threading.Event()

    def hold():
        with rt._lock:
            held.set()
            done.wait(10)
    threading.Thread(target=hold, daemon=True).start()
    assert held.wait(5)
    try:
        st = RuleState(rt.rules['ov-u1'])
        rt.states[('ov-u1', 'pv-u1')] = st
        dev, cl = _unit(app)
        t, out = _bg(rt._run_command, dev, cl, 'power_limit', {'value': 70, 'revert_s': 0}, who='rule ov-u1',
                     ip='-', via='rule:ov-u1',
                     recheck=rt._recheck_for(rt.rules['ov-u1'], dev, {'value': 70}, False, None))
        t.join(5)
        assert not t.is_alive() and out['r'][0] == 200
    finally:
        done.set()


@needs_tc
def test_owner_of_is_never_asked_under_the_bus_lock(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    lock = _lock(app)
    seen = []
    real = rt.owner_of

    def spy(*a, **k):
        seen.append(lock.locked())
        return real(*a, **k)
    rt.owner_of = spy
    gate = _busy(rt)
    _decide(app, ck, rt)
    r = client.post('/api/devices/pv-u1/commands/power_limit', json={'value': 60, 'override_s': 60})
    assert r.status_code == 200, r.text
    gate.set()
    _drain(rt)
    assert seen and not any(seen)


@needs_tc
def test_the_raw_lease_revert_holds_the_bus_lock(tmp_path, monkeypatch):
    """RD-4: the raw (register) revert wrote without the bus lock."""
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, rule=False)
    lock = _lock(app)
    revert = app.state.make_lease_revert('pv-u1', 'holding', 40232, 'uint16', 1.0, 100)
    _i, _c, facade = app.state.registry.find('pv-u1')
    seen = []
    facade.write_value = lambda *a, **k: (seen.append(lock.locked()), (True, None, [100]))[1]
    revert(lambda: True)
    assert seen == [True]


# ── D6: standby at the head ──────────────────────────────────────────────────

@needs_tc
def test_standby_at_the_head_cancels_a_rule_write_and_fails_a_release(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    dev, cl = _unit(app)
    lock = _lock(app)
    said = []
    rt.event_log = type('E', (), {'add': lambda s, level, src, msg, *a, **k: said.append(msg)})()
    with lock:                                              # the bus is busy
        t, out = _bg(rt._run_command, dev, cl, 'power_limit', {'value': 70, 'revert_s': 0}, who='rule ov-u1',
                     ip='-', via='rule:ov-u1',
                     recheck=rt._recheck_for(rt.rules['ov-u1'], dev, {'value': 70}, False, None))
        t2, out2 = _bg(rt._release_one, rt.rules['ov-u1'], dev, cl, {'value': 100, 'revert_s': 0}, 'test')
        time.sleep(0.3)
        app.state.role.role = 'standby'                    # the role changed while they waited
    try:
        t.join(5)
        t2.join(5)
        code, res = out['r']
        assert code == 409 and res['status'] == 'cancelled' and res['dropped'] == 'standby'
        assert any('release to safe FAILED' in x for x in said), said     # a release keeps its 503
    finally:
        app.state.role.role = 'active'
    assert invs['pv-u1'].writes == []


# ── the review-12 list ───────────────────────────────────────────────────────

@needs_tc
def test_a_rules_write_still_ends_every_lease_on_its_channel(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    r = client.post('/api/devices/pv-u1/commands/power_limit', json={'value': 90, 'lease_s': 600, 'override_s': 1})
    assert r.status_code == 200 and r.json()['lease_armed'] is True, r.text
    ck.t += 60
    for _ in range(2):
        _decide(app, ck, rt)
    _drain(rt)
    assert 7000 in _limits(invs['pv-u1'])
    assert [x for x in app.state.lease_manager.snapshot() if x['device'] == 'pv-u1'] == []


@needs_tc
def test_four_units_on_one_lane_are_all_written(tmp_path, monkeypatch):
    units = ('pv-u1', 'pv-u2', 'pv-u3', 'pv-u4')
    import tests.test_commands_api as tca
    monkeypatch.setattr(tca, 'PLANT_YAML', tca.PLANT_YAML.replace('units: [1, 2]', 'units: [1, 2, 3, 4]'))
    # no pace: OV (a fast step) is armed through the API (ADR-0022, Q5)
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, units=units, rule=False, interval=0)
    for inv in invs.values():
        inv.bus = 'datamanager'                               # one wire, one bus lock (the Datamanager)
    assert len({app.state.lane_key(*_unit(app, d)) for d in units}) == 1
    for d in units:
        raw = dict(OV, id='ov-' + d.split('-')[1], mode='armed', target={'device': d, 'command': 'power_limit'},
                   signal=f'max({d}.voltage_l1_n, {d}.voltage_l2_n, {d}.voltage_l3_n)',
                   timing={**OV['timing'], 'debounce': 1})
        assert client.post('/api/rules', json=raw).status_code == 200
    for d in units:
        _feed(app, ck, d, dict(WARN, power_limit_pct=100))
    rt.tick(ck())
    for d in units:
        _drain(rt, d)
    assert all(_limits(invs[d]) == [7000] for d in units)
    assert not any(x.get('dropped') for rid in rt.decisions for x in rt.decisions[rid])


# ── OV-F4-R3: no token on any drop ───────────────────────────────────────────

@needs_tc
def test_a_drop_spends_no_rate_token(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    cfg.security.write_rate_limit_per_s = 1.0                 # one token
    st = RuleState(rt.rules['ov-u1'])
    st.paused_until = ck() + 600
    rt.states[('ov-u1', 'pv-u1')] = st
    dev, cl = _unit(app)
    code, res = rt._run_command(dev, cl, 'power_limit', {'value': 70, 'revert_s': 0}, who='rule ov-u1', ip='-',
                                via='rule:ov-u1',
                                recheck=rt._recheck_for(rt.rules['ov-u1'], dev, {'value': 70}, False, None))
    assert res['dropped'] == 'paused'
    st.paused_until = None
    code, res = rt._run_command(dev, cl, 'power_limit', {'value': 70, 'revert_s': 0}, who='rule ov-u1', ip='-',
                                via='rule:ov-u1',
                                recheck=rt._recheck_for(rt.rules['ov-u1'], dev, {'value': 70}, False, None))
    assert code == 200, res                                   # the token was still there


# ── RD-3: the safety holder asked at the head ────────────────────────────────

@needs_tc
def test_an_outside_command_meets_a_safety_rule_armed_while_it_waited(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, rule=False)
    lock = _lock(app)
    with lock:
        t, out = _bg(client.post, '/api/devices/pv-u1/commands/power_limit', json={'value': 60, 'revert_s': 0})
        time.sleep(0.3)                                       # admitted: nobody owned the channel
        _cap(client, 'hot', when=ALWAYS, value=30, layer='safety')
        rt._writer = lambda dev: type('W', (), {'submit': lambda s, *a, **k: _never()})()   # it only decides
        rt.tick(ck())
        assert rt.states[('hot', 'pv-u1')].want is not None
    t.join(10)
    r = out['r']
    assert r.status_code == 409 and "safety rule 'hot'" in r.json()['reason'], r.text
    assert 6000 not in _limits(invs['pv-u1'])


def _never():
    from concurrent.futures import Future
    return Future()


# ── RL-LOAD-R7 (the delete half) ─────────────────────────────────────────────

@needs_tc
def test_a_deleted_rule_recreated_gets_no_old_clamp(tmp_path, monkeypatch):
    # no pace: OV is recreated through the API (a fast step on a paced channel
    # is refused there, ADR-0022 Q5)
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, interval=0)
    _decide(app, ck, rt, {'voltage_l1_n': 230, 'voltage_l2_n': 230, 'voltage_l3_n': 230})
    assert client.post('/api/rules/ov-u1/clamp', json={'max': 60}).status_code == 200
    raw = dict(rt.raw['ov-u1'])
    assert client.delete('/api/rules/ov-u1').status_code == 200
    assert client.post('/api/rules', json=raw).status_code == 200
    _decide(app, ck, rt, {'voltage_l1_n': 230, 'voltage_l2_n': 230, 'voltage_l3_n': 230})
    assert rt.states[('ov-u1', 'pv-u1')].clamp is None


@needs_tc
def test_a_tick_save_and_a_delete_at_once_tear_nothing(tmp_path, monkeypatch):
    """Deterministic: the tick's save (no rules lock) is stopped in the middle
    of its walk over the states while a delete runs on another thread. Before
    RL-LOAD-R7 the delete popped states under the walk (dict changed size)
    and never saved after itself: the deleted rule's counters stayed on disk."""
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    for i in range(3):
        _cap(client, f'c{i}', value=90, mode='shadow')
        rt.states[(f'c{i}', 'pv-u1')].on_total_s = 5.0      # something to save
    first = next(iter(rt.states.values()))
    real, deleted, fired = first.saved_counters, threading.Event(), []

    def mid_walk():
        if not fired:
            fired.append(1)
            threading.Thread(target=lambda: (rt.delete('c2', who='t'), deleted.set()), daemon=True).start()
            deleted.wait(1.0)                              # the delete gets in now, if nothing stops it
        return real()
    first.saved_counters = mid_walk
    rt._save_state()                                       # the tick's save
    assert fired and deleted.wait(10)
    data = json.loads((tmp_path / 'rules_state.json').read_text())
    assert 'c2:pv-u1' not in data and 'c0:pv-u1' in data   # the last save is the newest


def test_a_stamp_while_the_channels_are_saved_tears_nothing():
    """RC-2, deterministic: a writer thread stamps the channel (appends a
    start) while the save walks its starts — here, from inside the walk."""
    c = ChannelState()

    class _Start(float):
        def __rsub__(self, other):                         # now - t, in the walk
            c.starts.append(float(other))                  # a stamp meanwhile
            return float(other) - float(self)
    c.starts.extend([_Start(10.0), _Start(20.0)])
    assert c.export_mirror(100.0)['starts'] == [90.0, 80.0]


# ── review 3 (RC-3 .. RC-6) ──────────────────────────────────────────────────

@needs_tc
def test_an_urgent_want_decided_after_the_dropped_send_stays_urgent(tmp_path, monkeypatch):
    """RC-3: a paused tick set _urgent (an Emergency step) without a send;
    the drop of the earlier job undid it to the pre-send False, and the
    curtail went out later at the rate limit, non-urgent."""
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    gate = _busy(rt)
    _decide(app, ck, rt)                                   # send 1: 70 %, non-urgent, waits
    st = rt.states[('ov-u1', 'pv-u1')]
    assert client.post('/api/rules/ov-u1/override', json={'seconds': 60}).status_code == 200
    _decide(app, ck, rt, EMERG)                            # paused: the fast step sets _urgent, no send
    assert st._urgent is True and st.want['value'] == 50
    gate.set()
    _drain(rt)
    assert _last(rt)['dropped'] == 'paused'
    assert st._urgent is True and st.pending is True


def test_the_undo_keeps_what_a_later_tick_set():
    """RC-3, by delta: only the fields that still hold what the send set go
    back; a read-back that matched since (commanded + confirmed) stays."""
    st = _onoff_state()
    st._send(300.0, 'run', '', None, None, False)
    undo = st._last_undo
    st._urgent = True                                      # a later fast step, not sent
    st.commanded, st.confirmed = dict(st.want), True       # the read-back matched since
    st.unsend(undo)
    assert st._urgent is True
    assert st.commanded == st.want and st.confirmed is True and st.pending is False
    assert st.last_cmd_ts is None and st._sent_want is None  # what nothing touched goes back


def test_the_undo_restores_an_urgent_send_it_took_back():
    st = _onoff_state()
    st._urgent = True
    st._send(300.0, 'run', '', None, None, False)
    st.unsend(st._last_undo)
    assert st._urgent is True and st.pending is True


@needs_tc
def test_a_reconcile_that_learnt_nothing_does_not_ask_the_hold(tmp_path, monkeypatch):
    """RC-4: a no-op refresh (the device already held it, nothing written)
    took a sequence number; the rule's next write then asked the hold,
    judged on the rule's own earlier write."""
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    dev, _cl = _unit(app)
    rt.channel_note(dev, 'power_limit', {'value': 70, 'revert_s': 0})   # the rule's own write
    seq = rt._seq_of(dev, 'power_limit')
    rt.channel_reconcile(dev, 'power_limit', {'value': 70, 'revert_s': 0})
    assert rt._seq_of(dev, 'power_limit') == seq
    assert rt._recheck(rt.rules['ov-u1'], dev, {'value': 70, 'revert_s': 0}, False, seq) is None


@needs_tc
def test_a_shutdown_revert_gives_up_on_a_busy_bus(tmp_path, monkeypatch):
    """RC-5: the raw lease revert waited for the bus lock with no timeout;
    the shutdown budget was only checked between leases."""
    from multibus.write_lease import WriteLeaseManager
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, rule=False)
    mgr = WriteLeaseManager()
    mgr.arm('pv-u1', 'holding', 40232, 60000,
            app.state.make_lease_revert('pv-u1', 'holding', 40232, 'uint16', 1.0, 100))
    _i, _c, facade = app.state.registry.find('pv-u1')
    wrote = []
    facade.write_value = lambda *a, **k: (wrote.append(a), (True, None, [100]))[1]
    lock = _lock(app)
    assert lock.acquire(timeout=5)
    try:
        t, _out = _bg(mgr.stop, True, 0.3)
        t.join(3)
        hung = t.is_alive()
        app.state.command_dispatch.stop()                  # main.py's order (ADR-0022, review HIGH 1)
    finally:
        lock.release()
    t.join(5)
    time.sleep(0.3)                                        # a lane job still holding on would write now
    assert not hung
    assert ('pv-u1', 'holding', 40232) in mgr._leases       # left for boot recovery
    assert wrote == []


@needs_tc
def test_the_head_asks_the_rule_as_it_is_now(tmp_path, monkeypatch):
    """RC-6: a reload lowered the rule's priority while its job waited; the
    head judged it with the priority it had at the decision."""
    # no pace: the PUT of an armed fast-step rule (ADR-0022, Q5)
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, interval=0)
    _cap(client, 'hot', when='pv-u1.voltage_l2_n > 240', value=70, priority=40)
    gate = _busy(rt)
    _decide(app, ck, rt)                                   # ov 70 % (priority 50) waits
    _decide(app, ck, rt, dict(WARN, voltage_l2_n=241))     # hot wants 70 too: yields to ov on the tie
    assert rt.states[('hot', 'pv-u1')].yielding_to == 'ov-u1'
    r = client.put('/api/rules/ov-u1', json=dict(rt.raw['ov-u1'], priority=30))
    assert r.status_code == 200, r.text
    gate.set()
    _drain(rt)
    assert _last(rt)['dropped'] == 'yielded'


# ── 3.97: the kill switch is gone, the re-check is always asked ──────────────

@needs_tc
def test_a_rules_job_always_carries_its_recheck(tmp_path, monkeypatch):
    """`rules.recheck` was removed in 3.97 (ADR-0021, amended): a rule's job
    always carries what the head of the bus asks again, whatever a stale
    environment says."""
    monkeypatch.setenv('MBG_RULES_RECHECK', 'off')
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch)
    seen = []
    real = rt._run_command
    rt._run_command = lambda *a, **k: (seen.append(sorted(k)), real(*a, **k))[1]
    _decide(app, ck, rt)
    _drain(rt)
    assert seen == [['ip', 'recheck', 'still_wanted', 'via', 'who']]
    status = client.get('/api/status').json()
    assert 'rules_recheck' not in status and 'raw_lanes' not in status
