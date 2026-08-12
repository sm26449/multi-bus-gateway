# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Canonical field names — the SINGLE SOURCE OF TRUTH for register naming.

A register's ``name`` becomes its MQTT topic leaf, its InfluxDB field, and its
``name`` tag. If every template names the same physical quantity the same way,
MQTT topics and InfluxDB fields are uniform across ALL devices — an automation
reads ``voltage_l1_n`` everywhere instead of guessing ``ull_0`` on one meter and
``v_l1`` on another.

Convention: ``<quantity>_<position>`` — flat, lowercase, self-documenting.
  position: l1 / l2 / l3 (phase), l1_n… (phase-to-neutral), l1_l2… (line-to-line),
            ln_avg / ll_avg (system average), total (sum), n (neutral).

Templates SHOULD name their registers from this list; ``non_canonical()`` flags
the ones that don't (a warning, not an error — vendor maps like the Janitza
reference predate this and are allowed to differ).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# canonical name -> (influx measurement, typical unit, human description)
CANONICAL_FIELDS: Dict[str, Tuple[str, str, str]] = {
    # ── Voltage (measurement: voltage) ────────────────────────────────────────
    'voltage_l1_n':  ('voltage', 'V', 'L1–N RMS voltage'),
    'voltage_l2_n':  ('voltage', 'V', 'L2–N RMS voltage'),
    'voltage_l3_n':  ('voltage', 'V', 'L3–N RMS voltage'),
    'voltage_ln_avg':('voltage', 'V', 'Average phase-to-neutral voltage'),
    'voltage_l1_l2': ('voltage', 'V', 'L1–L2 line voltage'),
    'voltage_l2_l3': ('voltage', 'V', 'L2–L3 line voltage'),
    'voltage_l3_l1': ('voltage', 'V', 'L3–L1 line voltage'),
    'voltage_ll_avg':('voltage', 'V', 'Average line-to-line voltage'),
    # ── Current (measurement: current) ────────────────────────────────────────
    'current_l1':    ('current', 'A', 'L1 current'),
    'current_l2':    ('current', 'A', 'L2 current'),
    'current_l3':    ('current', 'A', 'L3 current'),
    'current_n':     ('current', 'A', 'Neutral current'),
    'current_total': ('current', 'A', 'Total / sum current'),
    # ── Active power (measurement: power_active) ──────────────────────────────
    'power_active_l1':    ('power_active', 'W', 'L1 active power'),
    'power_active_l2':    ('power_active', 'W', 'L2 active power'),
    'power_active_l3':    ('power_active', 'W', 'L3 active power'),
    'power_active_total': ('power_active', 'W', 'Total active power'),
    # ── Reactive power (measurement: power_reactive) ──────────────────────────
    'power_reactive_l1':    ('power_reactive', 'var', 'L1 reactive power'),
    'power_reactive_l2':    ('power_reactive', 'var', 'L2 reactive power'),
    'power_reactive_l3':    ('power_reactive', 'var', 'L3 reactive power'),
    'power_reactive_total': ('power_reactive', 'var', 'Total reactive power'),
    # ── Apparent power (measurement: power_apparent) ──────────────────────────
    'power_apparent_l1':    ('power_apparent', 'VA', 'L1 apparent power'),
    'power_apparent_l2':    ('power_apparent', 'VA', 'L2 apparent power'),
    'power_apparent_l3':    ('power_apparent', 'VA', 'L3 apparent power'),
    'power_apparent_total': ('power_apparent', 'VA', 'Total apparent power'),
    # ── Power factor (measurement: power_factor) ──────────────────────────────
    'power_factor_l1':    ('power_factor', '', 'L1 power factor'),
    'power_factor_l2':    ('power_factor', '', 'L2 power factor'),
    'power_factor_l3':    ('power_factor', '', 'L3 power factor'),
    'power_factor_total': ('power_factor', '', 'System power factor'),
    # ── Frequency (measurement: frequency) ────────────────────────────────────
    'frequency': ('frequency', 'Hz', 'Grid frequency'),
    # ── Active energy (measurement: energy_active) ────────────────────────────
    'energy_active_import':    ('energy_active', 'kWh', 'Total imported active energy'),
    'energy_active_export':    ('energy_active', 'kWh', 'Total exported active energy'),
    'energy_active_net':       ('energy_active', 'kWh', 'Net active energy (import − export)'),
    'energy_active_import_l1': ('energy_active', 'kWh', 'L1 imported active energy'),
    'energy_active_import_l2': ('energy_active', 'kWh', 'L2 imported active energy'),
    'energy_active_import_l3': ('energy_active', 'kWh', 'L3 imported active energy'),
    'energy_active_export_l1': ('energy_active', 'kWh', 'L1 exported active energy'),
    'energy_active_export_l2': ('energy_active', 'kWh', 'L2 exported active energy'),
    'energy_active_export_l3': ('energy_active', 'kWh', 'L3 exported active energy'),
    # ── Reactive / apparent energy ────────────────────────────────────────────
    'energy_reactive_import': ('energy_reactive', 'kvarh', 'Total imported reactive energy'),
    'energy_reactive_export': ('energy_reactive', 'kvarh', 'Total exported reactive energy'),
    'energy_apparent':        ('energy_apparent', 'kVAh', 'Total apparent energy'),
    # ── THD (measurement: thd) ────────────────────────────────────────────────
    'thd_voltage_l1': ('thd', '%', 'L1 voltage THD'),
    'thd_voltage_l2': ('thd', '%', 'L2 voltage THD'),
    'thd_voltage_l3': ('thd', '%', 'L3 voltage THD'),
    'thd_current_l1': ('thd', '%', 'L1 current THD'),
    'thd_current_l2': ('thd', '%', 'L2 current THD'),
    'thd_current_l3': ('thd', '%', 'L3 current THD'),
    # ── Diagnostics (measurement: diagnostic) ─────────────────────────────────
    'model_id':     ('diagnostic', '', 'Meter model identification code'),
    'firmware_rev': ('diagnostic', '', 'Firmware / revision code'),
    'serial':       ('diagnostic', '', 'Meter serial / identification'),
    'temperature':  ('diagnostic', '°C', 'Internal temperature'),
    'uptime':       ('diagnostic', 's', 'Meter uptime'),
}

CANONICAL_NAMES = frozenset(CANONICAL_FIELDS)


def is_canonical(name: str) -> bool:
    """True if ``name`` is a canonical field name."""
    return str(name).lower() in CANONICAL_NAMES


def suggest(name: str) -> Optional[str]:
    """Closest canonical name to ``name`` (for 'did you mean …?'), or None."""
    import difflib
    m = difflib.get_close_matches(str(name).lower(), CANONICAL_NAMES, n=1, cutoff=0.6)
    return m[0] if m else None


def non_canonical(names: List[str]) -> List[str]:
    """The subset of ``names`` that are not canonical."""
    return [n for n in names if not is_canonical(n)]
