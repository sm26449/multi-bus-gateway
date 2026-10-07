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
    'current_avg':   ('current', 'A', 'current/avg', 'Average phase current'),
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
    'energy_active_import':    ('energy_active', 'Wh', 'energy/active/import', 'Total imported active energy'),
    'energy_active_export':    ('energy_active', 'Wh', 'energy/active/export', 'Total exported active energy'),
    'energy_active_net':       ('energy_active', 'Wh', 'energy/active/net', 'Net active energy (import − export)'),
    'energy_active_total':     ('energy_active', 'Wh', 'energy/active/total', 'Total active energy (import + export)'),
    'energy_active_import_l1': ('energy_active', 'Wh', 'energy/active/import/l1', 'L1 imported active energy'),
    'energy_active_import_l2': ('energy_active', 'Wh', 'energy/active/import/l2', 'L2 imported active energy'),
    'energy_active_import_l3': ('energy_active', 'Wh', 'energy/active/import/l3', 'L3 imported active energy'),
    'energy_active_export_l1': ('energy_active', 'Wh', 'energy/active/export/l1', 'L1 exported active energy'),
    'energy_active_export_l2': ('energy_active', 'Wh', 'energy/active/export/l2', 'L2 exported active energy'),
    'energy_active_export_l3': ('energy_active', 'Wh', 'energy/active/export/l3', 'L3 exported active energy'),
    # ── Reactive / apparent energy ────────────────────────────────────────────
    'energy_reactive_import': ('energy_reactive', 'varh', 'energy/reactive/import', 'Total imported reactive energy'),
    'energy_reactive_export': ('energy_reactive', 'varh', 'energy/reactive/export', 'Total exported reactive energy'),
    'energy_reactive_total':  ('energy_reactive', 'varh', 'energy/reactive/total', 'Total reactive energy (import + export)'),
    'energy_apparent':        ('energy_apparent', 'VAh', 'energy/apparent/total', 'Total apparent energy'),
    # ── THD (measurement: thd) ────────────────────────────────────────────────
    'thd_voltage_l1': ('thd', '%', 'thd/voltage/l1', 'L1 voltage THD'),
    'thd_voltage_l2': ('thd', '%', 'thd/voltage/l2', 'L2 voltage THD'),
    'thd_voltage_l3': ('thd', '%', 'thd/voltage/l3', 'L3 voltage THD'),
    'thd_current_l1': ('thd', '%', 'thd/current/l1', 'L1 current THD'),
    'thd_current_l2': ('thd', '%', 'thd/current/l2', 'L2 current THD'),
    'thd_current_l3': ('thd', '%', 'thd/current/l3', 'L3 current THD'),
    # ── Site / installation totals (measurement: site) ────────────────────────
    #
    # These describe a whole INSTALLATION, not a device on a bus: how much it is
    # generating, what the house is drawing, which way the grid is flowing. A
    # Fronius DataManager computes them itself and serves them in one call
    # (GetPowerFlowRealtimeData), and they are NOT derivable from the per-unit
    # readings alone — nothing in an inverter's register map knows what the
    # house consumed.
    #
    # Sign convention follows the source: grid power is NEGATIVE while
    # exporting, battery power negative while charging. Flipping it here would
    # make our number disagree with the inverter's own display.
    #
    # The leaves do NOT repeat "site": a canonical name describes the QUANTITY,
    # and which thing it belongs to is already said by the device's own topic
    # prefix. Encoding it twice produced `pv/site/site/power/pv`.
    'power_pv':          ('site', 'W', 'power/pv', 'Total PV generation across the installation'),
    'power_grid':        ('site', 'W', 'power/grid', 'Grid power (negative = exporting)'),
    'power_load':        ('site', 'W', 'power/load', 'House load (negative = consuming)'),
    'power_battery':     ('site', 'W', 'power/battery', 'Battery power (negative = charging)'),
    'energy_today':      ('site', 'Wh', 'energy/today', 'Energy generated today'),
    'energy_year':       ('site', 'Wh', 'energy/year', 'Energy generated this year'),
    'energy_lifetime':   ('site', 'Wh', 'energy/lifetime', 'Energy generated since commissioning'),
    'autonomy':          ('site', '%', 'autonomy', 'Share of the load covered without the grid'),
    'self_consumption':  ('site', '%', 'self_consumption', 'Share of generation consumed on site'),
    # ── Controls (measurement: controls) — SunSpec model 123 read back ──────────
    # What the inverter is currently TOLD, as distinct from what it does. A
    # controller (over-voltage protection, a schedule) writes these through the
    # power-limit action; the leaves let it verify the command took.
    'power_limit_pct':      ('controls', '%', 'controls/power_limit_pct', 'Active power limit'),
    'power_limit_enabled':  ('controls', '', 'controls/power_limit_enabled', 'Power limit enabled'),
    'power_limit_revert_s': ('controls', 's', 'controls/power_limit_revert_s', 'Power limit reverts after'),
    'power_limit_ramp_s':   ('controls', 's', 'controls/power_limit_ramp_s', 'Power limit ramp time'),
    'controls_connected':   ('controls', '', 'controls/connected', 'Inverter connected (model 123)'),
    'controls_model_id':    ('controls', '', 'controls/model_id', 'SunSpec model id of the controls block (123)'),
    'power_limit_win_s':    ('controls', 's', 'controls/power_limit_win_s', 'Power limit window'),
    # ── Diagnostics (measurement: diagnostic) ─────────────────────────────────
    'model_id':     ('diagnostic', '', 'diagnostic/model_id', 'Meter model identification code'),
    'firmware_rev': ('diagnostic', '', 'diagnostic/firmware_rev', 'Firmware / revision code'),
    'serial':       ('diagnostic', '', 'diagnostic/serial', 'Meter serial / identification'),
    'temperature':  ('diagnostic', '°C', 'diagnostic/temperature', 'Internal temperature'),
    'uptime':       ('diagnostic', 's', 'diagnostic/uptime', 'Meter uptime'),
    'manufacturer': ('diagnostic', '', 'diagnostic/manufacturer', 'Device manufacturer'),
    'model':        ('diagnostic', '', 'diagnostic/model', 'Device model name'),

    # ── PV / inverter domain (same grammar: <quantity>_<position>) ───────────
    # DC bus (measurement: dc)
    'voltage_dc': ('dc', 'V', 'dc/voltage', 'DC bus voltage'),
    'current_dc': ('dc', 'A', 'dc/current', 'DC bus current'),
    'power_dc':   ('dc', 'W', 'dc/power', 'DC bus power'),
    # Generated energy — an inverter's lifetime production (measurement: energy_active)
    'energy_active_generated': ('energy_active', 'Wh', 'energy/active/generated',
                                'Lifetime generated active energy'),
    # MPPT strings (measurement: mppt; position = string number)
    'mppt_modules':      ('mppt', '', 'mppt/modules', 'Number of MPPT modules/strings'),
    'voltage_dc_mppt1':  ('mppt', 'V',  'mppt/1/voltage', 'MPPT string 1 DC voltage'),
    'current_dc_mppt1':  ('mppt', 'A',  'mppt/1/current', 'MPPT string 1 DC current'),
    'power_dc_mppt1':    ('mppt', 'W',  'mppt/1/power', 'MPPT string 1 DC power'),
    'energy_dc_mppt1':   ('mppt', 'Wh', 'mppt/1/energy', 'MPPT string 1 lifetime DC energy'),
    'temperature_mppt1': ('mppt', '°C', 'mppt/1/temperature', 'MPPT string 1 temperature'),
    'voltage_dc_mppt2':  ('mppt', 'V',  'mppt/2/voltage', 'MPPT string 2 DC voltage'),
    'current_dc_mppt2':  ('mppt', 'A',  'mppt/2/current', 'MPPT string 2 DC current'),
    'power_dc_mppt2':    ('mppt', 'W',  'mppt/2/power', 'MPPT string 2 DC power'),
    'energy_dc_mppt2':   ('mppt', 'Wh', 'mppt/2/energy', 'MPPT string 2 lifetime DC energy'),
    'temperature_mppt2': ('mppt', '°C', 'mppt/2/temperature', 'MPPT string 2 temperature'),
    # Temperatures (measurement: temperature)
    'temperature_cabinet':     ('temperature', '°C', 'temperature/cabinet', 'Cabinet temperature'),
    'temperature_heatsink':    ('temperature', '°C', 'temperature/heatsink', 'Heatsink temperature'),
    'temperature_transformer': ('temperature', '°C', 'temperature/transformer', 'Transformer temperature'),
    'temperature_other':       ('temperature', '°C', 'temperature/other', 'Other/auxiliary temperature'),
    # Operating status + event flags (measurement: status)
    'operating_state': ('status', '', 'status/operating_state', 'Operating state code (e.g. SunSpec St)'),
    'vendor_state':    ('status', '', 'status/vendor_state', 'Vendor-specific state code'),
    'event_flags_1':   ('status', '', 'status/event_flags/1', 'Standard event flags word 1'),
    'event_flags_2':   ('status', '', 'status/event_flags/2', 'Standard event flags word 2'),
    'vendor_event_flags_1': ('status', '', 'status/vendor_event_flags/1', 'Vendor event flags word 1'),
    'vendor_event_flags_2': ('status', '', 'status/vendor_event_flags/2', 'Vendor event flags word 2'),
    'vendor_event_flags_3': ('status', '', 'status/vendor_event_flags/3', 'Vendor event flags word 3'),
    'vendor_event_flags_4': ('status', '', 'status/vendor_event_flags/4', 'Vendor event flags word 4'),
    # Derived: a template ships these as calculated measurements, not as
    # registers — the decoded state and the two flags a controller acts on.
    'status_text':   ('status', '', 'status/text', 'Operating state, decoded to vendor wording'),
    'status_alarm':  ('status', '', 'status/alarm', 'Operating state is an alarm condition (1/0)'),
    'status_active': ('status', '', 'status/active', 'Device is actively producing (1/0)'),

    # ── Battery / BMS (measurement: battery) ───────────────────────────────
    # The names BMS maps already use where they are clear (soc, cycles,
    # cell_N…); battery_* where a bare word would mean something else on a
    # meter (current, power, status). Signs: current and power are POSITIVE
    # WHILE CHARGING (the BMS and Victron convention).
    'soc':                     ('battery', '%', 'battery/soc', 'State of charge'),
    'soh':                     ('battery', '%', 'battery/soh', 'State of health'),
    'cycles':                  ('battery', '', 'battery/cycles', 'Charge/discharge cycles'),
    'pack_voltage':            ('battery', 'V', 'battery/voltage', 'Pack voltage'),
    'battery_current':         ('battery', 'A', 'battery/current', 'Battery current (+ charging)'),
    'battery_power':           ('battery', 'W', 'battery/power', 'Battery power (+ charging)'),
    'battery_status':          ('battery', '', 'battery/status', 'Battery state (charge / discharge / standby…)'),
    'remaining_capacity':      ('battery', 'Ah', 'battery/capacity/remaining', 'Remaining capacity'),
    'total_capacity':          ('battery', 'Ah', 'battery/capacity/total', 'Full-charge capacity'),
    'total_discharge_capacity':('battery', 'Ah', 'battery/capacity/discharged', 'Lifetime discharged capacity'),
    'energy_remaining':        ('battery', 'Wh', 'battery/energy/remaining', 'Energy stored now'),
    'energy_to_full':          ('battery', 'Wh', 'battery/energy/to_full', 'Energy still to charge'),
    'charge_current_limit':    ('battery', 'A', 'battery/limit/charge_current', 'Max charge current (BMS limit)'),
    'discharge_current_limit': ('battery', 'A', 'battery/limit/discharge_current', 'Max discharge current (BMS limit)'),
    'charge_voltage_limit':    ('battery', 'V', 'battery/limit/charge_voltage', 'Charge voltage limit'),
    'discharge_voltage_limit': ('battery', 'V', 'battery/limit/discharge_voltage', 'Discharge cut-off voltage'),
    'min_cell_voltage':        ('battery', 'V', 'battery/cells/min', 'Lowest cell voltage'),
    'max_cell_voltage':        ('battery', 'V', 'battery/cells/max', 'Highest cell voltage'),
    'average_cell_voltage':    ('battery', 'V', 'battery/cells/avg', 'Average cell voltage'),
    'cell_delta':              ('battery', 'mV', 'battery/cells/delta', 'Highest − lowest cell voltage'),
    'balancing_count':         ('battery', '', 'battery/cells/balancing', 'Cells balancing now'),
    'cell_1': ('battery', 'V', 'battery/cells/1', 'Cell 1 voltage'),
    'cell_2': ('battery', 'V', 'battery/cells/2', 'Cell 2 voltage'),
    'cell_3': ('battery', 'V', 'battery/cells/3', 'Cell 3 voltage'),
    'cell_4': ('battery', 'V', 'battery/cells/4', 'Cell 4 voltage'),
    'cell_5': ('battery', 'V', 'battery/cells/5', 'Cell 5 voltage'),
    'cell_6': ('battery', 'V', 'battery/cells/6', 'Cell 6 voltage'),
    'cell_7': ('battery', 'V', 'battery/cells/7', 'Cell 7 voltage'),
    'cell_8': ('battery', 'V', 'battery/cells/8', 'Cell 8 voltage'),
    'cell_9': ('battery', 'V', 'battery/cells/9', 'Cell 9 voltage'),
    'cell_10': ('battery', 'V', 'battery/cells/10', 'Cell 10 voltage'),
    'cell_11': ('battery', 'V', 'battery/cells/11', 'Cell 11 voltage'),
    'cell_12': ('battery', 'V', 'battery/cells/12', 'Cell 12 voltage'),
    'cell_13': ('battery', 'V', 'battery/cells/13', 'Cell 13 voltage'),
    'cell_14': ('battery', 'V', 'battery/cells/14', 'Cell 14 voltage'),
    'cell_15': ('battery', 'V', 'battery/cells/15', 'Cell 15 voltage'),
    'cell_16': ('battery', 'V', 'battery/cells/16', 'Cell 16 voltage'),
    'cell_17': ('battery', 'V', 'battery/cells/17', 'Cell 17 voltage'),
    'cell_18': ('battery', 'V', 'battery/cells/18', 'Cell 18 voltage'),
    'cell_19': ('battery', 'V', 'battery/cells/19', 'Cell 19 voltage'),
    'cell_20': ('battery', 'V', 'battery/cells/20', 'Cell 20 voltage'),
    'cell_21': ('battery', 'V', 'battery/cells/21', 'Cell 21 voltage'),
    'cell_22': ('battery', 'V', 'battery/cells/22', 'Cell 22 voltage'),
    'cell_23': ('battery', 'V', 'battery/cells/23', 'Cell 23 voltage'),
    'cell_24': ('battery', 'V', 'battery/cells/24', 'Cell 24 voltage'),
    'cell_25': ('battery', 'V', 'battery/cells/25', 'Cell 25 voltage'),
    'cell_26': ('battery', 'V', 'battery/cells/26', 'Cell 26 voltage'),
    'cell_27': ('battery', 'V', 'battery/cells/27', 'Cell 27 voltage'),
    'cell_28': ('battery', 'V', 'battery/cells/28', 'Cell 28 voltage'),
    'cell_29': ('battery', 'V', 'battery/cells/29', 'Cell 29 voltage'),
    'cell_30': ('battery', 'V', 'battery/cells/30', 'Cell 30 voltage'),
    'cell_31': ('battery', 'V', 'battery/cells/31', 'Cell 31 voltage'),
    'cell_32': ('battery', 'V', 'battery/cells/32', 'Cell 32 voltage'),
    'min_cell_temp':           ('battery', '°C', 'battery/cell_temp/min', 'Lowest cell temperature'),
    'max_cell_temp':           ('battery', '°C', 'battery/cell_temp/max', 'Highest cell temperature'),
    'average_cell_temp':       ('battery', '°C', 'battery/cell_temp/avg', 'Average cell temperature'),
    'cell_temp_1': ('battery', '°C', 'battery/cell_temp/1', 'Cell temperature sensor 1'),
    'cell_temp_2': ('battery', '°C', 'battery/cell_temp/2', 'Cell temperature sensor 2'),
    'cell_temp_3': ('battery', '°C', 'battery/cell_temp/3', 'Cell temperature sensor 3'),
    'cell_temp_4': ('battery', '°C', 'battery/cell_temp/4', 'Cell temperature sensor 4'),
    'cell_temp_5': ('battery', '°C', 'battery/cell_temp/5', 'Cell temperature sensor 5'),
    'cell_temp_6': ('battery', '°C', 'battery/cell_temp/6', 'Cell temperature sensor 6'),
    'cell_temp_7': ('battery', '°C', 'battery/cell_temp/7', 'Cell temperature sensor 7'),
    'cell_temp_8': ('battery', '°C', 'battery/cell_temp/8', 'Cell temperature sensor 8'),
    'mosfet_temp':             ('battery', '°C', 'battery/mosfet_temp', 'Power switch (MOSFET) temperature'),

    # ── Environment (measurement: environment) ─────────────────────────────
    'ambient_temp':   ('environment', '°C', 'environment/temperature', 'Ambient temperature'),
    'humidity':       ('environment', '%', 'environment/humidity', 'Relative humidity'),
    'air_pressure':   ('environment', 'hPa', 'environment/pressure', 'Air pressure'),
    'irradiance':     ('environment', 'W/m²', 'environment/irradiance', 'Solar irradiance on the panel plane'),
    'module_temp':    ('environment', '°C', 'environment/module_temp', 'PV module (back-sheet) temperature'),
    'wind_speed':     ('environment', 'm/s', 'environment/wind/speed', 'Wind speed'),
    'wind_direction': ('environment', '°', 'environment/wind/direction', 'Wind direction (0 = north)'),
    'illuminance':    ('environment', 'lx', 'environment/illuminance', 'Illuminance'),
    'co2':            ('environment', 'ppm', 'environment/co2', 'CO₂ concentration'),

    # ── Water / gas / heat metering (measurements: water, gas, heat) ───────
    'water_volume': ('water', 'm³', 'water/volume', 'Water meter total (counter)'),
    'water_flow':   ('water', 'm³/h', 'water/flow', 'Water flow'),
    'gas_volume':   ('gas', 'm³', 'gas/volume', 'Gas meter total (counter)'),
    'gas_flow':     ('gas', 'm³/h', 'gas/flow', 'Gas flow'),
    'heat_energy':  ('heat', 'Wh', 'heat/energy', 'Heat meter energy total (counter)'),
    'heat_power':   ('heat', 'W', 'heat/power', 'Thermal power'),
    'heat_flow':    ('heat', 'm³/h', 'heat/flow', 'Heat-carrier volume flow'),
    'supply_temp':  ('heat', '°C', 'heat/supply_temp', 'Supply (flow) temperature'),
    'return_temp':  ('heat', '°C', 'heat/return_temp', 'Return temperature'),

    # ── EV charging (measurement: ev) ──────────────────────────────────────
    'ev_power':          ('ev', 'W', 'ev/power', 'Charging power'),
    'ev_energy_session': ('ev', 'Wh', 'ev/energy/session', 'Energy of the current session'),
    'ev_energy_total':   ('ev', 'Wh', 'ev/energy/total', 'Charger lifetime energy (counter)'),
    'ev_current_limit':  ('ev', 'A', 'ev/current_limit', 'Charging current limit'),
    'ev_status':         ('ev', '', 'ev/status', 'Charger state'),
    'ev_connected':      ('ev', '', 'ev/connected', 'Vehicle connected (1/0)'),

    # ── Tanks and levels (measurement: level) ──────────────────────────────
    'tank_level':  ('level', '%', 'level/percent', 'Tank fill level'),
    'tank_volume': ('level', 'L', 'level/volume', 'Tank contents'),
}

# Lifetime counters: they only grow, and a frozen value is still a TRUE
# statement. Every energy_* field is one, except the battery's two state
# figures; a few counters are not energy.
_COUNTERS = frozenset({'water_volume', 'gas_volume', 'heat_energy', 'ev_energy_total',
                        'total_discharge_capacity', 'cycles'})
_NOT_COUNTERS = frozenset({'energy_remaining', 'energy_to_full', 'ev_energy_session'})

# ── Your own canonical fields ─────────────────────────────────────────────
# The dictionary above is the gateway's; an installation adds the quantities
# it needs (a heat pump's COP, a pool's chlorine level) in
# config/canonical_fields_user.json, managed from Templates → Fields. They
# join CANONICAL_FIELDS — the SAME dict object every module imported — so a
# user field is canonical everywhere a built-in one is: MQTT topic, InfluxDB
# measurement, unit contract, editor guidance. A built-in name is never
# overridden.
BUILTIN_FIELDS: Dict[str, Tuple[str, str, str, str]] = dict(CANONICAL_FIELDS)
USER_FIELDS: Dict[str, Dict] = {}
CANONICAL_NAMES = frozenset(CANONICAL_FIELDS)

def validate_user_field(name: str, d: Dict) -> List[str]:
    """Errors in words for one user field definition (empty = fine)."""
    import re
    errs = []
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,47}", str(name or "")):
        errs.append("name: lowercase letters, digits and _ (2-48 chars, starts with a letter)")
    elif name in BUILTIN_FIELDS:
        errs.append(f"name: '{name}' is a built-in canonical field — use it as it is")
    cat = str((d or {}).get("category") or "")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", cat):
        errs.append("category: the InfluxDB measurement, lowercase a-z 0-9 _ (e.g. heat_pump)")
    topic = str((d or {}).get("topic") or "")
    if topic and not re.fullmatch(r"[a-z0-9_]+(/[a-z0-9_]+){0,5}", topic):
        errs.append("topic: lowercase path segments separated by / (e.g. heat_pump/cop)")
    unit = (d or {}).get("unit", "")
    if not isinstance(unit, str) or len(unit) > 16:
        errs.append("unit: text up to 16 characters (W, °C, m³/h…; empty for a plain number)")
    desc = (d or {}).get("description", "")
    if not isinstance(desc, str) or len(desc) > 160:
        errs.append("description: text up to 160 characters")
    if "counter" in (d or {}) and not isinstance(d.get("counter"), bool):
        errs.append("counter: true or false")
    return errs


def set_user_fields(fields: Dict[str, Dict]) -> List[str]:
    """Replace the user fields (already validated or not — bad ones are
    skipped and named). Rebuilds the shared dictionary in place."""
    global CANONICAL_NAMES
    bad, good = [], {}
    for name, d in (fields or {}).items():
        e = validate_user_field(name, d)
        if e:
            bad.append(f"{name}: {'; '.join(e)}")
            continue
        good[name] = {"category": d["category"], "unit": d.get("unit", "") or "",
                      "topic": d.get("topic") or f"{d['category']}/{name}",
                      "description": d.get("description", "") or "",
                      "counter": bool(d.get("counter", False))}
    USER_FIELDS.clear()
    USER_FIELDS.update(good)
    CANONICAL_FIELDS.clear()
    CANONICAL_FIELDS.update(BUILTIN_FIELDS)
    for name, d in good.items():
        CANONICAL_FIELDS[name] = (d["category"], d["unit"], d["topic"], d["description"])
    CANONICAL_NAMES = frozenset(CANONICAL_FIELDS)
    return bad


def load_user_fields(path) -> List[str]:
    """Read config/canonical_fields_user.json at boot. A missing file means
    none; a broken one is reported, never fatal (the built-ins still work)."""
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        set_user_fields({})
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        set_user_fields({})
        return [f"{p.name}: unreadable ({e})"]
    return set_user_fields((data or {}).get("fields") or {})


def save_user_fields(path) -> None:
    """Write the user fields atomically."""
    import json
    import os
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "fields": USER_FIELDS}, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def mqtt_topic_for(name: str) -> Optional[str]:
    """The hierarchical MQTT topic leaf for a canonical field, or None."""
    e = CANONICAL_FIELDS.get(str(name).lower())
    return e[2] if e else None


def measurement_for(name: str) -> Optional[str]:
    """The InfluxDB measurement for a canonical field, or None."""
    e = CANONICAL_FIELDS.get(str(name).lower())
    return e[0] if e else None



def field_meta(name: str):
    """A canonical field's ``{measurement, unit, topic, label}``, or None for a
    name outside the dictionary. Lets a view label a value it only knows by
    name — an endpoint's aggregate, say — without re-deriving the vocabulary."""
    row = CANONICAL_FIELDS.get(name)
    if not row:
        return None
    measurement, unit, topic, label = row
    return {"measurement": measurement, "unit": unit,
            "topic": topic, "label": label}

def is_canonical(name: str) -> bool:
    """True if ``name`` is a canonical field name."""
    return str(name).lower() in CANONICAL_NAMES


def canonical_unit_for(name: str) -> Optional[str]:
    """The CANONICAL unit of a canonical field (e.g. 'Wh' for energy — the
    base Wh-family, matching the live Janitza chain and the vmeter template
    scales), or None for unknown/unit-less fields. A device whose native
    register is kWh must convert in its selection scale — the canonical name
    is a contract on the unit too (a kWh value under an energy_* name is a
    silent 1000x error in every consumer)."""
    e = CANONICAL_FIELDS.get(str(name).lower())
    return (e[1] or None) if e else None


def is_cumulative_field(name: str) -> bool:
    """True for lifetime-accumulating canonical fields (energy counters).

    These need different absence semantics than instantaneous measurements:
    a frozen counter is a TRUE statement ("energy delivered so far"), while
    switching a counter row to another physical meter's lifetime total is a
    non-monotonic jump that corrupts downstream kWh statistics (Victron /
    DataManager). Used to exclude counter rows from device_fallback wiring."""
    n = str(name).lower()
    if n in USER_FIELDS:
        return USER_FIELDS[n]["counter"]
    if n in _NOT_COUNTERS:
        return False
    return n in _COUNTERS or n.startswith('energy_')


def suggest(name: str) -> Optional[str]:
    """Closest canonical name to ``name`` (for 'did you mean …?'), or None."""
    import difflib
    m = difflib.get_close_matches(str(name).lower(), CANONICAL_NAMES, n=1, cutoff=0.6)
    return m[0] if m else None


def non_canonical(names: List[str]) -> List[str]:
    """The subset of ``names`` that are not canonical."""
    return [n for n in names if not is_canonical(n)]


# ── Canonical-name inference (drives the "auto-canonicalize" button) ──────────
# Conservative by design: every composed candidate is validated against the
# dictionary, and anything ambiguous or not representable returns None rather
# than a plausible-but-wrong name — because this RENAMES live registers feeding
# MQTT/InfluxDB/automations. A wrong rename is far worse than no rename.
import re as _re  # noqa: E402

# Anchored unit tokens — matching the whole unit (not a substring) is what stops
# 'kvar' from being read as apparent power ('va') and 'W/m²' as active power.
_UNIT_RX: Dict[str, "_re.Pattern"] = {
    'voltage':         _re.compile(r'^k?v$'),
    'current':         _re.compile(r'^k?a$'),
    'power_active':    _re.compile(r'^[km]?w$'),
    'power_apparent':  _re.compile(r'^k?va$'),
    'power_reactive':  _re.compile(r'^(k?var|mvar)$'),
    'frequency':       _re.compile(r'^hz$'),
    'energy_active':   _re.compile(r'^[km]?wh$'),
    'energy_reactive': _re.compile(r'^k?varh$'),
    'energy_apparent': _re.compile(r'^k?vah$'),
}


def _phase_pair(a: str, b: str) -> Optional[str]:
    return {'12': 'l1_l2', '23': 'l2_l3', '13': 'l3_l1'}.get(''.join(sorted([a, b])))


def guess_canonical(name: str = '', label: str = '', unit: str = '',
                    description: str = '') -> Optional[str]:
    """Infer the canonical field name for a register from its descriptors, or
    None. Conservative: ambiguous direction words (delivered/forward/consumed —
    vendor-specific polarity), per-phase/tariff energy the dictionary can't
    represent, and weak position signals all yield None instead of a wrong name.
    """
    name = str(name or '')
    if is_canonical(name):
        return name.lower()
    hay = f'{label} {name} {description}'.lower()
    u = str(unit or '').strip().lower()

    def cand(n: Optional[str]) -> Optional[str]:
        return n if (n and is_canonical(n)) else None

    # An electrical unit means this is a measurement, not an identity register —
    # so 'model'/'serial'/'firmware' in the label (e.g. "Voltage L1 (model
    # SDM630)") must NOT short-circuit to a diagnostic.
    elec_unit = bool(u) and bool(_re.match(r'^[km]?(v|a|w|va|var|wh|varh|vah|hz)$', u))

    # diagnostics — require a specific token; the identity ones only when there
    # is no electrical unit; guard 'serial' against comms config
    if not elec_unit:
        if _re.search(r'\bserial\b', hay) and not _re.search(
                r'port|address|baud|parity|rs.?485|rs.?232|config|comm', hay):
            return 'serial'
        if _re.search(r'firmware|\bfw\b', hay):
            return 'firmware_rev'
        if _re.search(r'\bmodel\b', hay):
            return 'model_id'
    if _re.search(r'temperature', hay) or (_re.search(r'\btemp\b', hay) and 'c' in u):
        return 'temperature'
    if _re.search(r'\buptime\b', hay):
        return 'uptime'
    if _re.search(r'\bthd\b', hay):
        sub = 'current' if _re.search(r'current|\bamp|thd[\s_-]*i\b|\bi[123]\b', hay) else 'voltage'
        m = _re.search(r'l\s*([123])\b|phase\s*([123])', hay)
        ph = m and (m.group(1) or m.group(2))
        return cand(f'thd_{sub}_l{ph}') if ph else None

    def um(q: str) -> bool:
        return bool(u) and bool(_UNIT_RX[q].match(u))

    # quantity — energy before power (unit substrings), reactive/apparent before base
    q = None
    if um('energy_reactive') or (_re.search(r'reactive', hay) and _re.search(r'energ', hay)):
        q = 'energy_reactive'
    elif um('energy_apparent') or (_re.search(r'apparent', hay) and _re.search(r'energ', hay)):
        q = 'energy_apparent'
    elif um('energy_active') or (_re.search(r'energ', hay) and not _re.search(r'reactive|apparent', hay)
                                 and _re.search(r'import|export|\bnet\b|\btotal\b|active', hay)):
        q = 'energy_active'
    elif um('power_reactive') or (_re.search(r'reactive', hay) and _re.search(r'power', hay)):
        q = 'power_reactive'
    elif um('power_apparent') or (_re.search(r'apparent', hay) and not _re.search(r'energ', hay)):
        q = 'power_apparent'
    elif um('frequency') or _re.search(r'frequency|\bfreq\b', hay):
        return 'frequency'
    elif _re.search(r'power\s*factor|cos[\s._-]*(?:phi|φ)|\bpf\b', hay):
        q = 'power_factor'
    elif um('power_active') or _re.search(r'active\s*power|real\s*power', hay):
        q = 'power_active'
    elif um('voltage') or _re.search(r'voltage|\bvolt', hay):
        q = 'voltage'
    elif um('current') or _re.search(r'current|\bamp', hay):
        q = 'current'
    if not q:
        return None

    # position
    phases = _re.findall(r'l\s*([123])\b', hay) + _re.findall(r'phase\s*([123])', hay)
    distinct = sorted(set(phases))
    single_ph = phases[0] if phases else None
    is_total = bool(_re.search(r'\btotal\b|\bsum\b|sum3|\bsys\b|system', hay))
    is_avg = bool(_re.search(r'average|\bavg\b', hay))
    # 'neutral' the quantity (neutral current) — NOT the '-N' in an 'L-N'
    # line-to-neutral voltage reference, so key on the word, not a bare 'n'.
    is_neutral = bool(_re.search(r'neutral', hay))
    line_line = bool(_re.search(
        r'ull|l\s*[123]\s*(?:-|_|,|/|to|and)\s*l?\s*[123]|line[\s-]*to[\s-]*line'
        r'|line[\s-]*line|phase[\s-]*to[\s-]*phase', hay))

    # A "bare" quantity — no phase / avg / line-line / neutral / total token.
    # For POWER this is unambiguously the total (a single-phase meter's only
    # value AND a 3-phase meter's system power are both 'total'). For VOLTAGE and
    # CURRENT it is genuinely ambiguous — a bare 'Voltage' could be a single
    # phase (SDM120) OR the system average / total (SunSpec 'AC Voltage'), so we
    # return None rather than silently mislabel an aggregate as L1.
    bare = not phases and not is_avg and not line_line and not is_neutral and not is_total

    if q == 'voltage':
        if is_avg:
            return cand('voltage_ll_avg' if line_line else 'voltage_ln_avg')
        if line_line:
            if len(distinct) >= 2:
                return cand(f'voltage_{_phase_pair(distinct[0], distinct[1])}')
            return None
        if single_ph and len(distinct) == 1:
            return cand(f'voltage_l{single_ph}_n')
        return None                   # bare voltage is ambiguous → leave for the human
    if q == 'current':
        if is_neutral:
            return cand('current_n')
        if is_avg:
            return cand('current_avg')
        if single_ph and len(distinct) == 1:
            return cand(f'current_l{single_ph}')
        if is_total:
            return cand('current_total')
        return None                   # bare current is ambiguous → leave for the human
    if q in ('power_active', 'power_reactive', 'power_apparent', 'power_factor'):
        if single_ph and len(distinct) == 1:
            return cand(f'{q}_l{single_ph}')
        if is_total or bare:          # bare power = total (single-phase value or 3-phase system)
            return cand(f'{q}_total')
        return None
    if q in ('energy_active', 'energy_reactive'):
        # match import/imported/importing/imports (but NOT 'important')
        has_imp = bool(_re.search(r'\bimport(?:ed|ing|s)?\b', hay))
        has_exp = bool(_re.search(r'\bexport(?:ed|ing|s)?\b', hay))
        if has_imp and has_exp:
            dir_ = 'total'                       # both mentioned → the combined counter
        elif has_imp:
            dir_ = 'import'
        elif has_exp:
            dir_ = 'export'
        elif _re.search(r'\bnet\b', hay):
            dir_ = 'net'
        elif is_total:
            dir_ = 'total'
        else:
            return None
        # per-phase energy is representable ONLY for active import/export; any
        # other per-phase energy (total/net, reactive) has no canonical field →
        # None rather than silently dropping the phase into the aggregate.
        if single_ph:
            if q == 'energy_active' and dir_ in ('import', 'export') and len(distinct) == 1:
                return cand(f'{q}_{dir_}_l{single_ph}')
            return None
        return cand(f'{q}_{dir_}')
    if q == 'energy_apparent':
        if single_ph:                 # no per-phase apparent-energy field → don't aggregate
            return None
        return cand('energy_apparent')
    return None
