# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Characterization of config LOADER behaviour that the ADR-0047 P1 refactor must preserve (or,
for the reload case, change deliberately). Captured 2026-11-12 on `refactor/config-p1-...`.

Three behaviours not consolidated elsewhere and most at risk in the io.py refactor + clean reload:
  A. warm reload (`load()` on an existing object) — what a section absent from the new file does.
     This is the behaviour clean reload (P1, design §6/H2) will CHANGE; the current state is pinned
     here so the change is deliberate, and the target is a single xfail.
  B. primary-device resolution (ADR-0024): file key > `modbus:` presence > `MODBUS_HOST`.
  C. env-secret shadow: an env-provided secret is used live but is NOT persisted on save.
"""
from __future__ import annotations

import yaml

from multibus.config import Config

_MIN = "mqtt:\n  broker: x\n  enabled: false\n"   # enough to pass the empty/scalar plausibility guard


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# ── A. clean reload: a section absent from the new file RESETS to default (ADR-0047 P1) ──
def test_warm_reload_resets_a_section_absent_from_the_new_file(tmp_path):
    """After ADR-0047 P1 (clean reload), `load()` is total: deleting `security:` from the file turns
    allow_writes back off on reload — a warm reload now equals a cold boot (was the H2 bug)."""
    p = _write(tmp_path, _MIN + "security:\n  allow_writes: true\n")
    cfg = Config(str(p))
    assert cfg.security.allow_writes is True
    _write(tmp_path, _MIN)                       # the new file has no security: section
    cfg.load()                                   # warm reload on the same object
    assert cfg.security.allow_writes is False    # RESET to the cold-boot default (clean reload)


def test_cold_boot_without_security_defaults_to_locked(tmp_path):
    """The cold-boot baseline the warm reload above matches: a fresh Config with no security:
    section is locked (allow_writes False)."""
    cfg = Config(str(_write(tmp_path, _MIN)))
    assert cfg.security.allow_writes is False


def test_reload_equals_cold_boot_for_an_absent_section(tmp_path):
    """Clean reload (ADR-0047 P1): reloading a file that dropped security: equals a cold boot of
    that file (allow_writes back to the default)."""
    p = _write(tmp_path, _MIN + "security:\n  allow_writes: true\n")
    cfg = Config(str(p))
    _write(tmp_path, _MIN)
    cfg.load()
    assert cfg.security.allow_writes is False
    # identical to a fresh Config of the same file
    assert cfg.security.allow_writes is Config(str(_write(tmp_path, _MIN))).security.allow_writes


# ── B. primary-device resolution (ADR-0024) ───────────────────────────────────────────
def _primary(tmp_path, monkeypatch, body, *, modbus_host=None):
    monkeypatch.delenv("MODBUS_HOST", raising=False)
    if modbus_host is not None:
        monkeypatch.setenv("MODBUS_HOST", modbus_host)
    return Config(str(_write(tmp_path, body))).primary_enabled


def test_primary_file_key_wins_over_everything(tmp_path, monkeypatch):
    # explicit true, no modbus section → still primary
    assert _primary(tmp_path, monkeypatch, _MIN + "primary_device: true\n") is True
    # explicit false even with a modbus section → not primary (the file's word wins)
    assert _primary(tmp_path, monkeypatch, _MIN + "primary_device: false\nmodbus:\n  host: 1.2.3.4\n") is False


def test_primary_legacy_from_modbus_section_when_no_key(tmp_path, monkeypatch):
    # no primary_device key, but a modbus: section → legacy install has the primary
    assert _primary(tmp_path, monkeypatch, _MIN + "modbus:\n  host: 1.2.3.4\n") is True


def test_primary_from_modbus_host_env_when_no_key_no_section(tmp_path, monkeypatch):
    assert _primary(tmp_path, monkeypatch, _MIN, modbus_host="5.6.7.8") is True


def test_no_primary_on_a_fresh_file(tmp_path, monkeypatch):
    assert _primary(tmp_path, monkeypatch, _MIN) is False


# ── C. env-secret shadow: used live, NOT persisted on save ─────────────────────────────
def test_env_mqtt_password_is_used_live_but_not_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("MQTT_PASSWORD", "from-env")
    p = _write(tmp_path, "mqtt:\n  broker: x\n  enabled: false\n  password: stored-secret\n")
    cfg = Config(str(p))
    assert cfg.mqtt.password == "from-env"              # effective value is the env override
    cfg.save_yaml_config()
    on_disk = yaml.safe_load(p.read_text())
    assert on_disk["mqtt"]["password"] == "stored-secret"   # the env secret never reaches disk
