# Device catalog — bundled register maps & provenance

> **Generated** by `tools/gen_device_catalog.py` from `multibus/device_templates/*.json`. Do not edit by hand — re-run the generator after changing a template.

Every built-in device map, with its Modbus transport (function code + byte/word order), the exact register table, and the **source it was verified against**. We do not fabricate maps — each entry cites its provenance. **Confidence varies by entry:** some are *vendor-verified* (confirmed against the manufacturer manual or a field-tested driver — e.g. ABB B23 vs the ABB manual, Schneider iEM3000 vs volkszaehler/mbmd, Carlo Gavazzi EM24 vs Victron), while others are *community-sourced* and their description says to verify against your specific unit's manual before billing-grade use (e.g. the Eastron SDM entries). Read each entry's Source line. `scale` is a divisor — engineering value = raw / scale.

**13 device maps.**

| Map | Vendor | Model | Registers | Transport |
|---|---|---|---|---|
| [Janitza UMG 512-PRO](#janitza-umg-512-pro) | Janitza electronics GmbH | UMG 512-PRO | 4126 | FC03 / big |
| [ABB B21 (single-phase)](#abb-b21-single-phase) | ABB | B21 (System pro M compact) | 10 | FC03 / big |
| [ABB B23 (3-phase)](#abb-b23-3-phase) | ABB | B23 (System pro M compact) | 32 | FC03 / big |
| [BLE sensor (Theengs / BTHome → MQTT)](#ble-sensor-theengs--bthome--mqtt) | Theengs | BLE advertisement sensor | 5 | FC03 / big |
| [Carlo Gavazzi EM24 (AV5/AV53, 3-phase)](#carlo-gavazzi-em24-av5av53-3-phase) | Carlo Gavazzi | EM24-DIN AV5(3) | 17 | FC03 / little |
| [Eastron SDM120 (single-phase)](#eastron-sdm120-single-phase) | Eastron | SDM120 Modbus | 10 | FC04 / big |
| [Eastron SDM630 (3-phase)](#eastron-sdm630-3-phase) | Eastron | SDM630 Modbus V2 | 29 | FC04 / big |
| [Fronius Smart Meter 65A-3 (RTU)](#fronius-smart-meter-65a-3-rtu) | Fronius | Smart Meter 65A-3 | 30 | FC03 / little |
| [Fronius SunSpec inverter (int+SF, via datalogger)](#fronius-sunspec-inverter-intsf-via-datalogger) | Fronius | Symo / Primo / Eco (SunSpec 103) | 61 | FC03 / big |
| [Fronius SunSpec meter (int+SF, via datalogger)](#fronius-sunspec-meter-intsf-via-datalogger) | Fronius | Smart Meter 63A/50kA (SunSpec 203) | 48 | FC03 / big |
| [Generic MQTT (JSON)](#generic-mqtt-json) | Generic | MQTT JSON source | 3 | FC03 / big |
| [Schneider iEM3000 (3-phase)](#schneider-iem3000-3-phase) | Schneider Electric | iEM3155 / iEM3255 / iEM3455 / iEM3555 | 22 | FC03 / big |
| [Zigbee sensor (zigbee2mqtt)](#zigbee-sensor-zigbee2mqtt) | Zigbee2MQTT | climate / battery sensor | 6 | FC03 / big |

---

## Janitza UMG 512-PRO

**id** `janitza_umg512_pro` · **vendor** Janitza electronics GmbH · **model** UMG 512-PRO · **version** 1.0.0 · **registers** 4126

- **Transport:** FC03 (read holding registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** Modbus Address List

> Full Modbus register map of the Janitza UMG 512-PRO power quality analyzer, generated from the vendor Modbus address list. Curated defaults included for the common electrical measurements.

_Large built-in map (4126 registers) — not dumped here._ Categories: thd_harmonics_interharmonics (693), config_other (668), statistics_max (634), power_active (454), thd_harmonics_fft_voltage (441), statistics_mean (315), thd_harmonics_fft_current (252), statistics_min (174), energy_active (90), thd_harmonics_thd_voltage (77).

## ABB B21 (single-phase)

**id** `abb_b21` · **vendor** ABB · **model** B21 (System pro M compact) · **version** 1.0.0 · **registers** 10

- **Transport:** FC03 (read holding registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** ABB B-series Modbus (2CDC512084D0101); cross-checked vs steefan85/ABB_B23_Energy_Meter and roastedelectrons/ABBEnergyMeter

> ABB B21 single-phase DIN-rail energy meter (B-series). Same Modbus map as the B23, populated on L1/total. Integer measurements in HOLDING registers (FC03), big-endian / high-word-first. Scales are divisors: V/10, A/100, W|var|VA/100, Hz/100, PF/1000; energy raw kWh*100 is served as canonical Wh (scale 0.1). Verified against the same authoritative B-series sources as the B23 template.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 20480 / 0x5000 | `energy_active_import` | Active energy import (total) | uint64 | 0.1 | Wh | slow |
| 20484 / 0x5004 | `energy_active_export` | Active energy export (total) | uint64 | 0.1 | Wh | slow |
| 20488 / 0x5008 | `energy_active_net` | Active energy net (total) | int64 | 0.1 | Wh | slow |
| 23296 / 0x5B00 | `voltage_l1_n` | Voltage L-N | uint32 | 10 | V | realtime |
| 23308 / 0x5B0C | `current_l1` | Current | uint32 | 100 | A | realtime |
| 23316 / 0x5B14 | `power_active_total` | Active power | int32 | 100 | W | realtime |
| 23324 / 0x5B1C | `power_reactive_total` | Reactive power | int32 | 100 | var | normal |
| 23332 / 0x5B24 | `power_apparent_total` | Apparent power | int32 | 100 | VA | normal |
| 23340 / 0x5B2C | `frequency` | Frequency | uint16 | 100 | Hz | realtime |
| 23354 / 0x5B3A | `power_factor_total` | Power factor | int16 | 1000 | — | normal |

## ABB B23 (3-phase)

**id** `abb_b23` · **vendor** ABB · **model** B23 (System pro M compact) · **version** 1.0.0 · **registers** 32

- **Transport:** FC03 (read holding registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** ABB B23/B24 User Manual 2CMC485003M0201 ch.9 (docs/vendor/meters/abb-b23-b24-user-manual.pdf) — register-for-register confirmed; also cross-checked vs steefan85/ABB_B23_Energy_Meter and roastedelectrons/ABBEnergyMeter

> ABB B23 three-phase DIN-rail energy meter (B-series; B24 shares this map). Integer measurements in HOLDING registers (FC03), big-endian / high-word-first. 32-bit uint32/int32 for instantaneous values, 64-bit uint64/int64 for energy. Scales are divisors: V/10, A/100, W|var|VA/100, Hz/100, PF/1000; energy raw kWh|kvarh*100 is served as canonical Wh/varh (scale 0.1). Verified register-for-register against two independent open-source integrations (steefan85/ABB_B23_Energy_Meter register map + roastedelectrons/ABBEnergyMeter device CSV, which agree) and consistent with ABB manual 2CDC512084D0101.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 20480 / 0x5000 | `energy_active_import` | Active energy import (total) | uint64 | 0.1 | Wh | slow |
| 20484 / 0x5004 | `energy_active_export` | Active energy export (total) | uint64 | 0.1 | Wh | slow |
| 20488 / 0x5008 | `energy_active_net` | Active energy net (total) | int64 | 0.1 | Wh | slow |
| 20492 / 0x500C | `energy_reactive_import` | Reactive energy import (total) | uint64 | 0.1 | varh | slow |
| 20496 / 0x5010 | `energy_reactive_export` | Reactive energy export (total) | uint64 | 0.1 | varh | slow |
| 23296 / 0x5B00 | `voltage_l1_n` | Voltage L1-N | uint32 | 10 | V | realtime |
| 23298 / 0x5B02 | `voltage_l2_n` | Voltage L2-N | uint32 | 10 | V | realtime |
| 23300 / 0x5B04 | `voltage_l3_n` | Voltage L3-N | uint32 | 10 | V | realtime |
| 23302 / 0x5B06 | `voltage_l1_l2` | Voltage L1-L2 | uint32 | 10 | V | normal |
| 23304 / 0x5B08 | `voltage_l2_l3` | Voltage L3-L2 | uint32 | 10 | V | normal |
| 23306 / 0x5B0A | `voltage_l3_l1` | Voltage L1-L3 | uint32 | 10 | V | normal |
| 23308 / 0x5B0C | `current_l1` | Current L1 | uint32 | 100 | A | realtime |
| 23310 / 0x5B0E | `current_l2` | Current L2 | uint32 | 100 | A | realtime |
| 23312 / 0x5B10 | `current_l3` | Current L3 | uint32 | 100 | A | realtime |
| 23314 / 0x5B12 | `current_n` | Current Neutral | uint32 | 100 | A | normal |
| 23316 / 0x5B14 | `power_active_total` | Active power total | int32 | 100 | W | realtime |
| 23318 / 0x5B16 | `power_active_l1` | Active power L1 | int32 | 100 | W | realtime |
| 23320 / 0x5B18 | `power_active_l2` | Active power L2 | int32 | 100 | W | realtime |
| 23322 / 0x5B1A | `power_active_l3` | Active power L3 | int32 | 100 | W | realtime |
| 23324 / 0x5B1C | `power_reactive_total` | Reactive power total | int32 | 100 | var | normal |
| 23326 / 0x5B1E | `power_reactive_l1` | Reactive power L1 | int32 | 100 | var | normal |
| 23328 / 0x5B20 | `power_reactive_l2` | Reactive power L2 | int32 | 100 | var | normal |
| 23330 / 0x5B22 | `power_reactive_l3` | Reactive power L3 | int32 | 100 | var | normal |
| 23332 / 0x5B24 | `power_apparent_total` | Apparent power total | int32 | 100 | VA | normal |
| 23334 / 0x5B26 | `power_apparent_l1` | Apparent power L1 | int32 | 100 | VA | normal |
| 23336 / 0x5B28 | `power_apparent_l2` | Apparent power L2 | int32 | 100 | VA | normal |
| 23338 / 0x5B2A | `power_apparent_l3` | Apparent power L3 | int32 | 100 | VA | normal |
| 23340 / 0x5B2C | `frequency` | Frequency | uint16 | 100 | Hz | realtime |
| 23354 / 0x5B3A | `power_factor_total` | Power factor total | int16 | 1000 | — | normal |
| 23355 / 0x5B3B | `power_factor_l1` | Power factor L1 | int16 | 1000 | — | normal |
| 23356 / 0x5B3C | `power_factor_l2` | Power factor L2 | int16 | 1000 | — | normal |
| 23357 / 0x5B3D | `power_factor_l3` | Power factor L3 | int16 | 1000 | — | normal |

## BLE sensor (Theengs / BTHome → MQTT)

**id** `ble_theengs_sensor` · **vendor** Theengs · **model** BLE advertisement sensor · **version** 1.0.0 · **registers** 5

- **Transport:** FC03 (read holding registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** https://decoder.theengs.io/devices/devices_by_brand.html

> A BLE sensor (Xiaomi LYWSD03MMC/ATC, RuuviTag, Govee, SwitchBot…) whose advertisements are decoded to MQTT JSON by Theengs Gateway or OpenMQTTGateway. Set the device connection topic to the gateway's per-device topic (e.g. home/TheengsGateway/BTtoMQTT/<MAC>). Field names follow the Theengs decoder properties (tempc/hum/batt/volt/rssi) — edit per your sensor model. BLE advertising is slow: keep generous per-row stale bounds when used in composites.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 1 / 0x0001 | `tempc` | Temperature | float | 1 | °C | normal |
| 2 / 0x0002 | `hum` | Humidity | float | 1 | % | normal |
| 3 / 0x0003 | `batt` | Battery | float | 1 | % | normal |
| 4 / 0x0004 | `volt` | Battery voltage | float | 1 | V | normal |
| 5 / 0x0005 | `rssi` | RSSI | float | 1 | dBm | normal |

## Carlo Gavazzi EM24 (AV5/AV53, 3-phase)

**id** `carlo_gavazzi_em24` · **vendor** Carlo Gavazzi · **model** EM24-DIN AV5(3) · **version** 1.0.0 · **registers** 17

- **Transport:** FC03 (read holding registers) · byte order **little-endian, low word first (CDAB / word-swapped)**
- **Source / provenance:** Victron dbus-modbus-client carlo_gavazzi.py (EM24_Meter) + Carlo Gavazzi EM24-DIN communication protocol

> Carlo Gavazzi EM24-DIN 3-phase energy meter (the legacy Victron grid meter). 32-bit measurements are signed INT32 in HOLDING registers (FC03), LOW-WORD-FIRST (little-endian, Reg_s32l). Scales are divisors: V/10, A/1000, W/10, Hz/10; energy raw kWh*10 is served as canonical Wh (scale 0.01). Register map is authoritative from Victron dbus-modbus-client/carlo_gavazzi.py (model detected via reg 0x000b == 1651) and matches this repo's production-proven em24_av53 emulation. CG meters answer both FC03 and FC04 for measurements; FC03 is canonical.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 0 / 0x0000 | `voltage_l1_n` | Voltage L1-N | int32 | 10 | V | realtime |
| 2 / 0x0002 | `voltage_l2_n` | Voltage L2-N | int32 | 10 | V | realtime |
| 4 / 0x0004 | `voltage_l3_n` | Voltage L3-N | int32 | 10 | V | realtime |
| 11 / 0x000B | `model_id` | Meter model id | uint16 | 1 | — | slow |
| 12 / 0x000C | `current_l1` | Current L1 | int32 | 1000 | A | realtime |
| 14 / 0x000E | `current_l2` | Current L2 | int32 | 1000 | A | realtime |
| 16 / 0x0010 | `current_l3` | Current L3 | int32 | 1000 | A | realtime |
| 18 / 0x0012 | `power_active_l1` | Active power L1 | int32 | 10 | W | realtime |
| 20 / 0x0014 | `power_active_l2` | Active power L2 | int32 | 10 | W | realtime |
| 22 / 0x0016 | `power_active_l3` | Active power L3 | int32 | 10 | W | realtime |
| 40 / 0x0028 | `power_active_total` | Total active power | int32 | 10 | W | realtime |
| 51 / 0x0033 | `frequency` | Frequency | uint16 | 10 | Hz | realtime |
| 52 / 0x0034 | `energy_active_import` | Import active energy (total) | int32 | 0.01 | Wh | slow |
| 64 / 0x0040 | `energy_active_import_l1` | Import active energy L1 | int32 | 0.01 | Wh | slow |
| 66 / 0x0042 | `energy_active_import_l2` | Import active energy L2 | int32 | 0.01 | Wh | slow |
| 68 / 0x0044 | `energy_active_import_l3` | Import active energy L3 | int32 | 0.01 | Wh | slow |
| 78 / 0x004E | `energy_active_export` | Export active energy (total) | int32 | 0.01 | Wh | slow |

## Eastron SDM120 (single-phase)

**id** `eastron_sdm120` · **vendor** Eastron · **model** SDM120 Modbus · **version** 0.9.0 · **registers** 10

- **Transport:** FC04 (read input registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** Eastron SDM120/SDM220 Modbus Protocol (input registers, FC04, float32)

> Eastron SDM120 single-phase energy meter (also SDM220). Measurements are 32-bit IEEE-754 floats in INPUT registers (FC4), big-endian — same base addresses as the SDM630 phase 1. Community-sourced canonical map — verify against your unit's manual.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 0 / 0x0000 | `voltage_l1_n` | Voltage | float | 1 | V | realtime |
| 6 / 0x0006 | `current_l1` | Current | float | 1 | A | realtime |
| 12 / 0x000C | `power_active_total` | Active power | float | 1 | W | realtime |
| 18 / 0x0012 | `power_apparent_total` | Apparent power | float | 1 | VA | normal |
| 24 / 0x0018 | `power_reactive_total` | Reactive power | float | 1 | var | normal |
| 30 / 0x001E | `power_factor_total` | Power factor | float | 1 | — | normal |
| 70 / 0x0046 | `frequency` | Frequency | float | 1 | Hz | realtime |
| 72 / 0x0048 | `energy_active_import` | Import active energy | float | 0.001 | Wh | slow |
| 74 / 0x004A | `energy_active_export` | Export active energy | float | 0.001 | Wh | slow |
| 342 / 0x0156 | `energy_active_total` | Total active energy | float | 0.001 | Wh | slow |

## Eastron SDM630 (3-phase)

**id** `eastron_sdm630` · **vendor** Eastron · **model** SDM630 Modbus V2 · **version** 0.9.0 · **registers** 29

- **Transport:** FC04 (read input registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** Eastron SDM630 Modbus Protocol V2 (input registers, FC04, float32)

> Eastron SDM630 3-phase energy meter. Measurements are 32-bit IEEE-754 floats in INPUT registers (FC4), big-endian. Community-sourced canonical map — verify against your unit's manual before relying on it for billing.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 0 / 0x0000 | `voltage_l1_n` | Voltage L1-N | float | 1 | V | realtime |
| 2 / 0x0002 | `voltage_l2_n` | Voltage L2-N | float | 1 | V | realtime |
| 4 / 0x0004 | `voltage_l3_n` | Voltage L3-N | float | 1 | V | realtime |
| 6 / 0x0006 | `current_l1` | Current L1 | float | 1 | A | realtime |
| 8 / 0x0008 | `current_l2` | Current L2 | float | 1 | A | realtime |
| 10 / 0x000A | `current_l3` | Current L3 | float | 1 | A | realtime |
| 12 / 0x000C | `power_active_l1` | Active power L1 | float | 1 | W | realtime |
| 14 / 0x000E | `power_active_l2` | Active power L2 | float | 1 | W | realtime |
| 16 / 0x0010 | `power_active_l3` | Active power L3 | float | 1 | W | realtime |
| 18 / 0x0012 | `power_apparent_l1` | Apparent power L1 | float | 1 | VA | normal |
| 20 / 0x0014 | `power_apparent_l2` | Apparent power L2 | float | 1 | VA | normal |
| 22 / 0x0016 | `power_apparent_l3` | Apparent power L3 | float | 1 | VA | normal |
| 24 / 0x0018 | `power_reactive_l1` | Reactive power L1 | float | 1 | var | normal |
| 26 / 0x001A | `power_reactive_l2` | Reactive power L2 | float | 1 | var | normal |
| 28 / 0x001C | `power_reactive_l3` | Reactive power L3 | float | 1 | var | normal |
| 30 / 0x001E | `power_factor_l1` | Power factor L1 | float | 1 | — | normal |
| 32 / 0x0020 | `power_factor_l2` | Power factor L2 | float | 1 | — | normal |
| 34 / 0x0022 | `power_factor_l3` | Power factor L3 | float | 1 | — | normal |
| 52 / 0x0034 | `power_active_total` | Total active power | float | 1 | W | realtime |
| 56 / 0x0038 | `power_apparent_total` | Total apparent power | float | 1 | VA | normal |
| 60 / 0x003C | `power_reactive_total` | Total reactive power | float | 1 | var | normal |
| 62 / 0x003E | `power_factor_total` | Total power factor | float | 1 | — | normal |
| 70 / 0x0046 | `frequency` | Frequency | float | 1 | Hz | realtime |
| 72 / 0x0048 | `energy_active_import` | Import active energy | float | 0.001 | Wh | slow |
| 74 / 0x004A | `energy_active_export` | Export active energy | float | 0.001 | Wh | slow |
| 76 / 0x004C | `energy_reactive_import` | Import reactive energy | float | 0.001 | varh | slow |
| 78 / 0x004E | `energy_reactive_export` | Export reactive energy | float | 0.001 | varh | slow |
| 342 / 0x0156 | `energy_active_total` | Total active energy | float | 0.001 | Wh | slow |
| 344 / 0x0158 | `energy_reactive_total` | Total reactive energy | float | 0.001 | varh | slow |

## Fronius Smart Meter 65A-3 (RTU)

**id** `fronius_smart_meter_65a` · **vendor** Fronius · **model** Smart Meter 65A-3 · **version** 1.0.0 · **registers** 30

- **Transport:** FC03 (read holding registers) · byte order **little-endian, low word first (CDAB / word-swapped)**
- **Source / provenance:** Field-verified against a physical Fronius Smart Meter 65A-3 over Modbus RTU (2026-08-11): |P|<=S per phase, S^2~=P^2+Q^2, PF=P/S, Freq=50Hz.

> Fronius Smart Meter 65A-3, 3-phase, wired DIRECTLY to your own RS-485 line (Modbus RTU, or an RTU-over-TCP serial bridge). Register map field-verified against the physical meter: INT32 low-word-first (CDAB), holding registers from address 0. Use this ONLY for a direct serial connection to the meter itself. If the meter hangs off a Fronius DataManager/datalogger (the usual PV-system wiring, meter at unit 240), use the separate 'Fronius SunSpec meter (int+SF, via datalogger)' template instead: same hardware, completely different register map.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 0 / 0x0000 | `voltage_l1_n` | Voltage L1-N | int32 | 10.0 | V | realtime |
| 2 / 0x0002 | `voltage_l2_n` | Voltage L2-N | int32 | 10.0 | V | realtime |
| 4 / 0x0004 | `voltage_l3_n` | Voltage L3-N | int32 | 10.0 | V | realtime |
| 6 / 0x0006 | `voltage_l1_l2` | Voltage L1-L2 | int32 | 10.0 | V | realtime |
| 8 / 0x0008 | `voltage_l2_l3` | Voltage L2-L3 | int32 | 10.0 | V | realtime |
| 10 / 0x000A | `voltage_l3_l1` | Voltage L3-L1 | int32 | 10.0 | V | realtime |
| 11 / 0x000B | `model_id` | Meter model id | uint16 | 1.0 | — | slow |
| 12 / 0x000C | `current_l1` | Current L1 | int32 | 1000.0 | A | realtime |
| 14 / 0x000E | `current_l2` | Current L2 | int32 | 1000.0 | A | realtime |
| 16 / 0x0010 | `current_l3` | Current L3 | int32 | 1000.0 | A | realtime |
| 18 / 0x0012 | `power_active_l1` | Active Power L1 | int32 | 10.0 | W | realtime |
| 20 / 0x0014 | `power_active_l2` | Active Power L2 | int32 | 10.0 | W | realtime |
| 22 / 0x0016 | `power_active_l3` | Active Power L3 | int32 | 10.0 | W | realtime |
| 24 / 0x0018 | `power_apparent_l1` | Apparent Power L1 | int32 | 10.0 | VA | normal |
| 26 / 0x001A | `power_apparent_l2` | Apparent Power L2 | int32 | 10.0 | VA | normal |
| 28 / 0x001C | `power_apparent_l3` | Apparent Power L3 | int32 | 10.0 | VA | normal |
| 30 / 0x001E | `power_reactive_l1` | Reactive Power L1 | int32 | 10.0 | var | normal |
| 32 / 0x0020 | `power_reactive_l2` | Reactive Power L2 | int32 | 10.0 | var | normal |
| 34 / 0x0022 | `power_reactive_l3` | Reactive Power L3 | int32 | 10.0 | var | normal |
| 36 / 0x0024 | `voltage_ln_avg` | Voltage L-N sys | int32 | 10.0 | V | normal |
| 38 / 0x0026 | `voltage_ll_avg` | Voltage L-L sys | int32 | 10.0 | V | normal |
| 40 / 0x0028 | `power_active_total` | Active Power Total | int32 | 10.0 | W | realtime |
| 42 / 0x002A | `power_apparent_total` | Apparent Power Total | int32 | 10.0 | VA | normal |
| 44 / 0x002C | `power_reactive_total` | Reactive Power Total | int32 | 10.0 | var | normal |
| 49 / 0x0031 | `frequency` | Frequency | uint16 | 10.0 | Hz | normal |
| 51 / 0x0033 | `power_factor_total` | Power Factor sys | int16 | 1000.0 | — | normal |
| 52 / 0x0034 | `energy_active_import` | Energy Import Total | int32 | 0.01 | Wh | slow |
| 78 / 0x004E | `energy_active_export` | Energy Export Total | int32 | 0.01 | Wh | slow |
| 4096 / 0x1000 | `firmware_rev` | Firmware / revision | uint16 | 1.0 | — | slow |
| 20480 / 0x5000 | `serial` | Serial / ID (ASCII) | string:7 | 1.0 | — | slow |

## Fronius SunSpec inverter (int+SF, via datalogger)

**id** `fronius_sunspec_inverter` · **vendor** Fronius · **model** Symo / Primo / Eco (SunSpec 103) · **version** 1.0.0 · **registers** 61

- **Transport:** FC03 (read holding registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** SunSpec Information Models (int+SF); register map verified live against a production Fronius fleet (raw-frame decode parity, 2026-09-11)

> Fronius inverter read THROUGH its DataManager/datalogger over Modbus TCP, in the SunSpec int + scale-factor register mode (models 1/103/160: AC block, DC block, temperatures, status/events, per-string MPPT, identity). Use this when the datalogger's Modbus TCP slave is enabled and set to 'int+SF' (the Fronius default). For SEVERAL inverters behind one datalogger, add a `plants:` entry with this template and the unit IDs (1, 2, ...) — each unit becomes its own device. CAUTION: dataloggers serve only a few concurrent Modbus clients; if another system polls the same datalogger, keep poll intervals modest (10-15 s) or reduce the number of units read in parallel. Known vendor quirk: PF is published with the generic SunSpec decode (±100); Fronius units report PF raw ±10000 with an out-of-spec scale factor, so dedicated drivers show ±1.0.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 40004 / 0x9C44 | `manufacturer` | Manufacturer | string:16 | 1 | — | static |
| 40020 / 0x9C54 | `model` | Model | string:16 | 1 | — | static |
| 40052 / 0x9C74 | `serial_number` | Serial Number | string:16 | 1 | — | static |
| 40071 / 0x9C87 | `ac_current` | Ac Current | uint16 | 1 | A | normal |
| 40072 / 0x9C88 | `ac_current_a` | Ac Current A | uint16 | 1 | A | normal |
| 40073 / 0x9C89 | `ac_current_b` | Ac Current B | uint16 | 1 | A | normal |
| 40074 / 0x9C8A | `ac_current_c` | Ac Current C | uint16 | 1 | A | normal |
| 40075 / 0x9C8B | `a_sf` | A SF | int16 | 1 | — | normal |
| 40076 / 0x9C8C | `ac_voltage_ab` | Ac Voltage Ab | uint16 | 1 | V | normal |
| 40077 / 0x9C8D | `ac_voltage_bc` | Ac Voltage Bc | uint16 | 1 | V | normal |
| 40078 / 0x9C8E | `ac_voltage_ca` | Ac Voltage Ca | uint16 | 1 | V | normal |
| 40079 / 0x9C8F | `ac_voltage_an` | Ac Voltage An | uint16 | 1 | V | normal |
| 40080 / 0x9C90 | `ac_voltage_bn` | Ac Voltage Bn | uint16 | 1 | V | normal |
| 40081 / 0x9C91 | `ac_voltage_cn` | Ac Voltage Cn | uint16 | 1 | V | normal |
| 40082 / 0x9C92 | `v_sf` | V SF | int16 | 1 | — | normal |
| 40083 / 0x9C93 | `ac_power` | Ac Power | int16 | 1 | W | normal |
| 40084 / 0x9C94 | `w_sf` | W SF | int16 | 1 | — | normal |
| 40085 / 0x9C95 | `ac_frequency` | Ac Frequency | uint16 | 1 | Hz | normal |
| 40086 / 0x9C96 | `hz_sf` | HZ SF | int16 | 1 | — | normal |
| 40087 / 0x9C97 | `apparent_power` | Apparent Power | int16 | 1 | VA | normal |
| 40088 / 0x9C98 | `va_sf` | VA SF | int16 | 1 | — | normal |
| 40089 / 0x9C99 | `reactive_power` | Reactive Power | int16 | 1 | var | normal |
| 40090 / 0x9C9A | `var_sf` | VAR SF | int16 | 1 | — | normal |
| 40091 / 0x9C9B | `power_factor` | Power Factor | int16 | 1 | — | normal |
| 40092 / 0x9C9C | `pf_sf` | PF SF | int16 | 1 | — | normal |
| 40093 / 0x9C9D | `lifetime_energy` | Lifetime Energy | uint32 | 1 | Wh | normal |
| 40095 / 0x9C9F | `wh_sf` | WH SF | int16 | 1 | — | normal |
| 40096 / 0x9CA0 | `dc_current` | Dc Current | uint16 | 1 | A | normal |
| 40097 / 0x9CA1 | `dca_sf` | DCA SF | int16 | 1 | — | normal |
| 40098 / 0x9CA2 | `dc_voltage` | Dc Voltage | uint16 | 1 | V | normal |
| 40099 / 0x9CA3 | `dcv_sf` | DCV SF | int16 | 1 | — | normal |
| 40100 / 0x9CA4 | `dc_power` | Dc Power | int16 | 1 | W | normal |
| 40101 / 0x9CA5 | `dcw_sf` | DCW SF | int16 | 1 | — | normal |
| 40102 / 0x9CA6 | `temp_cabinet` | Temp Cabinet | int16 | 1 | °C | normal |
| 40103 / 0x9CA7 | `temp_heatsink` | Temp Heatsink | int16 | 1 | °C | normal |
| 40104 / 0x9CA8 | `temp_transformer` | Temp Transformer | int16 | 1 | °C | normal |
| 40105 / 0x9CA9 | `temp_other` | Temp Other | int16 | 1 | °C | normal |
| 40106 / 0x9CAA | `tmp_sf` | TMP SF | int16 | 1 | — | normal |
| 40107 / 0x9CAB | `status_code` | Status Code | uint16 | 1 | — | normal |
| 40108 / 0x9CAC | `status_vendor` | Status Vendor | uint16 | 1 | — | normal |
| 40109 / 0x9CAD | `evt1` | Evt1 | uint32 | 1 | — | normal |
| 40111 / 0x9CAF | `evt2` | Evt2 | uint32 | 1 | — | normal |
| 40113 / 0x9CB1 | `evt_vnd1` | Evt Vnd1 | uint32 | 1 | — | normal |
| 40115 / 0x9CB3 | `evt_vnd2` | Evt Vnd2 | uint32 | 1 | — | normal |
| 40117 / 0x9CB5 | `evt_vnd3` | Evt Vnd3 | uint32 | 1 | — | normal |
| 40119 / 0x9CB7 | `evt_vnd4` | Evt Vnd4 | uint32 | 1 | — | normal |
| 40255 / 0x9D3F | `dca_mppt_sf` | DCA_MPPT SF | int16 | 1 | — | normal |
| 40256 / 0x9D40 | `dcv_mppt_sf` | DCV_MPPT SF | int16 | 1 | — | normal |
| 40257 / 0x9D41 | `dcw_mppt_sf` | DCW_MPPT SF | int16 | 1 | — | normal |
| 40258 / 0x9D42 | `dcwh_mppt_sf` | DCWH_MPPT SF | int16 | 1 | — | normal |
| 40261 / 0x9D45 | `mppt_num_modules` | Mppt Num Modules | uint16 | 1 | — | normal |
| 40272 / 0x9D50 | `mppt1_dc_current` | Mppt1 Dc Current | uint16 | 1 | A | normal |
| 40273 / 0x9D51 | `mppt1_dc_voltage` | Mppt1 Dc Voltage | uint16 | 1 | V | normal |
| 40274 / 0x9D52 | `mppt1_dc_power` | Mppt1 Dc Power | uint16 | 1 | W | normal |
| 40275 / 0x9D53 | `mppt1_dc_energy` | Mppt1 Dc Energy | uint32 | 1 | Wh | normal |
| 40279 / 0x9D57 | `mppt1_temperature` | Mppt1 Temperature | int16 | 1 | °C | normal |
| 40292 / 0x9D64 | `mppt2_dc_current` | Mppt2 Dc Current | uint16 | 1 | A | normal |
| 40293 / 0x9D65 | `mppt2_dc_voltage` | Mppt2 Dc Voltage | uint16 | 1 | V | normal |
| 40294 / 0x9D66 | `mppt2_dc_power` | Mppt2 Dc Power | uint16 | 1 | W | normal |
| 40295 / 0x9D67 | `mppt2_dc_energy` | Mppt2 Dc Energy | uint32 | 1 | Wh | normal |
| 40299 / 0x9D6B | `mppt2_temperature` | Mppt2 Temperature | int16 | 1 | °C | normal |

## Fronius SunSpec meter (int+SF, via datalogger)

**id** `fronius_sunspec_meter` · **vendor** Fronius · **model** Smart Meter 63A/50kA (SunSpec 203) · **version** 1.0.0 · **registers** 48

- **Transport:** FC03 (read holding registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** SunSpec Information Models (int+SF); register map verified live against a production Fronius fleet (raw-frame decode parity, 2026-09-11)

> Fronius Smart Meter read THROUGH the DataManager/datalogger over Modbus TCP (SunSpec model 203, int + scale factor; full 3-phase set + per-phase import/export energies). The meter appears on the datalogger at unit ID 240 (default; 241/242 for additional meters). Use this when the meter hangs off a Fronius datalogger — for a Smart Meter wired DIRECTLY to your own RS-485 (RTU or an RTU-TCP bridge), use the separate 'Fronius Smart Meter 65A-3 (RTU)' template instead: same hardware, completely different register map. PF quirk as on the inverter template (generic ±100).

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 40004 / 0x9C44 | `manufacturer` | Manufacturer | string:16 | 1 | — | static |
| 40020 / 0x9C54 | `model` | Model | string:16 | 1 | — | static |
| 40052 / 0x9C74 | `serial_number` | Serial Number | string:16 | 1 | — | static |
| 40071 / 0x9C87 | `current_total` | Current Total | int16 | 1 | A | normal |
| 40072 / 0x9C88 | `current_a` | Current A | int16 | 1 | A | normal |
| 40073 / 0x9C89 | `current_b` | Current B | int16 | 1 | A | normal |
| 40074 / 0x9C8A | `current_c` | Current C | int16 | 1 | A | normal |
| 40075 / 0x9C8B | `a_sf` | A SF | int16 | 1 | — | normal |
| 40076 / 0x9C8C | `voltage_ln_avg` | Voltage Ln Avg | int16 | 1 | V | normal |
| 40077 / 0x9C8D | `voltage_an` | Voltage An | int16 | 1 | V | normal |
| 40078 / 0x9C8E | `voltage_bn` | Voltage Bn | int16 | 1 | V | normal |
| 40079 / 0x9C8F | `voltage_cn` | Voltage Cn | int16 | 1 | V | normal |
| 40080 / 0x9C90 | `voltage_ll_avg` | Voltage Ll Avg | int16 | 1 | V | normal |
| 40081 / 0x9C91 | `voltage_ab` | Voltage Ab | int16 | 1 | V | normal |
| 40082 / 0x9C92 | `voltage_bc` | Voltage Bc | int16 | 1 | V | normal |
| 40083 / 0x9C93 | `voltage_ca` | Voltage Ca | int16 | 1 | V | normal |
| 40084 / 0x9C94 | `v_sf` | V SF | int16 | 1 | — | normal |
| 40085 / 0x9C95 | `frequency` | Frequency | int16 | 1 | Hz | normal |
| 40086 / 0x9C96 | `hz_sf` | HZ SF | int16 | 1 | — | normal |
| 40087 / 0x9C97 | `power_total` | Power Total | int16 | 1 | W | normal |
| 40088 / 0x9C98 | `power_a` | Power A | int16 | 1 | W | normal |
| 40089 / 0x9C99 | `power_b` | Power B | int16 | 1 | W | normal |
| 40090 / 0x9C9A | `power_c` | Power C | int16 | 1 | W | normal |
| 40091 / 0x9C9B | `w_sf` | W SF | int16 | 1 | — | normal |
| 40092 / 0x9C9C | `va_total` | Va Total | int16 | 1 | VA | normal |
| 40093 / 0x9C9D | `va_a` | Va A | int16 | 1 | VA | normal |
| 40094 / 0x9C9E | `va_b` | Va B | int16 | 1 | VA | normal |
| 40095 / 0x9C9F | `va_c` | Va C | int16 | 1 | VA | normal |
| 40096 / 0x9CA0 | `va_sf` | VA SF | int16 | 1 | — | normal |
| 40097 / 0x9CA1 | `var_total` | Var Total | int16 | 1 | var | normal |
| 40098 / 0x9CA2 | `var_a` | Var A | int16 | 1 | var | normal |
| 40099 / 0x9CA3 | `var_b` | Var B | int16 | 1 | var | normal |
| 40100 / 0x9CA4 | `var_c` | Var C | int16 | 1 | var | normal |
| 40101 / 0x9CA5 | `var_sf` | VAR SF | int16 | 1 | — | normal |
| 40102 / 0x9CA6 | `pf_avg` | Pf Avg | int16 | 1 | — | normal |
| 40103 / 0x9CA7 | `pf_a` | Pf A | int16 | 1 | — | normal |
| 40104 / 0x9CA8 | `pf_b` | Pf B | int16 | 1 | — | normal |
| 40105 / 0x9CA9 | `pf_c` | Pf C | int16 | 1 | — | normal |
| 40106 / 0x9CAA | `pf_sf` | PF SF | int16 | 1 | — | normal |
| 40107 / 0x9CAB | `energy_exported` | Energy Exported | uint32 | 1 | Wh | normal |
| 40109 / 0x9CAD | `energy_exported_a` | Energy Exported A | uint32 | 1 | Wh | normal |
| 40111 / 0x9CAF | `energy_exported_b` | Energy Exported B | uint32 | 1 | Wh | normal |
| 40113 / 0x9CB1 | `energy_exported_c` | Energy Exported C | uint32 | 1 | Wh | normal |
| 40115 / 0x9CB3 | `energy_imported` | Energy Imported | uint32 | 1 | Wh | normal |
| 40117 / 0x9CB5 | `energy_imported_a` | Energy Imported A | uint32 | 1 | Wh | normal |
| 40119 / 0x9CB7 | `energy_imported_b` | Energy Imported B | uint32 | 1 | Wh | normal |
| 40121 / 0x9CB9 | `energy_imported_c` | Energy Imported C | uint32 | 1 | Wh | normal |
| 40123 / 0x9CBB | `wh_sf` | WH SF | int16 | 1 | — | normal |

## Generic MQTT (JSON)

**id** `mqtt_json_generic` · **vendor** Generic · **model** MQTT JSON source · **version** 1.0.0 · **registers** 3

- **Transport:** FC03 (read holding registers) · byte order **big-endian, high word first (ABCD)**

> Starter map for a device that publishes JSON on MQTT (Shelly, Tasmota, Zigbee2MQTT, ESPHome, custom). Each measurement reads a topic and a json_path into the payload — edit these to match your device, then add or remove rows in Measurements.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 1 / 0x0001 | `power` | Power | float | 1 | W | normal |
| 2 / 0x0002 | `voltage` | Voltage | float | 1 | V | normal |
| 3 / 0x0003 | `current` | Current | float | 1 | A | normal |

## Schneider iEM3000 (3-phase)

**id** `schneider_iem3000` · **vendor** Schneider Electric · **model** iEM3155 / iEM3255 / iEM3455 / iEM3555 · **version** 1.0.0 · **registers** 22

- **Transport:** FC03 (read holding registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** volkszaehler/mbmd meters/rs485/iem3000.go (field-tested; cites Schneider DOCA0005). Official register list saved at docs/vendor/meters/schneider-iem3000-modbus-register-list.pdf (image-only PDF, not machine-parsed).

> Schneider Electric iEM3000-series DIN-rail energy meter (Modbus models iEM3150/3155/3250/3255/3350/3355/3450/3455/3550/3555). HOLDING registers (FC03), big-endian / high-word-first. Instantaneous values are Float32; energy is INT64. Addresses are 0-based PDU (= Schneider register number − 1, e.g. Active power total register 3060 → address 3059). Schneider reports power float32 in kW and energy int64 in Wh; scales convert to canonical W / Wh (power /0.001; energy is already Wh, scale 1). Voltage/current/frequency float32 are already in V/A/Hz (scale 1). Register map verified register-for-register against the field-tested volkszaehler/mbmd iem3000 driver (which cites Schneider DOCA0005). Power factor is omitted (mbmd disables it as unreliable on this series). This is the verified CORE measurement set; the device also exposes many more registers (THD, min/max, demand, per-tariff energy) — add them via the Template Manager once you have your unit's full DOCA0005 register list.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 2999 / 0x0BB7 | `current_l1` | Current L1 | float | 1 | A | realtime |
| 3001 / 0x0BB9 | `current_l2` | Current L2 | float | 1 | A | realtime |
| 3003 / 0x0BBB | `current_l3` | Current L3 | float | 1 | A | realtime |
| 3009 / 0x0BC1 | `current_avg` | Current average | float | 1 | A | normal |
| 3027 / 0x0BD3 | `voltage_l1_n` | Voltage L1-N | float | 1 | V | realtime |
| 3029 / 0x0BD5 | `voltage_l2_n` | Voltage L2-N | float | 1 | V | realtime |
| 3031 / 0x0BD7 | `voltage_l3_n` | Voltage L3-N | float | 1 | V | realtime |
| 3035 / 0x0BDB | `voltage_ln_avg` | Voltage L-N average | float | 1 | V | normal |
| 3053 / 0x0BED | `power_active_l1` | Active power L1 | float | 0.001 | W | realtime |
| 3055 / 0x0BEF | `power_active_l2` | Active power L2 | float | 0.001 | W | realtime |
| 3057 / 0x0BF1 | `power_active_l3` | Active power L3 | float | 0.001 | W | realtime |
| 3059 / 0x0BF3 | `power_active_total` | Active power total | float | 0.001 | W | realtime |
| 3067 / 0x0BFB | `power_reactive_total` | Reactive power total | float | 0.001 | var | normal |
| 3075 / 0x0C03 | `power_apparent_total` | Apparent power total | float | 0.001 | VA | normal |
| 3109 / 0x0C25 | `frequency` | Frequency | float | 1 | Hz | realtime |
| 3203 / 0x0C83 | `energy_active_import` | Active energy import (total) | int64 | 1.0 | Wh | slow |
| 3207 / 0x0C87 | `energy_active_export` | Active energy export (total) | int64 | 1.0 | Wh | slow |
| 3219 / 0x0C93 | `energy_reactive_import` | Reactive energy import (total) | int64 | 1.0 | varh | slow |
| 3223 / 0x0C97 | `energy_reactive_export` | Reactive energy export (total) | int64 | 1.0 | varh | slow |
| 3517 / 0x0DBD | `energy_active_import_l1` | Active energy import L1 | int64 | 1.0 | Wh | slow |
| 3521 / 0x0DC1 | `energy_active_import_l2` | Active energy import L2 | int64 | 1.0 | Wh | slow |
| 3525 / 0x0DC5 | `energy_active_import_l3` | Active energy import L3 | int64 | 1.0 | Wh | slow |

## Zigbee sensor (zigbee2mqtt)

**id** `zigbee2mqtt_sensor` · **vendor** Zigbee2MQTT · **model** climate / battery sensor · **version** 1.0.0 · **registers** 6

- **Transport:** FC03 (read holding registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** https://www.zigbee2mqtt.io/guide/usage/exposes.html

> A Zigbee sensor bridged by zigbee2mqtt (Sonoff SNZB, Aqara, Tuya, Xiaomi…). Set the device connection topic to zigbee2mqtt/<friendly_name> — z2m publishes one JSON payload there and every row below reads a field from it (standard z2m exposes; field names per zigbee2mqtt.io). Rows are editable: remove what your sensor lacks, add plug fields (power/current/energy/state) or occupancy/contact for other device classes.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 1 / 0x0001 | `temperature` | Temperature | float | 1 | °C | normal |
| 2 / 0x0002 | `humidity` | Humidity | float | 1 | % | normal |
| 3 / 0x0003 | `pressure` | Pressure | float | 1 | hPa | normal |
| 4 / 0x0004 | `battery` | Battery | float | 1 | % | normal |
| 5 / 0x0005 | `voltage` | Battery voltage | float | 1 | mV | normal |
| 6 / 0x0006 | `linkquality` | Link quality | float | 1 | lqi | normal |
