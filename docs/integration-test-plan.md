# Integration test plan & R&D procurement list

Goal: exercise **every acquisition path** (Modbus RTU/TCP, HTTP/JSON, MQTT-in,
SunSpec) against the **devices the energy & IoT communities actually use** —
meters, temp/humidity sensors, smart plugs, lights, switches — so the bundled
map catalog and the wizard flows are field-verified, not theoretical. Each real
device we integrate becomes a shipped, field-tested template (policy: never
fabricate a register/map — verify it on hardware with the Diagnostics probe).

Prices are approximate EU retail (2026) for budgeting; buy from the usual DIY
channels. The gateway ACQUIRES state from all of these; controlling actuators
(lights/switches/plugs) stays a downstream job (Node-RED/HA), but reading their
state exercises the MQTT-in / json_path paths across diverse payloads.

---

## 0. Free wins — maps to create from hardware we already own

No purchase needed; each is one field-verified template (Diagnostics → probe →
map). Highest value first:

| Device (owned) | Path | Map status |
|----------------|------|-----------|
| Chint-style / Fronius Smart Meter ×3 (CT, 3-phase, 1-phase) | Modbus RTU | **create** (only the vmeter *emulation* + Solar-API HTTP exist today) |
| Janitza UMG 96 | Modbus RTU | **create** (UMG 512 map exists; 96 has its own register set) |
| Hiking DDS238-2 | Modbus RTU | **create** |
| Schneider iEM 3155 | Modbus RTU | **verify** against `schneider_iem3000` |
| Eastron SDM120 | Modbus RTU | **verify** `eastron_sdm120` on real HW |
| Victron VM-3P75CT | Modbus TCP (via GX) | **verify** the path + map |

---

## 1. Shopping list — by device class

### A. Energy meters — Modbus RTU (RS485), DIY-solar staples

| # | Product | ~€ | Why (community prevalence) |
|---|---------|----|----|
| A1 | **Chint DTSU666** (or DTSU666-H hybrid) | 45–60 | Ships with almost every budget hybrid inverter — **Deye**, Solis, Growatt, Sofar. The single most common DIY-solar grid meter. No map yet. |
| A2 | **Eastron SDM630-Modbus V2** (3-phase, CT or 100 A) | 40 | The community 3-phase Modbus meter. Verifies `eastron_sdm630` on HW. |
| A3 | **Eastron SDM72D-M** (3-phase, bidirectional, DIN, MID) | 45 | The EV/solar import-export MID meter; slightly different map than SDM630. |
| A4 | **Eastron SDM230-Modbus** (1-phase, DIN, MID) | 25 | The 1-phase MID meter for EV chargers / sub-metering. |

### B. Energy / power meters — MQTT + HTTP (Shelly Gen2/3)

| # | Product | ~€ | Why |
|---|---------|----|----|
| B1 | **Shelly Pro 3EM** (3-phase, DIN, 3×120 A CT) | 110–130 | *The* 3-phase energy meter of the HA/home-energy community. Speaks MQTT (RPC) **and** HTTP RPC **and** local WS — exercises two paths with the top IoT energy device. |
| B2 | **Shelly PM Mini Gen3** (inline power) | 15 | Cheap single-channel power meter; MQTT + RPC. |

### C. Temperature / humidity sensors

| # | Product | ~€ | Path exercised |
|---|---------|----|----|
| C1 | **Shelly H&T Gen3** (WiFi, temp+humidity) | 30 | MQTT-in with a battery-sensor payload (sleepy device → tests retained-drop + freshness). |
| C2 | **Shelly BLU H&T** (BLE, BTHome) | 20 | The BLE/BTHome path (our `ble_theengs_sensor` preset). Needs a BLE gateway (a Shelly Plus or Theengs). |
| C3 | **Aqara Temperature & Humidity** (Zigbee) | 12 | The Zigbee sensor path (`zigbee2mqtt_sensor`). Needs a coordinator (see G). |

### D. Smart plugs (with power metering)

| # | Product | ~€ | Why |
|---|---------|----|----|
| D1 | **Shelly Plug S Gen3** | 18 | Gen2/3 plug with power metering; MQTT + RPC. Very common. |
| D2 | **Athom / Nous plug pre-flashed Tasmota** | 12 | **Tasmota** is the most widespread DIY firmware; well-known MQTT telemetry format — verifies the generic MQTT-in + json_path path. |

### E. Lights (state + power reporting)

| # | Product | ~€ | Why |
|---|---------|----|----|
| E1 | **Shelly Duo / Shelly Bulb RGBW** (or Shelly RGBW2 controller) | 20 | Reports on/off, brightness, colour, power over MQTT — a richer/nested payload than a meter, good json_path stress. |
| E2 | **Sonoff/Athom light flashed with Tasmota or ESPHome** | 12 | The DIY light path; Tasmota `LIGHT`/ESPHome state topics. |

### F. Switches / relays (state)

| # | Product | ~€ | Why |
|---|---------|----|----|
| F1 | **Shelly Plus 1PM** (relay + power) | 18 | Reports relay state + power; the most common DIY relay. |
| F2 | **Sonoff MINI R4 flashed Tasmota/ESPHome** | 10 | DIY relay via Tasmota/ESPHome MQTT. |

### G. Zigbee stack (only if no coordinator yet)

| # | Product | ~€ | Why |
|---|---------|----|----|
| G1 | **Sonoff Zigbee 3.0 USB Dongle Plus (E/P)** | 25 | zigbee2mqtt coordinator — unlocks the whole Zigbee sensor class cheaply. |
| G2 | **Sonoff SNZB-02 (temp/hum)** + **SNZB-04 (door)** + a Zigbee plug | 8+8+12 | The cheapest sensor/actuator spread once a coordinator exists. |

---

## 2. Priority tiers (buy in order)

**P0 — community coverage core (~€230):** A1 Chint DTSU666, A2 Eastron SDM630,
B1 Shelly Pro 3EM, D2 Tasmota plug, C1 Shelly H&T Gen3.
→ Covers RTU grid meters (budget-inverter + Eastron), the top MQTT 3-phase
meter, the top DIY firmware, and a real sensor. This alone makes the catalog
credible to the energy + HA communities.

**P1 — breadth (~€120):** A3 SDM72D-M, A4 SDM230, D1 Shelly Plug S, E1 Shelly
RGBW, F1 Shelly Plus 1PM.
→ Adds bidirectional/MID meters, plug/light/relay classes (varied payloads that
stress json_path + the device-class presets).

**P2 — Zigbee + BLE (~€90, only if no coordinator):** G1 dongle, G2 sensor set,
C2 Shelly BLU H&T, E2/F2 Tasmota/ESPHome light+relay.
→ Completes the Zigbee/BTHome sensor path and the ESPHome ecosystem.

**Grand total P0+P1 ≈ €350; +P2 ≈ €440.**

---

## 3. Coverage matrix (what the plan exercises)

| Acquisition path | Devices |
|------------------|---------|
| **Modbus RTU** | Janitza 512/96, Schneider iEM3155, Fronius meters ×3, Hiking DDS238, Eastron SDM120/230/630/72D, **Chint DTSU666** |
| **Modbus TCP** | Janitza, Victron VM-3P75CT (via GX) |
| **HTTP/JSON** | Shelly Gen2/3 RPC, Fronius Solar API |
| **MQTT-in** | Shelly Pro 3EM, Shelly H&T/Plug/RGBW/1PM, **Tasmota** plug/light/relay |
| **SunSpec walk** | Fronius inverters (owned) |
| **Zigbee (z2m)** | Aqara / Sonoff SNZB sensors + plug |
| **BLE / BTHome** | Shelly BLU H&T (Theengs preset) |

Device **classes** covered: 3-phase & 1-phase energy meters, bidirectional/MID
meters, temp/humidity, smart plugs w/ power, lights (dim/RGB), switches/relays,
door/motion (Zigbee) — the full home-energy + IoT spread the communities run.

---

## 4. Order of testing (fastest path to value)

1. **Free maps first** (§0) — Fronius RTU meters, UMG 96, Hiking; verify
   iEM3155 & SDM120. Zero cost, immediate catalog growth.
2. **P0 arrivals** — Chint DTSU666 (RTU map + Deye relevance), Shelly Pro 3EM
   (MQTT+HTTP), Tasmota plug (generic MQTT-in), Shelly H&T (sensor freshness).
3. **P1** — the meter/plug/light/relay breadth; each new device-class payload
   feeds a wizard preset + a json_path example in the docs.
4. **P2** — Zigbee/BLE once a coordinator is in place.

Each integrated device ends as: a **field-verified template** in the catalog,
a **wizard preset** where useful, and a **json_path example** in the manual —
so the next person with the same device is plug-and-play.
