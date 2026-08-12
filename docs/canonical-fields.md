# Canonical field names

> **Generated** from `multibus/canonical_fields.py` (the single source of truth). Do not edit by hand — re-run `tools/gen_canonical_fields.py`.

The canonical name a register carries becomes its **MQTT topic leaf**, its **InfluxDB field**, and its `name` tag — so the same physical quantity is named the same on every device. Convention: `<quantity>_<position>` (l1/l2/l3, l1_n, l1_l2, ln_avg, ll_avg, total, n). Templates should name registers from this list; non-canonical names are flagged as a warning at template validation. Vendor reference maps (e.g. Janitza) predate this and may differ.

## voltage

| Field | Unit | Description |
|---|---|---|
| `voltage_l1_n` | V | L1–N RMS voltage |
| `voltage_l2_n` | V | L2–N RMS voltage |
| `voltage_l3_n` | V | L3–N RMS voltage |
| `voltage_ln_avg` | V | Average phase-to-neutral voltage |
| `voltage_l1_l2` | V | L1–L2 line voltage |
| `voltage_l2_l3` | V | L2–L3 line voltage |
| `voltage_l3_l1` | V | L3–L1 line voltage |
| `voltage_ll_avg` | V | Average line-to-line voltage |

## current

| Field | Unit | Description |
|---|---|---|
| `current_l1` | A | L1 current |
| `current_l2` | A | L2 current |
| `current_l3` | A | L3 current |
| `current_n` | A | Neutral current |
| `current_total` | A | Total / sum current |

## power_active

| Field | Unit | Description |
|---|---|---|
| `power_active_l1` | W | L1 active power |
| `power_active_l2` | W | L2 active power |
| `power_active_l3` | W | L3 active power |
| `power_active_total` | W | Total active power |

## power_reactive

| Field | Unit | Description |
|---|---|---|
| `power_reactive_l1` | var | L1 reactive power |
| `power_reactive_l2` | var | L2 reactive power |
| `power_reactive_l3` | var | L3 reactive power |
| `power_reactive_total` | var | Total reactive power |

## power_apparent

| Field | Unit | Description |
|---|---|---|
| `power_apparent_l1` | VA | L1 apparent power |
| `power_apparent_l2` | VA | L2 apparent power |
| `power_apparent_l3` | VA | L3 apparent power |
| `power_apparent_total` | VA | Total apparent power |

## power_factor

| Field | Unit | Description |
|---|---|---|
| `power_factor_l1` | — | L1 power factor |
| `power_factor_l2` | — | L2 power factor |
| `power_factor_l3` | — | L3 power factor |
| `power_factor_total` | — | System power factor |

## frequency

| Field | Unit | Description |
|---|---|---|
| `frequency` | Hz | Grid frequency |

## energy_active

| Field | Unit | Description |
|---|---|---|
| `energy_active_import` | kWh | Total imported active energy |
| `energy_active_export` | kWh | Total exported active energy |
| `energy_active_net` | kWh | Net active energy (import − export) |
| `energy_active_import_l1` | kWh | L1 imported active energy |
| `energy_active_import_l2` | kWh | L2 imported active energy |
| `energy_active_import_l3` | kWh | L3 imported active energy |
| `energy_active_export_l1` | kWh | L1 exported active energy |
| `energy_active_export_l2` | kWh | L2 exported active energy |
| `energy_active_export_l3` | kWh | L3 exported active energy |

## energy_reactive

| Field | Unit | Description |
|---|---|---|
| `energy_reactive_import` | kvarh | Total imported reactive energy |
| `energy_reactive_export` | kvarh | Total exported reactive energy |

## energy_apparent

| Field | Unit | Description |
|---|---|---|
| `energy_apparent` | kVAh | Total apparent energy |

## thd

| Field | Unit | Description |
|---|---|---|
| `thd_voltage_l1` | % | L1 voltage THD |
| `thd_voltage_l2` | % | L2 voltage THD |
| `thd_voltage_l3` | % | L3 voltage THD |
| `thd_current_l1` | % | L1 current THD |
| `thd_current_l2` | % | L2 current THD |
| `thd_current_l3` | % | L3 current THD |

## diagnostic

| Field | Unit | Description |
|---|---|---|
| `model_id` | — | Meter model identification code |
| `firmware_rev` | — | Firmware / revision code |
| `serial` | — | Meter serial / identification |
| `temperature` | °C | Internal temperature |
| `uptime` | s | Meter uptime |

_Total: 53 canonical fields across 12 measurements._
