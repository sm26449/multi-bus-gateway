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

Only three templates have live devices; the rest have none (safe to rename):

| Template | Live device | Consumers | Action |
|---|---|---|---|
| `janitza_umg512_pro` | umg512 | pv-stack-ui, Node-RED | **FROZEN — do not touch** |
| `fronius_smart_meter_65a` | fronius_rtu (test) | none | ✅ canonical (field). MQTT shape TBD |
| `fronius_solar_api` | fronius-solar (idle) | check before touching | pending |
| `carlo_gavazzi_em24` | — | none | pending (safe) |
| `abb_b21`, `abb_b23` | — | none | pending (safe) |
| `eastron_sdm120`, `eastron_sdm630` | — | none | pending (safe) |
| `schneider_iem3000` | — | none | pending (safe) |
| sensor maps (mqtt/zigbee/ble) | — | none | out of scope (non-electrical) |

## Open decision (before rolling out the rest)

**MQTT topic shape for canonical registers:** hierarchical (`voltage/l1_n`, matches
Janitza + consumers) or flat (`voltage_l1_n`, matches the InfluxDB field)? If
hierarchical, the canonical dictionary gains an `mqtt_topic` per field and the
Fronius pilot is revised to set it. Recommended: **hierarchical**, so new devices'
MQTT matches what pv-stack-ui/Node-RED already speak.
