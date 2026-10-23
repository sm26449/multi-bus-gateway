# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Sequences (rules phase 3 §4): commands in order with waits and checks,
undone in reverse when a step fails or a person cancels — through the one
write path, so a safety rule still stops them."""
import time

from multibus import clock
from multibus.sequences import SequenceRunner, parse_sequence, validate_sequence
from tests.test_endpoints import needs_tc

START = {
    'id': 'start-pool', 'label': 'Open the valve, then start the pump',
    'params': {'speed': {'min': 15, 'max': 50, 'default': 30}},
    'steps': [
        {'command': {'device': 'valve1', 'name': 'open'}},
        {'wait_until': 'valve1.position >= 95', 'timeout_s': 30},
        {'command': {'device': 'vfd1', 'name': 'run_at', 'params': {'value': '${speed}'}}},
        {'wait_s': 10},
        {'check': 'vfd1.output_current > 0.5'},
    ],
    'on_abort': [
        {'command': {'device': 'vfd1', 'name': 'run_at', 'params': {'value': 0}}},
        {'command': {'device': 'valve1', 'name': 'close'}},
    ],
}
OFFERED = {'valve1': ['open', 'close'], 'vfd1': ['run_at']}


class _Plant:
    """The gateway as a sequence sees it: a write path that records, a live
    store that the steps change, a clock that sleeps instantly."""

    def __init__(self, current=1.2, valve_opens=True, refuse=None):
        self.values = {'valve1.position': 0, 'vfd1.output_current': 0.0}
        self.writes, self.current, self.valve_opens, self.refuse = [], current, valve_opens, refuse
        self.t = 0.0

    def run_command(self, device, name, params, *, who, via, dry_run=False):
        self.writes.append((device, name, {k: v for k, v in params.items() if k != 'override_s'}, dry_run))
        if self.refuse and (device, name) == self.refuse:
            return 409, {'status': 'rejected', 'reason': "safety rule 'hot' holds this channel now"}
        if dry_run:
            return 200, {'status': 'dry_run'}
        if (device, name) == ('valve1', 'open') and self.valve_opens:
            self.values['valve1.position'] = 100
        if (device, name) == ('vfd1', 'run_at'):
            self.values['vfd1.output_current'] = self.current if params['value'] > 0 else 0.0
        return 200, {'status': 'success'}

    def resolver(self, _store):
        return lambda name: self.values.get(name)

    def sleep(self, s):
        self.t += s

    def mono(self):
        return self.t


def _runner(plant, **kw):
    return SequenceRunner(run_command=plant.run_command, resolver_factory=plant.resolver,
                          sleep=plant.sleep, mono=plant.mono, **kw)


def _cmds(plant):
    return [(d, n, p.get('value')) for d, n, p, _dry in plant.writes]


# ── the definition ───────────────────────────────────────────────────────────

def test_a_sequence_is_checked_in_words():
    ok = validate_sequence(START, devices=lambda d: OFFERED.get(d))
    assert ok == []
    errs = validate_sequence({**START, 'steps': []}, devices=lambda d: OFFERED.get(d))
    assert any('at least one step' in e for e in errs)
    bad = {**START, 'steps': [{'command': {'device': 'pump9', 'name': 'run'}},
                              {'command': {'device': 'vfd1', 'name': 'explode'}},
                              {'wait_s': 0}, {'wait_until': 'x.y > 1'}, {'wait_s': 1, 'check': 'x.y > 1'},
                              {'command': {'device': 'vfd1', 'name': 'run_at', 'params': {'value': '${nope}'}}}],
           'on_abort': [{'wait_s': 5}]}
    errs = ' | '.join(validate_sequence(bad, devices=lambda d: OFFERED.get(d)))
    for frag in ("no device 'pump9'", "offers no enabled command 'explode'", 'steps[2].wait_s', 'steps[3].timeout_s',
                 'steps[4]: exactly one of', 'steps[5].command.params.value', 'on_abort holds commands only'):
        assert frag in errs, frag


# ── running ──────────────────────────────────────────────────────────────────

def test_the_steps_run_in_order_with_their_waits_and_checks():
    plant = _Plant()
    r = _runner(plant)
    code, _run = r.start(parse_sequence(START), {'speed': 35}, who='ops', via='api', wait=True)
    assert code == 202
    assert _cmds(plant) == [('valve1', 'open', None), ('vfd1', 'run_at', 35)]
    run = r.view('start-pool')['last'][0]
    assert run['status'] == 'done' and [s['what'] for s in run['steps']] == ['command', 'wait_until', 'command', 'wait', 'check']
    assert plant.t >= 10                                        # the 10 s wait was waited


def test_a_failed_check_undoes_in_reverse():
    plant = _Plant(current=0.1)                                 # the motor does not draw: dry, or a broken belt
    r = _runner(plant)
    r.start(parse_sequence(START), {}, who='ops', via='api', wait=True)
    run = r.view('start-pool')['last'][0]
    assert run['status'] == 'aborted' and 'is not true' in run['reason']
    assert _cmds(plant)[-2:] == [('vfd1', 'run_at', 0), ('valve1', 'close', None)]
    assert [s['what'] for s in run['steps']][-2:] == ['on_abort', 'on_abort']


def test_a_wait_that_never_comes_true_aborts_after_its_timeout():
    plant = _Plant(valve_opens=False)
    r = _runner(plant)
    r.start(parse_sequence(START), {}, who='ops', via='api', wait=True)
    run = r.view('start-pool')['last'][0]
    assert run['status'] == 'aborted' and 'not true after 30' in run['reason']
    assert ('vfd1', 'run_at', 30) not in _cmds(plant)           # the pump never started on a closed valve
    assert 29 <= plant.t <= 32


def test_a_refused_command_aborts_and_says_why():
    plant = _Plant(refuse=('vfd1', 'run_at'))
    r = _runner(plant)
    r.start(parse_sequence(START), {}, who='ops', via='api', wait=True)
    run = r.view('start-pool')['last'][0]
    assert run['status'] == 'aborted' and "safety rule 'hot'" in run['reason']


def test_a_person_can_cancel_and_the_undo_still_runs():
    plant = _Plant()
    real = {'t': 0.0}
    plant.sleep = lambda s: (real.__setitem__('t', real['t'] + s), time.sleep(0.01))
    plant.mono = lambda: real['t']
    r = _runner(plant)
    seq = parse_sequence(START)
    code, _ = r.start(seq, {}, who='ops', via='api')
    assert code == 202
    for _ in range(200):
        if ('vfd1', 'run_at', 30) in _cmds(plant):
            break
        time.sleep(0.01)
    assert r.start(seq, {}, who='ops', via='api')[0] == 409     # one run at a time
    other = parse_sequence({'id': 'flush', 'steps': [{'command': {'device': 'vfd1', 'name': 'run_at', 'params': {'value': 20}}}]})
    assert 'is in sequence' in r.start(other, {}, who='ops', via='api')[1]['reason']
    assert r.cancel('start-pool', who='hmi')
    for _ in range(300):
        if not r.view('start-pool')['running']:
            break
        time.sleep(0.01)
    run = r.view('start-pool')['last'][0]
    assert run['status'] == 'cancelled' and run['reason'] == 'cancelled by hmi'
    assert _cmds(plant)[-2:] == [('vfd1', 'run_at', 0), ('valve1', 'close', None)]


def test_a_dry_run_plans_every_command_and_writes_nothing():
    plant = _Plant()
    code, run = _runner(plant).start(parse_sequence(START), {'speed': 40}, who='ops', via='api', dry_run=True)
    assert code == 200 and run['status'] == 'dry_run'
    assert all(dry for *_x, dry in plant.writes) and _cmds(plant) == [('valve1', 'open', None), ('vfd1', 'run_at', 40)]
    assert plant.t == 0                                         # no wait was waited


def test_parameters_meet_their_bounds():
    code, res = _runner(_Plant()).start(parse_sequence(START), {'speed': 80}, who='ops', via='api')
    assert code == 422 and 'speed' in res['reason']


# ── through the gateway, on the drive ────────────────────────────────────────

@needs_tc
def test_a_sequence_pauses_a_lower_rule_and_a_safety_rule_stops_it(tmp_path):
    from tests.test_rules_arbiter import ALWAYS, _drive_app, _freqs, _rule
    app, client, rt, drive, ck = _drive_app(tmp_path)
    clock.set_timezone('UTC')
    seq = {'id': 'boost', 'label': 'Run 30 Hz briefly',
           'steps': [{'command': {'device': 'vfd1', 'name': 'run_at', 'params': {'value': 30}}},
                     {'check': 'hour() >= 0'}],
           'on_abort': [{'command': {'device': 'vfd1', 'name': 'run_at', 'params': {'value': 0}}}]}
    assert client.post("/api/sequences", json=seq).status_code == 200
    assert client.post("/api/sequences", json={**seq, 'steps': [{'command': {'device': 'vfd1', 'name': 'nope'}}]}).status_code == 422
    assert client.post("/api/rules", json=_rule('day', 'schedule', ALWAYS, 25)).status_code == 200
    rt.tick()
    r = client.post("/api/sequences/boost/run", json={})
    assert r.status_code == 200 and r.json()['status'] in ('running', 'done')
    for _ in range(100):
        if not client.get("/api/sequences/boost").json()['running']:
            break
        time.sleep(0.05)
    last = client.get("/api/sequences/boost").json()['last'][0]
    assert last['status'] == 'done' and _freqs(drive)[-1] == 30
    assert rt.states[('day', 'vfd1')].paused_until is not None   # the schedule waits while the sequence acts
    plan = client.post("/api/sequences/boost/run", json={'dry_run': True}).json()
    assert plan['status'] == 'dry_run' and len(_freqs(drive)) == 2
    # a safety rule holding the drive: the sequence is refused at its first command
    assert client.post("/api/rules", json=_rule('hot', 'safety', ALWAYS, 0)).status_code == 200
    rt.tick()
    client.post("/api/sequences/boost/run", json={})
    for _ in range(100):
        last = client.get("/api/sequences/boost").json()['last'][0]
        if last['started'] > plan['started'] and not client.get("/api/sequences/boost").json()['running']:
            break
        time.sleep(0.05)
    assert last['status'] == 'aborted' and "safety rule 'hot'" in last['reason']


def test_while_it_undoes_it_says_so_and_ends_with_the_reason():
    plant = _Plant(current=0.1)
    seen = []
    r = _runner(plant)
    real = plant.run_command

    def spy(device, name, params, **kw):
        live = r.view('start-pool')['running']
        if live and live['status'] == 'undoing':
            seen.append((name, live['reason']))
        return real(device, name, params, **kw)
    r._run_command = spy
    r.start(parse_sequence(START), {}, who='ops', via='api', wait=True)
    assert [n for n, _ in seen] == ['run_at', 'close'] and 'is not true' in seen[0][1]
    assert r.view('start-pool')['last'][0]['status'] == 'aborted'


def test_sequences_travel_in_a_snapshot_and_are_checked_on_the_way_back(tmp_path):
    import io
    import zipfile

    import yaml

    from multibus.snapshots import BUNDLE_EXTRAS, validate_bundle
    assert 'sequences.yaml' in BUNDLE_EXTRAS
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('sequences.yaml', yaml.safe_dump({'sequences': [START, {'id': 'Bad Id', 'steps': []}]}))
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        errs = validate_bundle(zf)
    assert any('sequences.yaml[1]' in e for e in errs) and not any('sequences.yaml[0]' in e for e in errs)
