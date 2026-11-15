# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The OV two-tree parity decision (tools/ov/compare_day.py): equality is claimed ONLY when both
trees produced a terminal hash record, both exited 0, and the hashes match. The dangerous cases —
a crash that prints only a traceback, two identical error logs, a missing run — must all read as
FAILURE, never as a match."""
import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "compare_day", pathlib.Path(__file__).resolve().parent.parent / "tools" / "ov" / "compare_day.py")
cd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cd)

_OK = '{"variant": "synth-fast", "tuning": "default", "decisions": "aaaa1111", "writes": "bbbb2222"}'
_OK_PREFIX = 'some setup log line\n[tick] whatever\n' + _OK          # the record is the LAST line
_OTHER = '{"variant": "synth-fast", "tuning": "default", "decisions": "cccc3333", "writes": "bbbb2222"}'
_TRACE = 'Traceback (most recent call last):\n  File "x", line 1\nValueError: boom'


def test_identical_hashes_pass():
    ok, reason = cd.verdict("synth-fast", _OK_PREFIX, 0, _OK_PREFIX, 0)
    assert ok is True and "identical" in reason


def test_decisions_mismatch_fails():
    ok, reason = cd.verdict("synth-fast", _OK, 0, _OTHER, 0)
    assert ok is False and "CHANGED" in reason and "decisions" in reason


def test_a_crash_with_no_record_is_not_equality():
    # tree B crashed — only a traceback, no hash line → must FAIL, not claim a match
    ok, reason = cd.verdict("synth-fast", _OK, 0, _TRACE, 1)
    assert ok is False and "no hash record" in reason and "B" in reason


def test_two_identical_error_logs_are_not_equal():
    # the classic false success: both sides crashed identically. No hash record on either side.
    ok, reason = cd.verdict("replay", _TRACE, 1, _TRACE, 1)
    assert ok is False and "no hash record" in reason


def test_nonzero_exit_fails_even_with_a_record():
    # foreign prints a record then exits 1 when a branch was never reached → a failure
    ok, reason = cd.verdict("foreign", _OK, 1, _OK, 0)
    assert ok is False and "nonzero" in reason


def test_parse_record_takes_the_last_json_with_hashes():
    rec = cd.parse_record(_OK_PREFIX)
    assert rec is not None and rec["decisions"] == "aaaa1111"
    assert cd.parse_record(_TRACE) is None               # a traceback has no record
    assert cd.parse_record("") is None                   # empty output has no record


def test_cli_entrypoint(tmp_path):
    a = tmp_path / "a.out"; b = tmp_path / "b.out"
    a.write_text(_OK); b.write_text(_OK)
    assert cd.main(["synth-fast", str(a), "0", str(b), "0"]) == 0
    b.write_text(_OTHER)
    assert cd.main(["synth-fast", str(a), "0", str(b), "0"]) == 1
