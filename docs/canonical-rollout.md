# Canonical naming — roll-out & dependency audit

Tracks the roll-out of the [canonical field naming](canonical-fields.md) across
device templates, and — critically — **who consumes which device's topics /
buckets / fields**, so a rename never breaks a live consumer.

## Dependency audit (2026-08-12)

What reads MBG's published data, by device:

| Consumer | Uses Janitza (`umg512`) | Uses Fronius / others |
|---|---|---|
| **pv-stack-ui** (Flask) | **YES — 78 refs** to `janitza/umg512/*` MQTT topics (dashboard, flow templates) | no |
| **Node-RED flows** | **YES — 37 refs** to `janitza/umg512/*` MQTT topics | no |
| **Grafana** | **NO** (0 refs to the `janitza` bucket/fields; it queries aggregated measurements `system`, `grid`, `seplos_pack`, `fronius_inverter`) | fronius_inverter |

Janitza MQTT topics pv-stack-ui / Node-RED subscribe to (hierarchical, slash-separated):
```
janitza/umg512/voltage/l1_n            janitza/umg512/power/active/total
janitza/umg512/voltage/l2_n            janitza/umg512/power/active/l1|l2|l3
janitza/umg512/voltage/l3_n            janitza/umg512/power/apparent_power_l1|l2|l3
janitza/umg512/current/l1              janitza/umg512/power_factor/l1|l2|l3
janitza/umg512/frequency               janitza/umg512/thd/voltage/l1|l2|l3
janitza/umg512/status                  janitza/umg512/thd/current/l1|l2|l3
janitza/umg512/quality/unsymmetrical_voltage
```

### ⛔ Rule: Janitza (`umg512`) is FROZEN
Its MQTT topics are wired into pv-stack-ui (78) + Node-RED (37). Renaming its
topics / prefix / bucket / device tag would break both. **Do NOT rename the
Janitza device or its selected-register topics.** It stays the production
reference; the canonical scheme applies to OTHER devices only.

Note: the clean `voltage/l1_n` topics are set as `mqtt_topic` on the umg512
**device's selected registers** (not in the template — the template has 4126 raw
registers with cryptic names like `_G_ULL[0]` and no mqtt_topic). The InfluxDB
field stays the cryptic register name (`ull_0`), but Grafana does not use it.

## Convention finding — MQTT topic shape

| | MQTT topic | InfluxDB field |
|---|---|---|
| **Janitza** (consumers depend on this) | `voltage/l1_n` (hierarchical) | `ull_0` (cryptic) |
| **Fronius pilot** (current) | `voltage_l1_n` (flat) | `voltage_l1_n` (canonical) |

So the pilot fixed the InfluxDB field (canonical) but its MQTT topic shape (flat)
differs from the Janitza convention consumers already use (hierarchical). For
true uniformity, the canonical scheme should set **both**: InfluxDB field
`voltage_l1_n` (name) **and** MQTT topic `voltage/l1_n` (mqtt_topic, hierarchical,
matching Janitza). → **decision pending** before roll-out (see below).

## Roll-out status

| Template | Live device | Consumers | Action |
|---|---|---|---|
| `janitza_umg512_pro` | umg512 | pv-stack-ui, Node-RED, **both vmeters** | **FROZEN** — see Victron-source domain below |
| `fronius_solar_api` | fronius-solar (disabled) | vmeter drop-in source (latent) | **FROZEN** — see Victron-source domain below |
| `fronius_smart_meter_65a` | fronius_rtu (test) | none | ✅ canonical — field + hierarchical MQTT topic, live-verified |
| `carlo_gavazzi_em24` | — | none | ✅ canonical (2026-08-12) |
| `abb_b21`, `abb_b23` | — | none | ✅ canonical (2026-08-12) |
| `eastron_sdm120`, `eastron_sdm630` | — | none | ✅ canonical (2026-08-12) |
| `schneider_iem3000` | — | none | ✅ canonical (2026-08-12) |
| sensor maps (mqtt/zigbee/ble) | — | none | out of scope (non-electrical) |

Auto-select now applies the hierarchical MQTT topic + canonical InfluxDB
measurement from the dictionary (or the template's explicit `defaults`), so any
**new** device seeded from a canonical template is uniform out of the box.

Three canonical fields were added during this roll-out to cover real registers
the vendor maps report: `current_avg` (Schneider I_avg), `energy_active_total`
(SDM120/630 `Total_kWh` = import+export), `energy_reactive_total` (SDM630
`Total_kvarh`).

## ⛔ The Victron-source naming domain (Janitza + vmeters + fronius-solar)

The two virtual meters that feed the Victron ESS — `em24_av53` and
`fronius_ts_native` — map their live sources **by register name**: `live:
"_G_P_SUM3"`, `live: "_G_ULN[0]"`, `live: "_WH_V[4]"`, … Those names resolve
against the **primary (Janitza) device's** `current_values` (the vmeter
instances carry no `device:` field → primary cache). So the vmeter templates are
coupled to Janitza's cryptic names.

`fronius-solar` (Solar-API HTTP reader of the Fronius **Smart Meter TS 5kA-3**,
a *different* physical meter than fronius_rtu's 65A-3) deliberately mirrors those
same cryptic names so it can drop in as an alternative vmeter source. It is
`enabled: false` and no vmeter currently sources from it.

**Decision (2026-08-12):** Janitza, the two vmeter templates, and
`fronius-solar` form ONE naming domain and must be canonicalised **atomically**
in a night maintenance window (no production) — rename Janitza's selected
registers, update the two vmeter templates' `live:` keys to the canonical names,
migrate `fronius-solar`, and update pv-stack-ui (78) + Node-RED (37) topic refs
together. Renaming any one in isolation half-migrates the chain that feeds the
Victron. Prefer dual-publish for zero-gap since Janitza also feeds the grid/DG
controllers. Until then: **all three are FROZEN.**

## Settled — MQTT topic shape

**Hierarchical** (`voltage/l1_n`), matching what pv-stack-ui / Node-RED already
speak, while the InfluxDB field stays the flat canonical name (`voltage_l1_n`).
The dictionary carries both per field; auto-select applies them. Live-verified on
fronius_rtu.
