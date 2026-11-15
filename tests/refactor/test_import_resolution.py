# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""S0 check (P0.2): every intra-package import resolves — including the lazy
ones (78% of the graph) that pytest only exercises when the function runs.

Runs tools/check_imports.py isolated (``python3 -I``), as a developer does.
A move that leaves a relative import pointing at a module that no longer lives
there fails here, not six weeks later when a Seplos device happens to start.
The two pre-existing CounterFilter bugs (FINDINGS DUP-19) are allow-listed in
the tool; this test passing means no NEW breakage and no stale allow-list entry.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_no_unresolved_intra_package_imports():
    r = subprocess.run(
        [sys.executable, "-I", "tools/check_imports.py", "multibus"],
        cwd=REPO, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (
        "check_imports found new/stale unresolved imports:\n"
        f"{r.stdout}\n{r.stderr}")
    # the summary line proves the allow-list is matched, not bypassed
    assert "0 new" in r.stderr and "0 stale-known" in r.stderr, r.stderr
