# Canonical field names

> **Generated** from `multibus/canonical_fields.py` (the single source of truth). Do not edit by hand — re-run `tools/gen_canonical_fields.py`.

The canonical name a register carries becomes its **MQTT topic leaf**, its **InfluxDB field**, and its `name` tag — so the same physical quantity is named the same on every device. Convention: `<quantity>_<position>` (l1/l2/l3, l1_n, l1_l2, ln_avg, ll_avg, total, n). Templates should name registers from this list; non-canonical names are flagged as a warning at template validation. Vendor reference maps (e.g. Janitza) predate this and may differ.

**The unit is part of the contract.** A canonical name promises the unit in its Unit column — energy is the base **Wh family** (Wh/varh/VAh), matching the live Janitza chain and the virtual-meter template scales. A meter whose native map is kWh converts in its selection scale (`scale/1000`); selecting a canonical name with a different unit logs a warning, because the mismatch becomes a silent 1000× error the moment the register feeds a virtual meter, a fallback twin, or a cross-device dashboard.

**Beyond the grid.** The same rules cover batteries (`battery`), the environment, water/gas/heat metering, EV charging and tank levels. A battery's current and power are **positive while charging** (the BMS and Victron convention). BMS maps already used clear words (`soc`, `cycles`, `cell_1`…), so those are the canonical names; a bare word that would mean something else on a meter gets a prefix (`battery_current`, `battery_power`, `battery_status`).

**Counters** (marked Σ) only grow: every `energy_*` field except a battery's two state figures, plus water/gas volume, heat energy, an EV charger's lifetime energy and a battery's cycles. A frozen counter is still a true statement, so the gateway treats a stale one differently from a stale measurement.

**Your own fields.** A quantity this list does not cover (a heat pump's COP, a pool's chlorine level) can be added on **Templates → Canonical fields**: a name, the category (its InfluxDB measurement), a unit, the MQTT topic leaf (by default `category/name`), whether it is a counter. It is then canonical everywhere a built-in one is: topic, measurement, unit contract, editor guidance. Built-in names cannot be redefined; once a device reads a field its category and topic are fixed (they hold history). A template that names your fields carries their definitions when exported, and creates them on the gateway it is uploaded to. They are stored in `config/canonical_fields_user.json` (`POST/DELETE /api/canonical-fields/user`).

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
| `energy_active_import` Σ | `energy/active/import` | Wh | Total imported active energy |
| `energy_active_export` Σ | `energy/active/export` | Wh | Total exported active energy |
| `energy_active_net` Σ | `energy/active/net` | Wh | Net active energy (import − export) |
| `energy_active_total` Σ | `energy/active/total` | Wh | Total active energy (import + export) |
| `energy_active_import_l1` Σ | `energy/active/import/l1` | Wh | L1 imported active energy |
| `energy_active_import_l2` Σ | `energy/active/import/l2` | Wh | L2 imported active energy |
| `energy_active_import_l3` Σ | `energy/active/import/l3` | Wh | L3 imported active energy |
| `energy_active_export_l1` Σ | `energy/active/export/l1` | Wh | L1 exported active energy |
| `energy_active_export_l2` Σ | `energy/active/export/l2` | Wh | L2 exported active energy |
| `energy_active_export_l3` Σ | `energy/active/export/l3` | Wh | L3 exported active energy |
| `energy_active_generated` Σ | `energy/active/generated` | Wh | Lifetime generated active energy |

## energy_reactive

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `energy_reactive_import` Σ | `energy/reactive/import` | varh | Total imported reactive energy |
| `energy_reactive_export` Σ | `energy/reactive/export` | varh | Total exported reactive energy |
| `energy_reactive_total` Σ | `energy/reactive/total` | varh | Total reactive energy (import + export) |

## energy_apparent

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `energy_apparent` Σ | `energy/apparent/total` | VAh | Total apparent energy |

## thd

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `thd_voltage_l1` | `thd/voltage/l1` | % | L1 voltage THD |
| `thd_voltage_l2` | `thd/voltage/l2` | % | L2 voltage THD |
| `thd_voltage_l3` | `thd/voltage/l3` | % | L3 voltage THD |
| `thd_current_l1` | `thd/current/l1` | % | L1 current THD |
| `thd_current_l2` | `thd/current/l2` | % | L2 current THD |
| `thd_current_l3` | `thd/current/l3` | % | L3 current THD |

## site

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `power_pv` | `power/pv` | W | Total PV generation across the installation |
| `power_grid` | `power/grid` | W | Grid power (negative = exporting) |
| `power_load` | `power/load` | W | House load (negative = consuming) |
| `power_battery` | `power/battery` | W | Battery power (negative = charging) |
| `energy_today` Σ | `energy/today` | Wh | Energy generated today |
| `energy_year` Σ | `energy/year` | Wh | Energy generated this year |
| `energy_lifetime` Σ | `energy/lifetime` | Wh | Energy generated since commissioning |
| `autonomy` | `autonomy` | % | Share of the load covered without the grid |
| `self_consumption` | `self_consumption` | % | Share of generation consumed on site |

## controls

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `power_limit_pct` | `controls/power_limit_pct` | % | Active power limit |
| `power_limit_enabled` | `controls/power_limit_enabled` | — | Power limit enabled |
| `power_limit_revert_s` | `controls/power_limit_revert_s` | s | Power limit reverts after |
| `power_limit_ramp_s` | `controls/power_limit_ramp_s` | s | Power limit ramp time |
| `controls_connected` | `controls/connected` | — | Inverter connected (model 123) |
| `controls_model_id` | `controls/model_id` | — | SunSpec model id of the controls block (123) |
| `power_limit_win_s` | `controls/power_limit_win_s` | s | Power limit window |

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
| `energy_dc_mppt1` Σ | `mppt/1/energy` | Wh | MPPT string 1 lifetime DC energy |
| `temperature_mppt1` | `mppt/1/temperature` | °C | MPPT string 1 temperature |
| `voltage_dc_mppt2` | `mppt/2/voltage` | V | MPPT string 2 DC voltage |
| `current_dc_mppt2` | `mppt/2/current` | A | MPPT string 2 DC current |
| `power_dc_mppt2` | `mppt/2/power` | W | MPPT string 2 DC power |
| `energy_dc_mppt2` Σ | `mppt/2/energy` | Wh | MPPT string 2 lifetime DC energy |
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

## battery

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `soc` | `battery/soc` | % | State of charge |
| `soh` | `battery/soh` | % | State of health |
| `cycles` Σ | `battery/cycles` | — | Charge/discharge cycles |
| `pack_voltage` | `battery/voltage` | V | Pack voltage |
| `battery_current` | `battery/current` | A | Battery current (+ charging) |
| `battery_power` | `battery/power` | W | Battery power (+ charging) |
| `battery_status` | `battery/status` | — | Battery state (charge / discharge / standby…) |
| `remaining_capacity` | `battery/capacity/remaining` | Ah | Remaining capacity |
| `total_capacity` | `battery/capacity/total` | Ah | Full-charge capacity |
| `total_discharge_capacity` Σ | `battery/capacity/discharged` | Ah | Lifetime discharged capacity |
| `energy_remaining` | `battery/energy/remaining` | Wh | Energy stored now |
| `energy_to_full` | `battery/energy/to_full` | Wh | Energy still to charge |
| `charge_current_limit` | `battery/limit/charge_current` | A | Max charge current (BMS limit) |
| `discharge_current_limit` | `battery/limit/discharge_current` | A | Max discharge current (BMS limit) |
| `charge_voltage_limit` | `battery/limit/charge_voltage` | V | Charge voltage limit |
| `discharge_voltage_limit` | `battery/limit/discharge_voltage` | V | Discharge cut-off voltage |
| `min_cell_voltage` | `battery/cells/min` | V | Lowest cell voltage |
| `max_cell_voltage` | `battery/cells/max` | V | Highest cell voltage |
| `average_cell_voltage` | `battery/cells/avg` | V | Average cell voltage |
| `cell_delta` | `battery/cells/delta` | mV | Highest − lowest cell voltage |
| `balancing_count` | `battery/cells/balancing` | — | Cells balancing now |
| `cell_1` | `battery/cells/1` | V | Cell 1 voltage |
| `cell_2` | `battery/cells/2` | V | Cell 2 voltage |
| `cell_3` | `battery/cells/3` | V | Cell 3 voltage |
| `cell_4` | `battery/cells/4` | V | Cell 4 voltage |
| `cell_5` | `battery/cells/5` | V | Cell 5 voltage |
| `cell_6` | `battery/cells/6` | V | Cell 6 voltage |
| `cell_7` | `battery/cells/7` | V | Cell 7 voltage |
| `cell_8` | `battery/cells/8` | V | Cell 8 voltage |
| `cell_9` | `battery/cells/9` | V | Cell 9 voltage |
| `cell_10` | `battery/cells/10` | V | Cell 10 voltage |
| `cell_11` | `battery/cells/11` | V | Cell 11 voltage |
| `cell_12` | `battery/cells/12` | V | Cell 12 voltage |
| `cell_13` | `battery/cells/13` | V | Cell 13 voltage |
| `cell_14` | `battery/cells/14` | V | Cell 14 voltage |
| `cell_15` | `battery/cells/15` | V | Cell 15 voltage |
| `cell_16` | `battery/cells/16` | V | Cell 16 voltage |
| `cell_17` | `battery/cells/17` | V | Cell 17 voltage |
| `cell_18` | `battery/cells/18` | V | Cell 18 voltage |
| `cell_19` | `battery/cells/19` | V | Cell 19 voltage |
| `cell_20` | `battery/cells/20` | V | Cell 20 voltage |
| `cell_21` | `battery/cells/21` | V | Cell 21 voltage |
| `cell_22` | `battery/cells/22` | V | Cell 22 voltage |
| `cell_23` | `battery/cells/23` | V | Cell 23 voltage |
| `cell_24` | `battery/cells/24` | V | Cell 24 voltage |
| `cell_25` | `battery/cells/25` | V | Cell 25 voltage |
| `cell_26` | `battery/cells/26` | V | Cell 26 voltage |
| `cell_27` | `battery/cells/27` | V | Cell 27 voltage |
| `cell_28` | `battery/cells/28` | V | Cell 28 voltage |
| `cell_29` | `battery/cells/29` | V | Cell 29 voltage |
| `cell_30` | `battery/cells/30` | V | Cell 30 voltage |
| `cell_31` | `battery/cells/31` | V | Cell 31 voltage |
| `cell_32` | `battery/cells/32` | V | Cell 32 voltage |
| `min_cell_temp` | `battery/cell_temp/min` | °C | Lowest cell temperature |
| `max_cell_temp` | `battery/cell_temp/max` | °C | Highest cell temperature |
| `average_cell_temp` | `battery/cell_temp/avg` | °C | Average cell temperature |
| `cell_temp_1` | `battery/cell_temp/1` | °C | Cell temperature sensor 1 |
| `cell_temp_2` | `battery/cell_temp/2` | °C | Cell temperature sensor 2 |
| `cell_temp_3` | `battery/cell_temp/3` | °C | Cell temperature sensor 3 |
| `cell_temp_4` | `battery/cell_temp/4` | °C | Cell temperature sensor 4 |
| `cell_temp_5` | `battery/cell_temp/5` | °C | Cell temperature sensor 5 |
| `cell_temp_6` | `battery/cell_temp/6` | °C | Cell temperature sensor 6 |
| `cell_temp_7` | `battery/cell_temp/7` | °C | Cell temperature sensor 7 |
| `cell_temp_8` | `battery/cell_temp/8` | °C | Cell temperature sensor 8 |
| `mosfet_temp` | `battery/mosfet_temp` | °C | Power switch (MOSFET) temperature |

## environment

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `ambient_temp` | `environment/temperature` | °C | Ambient temperature |
| `humidity` | `environment/humidity` | % | Relative humidity |
| `air_pressure` | `environment/pressure` | hPa | Air pressure |
| `irradiance` | `environment/irradiance` | W/m² | Solar irradiance on the panel plane |
| `module_temp` | `environment/module_temp` | °C | PV module (back-sheet) temperature |
| `wind_speed` | `environment/wind/speed` | m/s | Wind speed |
| `wind_direction` | `environment/wind/direction` | ° | Wind direction (0 = north) |
| `illuminance` | `environment/illuminance` | lx | Illuminance |
| `co2` | `environment/co2` | ppm | CO₂ concentration |

## water

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `water_volume` Σ | `water/volume` | m³ | Water meter total (counter) |
| `water_flow` | `water/flow` | m³/h | Water flow |

## gas

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `gas_volume` Σ | `gas/volume` | m³ | Gas meter total (counter) |
| `gas_flow` | `gas/flow` | m³/h | Gas flow |

## heat

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `heat_energy` Σ | `heat/energy` | Wh | Heat meter energy total (counter) |
| `heat_power` | `heat/power` | W | Thermal power |
| `heat_flow` | `heat/flow` | m³/h | Heat-carrier volume flow |
| `supply_temp` | `heat/supply_temp` | °C | Supply (flow) temperature |
| `return_temp` | `heat/return_temp` | °C | Return temperature |

## ev

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `ev_power` | `ev/power` | W | Charging power |
| `ev_energy_session` | `ev/energy/session` | Wh | Energy of the current session |
| `ev_energy_total` Σ | `ev/energy/total` | Wh | Charger lifetime energy (counter) |
| `ev_current_limit` | `ev/current_limit` | A | Charging current limit |
| `ev_status` | `ev/status` | — | Charger state |
| `ev_connected` | `ev/connected` | — | Vehicle connected (1/0) |

## level

| InfluxDB field | MQTT topic | Unit | Description |
|---|---|---|---|
| `tank_level` | `level/percent` | % | Tank fill level |
| `tank_volume` | `level/volume` | L | Tank contents |

_Total: 195 canonical fields across 25 measurements._
