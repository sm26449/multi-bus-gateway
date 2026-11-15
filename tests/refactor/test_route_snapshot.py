# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""S0 check (P0.2): the FastAPI route set (method, path, endpoint name) is
frozen by a golden snapshot.

Phase 0 moves modules and (P0.8) extracts ~75 inline ``@app.*`` routes into
router modules. None of that may add, drop, rename or re-verb a route. This
snapshot — built from a fresh (device-less) config so it is deterministic —
catches any such drift. Regenerate deliberately after an intended route change:
``MBG_WRITE_ROUTE_GOLDEN=1 pytest tests/refactor/test_route_snapshot.py``.
"""
import os
from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient  # noqa: F401
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False

from multibus.config import Config

GOLDEN = Path(__file__).resolve().parent / "route_snapshot.txt"


class _Pub:
    """Minimal MQTT publisher surface for create_api (everything is a no-op)."""
    connected = True
    connects = 1

    def __init__(self):
        self.sent, self.topics, self.discovery_hooks = [], {}, []
        self.client = self

    def _publish(self, *a, **k):
        return True

    def register_command(self, topic, fn):
        self.topics[topic] = fn

    def set_command_write_handler(self, fn):
        self.handler = fn

    def __getattr__(self, name):
        return lambda *a, **k: None


def _lines_for(route, prefix: str = "") -> list[str]:
    """One snapshot line per real route, recursing into included routers.

    FastAPI ≥0.139 includes a router LAZILY: ``app.include_router(r)`` appends a
    single opaque ``_IncludedRouter`` wrapper to ``app.routes`` instead of
    copying r's routes in. To keep this guard meaningful across P0.8's inline →
    ``routes/*`` extractions — so a move produces an IDENTICAL snapshot, not an
    opaque blob — we expand each wrapper through ``original_router.routes`` (with
    its include prefix), so every endpoint shows its real method, path and name
    whether it is still inline or now lives in a module."""
    from starlette.routing import Mount, WebSocketRoute
    if type(route).__name__ == "_IncludedRouter":
        pfx = prefix + (getattr(route.include_context, "prefix", "") or "")
        out: list[str] = []
        for sub in route.original_router.routes:
            out.extend(_lines_for(sub, pfx))
        return out
    path = prefix + getattr(route, "path", getattr(route, "path_format", "?"))
    if isinstance(route, Mount):
        return [f"MOUNT {path} {route.name}"]
    if isinstance(route, WebSocketRoute):
        name = getattr(route.endpoint, "__name__", None) or getattr(route, "name", "?")
        return [f"WS {path} {name}"]
    methods = getattr(route, "methods", None)
    ep = getattr(route, "endpoint", None)
    name = getattr(ep, "__name__", None) or getattr(route, "name", "?")
    if methods:
        return [f"{','.join(sorted(methods))} {path} {name}"]
    return [f"{type(route).__name__} {path} {name}"]


def _snapshot(app) -> list[str]:
    lines: list[str] = []
    for r in app.routes:
        lines.extend(_lines_for(r))
    return sorted(lines)


def _build(tmp_path):
    from multibus.api import create_api
    cfg = Config(str(tmp_path / "config.yaml"))  # no file -> fresh, no devices
    pub = _Pub()
    app, _ = create_api(cfg, None, pub, None,
                        devices=[(d, None) for d in cfg.devices])
    return app


@pytest.mark.skipif(not _HAS_TC, reason="fastapi TestClient not installed")
def test_route_set_is_frozen(tmp_path):
    snap = _snapshot(_build(tmp_path))
    if os.environ.get("MBG_WRITE_ROUTE_GOLDEN"):
        GOLDEN.write_text("\n".join(snap) + "\n", encoding="utf-8")
        pytest.skip("regenerated route golden")
    assert GOLDEN.exists(), (
        "route_snapshot.txt missing — regenerate with MBG_WRITE_ROUTE_GOLDEN=1")
    want = GOLDEN.read_text(encoding="utf-8").splitlines()
    assert snap == want, _diff(want, snap)


def _diff(want, got):
    ws, gs = set(want), set(got)
    added = sorted(gs - ws)
    removed = sorted(ws - gs)
    return ("route set changed:\n  + "
            + "\n  + ".join(added or ["(none)"])
            + "\n  - " + "\n  - ".join(removed or ["(none)"]))
