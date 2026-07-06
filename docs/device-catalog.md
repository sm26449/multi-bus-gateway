# Device catalog — bundled register maps & provenance

> **Generated** by `tools/gen_device_catalog.py` from `janitza/device_templates/*.json`. Do not edit by hand — re-run the generator after changing a template.

Every built-in device map, with its Modbus transport (function code + byte/word order), the exact register table, and the **source it was verified against**. We do not fabricate maps — each entry cites its provenance. **Confidence varies by entry:** some are *vendor-verified* (confirmed against the manufacturer manual or a field-tested driver — e.g. ABB B23 vs the ABB manual, Schneider iEM3000 vs volkszaehler/mbmd, Carlo Gavazzi EM24 vs Victron), while others are *community-sourced* and their description says to verify against your specific unit's manual before billing-grade use (e.g. the Eastron SDM entries). Read each entry's Source line. `scale` is a divisor — engineering value = raw / scale.

**7 device maps.**

| Map | Vendor | Model | Registers | Transport |
|---|---|---|---|---|
| [Janitza UMG 512-PRO](#janitza-umg-512-pro) | Janitza electronics GmbH | UMG 512-PRO | 4126 | FC03 / big |
| [ABB B21 (single-phase)](#abb-b21-single-phase) | ABB | B21 (System pro M compact) | 10 | FC03 / big |
| [ABB B23 (3-phase)](#abb-b23-3-phase) | ABB | B23 (System pro M compact) | 32 | FC03 / big |
| [Carlo Gavazzi EM24 (AV5/AV53, 3-phase)](#carlo-gavazzi-em24-av5av53-3-phase) | Carlo Gavazzi | EM24-DIN AV5(3) | 16 | FC03 / little |
| [Eastron SDM120 (single-phase)](#eastron-sdm120-single-phase) | Eastron | SDM120 Modbus | 10 | FC04 / big |
| [Eastron SDM630 (3-phase)](#eastron-sdm630-3-phase) | Eastron | SDM630 Modbus V2 | 29 | FC04 / big |
| [Schneider iEM3000 (3-phase)](#schneider-iem3000-3-phase) | Schneider Electric | iEM3155 / iEM3255 / iEM3455 / iEM3555 | 22 | FC03 / big |

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

> ABB B21 single-phase DIN-rail energy meter (B-series). Same Modbus map as the B23, populated on L1/total. Integer measurements in HOLDING registers (FC03), big-endian / high-word-first. Scales are divisors: V/10, A/100, W|var|VA/100, Hz/100, PF/1000, kWh/100. Verified against the same authoritative B-series sources as the B23 template.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 20480 / 0x5000 | `Import_kWh` | Active energy import (total) | uint64 | 100 | kWh | slow |
| 20484 / 0x5004 | `Export_kWh` | Active energy export (total) | uint64 | 100 | kWh | slow |
| 20488 / 0x5008 | `Net_kWh` | Active energy net (total) | int64 | 100 | kWh | slow |
| 23296 / 0x5B00 | `V` | Voltage L-N | uint32 | 10 | V | realtime |
| 23308 / 0x5B0C | `I` | Current | uint32 | 100 | A | realtime |
| 23316 / 0x5B14 | `P` | Active power | int32 | 100 | W | realtime |
| 23324 / 0x5B1C | `Q` | Reactive power | int32 | 100 | var | normal |
| 23332 / 0x5B24 | `S` | Apparent power | int32 | 100 | VA | normal |
| 23340 / 0x5B2C | `Freq` | Frequency | uint16 | 100 | Hz | realtime |
| 23354 / 0x5B3A | `PF` | Power factor | int16 | 1000 | — | normal |

## ABB B23 (3-phase)

**id** `abb_b23` · **vendor** ABB · **model** B23 (System pro M compact) · **version** 1.0.0 · **registers** 32

- **Transport:** FC03 (read holding registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** ABB B23/B24 User Manual 2CMC485003M0201 ch.9 (docs/vendor/meters/abb-b23-b24-user-manual.pdf) — register-for-register confirmed; also cross-checked vs steefan85/ABB_B23_Energy_Meter and roastedelectrons/ABBEnergyMeter

> ABB B23 three-phase DIN-rail energy meter (B-series; B24 shares this map). Integer measurements in HOLDING registers (FC03), big-endian / high-word-first. 32-bit uint32/int32 for instantaneous values, 64-bit uint64/int64 for energy. Scales are divisors: V/10, A/100, W|var|VA/100, Hz/100, PF/1000, kWh|kvarh/100. Verified register-for-register against two independent open-source integrations (steefan85/ABB_B23_Energy_Meter register map + roastedelectrons/ABBEnergyMeter device CSV, which agree) and consistent with ABB manual 2CDC512084D0101.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 20480 / 0x5000 | `Import_kWh` | Active energy import (total) | uint64 | 100 | kWh | slow |
| 20484 / 0x5004 | `Export_kWh` | Active energy export (total) | uint64 | 100 | kWh | slow |
| 20488 / 0x5008 | `Net_kWh` | Active energy net (total) | int64 | 100 | kWh | slow |
| 20492 / 0x500C | `Import_kvarh` | Reactive energy import (total) | uint64 | 100 | kvarh | slow |
| 20496 / 0x5010 | `Export_kvarh` | Reactive energy export (total) | uint64 | 100 | kvarh | slow |
| 23296 / 0x5B00 | `V_L1` | Voltage L1-N | uint32 | 10 | V | realtime |
| 23298 / 0x5B02 | `V_L2` | Voltage L2-N | uint32 | 10 | V | realtime |
| 23300 / 0x5B04 | `V_L3` | Voltage L3-N | uint32 | 10 | V | realtime |
| 23302 / 0x5B06 | `V_L1_L2` | Voltage L1-L2 | uint32 | 10 | V | normal |
| 23304 / 0x5B08 | `V_L3_L2` | Voltage L3-L2 | uint32 | 10 | V | normal |
| 23306 / 0x5B0A | `V_L1_L3` | Voltage L1-L3 | uint32 | 10 | V | normal |
| 23308 / 0x5B0C | `I_L1` | Current L1 | uint32 | 100 | A | realtime |
| 23310 / 0x5B0E | `I_L2` | Current L2 | uint32 | 100 | A | realtime |
| 23312 / 0x5B10 | `I_L3` | Current L3 | uint32 | 100 | A | realtime |
| 23314 / 0x5B12 | `I_N` | Current Neutral | uint32 | 100 | A | normal |
| 23316 / 0x5B14 | `P_total` | Active power total | int32 | 100 | W | realtime |
| 23318 / 0x5B16 | `P_L1` | Active power L1 | int32 | 100 | W | realtime |
| 23320 / 0x5B18 | `P_L2` | Active power L2 | int32 | 100 | W | realtime |
| 23322 / 0x5B1A | `P_L3` | Active power L3 | int32 | 100 | W | realtime |
| 23324 / 0x5B1C | `Q_total` | Reactive power total | int32 | 100 | var | normal |
| 23326 / 0x5B1E | `Q_L1` | Reactive power L1 | int32 | 100 | var | normal |
| 23328 / 0x5B20 | `Q_L2` | Reactive power L2 | int32 | 100 | var | normal |
| 23330 / 0x5B22 | `Q_L3` | Reactive power L3 | int32 | 100 | var | normal |
| 23332 / 0x5B24 | `S_total` | Apparent power total | int32 | 100 | VA | normal |
| 23334 / 0x5B26 | `S_L1` | Apparent power L1 | int32 | 100 | VA | normal |
| 23336 / 0x5B28 | `S_L2` | Apparent power L2 | int32 | 100 | VA | normal |
| 23338 / 0x5B2A | `S_L3` | Apparent power L3 | int32 | 100 | VA | normal |
| 23340 / 0x5B2C | `Freq` | Frequency | uint16 | 100 | Hz | realtime |
| 23354 / 0x5B3A | `PF_total` | Power factor total | int16 | 1000 | — | normal |
| 23355 / 0x5B3B | `PF_L1` | Power factor L1 | int16 | 1000 | — | normal |
| 23356 / 0x5B3C | `PF_L2` | Power factor L2 | int16 | 1000 | — | normal |
| 23357 / 0x5B3D | `PF_L3` | Power factor L3 | int16 | 1000 | — | normal |

## Carlo Gavazzi EM24 (AV5/AV53, 3-phase)

**id** `carlo_gavazzi_em24` · **vendor** Carlo Gavazzi · **model** EM24-DIN AV5(3) · **version** 1.0.0 · **registers** 16

- **Transport:** FC03 (read holding registers) · byte order **little-endian, low word first (CDAB / word-swapped)**
- **Source / provenance:** Victron dbus-modbus-client carlo_gavazzi.py (EM24_Meter) + Carlo Gavazzi EM24-DIN communication protocol

> Carlo Gavazzi EM24-DIN 3-phase energy meter (the legacy Victron grid meter). 32-bit measurements are signed INT32 in HOLDING registers (FC03), LOW-WORD-FIRST (little-endian, Reg_s32l). Scales are divisors: V/10, A/1000, W/10, Hz/10, kWh/10. Register map is authoritative from Victron dbus-modbus-client/carlo_gavazzi.py (model detected via reg 0x000b == 1651) and matches this repo's production-proven em24_av53 emulation. CG meters answer both FC03 and FC04 for measurements; FC03 is canonical.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 0 / 0x0000 | `V_L1` | Voltage L1-N | int32 | 10 | V | realtime |
| 2 / 0x0002 | `V_L2` | Voltage L2-N | int32 | 10 | V | realtime |
| 4 / 0x0004 | `V_L3` | Voltage L3-N | int32 | 10 | V | realtime |
| 12 / 0x000C | `I_L1` | Current L1 | int32 | 1000 | A | realtime |
| 14 / 0x000E | `I_L2` | Current L2 | int32 | 1000 | A | realtime |
| 16 / 0x0010 | `I_L3` | Current L3 | int32 | 1000 | A | realtime |
| 18 / 0x0012 | `P_L1` | Active power L1 | int32 | 10 | W | realtime |
| 20 / 0x0014 | `P_L2` | Active power L2 | int32 | 10 | W | realtime |
| 22 / 0x0016 | `P_L3` | Active power L3 | int32 | 10 | W | realtime |
| 40 / 0x0028 | `P_total` | Total active power | int32 | 10 | W | realtime |
| 51 / 0x0033 | `Freq` | Frequency | uint16 | 10 | Hz | realtime |
| 52 / 0x0034 | `Import_kWh` | Import active energy (total) | int32 | 10 | kWh | slow |
| 64 / 0x0040 | `Energy_L1_Import` | Import active energy L1 | int32 | 10 | kWh | slow |
| 66 / 0x0042 | `Energy_L2_Import` | Import active energy L2 | int32 | 10 | kWh | slow |
| 68 / 0x0044 | `Energy_L3_Import` | Import active energy L3 | int32 | 10 | kWh | slow |
| 78 / 0x004E | `Export_kWh` | Export active energy (total) | int32 | 10 | kWh | slow |

## Eastron SDM120 (single-phase)

**id** `eastron_sdm120` · **vendor** Eastron · **model** SDM120 Modbus · **version** 0.9.0 · **registers** 10

- **Transport:** FC04 (read input registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** Eastron SDM120/SDM220 Modbus Protocol (input registers, FC04, float32)

> Eastron SDM120 single-phase energy meter (also SDM220). Measurements are 32-bit IEEE-754 floats in INPUT registers (FC4), big-endian — same base addresses as the SDM630 phase 1. Community-sourced canonical map — verify against your unit's manual.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 0 / 0x0000 | `V` | Voltage | float | 1 | V | realtime |
| 6 / 0x0006 | `I` | Current | float | 1 | A | realtime |
| 12 / 0x000C | `P` | Active power | float | 1 | W | realtime |
| 18 / 0x0012 | `S` | Apparent power | float | 1 | VA | normal |
| 24 / 0x0018 | `Q` | Reactive power | float | 1 | var | normal |
| 30 / 0x001E | `PF` | Power factor | float | 1 | — | normal |
| 70 / 0x0046 | `Freq` | Frequency | float | 1 | Hz | realtime |
| 72 / 0x0048 | `Import_kWh` | Import active energy | float | 1 | kWh | slow |
| 74 / 0x004A | `Export_kWh` | Export active energy | float | 1 | kWh | slow |
| 342 / 0x0156 | `Total_kWh` | Total active energy | float | 1 | kWh | slow |

## Eastron SDM630 (3-phase)

**id** `eastron_sdm630` · **vendor** Eastron · **model** SDM630 Modbus V2 · **version** 0.9.0 · **registers** 29

- **Transport:** FC04 (read input registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** Eastron SDM630 Modbus Protocol V2 (input registers, FC04, float32)

> Eastron SDM630 3-phase energy meter. Measurements are 32-bit IEEE-754 floats in INPUT registers (FC4), big-endian. Community-sourced canonical map — verify against your unit's manual before relying on it for billing.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 0 / 0x0000 | `V_L1` | Voltage L1-N | float | 1 | V | realtime |
| 2 / 0x0002 | `V_L2` | Voltage L2-N | float | 1 | V | realtime |
| 4 / 0x0004 | `V_L3` | Voltage L3-N | float | 1 | V | realtime |
| 6 / 0x0006 | `I_L1` | Current L1 | float | 1 | A | realtime |
| 8 / 0x0008 | `I_L2` | Current L2 | float | 1 | A | realtime |
| 10 / 0x000A | `I_L3` | Current L3 | float | 1 | A | realtime |
| 12 / 0x000C | `P_L1` | Active power L1 | float | 1 | W | realtime |
| 14 / 0x000E | `P_L2` | Active power L2 | float | 1 | W | realtime |
| 16 / 0x0010 | `P_L3` | Active power L3 | float | 1 | W | realtime |
| 18 / 0x0012 | `S_L1` | Apparent power L1 | float | 1 | VA | normal |
| 20 / 0x0014 | `S_L2` | Apparent power L2 | float | 1 | VA | normal |
| 22 / 0x0016 | `S_L3` | Apparent power L3 | float | 1 | VA | normal |
| 24 / 0x0018 | `Q_L1` | Reactive power L1 | float | 1 | var | normal |
| 26 / 0x001A | `Q_L2` | Reactive power L2 | float | 1 | var | normal |
| 28 / 0x001C | `Q_L3` | Reactive power L3 | float | 1 | var | normal |
| 30 / 0x001E | `PF_L1` | Power factor L1 | float | 1 | — | normal |
| 32 / 0x0020 | `PF_L2` | Power factor L2 | float | 1 | — | normal |
| 34 / 0x0022 | `PF_L3` | Power factor L3 | float | 1 | — | normal |
| 52 / 0x0034 | `P_total` | Total active power | float | 1 | W | realtime |
| 56 / 0x0038 | `S_total` | Total apparent power | float | 1 | VA | normal |
| 60 / 0x003C | `Q_total` | Total reactive power | float | 1 | var | normal |
| 62 / 0x003E | `PF_total` | Total power factor | float | 1 | — | normal |
| 70 / 0x0046 | `Freq` | Frequency | float | 1 | Hz | realtime |
| 72 / 0x0048 | `Import_kWh` | Import active energy | float | 1 | kWh | slow |
| 74 / 0x004A | `Export_kWh` | Export active energy | float | 1 | kWh | slow |
| 76 / 0x004C | `Import_kvarh` | Import reactive energy | float | 1 | kvarh | slow |
| 78 / 0x004E | `Export_kvarh` | Export reactive energy | float | 1 | kvarh | slow |
| 342 / 0x0156 | `Total_kWh` | Total active energy | float | 1 | kWh | slow |
| 344 / 0x0158 | `Total_kvarh` | Total reactive energy | float | 1 | kvarh | slow |

## Schneider iEM3000 (3-phase)

**id** `schneider_iem3000` · **vendor** Schneider Electric · **model** iEM3155 / iEM3255 / iEM3455 / iEM3555 · **version** 1.0.0 · **registers** 22

- **Transport:** FC03 (read holding registers) · byte order **big-endian, high word first (ABCD)**
- **Source / provenance:** volkszaehler/mbmd meters/rs485/iem3000.go (field-tested; cites Schneider DOCA0005). Official register list saved at docs/vendor/meters/schneider-iem3000-modbus-register-list.pdf (image-only PDF, not machine-parsed).

> Schneider Electric iEM3000-series DIN-rail energy meter (Modbus models iEM3150/3155/3250/3255/3350/3355/3450/3455/3550/3555). HOLDING registers (FC03), big-endian / high-word-first. Instantaneous values are Float32; energy is INT64. Addresses are 0-based PDU (= Schneider register number − 1, e.g. Active power total register 3060 → address 3059). Schneider reports power float32 in kW and energy int64 in Wh; scales convert to W / kWh (power /0.001, energy /1000). Voltage/current/frequency float32 are already in V/A/Hz (scale 1). Register map verified register-for-register against the field-tested volkszaehler/mbmd iem3000 driver (which cites Schneider DOCA0005). Power factor is omitted (mbmd disables it as unreliable on this series). This is the verified CORE measurement set; the device also exposes many more registers (THD, min/max, demand, per-tariff energy) — add them via the Template Manager once you have your unit's full DOCA0005 register list.

| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |
|---|---|---|---|---|---|---|
| 2999 / 0x0BB7 | `I_L1` | Current L1 | float | 1 | A | realtime |
| 3001 / 0x0BB9 | `I_L2` | Current L2 | float | 1 | A | realtime |
| 3003 / 0x0BBB | `I_L3` | Current L3 | float | 1 | A | realtime |
| 3009 / 0x0BC1 | `I_avg` | Current average | float | 1 | A | normal |
| 3027 / 0x0BD3 | `V_L1` | Voltage L1-N | float | 1 | V | realtime |
| 3029 / 0x0BD5 | `V_L2` | Voltage L2-N | float | 1 | V | realtime |
| 3031 / 0x0BD7 | `V_L3` | Voltage L3-N | float | 1 | V | realtime |
| 3035 / 0x0BDB | `V_avg` | Voltage L-N average | float | 1 | V | normal |
| 3053 / 0x0BED | `P_L1` | Active power L1 | float | 0.001 | W | realtime |
| 3055 / 0x0BEF | `P_L2` | Active power L2 | float | 0.001 | W | realtime |
| 3057 / 0x0BF1 | `P_L3` | Active power L3 | float | 0.001 | W | realtime |
| 3059 / 0x0BF3 | `P_total` | Active power total | float | 0.001 | W | realtime |
| 3067 / 0x0BFB | `Q_total` | Reactive power total | float | 0.001 | var | normal |
| 3075 / 0x0C03 | `S_total` | Apparent power total | float | 0.001 | VA | normal |
| 3109 / 0x0C25 | `Freq` | Frequency | float | 1 | Hz | realtime |
| 3203 / 0x0C83 | `Import_kWh` | Active energy import (total) | int64 | 1000 | kWh | slow |
| 3207 / 0x0C87 | `Export_kWh` | Active energy export (total) | int64 | 1000 | kWh | slow |
| 3219 / 0x0C93 | `Import_kvarh` | Reactive energy import (total) | int64 | 1000 | kvarh | slow |
| 3223 / 0x0C97 | `Export_kvarh` | Reactive energy export (total) | int64 | 1000 | kvarh | slow |
| 3517 / 0x0DBD | `Import_L1_kWh` | Active energy import L1 | int64 | 1000 | kWh | slow |
| 3521 / 0x0DC1 | `Import_L2_kWh` | Active energy import L2 | int64 | 1000 | kWh | slow |
| 3525 / 0x0DC5 | `Import_L3_kWh` | Active energy import L3 | int64 | 1000 | kWh | slow |
