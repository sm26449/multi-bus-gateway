# Canonical field names

> **Generated** from `multibus/canonical_fields.py` (the single source of truth). Do not edit by hand — re-run `tools/gen_canonical_fields.py`.

The canonical name a register carries becomes its **MQTT topic leaf**, its **InfluxDB field**, and its `name` tag — so the same physical quantity is named the same on every device. Convention: `<quantity>_<position>` (l1/l2/l3, l1_n, l1_l2, ln_avg, ll_avg, total, n). Templates should name registers from this list; non-canonical names are flagged as a warning at template validation. Vendor reference maps (e.g. Janitza) predate this and may differ.

**The unit is part of the contract.** A canonical name promises the unit in its Unit column — energy is the base **Wh family** (Wh/varh/VAh), matching the live Janitza chain and the virtual-meter template scales. A meter whose native map is kWh converts in its selection scale (`scale/1000`); selecting a canonical name with a different unit logs a warning, because the mismatch becomes a silent 1000× error the moment the register feeds a virtual meter, a fallback twin, or a cross-device dashboard.

## voltage

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `voltage_l1_n` | `voltage/l1_n` | V | L1–N RMS voltage |
| `voltage_l2_n` | `voltage/l2_n` | V | L2–N RMS voltage |
| `voltage_l3_n` | `voltage/l3_n` | V | L3–N RMS voltage |
| `voltage_ln_avg` | `voltage/ln_avg` | V | Average phase-to-neutral voltage |
| `voltage_l1_l2` | `voltage/l1_l2` | V | L1–L2 line voltage |
| `voltage_l2_l3` | `voltage/l2_l3` | V | L2–L3 line voltage |
| `voltage_l3_l1` | `voltage/l3_l1` | V | L3–L1 line voltage |
| `voltage_ll_avg` | `voltage/ll_avg` | V | Average line-to-line voltage |

## current

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `current_l1` | `current/l1` | A | L1 current |
| `current_l2` | `current/l2` | A | L2 current |
| `current_l3` | `current/l3` | A | L3 current |
| `current_n` | `current/n` | A | Neutral current |
| `current_total` | `current/total` | A | Total / sum current |
| `current_avg` | `current/avg` | A | Average phase current |

## power_active

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `power_active_l1` | `power/active/l1` | W | L1 active power |
| `power_active_l2` | `power/active/l2` | W | L2 active power |
| `power_active_l3` | `power/active/l3` | W | L3 active power |
| `power_active_total` | `power/active/total` | W | Total active power |

## power_reactive

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `power_reactive_l1` | `power/reactive/l1` | var | L1 reactive power |
| `power_reactive_l2` | `power/reactive/l2` | var | L2 reactive power |
| `power_reactive_l3` | `power/reactive/l3` | var | L3 reactive power |
| `power_reactive_total` | `power/reactive/total` | var | Total reactive power |

## power_apparent

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `power_apparent_l1` | `power/apparent/l1` | VA | L1 apparent power |
| `power_apparent_l2` | `power/apparent/l2` | VA | L2 apparent power |
| `power_apparent_l3` | `power/apparent/l3` | VA | L3 apparent power |
| `power_apparent_total` | `power/apparent/total` | VA | Total apparent power |

## power_factor

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `power_factor_l1` | `power_factor/l1` | — | L1 power factor |
| `power_factor_l2` | `power_factor/l2` | — | L2 power factor |
| `power_factor_l3` | `power_factor/l3` | — | L3 power factor |
| `power_factor_total` | `power_factor/total` | — | System power factor |

## frequency

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `frequency` | `frequency` | Hz | Grid frequency |

## energy_active

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `energy_active_import` | `energy/active/import` | Wh | Total imported active energy |
| `energy_active_export` | `energy/active/export` | Wh | Total exported active energy |
| `energy_active_net` | `energy/active/net` | Wh | Net active energy (import − export) |
| `energy_active_total` | `energy/active/total` | Wh | Total active energy (import + export) |
| `energy_active_import_l1` | `energy/active/import/l1` | Wh | L1 imported active energy |
| `energy_active_import_l2` | `energy/active/import/l2` | Wh | L2 imported active energy |
| `energy_active_import_l3` | `energy/active/import/l3` | Wh | L3 imported active energy |
| `energy_active_export_l1` | `energy/active/export/l1` | Wh | L1 exported active energy |
| `energy_active_export_l2` | `energy/active/export/l2` | Wh | L2 exported active energy |
| `energy_active_export_l3` | `energy/active/export/l3` | Wh | L3 exported active energy |
| `energy_active_generated` | `energy/active/generated` | Wh | Lifetime generated active energy |

## energy_reactive

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `energy_reactive_import` | `energy/reactive/import` | varh | Total imported reactive energy |
| `energy_reactive_export` | `energy/reactive/export` | varh | Total exported reactive energy |
| `energy_reactive_total` | `energy/reactive/total` | varh | Total reactive energy (import + export) |

## energy_apparent

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `energy_apparent` | `energy/apparent/total` | VAh | Total apparent energy |

## thd

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `thd_voltage_l1` | `thd/voltage/l1` | % | L1 voltage THD |
| `thd_voltage_l2` | `thd/voltage/l2` | % | L2 voltage THD |
| `thd_voltage_l3` | `thd/voltage/l3` | % | L3 voltage THD |
| `thd_current_l1` | `thd/current/l1` | % | L1 current THD |
| `thd_current_l2` | `thd/current/l2` | % | L2 current THD |
| `thd_current_l3` | `thd/current/l3` | % | L3 current THD |

## diagnostic

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `model_id` | `diagnostic/model_id` | — | Meter model identification code |
| `firmware_rev` | `diagnostic/firmware_rev` | — | Firmware / revision code |
| `serial` | `diagnostic/serial` | — | Meter serial / identification |
| `temperature` | `diagnostic/temperature` | °C | Internal temperature |
| `uptime` | `diagnostic/uptime` | s | Meter uptime |
| `manufacturer` | `diagnostic/manufacturer` | — | Device manufacturer |
| `model` | `diagnostic/model` | — | Device model name |

## dc

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `voltage_dc` | `dc/voltage` | V | DC bus voltage |
| `current_dc` | `dc/current` | A | DC bus current |
| `power_dc` | `dc/power` | W | DC bus power |

## mppt

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `mppt_modules` | `mppt/modules` | — | Number of MPPT modules/strings |
| `voltage_dc_mppt1` | `mppt/1/voltage` | V | MPPT string 1 DC voltage |
| `current_dc_mppt1` | `mppt/1/current` | A | MPPT string 1 DC current |
| `power_dc_mppt1` | `mppt/1/power` | W | MPPT string 1 DC power |
| `energy_dc_mppt1` | `mppt/1/energy` | Wh | MPPT string 1 lifetime DC energy |
| `temperature_mppt1` | `mppt/1/temperature` | °C | MPPT string 1 temperature |
| `voltage_dc_mppt2` | `mppt/2/voltage` | V | MPPT string 2 DC voltage |
| `current_dc_mppt2` | `mppt/2/current` | A | MPPT string 2 DC current |
| `power_dc_mppt2` | `mppt/2/power` | W | MPPT string 2 DC power |
| `energy_dc_mppt2` | `mppt/2/energy` | Wh | MPPT string 2 lifetime DC energy |
| `temperature_mppt2` | `mppt/2/temperature` | °C | MPPT string 2 temperature |

## temperature

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `temperature_cabinet` | `temperature/cabinet` | °C | Cabinet temperature |
| `temperature_heatsink` | `temperature/heatsink` | °C | Heatsink temperature |
| `temperature_transformer` | `temperature/transformer` | °C | Transformer temperature |
| `temperature_other` | `temperature/other` | °C | Other/auxiliary temperature |

## status

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `operating_state` | `status/operating_state` | — | Operating state code (e.g. SunSpec St) |
| `vendor_state` | `status/vendor_state` | — | Vendor-specific state code |
| `event_flags_1` | `status/event_flags/1` | — | Standard event flags word 1 |
| `event_flags_2` | `status/event_flags/2` | — | Standard event flags word 2 |
| `vendor_event_flags_1` | `status/vendor_event_flags/1` | — | Vendor event flags word 1 |
| `vendor_event_flags_2` | `status/vendor_event_flags/2` | — | Vendor event flags word 2 |
| `vendor_event_flags_3` | `status/vendor_event_flags/3` | — | Vendor event flags word 3 |
| `vendor_event_flags_4` | `status/vendor_event_flags/4` | — | Vendor event flags word 4 |
| `status_text` | `status/text` | — | Operating state, decoded to vendor wording |
| `status_alarm` | `status/alarm` | — | Operating state is an alarm condition (1/0) |
| `status_active` | `status/active` | — | Device is actively producing (1/0) |

_Total: 88 canonical fields across 16 measurements._
