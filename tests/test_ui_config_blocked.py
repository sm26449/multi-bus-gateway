# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""A save while config.yaml is blocked answers 409 with
``detail.config_blocked`` (QA-D-11, review m2). The UI has 409 branches that
mean something else — "in use", "a template with that id exists, overwrite?".
Each must let the blocked answer through to its words, not read it as its
own case. A branch is fine when it checks ``config_blocked``, or when it only
acts on a key the blocked answer never carries (``dependents``) or shows the
answer's errors as they are (the installation editor's ``onRefused``)."""
import pathlib
import re

UI = pathlib.Path(__file__).resolve().parent.parent / "ui" / "js"
# branches that show the errors as they are, or need a key the blocked answer lacks
_SAFE = re.compile(r"config_blocked|dependents|onRefused")


def test_every_409_branch_of_the_ui_lets_a_blocked_config_through():
    paths = sorted(UI.glob("*.js"))
    assert paths
    bad = []
    for p in paths:
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"status\s*===\s*409", line) and not _SAFE.search(line):
                bad.append(f"{p.name}:{n}: {line.strip()}")
    assert not bad, "409 read as its own case, a blocked config.yaml included:\n" + "\n".join(bad)


def test_the_blocked_answer_carries_the_marker():
    src = (UI.parent.parent / "multibus" / "api.py").read_text(encoding="utf-8")
    assert '"config_blocked": True' in src
