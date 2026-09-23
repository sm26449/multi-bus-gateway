# Configuration reference

Every knob the gateway reads — environment variables, every `config.yaml` key the
parser actually understands, and the per-register options — in one place. Normal
operation needs none of this by hand: the UI (Config → Settings) edits and
persists `config.yaml` for you. This reference exists for pre-seeding deployments,
reviewing a config in git, and understanding exactly what a key does.

A ready-to-copy annotated starting point ships as
[`config/config.example.yaml`](../config/config.example.yaml) — its keys and
defaults track this document.

## Configuration sources and precedence

1. **`config/config.yaml`** — the persistent configuration, written by the UI
   (atomic write, file mode `0600` — it holds credentials).
2. **Environment variables** — a small set of overrides applied **on every
   start, after** `config.yaml` is loaded. An env value therefore wins over the
   file/UI on each boot. Secrets set via env (`MQTT_PASSWORD`, `INFLUXDB_TOKEN`,
   `ESPHOME_PASSWORD`) are *shadowed*: a UI save writes the config-file value
   back to disk, never the env secret.
3. **`config/selected_registers.json`** (primary device) and
   **`config/devices/<id>/selected_registers.json`** (every other device) — the
   per-device register selection, poll groups, energy fields and calculated
   registers.

Unknown keys in `config.yaml` are ignored at load time (the parser reads only
the keys listed below, with defaults for anything missing) — but see the
[upgrade guide](upgrade-guide.md) for what a *save* does to unknown keys.

If `config.yaml` fails to parse at boot, the gateway restores the last-known-good
snapshot instead of running on defaults, keeps the broken file as
`config.yaml.bad`, and disables saves until it is repaired (visible in
`/api/status` → `config_status`).

## Environment variables

### Config overrides (re-applied every start)

These map 1:1 onto `config.yaml` keys and override them at each boot.

| Variable | Overrides | Notes |
|----------|-----------|-------|
| `MODBUS_HOST` | `modbus.host` | primary device host |
| `MODBUS_PORT` | `modbus.port` | |
| `MODBUS_UNIT_ID` | `modbus.unit_id` | |
| `MODBUS_STALE_AFTER_S` | `modbus.stale_after_s` | |
| `MQTT_ENABLED` | `mqtt.enabled` | `true`/`false` |
| `MQTT_BROKER` | `mqtt.broker` | |
| `MQTT_PORT` | `mqtt.port` | |
| `MQTT_USERNAME` | `mqtt.username` | |
| `MQTT_PASSWORD` | `mqtt.password` | secret — never written back to `config.yaml` |
| `MQTT_PREFIX` | `mqtt.topic_prefix` | |
| `MQTT_PUBLISH_MODE` | `mqtt.publish_mode` | `changed` or `all` |
| `INFLUXDB_ENABLED` | `influxdb.enabled` | `true`/`false` |
| `INFLUXDB_URL` | `influxdb.url` | |
| `INFLUXDB_TOKEN` | `influxdb.token` | secret — never written back to `config.yaml` |
| `INFLUXDB_ORG` | `influxdb.org` | |
| `INFLUXDB_BUCKET` | `influxdb.bucket` | |
| `INFLUXDB_PUBLISH_MODE` | `influxdb.publish_mode` | `changed` or `all` |
| `UI_PORT` | `ui.port` | |
| `UI_HOST` | `ui.host` | container image sets `0.0.0.0`; bare-metal default is loopback |

Active overrides are reported by `GET /api/config` (`env_overrides`), with
secret values masked.

### Device Builder (ESPHome) — seed + override

| Variable | Behaviour |
|----------|-----------|
| `ESPHOME_URL` | Sets `esphome.url`. On a **fresh deploy** (no `esphome:` block in `config.yaml` yet) its presence also **enables** the Builder outright — zero-config with the bundled `esphome` compose service. Once a block exists (any UI save), the saved enabled/disabled choice wins. |
| `ESPHOME_USERNAME` | Sets `esphome.username`. |
| `ESPHOME_PASSWORD` | Sets `esphome.password` (secret — shadowed on save). |
| `ESPHOME_ENABLED` | `true`/`false` — explicit override of the saved choice, every start. |

### Process-only variables (never stored in config.yaml)

| Variable | Default | Purpose |
|----------|---------|---------|
| `API_KEY` (alias `JANITZA_API_KEY`) | *(unset = open)* | Opt-in write protection: when set, every state-changing request (POST/PUT/PATCH/DELETE) must carry a matching `X-API-Key` header. GET telemetry and the read-only query POSTs stay open. |
| `CORS_ALLOW_ORIGINS` | *(unset = no CORS)* | Comma-separated origins allowed cross-origin API access (credentials off). The UI is same-origin and needs none. |
| `VMETER_PORT_START` | `1502` | First TCP port of the virtual-meter range (the UI offers/validates instance ports from it). |
| `VMETER_PORT_END` | `1512` | Last port of the range. Widen it (and the compose port mapping) for more meters. |
| `INFLUX_BUFFER_PATH` | `config/influx_buffer.jsonl` | On-disk location of the InfluxDB store-and-forward buffer (used only when `influxdb.buffer_persist` is true). |
| `SERIAL_BRIDGE_URL` | `http://mbg-serial-bridge:7000` | Base URL of the optional ser2net serial-bridge companion, used by the commissioning UI to list remote serial adapters. |
| `TZ` | `Europe/Bucharest` (compose) | Standard container timezone. Calendar reports (monthly energy) use `ui.timezone`, not `TZ`. |

### Compose-stack variables (infrastructure, not the gateway)

Consumed by `docker-compose.yml` for the bundled services — set them in `.env`:

| Variable | Default | Purpose |
|----------|---------|---------|
| `MQTT_BROKER_PORT` / `MQTT_WS_PORT` | `1883` / `9001` | host-port mapping of the bundled broker (distinct from the gateway's `MQTT_PORT` override, which targets an EXTERNAL broker) |
| `MQTT_EXPLORER_PORT` | `4000` | MQTT Explorer web UI |
| `DOCKER_INFLUXDB_INIT_USERNAME` / `_PASSWORD` / `_ORG` / `_BUCKET` / `_ADMIN_TOKEN` | `admin` / change-me / `multibus` / `multibus` / change-me | InfluxDB first-boot self-setup; paste the token into Config → InfluxDB |
| `GF_SECURITY_ADMIN_PASSWORD` | change-me | Grafana `admin` login |
| `PV_STACK_NETWORK` | `pv-stack-network` | name of the docker network (created by the base file; joined as external by the overlay) |
| `BRIDGE_EXCLUDE` | *(empty)* | serial adapters the bridge must never expose (`rtu-bridge` profile) |
| `SERIAL_BRIDGE_URL` | `http://mbg-serial-bridge:7000` | where the gateway reaches the bridge's control API |

### Backfill utility (`multibus/backfill.py`)

A standalone gap-backfill tool for InfluxDB; it reads its own environment:

| Variable | Default | Purpose |
|----------|---------|---------|
| `JANITZA_METER_URL` | `http://$MODBUS_HOST` | Gateway/meter HTTP endpoint to read from (falls back to `MODBUS_HOST`, default `192.168.1.100`). |
| `INFLUXDB_URL` | `http://influxdb:8086` | Target InfluxDB. |
| `INFLUXDB_ORG` | `janitza` | |
| `INFLUXDB_BUCKET` | `janitza` | |
| `INFLUXDB_TOKEN` | *(empty)* | |
| `JANITZA_MIN_GAP_SEC` | `180` | Minimum gap (seconds) worth backfilling. |
| `JANITZA_MAX_LOOKBACK_H` | `48` | How far back to scan. |
| `JANITZA_REGISTERS_PATH` | `config/selected_registers.json` | The live selection the point SCHEMA is derived from (tags, fields, measurement, poll group) via the publisher's own `build_point` — backfilled points land in byte-identical series. An address missing from the selection is skipped, never written with a guessed schema. |

## config.yaml

| top-level key | default | meaning |
|---|---|---|
| `config_version` | written automatically | version of the gateway that last saved the file (kept as the first key). A downgrade loading a newer-stamped file warns, raises a `config-downgrade` alert and flags `/api/status` → `config_status.written_by_newer`: saving from the older version drops settings the newer one introduced. See [upgrade-guide.md](upgrade-guide.md). |

### `modbus:` — the primary device connection

The primary device is always Modbus TCP; its connection lives in this flat
section. (Serial/RTU and other transports are available on additional
`devices:` entries, below.)

| Key | Default | Notes |
|-----|---------|-------|
| `host` | `192.168.1.100` | |
| `port` | `502` | |
| `unit_id` | `1` | |
| `timeout` | `3` | seconds |
| `retry_attempts` | `3` | |
| `retry_delay` | `1.0` | seconds |
| `stale_after_s` | `30` | no successful read within this many seconds ⇒ data marked stale in `/health` + `/api/status` (does **not** fail the container probe) |
| `max_gap` | `10` | max address gap (registers) the poller bridges when merging two registers into one batch read; set `0` for strict slaves that answer merged blocks with ILLEGAL DATA ADDRESS |
| `startup_jitter_s` | `0.0` (off) | each poll group waits a random delay in `[0, min(interval, startup_jitter_s)]` before its *first* read, so groups don't hammer a shared transport in lock-step at boot. (Legacy location `polling.startup_jitter_s` is still read as a fallback.) |
| `illegal_registers` | `[]` | addresses this slave answers with ILLEGAL DATA ADDRESS; a merged read is never bridged across one, and a selected register sitting on one is skipped. Decimal or `0x` hex; bad entries are dropped, not fatal. |
| `drop_all_zero` | `false` (opt-in) | data-readiness gate for sleepy devices: a poll group whose numeric values are ALL exactly zero (≥2 of them) is dropped and the cache keeps last-good values |
| `serialize_endpoint` | `true` | queue this device's Modbus transactions behind every other device that shares the same `host:port`. Cheap gateways (a Fronius DataManager, most RS-485-over-TCP bridges) serialize internally and serve a handful of clients: several devices polling one of them at once go **slower**, not faster. Costs nothing when a device has the endpoint to itself; set `false` to restore free-for-all access |
| `endpoint_min_gap_s` | `0.0` | breathing room between consecutive transactions on this access point. A master device is a small computer with its own job — a Fronius DataManager has to poll its RS-485 side while it answers us — and hit back-to-back it starves that side. The symptom is not a polite slowdown but collapse: measured on a production one, demanding 32 transactions a minute returned 21.6 with 6.5 % errors, while demanding 26 returned 26. Our own long-lived Fronius collector never had that problem because it waits a full second after every device and 200 ms between register blocks, leaving the datalogger idle about a fifth of the time **on purpose**. The gap belongs to the access point, so when several endpoints share one the most cautious declaration wins. Costs exactly `gap × transactions` of wire time, so budget it: 0.3 s at 26 tx/min is 7.8 s a minute. `0` keeps the old back-to-back behaviour |
| `endpoint_wait_s` | `10.0` | how long a read may wait for its turn on a shared endpoint before skipping the cycle. A missed turn is **not** a device failure: nothing is counted against the link, and a single `bus_busy` event per episode says the gateway is the bottleneck |
| `share_transport` | `true` | use ONE socket for every device behind the same access point, the way a master device is meant to be talked to. A socket per unit never made units independent — they still queue inside the master — and a master that serves a handful of clients simply runs out of them. Per-unit counters, latency, error taxonomy, reachability and health are unaffected: units share a wire, never an identity. A directly-attached serial line (`protocol: rtu`) never shares. Set `false` for a socket per unit |
| `max_connections` | `1` | how many sockets that access point is worth, with the units shared out between them (sticky by unit id, so a unit always rides the same one). Transactions overlap **across** connections and queue **within** each. One is the honest default: a master that serializes internally — a Fronius DataManager, measured at ~0.4 s per read alone and ~3 s with five callers racing — gains nothing from a second and loses a client slot. A master with an engine per line (an RS-485 bridge with several, a PLC front end) cuts its sweep almost proportionally. Which one yours is, is a **measurement**: `scripts/calibrate_endpoint.py` makes it. Range 1..8 |

**Poll intervals are a cadence, not a pause.** A poll group waits
`interval − (time the sweep took)`, so a 5 s interval means a reading every
5 s rather than every 5 s + however long the bus needed. A group that cannot
keep up still gets a breather (10 % of its interval), counts `overruns`, and
says so once per episode. `/api/status` reports each group's `cycle_s` (what
one sweep actually costs) and `reads` (how many batch reads it takes) next to
its interval — the two numbers an interval is chosen from.

### `mqtt:`

| Key | Default | Notes |
|-----|---------|-------|
| `enabled` | `true` | |
| `broker` | `mosquitto` | the bundled compose broker; point it at your own from the UI |
| `port` | `1883` | |
| `username` / `password` | `""` | |
| `topic_prefix` | `multibus/umg512` | primary device's prefix |
| `retain` | `true` | |
| `qos` | `0` | |
| `publish_mode` | `changed` | `changed` or `all` |
| `heartbeat_interval` | `0` (off) | in `changed` mode, force a republish of an *unchanged* value after this many seconds so steady readings keep a fresh timestamp (HA doesn't grey the entity out) |
| `ha_discovery.enabled` | `true` | Home Assistant MQTT discovery |
| `ha_discovery.prefix` | `homeassistant` | |
| `ha_discovery.device_name` | `Janitza UMG 512-PRO` | primary device's HA name |
| `allow_write_entities` | `false` (opt-in) | expose writable registers as HA number/select entities and subscribe to their command topics. **Double-gated**: a command executes only when this AND `security.allow_writes` are true, the register is declared writable, and the value is within its envelope. Refused while `mqtt.username` is empty: an anonymous broker session would turn any LAN publish into a hardware write (3.80.0). |
| `default_topic_pattern` | `mbg/devices/{device}` | topic-prefix pattern seeded onto **new** devices (`{device}` = device id). Namespace convention: device values under `mbg/devices/<id>/…`; `mbg/vmeter/<id>/…` is reserved for virtual-meter MQTT publishing. Pre-existing devices keep their persisted prefixes. |
| `compat_aliases` | `[]` | topic-migration dual-publish: every publish whose topic starts with `from` is *also* published under `to`, with `leaves` renaming individual tails — old consumers keep receiving byte-identical topics while they migrate. Example: `[{from: meters/umg512, to: janitza/umg512, leaves: {energy/active/import: energy/active/consumed}}]`. Remove the entry once no subscriber uses the old prefix. |
| `tls_enabled` | `false` (opt-in) | broker TLS (8883) |
| `tls_ca_cert` / `tls_client_cert` / `tls_client_key` | `""` | container-local paths; client pair adds mutual TLS |
| `tls_insecure` | `false` | skip hostname/cert checks (test only) |

### `influxdb:`

| Key | Default | Notes |
|-----|---------|-------|
| `enabled` | `false` (opt-in) | |
| `url` | `http://influxdb:8086` | |
| `token` | `""` | paste the token from your InfluxDB (the bundled stack prints/sets it via `DOCKER_INFLUXDB_INIT_ADMIN_TOKEN`) |
| `org` | `multibus` | matches the bundled first-boot org |
| `bucket` | `multibus` | primary device's bucket |
| `write_interval` | `5` | seconds |
| `publish_mode` | `changed` | `changed` or `all` |
| `default_bucket_pattern` | `{device}` | bucket pattern seeded onto **new** devices |
| `buffer_minutes` | `120` | store-and-forward: keep at most this much history while InfluxDB is down; replayed with original timestamps on reconnect |
| `buffer_max_points` | `200000` | hard cap (drop-oldest) |
| `buffer_persist` | `true` | persist the buffer to disk (see `INFLUX_BUFFER_PATH`) so it survives a restart during an outage |

### `ui:`

| Key | Default | Notes |
|-----|---------|-------|
| `host` | `127.0.0.1` | loopback on bare metal; the container image sets `UI_HOST=0.0.0.0` |
| `port` | `8080` | |
| `auth.enabled` | `false` | a FRESH install's first run writes `true` + a generated admin password (printed once to the log) |
| `auth.username` / `auth.password` | `admin` / `""` | password default is blank; enabling auth with a blank or `admin` password is refused |
| `auth.viewer_username` / `auth.viewer_password` | `""` | optional read-only account (GET only) |
| `auth.operator_username` / `auth.operator_password` | `""` | optional operator account: live actions (bounded writes, diagnostics, discovery) but no configuration changes |
| `auth.lockout_threshold` | `5` | failed logins per client IP before lockout |
| `auth.lockout_minutes` | `5` | lockout duration |
| `canonical_url` | `""` (off) | when set, browsers are steered to this canonical HTTPS URL (client-side; the local IP stays reachable via `?local`) |
| `tls.enabled` | `false` (opt-in) | HTTPS for the UI (uvicorn TLS) |
| `tls.cert` / `tls.key` | `""` | container-local paths |
| `trusted_proxies` | `[]` (trust nobody) | reverse-proxy IPs whose `X-Forwarded-*` headers are trusted. Required when TLS terminates in a proxy in front — otherwise lockouts, the IP allowlist and the audit trail see the proxy's IP |
| `timezone` | `Europe/Bucharest` | IANA timezone for calendar reports (monthly-energy month boundaries) |
| `default_colors` | `{}` | default widget colors (phase convention + per-category hues), applied to **new** widgets only |

### `security:`

| Key | Default | Notes |
|-----|---------|-------|
| `allowlist` | `[]` (open) | IPs/CIDRs allowed to reach the HTTP API/UI. Loopback and the docker gateway are always allowed. |
| `allow_nonlan_http_devices` | `false` (opt-in) | SSRF guard: HTTP/JSON device URLs must point at a private LAN host unless this is true |
| `allow_writes` | `false` (opt-in) | master **arming** switch for Modbus writes (FC5/6/15/16). When armed, any device that is not `write_locked` can be written — declared registers with their template encoding + whatever guards were declared; undeclared registers via the raw path (`unguarded: true` in the payload). Every write is authenticated, rate-limited and audited. |
| `primary_write_locked` | `true` | per-device write LOCK for the primary. Read-only used to be hardcoded; it is now this flag, defaulting to locked so the historical behavior survives upgrades. Unlock deliberately. Non-primary devices carry `write_locked` in their `devices[]`/`endpoints:` entry (default unlocked) — toggle from the device's Outputs tab. |
| `write_rate_limit_per_s` | `10.0` | per-client-IP write rate limit on the write API; excess gets 429; `0` disables |

### `polling:`

```yaml
polling:
  groups:
    realtime: {interval: 1,  description: Real-time values}
    normal:   {interval: 5,  description: Standard measurements}
    slow:     {interval: 60, description: Energy counters}
```

Groups above are the built-in defaults; per-device groups saved from the UI live
in the device's registers file and override these. Intervals may be sub-second
(floor: `0.05 s`).

### `http_output:` and `rest_push:` (primary device)

Both are opt-in output sinks for the **primary** device; other devices carry
the same blocks inside their `devices[]` entry.

```yaml
http_output:
  enabled: true          # serve live values as JSON at GET /api/meters/<id>

rest_push:
  enabled: true
  url: https://example.net/ingest
  interval_s: 30         # min 5
  headers: {X-API-Key: "…"}
  format: native         # native = {values: {name: {value,unit,ts}}}; flat = {values: {name: value}}
  verify_tls: true       # false allows self-signed targets
  timeout: 10
```

### `pq_recorder:` (primary device) — Janitza/Jasic PQ event recorder

Opt-in acquisition of the meter's **on-device power-quality event recorder**
(dips, swells, outages, rapid voltage changes) over the Jasic web firmware's
HTTP endpoints — Janitza UMG 604/605/508/511/512 templates. The device keeps
only a small event ring (32 entries on the UMG512); the gateway archives it
permanently: events → InfluxDB `pq_events`, lifetime counters →
`pq_counters`, and the half-wave-RMS traces of the channels implicated in
each new event → `pq_waveforms`. New events are also published to MQTT
(`<topic prefix>/pq/event`, retained) and the gateway event log. Browse them
in the device workspace → **Power Quality** tab. Other devices carry the
same block inside their `devices[]` entry.

```yaml
pq_recorder:
  enabled: true
  poll_s: 60               # recorder poll interval (floor 30 s; keep short —
                           # the meter serves only its newest capture window)
  archive_waveforms: true  # fetch + store RMS traces for new events
  base_url: ""             # default: http://<connection.host>
  bucket: ""               # optional dedicated InfluxDB bucket for PQ history
                           # (created with infinite retention if missing) —
                           # decouples forensically-precious PQ data from the
                           # telemetry bucket's retention. Default: the
                           # device's normal bucket.
```

See [pq-recorder.md](pq-recorder.md) for the endpoints, the data model and
the reason-bitmask semantics.

### `devices:` — additional southbound devices

Each entry describes one extra device (the primary comes from the flat sections
above). A malformed entry is skipped with a warning — it never blocks boot.

```yaml
devices:
  - id: sdm630_garage            # required, unique, [A-Za-z0-9][A-Za-z0-9_.-]*
    name: Garage SDM630
    template: eastron_sdm630     # device-template id
    enabled: true
    connection:
      protocol: tcp              # tcp | rtu | rtu-tcp | http | mqtt
      # tcp / rtu-tcp:
      host: 192.168.1.60
      port: 502
      unit_id: 1
      timeout: 3
      retry_attempts: 3
      retry_delay: 1.0
      stale_after_s: 30
      max_gap: 10
      startup_jitter_s: 0        # inherits modbus.startup_jitter_s when unset
      illegal_registers: []
      drop_all_zero: false
      # rtu (direct serial):
      # serial_port: /dev/ttyUSB0
      # baudrate: 9600
      # parity: N                # N | E | O
      # stopbits: 1
      # bytesize: 8
      # http (HTTP/JSON input): url, timeout, headers, verify_tls
      # mqtt (MQTT input): broker, port, username, password, tls, topic
    mqtt:
      topic_prefix: meters/${device_id}   # ${device_id} / ${id} substituted
      ha_discovery: true
      enabled: true              # route this device's values to MQTT
    influxdb:
      bucket: sdm630_garage      # default: the global bucket
      device_tag: sdm630_garage  # default: the device id
      enabled: true
    http_output: {enabled: false}
    rest_push: {}                # same shape as the primary's block
    pq_recorder: {}              # same shape as the primary's block (Janitza only)
```

`rtu-tcp` speaks RTU framing over a TCP socket (a ser2net-style serial bridge);
`rtu` opens a local serial port directly — see [rtu-serial.md](rtu-serial.md).

### `endpoints:` — N units of the same device behind one endpoint

An endpoint instantiates **one template** for **several unit IDs** on **one
endpoint** — the classic case is several inverters behind a single
datalogger/gateway (Fronius DataManager, Deye/Huawei loggers, an RS-485
multi-drop bridge). Each unit is materialized as an ordinary device (visible
in `/api/devices` and the UI) with its **own socket** — deliberate:
unit-switching on a shared socket corrupts some gateway buffers, and units
must fail independently. Materialized devices are managed **through the
endpoint**: device create/edit/delete refuses their ids.

```yaml
endpoints:
  - id: fronius                  # required; device ids default to <id>-u<unit>
    name: Fronius PV
    template: fronius_sunspec_inverter
    enabled: true                # false = units stay listed but do not poll
    connection:                  # shared by every unit (no unit_id here)
      protocol: tcp              # tcp | rtu-tcp
      host: 192.168.1.50
      port: 502
    units: [1, 2, 3, 4]          # bare ids, or {unit_id: 3, id: inv3, name: East roof}
    mqtt:
      topic_prefix: mbg/devices/${device_id}
      ha_discovery: false
    influxdb:
      bucket: fronius_shadow
      device_tag: inverter_${unit_id}
    aggregates: true             # false = no endpoint-level output (see below)
    write_locked: false          # applies to every unit (one endpoint, one lock)
    http_output: { enabled: false }        # applies to every unit
    rest_push:   { enabled: false, url: "" }   # applies to every unit
```

An endpoint is ONE endpoint, so the sinks that belong to the endpoint are declared
once: `write_locked`, `http_output` and `rest_push` sit on the endpoint and are
propagated to every materialized unit. They are edited from any unit's Outputs
tab (the switch says it applies endpoint-wide) or from the endpoint page.

Editing an endpoint re-materializes its units only when something they are BUILT
from changed (connection, template, unit membership, routing, the endpoint-level
flags). A settings-only edit — a rename, the `aggregates` toggle, a unit's
display name — keeps every poller running: a cosmetic save must not punch a
hole in acquisition.

`${unit_id}`, `${endpoint_id}` and `${device_id}` substitute per unit in the
topic prefix, bucket, device tag and name. Each unit's register selection is
seeded from the template at boot (`devices/<id>/selected_registers.json`) and
can then be tuned per unit like any device. API: `GET/POST /api/endpoints`,
`GET/PUT/DELETE /api/endpoints/{id}` (unit availability is aggregated in the
`GET` responses). Deleting an endpoint keeps the units' register files on disk,
so re-adding it restores the selection.

**Endpoint-level output** (`aggregates: true`, the default): every 10 s the
endpoint publishes its units' combined values on `mbg/endpoints/<id>/<canonical
topic>`, plus `units_online`, `units_total` and `status`. The same values go
to InfluxDB under the canonical measurements, tagged `device=<endpoint id>,
aggregate=endpoint`, written on change like every other sink.

Three rules, because three kinds of quantity behave differently:

| Quantity | Rule | Freshness |
|---|---|---|
| `power_*` (bar power factor), `current_*` | sum | only values younger than 4× the unit's poll interval (60 s floor); a stalled unit drops out |
| `voltage_*`, `frequency`, `temperature_*` | average | same gate |
| `energy_*` (counters) | sum of **last-known** values | none — but published only when EVERY expected unit has a value |

A counter is never freshness-gated: a sleeping inverter still holds its
lifetime energy, and dropping it would make the endpoint total jump backwards
and poison every `increase()` downstream. An incomplete sum is withheld
entirely, so the retained topic keeps the last COMPLETE total.

`power_factor_total` is **derived** as Σ active / Σ apparent (bounded to ±1),
never an average of ratios; `power_factor_l*` is not aggregated.

`status` is `online` (every expected unit fresh), `partial`, or `offline`, and
it publishes on every cycle — including the one where nothing is fresh, which
is exactly what a consumer needs at nightfall. `units_total` counts the units
EXPECTED to contribute (the enabled ones), so a disabled unit neither holds the
counters hostage nor makes `online` unreachable.

The aggregate's InfluxDB bucket resolves `${endpoint_id}` / `${device_id}` /
`${unit_id}` all to the endpoint itself — the endpoint's own points belong to no
single unit.

**Device liveness leaves** (every device, endpoint units included): retained
`availability` (`online`/`offline`), `runtime/status` (same verdict),
`runtime/last_seen` (ISO timestamp of the last successful read) and
`runtime/read_errors` (cumulative failed reads), published on change next to
the device's data topics.

The verdict behind `availability` / `runtime/status` is **data freshness**, not
socket state: a device counts as alive while its acquisition health is `ok` or
`degraded`, and offline once it reads `down`. A transport flag survives a
vanished endpoint; a freshness verdict does not.

### `alerts:` (opt-in)

| Key | Default | Notes |
|-----|---------|-------|
| `enabled` | `false` | |
| `mqtt` | `true` | publish alerts to MQTT |
| `webhook_url` | `""` | POST each alert as JSON (redirects refused — the request may carry credentials) |
| `webhook_headers` | `{}` | e.g. an API key header |
| `webhook_body` | *(unset)* | optional body template: a dict whose string values are rendered with the alert's fields |
| `min_interval_s` | `300` | per-alert-key rate limit |
| `latency_ms` | `1000` | read-latency alert threshold |
| `buffer_points` | `1000` | Influx-buffer-depth alert threshold |
| `signals.device` | `true` | device up/down alerts |
| `signals.sink` | `true` | MQTT/InfluxDB sink up/down |
| `signals.latency` | `true` | |
| `signals.buffer` | `true` | |
| `signals.threshold` | `false` (opt-in) | value alerts from the dashboard's per-register thresholds |
| `threshold_deadband_pct` | `2.0` | hysteresis around a threshold before re-alerting |
| `threshold_alert_on_start` | `true` | evaluate thresholds immediately at startup |

See [alerts-webhooks.md](alerts-webhooks.md) for payloads and examples.

### `esphome:` (opt-in — Device Builder)

Raw block: `{enabled, url, username, password, timeout_s}`. Usually managed from
the UI or seeded by `ESPHOME_URL` (see above).

## Per-register options

These appear in `selected_registers.json` / `config/devices/<id>/selected_registers.json`
and in device templates (`registers:` list). The decode pipeline per read:
**raw registers → byte order → type decode → `nan` sentinel check → (`enum`/`bits`
text decode) or (`scale` + `offset`) → `monotonic` filter → outputs.**

| Key | Default | Meaning |
|-----|---------|---------|
| `address` | required* | Modbus address, `0–65535` (\* HTTP/JSON/MQTT registers may use `json_path` instead) |
| `name` | required | field name — becomes the MQTT topic leaf and the InfluxDB field ([canonical names](canonical-fields.md) recommended) |
| `label` | = name | human label |
| `unit` | `""` | `V`, `A`, `W`, … (also drives HA typing inference) |
| `data_type` | `float` | `float`/`float32`, `double`, `int16`/`short`, `uint16`, `int32`, `uint32`, `int64`/`long64`, `uint64`, `string:N` (N registers of ASCII), plus signed-magnitude `sm16`/`sm32` |
| `register_type` | `holding` | `holding` (FC3), `input` (FC4), `coil` (FC1/FC5), `discrete` (FC2); aliases accepted (`fc4`, `ir`, `di`, …) |
| `poll_group` | `normal` | which poll group reads it |
| `scale` | `1.0` | **engineering = raw ÷ scale + offset** (SunSpec int+SF, transformer ratios) |
| `scale_from` | `""` | **dynamic scale factor** (SunSpec `*_SF`): name of a sibling register whose raw value is a signed base-10 exponent — **engineering = raw × 10^SF**. A fixed `scale` MAY accompany it and divides AFTER the exponent, as the unit conversion the exponent cannot express — SunSpec reports power factor as a PERCENTAGE, so the Fronius maps carry `scale: 100` to publish the ±1 fraction every other device does. No valid SF (missing, non-numeric, \|SF\| > 10) reads as *missing* rather than wrongly scaled; the poller prescans SF registers per batch and bridges gaps with the last good exponent |
| `offset` | `0.0` | zero-point / unit shift, applied after scale |
| `nan` | *(unset)* | not-available sentinel: `true` = the type's standard value (`0x8000`/`0xFFFF`/…), or a raw value, or a list; a match reads as *missing*, not data |
| `monotonic` | `false` | cumulative counter (energy): a downward glitch is rejected so it never looks like a counter reset to consumers |
| `enum` | *(unset)* | `{code: label}` — raw int → status text; unmapped codes render as `unknown (n)` |
| `bits` | *(unset)* | `{bit: name}` — status word → joined names of the set bits |
| `mask` / `shift` | *(unset)* | extract a sub-field before `enum` decode: `(raw & mask) >> shift` |
| `json_path` | `""` | HTTP/JSON + MQTT input: dot/bracket path into the payload |
| `topic` | `""` | MQTT input: per-register source topic (else the device's base topic) |
| `thresholds` | *(unset)* | color-coding thresholds (dashboard; also feeds value alerts when enabled) |

**Byte order** is set per *template*, not per register: `protocol.byte_order`
in the template — `big`/`abcd` (default), `little`/`cdab`, `badc`, `dcba`.
Ambiguous names like `le` are deliberately rejected (they fall back to big).

### Home Assistant typing (explicit, overrides the unit heuristic)

| Key | Meaning |
|-----|---------|
| `device_class` | HA device class (`power`, `energy`, `voltage`, …); `none` suppresses the key |
| `state_class` | `measurement` \| `total` \| `total_increasing` |
| `entity_category` | `diagnostic` \| `config` — groups the entity out of HA's main view |
| `enabled_by_default` | `false` → HA hides the entity until enabled |
| `icon` | mdi icon, e.g. `mdi:flash` |
| `suggested_display_precision` | decimal places in HA |

### Routing and UI (selected-registers files)

| Key | Default | Meaning |
|-----|---------|---------|
| `mqtt.enabled` | `true` | publish this register to MQTT |
| `mqtt.topic` | `""` | topic override (else derived from name) |
| `influxdb.enabled` | `true` | |
| `influxdb.measurement` | `""` | measurement override |
| `influxdb.tags` | `{}` | extra tags |
| `ui.show_on_dashboard` | `true` | |
| `ui.widget` | `value` | widget type + widget config |

### Template-only extras

| Key | Meaning |
|-----|---------|
| `access` | `RD` / `RD/WR` (informative) |
| `category` | UI grouping (validated against the template's `categories` when declared) |
| `writable` | declares the register for the write API / HA write entities. **Guards below are OPT-IN** — capability is the base; each guard you declare is enforced on every write AND improves the write dialog. A writable register with no guards accepts any finite value after an explicit confirmation; even an *undeclared* register can be written through the raw path (`unguarded: true`). |
| `write_min` / `write_max` | optional engineering-value bounds — out-of-range writes are refused (min must be ≤ max) |
| `write_allowed` | optional list of exactly-allowed values (e.g. `[0, 1, 4]` for a mode register) — anything else is refused; renders as a dropdown |
| `write_safe` | value to revert to when a write-lease expires (required to use `lease_ms`; raw unguarded writes cannot lease) |

### `calculated:` — derived measurements a template ships

A template may carry a top-level `calculated:` list next to `registers:`. Each
entry is an ordinary calculated register (a formula over the device's own
measurements, evaluated at a synthetic address and routed to MQTT/InfluxDB like
any other value) — except the TEMPLATE owns it, so every device seeded from
that template gets it without anyone retyping the formula. This is how a
vendor's decoded status or an alarm flag ships WITH the device map instead of
being reinvented per unit.

```yaml
calculated:
  - name: status_text            # must not collide with a register name
    label: Operating state
    expr: operating_state        # same safe expression language as the UI
    poll_group: normal           # must be one the template declares
    topic: status/text           # MQTT leaf, relative to the device prefix
    influxdb: false              # text has no use as an InfluxDB field
    enum: {4: Tracking power point, 7: One or more faults exist}
```

| Key | Meaning |
|-----|---------|
| `name` | required; shares the device's value namespace with register names, so it may not shadow one |
| `expr` | required; validated at template load, same whitelisted AST as user formulas |
| `label` / `unit` / `decimals` | as for any calculated register |
| `poll_group` | which group triggers the evaluation (default `normal`) |
| `topic` | explicit MQTT leaf — a derived value usually belongs under an existing branch. Omit it and the value publishes under its flat name (what every pre-existing calculated register does; routing identity never shifts under an upgrade) |
| `measurement` | explicit InfluxDB measurement |
| `enum` | `{code: label}` — turns the computed code into text through the same decoder real registers use; unmapped codes render as `unknown (n)` |
| `mqtt` / `influxdb` | `false` keeps the value off that sink |

Existing devices are **not** re-seeded when a template gains derived
measurements — a device's selection is a copy taken once. Carry them over with
a migration (see `scripts/migrate_p3_fronius_pf_status.py` for the Fronius one).

See also: [MANUAL.md](MANUAL.md) · [upgrade-guide.md](upgrade-guide.md) ·
[canonical-fields.md](canonical-fields.md) · [csv-import.md](csv-import.md) ·
[yaml-import.md](yaml-import.md) · [rtu-serial.md](rtu-serial.md) ·
[alerts-webhooks.md](alerts-webhooks.md) · [API.md](API.md)
