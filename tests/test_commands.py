# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The command engine (docs/commands-design.md): expressions, encoding through
the template, frames, and the five verdicts — on the Fronius preset, against a
fake inverter that answers model 123 the way a real one did on 2026-09-13."""
import pytest

from multibus.commands import (ExprError, evaluate, normalize_params, parse_command_def,
                               parse_payload, plan_frames, run_command)
from multibus.device_template import TemplateRegistry
from multibus.encoder import RegisterEncoder


@pytest.fixture(scope='module')
def fronius():
    t = TemplateRegistry().get('fronius_sunspec_inverter')
    return t, {r.name: r for r in t.registers}


def _cmd(t, name='power_limit'):
    return parse_command_def(name, t.commands[name])


class _Inverter:
    """Model 123 at 40227 (0-based) as read live: header 123, Conn, WMaxLimPct
    raw with SF, …, WMaxLim_Ena, SF at +23."""

    def __init__(self, sf=-2, limit_raw=10000, ena=0, model_id=123, write_ok=True,
                 answer_after=True, garbage_first=False):
        self.mem = {40227 + i: 0 for i in range(26)}
        self.mem[40227], self.mem[40228] = model_id, 24
        self.mem[40231] = 1
        self.mem[40232] = limit_raw
        self.mem[40236] = ena
        self.mem[40250] = sf & 0xFFFF
        self.write_ok, self.answer_after, self.garbage_first = write_ok, answer_after, garbage_first
        self.writes, self.reads = [], 0

    def read_registers(self, address, count, register_type='holding'):
        self.reads += 1
        if self.garbage_first and self.reads == 1:
            return [0xBEEF] * count
        if self.writes and not self.answer_after:
            return None
        return [self.mem.get(address + i, 0) for i in range(count)]

    def write(self, address, register_type='holding', values=None, **kw):
        self.writes.append((address, list(values)))
        if not self.write_ok:
            return False, 'timeout'
        for i, v in enumerate(values):
            self.mem[address + i] = v
        return True, None


# ── expressions ──────────────────────────────────────────────────────────────

def test_expressions_are_tiny_on_purpose():
    p = {'value': 60, 'revert_s': 600}
    assert evaluate(0, p) == 0 and evaluate('${value}', p) == 60
    assert evaluate({'if': '${value} < 100', 'then': 1, 'else': 0}, p) == 1
    assert evaluate({'if': '${value} >= 100', 'then': 1, 'else': 0}, p) == 0
    assert evaluate({'*': ['${value}', 10]}, p) == 600.0
    assert evaluate({'/': ['${revert_s}', 60]}, p) == 10.0
    with pytest.raises(ExprError):
        evaluate('${missing}', p)
    with pytest.raises(ExprError):
        evaluate({'eval': 'x'}, p)
    with pytest.raises(ExprError):
        evaluate({'*': [1, 2, 3]}, p)


def test_params_get_defaults_aliases_and_bounds(fronius):
    t, _ = fronius
    cmd = _cmd(t)
    assert normalize_params(cmd, {'value': 60}) == {'value': 60, 'revert_s': 600, 'ramp_s': 0}
    # the legacy collector's names still land
    assert normalize_params(cmd, {'limit_pct': 70, 'revert_timeout': 120, 'ramp_time': 3}) == \
        {'value': 70, 'revert_s': 120, 'ramp_s': 3}
    for bad in ({}, {'value': 101}, {'value': -1}, {'value': 'x'}, {'value': 50, 'revert_s': 70000}):
        with pytest.raises(ValueError):
            normalize_params(cmd, bad)


def test_a_payload_is_a_number_or_an_object(fronius):
    t, _ = fronius
    cmd = _cmd(t)
    assert parse_payload(cmd, '60') == {'value': 60.0}
    assert parse_payload(cmd, b'{"limit_pct": 70, "revert_timeout": 120, "source": "ov"}') == \
        {'limit_pct': 70, 'revert_timeout': 120, 'source': 'ov'}
    for bad in ('abc', '[1]', 'true'):
        with pytest.raises(ValueError):
            parse_payload(cmd, bad)


# ── encoding and frames ──────────────────────────────────────────────────────

def test_the_five_registers_become_one_frame_scaled_by_the_live_sf(fronius):
    t, regs = fronius
    cmd = _cmd(t)
    frames = plan_frames(cmd, normalize_params(cmd, {'value': 60, 'revert_s': 300, 'ramp_s': 5}),
                         regs, RegisterEncoder('big'), {'wmaxlimpct_sf': -2})
    assert len(frames) == 1
    assert frames[0]['address'] == 40232 and frames[0]['words'] == [6000, 0, 300, 5, 1]
    assert frames[0]['registers'] == ['power_limit_pct', 'power_limit_win_s', 'power_limit_revert_s',
                                      'power_limit_ramp_s', 'power_limit_enabled']
    # the scale factor decides the raw value
    assert plan_frames(cmd, normalize_params(cmd, {'value': 60}), regs, RegisterEncoder('big'),
                       {'wmaxlimpct_sf': -1})[0]['words'][0] == 600
    assert plan_frames(cmd, normalize_params(cmd, {'value': 60}), regs, RegisterEncoder('big'),
                       {'wmaxlimpct_sf': 0})[0]['words'][0] == 60
    with pytest.raises(ValueError):
        plan_frames(cmd, normalize_params(cmd, {'value': 60}), regs, RegisterEncoder('big'), {})


def test_restore_clears_the_enable_bit(fronius):
    t, regs = fronius
    cmd = _cmd(t)
    fr = plan_frames(cmd, normalize_params(cmd, {'value': 100, 'revert_s': 0}), regs,
                     RegisterEncoder('big'), {'wmaxlimpct_sf': -2})
    assert fr[0]['words'] == [10000, 0, 0, 0, 0]


# ── the run ──────────────────────────────────────────────────────────────────

def test_a_limit_is_guarded_written_settled_and_read_back(fronius):
    t, regs = fronius
    inv = _Inverter(sf=-2)
    r = run_command(_cmd(t), {'value': 60, 'revert_s': 600, 'ramp_s': 0}, inv, regs, sleep=lambda s: None)
    assert r['status'] == 'success', r
    assert inv.writes == [(40232, [6000, 0, 600, 0, 1])]
    assert r['guard'] == {'controls_model_id': 123, 'wmaxlimpct_sf': -2}
    assert r['before']['power_limit_pct'] == 100 and r['after']['power_limit_pct'] == 60
    assert r['after']['power_limit_enabled'] == 1 and r['frames_sent'] == 1


def test_the_guard_refuses_a_wrong_block_and_a_wrong_scale_factor(fronius):
    t, regs = fronius
    bad = _Inverter(model_id=7)
    r = run_command(_cmd(t), {'value': 50, 'revert_s': 600, 'ramp_s': 0}, bad, regs, sleep=lambda s: None)
    assert r['status'] == 'rejected' and 'controls_model_id' in r['reason'] and bad.writes == []
    odd = _Inverter(sf=3)
    r = run_command(_cmd(t), {'value': 50, 'revert_s': 600, 'ramp_s': 0}, odd, regs, sleep=lambda s: None)
    assert r['status'] == 'rejected' and 'wmaxlimpct_sf' in r['reason'] and odd.writes == []
    # one garbage answer is retried once, not fatal
    flaky = _Inverter(garbage_first=True)
    assert run_command(_cmd(t), {'value': 50, 'revert_s': 600, 'ramp_s': 0}, flaky, regs,
                       sleep=lambda s: None)['status'] == 'success'


def test_write_failure_silent_read_back_and_mismatch_are_told_apart(fronius):
    t, regs = fronius
    p = {'value': 50, 'revert_s': 600, 'ramp_s': 0}
    r = run_command(_cmd(t), p, _Inverter(write_ok=False), regs, sleep=lambda s: None)
    assert r['status'] == 'error' and r['frames_sent'] == 0 and 'nothing went out' in r['reason']
    r = run_command(_cmd(t), p, _Inverter(answer_after=False), regs, sleep=lambda s: None)
    assert r['status'] == 'unverified'

    class _Stubborn(_Inverter):
        def write(self, address, register_type='holding', values=None, **kw):
            self.writes.append((address, list(values)))
            self.mem[40232], self.mem[40236] = 8000, 1
            return True, None
    r = run_command(_cmd(t), p, _Stubborn(), regs, sleep=lambda s: None)
    assert r['status'] == 'mismatch' and r['after']['power_limit_pct'] == 80


def test_a_dry_run_shows_the_frames_and_touches_nothing(fronius):
    t, regs = fronius
    inv = _Inverter()
    r = run_command(_cmd(t), {'value': 40, 'revert_s': 600, 'ramp_s': 0}, inv, regs, dry_run=True)
    assert r['status'] == 'dry_run' and r['frames'][0]['words'] == [4000, 0, 600, 0, 1]
    assert inv.writes == []
