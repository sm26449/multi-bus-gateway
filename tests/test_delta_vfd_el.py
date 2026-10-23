# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The Delta VFD-EL map (delta_vfd_el), from the manual (rev. 2016/03, V1.14):
what each command puts on the wire, the guard on Pr 02.00, the frequency
bounds, and how the status word 2101H is decoded."""
import pytest

from multibus import expressions
from multibus.commands import normalize_params, parse_command_def, plan_frames, run_command
from multibus.device_template import TemplateRegistry
from multibus.encoder import RegisterEncoder

CMD, FREQ, FAULT, ERR, STATUS, F_CMD = 0x2000, 0x2001, 0x2002, 0x2100, 0x2101, 0x2102
FREQ_SOURCE = 0x0200
MAX_READ = 1                                                    # the map's protocol.max_registers_per_read


@pytest.fixture(scope="module")
def vfd():
    t = TemplateRegistry().get("delta_vfd_el")
    return t, {r.name: r for r in t.registers}


class _Drive:
    """A VFD-EL as the manual describes it: 2001H is echoed as the frequency
    command at 2102H, a reset clears 2100H."""

    def __init__(self, frequency_source=3, fault=0):
        self.mem = {CMD: 0, FREQ: 0, FAULT: 0, ERR: fault, STATUS: 0, F_CMD: 0, FREQ_SOURCE: frequency_source}
        self.writes, self.reads = [], []

    def read_registers(self, address, count, register_type="holding"):
        self.reads.append((address, count))
        if address < 0x2000 and count > 1:
            return None                                         # one parameter per read (manual p. 108)
        return [self.mem.get(address + i, 0) for i in range(count)]

    def write(self, address, register_type="holding", values=None, **kw):
        self.writes.append((address, list(values)))
        for i, v in enumerate(values):
            self.mem[address + i] = v
        if address <= FREQ < address + len(values):
            self.mem[F_CMD] = self.mem[FREQ]
        if address <= FAULT < address + len(values) and self.mem[FAULT] & 2:
            self.mem[ERR] = 0
        return True, None


def _frames(t, regs, name, params):
    cmd = parse_command_def(name, t.commands[name])
    return plan_frames(cmd, normalize_params(cmd, params), regs, RegisterEncoder("big"), {})


def test_the_drive_is_written_one_register_at_a_time(vfd):
    t, _ = vfd
    assert t.protocol["single_register_writes"] is True        # the VFD-EL has no FC16
    assert t.protocol["max_registers_per_read"] == 1            # one parameter per read


def test_run_stop_and_reverse_are_the_manuals_command_words(vfd):
    t, regs = vfd
    for value, word in ((0, 1), (1, 6), (2, 10)):               # 01 stop · 10 run + 01 FWD · 10 run + 10 REV
        frames = _frames(t, regs, "run", {"value": value})
        assert [(f["address"], f["words"]) for f in frames] == [(CMD, [word])], value


def test_a_lease_that_expires_stops_the_motor(vfd):
    t, _ = vfd
    assert parse_command_def("run", t.commands["run"]).safe == {"value": 0}
    assert t.commands["stop"]["alias"] == {"command": "run", "params": {"value": 0}}


def test_the_frequency_is_written_in_hundredths_and_checked_back(vfd):
    t, regs = vfd
    assert [(f["address"], f["words"]) for f in _frames(t, regs, "set_frequency", {"value": 37.5})] == [(FREQ, [3750])]
    drive = _Drive()
    cmd = parse_command_def("set_frequency", t.commands["set_frequency"])
    res = run_command(cmd, normalize_params(cmd, {"value": 37.5}), drive, regs, max_read=MAX_READ, sleep=lambda s: None)
    assert res["status"] == "success" and drive.writes == [(FREQ, [3750])]
    assert set(drive.reads) == {(FREQ_SOURCE, 1), (F_CMD, 1)}  # the guard and the check: a read each


def test_no_frequency_is_written_unless_the_drive_takes_it_from_rs485(vfd):
    t, regs = vfd
    drive = _Drive(frequency_source=0)                          # keypad: the write would be ignored silently
    cmd = parse_command_def("set_frequency", t.commands["set_frequency"])
    res = run_command(cmd, normalize_params(cmd, {"value": 20}), drive, regs, max_read=MAX_READ, sleep=lambda s: None)
    assert res["status"] == "rejected" and "frequency_source" in res["reason"] and drive.writes == []


def test_a_frequency_above_the_bound_is_refused(vfd):
    t, _ = vfd
    cmd = parse_command_def("set_frequency", t.commands["set_frequency"])
    with pytest.raises(ValueError):
        normalize_params(cmd, {"value": 51})


def test_reset_writes_bit_1_and_checks_the_fault_cleared(vfd):
    t, regs = vfd
    drive = _Drive(fault=1)                                     # oc
    cmd = parse_command_def("reset_fault", t.commands["reset_fault"])
    res = run_command(cmd, normalize_params(cmd, {}), drive, regs, max_read=MAX_READ, sleep=lambda s: None)
    assert res["status"] == "success" and drive.writes == [(FAULT, [2])]


@pytest.mark.parametrize("word, want", [
    (0x0000, dict(run_state=0, running=0, reverse=0, frequency_from_comm=0, command_from_comm=0)),
    (0x0503, dict(run_state=3, running=1, reverse=0, frequency_from_comm=1, command_from_comm=1)),
    (0x051B, dict(run_state=3, running=1, reverse=1, frequency_from_comm=1, command_from_comm=1)),
    (0x0011, dict(run_state=1, running=0, reverse=1, frequency_from_comm=0, command_from_comm=0)),
    (0x0202, dict(run_state=2, running=0, reverse=0, frequency_from_comm=0, command_from_comm=0)),
])
def test_the_status_word_is_decoded_as_the_manual_says(vfd, word, want):
    t, _ = vfd
    got = {c.name: int(expressions.evaluate_tree(expressions.compile_expression(c.expr), lambda _r: word))
           for c in t.calculated if c.name in want}
    assert got == want


def test_run_at_sets_the_frequency_then_starts_and_zero_stops(vfd):
    t, regs = vfd
    drive = _Drive()
    cmd = parse_command_def("run_at", t.commands["run_at"])
    res = run_command(cmd, normalize_params(cmd, {"value": 12.5}), drive, regs, max_read=MAX_READ,
                      sleep=lambda s: None)
    assert res["status"] == "success" and drive.writes == [(FREQ, [1250]), (CMD, [6])]   # frequency first
    drive.writes.clear()
    res = run_command(cmd, normalize_params(cmd, {"value": 0}), drive, regs, max_read=MAX_READ,
                      sleep=lambda s: None)
    assert res["status"] == "success" and drive.writes == [(FREQ, [0]), (CMD, [1])]
    assert cmd.safe == {"value": 0}                                                       # a lease ends in stop
