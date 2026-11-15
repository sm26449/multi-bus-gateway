# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""`tools/gate.sh --list` is resolve-only: it prints the test paths a mode would run and confirms
each exists, WITHOUT Docker or running anything. It still fails early (exit 2) on a drifted path or
a bad mode — the point is to validate a recipe cheaply (the §6 plan's dead `test_config_bundle*`
globs are exactly what this catches)."""
import pathlib
import subprocess

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _gate(*args):
    return subprocess.run(["sh", "tools/gate.sh", *args], cwd=_ROOT,
                          capture_output=True, text=True)


def test_list_fast_resolves_and_exits_zero():
    r = _gate("--list", "fast")
    assert r.returncode == 0
    assert "resolve-only" in r.stdout
    assert "tests/test_command_binding.py" in r.stdout      # a named fast path
    assert "tests/characterization/" in r.stdout            # a directory, annotated


def test_list_close_is_the_whole_tests_dir():
    r = _gate("--list", "close")
    assert r.returncode == 0 and "tests/  (directory)" in r.stdout


def test_bad_mode_fails_with_usage():
    r = _gate("--list", "bogus")
    assert r.returncode == 2 and "usage:" in r.stderr


def test_missing_paths_fail_early_without_running():
    # a tree with no tests/ → the fail-closed check fires before any Docker work
    r = _gate("--list", "fast", "/tmp")
    assert r.returncode == 2 and "match no file" in r.stderr
