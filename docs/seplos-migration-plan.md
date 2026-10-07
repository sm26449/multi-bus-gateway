# Seplos collector → MBG migration (rtu_tap)

Replacing the external `seplos-modbus-mqtt` snooper with MBG's listen-only
RTU tap, so a battery bank is just *one USB adapter + one template*. Namespace
decision: the `seplos/*` MQTT topics are KEPT at cutover (one writer after the
collector retires); the `mbg/*` convention stays for new devices.

## Status

| Phase | Content | State |
|---|---|---|
| 1 | FC04 telemetry + FC01 PIC alarms in `rtu_tap`, template `seplos_bms_v3_rtu_tap` | DONE — validated live, byte-identical |
| 2 | Derived counts (popcount calc registers) + template-declared bank aggregation (`aggregates:` fan-out) | DONE — unit-tested |
| 3 | Consumer compatibility (this document) | DONE |
| 4 | Provisioning + cutover window | DONE 2026-10-07 — collector retired |

## Consumer inventory (verified 2026-10-07)

- **Node-RED**: consumes NO `seplos/*` topic.
- **Grafana** (`energy-flow-new.json`): bucket `seplos`, measurement
  `seplos_pack`, fields `average_soc`, `total_power` — 2 queries total.
- **alertd**: `seplos/health/status` + `seplos/health/uptime` (LWT), and
  `seplos/pack/{pack_average_soc, pack_avg_temp, pack_batteries_online,
  pack_cell_delta, pack_last_update, pack_total_power, pack_total_voltage}`.
- **Home Assistant**: no broker evidence of a live HA (no retained birth
  topic); collector discovery appears unconsumed. MBG `ha_discovery` stays
  OFF unless someone claims it.
- **InfluxDB**: collector wrote `seplos_battery` (tag `battery_id`, flat
  field names) and `seplos_pack`. Only `seplos_pack` is queried (Grafana).

Deliberately dropped (no consumer found): `pack_status` consensus,
`balancing_cells` text, `last_update` per battery.

## How parity is achieved

- **Per-pack MQTT**: device `mqtt_topic_prefix: seplos/battery_<N>`; register
  topics default to the flat name → byte-identical topics. ON/OFF and status
  labels ride the enum decode.
- **Bank MQTT**: endpoint `mqtt.aggregate_prefix: seplos/pack`; the
  template-declared `aggregates` outputs (`pack_average_soc`, …) publish
  under their own names → byte-identical for alertd. `units_total >= 2`
  required by the publisher — satisfied (8 packs).
- **Per-pack Influx**: template defaults carry
  `influxdb.measurement: seplos_battery` on exactly the fields the collector
  wrote (influx stays OFF until provisioning enables it). The `battery_id`
  tag is per-device: the provisioning script injects
  `influxdb.tags: {battery_id: "<N>"}` into each seeded file.
- **Bank Influx**: endpoint influx writes measurement `endpoint`
  (tag `device=<endpoint id>`, field = aggregate name). At cutover the TWO
  Grafana queries change to: measurement `endpoint`, fields
  `pack_average_soc` / `pack_total_power` (bucket from
  `endpoint.influxdb.bucket: seplos`).

## alertd rule changes (apply at cutover)

| Old | New |
|---|---|
| LWT flag on `seplos/health/status` != online | `seplos/pack/status` != `online` (endpoint census: online/partial/offline) |
| freshness on `seplos/health/uptime` | freshness on `seplos/pack/pack_average_soc` arrival (any aggregate topic works) |
| `seplos/pack/pack_batteries_online` | `seplos/pack/units_online` (MBG census name; alternatively keep watching batteries_online = add an `aggregates` alias — not done, honest names preferred) |
| `seplos/pack/pack_last_update` freshness | drop — same arrival-freshness as above covers it |
| value rules on `pack_average_soc`, `pack_avg_temp`, `pack_cell_delta`, `pack_total_power`, `pack_total_voltage` | unchanged (topics identical) |

## Phase 4 runbook (cutover window)

1. Provision (script, MBG stopped collector NOT yet): 8 devices
   `seplos-tap-p1..8` (unit_id 1..8, `/dev/ttySEPLOS`, 19200, template
   `seplos_bms_v3_rtu_tap`, `mqtt_topic_prefix: seplos/battery_N`,
   `mqtt.enabled: true`, influx enabled + bucket `seplos` + `battery_id` tag
   injected per seeded file, all `enabled: false` initially) + endpoint
   `battery-bank` (units p1..8, `mqtt.aggregate_prefix: seplos/pack`,
   `influxdb: {enabled: true, bucket: seplos}`).
2. `docker stop pv-stack-seplos-modbus-mqtt` (alert expected on the OLD LWT
   rule — last time it fires).
3. Enable the 8 devices + restart MBG; verify: `seplos/battery_1/#` flows,
   `seplos/pack/pack_average_soc` flows, Influx `seplos_battery` points land.
4. Apply the alertd rule changes + the 2 Grafana query edits.
5. Retire: remove the OLD MBG MQTT devices `seplos-p1..8` + `seplos-bank`
   (they subscribed to the collector's topics; the tap devices replace them
   on the dashboard), disable the collector's compose service.
6. Soak 24h; rollback = reverse order (collector start ↔ tap disable).

## Known gaps (accepted)

- HA discovery off (no consumer). Old HA entities, if any, go stale.
- `seplos_battery` Influx series: tag set changes (`battery_id` kept via
  provisioning, `device`/`poll_group` tags added by MBG). No known queries.
- PIC coils carry no per-cell alarm text lists (`balancing_cells`); the
  bitmask `balancing_bits` + `balancing_count` carry the same information.

## Cutover record (2026-10-07)

Executed by `tools/seplos_bank_cutover.py --apply` + the window:
collector stopped, MBG redeployed; the `battery-bank` endpoint
materialized 8 rtu_tap units which auto-seeded (84 registers + 8
calculated each) from template 1.5.0. Verified live: all 8
`seplos/battery_N/*` flowing (telemetry, PIC status/alarms, calculated
counts), `seplos/pack/*` aggregates (SOC avg/min/max/spread, total
power, units_online 8, status online), InfluxDB `seplos_battery` +
`endpoint` measurements landing. alertd variables 10/112/188/189
retopiced (backups in the MBG config dir); the two Grafana bank queries
moved to measurement `endpoint`. The collector service sits in the
compose `retired` profile — rollback is documented inline there.

## Pre-release checklist (before the public 3.85.0 push)

- [ ] Device wizard (Add/Edit) supports `protocol: rtu_tap` — serial
      port, baudrate, unit_id, stale_after_s — so a listen-only device
      is configurable from the UI, not only from config.yaml.
- [ ] CHANGELOG entries for 3.85.0 (fleet-first dashboard + rtu_tap +
      seplos migration features).
