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
| `SERIAL_BRIDGE_URL` | `http://pv-stack-serial-bridge:7000` | Base URL of the optional ser2net serial-bridge companion, used by the commissioning UI to list remote serial adapters. |
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
| `SERIAL_BRIDGE_URL` | `http://pv-stack-serial-bridge:7000` | where the gateway reaches the bridge's control API |

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
| `allow_write_entities` | `false` (opt-in) | expose writable registers as HA number/select entities and subscribe to their command topics. **Double-gated**: a command executes only when this AND `security.allow_writes` are true, the register is declared writable, and the value is within its envelope. |
| `default_topic_pattern` | `meters/{device}` | topic-prefix pattern seeded onto **new** devices (`{device}` = device id) |
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
| `allow_writes` | `false` (opt-in) | master gate for Modbus writes (FC5/6/15/16) to devices. The primary device stays read-only regardless. |
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
```

`rtu-tcp` speaks RTU framing over a TCP socket (a ser2net-style serial bridge);
`rtu` opens a local serial port directly — see [rtu-serial.md](rtu-serial.md).

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
| `writable` | opt-in: register may be written via the write API |
| `write_min` / `write_max` | engineering-value bounds — **required** for a writable holding register |
| `write_safe` | value to revert to when a write-lease expires |

See also: [MANUAL.md](MANUAL.md) · [upgrade-guide.md](upgrade-guide.md) ·
[canonical-fields.md](canonical-fields.md) · [csv-import.md](csv-import.md) ·
[yaml-import.md](yaml-import.md) · [rtu-serial.md](rtu-serial.md) ·
[alerts-webhooks.md](alerts-webhooks.md) · [API.md](API.md)
