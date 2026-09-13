# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The active power limit (SunSpec model 123) — the sequence, not the policy."""
import pytest

from multibus.power_limit import (MODEL_BASE, MODEL_LEN, WRITE_BASE, apply_power_limit,
                                  parse_command, read_controls)


class _Inverter:
    """A model-123 block as a Fronius answers it, with a write that lands."""

    def __init__(self, sf=-2, limit_raw=10000, ena=0, model_id=123, garbage_first=False,
                 write_ok=True, answer_after=True):
        self.regs = [0] * MODEL_LEN
        self.regs[0], self.regs[1] = model_id, 24
        self.regs[4] = 1                       # Conn
        self.regs[5] = limit_raw
        self.regs[9] = ena
        self.regs[23] = sf & 0xFFFF
        self.garbage_first = garbage_first
        self.write_ok = write_ok
        self.answer_after = answer_after
        self.writes = []
        self.reads = 0

    def read_registers(self, address, count, register_type='holding'):
        self.reads += 1
        if self.garbage_first and self.reads == 1:
            return [0xBEEF] * count            # datalogger buffer garbage
        if self.writes and not self.answer_after:
            return None
        assert address == MODEL_BASE and count == MODEL_LEN
        return list(self.regs)

    def write(self, address, register_type='holding', values=None, **kw):
        self.writes.append((address, list(values)))
        if not self.write_ok:
            return False, 'timeout'
        self.regs[5:10] = values
        return True, None


def test_the_block_sits_where_the_datalogger_puts_it():
    """Read live on 2026-09-13 from a Fronius DataManager (int+SF map), 0-based:
    40227 = [123, 24, 0, 0, Conn=1, WMaxLimPct=10000, 0, 0, 0, Ena=0, OutPFSet=1000,
    …, SF(-2) at +23]. The legacy collector spoke of 40228: its client counted
    from one. A limit written one register off would land in WinTms."""
    assert MODEL_BASE == 40227 and WRITE_BASE == 40232
    live = [123, 24, 0, 0, 1, 10000, 0, 0, 0, 0, 1000, 0, 0, 0, 0, 0, 0, 32768, 0, 0, 0, 2, 0, 65534, 65533, 0]
    class _Live:
        def read_registers(self, a, n, register_type='holding'):
            assert a == 40227 and n == 26
            return live
    assert read_controls(_Live()) == {'sf': -2, 'limit_pct': 100.0, 'enabled': False,
                                      'connected': True, 'revert_s': 0, 'ramp_s': 0}


def test_read_controls_decodes_the_block():
    inv = _Inverter(sf=-2, limit_raw=6000, ena=1)
    c = read_controls(inv)
    assert c == {'sf': -2, 'limit_pct': 60.0, 'enabled': True, 'connected': True,
                 'revert_s': 0, 'ramp_s': 0}
    assert read_controls(_Inverter(model_id=124)) is None


def test_a_limit_is_one_write_of_five_registers_then_read_back():
    inv = _Inverter(sf=-2)
    r = apply_power_limit(inv, 60, revert_s=600, ramp_s=5, sleep=lambda s: None)
    assert r['status'] == 'success', r
    assert inv.writes == [(WRITE_BASE, [6000, 0, 600, 5, 1])]     # raw = 60 / 10^-2
    assert r['before_pct'] == 100.0 and r['after_pct'] == 60.0 and r['enabled'] is True
    assert r['sf'] == -2 and r['ms'] >= 0


def test_raw_follows_the_scale_factor():
    inv1 = _Inverter(sf=-1); apply_power_limit(inv1, 60, sleep=lambda s: None)
    inv0 = _Inverter(sf=0); apply_power_limit(inv0, 60, sleep=lambda s: None)
    assert inv1.writes[0][1][0] == 600 and inv0.writes[0][1][0] == 60


def test_restoring_to_100_clears_the_enable_bit():
    inv = _Inverter(sf=-2, limit_raw=6000, ena=1)
    r = apply_power_limit(inv, 100, sleep=lambda s: None)
    assert r['status'] == 'success' and inv.writes[0][1] == [10000, 0, 600, 0, 0]
    assert r['enabled'] is False


def test_garbage_header_is_never_written_to():
    bad = _Inverter(model_id=7)
    r = apply_power_limit(bad, 50, sleep=lambda s: None)
    assert r['status'] == 'error' and 'not 123' in r['reason'] and bad.writes == []
    # one transient garbage answer is retried, not fatal
    flaky = _Inverter(garbage_first=True)
    assert apply_power_limit(flaky, 50, sleep=lambda s: None)['status'] == 'success'


def test_an_implausible_scale_factor_is_refused():
    inv = _Inverter(sf=3)
    r = apply_power_limit(inv, 50, sleep=lambda s: None)
    assert r['status'] == 'rejected' and 'implausible' in r['reason'] and inv.writes == []
    # …and so is a plausible one that differs from the last good one
    inv2 = _Inverter(sf=-1)
    r2 = apply_power_limit(inv2, 50, last_good_sf=-2, sleep=lambda s: None)
    assert r2['status'] == 'rejected' and inv2.writes == []


def test_out_of_range_never_touches_the_wire():
    inv = _Inverter()
    assert apply_power_limit(inv, 101, sleep=lambda s: None)['status'] == 'rejected'
    assert apply_power_limit(inv, -1, sleep=lambda s: None)['status'] == 'rejected'
    assert apply_power_limit(inv, 'x', sleep=lambda s: None)['status'] == 'rejected'
    assert inv.writes == []


def test_write_failure_and_silent_read_back_are_told_apart():
    assert apply_power_limit(_Inverter(write_ok=False), 50, sleep=lambda s: None)['status'] == 'error'
    r = apply_power_limit(_Inverter(answer_after=False), 50, sleep=lambda s: None)
    assert r['status'] == 'unverified'


def test_a_mismatching_read_back_is_a_mismatch():
    class _Stubborn(_Inverter):
        def write(self, address, register_type='holding', values=None, **kw):
            self.writes.append((address, list(values)))
            self.regs[5] = 8000; self.regs[9] = 1        # the inverter clamps
            return True, None
    r = apply_power_limit(_Stubborn(), 50, sleep=lambda s: None)
    assert r['status'] == 'mismatch' and r['after_pct'] == 80.0


def test_commands_arrive_as_a_number_or_an_object_old_or_new_names():
    assert parse_command('60') == {'limit_pct': 60.0, 'revert_s': 600, 'ramp_s': 0, 'source': 'mqtt'}
    assert parse_command(b'{"limit_pct": 70, "revert_s": 120, "ramp_s": 3, "source": "ov"}') == \
        {'limit_pct': 70.0, 'revert_s': 120, 'ramp_s': 3, 'source': 'ov'}
    # the legacy collector's names still work — a controller only re-points its topics
    assert parse_command('{"limit_pct": 80, "revert_timeout": 300, "ramp_time": 2}') == \
        {'limit_pct': 80.0, 'revert_s': 300, 'ramp_s': 2, 'source': 'mqtt'}
    for bad in ('', 'abc', '{"revert_s": 5}', '[1]'):
        with pytest.raises(ValueError):
            parse_command(bad)
