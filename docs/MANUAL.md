# User Manual — Multi-Bus Gateway 3.0.0

🇬🇧 **English** | [🇷🇴 Română](MANUAL.ro.md)

A step-by-step guide for the technician/integrator: from a fresh install to a
multi-device gateway with virtual meters, diagnostics and hardened access.
Companion documents:

- **[architecture.md](architecture.md)** — how the engine works (diagrams).
- **[API.md](API.md)** — full REST endpoint reference with required roles.
- **[virtual-meter-spec.md](virtual-meter-spec.md)** — the wire-level contract
  of a virtual meter (staleness policies, quality block at 61440).
- **[device-catalog.md](device-catalog.md)** — every bundled register map and
  the source it was verified against.
- **[csv-import.md](csv-import.md)** — importing a vendor CSV register map.
- **[alerts-webhooks.md](alerts-webhooks.md)** — infrastructure alerting.

## Contents
1. [What you need](#1-what-you-need)
2. [Install (Docker)](#2-install-docker)
3. [First configuration](#3-first-configuration)
4. [The Web UI, tab by tab](#4-the-web-ui-tab-by-tab)
5. [Devices & device templates (multi-device)](#5-devices--device-templates-multi-device)
6. [Registers, poll groups & thresholds](#6-registers-poll-groups--thresholds)
7. [Calculated registers](#7-calculated-registers)
8. [MQTT & Home Assistant](#8-mqtt--home-assistant)
9. [InfluxDB & Grafana](#9-influxdb--grafana)
10. [REST push & the HTTP/JSON feed](#10-rest-push--the-httpjson-feed)
11. [Virtual Meters — step by step](#11-virtual-meters--step-by-step)
12. [Alerts & webhooks](#12-alerts--webhooks)
13. [Diagnostics](#13-diagnostics)
14. [Modbus writes & dead-man leases](#14-modbus-writes--dead-man-leases)
15. [Config safety: snapshots, rollback, backup](#15-config-safety-snapshots-rollback-backup)
16. [Security](#16-security)
17. [Observability: Status, /metrics, events](#17-observability-status-metrics-events)
18. [Languages & timezone](#18-languages--timezone)
19. [Troubleshooting](#19-troubleshooting)

---

## 1. What you need

- At least one southbound source: a **Modbus TCP** device (e.g. a Janitza
  UMG 512-PRO — port 502 enabled), a **Modbus RTU** device on a serial line
  (`/dev/ttyUSB0` + RS-485 adapter), an **HTTP/JSON** endpoint (Fronius Solar
  API, Shelly, Tasmota…), or an **MQTT** broker with telemetry topics.
- A host with **Docker + Docker Compose** (amd64 or arm64 — a Raspberry Pi
  works).
- *(Optional)* an MQTT broker for Home Assistant, and/or InfluxDB for
  Grafana/history.

---

## 2. Install (Docker)

```bash
# 1) Get the code
git clone https://github.com/sm26449/multi-bus-gateway.git
cd multi-bus-gateway

# 2) Create your environment file (optional — everything is configurable in the UI)
cp .env.example .env

# 3) Start it
docker compose up -d

# 4) Open the UI
#    http://<host>:8080
```

Ports published by the default compose file: `8080` (UI/API),
`1502–1512` (virtual-meter range, grow via `VMETER_PORT_START/END`), and
`502` (standard Modbus, for consumers that insist on it — drop it if the host
already uses it). For an RTU device pass the serial adapter through:
`devices: ["/dev/ttyUSB0:/dev/ttyUSB0"]` in a compose override.

Logs: `docker compose logs -f`.

---

## 3. First configuration

> **Tip:** after the first start you can set everything from the UI — device
> connections on the **Devices** page, MQTT/InfluxDB under **Config** — it
> persists to `config/config.yaml` and applies without a restart. `.env` only
> pre-seeds a fresh deploy; an env value takes precedence and **locks** that
> field in the UI (remove it from the environment to edit it again).

The optional env variables:

| Variable | What it is | Example |
|----------|-----------|---------|
| `MODBUS_HOST` / `MODBUS_PORT` / `MODBUS_UNIT_ID` | primary device connection | `192.168.1.100` / `502` / `1` |
| `MQTT_ENABLED` / `MQTT_BROKER` / `MQTT_PORT` | MQTT sink | `true` / `mosquitto` / `1883` |
| `MQTT_USERNAME` / `MQTT_PASSWORD` / `MQTT_PREFIX` / `MQTT_PUBLISH_MODE` | MQTT details | — |
| `INFLUXDB_ENABLED` / `INFLUXDB_URL` / `INFLUXDB_TOKEN` / `INFLUXDB_ORG` / `INFLUXDB_BUCKET` | InfluxDB sink | — |
| `UI_PORT` | Web UI port | `8080` |
| `API_KEY` | require `X-API-Key` on mutating requests | — |
| `VMETER_PORT_START` / `VMETER_PORT_END` | virtual-meter port range | `1502` / `1512` |

The status dots in the UI top bar (Modbus / MQTT / InfluxDB) turn green as
each pipeline connects — click one for details.

---

## 4. The Web UI, tab by tab

Open `http://<host>:8080`. The top navigation:

- **Dashboard** — global live KPI cards + the values you pinned across
  devices. *Customize* picks the cards; card/table view toggle; default
  widget colors follow the phase convention configured under Config →
  General.
- **Devices** — every southbound source as a card with live health; the
  **Add Device** wizard and **Discover devices** live here. Opening a device
  gives its tabbed workspace: *Overview* (read-only summary + data health),
  *Edit* (connection, template, poll intervals, output toggles),
  *Registers*, *Calculated*, *Outputs*, and per-device *Monitor / History /
  Energy*.
- **Virtual Meters** — serve the live values as standard Modbus meters
  (see §11).
- **Diagnostics** — the commissioning toolbox: bus monitor, register probe,
  discovery, SunSpec scan (see §13).
- **Status** — pipeline health, error taxonomy, events, alerts, resource
  footprint (see §17).
- **Config** — global settings: MQTT, InfluxDB, General (timezone, colors),
  Security, Backup & Snapshots.

---

## 5. Devices & device templates (multi-device)

One install reads **several sources**. Each device pairs a **connection**
with a **device template** (the register map of that equipment type) and its
**own data routing** (MQTT topic prefix, InfluxDB bucket + tag, output
toggles).

### 5.1 Discovery — find the device first

Devices → *Discover devices*:

- **Modbus TCP scan** — sweeps a private CIDR (max /24) on a port (default
  502) for devices that answer. Read-only; LAN-restricted.
- **Unit-ID sweep** — walks unit/slave IDs (default 1–32) on one TCP host or
  one RTU serial line.
- **SunSpec scan** — walks the SunSpec model chain (the `SunS` marker at
  40000/50000/0, then every declared model, identity block included). The
  natural first step for Fronius/SolarEdge/Huawei hardware.
- **MQTT topic browse** — connects to a broker and shows its speaking topics
  with payload previews (retained topics appear instantly; a short listen
  window catches live publishers), so you pick a topic instead of typing it.
- **Fronius Solar API discover** — enumerates inverters/meters behind a
  DataManager, with the hint that the meter sits at Modbus unit 240.

*Use* on any result prefills the Add Device wizard.

### 5.2 Add a device (wizard)

1. **Connection** — pick the protocol:
   - **Modbus TCP**: host, port, unit ID, timeout.
   - **Modbus RTU**: two modes (see [rtu-serial.md](rtu-serial.md)).
     **Over network (recommended)** — press **Scan** and pick a USB adapter
     from the serial bridge (plug in → appears; unplug → gone), MBG stays
     unprivileged. **Direct serial** — serial port (e.g. `/dev/ttyUSB0`),
     baud, parity, stop bits, with the adapter mapped into the container.
   - **HTTP/JSON**: a URL returning JSON; each register extracts its value
     with a `json_path` (e.g. `Body.Data.PowerReal_P_Sum`). URLs must point
     at a private LAN host unless `security.allow_nonlan_http_devices` is
     set (SSRF guard).
   - **MQTT**: broker, port, credentials/TLS, a subscribe topic; each
     register reads from the JSON payload by `json_path` or takes the bare
     payload as the number, and may use its own topic with `+`/`#`
     wildcards.

   Press **Test connection**: for Modbus, *any* protocol-level answer — even
   an exception — proves a live device; for HTTP with a template chosen the
   test reports how many `json_path`s resolved; for MQTT it connects and
   waits briefly for a sample message.
2. **Template** — choose from the library (11 bundled maps, see
   [device-catalog.md](device-catalog.md)), **upload** a `.json` template
   (validated row by row; id conflicts ask before overwriting), **create**
   one in the editor, or **import a CSV** register map
   ([csv-import.md](csv-import.md)). Built-ins are read-only — *Duplicate to
   edit*. A template used by a device cannot be deleted.
3. **Data routing** — the device **id** becomes the routing key: values
   publish under the device's MQTT topic prefix (live preview) and land in
   its InfluxDB bucket (auto-created, 90-day retention) tagged with its
   device tag. **Pitfall:** the routing identity is fixed after creation —
   changing it would orphan history and Home Assistant entities.

After creation the device polls immediately: curated templates auto-select
their recommended register subset (the Janitza map selects 58 of 4,126); an
uncurated template with more than 300 registers selects none, to avoid
flooding MQTT/InfluxDB with thousands of series in one click.

**Existing installs:** the original meter appears automatically as device #1
with its routing identity locked — topics, bucket, tags and HA entities stay
byte-identical.

**Deleting a device** keeps its register file on disk (data safety) and is
blocked while any virtual meter sources it.

---

## 6. Registers, poll groups & thresholds

The device workspace → **Registers**:

- **Available** — the catalog from the device's template (browse/search;
  4,126 entries for the Janitza). Add a **custom register** by hand if the
  map misses one.
- **Selected** — what is actually polled. Per register: poll group, data
  type, scale (engineering value = raw ÷ scale), register type
  (holding FC3 / input FC4 / coil FC1 / discrete FC2), MQTT topic,
  InfluxDB measurement + tags, dashboard widget, and **thresholds**.

**Poll groups** (`realtime` / `normal` / `slow` by default) each poll at
their own interval — editable per device from 0.05 s to 24 h, applied live.
Registers in one group are merged into batch reads bridging gaps up to
`max_gap` (default 10); strict slaves that reject merged blocks with
*illegal data address* should set `max_gap: 0`.

**Thresholds** color-code a value (warningLow/High, dangerLow/High) on the
dashboard and gauges. They are per register, visual only — for *alerting*
see §12 (infrastructure) or use a downstream system for value alarms.

Saving the selection hot-reloads only that device's pollers.

---

## 7. Calculated registers

Device workspace → **Calculated**: derive new measurements by formula from
live values. Examples:

| Goal | Expression |
|---|---|
| Power factor | `_G_P_SUM3 / _G_S_SUM3` |
| Phase sum | `p_l1 + p_l2 + p_l3` |
| Unit conversion | `energy_wh / 1000` |
| Current imbalance % | `(max(i1,i2,i3) - avg(i1,i2,i3)) / avg(i1,i2,i3) * 100` |
| Average power from an energy counter | `(E - prev(E)) / dt * 3600` |
| Cross-device | `grid.p_total + pv.p_total` |

- The builder offers clickable measurement chips, a function/operator
  palette (`min max avg abs round sqrt pow floor ceil clamp`, `pi e`,
  comparisons, `a if cond else b`), a **live preview**, and presets — you
  can save your own formulas as reusable presets.
- `prev(x)` and `dt` make rate calculations possible; a stateful formula
  cannot be previewed one-shot (the UI says so) — it produces values from
  the second evaluation on.
- Expressions are validated against a safe whitelist before saving; at
  runtime a missing input skips that round (no partial or fabricated
  values), and an error can never kill the poller.
- A calculated value flows to **every** output (MQTT, InfluxDB, virtual
  meters, feeds) and shows up in Monitor/History like any measurement. Its
  poll group decides how often it recomputes.

---

## 8. MQTT & Home Assistant

Config → **MQTT**: broker, port, credentials, QoS, retain, and TLS
(port 8883; CA certificate to verify the broker, optional client cert+key
for mutual TLS — put the files under `config/` and reference their
in-container paths; "skip verification" is for testing only).

- **Topics**: `<device prefix>/<register topic or derived name>`. Device #1
  keeps its historical prefix; new devices default to the
  `mqtt.default_topic_pattern` (`meters/{device}`).
- **Publish mode**: `changed` (default — only values that changed publish;
  the change cache confirms only after a successful publish, so a broker
  outage loses nothing) or `all` (every reading).
- **Availability**: a Last-Will marks `<prefix>/status` = `offline` if the
  gateway dies; `online` is retained on connect.
- **Home Assistant discovery**: on by default. Each device becomes an HA
  device with its selected registers as sensors (`unique_id`
  `mbg_dev_<device>_<addr>_<name>` for non-primary devices), with sensible
  device/state classes from the units. Virtual meters publish their own
  diagnostic entities (serving state, request rate, errors, freshness…).
  Deleting a device clears its retained discovery so HA drops the entities.

**Pitfall:** with `retain: true` (default) a consumer that subscribes late
still sees the last value — but after a broker restart without persistence,
values reappear only as they next publish; the gateway clears its change
cache on every reconnect and republishes the full state for exactly this
reason.

---

## 9. InfluxDB & Grafana

Config → **InfluxDB**: URL, token, org, bucket. Per-device buckets are
auto-created with 90-day retention; per-register measurement/tags are set in
the Registers tab. Point Grafana at the same bucket. The optional compose
profiles start a local InfluxDB + Grafana (`--profile influxdb --profile
grafana`).

**Data guarantees.** Every point is stamped with the *read* time, not the
flush time. If InfluxDB becomes unreachable, points go to a
**store-and-forward buffer** (default 10 minutes / 50,000 points — tune
`influxdb.buffer_minutes` / `buffer_max_points`) and are replayed with their
original timestamps on reconnect, idempotently. With
`influxdb.buffer_persist: true` (default) the buffer also survives a
restart during the outage (`config/influx_buffer.jsonl`). Batches the client
gives up on after its ~5 min of internal retries are recovered into the same
buffer. Outages longer than the window drop the oldest points; for the
Janitza's voltages the meter's onboard recording can backfill those via
`python -m multibus.backfill`. Watch `buffer_points` / `replayed_total` /
`dropped_total` in `/api/status` or `gateway_influx_buffer_points` in
`/metrics`.

MQTT is deliberately **not** replayed: it is a live bus — on reconnect the
current state is republished instead.

Per-device **History** (aggregated lines with a min/max band) and **Energy**
(monthly totals from cumulative counters + daily breakdown, in the timezone
from §18; the Energy tab's counter selection is per device) read this data
back.

---

## 10. REST push & the HTTP/JSON feed

Device workspace → **Outputs** — two additional per-device sinks, both
opt-in:

- **HTTP/JSON output** — serve the device's live values as read-only JSON at
  `GET /api/meters/<id>` (Solar-API style, keyed by register name, with a
  `stale` flag). Nothing to poll from the gateway side; any HTTP client
  pulls it. Note: when login is enabled the feed requires a session or the
  consumer's IP being covered by your access design.
- **REST push** — POST the device's values as JSON to an external URL on an
  interval (min 5 s): `{enabled, url, interval_s, headers, format, verify_tls,
  timeout}`. `format: native` sends `{name: {value, unit, ts}}`; `flat`
  sends `{name: value}`. Auth goes in `headers` (stored masked, preserved on
  save). Push targets *may* be external (unlike HTTP device inputs), but
  redirects are refused so your credentials can't be replayed elsewhere.
  Delivery is fire-and-forget; the card shows the last status and **Test**
  pushes once immediately.

---

## 11. Virtual Meters — step by step

Goal: let another system (Victron ESS, a Fronius inverter, any SunSpec
client) read this gateway as the meter *it* expects.

> ⚠️ A virtual meter can feed a control loop. Do steps 11.1→11.3 (validate
> in parallel) before you ever make it a consumer's only meter.

**11.0 — Publish the ports (once).** The compose file publishes
`1502–1512` (plus `502`). Pick instance ports inside the range; widen it via
`VMETER_PORT_START/END` + recreate the container if you need more.

**11.1 — Pick or create a template.** Virtual Meters → **Templates**.
Shipped: `em24_av53` (Carlo Gavazzi EM24 → Victron), `fronius_ts_native`
(Fronius Smart Meter TS → DataManager), `fronius_sunspec_meter` (SunSpec 213
float). A template row = address, type, scale, word order, and a **source**:
a live register (`name`, or `device.register` for another device), a
constant, or a `sum` of several sources. Import/export as YAML.

**11.2 — Add an instance.** On the **Meters** tab: template, a free port,
unit id, source device, staleness policy → **Add instance**. It starts
**disabled**.

**11.3 — Validate in parallel.** Enable it and point a *test* consumer — or
just watch the **Logs** tab — at `host:port`. The query log shows exactly
what the consumer reads, when, and what was answered (last 1024 requests).
Compare against the real meter. **Stats** shows rate, errors, most-read
registers; **Decode** interprets any address range back to its source
variables.

**11.4 — Choose the staleness policy** (`on_stale`) — what a stale/missing
row serves:

| Policy | Behavior | Use for |
|---|---|---|
| `legacy` (default) | classic single-source behavior: one freshness watchdog per instance; gaps keep last words | existing meters — untouched |
| `fail` | any read touching a stale row → **Modbus exception** (no partial truth) | control consumers (Victron, PLC) |
| `sentinel` | SunSpec N/A: float→NaN, int16→0x8000, uint16→0xFFFF… | sentinel-aware consumers |
| `hold` | last value up to `max_hold_s` (default 30 s), then like `fail` | tolerant displays |

Absence is **never** served as 0/false. Sums take the quality of their worst
member. In policy modes the server stays up while at least one source is
fresh; all dead → it stops responding so the consumer's own meter-loss
fail-safe engages.

**11.5 — Optional in-band quality block.** If the consumer (PLC/SCADA) needs
to know the data quality on the same Modbus connection, enable *In-band
quality block* on the instance. It serves a read-only block at **61440**
(identical for every meter):

| Address | Type | Meaning |
|--------:|------|---------|
| 61440 | u16 | format version (1) |
| 61441 | u16 | 0 legacy · 1 ok · 2 degraded · 3 stale |
| 61442–61444 | u16 | fresh / stale / missing rows |
| 61445 | u16 | total data rows |
| 61446 | u32 | age of newest fresh value (s); 0xFFFFFFFF = never |

Typical consumer guard: *use the data only while 61441 == 1; alarm on 3*.
Full wire spec: [virtual-meter-spec.md](virtual-meter-spec.md).

**11.6 — Cut over.** Point the real consumer at the virtual meter. The
freshness watchdog is the safety net. The same map is also served as JSON at
`/api/virtual-meters/<id>/values` under the aggregator convention
(`value: null` + `quality` + `age_s`, `last_value` separate) — a SCADA can
read everything in one poll.

**Pitfalls:** the meter responds on any unit id (the configured one is
informational); reads outside the emulated map answer *illegal data
address* by design (consumers fingerprint meters by probing low addresses);
deleting a source device is blocked while a meter uses it.

---

## 12. Alerts & webhooks

Infrastructure-health alerting (a device or sink goes down, read latency
stays high, the InfluxDB buffer backs up) — **not** value alarms. Configure
under Config → Alerts or the `alerts:` block:

```yaml
alerts:
  enabled: true
  mqtt: true                                   # publish to <topic_prefix>/alert
  webhook_url: "https://ntfy.example/gateway"  # empty = webhook off
  webhook_headers: { "X-API-Key": "secret" }
  webhook_body: { "message": "[{severity}] {source}: {message}" }
  min_interval_s: 300
  latency_ms: 1000
  buffer_points: 1000
  signals: { device: true, sink: true, latency: true, buffer: true }
```

Alerts are rate-limited per key (`min_interval_s`), mirrored to the event
log and the Status page. The **Test** button (or `POST /api/alerts/test`)
fires a synthetic alert through the real channels — it requires login or an
API key and is cooldown-throttled, because it drives real outbound traffic.
Webhook delivery is best-effort (no retry) and refuses redirects. Details:
[alerts-webhooks.md](alerts-webhooks.md).

---

## 13. Diagnostics

The commissioning toolbox (Diagnostics page). Everything here is read-only
on the bus.

- **Bus monitor** — frame-level capture of every Modbus transaction: TX/RX
  hex, decoded function/address/count, result
  (ok / exception / no-response / CRC error), latency — and **each retry as
  its own entry**, so you see exactly what a flaky link does. Runtime-only
  ring buffer (default 1000, up to 20000 frames): it boots disabled and
  never persists, so a forgotten session can't grow into the RAM of a box
  that's been up for a year.
- **Register probe** — one-shot read of any address (FC1–4, count 1–8) on
  any Modbus device, decoded **every plausible way**: a matrix of data type
  (uint16/int16, float/int32/uint32, double/int64/uint64) × word order
  (ABCD / CDAB / BADC / DCBA), plus raw hex and ASCII. This is the
  endianness workbench: when a value reads as garbage, the correct
  type+order combination is usually staring at you in the matrix. NaN is
  shown as a finding (SunSpec "not available"), not hidden.
- **Payload sample** — grab one full JSON payload from a saved HTTP or MQTT
  device (retained MQTT → instant) to drive the `json_path` picker.
- **Error taxonomy** — read failures are counted by kind: `timeout` (no
  answer), `exception_N` (the device answered with Modbus exception N —
  the link is fine, the request is wrong), `connection` (TCP/serial-level).
  Surfaced per device on Status and `/metrics`.

---

## 14. Modbus writes & dead-man leases

Writing to field hardware is guarded in depth. All of it is **off by
default**.

1. Enable `security.allow_writes: true` (Config → Security).
2. Writes must be **authenticated** — enable login or set `API_KEY`
   (anonymous writes are refused even with the gate open).
3. The register must be declared **writable in the device template**, with
   optional `write_min` / `write_max` bounds; the encoding (data type,
   scale) always comes from the template row, never from the caller.
4. The **primary device is always read-only**; HTTP/JSON devices and
   input/discrete registers can't be written.
5. Per-IP rate limit (`security.write_rate_limit_per_s`, default 10/s).

`POST /api/devices/<id>/write` with `{"address": ..., "value": ...}` writes
FC6/FC16 (holding) or FC5 (coil), verifies by read-back, and logs every
attempt to the audit trail.

**Dead-man leases.** Add `"lease_ms": 5000` and the write arms a lease: if
it is not renewed (by another leased write) within 5 s, the gateway reverts
the register to the template's declared `write_safe` value. Leases are
**crash-safe**: the active lease set persists to disk, so if the gateway
itself crashes, the registers revert to safe on the next boot. This is the
correct primitive for export-limit / power-setpoint control loops: a crashed
controller can't leave a dangerous setpoint standing. Active leases:
`GET /api/writes/leases`.

---

## 15. Config safety: snapshots, rollback, backup

Config → **Backup & Snapshots**.

- **Automatic snapshots** — every successful config change (devices,
  registers, templates, virtual meters, settings) takes a snapshot of the
  whole config bundle (2 s debounce coalesces bursts). 50 kept; manual
  snapshots with a note anytime.
- **Semantic diff** — any snapshot diffs against the live config or another
  snapshot **key by key** (not line noise): *what would a rollback undo*.
  Secret values are masked.
- **Rollback** — restore replaces the bundle verbatim, after taking a
  `pre-restore` snapshot, so a rollback is itself reversible.
- **Last-known-good (LKG) boot seatbelt** — after ~5 minutes of healthy
  uptime the bundle is marked LKG. If `config.yaml` fails to parse at boot
  (bad manual edit, torn write), the gateway restores LKG automatically and
  boots — an unattended box comes back up. A corrupt file also blocks saves
  (a copy is kept as `config.yaml.bad`) so defaults can never overwrite your
  real config.
- **Backup export/import (ZIP)** — for portability between hosts. The
  export **strips secrets** (MQTT/Influx credentials, password hashes,
  webhook/REST-push headers) and host identity by default;
  `include_secrets=true` requires the admin role or the API key and is
  audit-logged. Import (raw ZIP body, ≤25 MB) **merges** over the live
  config so stripped secrets survive, and takes a `pre-import` snapshot
  first. Snapshots, by contrast, are full-fidelity local restore points —
  downloading one is gated like a with-secrets export.

**Pitfall:** snapshots live under `config/snapshots/` inside the config
volume — they protect against bad edits, not against losing the volume.
Keep an exported ZIP somewhere else too.

---

## 16. Security

Everything is **off by default** — the appliance targets a trusted LAN.
Turn on layers from **Config → Security** as exposure grows. Defense in
depth: each layer applies independently.

### 16.1 Login & roles

One **admin** account, plus optional **operator** and **viewer** accounts:

| Role | Can | Cannot |
|---|---|---|
| `viewer` | see everything (GET), run the on-demand register queries | change anything |
| `operator` | live actions: diagnostics, bus trace, discovery, device tests, payload samples, **Modbus writes** (within template bounds), alert test, register reload, own passkeys | anything that lands in a config file (devices, registers, templates, vmeters, settings, snapshots), audit trail |
| `admin` | everything | — |

Passwords are hashed (PBKDF2-SHA256, 600k iterations); leave a password
field blank on save to keep the current one. **Enabling login refuses the
default admin/admin** — set a real password first. Failed logins are locked
out per IP (`lockout_threshold` / `lockout_minutes`, defaults 5 / 5 min).
Sessions are HttpOnly cookies, 7-day sliding, in-memory — a container restart
logs everyone out. The audit trail is admin-only.

### 16.2 Passkeys (WebAuthn)

Passwordless login with a platform authenticator or security key. Enroll
from the user menu (each account manages its own; admin sees all).
Requirements and pitfalls:

- Browsers run WebAuthn only in a **secure context**: open the UI via
  `localhost` (on the box) or via a **hostname over HTTPS** (e.g.
  `gateway.lan` behind Traefik). A raw IP address is rejected — the RP ID
  must be a hostname.
- A passkey is bound to the hostname it was enrolled on; a different name =
  enroll again.
- Passkey login shares the password lockout, and you can enroll **before**
  enabling login, so you're never locked out mid-migration.

### 16.3 HTTPS & reverse proxy (Traefik)

- **Built-in TLS**: point Config → Security at a cert + key under
  `config/`, or leave blank to auto-generate a self-signed pair
  (**restart to apply**; browsers will warn on self-signed).
- **Behind a reverse proxy** (recommended for real certificates): terminate
  TLS in Traefik/nginx/Caddy and set `ui.trusted_proxies` to the proxy's IP
  (e.g. the Traefik container's address). Only then are `X-Forwarded-For` /
  `X-Forwarded-Proto` honored — otherwise the IP allowlist, login lockout
  and audit trail would all see the proxy instead of the real client, and
  session cookies wouldn't be marked Secure. Empty (default) = trust
  nobody. Minimal Traefik idea: route `gateway.example.com` → `:8080`, and
  add the gateway container's network IP to `trusted_proxies`.
- **Canonical address** (`ui.canonical_url`): once the box is reachable by a
  proper hostname over HTTPS, set this (e.g. `https://gateway.lan`). The UI
  then injects a tiny client-side redirect that steers any visitor who opened
  it by raw IP or plain HTTP onto the canonical origin — so cookies, passkeys
  and HSTS all bind to the one hostname. It's a browser-side steer, not a
  server redirect, so the IP is never *blocked*: append **`?local`** to the URL
  to stay on the IP (it sets a sticky `mbg-stay-local` flag in that browser),
  which is the escape hatch when DNS/the proxy is down and you must reach the
  box directly.

### 16.4 IP allowlist

`security.allowlist` — one IP/CIDR per line (e.g. `192.168.1.0/24`); empty
= open. Loopback is always allowed; it also guards `/ws`, `/health` and
`/metrics`. The card shows *your current IP* so you don't lock yourself
out. **Docker note:** behind the default bridge network, clients often
appear as the docker gateway IP — check "your current IP" and allowlist
what you actually see, or use host/macvlan networking for true per-client
filtering. Locked out anyway? Edit `security.allowlist` in
`config/config.yaml` and restart.

### 16.5 API key

Set `API_KEY` in the environment to require `X-API-Key` on every
state-changing request, independent of login — useful for scripts and CI.
Read-only GETs and the on-demand query POSTs stay open.

### 16.6 Audit trail

Append-only JSONL under `config/audit.jsonl` (1 MB × 5 files rotation):
logins (including failures and lockouts), passkey events, **every Modbus
write**, config exports/imports, snapshot restores — with user, IP, action,
target, status; secret values in payloads are redacted before they are
written. View/filter it on the Status page (admin), or export CSV via
`GET /api/audit/export.csv`.

---

## 17. Observability: Status, /metrics, events

- **Status page** — per-device health (connected, poll rate, error
  taxonomy, staleness age, latency), sink stats (published/skipped/failed,
  buffer counters), WebSocket clients, process resources (CPU, RSS,
  threads, FDs), recent events and alerts.
- **`/metrics`** — Prometheus text format, no login required (a scraper
  can't log in), still behind the IP allowlist; counters and health only,
  never config. Series: `gateway_device_up/poll_rate/reads_total/
  errors_total/read_latency_ms/staleness_seconds/health`,
  `gateway_mqtt_connected/published_total`,
  `gateway_influx_connected/written_total/buffer_points/dropped_total`,
  `gateway_vmeter_up/requests_total/request_rate/errors_total/connections/
  quality`. Scrape example:

  ```yaml
  scrape_configs:
    - job_name: multi-bus-gateway
      static_configs: [{ targets: ["gateway:8080"] }]
  ```
- **`/health`** — container probe. Returns HTTP 503 **only** when an
  enabled virtual meter is genuinely down (something a restart may fix); an
  unreachable upstream meter degrades the body status but stays HTTP 200 —
  restarting the container can't fix your wiring, and the vmeter watchdog
  already fail-safes consumers.
- **Event log** — persisted ring (`config/events.jsonl`, last 300): read
  failures, sink connect/disconnect, vmeter lifecycle, rollbacks, alerts.

---

## 18. Languages & timezone

- **Languages** — the UI ships English and Romanian; the selector is in the
  title bar. Languages are plain files: copy `ui/languages/en.json` to
  `<code>.json`, translate, and it appears in the selector — no rebuild.
  See `ui/languages/README.md`.
- **Timezone** — Config → General. An IANA zone (validated) that drives the
  monthly-energy calendar boundaries; applied live. Default
  `Europe/Bucharest`.

---


## 18b. Running on constrained hardware (Raspberry Pi)

Multi-Bus Gateway runs comfortably on a Raspberry Pi 3/4/5 or a low-power
Intel box. It is frugal by design — at a typical single-meter install it uses
~90 MB RAM and a few percent CPU, with bounded buffers and no leaks.

**The one thing that matters on a Pi: the realtime poll interval.** Everything
the gateway does per value (read, parse, publish, buffer) happens under one
Python GIL, so total CPU scales with *polls per second*, and a Cortex-A53 core
is ~8–10x slower than a desktop Intel core. RAM and thread count are never the
wall — the CPU at a fast cadence is.

**Capacity envelope (measured + projected):**

| Setup | RSS | Threads | RPi 3 CPU @ realtime 1 s | RPi 3 @ 250 ms |
|-------|-----|---------|--------------------------|----------------|
| 1 device | 65–80 MB | ~14 | 3–5 % | 10–18 % |
| 5 devices + 3 vmeters | 90–110 MB | ~30 | 15–20 % | 55–75 % |
| 10 devices + 10 vmeters | 130–160 MB | ~58 | 35–50 % | saturates a core |

**Recommended settings on a Pi 3:**

- Keep **realtime at 1 s** for general monitoring. Reserve sub-second polling
  (e.g. 250 ms) for the *one* register that drives a real-time control loop —
  a grid meter feeding an inverter export limit — not for the whole map.
- Use **normal (5 s)** and **slow (60 s)** poll groups for everything that is
  not control-critical (energy counters, temperatures, diagnostics).
- The interval floor is 50 ms; `0` is refused (it would flood the bus).
- A Pi 4/5 has ~2–3x the headroom of a Pi 3 — the 250 ms cadence is fine there
  for a small map.

**Rule of thumb:** an RPi 3 comfortably runs **~4–5 devices + ~3 virtual
meters** with realtime ≥ 1 s. Beyond that, scale *out* (a second Pi / host per
bus), not *up* — the GIL can't be scaled away inside one process.


## 18c. What consumers see when a source is lost

The gateway's core promise: **it never reports absence as a plausible value.**
When a device stops responding, a read fails, or a connection drops, no output
ever receives an invented `0`/`false`/last-guess. Absence stays absence — but
it *looks* different on each output, so a downstream system (Node-RED, Home
Assistant, a PLC, Grafana) must read the right signal.

### Per-output behaviour

| Output | On source loss | How the consumer detects it |
|--------|----------------|-----------------------------|
| **MQTT** | value topics stop updating; the retained topic holds the LAST value | subscribe to `<prefix>/status` (retained) + the LWT — `offline` means stale; do NOT trust a value topic alone |
| **InfluxDB** | no write → a **gap** in the series (never a flat-line of the old value) | `last()` + timestamp age, or a "no data" alert |
| **Virtual meter** | your chosen policy: `legacy` holds last words · `fail` returns a Modbus exception · `sentinel` serves SunSpec NA (NaN / 0x8000 / 0xFFFF) · `hold` holds up to a cap then fails. If ALL sources are stale the server stops (connection refused, like an unplugged meter). | the exception / refused connection, and the in-band **quality block at 61440** (state + age) |
| **HTTP push / output** | no new push (the last payload is not re-sent as fresh) | absence of an update; NaN/inf are rejected, never emitted as invalid JSON |

### The one thing to configure downstream

On **MQTT**, the retained *value* carries no per-value freshness stamp — the
freshness signal lives on the `<prefix>/status` topic and the Last-Will. A
consumer that reads only the value and ignores status can act on stale data.
This is why a control loop should gate on freshness (e.g. Node-RED's
`armed ∧ leader ∧ fresh-telemetry`), not merely on "I have a value". If you
need in-band freshness **without** MQTT, give the virtual meter the `fail`
policy and read the quality block at 61440.

### How this compares to other equipment

This behaviour follows the established standards rather than inventing its own:

- **SunSpec** (the de-facto solar/meter model) defines the exact "not
  accessible" sentinels — NaN for float, `0x8000`/`0xFFFF` for integers — that
  the `sentinel` policy emits, so a SunSpec-aware consumer understands it
  natively.
- **Home Assistant / MQTT**: the `<prefix>/status` availability topic + Last
  Will is the standard availability pattern the whole ecosystem consumes.
- **Time-series (InfluxDB / Prometheus)**: a gap (no write) is the idiomatic,
  correct representation of missing data — you never backfill a stale value.

Where common industrial Modbus gateways default to silently *holding the last
value with no staleness signal* — the dangerous footgun — this gateway offers
that (`hold`) only as an explicit choice, alongside the safer `fail` (a
meter-loss exception) and the in-band quality block. In short: nothing risky is
reinvented; where the industry has a footgun, the correct alternative is
provided.

## 19. Troubleshooting

| Symptom | Check |
|---------|-------|
| Modbus dot red | host/port/unit id correct? Modbus TCP enabled on the device? firewall? Use Diagnostics → probe: an exception answer still means the device is alive |
| Values look like garbage | wrong data type or word order — run the register probe and read the type×order matrix |
| A merged read fails with *illegal data address* | strict/gapped slave — set the device's `max_gap: 0` |
| UI shows an old version after update | hard-refresh (the bundle is cache-busted, but proxies can cache) |
| Virtual meter "stale / starting" | the source isn't fresh — check the device connection; the watchdog refuses to serve stale data by design |
| Consumer can't reach a virtual meter | port inside the published compose range? reachable from the consumer's network? watch the Logs tab for incoming reads |
| MQTT entities missing in HA | broker reachable? discovery enabled? check `docker compose logs` |
| InfluxDB write-retry warnings | URL/token/bucket correct? The client retries ~5 min, then the batch is recovered into the buffer and replayed — watch `replayed_total`/`dropped_total` |
| Login refuses to enable | set a new admin password first — the default `admin` cannot be used |
| Locked out (login) | wait `lockout_minutes`, or restart the container (sessions/lockouts are in-memory) |
| Locked out (IP allowlist) | edit `security.allowlist` in `config/config.yaml`, restart |
| Passkey enrollment fails | you're on an IP URL or plain HTTP — use `localhost` or a hostname over HTTPS |
| Config broke after an edit | boot restores last-known-good automatically; the broken file is kept as `config.yaml.bad`; or roll back a snapshot from Config → Backup |
| Writes return 403 | `security.allow_writes` off, no credential (login/API key), register not declared writable, or value outside `write_min`/`write_max` |

Still stuck? Open an issue — include `docker compose logs` and your
(redacted) config.
