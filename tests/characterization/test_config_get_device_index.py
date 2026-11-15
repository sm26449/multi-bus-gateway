# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""§4 C4 / DUP-15 — get_device() now reads an index rebuilt in _build_devices
instead of an O(N) scan per call (PERF-10). This pins that the index returns
exactly what the old ``next(d for d in self.devices if d.id == id)`` scan did,
and that it stays correct across a reload.
"""
from __future__ import annotations

from pathlib import Path

from multibus.config import Config

FIXTURE = Path(__file__).parent / "fixtures" / "s4_config_seam.yaml"


def _load(tmp_path) -> Config:
    dst = tmp_path / "config.yaml"
    dst.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return Config(str(dst))


def _scan(cfg, device_id):
    return next((d for d in cfg.devices if d.id == device_id), None)


def test_get_device_matches_a_linear_scan_for_every_id(tmp_path):
    cfg = _load(tmp_path)
    assert cfg.devices, "fixture should load devices"
    for d in cfg.devices:
        assert cfg.get_device(d.id) is _scan(cfg, d.id) is d


def test_get_device_unknown_is_none(tmp_path):
    cfg = _load(tmp_path)
    assert cfg.get_device("no-such-device") is None


def test_index_survives_a_reload(tmp_path):
    cfg = _load(tmp_path)
    first = cfg.get_device("hp")
    assert first is not None
    cfg.load()                                   # rebuilds self.devices + the index
    again = cfg.get_device("hp")
    assert again is not None and again is _scan(cfg, "hp")
    # the rebuilt index holds the fresh objects, not the stale ones
    assert again is not first
