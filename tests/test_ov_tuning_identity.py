# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""ADR-0012: the opt-in tuning keys change NOTHING while they are off.

The engine is run in lockstep against a frozen copy of itself from before
the keys (tests/_baseline/rules_baseline.py, 042bdee) on random streams that
stay near every threshold — repeated samples, read-backs that come and go,
clamps, forces, pauses, failed and queued sends — and every Decision, every
snapshot() and every export_mirror() must be equal, tick by tick. Then, with
the keys ON, the safety property: at every instant the rule is at least as
far up the steps as without them, and what it has written is never a higher
(less protective) limit."""
import importlib.util
import random
import sys
from pathlib import Path

import pytest

from multibus import rules as new
from tests.test_rules import OV
from tests.test_rules_schedule import WEEK
from tests.test_rules_trim import SURPLUS

_spec = importlib.util.spec_from_file_location(
    'rules_baseline', Path(__file__).parent / '_baseline' / 'rules_baseline.py')
base = importlib.util.module_from_spec(_spec)
sys.modules['rules_baseline'] = base          # dataclasses look their module up
_spec.loader.exec_module(base)

COND = {'id': 'buffer', 'mode': 'armed', 'kind': 'condition', 'target': {'device': 'hp', 'command': 'power'},
        'when': 'hp.water_in < 25', 'release': 'hp.water_in > 35',
        'then': {'params': {'value': 1}}, 'else': {'params': {'value': 0}},
        'timing': {'debounce': 2, 'min_interval_s': 10, 'min_on_s': 20, 'min_off_s': 20},
        'stale_after_s': 30, 'on_stale': 'safe'}


def _ov(**over):
    return dict(OV, mode='armed', **over)


SHAPES = {
    'ov': _ov(),
    'ov-guard': _ov(signal_valid={'min': 100, 'max': 350, 'max_step': 10}),
    'ov-stale-safe': _ov(on_stale='safe'),
    'ov-stale-60': _ov(on_stale=60),
    'ov-for': _ov(timing={**OV['timing'], 'for_s': 6, 'debounce': 2}),
    'ov-norate': _ov(timing={**OV['timing'], 'min_interval_s': 0, 'debounce': 1}),
    'ov-noopinion': _ov(normal=None),
    'ov-shadow': dict(OV, mode='shadow'),
    'cond': COND,
    'cond-rest': {**COND, 'timing': {**COND['timing'], 'max_on_s': 40, 'max_starts_per_h': 3}},
    'trim': SURPLUS,
    'trim-gain': {**SURPLUS, 'gain': 0.01, 'on_stale': 'hold'},
    'sched': WEEK,
}
WALL0 = 1792465200.0                                  # 2026-10-20 06:00 EEST
# the keys, present but OFF: must be the same as absent
OFF_KEYS = {'timing': {'boot_wait_s': 0, 'urgent_retries': 0}, 'resume_on_boot': False}
ON_KEYS = {'timing': {'boot_wait_s': 60, 'urgent_retries': 3}, 'resume_on_boot': True}


def _with(raw, keys):
    if raw.get('kind') != 'steps':
        return raw
    return {**raw, 'timing': {**raw.get('timing', {}), **keys['timing']}, 'resume_on_boot': keys['resume_on_boot']}


def _bound(mod, raw):
    r = mod.parse_rule_def(raw)
    r.safe_params = {'value': 100, 'revert_s': 0} if r.kind == 'steps' else {'value': 0}
    r.tolerance = 1.0 if r.kind == 'steps' else 0.05
    return r


def _signal(rnd, raw):
    if raw['kind'] == 'condition':
        w = rnd.choice([20, 24.9, 25, 25.1, 30, 34.9, 35, 35.1, 40])
        return (w < 25, w > 35)
    if raw['kind'] == 'trim':
        return rnd.choice([-50, 99, 100, 101, 300, 499, 500, 501, 900])
    if raw['kind'] == 'schedule':
        return rnd.choice([True, True, False, None])
    pts = [s['at'] for s in raw['steps']] + [raw['release_below']]
    v = rnd.choice(pts) + rnd.choice([-0.2, -0.1, -0.01, 0, 0.01, 0.1, 0.2, -3, 3])
    if rnd.random() < 0.02:
        v += 25                                       # a one-sample artefact
    if rnd.random() < 0.01:
        return None
    return round(v, 2)


def _drive(seed, raw_a, raw_b, mod_a, mod_b, ticks=240, fail=True):
    """Run two engines on the same random script, each against its own
    device; yield each tick's pair. The script never depends on what either
    engine decided, so both see exactly the same inputs."""
    rnd = random.Random(seed)
    a, b = mod_a.RuleState(_bound(mod_a, raw_a)), mod_b.RuleState(_bound(mod_b, raw_b))
    t, sample_ts = 0.0, 0.0
    held = [rnd.choice([None, 100, 80, 70, 60, 50, 93])] * 2
    sig = _signal(rnd, raw_a)
    for _i in range(ticks):
        t += rnd.choice([1.0, 2.0, 2.0, 2.0, 5.0, 30.0])
        if rnd.random() < 0.6:                        # a new poll, else the same reading again
            sig, sample_ts = _signal(rnd, raw_a), t - 0.5
        age = None if sig is None else max(0.0, t - sample_ts)
        r = rnd.random()
        a_age = 999.0 if r > 0.9 else rnd.choice([0.5, 5.0, 30.0])
        x = rnd.random()
        y = rnd.random()                              # how a send this tick ends
        cmax, cfor, fval = rnd.choice([60, 70, 90]), rnd.choice([10, 100]), rnd.choice([0, 1, 50, 80])
        for st in (a, b):                              # the same knobs on both
            if x < 0.02:
                st.set_clamp(cmax, WALL0 + t + cfor)
            elif x < 0.03:
                st.clear_clamp()
            elif x < 0.035:
                st.force({'value': fval}, WALL0 + t + 20)
            elif x < 0.04:
                st.paused_until = WALL0 + t + 15
            elif x < 0.045:
                st.restore_counters(st.saved_counters(), st.day)
                st.starts_last_h(t)
        out = []
        for i, st in enumerate((a, b)):
            actual = None if r < 0.15 else held[i]
            out.append(st.evaluate(t, sig, age, actual=actual, actual_age_s=a_age, actual_stale_s=60,
                                   sample_ts=sample_ts, wall=WALL0 + t))
        yield a, b, out[0], out[1], t
        # the runtime's answer to a send: written, failed, or queued behind
        # the device's last command
        for i, (st, d) in enumerate(((a, out[0]), (b, out[1]))):
            if d.action not in ('run', 'reassert'):
                continue
            if fail and y < 0.15:
                st.pending = True
                st.note_result(False)
            elif fail and y < 0.2:
                st.pending = True
            else:
                held[i] = (d.want or {}).get(st.rule.param, held[i])
                st.note_result(True)


def _same(a, b, da, db, t):
    assert da.to_dict() == db.to_dict()
    assert a.snapshot() == b.snapshot()
    assert a.export_mirror(t) == b.export_mirror(t)


@pytest.mark.parametrize('shape', sorted(SHAPES))
@pytest.mark.parametrize('keys', ['absent', 'present-off'])
def test_without_the_keys_the_engine_is_the_baseline(shape, keys):
    raw = SHAPES[shape]
    raw_b = raw if keys == 'absent' else _with(raw, OFF_KEYS)
    for seed in range(40):
        for a, b, da, db, t in _drive(seed, raw, raw_b, base, new):
            _same(a, b, da, db, t)
            if isinstance(b, new.RuleState):
                assert b.rearm_urgent() is False       # off: never re-arms


def _mutate(rnd, raw):
    """A random edit of a rule as a hand or an old UI could write it."""
    raw = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in raw.items()}
    junk = [None, True, False, 0, -1, 1.5, 1e9, float('nan'), '', 'x', [], {}, [1], {'a': 1}, '60']
    for _ in range(rnd.randint(1, 4)):
        k = rnd.choice(list(raw) + ['timing', 'normal', 'release_below', 'on_stale', 'layer', 'priority', 'off_value'])
        if k == 'timing' and isinstance(raw.get('timing'), dict) and rnd.random() < 0.7:
            tk = rnd.choice(['every_s', 'debounce', 'min_interval_s', 'reassert_s', 'for_s', 'max_on_s'])
            raw['timing'] = {**raw['timing'], tk: rnd.choice(junk + [2, 30, 120])}
        else:
            raw[k] = rnd.choice(junk)
    return raw


def _outcome(fn):
    try:
        return ('ok', fn())
    except Exception as e:                              # noqa: BLE001 — compared, not hidden
        return ('raises', type(e).__name__)


@pytest.mark.parametrize('shape', sorted(SHAPES))
def test_parsing_and_validation_are_the_baseline_without_the_keys(shape):
    """to_dict() and validate_rule_def() on random hand edits give the same
    answers as the baseline — the same refusals, and the same exceptions on
    the inputs the baseline could not read (FINDINGS RL-LOAD), except a
    non-mapping `timing`, which now answers instead of raising."""
    rnd = random.Random(shape)
    for _ in range(400):
        raw = _mutate(rnd, SHAPES[shape])
        bad_timing = 'timing' in raw and raw['timing'] and not isinstance(raw['timing'], dict)
        for fn in ('validate', 'to_dict'):
            def call(mod):
                return mod.validate_rule_def(raw) if fn == 'validate' else mod.parse_rule_def(raw).to_dict()
            want, got = _outcome(lambda: call(base)), _outcome(lambda: call(new))
            if bad_timing and want[0] == 'raises':
                continue                                 # fixed there; the input may still be unreadable further on
            assert got == want, (fn, raw)


def test_the_helpers_the_engine_imports_are_unchanged():
    # the baseline imports the live clock module: a change there would hide
    # behind both sides — pin that rules.py still uses the same one
    assert base.clock is new.clock


# ── keys ON: pointwise dominance (B1, §1.7) ──────────────────────────────────

def _level(st):
    if st.state in ('normal', 'stale'):
        return -1
    labels = [s.label for s in st.rule.steps]
    return labels.index(st.state) if st.state in labels else -1


@pytest.mark.parametrize('shape', ['ov', 'ov-guard', 'ov-stale-safe', 'ov-for', 'ov-norate'])
def test_with_the_keys_the_rule_is_never_less_protective(shape):
    """Every write goes through (L = 0, §1.7): at every tick the rule with the
    keys is at least as far up the steps, and the limit last written to its
    device (or what the device held, before any write) is never higher."""
    raw = SHAPES[shape]
    for seed in range(60):
        wire = None
        for a, b, da, db, t in _drive(1000 + seed, raw, _with(raw, ON_KEYS), new, new, fail=False):
            if wire is None:                          # what the devices held before any write
                wire = [da.actual, db.actual]
            for i, d in enumerate((da, db)):
                if d.action in ('run', 'reassert'):
                    wire[i] = d.want['value']
            if a.state == 'stale' or b.state == 'stale' or a.forced or b.forced:
                continue
            assert _level(b) >= _level(a), (seed, t, a.state, b.state)
            if wire[0] is not None and wire[1] is not None:
                assert wire[1] <= wire[0], (seed, t, wire)
