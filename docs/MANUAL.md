# User Manual — Multi-Bus Gateway

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
- **[alerts-webhooks.md](alerts-webhooks.md)** — infrastructure + value-threshold alerting.

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
11b. [Device Builder — remote ESP32 nodes (ESPHome)](#11b-device-builder--remote-esp32-nodes-esphome)
12. [Alerts & webhooks](#12-alerts--webhooks)
13. [Diagnostics](#13-diagnostics)
14. [Modbus writes & dead-man leases](#14-modbus-writes--dead-man-leases)
15. [Config safety: snapshots, rollback, backup](#15-config-safety-snapshots-rollback-backup)
16. [Security](#16-security)
17. [Observability: Status, /metrics, events](#17-observability-status-metrics-events)
18. [Languages & timezone](#18-languages--timezone)
19. [Troubleshooting](#19-troubleshooting)

---

> The defensive design behind everything in this manual — every fail-safe,
> delivery guarantee and self-heal — is cataloged in
> [reliability.md](reliability.md).

## 1. What you need

- At least one southbound source: a **Modbus TCP** device (e.g. a Janitza
  UMG 512-PRO — port 502 enabled), a **Modbus RTU** device on a serial line
  (`/dev/ttyUSB0` + RS-485 adapter), an **HTTP/JSON** endpoint (Fronius Solar
  API, Shelly, Tasmota…), or an **MQTT** broker with telemetry topics.
- A host with **Docker + Docker Compose** (amd64 or arm64 — a Raspberry Pi
  works).
- *(Optional)* your **own** MQTT broker and/or InfluxDB, if you prefer them
  over the bundled ones — the default compose stack ships both.

---

## 2. Install (Docker)

```bash
# 1) Get the code
git clone https://github.com/sm26449/multi-bus-gateway.git
cd multi-bus-gateway

# 2) Create your environment file (optional — everything is configurable in the UI)
cp .env.example .env

# 3) Start the COMPLETE stack — gateway + MQTT broker (mosquitto) +
#    MQTT Explorer + InfluxDB + Grafana + ESPHome. Everything the product
#    needs ships in this one file; nothing external to install.
docker compose up -d

# 4) Grab the generated admin password (first boot only), then open the UI
docker compose logs multi-bus-gateway | grep -A3 'FIRST RUN'
#    http://<host>:8080
```

Out of the box the pipeline is live end-to-end: the gateway publishes to
the bundled broker (its default broker host is `mosquitto`), **MQTT
Explorer** on `:4000` shows every topic flowing, and InfluxDB self-
configures on first boot (org/bucket `multibus`; change the password/token
in `.env`, then enable the sink in Config → InfluxDB and paste the token —
Grafana is on `:3000`). Want less? Start only what you need:
`docker compose up -d multi-bus-gateway mosquitto`. Prefer your own broker
or InfluxDB later? Point the gateway at them from the UI — the bundled ones
are ordinary containers you can simply stop.

Already running a wider stack on the shared network? The base file *names*
its network `pv-stack-network` (override: `PV_STACK_NETWORK` in `.env`), so
a fresh install creates it and later services can join. If the network —
and the broker/InfluxDB — **already exist**, use the overlay and start only
the gateway services:

```bash
docker compose -f docker-compose.yml -f docker-compose.pv-stack.yml \
  up -d multi-bus-gateway
```

Ports published by the default compose file: `8080` (UI/API), `1883` +
`9001` (bundled MQTT + WebSockets), `4000` (MQTT Explorer), `8086`
(InfluxDB), `3000` (Grafana), `1502–1512` (virtual-meter range, grow via
`VMETER_PORT_START/END`), and `502` (standard Modbus, for consumers that
insist on it — drop it if the host already uses it). The containers run **non-root** (gateway uid 10001; the
serial bridge uid 10002 with `/dev` read-only and tty-only device access),
which is why the compose sets `net.ipv4.ip_unprivileged_port_start=0` —
scoped to the container's own network namespace, so an unprivileged process
may bind `:502`; nothing changes on the host. For an RTU device either
start the bundled serial bridge — `docker compose --profile rtu-bridge up
-d` (recommended; see [rtu-serial.md](rtu-serial.md)) — or pass the adapter
through directly: `devices: ["/dev/ttyUSB0:/dev/ttyUSB0"]` in a compose
override.

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
| `UI_HOST` | bind address — bare-metal default is loopback (`127.0.0.1`); the container image sets `0.0.0.0` (exposure is governed by the compose port mapping) | `0.0.0.0` |
| `API_KEY` | require `X-API-Key` on mutating requests | — |
| `VMETER_PORT_START` / `VMETER_PORT_END` | virtual-meter port range | `1502` / `1512` |
| `TZ` | timezone for the bundled ESPHome container and timestamps | `Europe/Bucharest` |
| `ESPHOME_URL` | where the gateway finds the ESPHome dashboard (Device Builder); on a fresh deploy it also enables the section | `http://esphome:6052` |
| `ESPHOME_ENABLED` | force the Builder on/off at every start (otherwise the UI decides) | — |
| `ESPHOME_DASHBOARD_USERNAME` / `ESPHOME_DASHBOARD_PASSWORD` | login on the ESPHome dashboard; the same values also configure the gateway's client | — |

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
- **ESPHome scan** — sweeps the LAN on the native-API port (6053) and asks
  each node for its identity (name, version; nodes with an encrypted API are
  still detected). Works from Docker — the scan is unicast, so it doesn't
  depend on mDNS/multicast, which doesn't cross the Docker bridge. Nodes that
  announce their adoption package can be **imported directly** into the
  Device Builder (they become managed nodes: build/OTA from here).
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
     unprivileged. **One master per bridged line** is enforced: a second
     device on the same bridge endpoint is rejected at validation (two
     masters would evict each other forever), and Test-connection refuses
     an endpoint a running device is polling. **Direct serial** — serial
     port (e.g. `/dev/ttyUSB0`), baud, parity, stop bits, with the adapter
     mapped into the container.
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
   one in the editor, or **import a CSV or YAML** register map
   ([csv-import.md](csv-import.md), [yaml-import.md](yaml-import.md)). Built-ins are read-only — *Duplicate to
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
dashboard and gauges. They are per register — and the same limits can also
fire **alerts** on crossings: enable the built-in threshold engine
(`alerts.signals.threshold`, see §12).

Saving the selection hot-reloads only that device's pollers.

### 6.1 Canonical field names

So automations, dashboards and predictions stay predictable, a register's
**name** is drawn from a shared dictionary — the same physical quantity is named
the same on every device (`voltage_l1_n` everywhere, not `ull_0` on one meter
and `v_l1` on another). The name becomes the MQTT topic leaf, the InfluxDB
field, and the `name` tag; the MQTT topic is hierarchical (`voltage/l1_n`) while
the InfluxDB field is the flat canonical name. The full list is in
[`docs/canonical-fields.md`](canonical-fields.md) (56 fields). Convention:
`<quantity>_<position>` — `voltage_l1_n`, `current_l2`, `power_active_total`,
`energy_active_import`, `frequency`, …

- **In the register editor**, the *Name* field autocompletes from the
  dictionary, flags a non-canonical name with a "did you mean …?" hint, and
  auto-fills the MQTT topic + InfluxDB measurement when the name is canonical.
- **In the template editor** (Templates → *New map* / *Edit*), each row's name
  is checked live, a "**N/M canonical**" summary is shown, and **Auto-canonicalize**
  infers canonical names for a cryptic imported map from each row's label/unit.
  It is deliberately conservative — anything it isn't sure about is left amber
  for you to set, never renamed to the wrong thing. Review the grid, then Save.
- A template promises canonical names with `"canonical": true`; non-canonical or
  duplicate names then show as a warning in the Template manager.

**The unit is part of the contract.** Each canonical field carries a
canonical unit (see the Unit column in
[`canonical-fields.md`](canonical-fields.md)); energy is the base **Wh
family** (Wh/varh/VAh). A meter whose native map is kWh converts in its
selection **scale** (`scale/1000`) — the value it publishes must already be
the canonical unit, or every cross-device dashboard, vmeter binding and
failover twin inherits a silent ×1000 error. Two guards enforce this:

- **In the template editor**, the *Unit* cell shows the canonical unit as a
  tooltip for canonical names and turns **amber live** when the typed unit
  differs, with the fix spelled out (adjust the scale, or rename the row).
- **At save time**, selecting a canonical name with a mismatched unit logs
  a warning (the save still goes through — the warning is the tripwire).

Vendor reference maps (e.g. the Janitza UMG512) predate this and keep their
native names — the scheme applies to the templates you build/import.

### 6.2 Polling & decode hygiene (opt-in)

Everything here is **off by default** — enable it globally, per device or per
register when the hardware needs it.

- **Startup jitter** — `polling.startup_jitter_s` (global default) or
  `startup_jitter_s` under a device's `connection:`. Each poll group waits a
  random delay in `[0, min(interval, jitter)]` before its **first** read, so
  several devices/groups don't fire in lock-step and hammer a shared transport
  (notably an RTU-over-network serial bridge) at boot. `0` = off.
- **Illegal-register skip-list** — `illegal_registers` under a device's
  `connection:`: addresses the slave answers with *illegal data address*
  (exception 02). Merged batch reads never bridge across one, and a selected
  register sitting on one is skipped — the fix for a hole *inside* a
  contiguous run, which `max_gap: 0` can't solve. Decimal or `0x…`.
- **All-zero frame gate** — `drop_all_zero: true` on a device: a sleepy device
  (an inverter at night) can answer with an ALL-ZERO frame instead of an
  error; published as-is that reads as real 0 V / 0 W. With the gate on, a
  poll group whose numeric values are all exactly zero (and there are ≥2 of
  them) is dropped and the cache keeps the last-good values until the device
  wakes.

```yaml
polling:
  startup_jitter_s: 2          # global default for all devices
devices:
  - id: inverter
    connection:
      host: 192.168.1.60
      startup_jitter_s: 5      # per-device override
      illegal_registers: [0x2100, 505]
      drop_all_zero: true      # sleeps at night
```

- **Per-register decode options** (register/template editor or template JSON):

| Key | Effect |
|---|---|
| `offset` | engineering value = `raw / scale + offset` — zero-point / unit shifts (e.g. Kelvin×10 → °C with `scale: 10, offset: -273.15`); skipped for enum/bitfield rows |
| `nan` | not-available sentinel: `true` = the data type's SunSpec value (`0x8000`/`0xFFFF`…), or an explicit raw value / list. A match reads as *missing*, never a garbage number (−32768 °C). Float NaN/Inf is always dropped |
| `monotonic: true` | cumulative counters (Wh/kWh/varh): an implausible step in **either direction** is dropped (the cache holds last-good) — a downward glitch, but also an upward jump >50% above the baseline (a corrupted high word would otherwise inject phantom GWh). A genuine sustained reset is adopted only after coherent confirming reads (within 5% of each other) and logs a WARNING with the new baseline |
| `enum` / `bits` (+ `mask`/`shift`) | decode a raw status word to text — `enum: {7: "Fault"}` (unmapped → `unknown (n)`), `bits: {0: "overvoltage"}` joins set-bit names; built visually via the **States** button in the template editor. Bundled maps use it for identity registers too (e.g. the EM24 detection code 1651, the Fronius 65A model id 731) |

These options apply **identically on every transport** — Modbus, HTTP/JSON
and MQTT-in sources all run the same wire→value pipeline (sentinel →
enum/bits → scale+offset → monotonic), so an energy counter fed over MQTT
(Shelly, Zigbee2MQTT…) gets the same rollover protection as a Modbus one.
When a value is dropped, the reason is one of three visible stages:
`sentinel` (declared not-available), `decode_failed` (unmappable status,
edge-logged once) or `filter_drop` (monotonic rejection, debug-logged).

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
- **Heartbeat** — `mqtt.heartbeat_interval` (seconds, `0` = off, default): in
  `changed` mode, force a republish of an *unchanged* value after N seconds,
  so a steady reading keeps a fresh timestamp and Home Assistant doesn't grey
  the entity out during long steady states.
- **Availability**: a Last-Will marks `<prefix>/status` = `offline` if the
  gateway dies; `online` is retained on connect. **Every device publishes a
  retained availability topic, the primary included**, and the availability
  cache is cleared on reconnect so it is always re-asserted — a consumer
  never has to guess from silence. It is confirmed only after a successful
  publish, so a broker hiccup can't leave a stale `online` behind.
- **Retained commands are never obeyed** — a retained MQTT message sitting
  on a *command* topic (a stray `mosquitto_pub -r`, an HA `retain: true`)
  would otherwise re-actuate hardware on **every** reconnect. Retained
  deliveries on command topics are dropped and the retained copy is cleared
  at subscribe.
- **Home Assistant discovery**: on by default. Each device becomes an HA
  device with its selected registers as sensors (`unique_id`
  `mbg_dev_<device>_<addr>_<name>` for non-primary devices), with sensible
  device/state classes from the units, plus a per-device **connectivity
  `binary_sensor`** (availability). Registers the template marks writable
  become HA **`number`/`select` entities** (bounds from the template,
  double-gated by `mqtt.allow_write_entities` + `security.allow_writes` —
  see §14); the write-aware discovery is registered at boot, so control
  survives a restart. Virtual meters publish their own diagnostic entities
  (serving state, request rate, errors, freshness…). Deleting a device
  clears its retained discovery so HA drops the entities.
- **Topic-prefix migrations** — `mqtt.compat_aliases` dual-publishes old
  topic names alongside the new ones so consumers can be flipped without a
  gap (see [config-reference.md](config-reference.md)); remove the aliases
  once every consumer has moved.

**Pitfall:** with `retain: true` (default) a consumer that subscribes late
still sees the last value — but after a broker restart without persistence,
values reappear only as they next publish; the gateway clears its change
cache on every reconnect and republishes the full state for exactly this
reason.

---

## 9. InfluxDB & Grafana

Config → **InfluxDB**: URL, token, org, bucket. Per-device buckets are
auto-created with 90-day retention; per-register measurement/tags are set in
the Registers tab. Point Grafana at the same bucket. Both ship in the
default compose stack — InfluxDB self-configures on first boot and Grafana
listens on `:3000` (see §2); this page is only about wiring the gateway to
them (or to your own instances).

**Data guarantees.** Every point is stamped with the *read* time, not the
flush time. If InfluxDB becomes unreachable, points go to a
**store-and-forward buffer** (default **120 minutes / 200,000 points**,
~40 MB bounded — tune `influxdb.buffer_minutes` / `buffer_max_points`) and
are replayed with their original timestamps on reconnect, idempotently. The
window is **data-relative** (measured from the newest buffered point, not
the wall clock), so a delayed restart never throws a valid snapshot away.
With `influxdb.buffer_persist: true` (default) the buffer also survives a
restart during the outage (`config/influx_buffer.jsonl`). Batches the client
gives up on after its ~5 min of internal retries are recovered into the same
buffer. **Authentication failures are detected explicitly**: a 401/403/404
(rotated token, deleted bucket) sets an `influx_auth_failed` flag, raises an
operator alert and re-buffers instead of dropping — only genuinely
malformed points (400/422) are ever discarded, and `writes_confirmed` /
`last_confirm_age_s` prove data is actually landing (the old failure mode
was `connected: true` with 100 % loss). Outages longer than the window drop
the oldest points; for the Janitza's voltages the meter's onboard recording
can backfill those via `python -m multibus.backfill` — the backfill derives
its point schema (tags, fields, measurement) **from the live register
selection**, so repaired points land in byte-identical series, and a
deselected address is skipped rather than written with a guessed schema.
Watch `buffer_points` / `replayed_total` / `dropped_total` in `/api/status`
or `gateway_influx_buffer_points` in `/metrics`.

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

Two per-row refinements:

- **A row whose source never resolved** (renamed register, deselect, a
  template typo) fails the freshness verdict outright and raises an
  `unresolved` event — distinct from *stale*. Before this rule the
  zero-seeded block would have served a plausible 0 W while the meter
  looked healthy; now the meter withholds loudly until the source resolves
  once.
- **Per-row freshness bound** — a register may carry its own
  `stale_after_s`, overriding the instance bound, so a 60-second BLE sensor
  can share a meter with 250-ms grid rows without false staleness; the
  effective bound also derives automatically from the producing poll
  group's cadence.

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

**11.7 — Redundant sources (failover).** A meter that feeds a control loop
shouldn't stop with one source outage. Two layers, driving the same engine:

- **Per register** — in the template editor the source-kind dropdown offers
  **Failover (live…)**: a comma-separated candidate list in priority order
  (`device.register` for a cross-device source). YAML:
  `source: { failover: ["_G_P_SUM3", "fronius.power_active_total"] }`
  (`combined` is an accepted alias). Each rebuild serves the **first fresh**
  candidate, skips missing/stale ones in order, and switches back the instant
  the primary is fresh again.
- **Per instance** — the Add/Edit instance modal's *Secondary source device
  (failover)* dropdown (`device_fallback:` on the instance) names a **twin
  device**. At start, every bare-name live register is rewritten into the
  pair `[name, <twin>.name]` — no template edits, because canonical field
  names are identical across devices. Const, sum, already-failover and
  explicit `device.register` rows stay as authored. **Cumulative energy
  counters are deliberately excluded**: a twin is a different physical
  meter, so its lifetime total would be a non-monotonic jump that corrupts
  downstream kWh statistics (a Fronius DataManager treats a backward-moving
  counter as a fault). Counter rows stay on the primary and, on an outage,
  **freeze at their last good value** in every policy — a frozen counter is
  a true statement ("energy delivered so far") — while the instantaneous
  rows fail over; when the primary returns, the counter resumes with a
  legitimate forward jump, like after any meter power-cycle. The fallback is validated
  at save (known device, different from the source) and re-checked at start
  (a bad value is ignored with a warning — it never blocks the meter). A
  configured-but-offline twin arms and engages the moment it publishes.

**Staleness interaction:** failover only ever serves a *fresh* candidate; if
none is fresh, the row degrades through the instance's `on_stale` policy
exactly like a single stale source (under `fail` the read is refused).
**Switch events:** every change of serving source lands in the event log and
the process log — `warn` on a drop to a lower-priority source, `info` on
recovery — and the meter card shows `✓ on primary` / `⇢ n/m on fallback`.

**Published state.** Each meter's retained MQTT state
(`<mqtt prefix>/vmeter/<id>/state`) carries the staleness policy and bounds
(`on_stale`, `stale_after_s`, `max_hold_s`), the last rebuild's quality
counts (fresh/stale/missing) and the live failover routing (per register:
candidates, active source, `on_primary`) — an external monitor sees *how* the
meter degrades and which source feeds it, not just that it went stale.

**Pitfalls:** the meter responds on any unit id (the configured one is
informational); reads outside the emulated map answer *illegal data
address* by design (consumers fingerprint meters by probing low addresses);
deleting a source device is blocked while a meter uses it.

---

## 11b. Device Builder — remote ESP32 nodes (ESPHome)

Got a meter on RS485 in another building or at another site, with no network
cable to it? The **Builder** section builds firmware for an ESP32/ESP8266
node that reads the meter over Modbus RTU and publishes the values via MQTT
back into the gateway — all from the UI, no toolchain installed.

Compilation is done by a standard **ESPHome** container, on your own
hardware; the gateway drives it through its API, so it needs no shared
volumes or new dependencies. Nothing leaves your network.

**Getting started: zero configuration.** `docker compose up -d` also starts
the bundled ESPHome service, and the gateway binds to it by itself
(`ESPHOME_URL` is pre-filled in the compose file). The **Device Builder**
card awaits you on the **Devices** page, below the device list, with the
green banner and the ESPHome version already showing; the **Deploy new
device** button in the toolbar takes you straight into the wizard. The
ESPHome dashboard is not exposed on the LAN — everything goes through a
single UI, with a single login and a single audit trail. (Already running
ESPHome elsewhere? Change the URL under Devices → Device Builder → ⚙, or set
`ESPHOME_URL` in `.env`. Want a login on the ESPHome dashboard too?
`ESPHOME_DASHBOARD_USERNAME/PASSWORD` in `.env` set both ends at once.)

**The full flow, from template to live data:**

1. **Generate from template** — pick a Modbus template (e.g. Eastron
   SDM630), the register subset, the hardware profile (board + UART/DE-RE
   pins) and the meter's Modbus address. The MQTT broker is inherited from
   the gateway automatically.
2. **Preview** — you get the complete YAML (uart/modbus/modbus_controller
   with the correct data types, byte order and scaling; values leave in
   engineering units on explicit per-register topics).
3. **Save + Adopt as device** — one click saves the firmware to the ESPHome
   dashboard, fills the missing keys in `secrets.yaml` (`CHANGE_ME`
   placeholder for Wi-Fi/OTA) **and automatically creates the gateway-side
   pair**: an MQTT template + an mqtt-in device with exactly the same
   topics. Zero double configuration.
4. Fill in `secrets.yaml` (the edit button accepts secrets.yaml too), then
   **Build** — live logs in the console.
5. **First flash: over USB, straight from the browser** (the *USB* button;
   needs Chrome/Edge and HTTPS or localhost — esp-web-tools is served
   locally, no cloud). The same dialog configures Wi-Fi over the cable
   (Improv). Afterwards: **Flash OTA** from the same page.
6. The node boots, publishes, and the paired gateway device picks the values
   up automatically — you see them in Dashboard/Monitor, LWT staleness
   included.

**What the generator covers today (and what it doesn't, yet):**

| Node interface | State | Notes |
|---|---|---|
| **RS485 / Modbus RTU** | ✅ complete | uart + modbus + modbus_controller from any Modbus template; validated by ESPHome ("Configuration is valid!") |
| **MQTT northbound** | ✅ complete | explicit per-register topics + LWT; the mqtt-in pair is created at Adopt |
| **BLE (sensors)** | ⚠️ partial | the gateway consumes BLE via MQTT (the `ble_theengs_sensor` template); the generator doesn't emit `esp32_ble_tracker` profiles yet |
| **CAN bus** | ⏳ planned | ESPHome has `canbus` (ESP32 internal TWAI / MCP2515), but CAN is frame+signal oriented, not register oriented — it needs a template schema extension (frame id, bits, scaling), in design |

A manually imported YAML can use ANY ESPHome component (including
canbus/BLE) today already — the limits above concern only the automatic
**generator** from templates. Extending the generator (BLE, CAN both ways,
I/O nodes, integrated boards like the LilyGO T-CAN485) is on the project
roadmap.

**Worth remembering:**

- Without auth enabled, everything is open (trusted LAN, like the rest of
  the app); with auth, node YAMLs, builds and flashing are **admin-only**,
  and everything lands in the audit log.
- **Update all** recompiles and OTA-updates every node with old firmware
  (after an ESPHome upgrade, for instance).
- Hardware profiles (pins/board) are saved and reused across nodes; two
  generic profiles are included.
- Deleting a node **archives** it on the ESPHome dashboard — nothing is
  permanently lost.

## 12. Alerts & webhooks

Two alert families over one delivery path (MQTT + webhook):

- **Infrastructure health** — a device or sink goes down, read latency stays
  high, the InfluxDB buffer backs up.
- **Value thresholds** — the per-register warning/danger limits from §6
  become alert events through a built-in hysteresis engine (**off by
  default**, `signals.threshold`): five bands, fires only on band
  transitions, *fast to alarm, slow to clear* (deadband
  `threshold_deadband_pct`, default 2 %), suppressed on stale data so a comms
  loss can't fire a phantom crossing.

Configure under Config → Alerts or the `alerts:` block:

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
  signals: { device: true, sink: true, latency: true, buffer: true,
             threshold: true }                 # threshold defaults to false
  threshold_deadband_pct: 2.0
  threshold_alert_on_start: true
```

Alerts are rate-limited per key (`min_interval_s`), mirrored to the event
log and the Status page. The gateway *detects and delivers*; deduplication,
routing and channel fan-out (Telegram/SMS/e-mail) belong in your webhook
receiver / notification system. The **Test** button (or
`POST /api/alerts/test`) fires a synthetic alert through the real channels —
it requires login or an API key and is cooldown-throttled, because it drives
real outbound traffic. Webhook delivery is best-effort (no retry) and
refuses redirects. Payload shape and the threshold engine in detail:
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
  Surfaced per device on Status and `/metrics`. Retry policy is owned by
  the gateway (`retry_attempts`/`retry_delay`), never doubled by the Modbus
  library, and a short or empty response counts as a failure, not a
  success. *Reachability* is a **link verdict**, not a per-batch one: a
  device is declared unreachable only after the consecutive-failure
  backstop trips (which also force-reopens a wedged-but-open link), so one
  chronically bad register batch can't flap `unreachable/recovered` events
  while the link is fine — per-batch loss stays visible as
  `batch_failures` + per-group `stale_groups` in the health surface.
- **Query now** returns both the **raw** wire value and, when the address
  is a selected register, an additive **`corrected`** field — the exact
  value the poll pipeline would publish (scale/offset/enum applied,
  stateless: a debug read never advances the monotonic filter). If the two
  differ unexpectedly, the register's scale/decode declaration is where to
  look.

---

## 14. Modbus writes & dead-man leases

Writing to field hardware is guarded in depth. All of it is **off by
default**.

1. Enable `security.allow_writes: true` (Config → Security).
2. Writes must be **authenticated** — enable login or set `API_KEY`
   (anonymous writes are refused even with the gate open).
3. The register must be declared **writable in the device template**, with
   optional `write_min` / `write_max` bounds; the encoding (data type,
   scale, and `offset` — inverted on write, `raw = (value − offset) ×
   scale`, including on the safety revert) always comes from the template
   row, never from the caller.
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
- **Config self-healing (`config.yaml.good`)** — independently of snapshots,
  every good load keeps a `config.yaml.good` copy; a later corrupt edit falls
  back to that **last-known-good** file (never bare defaults), so the primary
  keeps polling the right host through a bad edit. The condition surfaces as
  `config.healthy` in `/api/status` and is raised as an alert; saves stay
  disabled until the file is repaired. The heal also catches the sneaky
  case of a file that still *parses* but is a truncated husk (empty file,
  bare scalar, cut before the sections every save writes): a plausibility
  gate routes it through the same `.bad` + heal path, and a snapshot that
  would *lose devices* is never promoted over the existing `.good` (an
  intentional device removal refreshes `.good` via the save itself). The
  register selections get the same contract: `selected_registers.json` —
  the primary's **and every device's** — keeps its own `.good`/`.bad`
  pair, so a truncated selection heals instead of silently emptying every
  poller.
- **Backup export/import (ZIP)** — for portability between hosts. The
  export **strips secrets** (MQTT/Influx credentials, password hashes,
  webhook/REST-push headers) and host identity by default;
  `include_secrets=true` requires the admin role or the API key and is
  audit-logged. Import (raw ZIP body, ≤25 MB) **merges** over the live
  config so stripped secrets survive, and takes a `pre-import` snapshot
  first. Snapshots, by contrast, are full-fidelity local restore points —
  downloading one is gated like a with-secrets export.

**What goes into a backup/snapshot:** `config.yaml`, each device's selected
registers, the user's device templates, `virtual_meters.yaml` + the
virtual-meter templates under `config/templates/`, calculated-register
presets and the Builder's hardware profiles. The passkey registry
(`passkeys.json`) goes **only** into the with-secrets backup
(`include_secrets=true`) and into snapshots — otherwise a restore would
unlock authentication.

**What does NOT go in (and how to save it separately):**
- `audit.jsonl` / `events.jsonl` — operational history, self-rotating; copy
  them manually if you need them for analysis.
- The Device Builder's ESP32 node YAMLs — they live in the **ESPHome volume**
  (`esphome-config`), not in the gateway's `config/`. Include it in your
  infrastructure backup (Duplicati etc.) if you use the Builder.
- `write_leases.json` — ephemeral by design (leases rebuild themselves).

**Pitfall:** snapshots live under `config/snapshots/` inside the config
volume — they protect against bad edits, not against losing the volume.
Keep an exported ZIP somewhere else too.

**Disaster recovery (lost host / lost volume), start to finish:**

1. On the new host: `git clone … && cp .env.example .env && docker compose
   up -d` — a fresh gateway boots with first-run credentials (printed once
   in the log).
2. Log in, go to Config → **Backup & Snapshots** → **Import**, upload your
   exported ZIP (a with-secrets export restores passkeys and device
   credentials too; a sanitized one asks you to re-enter secrets).
3. The import applies config, devices, register selections, virtual meters
   and calculated presets in one shot. With **more than one device**, the
   response may set `restart_required` — restart the container
   (`docker compose restart multi-bus-gateway`) so every poller starts from
   the restored definitions.
4. Re-check: `/health` shows the meters, Status shows every device polling,
   and your ESS/consumers reconnect to the virtual-meter ports on their own.
5. If you use the Device Builder, restore the `esphome-config` volume from
   your infrastructure backup (step above) — node YAMLs are not part of the
   gateway ZIP.

---

## 16. Security

**Login is ON from the first run** — a fresh install generates an admin
password (printed once to the log) and enables authentication. The remaining
layers (IP allowlist, API key, TLS, canonical URL) are opt-in — turn them on
from **Config → Security** as exposure grows. Defense in depth: each layer
applies independently.

> **The bundled stack has its own doors.** The default compose also exposes
> the broker (`1883`/`9001`, anonymous by default — add credentials via the
> two-line recipe in `mosquitto/config/mosquitto.conf`), MQTT Explorer
> (`4000`, unauthenticated viewer), InfluxDB (`8086`) and Grafana (`3000`,
> login `admin` / `GF_SECURITY_ADMIN_PASSWORD` from `.env`). On anything
> beyond a trusted LAN, set broker credentials, change the change-me
> passwords, and drop the port mappings you don't need.

### 16.1 Login & roles

One **admin** account, plus optional **operator** and **viewer** accounts:

| Role | Can | Cannot |
|---|---|---|
| `viewer` | see everything (GET), run the on-demand register queries | change anything |
| `operator` | live actions: diagnostics, bus trace, discovery, device tests, payload samples, **Modbus writes** (within template bounds), alert test, register reload, own passkeys | anything that lands in a config file (devices, registers, templates, vmeters, settings, snapshots), audit trail |
| `admin` | everything | — |

Passwords are hashed (PBKDF2-SHA256, 600k iterations); leave a password
field blank on save to keep the current one. **Enabling login with a blank
or default `admin` password is refused at every entry point** — the UI
route, a hand-edited `config.yaml`, a config import and a snapshot restore
all hit the same guard, so no path yields a gateway that *looks* locked
but accepts `admin/admin`. Failed logins are locked
out per IP (`lockout_threshold` / `lockout_minutes`, defaults 5 / 5 min).
Sessions are HttpOnly cookies, 7-day sliding, persisted as SHA-256 token
hashes in `config/sessions.json` — a container restart keeps you logged in.
Rotating any password revokes **every** session (an old cookie can't outlive
the rotation) — except the author's: the security save re-issues your own
session, so changing passwords never logs *you* out mid-task. The audit
trail is admin-only.

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
  (**restart to apply**; browsers will warn on self-signed). Generated
  private keys are created mode `0600`.
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
Read-only GETs and the on-demand query POSTs stay open. The key also gates
the OTA-capable **Device Builder WebSocket** (`/api/builder/stream`):
scripts send the `X-API-Key` header; browsers — which cannot set custom WS
headers — send the `mbg-api-key.<base64url(key)>` subprotocol (the UI does
this automatically; a query parameter is deliberately not accepted, it
would leak the key into access logs).

### 16.5b Browser hardening

State-changing requests from a **different site** are rejected outright
(`Sec-Fetch-Site` / `Origin` checks — a drive-by page in the operator's
browser cannot fire configuration changes), on `/ws` too. Standard security
headers are applied, including on the login shell, and the canonical-URL
value is output-escaped against stored-XSS. Secrets never round-trip to the
browser: exports, env listings and logs redact tokens and password hashes.

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
| Locked out (login) | wait `lockout_minutes`, or restart the container (the lockout counter is in-memory; sessions survive the restart) |
| Locked out (IP allowlist) | edit `security.allowlist` in `config/config.yaml`, restart |
| Passkey enrollment fails | you're on an IP URL or plain HTTP — use `localhost` or a hostname over HTTPS |
| Config broke after an edit | boot restores last-known-good automatically; the broken file is kept as `config.yaml.bad`; or roll back a snapshot from Config → Backup |
| Writes return 403 | `security.allow_writes` off, no credential (login/API key), register not declared writable, or value outside `write_min`/`write_max` |

Still stuck? Open an issue — include `docker compose logs` and your
(redacted) config.
