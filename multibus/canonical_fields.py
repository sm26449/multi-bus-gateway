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

# canonical name -> (influx measurement, typical unit, MQTT topic, human description)
# The MQTT topic is hierarchical (slash-separated) — the shape pv-stack-ui and
# Node-RED already speak on the Janitza reference — while the InfluxDB field is
# the flat snake_case name (the dict key).
CANONICAL_FIELDS: Dict[str, Tuple[str, str, str, str]] = {
    # ── Voltage (measurement: voltage) ────────────────────────────────────────
    'voltage_l1_n':  ('voltage', 'V', 'voltage/l1_n', 'L1–N RMS voltage'),
    'voltage_l2_n':  ('voltage', 'V', 'voltage/l2_n', 'L2–N RMS voltage'),
    'voltage_l3_n':  ('voltage', 'V', 'voltage/l3_n', 'L3–N RMS voltage'),
    'voltage_ln_avg':('voltage', 'V', 'voltage/ln_avg', 'Average phase-to-neutral voltage'),
    'voltage_l1_l2': ('voltage', 'V', 'voltage/l1_l2', 'L1–L2 line voltage'),
    'voltage_l2_l3': ('voltage', 'V', 'voltage/l2_l3', 'L2–L3 line voltage'),
    'voltage_l3_l1': ('voltage', 'V', 'voltage/l3_l1', 'L3–L1 line voltage'),
    'voltage_ll_avg':('voltage', 'V', 'voltage/ll_avg', 'Average line-to-line voltage'),
    # ── Current (measurement: current) ────────────────────────────────────────
    'current_l1':    ('current', 'A', 'current/l1', 'L1 current'),
    'current_l2':    ('current', 'A', 'current/l2', 'L2 current'),
    'current_l3':    ('current', 'A', 'current/l3', 'L3 current'),
    'current_n':     ('current', 'A', 'current/n', 'Neutral current'),
    'current_total': ('current', 'A', 'current/total', 'Total / sum current'),
    # ── Active power (measurement: power_active) ──────────────────────────────
    'power_active_l1':    ('power_active', 'W', 'power/active/l1', 'L1 active power'),
    'power_active_l2':    ('power_active', 'W', 'power/active/l2', 'L2 active power'),
    'power_active_l3':    ('power_active', 'W', 'power/active/l3', 'L3 active power'),
    'power_active_total': ('power_active', 'W', 'power/active/total', 'Total active power'),
    # ── Reactive power (measurement: power_reactive) ──────────────────────────
    'power_reactive_l1':    ('power_reactive', 'var', 'power/reactive/l1', 'L1 reactive power'),
    'power_reactive_l2':    ('power_reactive', 'var', 'power/reactive/l2', 'L2 reactive power'),
    'power_reactive_l3':    ('power_reactive', 'var', 'power/reactive/l3', 'L3 reactive power'),
    'power_reactive_total': ('power_reactive', 'var', 'power/reactive/total', 'Total reactive power'),
    # ── Apparent power (measurement: power_apparent) ──────────────────────────
    'power_apparent_l1':    ('power_apparent', 'VA', 'power/apparent/l1', 'L1 apparent power'),
    'power_apparent_l2':    ('power_apparent', 'VA', 'power/apparent/l2', 'L2 apparent power'),
    'power_apparent_l3':    ('power_apparent', 'VA', 'power/apparent/l3', 'L3 apparent power'),
    'power_apparent_total': ('power_apparent', 'VA', 'power/apparent/total', 'Total apparent power'),
    # ── Power factor (measurement: power_factor) ──────────────────────────────
    'power_factor_l1':    ('power_factor', '', 'power_factor/l1', 'L1 power factor'),
    'power_factor_l2':    ('power_factor', '', 'power_factor/l2', 'L2 power factor'),
    'power_factor_l3':    ('power_factor', '', 'power_factor/l3', 'L3 power factor'),
    'power_factor_total': ('power_factor', '', 'power_factor/total', 'System power factor'),
    # ── Frequency (measurement: frequency) ────────────────────────────────────
    'frequency': ('frequency', 'Hz', 'frequency', 'Grid frequency'),
    # ── Active energy (measurement: energy_active) ────────────────────────────
    'energy_active_import':    ('energy_active', 'kWh', 'energy/active/import', 'Total imported active energy'),
    'energy_active_export':    ('energy_active', 'kWh', 'energy/active/export', 'Total exported active energy'),
    'energy_active_net':       ('energy_active', 'kWh', 'energy/active/net', 'Net active energy (import − export)'),
    'energy_active_import_l1': ('energy_active', 'kWh', 'energy/active/import/l1', 'L1 imported active energy'),
    'energy_active_import_l2': ('energy_active', 'kWh', 'energy/active/import/l2', 'L2 imported active energy'),
    'energy_active_import_l3': ('energy_active', 'kWh', 'energy/active/import/l3', 'L3 imported active energy'),
    'energy_active_export_l1': ('energy_active', 'kWh', 'energy/active/export/l1', 'L1 exported active energy'),
    'energy_active_export_l2': ('energy_active', 'kWh', 'energy/active/export/l2', 'L2 exported active energy'),
    'energy_active_export_l3': ('energy_active', 'kWh', 'energy/active/export/l3', 'L3 exported active energy'),
    # ── Reactive / apparent energy ────────────────────────────────────────────
    'energy_reactive_import': ('energy_reactive', 'kvarh', 'energy/reactive/import', 'Total imported reactive energy'),
    'energy_reactive_export': ('energy_reactive', 'kvarh', 'energy/reactive/export', 'Total exported reactive energy'),
    'energy_apparent':        ('energy_apparent', 'kVAh', 'energy/apparent/total', 'Total apparent energy'),
    # ── THD (measurement: thd) ────────────────────────────────────────────────
    'thd_voltage_l1': ('thd', '%', 'thd/voltage/l1', 'L1 voltage THD'),
    'thd_voltage_l2': ('thd', '%', 'thd/voltage/l2', 'L2 voltage THD'),
    'thd_voltage_l3': ('thd', '%', 'thd/voltage/l3', 'L3 voltage THD'),
    'thd_current_l1': ('thd', '%', 'thd/current/l1', 'L1 current THD'),
    'thd_current_l2': ('thd', '%', 'thd/current/l2', 'L2 current THD'),
    'thd_current_l3': ('thd', '%', 'thd/current/l3', 'L3 current THD'),
    # ── Diagnostics (measurement: diagnostic) ─────────────────────────────────
    'model_id':     ('diagnostic', '', 'diagnostic/model_id', 'Meter model identification code'),
    'firmware_rev': ('diagnostic', '', 'diagnostic/firmware_rev', 'Firmware / revision code'),
    'serial':       ('diagnostic', '', 'diagnostic/serial', 'Meter serial / identification'),
    'temperature':  ('diagnostic', '°C', 'diagnostic/temperature', 'Internal temperature'),
    'uptime':       ('diagnostic', 's', 'diagnostic/uptime', 'Meter uptime'),
}

CANONICAL_NAMES = frozenset(CANONICAL_FIELDS)


def mqtt_topic_for(name: str) -> Optional[str]:
    """The hierarchical MQTT topic leaf for a canonical field, or None."""
    e = CANONICAL_FIELDS.get(str(name).lower())
    return e[2] if e else None


def measurement_for(name: str) -> Optional[str]:
    """The InfluxDB measurement for a canonical field, or None."""
    e = CANONICAL_FIELDS.get(str(name).lower())
    return e[0] if e else None


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
