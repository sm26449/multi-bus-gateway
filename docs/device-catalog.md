# Device catalog — bundled register maps & provenance

> **Generated** by `tools/gen_device_catalog.py` from `multibus/device_templates/*.json`. Do not edit by hand — re-run the generator after changing a template.

Every built-in device map, with its Modbus transport (function code + byte/word order), the exact register table, and the **source it was verified against**. We do not fabricate maps — each entry cites its provenance. **Confidence varies by entry:** some are *vendor-verified* (confirmed against the manufacturer manual or a field-tested driver — e.g. ABB B23 vs the ABB manual, Schneider iEM3000 vs volkszaehler/mbmd, Carlo Gavazzi EM24 vs Victron), while others are *community-sourced* and their description says to verify against your specific unit's manual before billing-grade use (e.g. the Eastron SDM entries). Read each entry's Source line. `scale` is a divisor — engineering value = raw / scale.

**19 device maps.**

| Map | Vendor | Model | Registers | Transport |
|---|---|---|---|---|
| [Janitza UMG 512-PRO](#janitza-umg-512-pro) | Janitza electronics GmbH | UMG 512-PRO | 4126 | FC03 / big |
| [ABB B21 (single-phase)](#abb-b21-single-phase) | ABB | B21 (System pro M compact) | 10 | FC03 / big |
| [ABB B23 (3-phase)](#abb-b23-3-phase) | ABB | B23 (System pro M compact) | 32 | FC03 / big |
| [BLE sensor (Theengs / BTHome → MQTT)](#ble-sensor-theengs--bthome--mqtt) | Theengs | BLE advertisement sensor | 5 | MQTT |
| [Carlo Gavazzi EM24 (AV5/AV53, 3-phase)](#carlo-gavazzi-em24-av5av53-3-phase) | Carlo Gavazzi | EM24-DIN AV5(3) | 17 | FC03 / little |
| [Eastron SDM120 (single-phase)](#eastron-sdm120-single-phase) | Eastron | SDM120 Modbus | 10 | FC04 / big |
| [Eastron SDM630 (3-phase)](#eastron-sdm630-3-phase) | Eastron | SDM630 Modbus V2 | 29 | FC04 / big |
| [Fronius Smart Meter 65A-3 (RTU)](#fronius-smart-meter-65a-3-rtu) | Fronius | Smart Meter 65A-3 | 30 | FC03 / little |
| [Fronius inverter (Solar API / HTTP-JSON)](#fronius-inverter-solar-api--http-json) | — | — | 7 | HTTP/JSON |
| [Fronius inverter, per-phase AC (Solar API / HTTP-JSON)](#fronius-inverter-per-phase-ac-solar-api--http-json) | — | — | 6 | HTTP/JSON |
| [Fronius installation (Solar API site totals)](#fronius-installation-solar-api-site-totals) | — | — | 9 | HTTP/JSON |
| [Fronius SunSpec inverter (int+SF, via datalogger)](#fronius-sunspec-inverter-intsf-via-datalogger) | Fronius | Symo / Primo / Eco (SunSpec 103) | 69 | FC03 / big |
| [Fronius SunSpec meter (int+SF, via datalogger)](#fronius-sunspec-meter-intsf-via-datalogger) | Fronius | Smart Meter 63A/50kA (SunSpec 203) | 48 | FC03 / big |
| [Generic MQTT (JSON)](#generic-mqtt-json) | Generic | MQTT JSON source | 3 | MQTT |
| [Schneider iEM3000 (3-phase)](#schneider-iem3000-3-phase) | Schneider Electric | iEM3155 / iEM3255 / iEM3455 / iEM3555 | 22 | FC03 / big |
| [Seplos BMS V3 bank (via seplos-bms-mqtt)](#seplos-bms-v3-bank-via-seplos-bms-mqtt) | Seplos | BMS V3 bank | 28 | MQTT |
| [Seplos BMS V3 (via seplos-bms-mqtt)](#seplos-bms-v3-via-seplos-bms-mqtt) | Seplos | BMS V3 | 58 | MQTT |
| [Seplos BMS V3 pack (RTU tap, listen-only)](#seplos-bms-v3-pack-rtu-tap-listen-only) | Seplos | BMS V3 | 84 | RTU tap FC01+FC04 / big |
| [Zigbee sensor (zigbee2mqtt)](#zigbee-sensor-zigbee2mqtt) | Zigbee2MQTT | climate / battery sensor | 6 | MQTT |

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

- **Transport:** MQTT — values by `json_path` from the subscribed payload
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

## Fronius inverter (Solar API / HTTP-JSON)

**id** `fronius_solar_api_inverter` · **vendor** — · **model** — · **version** — · **registers** 7

- **Transport:** HTTP/JSON — values by `json_path`

> ONE HTTP request per inverter to the DataManager's Solar API (GetInverterRealtimeData.cgi?Scope=Device&DeviceId=${unit_id}&DataCollection=CommonInverterData).

Measured on a production DataManager 2026-09-12: 54 ms median per call (25 calls, worst 197 ms) against 1945-2376 ms for a single Modbus read of the same box, and those calls over live Modbus polling caused zero overruns and left Modbus latency unchanged - the web server answers from the cache it fills off its own RS-485 side. That cache refreshes about every 2.6 s, so polling faster than 5 s buys nothing real.

This collection carries ONE ac voltage and ONE ac current, not per phase: the per-phase values live in a different collection (3PInverterData) and would cost a second request. They stay on Modbus, together with the four things the Solar API cannot give at all - power factor, reactive and apparent power, the SunSpec event flags, and the per-string MPPT block.

NIGHT BEHAVIOUR, measured 2026-09-12 19:31: when an inverter stops producing, the DataManager OMITS PAC, FAC, UAC and IAC from the response entirely — it does not return them as zero, and the call still succeeds with Status.Code 0. Only UDC, IDC and the energy counters remain. So this source simply stops offering those fields at dusk, which is correct and needs no special handling: under the source model a field nobody offers falls to whichever source still does, and a SunSpec source beside this one keeps supplying them all night. Do not 'fix' the absence by defaulting them to zero — a fabricated zero is indistinguishable from a real one.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 1 / 0x0001 | `power_active_total` | Total active power | float | 1 | W | realtime |
| 2 / 0x0002 | `frequency` | Frequency | float | 1 | Hz | realtime |
| 3 / 0x0003 | `voltage_ln_avg` | AC voltage | float | 1 | V | realtime |
| 4 / 0x0004 | `current_total` | Total AC current | float | 1 | A | realtime |
| 5 / 0x0005 | `voltage_dc` | DC voltage | float | 1 | V | realtime |
| 6 / 0x0006 | `current_dc` | DC current | float | 1 | A | realtime |
| 7 / 0x0007 | `energy_active_generated` | Total energy generated | float | 1 | Wh | normal |

## Fronius inverter, per-phase AC (Solar API / HTTP-JSON)

**id** `fronius_solar_api_inverter_3p` · **vendor** — · **model** — · **version** — · **registers** 6

- **Transport:** HTTP/JSON — values by `json_path`

> ONE HTTP request per inverter to the DataManager's Solar API (GetInverterRealtimeData.cgi?Scope=Device&DeviceId=${unit_id}&DataCollection=3PInverterData): the three line-to-neutral voltages and the three phase currents that CommonInverterData does not carry.

Why a second source (2026-09-14): the over-voltage protection (Node-RED OV, the gateway's ov-u* rules, alertd's ANRE rules) keys on max(L1, L2, L3). On SunSpec those live in the 'normal' block, and four inverters sharing one datalogger could not sweep that block under ~25 s (measured 21-33 s at a 20 s interval, sources flapping ok/degraded ~50 times per unit per 3 h). This call costs the web server ~54 ms and does not touch the Modbus side, so the phase voltages arrive every 5 s and the SunSpec block can slow to 60 s for the things only it carries (PF, VA/var, event flags, temperatures, MPPT).

The addresses are the SunSpec model-101/103 addresses of the same points on purpose: the field's identity (HA unique_id, InfluxDB address tag) does not change with the source that happens to own it. Declare this source BEFORE the SunSpec one so it owns the six fields; keep stale_after_s so SunSpec fills them when the DataManager's web server stalls or sleeps at night.

NIGHT: like CommonInverterData, the DataManager omits UAC_*/IAC_* once the inverter stops producing (the call still succeeds); the fields then fall to the SunSpec source. Do not default them to zero.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 40072 / 0x9C88 | `current_l1` | L1 current | float | 1 | A | realtime |
| 40073 / 0x9C89 | `current_l2` | L2 current | float | 1 | A | realtime |
| 40074 / 0x9C8A | `current_l3` | L3 current | float | 1 | A | realtime |
| 40079 / 0x9C8F | `voltage_l1_n` | L1-N voltage | float | 1 | V | realtime |
| 40080 / 0x9C90 | `voltage_l2_n` | L2-N voltage | float | 1 | V | realtime |
| 40081 / 0x9C91 | `voltage_l3_n` | L3-N voltage | float | 1 | V | realtime |

## Fronius installation (Solar API site totals)

**id** `fronius_solar_api_site` · **vendor** — · **model** — · **version** — · **registers** 9

- **Transport:** HTTP/JSON — values by `json_path`

> The whole INSTALLATION in one call — GetPowerFlowRealtimeData.fcgi — not a device on a bus.

Measured on a production DataManager 2026-09-13: 89 ms per call, and the values behind it refresh every 2.5-3.3 s. Polling faster than about 2 s returns the same number twice; the datalogger polls its own RS-485 side on its own rhythm and we cannot outrun it.

What makes this worth a source of its own: house load, autonomy and self-consumption are NOT derivable from the inverters. Nothing in an inverter's register map knows what the house consumed. Sum the inverters and you get generation; you still cannot say how much of it stayed on site.

Grid and battery power ship UNTICKED. A meter wired at the grid connection reports the same flow first-hand and faster (measured: 1.1 s against 2.5 s here), so taking it from the site view would be a slower second copy. Tick them only when there is no such meter.

Sign convention follows the source: grid negative while exporting, battery negative while charging. Flipping it would make our number disagree with the inverter's own display.

The URL carries no ${unit_id}: an installation is one thing, so this group holds exactly one unit.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 1 / 0x0001 | `power_pv` | PV generation | float | 1 | W | realtime |
| 2 / 0x0002 | `power_load` | House load | float | 1 | W | realtime |
| 3 / 0x0003 | `power_grid` | Grid power | float | 1 | W | realtime |
| 4 / 0x0004 | `power_battery` | Battery power | float | 1 | W | realtime |
| 5 / 0x0005 | `autonomy` | Autonomy | float | 1 | % | realtime |
| 6 / 0x0006 | `self_consumption` | Self-consumption | float | 1 | % | realtime |
| 7 / 0x0007 | `energy_today` | Energy today | float | 1 | Wh | normal |
| 8 / 0x0008 | `energy_year` | Energy this year | float | 1 | Wh | normal |
| 9 / 0x0009 | `energy_lifetime` | Energy lifetime | float | 1 | Wh | normal |

## Fronius SunSpec inverter (int+SF, via datalogger)

**id** `fronius_sunspec_inverter` · **vendor** Fronius · **model** Symo / Primo / Eco (SunSpec 103) · **version** 2.2.0 · **registers** 69

- **Transport:** FC03 (read holding registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** SunSpec Information Models (int+SF); register map verified live against a production Fronius fleet (raw-frame decode parity, 2026-09-11)

> Fronius inverter read THROUGH its DataManager/datalogger over Modbus TCP, in the SunSpec int + scale-factor register mode (models 1/103/160: AC block, DC block, temperatures, status/events, per-string MPPT, identity). Registers are CANONICAL: this device publishes the same topics/fields/measurements as every other MBG device (power/active/total, dc/power, mppt/1/power …); a legacy SunSpec-name tree (…/W, …/PhVphA) is available via mqtt.compat_aliases. Use this when the datalogger's Modbus TCP slave is enabled and set to 'int+SF' (the Fronius default). For SEVERAL inverters behind one datalogger, add a `endpoints:` entry with this template and the unit IDs (1, 2, ...) — each unit becomes its own device. CAUTION: dataloggers serve only a few concurrent Modbus clients; if another system polls the same datalogger, keep poll intervals modest (10-15 s) or reduce the number of units read in parallel. Power factor is normalized to the ±1 fraction every other gateway device publishes (SunSpec reports it as a percentage, so the map carries scale: 100 on top of the dynamic scale factor). The operating state is also shipped decoded: status/text (vendor wording), status/alarm and status/active are derived measurements the template brings with it, so every unit of an endpoint speaks the same status without anyone retyping a formula. Register selection is what this hardware actually answers: the model-103 DC current/voltage and every temperature point (cabinet, heatsink, transformer, other, and both MPPT probes) read the SunSpec not-implemented sentinel on a Symo and are left out of the curated set — they stay in the map, selectable, for models that do implement them. The MPPT block sits 135 registers past the AC block, so it can never share a read with it; it polls on its own slower cadence instead of making every AC sweep cost two transactions.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 40004 / 0x9C44 | `manufacturer` | Device manufacturer | string:16 | 1 | — | static |
| 40020 / 0x9C54 | `model` | Device model name | string:16 | 1 | — | static |
| 40052 / 0x9C74 | `serial` | Meter serial / identification | string:16 | 1 | — | static |
| 40071 / 0x9C87 | `current_total` | Total / sum current | uint16 | 1 | A | normal |
| 40072 / 0x9C88 | `current_l1` | L1 current | uint16 | 1 | A | normal |
| 40073 / 0x9C89 | `current_l2` | L2 current | uint16 | 1 | A | normal |
| 40074 / 0x9C8A | `current_l3` | L3 current | uint16 | 1 | A | normal |
| 40075 / 0x9C8B | `a_sf` | A Scale Factor | int16 | 1 | — | normal |
| 40076 / 0x9C8C | `voltage_l1_l2` | L1–L2 line voltage | uint16 | 1 | V | normal |
| 40077 / 0x9C8D | `voltage_l2_l3` | L2–L3 line voltage | uint16 | 1 | V | normal |
| 40078 / 0x9C8E | `voltage_l3_l1` | L3–L1 line voltage | uint16 | 1 | V | normal |
| 40079 / 0x9C8F | `voltage_l1_n` | L1–N RMS voltage | uint16 | 1 | V | normal |
| 40080 / 0x9C90 | `voltage_l2_n` | L2–N RMS voltage | uint16 | 1 | V | normal |
| 40081 / 0x9C91 | `voltage_l3_n` | L3–N RMS voltage | uint16 | 1 | V | normal |
| 40082 / 0x9C92 | `v_sf` | V Scale Factor | int16 | 1 | — | normal |
| 40083 / 0x9C93 | `power_active_total` | Total active power | int16 | 1 | W | normal |
| 40084 / 0x9C94 | `w_sf` | W Scale Factor | int16 | 1 | — | normal |
| 40085 / 0x9C95 | `frequency` | Grid frequency | uint16 | 1 | Hz | normal |
| 40086 / 0x9C96 | `hz_sf` | HZ Scale Factor | int16 | 1 | — | normal |
| 40087 / 0x9C97 | `power_apparent_total` | Total apparent power | int16 | 1 | VA | normal |
| 40088 / 0x9C98 | `va_sf` | VA Scale Factor | int16 | 1 | — | normal |
| 40089 / 0x9C99 | `power_reactive_total` | Total reactive power | int16 | 1 | var | normal |
| 40090 / 0x9C9A | `var_sf` | VAR Scale Factor | int16 | 1 | — | normal |
| 40091 / 0x9C9B | `power_factor_total` | System power factor | int16 | 100 | — | normal |
| 40092 / 0x9C9C | `pf_sf` | PF Scale Factor | int16 | 1 | — | normal |
| 40093 / 0x9C9D | `energy_active_generated` | Lifetime generated active energy | uint32 | 1 | Wh | normal |
| 40095 / 0x9C9F | `wh_sf` | WH Scale Factor | int16 | 1 | — | normal |
| 40096 / 0x9CA0 | `current_dc` | DC bus current | uint16 | 1 | A | normal |
| 40097 / 0x9CA1 | `dca_sf` | DCA Scale Factor | int16 | 1 | — | normal |
| 40098 / 0x9CA2 | `voltage_dc` | DC bus voltage | uint16 | 1 | V | normal |
| 40099 / 0x9CA3 | `dcv_sf` | DCV Scale Factor | int16 | 1 | — | normal |
| 40100 / 0x9CA4 | `power_dc` | DC bus power | int16 | 1 | W | normal |
| 40101 / 0x9CA5 | `dcw_sf` | DCW Scale Factor | int16 | 1 | — | normal |
| 40102 / 0x9CA6 | `temperature_cabinet` | Cabinet temperature | int16 | 1 | °C | normal |
| 40103 / 0x9CA7 | `temperature_heatsink` | Heatsink temperature | int16 | 1 | °C | normal |
| 40104 / 0x9CA8 | `temperature_transformer` | Transformer temperature | int16 | 1 | °C | normal |
| 40105 / 0x9CA9 | `temperature_other` | Other/auxiliary temperature | int16 | 1 | °C | normal |
| 40106 / 0x9CAA | `tmp_sf` | TMP Scale Factor | int16 | 1 | — | normal |
| 40107 / 0x9CAB | `operating_state` | Operating state code (e.g. SunSpec St) | uint16 | 1 | — | normal |
| 40108 / 0x9CAC | `vendor_state` | Vendor-specific state code | uint16 | 1 | — | normal |
| 40109 / 0x9CAD | `event_flags_1` | Standard event flags word 1 | uint32 | 1 | — | normal |
| 40111 / 0x9CAF | `event_flags_2` | Standard event flags word 2 | uint32 | 1 | — | normal |
| 40113 / 0x9CB1 | `vendor_event_flags_1` | Vendor event flags word 1 | uint32 | 1 | — | normal |
| 40115 / 0x9CB3 | `vendor_event_flags_2` | Vendor event flags word 2 | uint32 | 1 | — | normal |
| 40117 / 0x9CB5 | `vendor_event_flags_3` | Vendor event flags word 3 | uint32 | 1 | — | normal |
| 40119 / 0x9CB7 | `vendor_event_flags_4` | Vendor event flags word 4 | uint32 | 1 | — | normal |
| 40227 / 0x9D23 | `controls_model_id` | SunSpec model id (123) | uint16 | 1 | — | controls |
| 40231 / 0x9D27 | `controls_connected` | Connected (model 123) | uint16 | 1 | — | controls |
| 40232 / 0x9D28 | `power_limit_pct` | Active power limit | uint16 | 1 | % | controls |
| 40233 / 0x9D29 | `power_limit_win_s` | Power limit window | uint16 | 1 | s | controls |
| 40234 / 0x9D2A | `power_limit_revert_s` | Power limit reverts after | uint16 | 1 | s | controls |
| 40235 / 0x9D2B | `power_limit_ramp_s` | Power limit ramp time | uint16 | 1 | s | controls |
| 40236 / 0x9D2C | `power_limit_enabled` | Power limit enabled | uint16 | 1 | — | controls |
| 40250 / 0x9D3A | `wmaxlimpct_sf` | WMaxLimPct Scale Factor | int16 | 1 | — | controls |
| 40255 / 0x9D3F | `dca_mppt_sf` | DCA MPPT Scale Factor | int16 | 1 | — | slow |
| 40256 / 0x9D40 | `dcv_mppt_sf` | DCV MPPT Scale Factor | int16 | 1 | — | slow |
| 40257 / 0x9D41 | `dcw_mppt_sf` | DCW MPPT Scale Factor | int16 | 1 | — | slow |
| 40258 / 0x9D42 | `dcwh_mppt_sf` | DCWH MPPT Scale Factor | int16 | 1 | — | slow |
| 40261 / 0x9D45 | `mppt_modules` | Number of MPPT modules/strings | uint16 | 1 | — | slow |
| 40272 / 0x9D50 | `current_dc_mppt1` | MPPT string 1 DC current | uint16 | 1 | A | slow |
| 40273 / 0x9D51 | `voltage_dc_mppt1` | MPPT string 1 DC voltage | uint16 | 1 | V | slow |
| 40274 / 0x9D52 | `power_dc_mppt1` | MPPT string 1 DC power | uint16 | 1 | W | slow |
| 40275 / 0x9D53 | `energy_dc_mppt1` | MPPT string 1 lifetime DC energy | uint32 | 1 | Wh | slow |
| 40279 / 0x9D57 | `temperature_mppt1` | MPPT string 1 temperature | int16 | 1 | °C | normal |
| 40292 / 0x9D64 | `current_dc_mppt2` | MPPT string 2 DC current | uint16 | 1 | A | slow |
| 40293 / 0x9D65 | `voltage_dc_mppt2` | MPPT string 2 DC voltage | uint16 | 1 | V | slow |
| 40294 / 0x9D66 | `power_dc_mppt2` | MPPT string 2 DC power | uint16 | 1 | W | slow |
| 40295 / 0x9D67 | `energy_dc_mppt2` | MPPT string 2 lifetime DC energy | uint32 | 1 | Wh | slow |
| 40299 / 0x9D6B | `temperature_mppt2` | MPPT string 2 temperature | int16 | 1 | °C | normal |

**Derived measurements** — computed from the registers above and seeded into every device made from this template, so each unit publishes them identically.

| Name | MQTT topic | Formula | Decoded |
|---|---|---|---|
| `status_text` | `status/text` | `operating_state` | 13 states |
| `status_alarm` | `status/alarm` | `1 if (operating_state == 5 or operating_state == 7 or operating_state == 9 or operating_state == 10 or operating_state == 11 or operating_state == 13) else 0` | — |
| `status_active` | `status/active` | `1 if (operating_state == 4 or operating_state == 5) else 0` | — |

## Fronius SunSpec meter (int+SF, via datalogger)

**id** `fronius_sunspec_meter` · **vendor** Fronius · **model** Smart Meter 63A/50kA (SunSpec 203) · **version** 2.1.0 · **registers** 48

- **Transport:** FC03 (read holding registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** SunSpec Information Models (int+SF); register map verified live against a production Fronius fleet (raw-frame decode parity, 2026-09-11)

> Fronius Smart Meter read THROUGH the DataManager/datalogger over Modbus TCP (SunSpec model 203, int + scale factor; full 3-phase set + per-phase import/export energies). Registers are CANONICAL (voltage/l1_n, power/active/total, energy/active/import …) — same output shape as every other MBG meter; the legacy SunSpec-name tree is available via mqtt.compat_aliases. The meter appears on the datalogger at unit ID 240 (default; 241/242 for additional meters). Use this when the meter hangs off a Fronius datalogger — for a Smart Meter wired DIRECTLY to your own RS-485 (RTU or an RTU-TCP bridge), use the separate 'Fronius Smart Meter 65A-3 (RTU)' template instead: same hardware, completely different register map. Power factor is normalized to the ±1 fraction every other gateway device publishes (SunSpec reports it as a percentage, so the map carries scale: 100 on top of the dynamic scale factor).

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 40004 / 0x9C44 | `manufacturer` | Device manufacturer | string:16 | 1 | — | static |
| 40020 / 0x9C54 | `model` | Device model name | string:16 | 1 | — | static |
| 40052 / 0x9C74 | `serial` | Meter serial / identification | string:16 | 1 | — | static |
| 40071 / 0x9C87 | `current_total` | Total / sum current | int16 | 1 | A | normal |
| 40072 / 0x9C88 | `current_l1` | L1 current | int16 | 1 | A | normal |
| 40073 / 0x9C89 | `current_l2` | L2 current | int16 | 1 | A | normal |
| 40074 / 0x9C8A | `current_l3` | L3 current | int16 | 1 | A | normal |
| 40075 / 0x9C8B | `a_sf` | A Scale Factor | int16 | 1 | — | normal |
| 40076 / 0x9C8C | `voltage_ln_avg` | Average phase-to-neutral voltage | int16 | 1 | V | normal |
| 40077 / 0x9C8D | `voltage_l1_n` | L1–N RMS voltage | int16 | 1 | V | normal |
| 40078 / 0x9C8E | `voltage_l2_n` | L2–N RMS voltage | int16 | 1 | V | normal |
| 40079 / 0x9C8F | `voltage_l3_n` | L3–N RMS voltage | int16 | 1 | V | normal |
| 40080 / 0x9C90 | `voltage_ll_avg` | Average line-to-line voltage | int16 | 1 | V | normal |
| 40081 / 0x9C91 | `voltage_l1_l2` | L1–L2 line voltage | int16 | 1 | V | normal |
| 40082 / 0x9C92 | `voltage_l2_l3` | L2–L3 line voltage | int16 | 1 | V | normal |
| 40083 / 0x9C93 | `voltage_l3_l1` | L3–L1 line voltage | int16 | 1 | V | normal |
| 40084 / 0x9C94 | `v_sf` | V Scale Factor | int16 | 1 | — | normal |
| 40085 / 0x9C95 | `frequency` | Grid frequency | int16 | 1 | Hz | normal |
| 40086 / 0x9C96 | `hz_sf` | HZ Scale Factor | int16 | 1 | — | normal |
| 40087 / 0x9C97 | `power_active_total` | Total active power | int16 | 1 | W | normal |
| 40088 / 0x9C98 | `power_active_l1` | L1 active power | int16 | 1 | W | normal |
| 40089 / 0x9C99 | `power_active_l2` | L2 active power | int16 | 1 | W | normal |
| 40090 / 0x9C9A | `power_active_l3` | L3 active power | int16 | 1 | W | normal |
| 40091 / 0x9C9B | `w_sf` | W Scale Factor | int16 | 1 | — | normal |
| 40092 / 0x9C9C | `power_apparent_total` | Total apparent power | int16 | 1 | VA | normal |
| 40093 / 0x9C9D | `power_apparent_l1` | L1 apparent power | int16 | 1 | VA | normal |
| 40094 / 0x9C9E | `power_apparent_l2` | L2 apparent power | int16 | 1 | VA | normal |
| 40095 / 0x9C9F | `power_apparent_l3` | L3 apparent power | int16 | 1 | VA | normal |
| 40096 / 0x9CA0 | `va_sf` | VA Scale Factor | int16 | 1 | — | normal |
| 40097 / 0x9CA1 | `power_reactive_total` | Total reactive power | int16 | 1 | var | normal |
| 40098 / 0x9CA2 | `power_reactive_l1` | L1 reactive power | int16 | 1 | var | normal |
| 40099 / 0x9CA3 | `power_reactive_l2` | L2 reactive power | int16 | 1 | var | normal |
| 40100 / 0x9CA4 | `power_reactive_l3` | L3 reactive power | int16 | 1 | var | normal |
| 40101 / 0x9CA5 | `var_sf` | VAR Scale Factor | int16 | 1 | — | normal |
| 40102 / 0x9CA6 | `power_factor_total` | System power factor | int16 | 100 | — | normal |
| 40103 / 0x9CA7 | `power_factor_l1` | L1 power factor | int16 | 100 | — | normal |
| 40104 / 0x9CA8 | `power_factor_l2` | L2 power factor | int16 | 100 | — | normal |
| 40105 / 0x9CA9 | `power_factor_l3` | L3 power factor | int16 | 100 | — | normal |
| 40106 / 0x9CAA | `pf_sf` | PF Scale Factor | int16 | 1 | — | normal |
| 40107 / 0x9CAB | `energy_active_export` | Total exported active energy | uint32 | 1 | Wh | normal |
| 40109 / 0x9CAD | `energy_active_export_l1` | L1 exported active energy | uint32 | 1 | Wh | normal |
| 40111 / 0x9CAF | `energy_active_export_l2` | L2 exported active energy | uint32 | 1 | Wh | normal |
| 40113 / 0x9CB1 | `energy_active_export_l3` | L3 exported active energy | uint32 | 1 | Wh | normal |
| 40115 / 0x9CB3 | `energy_active_import` | Total imported active energy | uint32 | 1 | Wh | normal |
| 40117 / 0x9CB5 | `energy_active_import_l1` | L1 imported active energy | uint32 | 1 | Wh | normal |
| 40119 / 0x9CB7 | `energy_active_import_l2` | L2 imported active energy | uint32 | 1 | Wh | normal |
| 40121 / 0x9CB9 | `energy_active_import_l3` | L3 imported active energy | uint32 | 1 | Wh | normal |
| 40123 / 0x9CBB | `wh_sf` | WH Scale Factor | int16 | 1 | — | normal |

## Generic MQTT (JSON)

**id** `mqtt_json_generic` · **vendor** Generic · **model** MQTT JSON source · **version** 1.0.0 · **registers** 3

- **Transport:** MQTT — values by `json_path` from the subscribed payload

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

## Seplos BMS V3 bank (via seplos-bms-mqtt)

**id** `seplos_bms_bank_mqtt` · **vendor** Seplos · **model** BMS V3 bank · **version** 1.0 · **registers** 28

- **Transport:** MQTT — values by `json_path` from the subscribed payload

> The whole battery BANK as one device: the seplos-bms-mqtt collector aggregates its packs under seplos/pack/pack_* (sums, averages, min/max, worst-cell). Set the device input topic to seplos/pack/# — register topics are relative. Pairs with seplos_bms_mqtt for per-pack detail.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 1 / 0x0001 | `average_soc` | State of charge (avg) | float | 1 | % | normal |
| 2 / 0x0002 | `total_power` | Total power | float | 1 | W | normal |
| 3 / 0x0003 | `total_voltage` | Bank voltage (avg) | float | 1 | V | normal |
| 4 / 0x0004 | `total_current` | Total current | float | 1 | A | normal |
| 5 / 0x0005 | `status` | Status | uint16 | 1 | — | normal |
| 6 / 0x0006 | `energy_remaining` | Energy remaining | float | 0.001 | Wh | normal |
| 7 / 0x0007 | `energy_to_full` | Energy to full | float | 0.001 | Wh | normal |
| 8 / 0x0008 | `batteries_online` | Packs online | float | 1 | — | normal |
| 9 / 0x0009 | `cell_delta` | Cell delta (worst) | float | 1 | mV | normal |
| 10 / 0x000A | `soc_spread` | SOC spread | float | 1 | % | normal |
| 11 / 0x000B | `min_soc` | SOC (min pack) | float | 1 | % | normal |
| 12 / 0x000C | `max_soc` | SOC (max pack) | float | 1 | % | normal |
| 13 / 0x000D | `min_soh` | SOH (min pack) | float | 1 | % | normal |
| 14 / 0x000E | `remaining_capacity` | Remaining capacity | float | 1 | Ah | normal |
| 15 / 0x000F | `total_capacity` | Total capacity | float | 1 | Ah | normal |
| 16 / 0x0010 | `max_cycles` | Cycles (max pack) | float | 1 | — | normal |
| 17 / 0x0011 | `avg_cell_voltage` | Cell voltage (avg) | float | 1 | V | normal |
| 18 / 0x0012 | `min_cell_voltage` | Cell voltage (min) | float | 1 | V | normal |
| 19 / 0x0013 | `max_cell_voltage` | Cell voltage (max) | float | 1 | V | normal |
| 20 / 0x0014 | `balancing_cells` | Cells balancing | float | 1 | — | normal |
| 21 / 0x0015 | `avg_temp` | Temperature (avg) | float | 1 | °C | normal |
| 22 / 0x0016 | `min_temp` | Temperature (min) | float | 1 | °C | normal |
| 23 / 0x0017 | `max_temp` | Temperature (max) | float | 1 | °C | normal |
| 24 / 0x0018 | `max_charge_current` | Charge current limit | float | 1 | A | normal |
| 25 / 0x0019 | `max_discharge_current` | Discharge current limit | float | 1 | A | normal |
| 26 / 0x001A | `total_alarms` | Active alarms | float | 1 | — | normal |
| 27 / 0x001B | `total_protections` | Active protections | float | 1 | — | normal |
| 28 / 0x001C | `last_update` | Last aggregate | string:8 | 1 | — | normal |

## Seplos BMS V3 (via seplos-bms-mqtt)

**id** `seplos_bms_mqtt` · **vendor** Seplos · **model** BMS V3 · **version** 1.0 · **registers** 58

- **Transport:** MQTT — values by `json_path` from the subscribed payload

> One battery pack of the seplos-bms-mqtt collector (https://github.com/sm2669/seplos-bms-mqtt): the collector passively snoops the RS485 pack bus and publishes seplos/battery_N/<field>; this template ingests the full field set over MQTT. Set the device input topic to seplos/battery_N/# — register topics are RELATIVE (~/).

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 1 / 0x0001 | `alarm_cell_overvolt` | Alarm Cell Overvolt | float | 1 | — | normal |
| 2 / 0x0002 | `alarm_cell_temp` | Alarm Cell Temp | float | 1 | — | normal |
| 3 / 0x0003 | `alarm_cell_undervolt` | Alarm Cell Undervolt | float | 1 | — | normal |
| 4 / 0x0004 | `alarm_count` | Alarm Count | float | 1 | — | normal |
| 5 / 0x0005 | `ambient_temp` | Ambient Temp | float | 1 | °C | normal |
| 6 / 0x0006 | `average_cell_temp` | Average Cell Temp | float | 1 | °C | normal |
| 7 / 0x0007 | `average_cell_voltage` | Average Cell Voltage | float | 1 | V | normal |
| 8 / 0x0008 | `balancing_bits` | Balancing Bits | float | 1 | — | normal |
| 9 / 0x0009 | `balancing_count` | Balancing Count | float | 1 | — | normal |
| 10 / 0x000A | `cell_1` | Cell 1 | float | 1 | V | normal |
| 11 / 0x000B | `cell_10` | Cell 10 | float | 1 | V | normal |
| 12 / 0x000C | `cell_11` | Cell 11 | float | 1 | V | normal |
| 13 / 0x000D | `cell_12` | Cell 12 | float | 1 | V | normal |
| 14 / 0x000E | `cell_13` | Cell 13 | float | 1 | V | normal |
| 15 / 0x000F | `cell_14` | Cell 14 | float | 1 | V | normal |
| 16 / 0x0010 | `cell_15` | Cell 15 | float | 1 | V | normal |
| 17 / 0x0011 | `cell_16` | Cell 16 | float | 1 | V | normal |
| 18 / 0x0012 | `cell_2` | Cell 2 | float | 1 | V | normal |
| 19 / 0x0013 | `cell_3` | Cell 3 | float | 1 | V | normal |
| 20 / 0x0014 | `cell_4` | Cell 4 | float | 1 | V | normal |
| 21 / 0x0015 | `cell_5` | Cell 5 | float | 1 | V | normal |
| 22 / 0x0016 | `cell_6` | Cell 6 | float | 1 | V | normal |
| 23 / 0x0017 | `cell_7` | Cell 7 | float | 1 | V | normal |
| 24 / 0x0018 | `cell_8` | Cell 8 | float | 1 | V | normal |
| 25 / 0x0019 | `cell_9` | Cell 9 | float | 1 | V | normal |
| 26 / 0x001A | `cell_delta` | Cell Delta | float | 1 | mV | normal |
| 27 / 0x001B | `cell_temp_1` | Cell Temp 1 | float | 1 | °C | normal |
| 28 / 0x001C | `cell_temp_2` | Cell Temp 2 | float | 1 | °C | normal |
| 29 / 0x001D | `cell_temp_3` | Cell Temp 3 | float | 1 | °C | normal |
| 30 / 0x001E | `cell_temp_4` | Cell Temp 4 | float | 1 | °C | normal |
| 31 / 0x001F | `current` | Current | float | 1 | A | normal |
| 32 / 0x0020 | `cycles` | Cycles | float | 1 | cycles | normal |
| 33 / 0x0021 | `failure_count` | Failure Count | float | 1 | — | normal |
| 34 / 0x0022 | `max_cell_temp` | Max Cell Temp | float | 1 | °C | normal |
| 35 / 0x0023 | `max_cell_voltage` | Max Cell Voltage | float | 1 | V | normal |
| 36 / 0x0024 | `maxchgcurt` | MaxChgCurt | float | 1 | A | normal |
| 37 / 0x0025 | `maxdiscurt` | MaxDisCurt | float | 1 | A | normal |
| 38 / 0x0026 | `min_cell_temp` | Min Cell Temp | float | 1 | °C | normal |
| 39 / 0x0027 | `min_cell_voltage` | Min Cell Voltage | float | 1 | V | normal |
| 40 / 0x0028 | `mosfet_temp` | MOSFET Temp | float | 1 | °C | normal |
| 41 / 0x0029 | `pack_voltage` | Pack Voltage | float | 1 | V | normal |
| 42 / 0x002A | `power` | Power | float | 1 | W | normal |
| 43 / 0x002B | `protection_count` | Protection Count | float | 1 | — | normal |
| 44 / 0x002C | `remaining_capacity` | Remaining Capacity | float | 1 | Ah | normal |
| 45 / 0x002D | `soc` | SOC | float | 1 | % | normal |
| 46 / 0x002E | `soh` | SOH | float | 1 | % | normal |
| 47 / 0x002F | `total_capacity` | Total Capacity | float | 1 | Ah | normal |
| 48 / 0x0030 | `total_discharge_capacity` | Total Discharge Capacity | float | 1 | Ah | normal |
| 49 / 0x0031 | `balancing_active` | Balancing Active | uint16 | 1 | — | normal |
| 50 / 0x0032 | `fet_charge` | FET Charge | uint16 | 1 | — | normal |
| 51 / 0x0033 | `fet_discharge` | FET Discharge | uint16 | 1 | — | normal |
| 52 / 0x0034 | `fet_heater` | FET Heater | uint16 | 1 | — | normal |
| 53 / 0x0035 | `heating_active` | Heating Active | uint16 | 1 | — | normal |
| 54 / 0x0036 | `fet_current_limit` | FET Current Limit | uint16 | 1 | — | normal |
| 55 / 0x0037 | `status` | Status | uint16 | 1 | — | normal |
| 56 / 0x0038 | `state` | Collector availability | uint16 | 1 | — | normal |
| 57 / 0x0039 | `balancing_cells` | Balancing cells | string:8 | 1 | — | normal |
| 58 / 0x003A | `last_update` | Last BMS frame | string:8 | 1 | — | normal |

## Seplos BMS V3 pack (RTU tap, listen-only)

**id** `seplos_bms_v3_rtu_tap` · **vendor** Seplos · **model** BMS V3 · **version** 1.8.1 · **registers** 84

- **Transport:** Modbus RTU **listen-only tap** (decodes another master's exchanges): FC01 (read coils), FC04 (read input registers) · byte order **big-endian, high word first (ABCD)**

> One Seplos V3 battery pack observed on the inter-pack RS485 bus via protocol rtu_tap (the master pack is the bus master — never poll this bus actively). Telemetry blocks PIA (0x1000, FC04) and PIB (0x1100, FC04); names mirror seplos_bms_mqtt 1:1 so the two read paths compare side by side. Alarm/status coils (PIC 0x1200, FC01) decode in BIT-address space: a coil register reads one bit, a uint16 coil packs 16 bits LSB-first — the same math the collector uses for its masks. Derived counts (alarm_count, balancing_count/cells) are Phase-2 calc registers. Set unit_id to the pack address (1..16). Publishing defaults are ON for every field the retired seplos-modbus-mqtt collector published (topics byte-identical under mqtt.topic_prefix seplos/battery_${unit_id}); the internal per-bit alarms stay MQTT-off and feed the counts.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 4608 / 0x1200 | `alarm_cell_undervolt` | Cell Undervolt Mask | uint16 | 1 | — | realtime |
| 4624 / 0x1210 | `alarm_cell_overvolt` | Cell Overvolt Mask | uint16 | 1 | — | realtime |
| 4640 / 0x1220 | `alarm_cell_temp` | Cell Temp Alarm Mask | uint16 | 1 | — | realtime |
| 4656 / 0x1230 | `balancing_bits` | Balancing Mask | uint16 | 1 | — | realtime |
| 4672 / 0x1240 | `status` | Status | uint16 | 1 | — | realtime |
| 4680 / 0x1248 | `alarm_cell_high_v` | Cell High V | uint16 | 1 | — | realtime |
| 4681 / 0x1249 | `alarm_cell_overvolt_prot` | Cell OV Prot | uint16 | 1 | — | realtime |
| 4682 / 0x124A | `alarm_cell_low_v` | Cell Low V | uint16 | 1 | — | realtime |
| 4683 / 0x124B | `alarm_cell_undervolt_prot` | Cell UV Prot | uint16 | 1 | — | realtime |
| 4684 / 0x124C | `alarm_pack_high_v` | Pack High V | uint16 | 1 | — | realtime |
| 4685 / 0x124D | `alarm_pack_overvolt_prot` | Pack OV Prot | uint16 | 1 | — | realtime |
| 4686 / 0x124E | `alarm_pack_low_v` | Pack Low V | uint16 | 1 | — | realtime |
| 4687 / 0x124F | `alarm_pack_undervolt_prot` | Pack UV Prot | uint16 | 1 | — | realtime |
| 4688 / 0x1250 | `alarm_charge_high_temp` | Chg High Temp | uint16 | 1 | — | realtime |
| 4689 / 0x1251 | `alarm_charge_overtemp_prot` | Chg OT Prot | uint16 | 1 | — | realtime |
| 4690 / 0x1252 | `alarm_charge_low_temp` | Chg Low Temp | uint16 | 1 | — | realtime |
| 4691 / 0x1253 | `alarm_charge_undertemp_prot` | Chg UT Prot | uint16 | 1 | — | realtime |
| 4692 / 0x1254 | `alarm_discharge_high_temp` | Dis High Temp | uint16 | 1 | — | realtime |
| 4693 / 0x1255 | `alarm_discharge_overtemp_prot` | Dis OT Prot | uint16 | 1 | — | realtime |
| 4694 / 0x1256 | `alarm_discharge_low_temp` | Dis Low Temp | uint16 | 1 | — | realtime |
| 4695 / 0x1257 | `alarm_discharge_undertemp_prot` | Dis UT Prot | uint16 | 1 | — | realtime |
| 4696 / 0x1258 | `alarm_ambient_high_temp` | Ambient High Temp | uint16 | 1 | — | realtime |
| 4697 / 0x1259 | `alarm_ambient_overtemp_prot` | Ambient OT Prot | uint16 | 1 | — | realtime |
| 4700 / 0x125C | `alarm_mosfet_high_temp` | MOSFET High Temp | uint16 | 1 | — | realtime |
| 4701 / 0x125D | `alarm_mosfet_overtemp_prot` | MOSFET OT Prot | uint16 | 1 | — | realtime |
| 4702 / 0x125E | `heating_active` | Heating | uint16 | 1 | — | realtime |
| 4704 / 0x1260 | `alarm_charge_current` | Chg Current | uint16 | 1 | — | realtime |
| 4705 / 0x1261 | `alarm_charge_overcurrent_prot` | Chg OC Prot | uint16 | 1 | — | realtime |
| 4706 / 0x1262 | `alarm_charge_overcurrent_2_prot` | Chg OC2 Prot | uint16 | 1 | — | realtime |
| 4707 / 0x1263 | `alarm_discharge_current` | Dis Current | uint16 | 1 | — | realtime |
| 4708 / 0x1264 | `alarm_discharge_overcurrent_prot` | Dis OC Prot | uint16 | 1 | — | realtime |
| 4709 / 0x1265 | `alarm_discharge_overcurrent_2_prot` | Dis OC2 Prot | uint16 | 1 | — | realtime |
| 4710 / 0x1266 | `alarm_short_circuit_prot` | Short Circuit Prot | uint16 | 1 | — | realtime |
| 4722 / 0x1272 | `alarm_soc_low` | SOC Low | uint16 | 1 | — | realtime |
| 4723 / 0x1273 | `alarm_soc_prot` | SOC Prot | uint16 | 1 | — | realtime |
| 4724 / 0x1274 | `alarm_cell_diff` | Cell Diff Alarm | uint16 | 1 | — | realtime |
| 4728 / 0x1278 | `fet_discharge` | FET Discharge | uint16 | 1 | — | realtime |
| 4729 / 0x1279 | `fet_charge` | FET Charge | uint16 | 1 | — | realtime |
| 4730 / 0x127A | `fet_current_limit` | FET Current Limit | uint16 | 1 | — | realtime |
| 4731 / 0x127B | `fet_heater` | FET Heater | uint16 | 1 | — | realtime |
| 4736 / 0x1280 | `balancing_active` | Balancing | uint16 | 1 | — | realtime |
| 4744 / 0x1288 | `failure_ntc` | Failure NTC | uint16 | 1 | — | realtime |
| 4745 / 0x1289 | `failure_afe` | Failure AFE | uint16 | 1 | — | realtime |
| 4746 / 0x128A | `failure_charge_mosfet` | Failure Chg MOSFET | uint16 | 1 | — | realtime |
| 4747 / 0x128B | `failure_discharge_mosfet` | Failure Dis MOSFET | uint16 | 1 | — | realtime |
| 4748 / 0x128C | `failure_cell_diff` | Failure Cell Diff | uint16 | 1 | — | realtime |
| 4096 / 0x1000 | `pack_voltage` | Pack Voltage | uint16 | 100 | V | realtime |
| 4097 / 0x1001 | `current` | Current (+ charging) | int16 | 100 | A | realtime |
| 4098 / 0x1002 | `remaining_capacity` | Remaining Capacity | uint16 | 100 | Ah | realtime |
| 4099 / 0x1003 | `total_capacity` | Total Capacity | uint16 | 100 | Ah | realtime |
| 4100 / 0x1004 | `total_discharge_capacity` | Total Discharge Capacity | uint16 | 0.1 | Ah | realtime |
| 4101 / 0x1005 | `soc` | SOC | uint16 | 10 | % | realtime |
| 4102 / 0x1006 | `soh` | SOH | uint16 | 10 | % | realtime |
| 4103 / 0x1007 | `cycles` | Cycles | uint16 | 1 | cycles | realtime |
| 4104 / 0x1008 | `average_cell_voltage` | Average Cell Voltage | uint16 | 1000 | V | realtime |
| 4105 / 0x1009 | `average_cell_temp` | Average Cell Temp | uint16 | 10 | °C | realtime |
| 4106 / 0x100A | `max_cell_voltage` | Max Cell Voltage | uint16 | 1000 | V | realtime |
| 4107 / 0x100B | `min_cell_voltage` | Min Cell Voltage | uint16 | 1000 | V | realtime |
| 4108 / 0x100C | `max_cell_temp` | Max Cell Temp | uint16 | 10 | °C | realtime |
| 4109 / 0x100D | `min_cell_temp` | Min Cell Temp | uint16 | 10 | °C | realtime |
| 4111 / 0x100F | `maxdiscurt` | Max discharge current (BMS limit) | uint16 | 1 | A | realtime |
| 4112 / 0x1010 | `maxchgcurt` | Max charge current (BMS limit) | uint16 | 1 | A | realtime |
| 4352 / 0x1100 | `cell_1` | Cell 1 | uint16 | 1000 | V | realtime |
| 4353 / 0x1101 | `cell_2` | Cell 2 | uint16 | 1000 | V | realtime |
| 4354 / 0x1102 | `cell_3` | Cell 3 | uint16 | 1000 | V | realtime |
| 4355 / 0x1103 | `cell_4` | Cell 4 | uint16 | 1000 | V | realtime |
| 4356 / 0x1104 | `cell_5` | Cell 5 | uint16 | 1000 | V | realtime |
| 4357 / 0x1105 | `cell_6` | Cell 6 | uint16 | 1000 | V | realtime |
| 4358 / 0x1106 | `cell_7` | Cell 7 | uint16 | 1000 | V | realtime |
| 4359 / 0x1107 | `cell_8` | Cell 8 | uint16 | 1000 | V | realtime |
| 4360 / 0x1108 | `cell_9` | Cell 9 | uint16 | 1000 | V | realtime |
| 4361 / 0x1109 | `cell_10` | Cell 10 | uint16 | 1000 | V | realtime |
| 4362 / 0x110A | `cell_11` | Cell 11 | uint16 | 1000 | V | realtime |
| 4363 / 0x110B | `cell_12` | Cell 12 | uint16 | 1000 | V | realtime |
| 4364 / 0x110C | `cell_13` | Cell 13 | uint16 | 1000 | V | realtime |
| 4365 / 0x110D | `cell_14` | Cell 14 | uint16 | 1000 | V | realtime |
| 4366 / 0x110E | `cell_15` | Cell 15 | uint16 | 1000 | V | realtime |
| 4367 / 0x110F | `cell_16` | Cell 16 | uint16 | 1000 | V | realtime |
| 4368 / 0x1110 | `cell_temp_1` | Cell Temp 1 | uint16 | 10 | °C | realtime |
| 4369 / 0x1111 | `cell_temp_2` | Cell Temp 2 | uint16 | 10 | °C | realtime |
| 4370 / 0x1112 | `cell_temp_3` | Cell Temp 3 | uint16 | 10 | °C | realtime |
| 4371 / 0x1113 | `cell_temp_4` | Cell Temp 4 | uint16 | 10 | °C | realtime |
| 4376 / 0x1118 | `ambient_temp` | Ambient Temp | uint16 | 10 | °C | realtime |
| 4377 / 0x1119 | `mosfet_temp` | MOSFET Temp | uint16 | 10 | °C | realtime |

**Derived measurements** — computed from the registers above and seeded into every device made from this template, so each unit publishes them identically.

| Name | MQTT topic | Formula | Decoded |
|---|---|---|---|
| `power` | `power` | `0 - current * pack_voltage` | — |
| `cell_delta` | `cell_delta` | `(max_cell_voltage - min_cell_voltage) * 1000` | — |
| `balancing_count` | `balancing_count` | `popcount(balancing_bits)` | — |
| `alarm_count` | `alarm_count` | `alarm_cell_high_v + alarm_cell_overvolt_prot + alarm_cell_low_v + alarm_cell_undervolt_prot + alarm_pack_high_v + alarm_pack_overvolt_prot + alarm_pack_low_v + alarm_pack_undervolt_prot + alarm_charge_high_temp + alarm_charge_overtemp_prot + alarm_charge_low_temp + alarm_charge_undertemp_prot + alarm_discharge_high_temp + alarm_discharge_overtemp_prot + alarm_discharge_low_temp + alarm_discharge_undertemp_prot + alarm_ambient_high_temp + alarm_ambient_overtemp_prot + alarm_mosfet_high_temp + alarm_mosfet_overtemp_prot + alarm_charge_current + alarm_charge_overcurrent_prot + alarm_discharge_current + alarm_discharge_overcurrent_prot + alarm_short_circuit_prot + alarm_soc_low + alarm_cell_diff` | — |
| `protection_count` | `protection_count` | `alarm_cell_overvolt_prot + alarm_cell_undervolt_prot + alarm_pack_overvolt_prot + alarm_pack_undervolt_prot + alarm_charge_overtemp_prot + alarm_charge_undertemp_prot + alarm_discharge_overtemp_prot + alarm_discharge_undertemp_prot + alarm_ambient_overtemp_prot + alarm_mosfet_overtemp_prot + alarm_charge_overcurrent_prot + alarm_charge_overcurrent_2_prot + alarm_discharge_overcurrent_prot + alarm_discharge_overcurrent_2_prot + alarm_short_circuit_prot + alarm_soc_prot` | — |
| `failure_count` | `failure_count` | `failure_ntc + failure_afe + failure_charge_mosfet + failure_discharge_mosfet + failure_cell_diff` | — |
| `energy_remaining` | `energy_remaining` | `remaining_capacity * pack_voltage` | — |
| `energy_to_full` | `energy_to_full` | `(total_capacity - remaining_capacity) * pack_voltage` | — |

## Zigbee sensor (zigbee2mqtt)

**id** `zigbee2mqtt_sensor` · **vendor** Zigbee2MQTT · **model** climate / battery sensor · **version** 1.0.0 · **registers** 6

- **Transport:** MQTT — values by `json_path` from the subscribed payload
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
