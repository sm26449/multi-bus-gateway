# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""§4 Config — C1 characterization of the package's observable output across the
layering seams that §4 inverts (ADR-0034; .importlinter #1-16).

The config package currently imports UPWARD into catalog / devices / transports
at ten edges (ignore_imports in .importlinter): canonical units (util), device
kind/type normalization (connection, raw_devices), meter links and device-patch
sinks (build, raw_devices), and the serial-bridge guard (build). C2/C3 invert
those dependencies. This file pins what a representative config.yaml produces
THROUGH those seams — the normalized per-device protocol/kind, the endpoint-
derived devices, the raw bridge list — plus the save→reload round-trip, so the
inversion is provably behaviour-identical.

Coverage note: this fixture exercises the device-kind/device-type and
bridge-guard seams and the endpoint expansion. The canonical-unit, meter-link,
device-patch and user-device-type seams are not exercised here; their
characterization is added as the first step of the lot (C2/C3) that inverts
each, where the seam module is being read anyway.
"""
from __future__ import annotations

from pathlib import Path

from multibus.config import Config

FIXTURE = Path(__file__).parent / "fixtures" / "s4_config_seam.yaml"

# Captured 2026-11-06 on main @ 63cc7b5, pre-C2/C3 (the behaviour to preserve).
EXPECTED_DEVICES = [
    ("hp", "rtu-tcp"),
    ("rs", "rtu"),
    ("asc", "ascii"),
    ("udp", "udp"),
    ("gx", "mqtt"),
    ("gwm", "tcp"),
    ("web", "http"),
    ("gx-battery-288", "mqtt"),
    ("pv-u1", "tcp"),
    ("pv-u2", "tcp"),
    ("pv-u240", "tcp"),
    ("bank-u1", "rtu_tap"),
    ("bank-u2", "rtu_tap"),
]
EXPECTED_RAW_BRIDGES = [
    {"id": "ws", "name": "Panel bridge", "type": "rtu_transparent",
     "host": "127.0.0.1", "ports": [{"port": 15996}]},
    {"id": "gw", "name": "Meter gateway", "type": "modbus_gateway",
     "host": "127.0.0.1", "ports": [{"port": 15997}, {"port": 15998}]},
]


def _projection(cfg):
    """The seam-derived model: ids + normalized protocol/kind/enabled, and the
    raw bridge list. Everything the ten upward edges feed into the model."""
    devs = [{"id": d.id,
             "protocol": getattr(d, "protocol", None),
             "kind": getattr(d, "kind", None),
             "enabled": getattr(d, "enabled", None),
             "meters": getattr(d, "meters", None),
             "meter_options": getattr(d, "meter_options", None)}
            for d in cfg.devices]
    return {"devices": devs, "raw_bridges": getattr(cfg, "_raw_bridges", None)}


def _load(tmp_path) -> Config:
    dst = tmp_path / "config.yaml"
    dst.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return Config(str(dst))


def test_fixture_loads_clean(tmp_path):
    cfg = _load(tmp_path)
    assert getattr(cfg, "_load_failed", False) is False


def test_device_kind_and_type_normalization(tmp_path):
    """connection -> device_kind/device_types and the endpoint expansion:
    each device's normalized protocol (and empty user-kind) is pinned."""
    cfg = _load(tmp_path)
    got = [(d.id, d.protocol) for d in cfg.devices]
    assert got == EXPECTED_DEVICES
    assert all(d.kind == "" for d in cfg.devices)
    assert all(d.enabled is False for d in cfg.devices)


def test_raw_bridges_from_bridge_guard_seam(tmp_path):
    """build -> transports.serial.bridges/bridge_guard: the raw bridge list the
    guard is configured from is preserved verbatim."""
    cfg = _load(tmp_path)
    assert cfg._raw_bridges == EXPECTED_RAW_BRIDGES


def test_no_user_device_types_in_this_fixture(tmp_path):
    cfg = _load(tmp_path)
    assert getattr(cfg, "_raw_device_types", None) == []


def test_save_reload_round_trip_is_projection_identical(tmp_path):
    """Load → save_yaml_config → reload leaves the seam projection unchanged."""
    cfg = _load(tmp_path)
    before = _projection(cfg)
    cfg.save_yaml_config()
    again = Config(str(tmp_path / "config.yaml"))
    assert getattr(again, "_load_failed", False) is False
    assert _projection(again) == before
