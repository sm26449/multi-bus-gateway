# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Every e2e script is named by tools/e2e/run_all.sh (E2E-HARNESS-1).

21 scripts added by later lots were never registered, so "the full e2e run"
skipped them without a word. run_all.sh now fails an unregistered script at
the end of a full run; this test says it at once, in the normal suite.
"""
import re
from pathlib import Path

E2E = Path(__file__).resolve().parent.parent / "tools" / "e2e"


def _registered(text: str) -> set:
    names = set(re.findall(r"^\s*[RS]\s+([A-Za-z0-9_]+)\b", text, re.M))
    # `for t in a b \ c; do  R $t …` — the loop's list, line continuations joined
    for body in re.findall(r"^\s*for t in (.*?);\s*do", text.replace("\\\n", " "), re.M | re.S):
        names.update(body.split())
    return names


def test_every_e2e_script_is_registered_in_run_all():
    names = _registered((E2E / "run_all.sh").read_text())
    scripts = {p.stem for p in E2E.glob("*.mjs")}
    missing = sorted(scripts - names)
    assert not missing, f"not in tools/e2e/run_all.sh (add R with its fixture, or S with why): {missing}"


def test_run_all_names_no_script_that_does_not_exist():
    names = _registered((E2E / "run_all.sh").read_text())
    scripts = {p.stem for p in E2E.glob("*.mjs")} | {"cmd_dispatch_live", "can_vcan"}
    assert not sorted(names - scripts), sorted(names - scripts)


def test_the_parser_sees_both_forms():
    text = "R one 1 ''\nS two 'why'\nfor t in three \\\n         four; do\n  R $t 1 ''\ndone\n"
    assert _registered(text) == {"one", "two", "three", "four"}
