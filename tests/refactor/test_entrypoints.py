# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""S0 check (P0.2): the real entry points still start after the refactor.

A ``sys.modules`` shim does not run under ``python -m`` and does not fix a
broken top-level import in main.py, so these are checked as the user runs them
(subprocess), plus an in-process /health probe of the composed app.
"""
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

import multibus

REPO = Path(__file__).resolve().parents[2]

try:
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False


def _run(args, timeout=60, **kw):
    return subprocess.run([sys.executable, *args], cwd=REPO,
                          capture_output=True, text=True, timeout=timeout, **kw)


def test_version_is_a_literal_and_import_is_light():
    """import multibus must not drag in pymodbus/paho/influx (no re-exports)."""
    # not -I: isolated mode drops cwd from sys.path, so multibus (run from the
    # repo tree, not pip-installed) would not import. cwd=REPO keeps it findable.
    r = _run(["-c",
              "import sys, multibus; "
              "print(multibus.__version__); "
              "assert 'pymodbus' not in sys.modules; "
              "assert 'paho' not in sys.modules; "
              "assert 'influxdb_client' not in sys.modules"])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == multibus.__version__


def test_backfill_module_entrypoint():
    r = _run(["-m", "multibus.backfill", "--help"], timeout=60)
    assert r.returncode == 0, r.stderr
    assert "usage" in (r.stdout + r.stderr).lower()


def test_main_imports_resolve():
    """python main.py --help exercises main.py's top-level import graph."""
    r = _run(["main.py", "--help"], timeout=60)
    assert r.returncode == 0, r.stderr
    assert "usage" in (r.stdout + r.stderr).lower()


@pytest.mark.skipif(not _HAS_TC, reason="fastapi TestClient not installed")
def test_health_endpoint_from_composed_app(tmp_path):
    from multibus.api import create_api
    from multibus.config import Config
    from tests.refactor.test_route_snapshot import _Pub
    cfg = Config(str(tmp_path / "config.yaml"))
    app, _ = create_api(cfg, None, _Pub(), None,
                        devices=[(d, None) for d in cfg.devices])
    with TestClient(app, raise_server_exceptions=False) as cl:
        resp = cl.get("/health")
    assert resp.status_code == 200, resp.text


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_main_boots_and_serves_health(tmp_path):
    """Boot the real process (python main.py -c ...) with no devices and hit
    /health, then stop it. Covers GatewayApp.setup()/start() wiring that the
    in-process TestClient probe skips."""
    port = _free_port()
    cfg = tmp_path / "config.yaml"  # absent -> fresh, no devices; writes stay in tmp
    proc = subprocess.Popen(
        [sys.executable, "main.py", "-c", str(cfg),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 40
        last = None
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"main.py exited early rc={proc.returncode}\n{out}")
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    if r.status == 200:
                        json.loads(r.read().decode() or "{}")
                        break
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.5)
        else:
            pytest.fail(f"/health never came up on :{port} ({last})")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
