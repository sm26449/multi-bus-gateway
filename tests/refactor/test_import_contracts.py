# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""S0 check (P0.3, ADR-0034): the package layer contract holds.

Runs import-linter against .importlinter. On the flat tree the contract is
near-vacuous by design (packages are optional until their move step creates
them); it grows teeth as each layer lands, and any NEW upward import that is not
in ignore_imports then fails here. Skipped only if import-linter is absent (so a
not-yet-rebuilt image does not hard-fail collection).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_LINT = shutil.which("lint-imports")


@pytest.mark.skipif(_LINT is None, reason="import-linter not installed (rebuild mbg-test)")
def test_layer_contract_holds():
    r = subprocess.run([_LINT], cwd=REPO, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, (
        "import-linter contract broken:\n" + r.stdout + "\n" + r.stderr)
    assert "0 broken." in r.stdout, r.stdout
