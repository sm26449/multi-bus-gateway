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
   file/UI on each boot. Secrets set via env (`MQTT_PASSWORD`, `INFLUXDB_TOKEN`)
   are *shadowed*: a UI save writes the config-file value
   back to disk, never the env secret.
3. **`config/selected_registers.json`** (the legacy primary device; ignored
   without one) and
   **`config/devices/<id>/selected_registers.json`** (every other device) — the
   per-device register selection, poll groups, energy fields and calculated
   registers.

Unknown keys in `config.yaml` are ignored at load time (the parser reads only
the keys listed below, with defaults for anything missing) — but see the
[upgrade guide](upgrade-guide.md) for what a *save* does to unknown keys.

If `config.yaml` fails to parse at boot, the gateway restores the last-known-good
snapshot instead of running on defaults, keeps the broken file as
`config.yaml.bad`, and disables saves until it is repaired (visible in
`/api/status` → `config`). A save tried meanwhile answers 409 with what to
repair (`detail.config_blocked: true`).

**A section in the wrong shape** (a scalar or a list where a section of
`key: value` lines belongs, e.g. `mqtt: true`):

| Section | In another shape | Empty (`null`) |
|---|---|---|
| `modbus`, `mqtt`, `influxdb` | **the whole load is refused** as above (`.bad`, last good copy, saves blocked); the log names the key. Their defaults would lose the address, the broker, the credentials and the token for good on the next save | its defaults |
| `ui`, `ui.auth`, `ui.tls`, `security` | **the whole load is refused**, the key named: their defaults would remove the login, TLS or the allowlist | **refused too** |
| `opcua`, `polling`, `alerts`, `http_output`, `rest_push`, `opcua_output`, `pq_recorder`, `gateway`; `polling.groups` and each group in it; `mqtt.ha_discovery` | read as its defaults (a group: skipped), one WARNING naming the key; the rest of the file loads | its defaults, silently |

One shape is understood rather than refused: `mqtt.ha_discovery: true|false`
(the per-device form) is read as `{enabled: true|false}` with the default
prefix, with a WARNING.

**A load that read a section as its defaults never refreshes
`config.yaml.good`** (one WARNING says so): the last good copy keeps the real
section until the file is repaired.

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
| `DOCKER_INFLUXDB_INIT_USERNAME` / `_PASSWORD` / `_ORG` / `_BUCKET` / `_ADMIN_TOKEN` | `admin` / **required** / `multibus` / `multibus` / **required** | InfluxDB first-boot self-setup; no built-in password or token since 3.81.0 — `docker compose up` refuses to start without them; paste the token into Config → InfluxDB |
| `GF_SECURITY_ADMIN_PASSWORD` | **required** | Grafana `admin` login (no built-in default since 3.81.0) |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | `mbg` / **required** | the bundled broker's only account; the broker refuses the `.env.example` placeholder (3.83.0) |
| `STACK_BIND` | `0.0.0.0` | host interface the bundled broker, InfluxDB and Grafana bind to; `127.0.0.1` keeps them host-local |
| `MBG_VERSION` | `latest` | image tag for the gateway and the serial bridge: a version, a minor line (`3.83`) or `latest` |
| `MQTT_BROKER_PORT` / `MQTT_WS_PORT` / `INFLUXDB_HOST_PORT` / `GRAFANA_HOST_PORT` / `MQTT_EXPLORER_PORT` | `1883` / `9001` / `8086` / `3000` / `4000` | host ports of the bundled services (Explorer only with `--profile debug`, on 127.0.0.1) |
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
| `identity_scope` | `gateway` on a fresh install | how **new** devices are named. `gateway`: topic `mbg/<gateway.id>/devices/<id>`, bucket `<gateway.id>-<id>`, HA unique_id `mbg_<gateway.id>_dev_<id>_…` — two gateways can share one broker and one InfluxDB. `legacy`: `mbg/devices/<id>`, bucket `<id>`, HA `mbg_dev_<id>_…`. A file **without** the key (every install before 3.97) is `legacy`, and the next save writes it out. Existing devices keep their stored prefix, bucket and HA node (`devices[].mqtt.ha_node`, written for devices created under `gateway`; none = `mbg_dev_<id>`) under either scope: changing it (Settings → General) names new devices only. A snapshot restore or backup import keeps the live scope and id unless asked (`restore_identity=true`). Another value is reported and read as `legacy`. To switch an older file to `gateway` by hand, also remove a `default_topic_pattern` / `default_bucket_pattern` it wrote out, or use Settings, which does both. |
| `gateway.id` | `mbg-` + 6 hex characters, generated on a fresh install | this gateway's id (`^[a-z0-9][a-z0-9-]{1,31}$`), the `{gateway}` of the default patterns. Kept once saved. Written when set; a `gateway` scope without one gets one generated and written at once. `gateway` is reserved. Changing it names devices created afterwards only (topic, bucket and HA node). An invalid value is reported and ignored. |
| `primary_device` | `false` on a fresh install | whether the **legacy primary device** exists — device #1, id `umg512`, a Janitza UMG 512-PRO read over the flat `modbus:` section (topics under `mqtt.topic_prefix`, Influx tag `janitza_umg512`, HA device `janitza_umg512`). A fresh install has none: add devices from the UI. A file **without** the key (written before 3.96) is an older install: it has device #1 exactly when it holds a `modbus:` section (or `MODBUS_HOST` is set), keeps `mqtt.topic_prefix: multibus/umg512` and `ha_discovery.device_name: Janitza UMG 512-PRO` where it did not name them, and the next save writes all three out. Set `false` on a file an older gateway saved for a bench with no such meter — and again after a downgrade and re-upgrade (an older gateway drops the key and brings the meter back). `MODBUS_HOST`, or an explicit `true`, also keeps the old prefix and HA name where the file is silent. An import or restore that changes it answers `restart_required`. Without a primary, a history/energy query must name a device. An unreadable file keeps device #1 (saves are blocked until it is repaired). The id `umg512` stays reserved either way. |

### `modbus:` — the primary device connection

Read only when `primary_device` is true (an older install). The primary device
is always Modbus TCP; its connection lives in this flat
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
| `connect_settle_s` | unset (the template's `protocol.connect_settle_s`, else `0`) | a pause after every successful connect, before the first request. Some gateways ignore or refuse what arrives in the first seconds after a TCP connect (a Huawei SDongle, its WLAN on port 6607, some serial bridges), and the first batch after every reconnect then burns its whole retry budget. Runs once per (re)connect, in `connect()` and in the reconnect inside a read, never per read. It is held under the transport lock, so a unit sharing the socket waits too, and it belongs to the access point: when several units share one, the largest value wins, as with `endpoint_min_gap_s`. A template can carry a default in its `protocol` block; the connection's own value overrides it, and an explicit `0` turns it off. Clamped to 0..30 s. Keep it below `endpoint_wait_s`, or a sibling waiting for its turn during the pause skips that cycle (`bus_busy`). `0` keeps the old behaviour |
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
| `topic_prefix` | `mbg/gateway` | the gateway's own root (`<prefix>/status`, `<prefix>/site/meta`, virtual-meter state) and the primary device's prefix where there is one. An older install whose file does not name it keeps `multibus/umg512` (see `primary_device`). |
| `retain` | `true` | |
| `qos` | `0` | |
| `publish_mode` | `changed` | `changed` or `all` |
| `heartbeat_interval` | `0` (off) | in `changed` mode, force a republish of an *unchanged* value after this many seconds so steady readings keep a fresh timestamp (HA doesn't grey the entity out) |
| `ha_discovery.enabled` | `true` | Home Assistant MQTT discovery |
| `ha_discovery.prefix` | `homeassistant` | |
| `ha_discovery.device_name` | `""` | the primary device's HA name (only with `primary_device: true`; an older install whose file does not name it keeps `Janitza UMG 512-PRO`). Without a primary, other devices and virtual meters carry no `via_device` and the virtual meters' HA ids are `mbg_vmeter_<id>` (with one: `janitza_vmeter_<id>`, unchanged). |
| `allow_write_entities` | `false` (opt-in) | expose writable registers as HA number/select entities and subscribe to their command topics. **Double-gated**: a command executes only when this AND `security.allow_writes` are true, the register is declared writable, and the value is within its envelope. Refused while `mqtt.username` is empty: an anonymous broker session would turn any LAN publish into a hardware write (3.80.0). |
| `default_topic_pattern` | the scope's: `mbg/devices/{device}` (`legacy`), `mbg/{gateway}/devices/{device}` (`gateway`) | topic-prefix pattern seeded onto **new** devices (`{device}` = device id, `{gateway}` = `gateway.id`, under either scope; `{gateway}` needs an id). An explicit pattern wins over the scope's. A legacy file keeps it written out (as every save before 3.97); under `gateway` only a chosen one is written. Namespace convention: device values under `mbg/devices/<id>/…` or `mbg/<gateway>/devices/<id>/…`; `mbg/vmeter/<id>/…` is reserved for virtual-meter MQTT publishing. Pre-existing devices keep their persisted prefixes. The prefix a new device gets is checked (unique, no overlap, no `+`/`#`; see docs/API.md). |
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
| `default_bucket_pattern` | the scope's: `{device}` (`legacy`), `{gateway}-{device}` (`gateway`) | bucket pattern seeded onto **new** devices; `{gateway}` as in `mqtt.default_topic_pattern`, and written out the same way |
| `heartbeat_interval` | `600` (written into the file) | in `changed` mode, rewrite an *unchanged* value once it has not been written for this many seconds, so state timelines and "last value" panels always have a recent point; `write_interval` stays the minimum gap; whole seconds `0`–`86400`, `0` = off. A config.yaml without the key gets `600` written into it on its first load (one INFO log line); like any save from the UI, that one-time rewrite drops the file's comments and YAML anchors (`config.example.yaml` already has the key, so a fresh copy of it is never rewritten). From then on the file's value is the only one that runs (Settings → InfluxDB). A device or an installation sets its own in its `influxdb:` block (a device's **Outputs → InfluxDB** shows "gateway: N s" beside an empty field); a value that is not whole seconds is ignored with a warning (the gateway's applies). An installation's totals and its extra InfluxDB outputs use the installation's value, else the gateway's, so a total that stays at 0 W all night is still rewritten. **Upgrade note:** in a high-availability pair (available in MBG Pro) run the same version on both nodes — a newer node adds this key to a file synced from an older one. |
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
| `nodes_url` | `""` (no link) | address of the ESP nodes service (available in MBG Pro), a path such as `/nodes/` or an `http(s)://` URL; when set, Devices shows an **ESP nodes** link to it (signed-in users only). Settings → Security |
| `tls.enabled` | `false` (opt-in) | HTTPS for the UI (uvicorn TLS) |
| `tls.cert` / `tls.key` | `""` | container-local paths |
| `trusted_proxies` | `[]` (trust nobody) | reverse-proxy IPs whose `X-Forwarded-*` headers are trusted. Required when TLS terminates in a proxy in front — otherwise lockouts, the IP allowlist and the audit trail see the proxy's IP |
| `timezone` | `Europe/Bucharest` | IANA timezone for calendar reports (monthly-energy month boundaries) |
| `default_colors` | `{}` | default widget colors (phase convention + per-category hues), applied to **new** widgets only |

### `security:`

| Key | Default | Notes |
|-----|---------|-------|
| `allowlist` | `[]` (open) | IPs/CIDRs allowed to reach the HTTP API/UI. Loopback and the docker gateway are always allowed. An entry that does not parse **denies every client** until it is fixed (3.83.0) — the API refuses such an entry, so this only happens to a hand-edited file. |
| `allow_nonlan_http_devices` | `false` (opt-in) | SSRF guard: HTTP/JSON device URLs must point at a private LAN host unless this is true |
| `allow_writes` | `false` (opt-in) | master **arming** switch for Modbus writes (FC5/6/15/16). When armed, any device that is not `write_locked` can be written — declared registers with their template encoding + whatever guards were declared; undeclared registers via the raw path (`unguarded: true` in the payload). Every write is authenticated, rate-limited and audited. |
| `primary_write_locked` | `true` | per-device write LOCK for the primary. Read-only used to be hardcoded; it is now this flag, defaulting to locked so the historical behavior survives upgrades. Unlock deliberately. Non-primary devices carry `write_locked` in their `devices[]`/`endpoints:` entry. A device, installation or adopted device made from the UI or the API is stored locked (`write_locked: true`), and a restored one comes back locked with its command bindings off; an entry written by hand without the key stays unlocked, as before. Switch it from the device's ⋯ menu: **Unlock writes…** (it asks first) / **Lock writes**. |
| `write_rate_limit_per_s` | `10.0` | per-client-IP write rate limit on the write API; excess gets 429; `0` disables |

### `preview:` — the read preview's budget

What a read preview may cost: Add Device → **Check**, and **Preview** on a
device's page. Set in Settings → Modbus → **Read preview budget**, or with
`GET/POST /api/config/preview`. Defaults: `PREVIEW_BUDGET` in
`multibus/config.py`.

| Key | Default | Notes |
|-----|---------|-------|
| `max_requests` | `4` (written into the file) | at most this many read requests per preview, whole number `1`–`20` |
| `listen_s` | `10` (written into the file) | how long a preview listens on a bus another master polls (it never transmits there), whole seconds `1`–`60` |

A file without the section gets it written in on its first load, in one
rewrite with one INFO line (as `influxdb.heartbeat_interval`); nothing else in
the file changes. A value out of range runs as its default, with a warning.

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

### `modules:` (Settings → Modules)

Separate services beside the gateway, not part of the open-source gateway:

```yaml
modules:
  - id: nodes
    name: ESP nodes
    icon: usb-plug
    url: /nodes/
    health: /nodes/api/health          # optional: the page shows whether it answers
    description: Flash ESP boards from the browser and list the nodes.   # optional
```

Listed in Settings → Modules once signed in, each with whether it answers
and an Open button; a wrong entry is skipped and logged. See
[API.md](API.md#modules-settings--modules).

### `opcua_output:` (primary device)

Whether the primary device is served on the OPC UA server, and which values:
`opcua_output: {enabled: true, fields: [frequency, power_active_total]}`.
Absent = served, every value. Other devices carry an `opcua:` block; see
[opcua.md](opcua.md#which-devices-and-values-are-served).

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

A save through the API (create, edit, `PATCH`) checks the advanced connection
keys it sets to a new value, each in its range: `retry_attempts`
0..10, `retry_delay` 0..60, `stale_after_s` 0..86400, `max_gap` 0..125,
`illegal_registers` (addresses 0..65535, up to 1000), `startup_jitter_s`
0..3600, `max_connections` 1..8, `endpoint_wait_s` 0.1..600,
`endpoint_min_gap_s` 0..60, `connect_settle_s` 0..30, and the switches
`drop_all_zero`, `serialize_endpoint`, `share_transport` as true/false. A
value already stored (written by hand) is not judged again until a save
changes it. The ranges and defaults live in `multibus/conn_advanced.py`.

`energy_counter_resets: [{at, ts, reason}]` is written by **Replace hardware**
(`PATCH /api/devices/{id}?intent=replace`): when a meter's counter started
again from a low value. Every save keeps it; do not edit it by hand.

```yaml
devices:
  - id: sdm630_garage            # required, unique, [A-Za-z0-9][A-Za-z0-9_.-]*
    name: Garage SDM630
    template: eastron_sdm630     # device-template id
    enabled: true
    kind: meter                  # the device's type: a built-in kind or one of your device_types (absent = the template's)
    write_locked: true           # stored true for every device made from the UI or the API; absent = false
    connection:
      protocol: tcp              # tcp | udp | rtu | ascii | rtu-tcp | ascii-tcp | http | mqtt | rtu_tap | seplos_v2 | seplos_v2-tcp | can
      # tcp / udp / rtu-tcp / ascii-tcp:
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
      # rtu / ascii (direct serial):
      # serial_port: /dev/ttyUSB0
      # baudrate: 9600
      # parity: N                # N | E | O
      # stopbits: 1
      # bytesize: 8
      # http (HTTP/JSON input): url, timeout, headers, verify_tls
      # mqtt (MQTT input): broker, port, username, password, tls, topic,
      #   accept_retained (true = ingest the broker's retained snapshot on
      #   subscribe — for collectors that retain + carry an LWT; a restart
      #   then repopulates instantly instead of waiting for each change),
      #   availability {topic, online, offline}, stale_after_s, vars {name: value},
      #   keepalive {topic, first_payload, payload, every_s} or false (overrides
      #   the template's protocol.keepalive), tls_ca (CA file) / tls_insecure
      # can (CAN input, listen-only): carrier (socketcan), channel (can0),
      #   bitrate (what the bus runs at — shown; the host sets it), stale_after_s
      # seplos_v2 (serial_port + line) / seplos_v2-tcp (host + port): unit_id = the
      #   pack address, mode: poll | listen
    mqtt:
      topic_prefix: meters/${device_id}   # ${device_id} / ${id} substituted
      ha_discovery: true
      # ha_node: mbg_<gateway>_dev_<id>   # written for a device created under identity_scope
      #                                   # gateway; absent = mbg_dev_<id>. Never change it by hand:
      #                                   # it is the Home Assistant device every entity hangs on
      enabled: true              # route this device's values to MQTT
      # its own publishing; absent = the gateway's (mqtt: above, Settings → MQTT)
      # publish_mode: changed     # changed | all (every reading)
      # heartbeat_interval: 30    # s; resend an unchanged value; 0 = never
    influxdb:
      bucket: sdm630_garage      # default: the global bucket
      device_tag: sdm630_garage  # default: the device id
      enabled: true
      tags: {}                   # static tags on every point (see endpoints: below)
      # its own publishing; absent = the gateway's (influxdb: above)
      # publish_mode: changed     # changed | all
      # write_interval: 5         # s; at most one point per value this often
      # heartbeat_interval: 600   # s; rewrite an unchanged value; 0 = never
    http_output: {enabled: false}
    opcua: {enabled: true}       # served on the OPC UA server; fields: [..] = only those values
                                 # (absent = served, every value; a device made in the UI gets false)
    rest_push: {}                # same shape as the primary's block
    pq_recorder: {}              # same shape as the primary's block (Janitza only)
```

`rtu-tcp` speaks RTU framing over a TCP socket (a ser2net-style serial bridge);
`rtu` opens a local serial port directly; `ascii` and `ascii-tcp` are the same
two with Modbus ASCII framing (`:` … LRC CRLF, older PLCs and meters); `udp`
sends Modbus TCP frames in UDP datagrams (devices whose manual says UDP);
`rtu_tap` is a LISTEN-ONLY observer
of a bus mastered by someone else (never transmits — decodes the existing
master↔slave traffic) — see [rtu-serial.md](rtu-serial.md).
`seplos_v2` / `seplos_v2-tcp` are not Modbus: they read a Seplos V2 BMS over
its ASCII protocol (a serial port here, or a bridge's raw TCP port), read-only,
with `mode: poll` (the gateway asks the pack) or `mode: listen` (it decodes a
master pack's exchanges and never transmits). The mode is required for a new
device; a stored one without it reads as `poll`. A template that only listens
(`protocol.listen_only`, e.g. `seplos_bms_v2_tap`) refuses `mode: poll`. One
device per pack address on a port. See
[seplos-v2.md](seplos-v2.md).
`can` listens to a CAN bus through a carrier: `carrier: socketcan`,
`channel` (default `can0`), `bitrate` (default `500000`, 10000–1000000; what
the bus runs at, shown only — the host sets it), `stale_after_s` (1–3600; the Add form proposes `5`, a file without it gets
the connection default `30`). Frames are decoded by the template's signal rows, nothing is
ever transmitted, and the device cannot be written. The interface itself
must be listen-only on the host. Added from Add Device (port **CAN**) or the
API. See [can.md](can.md).

More keys tie a device into an installation, give it named actions and parts
(user-facing walk-through in [MANUAL.md §5b and §14b](MANUAL.md)):

| Key | Default | Meaning |
|-----|---------|---------|
| `group_id` | `""` | which group of its endpoint the unit belongs to (`inverters`, `meters`…); the aggregator sums powers per group, never across groups |
| `role` | `""` | what the unit is inside the installation (`inverter`, `meter`, `site`…) — a label the plant page and the aggregator read, not a permission |
| `commands` | `[]` | command bindings: `[{name, from_template, enabled, faces, confirm, lease_s, safe}]` enables a preset the template ships (or carries a full inline recipe); see [commands-design.md](commands-design.md). Edited per command with `PUT /api/devices/{id}/commands/{name}` |
| `channels` | `{}` | this device's pace for a template channel, over the template's: `{<channel or command name>: {min_interval_s, min_on_s, min_off_s, max_starts_per_h, max_age_s}}`, each 0..86400 (`max_age_s` at least 1, `max_starts_per_h` whole). Absent = the template's (`max_age_s` 30 s). An installation group carries the same under `groups[].channels`. Edited from the device's **Commands & bindings…** or `PUT /api/devices/{id}/channels/{key}` |
| `components` | `[]` | parts a device holds that are devices of their own (a GX's batteries and sensors): `[{type, instance, name?, enabled?}]`; each becomes a device `<parent>-<type>-<instance>` read through its parent's connection. A malformed or repeated entry is skipped with a warning |

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
      protocol: tcp              # tcp | udp | rtu-tcp | ascii-tcp | rtu_tap (serial_port + baudrate instead of host/port)
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
    opcua:       { enabled: true }         # every unit; a group's own `opcua:` wins (its template, its fields)
```

An endpoint is ONE endpoint, so the sinks that belong to the endpoint are declared
once: `write_locked`, `http_output` and `rest_push` sit on the endpoint and are
propagated to every materialized unit. They are edited from any unit's ⋯ →
Outputs… (the panel says it applies endpoint-wide; the write lock from its ⋯ →
Unlock writes… / Lock writes) or from the endpoint page.

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

**Template-declared totals and your own (`totals:`).** Beyond the three rules,
a template may declare how a vendor's fields combine across a bank or a farm:
a register (or calculated register) carries `aggregates: {output_name: op}`
with `op` one of `sum`, `avg`, `min`, `max`, `spread`, `mode` (the majority
value, text included — a bank's one-word status) — `soc` →
`pack_average_soc: avg, pack_min_soc: min, pack_max_soc: max` all at once.
The endpoint may override any of them, or add its own, without touching the
template:

```yaml
endpoints:
  - id: battery-bank
    totals:
      # replaces the template's pack_max_temp (cells only) with cells + ambient
      pack_max_temp: {op: max, from: [cell_temp_1, cell_temp_2, cell_temp_3, cell_temp_4,
                                      ambient_temp, min_cell_temp, max_cell_temp]}
      pack_balancing_cells: {op: sum, from: [balancing_count]}
```

Every listed field of every **fresh** unit goes into one pool, then the
operation runs over it. A total named here replaces the template's
declaration of the same name; the rest of the template's totals still apply.
Totals publish with the others (MQTT under the totals topic, InfluxDB under
the canonical measurement or `endpoint`). The endpoint page shows the
template's totals, the fields the units carry, and edits these.

**Static InfluxDB tags (`influxdb.tags`).** Tags written on every point of
every unit, raw and calculated — `${unit_id}`, `${endpoint_id}` and
`${device_id}` are substituted per unit (a device can carry them too, under
its own `influxdb:`). They never displace the identity tags (`device`,
`address`, `name`) nor a tag a register declares itself. A tag is part of a
series' identity: changing one starts a new series.

```yaml
    influxdb:
      bucket: seplos
      device_tag: battery_${unit_id}
      tags: {battery_id: "${unit_id}"}     # → device=battery_3,battery_id=3
```

**Extra InfluxDB outputs (`influxdb.outputs`).** The totals can also be
written under a measurement and field names of the operator's choosing — the
way a consumer that reads a measurement of its own (a dashboard, a report)
keeps reading it when the source behind it changes. Each output is one point
per cycle with every field whose total exists:

```yaml
    influxdb:
      outputs:
        - id: legacy-pack            # a-z 0-9 - _ ; unique per endpoint
          enabled: true
          measurement: seplos_pack
          bucket: ""                 # empty = the endpoint's bucket; ${endpoint_id} allowed
          group: ""                  # empty = the first group's totals
          mode: changed              # changed (default) | every
          tags: {}                   # static, ${endpoint_id} allowed
          fields:
            - {name: total_power, source: pack_total_power}
            - {name: energy_remaining, source: pack_energy_remaining, scale: 0.001}   # Wh → kWh
            - {name: batteries_online, source: units_online}
```

`source` is any total of the group (template-declared, your own, the
rule-based ones, `units_online`, `units_total`); a total that does not exist
this cycle is simply left out of the point. Outputs stop with the endpoint's
`influxdb.enabled`. Editing outputs never restarts a unit; static tags do (they
change what the units write). Both are edited on the endpoint page; a form
that does not send them (the settings dialog) keeps them.

**Device liveness leaves** (every device, endpoint units included): retained
`availability` (`online`/`offline`), `runtime/status` (same verdict),
`runtime/last_seen` (ISO timestamp of the last successful read) and
`runtime/read_errors` (cumulative failed reads), published on change next to
the device's data topics.

The verdict behind `availability` / `runtime/status` is **data freshness**, not
socket state: a device counts as alive while its acquisition health is `ok` or
`degraded`, and offline once it reads `down`. A transport flag survives a
vanished endpoint; a freshness verdict does not.

### `opcua:` — the OPC UA server (off by default)

| Key | Default | Notes |
|-----|---------|-------|
| `enabled` | `false` | the `asyncua` library is imported only when true |
| `host` / `port` | `0.0.0.0` / `4840` | publish the port in Docker (`OPCUA_PORT`) |
| `encryption` | `required` | `required` (Basic256Sha256 sign & encrypt only), `optional` (also none — passwords in clear), `off` |
| `allow_anonymous` | `false` | otherwise the gateway's accounts; 5 wrong passwords lock a name for 5 min |
| `update_s` | `1.0` | how often values are pushed into the address space (0.2..60) |
| `public_url` | `""` | behind a load balancer (a high-availability pair, available in MBG Pro): `opc.tcp://<balancer>:<port>/<path>` — the path served and the host the certificate names; the server still listens on `host`/`port`. A standby takes its partner's certificate with the configuration copy |

The self-signed server certificate and key live in `config/opcua/`. See
[opcua.md](opcua.md).

### `rules:` and `commands:` — removed in 3.97

The kill switches `rules.recheck` (added in 3.94) and `commands.raw_lanes`
(added in 3.95), with their environment variables `MBG_RULES_RECHECK` and
`MBG_RAW_LANES`, were removed in 3.97; their `on` behaviour is the only one:

- a rule's write is asked again under the bus lock, just before it goes out:
  paused, yielded to another rule, or held by the channel since the rule
  decided → `cancelled`, sent again when it may
  ([rules.md §3.9](rules.md#39-several-rules-on-one-target-layers)); a rule's
  correction (`reassert_s`) waits for the channel's pace, emergency wants
  exempt;
- raw register writes — an HA entity on a register,
  `POST /api/devices/{id}/write`, a raw write's dead-man revert — travel the
  device's lane like commands: the MQTT worker never waits for a bus, a newer
  value of the same register replaces one still waiting, a write that waited
  longer than 30 s expires, every HA outcome is told on `<prefix>/cmd/result`.

A `config.yaml` that still holds `rules: {recheck: …}` or
`commands: {raw_lanes: …}` loads as before; the keys are ignored with one INFO
log line on each load until the next save drops them. The environment variables are ignored
the same way. `/api/status` no longer reports `rules_recheck` / `raw_lanes`.
Not behind any switch either: a register any command of the device writes — at
any position, on the same slave — is never written raw (HA, REST, `unguarded`
included).

### `device_types:` — your own device types

The types a device, a template and an installation group may name besides
the built-in kinds (meter, inverter, battery, site, heat_pump, drive, ups,
charger, sensor, converter, other — the ones that carry behaviour). Edited
from Templates → Device types or `/api/device-types` (admin); written only
when there is one. Part of every snapshot, the HA mirror and the export
(it lives in this file).

```yaml
device_types:
  - id: chiller                    # a-z 0-9 _, 2–32, not a built-in nor one of its aliases (system, gx, vfd…)
    labels: {en: Chiller, ro: Răcitor}   # en required: the fallback for every other language
    icon: snow                     # one of the fixed set (GET /api/device-types → icons); default other
    behaves_like: heat_pump        # a built-in kind; default other
```

A device names its type under its own `kind:` key (the key is unchanged:
`kind: ups` and `kind: chiller` are both read as the device's type). What
goes out — the device list's `kind`, `<dev>/meta` `kind` — is the type's
built-in kind, a user type's `behaves_like`: a consumer never sees an
unknown kind; `type` and `type_label` say the rest. An installation group's
`role` may be a user type too. A type is not deleted while a device, a
deleted device that can still be restored, a template or a group uses it.
An entry that cannot be read is skipped at load (a warning), kept in the
file, and its devices fall back to their derived kind.

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

### `esphome:` (removed)

The Device Builder moved out of the gateway (ESP flashing from the browser,
the node list are available in **MBG Pro**). An `esphome:` block
left in `config.yaml` by an older version is ignored (one log line) and
dropped by the next save; the `ESPHOME_*` variables are no longer read. Link
to the service with `ui.nodes_url` (listed in Settings → Modules) or a
`modules:` entry.

### `rules.yaml` — the rules engine

Rules live in `config/rules.yaml` next to `config.yaml` (edited from the Rules
page or by hand; the file is part of every snapshot). One list under `rules:`;
the full semantics — steps, debounce by sample, stale handling, clamp,
shadow vs armed — are in [MANUAL.md §14c](MANUAL.md) and
[rules-design.md](rules-design.md).

```yaml
rules:
  - id: ov-u1                          # [a-z][a-z0-9_-]{0,47}
    label: "Over-voltage · inverter 1"
    enabled: true
    mode: shadow                       # shadow (decide, never write) | armed
    target: { device: pv-u1, command: power_limit }   # or {endpoint, group, command}
    kind: steps                        # steps | condition | trim | schedule
    signal: max(pv-u1.voltage_l1_n, pv-u1.voltage_l2_n, pv-u1.voltage_l3_n)
    signal_valid: { min: 100, max: 350, max_step: 10 }  # outside min/max → ignored;
                                       # a change > max_step is held until the next sample confirms it
    stale_after_s: 90                  # oldest input older than this → stale
    steps:                             # ascending; the highest matching step wins
      - { at: 250.0, value: 80, label: Warning }
      - { at: 253.0, value: 50, label: Emergency, fast: true }   # fast: no debounce
    release_below: 248.0               # normal only under this (dead band above it)
    normal: { value: 100 }             # what "no step" asks for
    params: { revert_s: 0, ramp_s: 10 }  # fixed params sent with every want
    timing: { every_s: 2, debounce: 3, min_interval_s: 30, reassert_s: 120 }
    # optional timers: for_s (a change wanted this long), min_on_s, min_off_s,
    # max_on_s (then a rest of min_off_s), max_starts_per_h — on/off against off_value
    # (or a condition's else, a steps rule's normal); see rules.md §1
    on_stale: hold                     # hold | safe | <number> (fail closed to a value)
    on_disable: safe                   # safe | hold
```

A `trim` rule (follow a signal into a band by nudging the value) carries
`signal`, `above`, `below`, `step`, `min`, `max`, and optionally `gain`,
`start`, `off_value` instead of `steps` / `release_below` / `normal`; see
[rules.md §3.6](rules.md#36-a-drive-that-soaks-up-the-solar-surplus-trim).

### `sequences.yaml` — commands in order

Next to `rules.yaml`; edited from the Rules page (Sequences) or by hand; part
of every snapshot directory. One list under `sequences:` — each
`{id, label, params, max_duration_s, steps, on_abort}`; see
[rules.md §3.10](rules.md#310-sequences-commands-in-order).

### `attributes.yaml` — facts about the equipment

Next to `config.yaml`; edited from Settings → Site, an installation's page, a
device's ⋯ → About (attributes), or the API. Written only through the config store
(locked, atomic, 0600). Part of every snapshot, the HA mirror and the
export. Coordinates are only in the export when it includes identity.

```yaml
schema: 1
settings: {location_publish: coarse, publish_serials: false}   # coarse | exact | off; serials off by default
site:         {location: {latitude_deg: 48.86, longitude_deg: 2.35}}
installation: {pv: {}}
device:
  umg512: {grid_connection: {role: connection_point, max_import_kw: 10, max_export_kw: 8}}
  pv-u1:  {pv_input: [{mppt: 1, modules: 24, module_wp: 450, kwp: 10.8, azimuth_deg: 180, tilt_deg: 30}]}
  hp:     {identity: {description: "Pump room", location: "garage, cabinet 2", labels: [hvac, floor-1],
                      manufacturer: Samsung, model: AE160, serial_number: SN-0042, installed_on: "2024-05-17"}}
```

The catalog (categories, fields, limits) and its rules:
[attributes.md](attributes.md). A device template declares its `kind:` (a
built-in kind, see `device_types:`); the attributes that fit are offered
first. `identity` (device and installation level) holds the description,
where it is mounted, labels and the nameplate; `identity.serial_number` is
published only with `settings.publish_serials: true`. A device created with
attributes (Add Device → **Name & save**, or `POST /api/devices` with
`attributes`) gets them here under `device.<id>`; the Edit device dialog's
**Identity** section saves them with the device's one **Save**.

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
| `daily` | `false` | day counter recomputed from whatever is awake (Fronius `Site.E_Day`): the day's maximum is held, only a drop below 2 % of it (midnight) is adopted; held samples are not published or written |
| `enum` | *(unset)* | `{code: label}` — raw int → status text; unmapped codes render as `unknown (n)` |
| `bits` | *(unset)* | `{bit: name}` — status word → joined names of the set bits |
| `mask` / `shift` | *(unset)* | extract a sub-field before `enum` decode: `(raw & mask) >> shift` |
| `json_path` | `""` | HTTP/JSON + MQTT input: dot/bracket path into the payload |
| `topic` | `""` | MQTT input: per-register source topic (else the device's base topic). A `~/leaf` form is RELATIVE to the device's base topic (`seplos/battery_3/#` + `~/soc` → `seplos/battery_3/soc`) — what lets one template serve many units (3.84.2) |
| `thresholds` | *(unset)* | color-coding thresholds (dashboard; also feeds value alerts when enabled): `{enabled, dangerLow, warningLow, warningHigh, dangerHigh}`, compared with the RAW value in the register's unit. Optional `onlyWhen: {field, above}` judges them only while another field of the same device is above a value — a current's THD means nothing at near-zero load (`{field: current_l1, above: 2}`); the field missing → not judged; an alarm it was in clears with an event. Same rule on the dashboard, the fleet's alarm count and value alerts |

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
a migration (the 3.75 Fronius one shipped as a one-off script and is no longer in the tree).

**Update from template** (3.86; offered by a notice since 3.87 — the page
asks `GET …/template-changes` and shows *The template has been updated* only
when there is something to bring in, with a review of every change before it
is applied; `POST /api/endpoints/{id}/refresh-from-template`, or
`/api/devices/{id}/…` for a standalone device) carries a template's
*descriptions* over to the rows a unit already has: `label`, `unit` (kept
when the operator changed the row's scale or offset),
`description`, `category` and `aggregates` on registers, `label` and `unit`
on template-shipped calculated fields, matched by name. It keeps what the
operator decided — which rows are selected, dashboard/UI flags, MQTT/InfluxDB
switches and topics, thresholds, and every formula — and adds no row, unless
asked (`?add_new=true`; the UI asks): then the template's calculated fields
the unit does not have are added — one deleted on purpose comes back. The
units restart so the pipeline sees the new metadata; an installation's unit
is refreshed through its installation, so every unit stays alike.

### `display:` — how a unit of this kind is shown

A template may say how the UI presents a unit made from it. The block is read
**live** from the template, never copied into a unit: an improved template
reaches every existing unit on the next page load, no *Update from template*
needed. Absent, the UI falls back to the defaults of the device's role
(meter, inverter, …). The fields `glance`, `hero`, `alarms` and `bitmasks`
name must be fields of the template (a register or a template calculated
field); `headline` names **totals**, so only its shape is checked; a
`sections` key must be one of the template's `categories` when it declares
any. A template that breaks this is refused at load with the key named.

```json
"display": {
  "unit_label": "Battery pack", "unit_label_plural": "Battery packs",
  "icon": "battery-half",
  "glance": ["power", "soc", "pack_voltage", "max_cell_temp", "status"],
  "hero": ["soc", "power", "status"],
  "headline": [
    {"field": "pack_total_power", "label": "Battery power", "hint": "− charging · + discharging"},
    {"field": "pack_average_soc", "label": "State of charge"}
  ],
  "alarms": [
    {"field": "alarm_count", "severity": "warning"},
    {"field": "protection_count", "severity": "danger"}
  ],
  "sections": {
    "cells":  {"widget": "grid", "tiles": "^cell_\\d+$", "decimals": 3},
    "alarms": {"widget": "active_only"}
  },
  "bitmasks": {"balancing_bits": "Cell {n}", "alarm_cell_temp": "Sensor {n}"}
}
```

| Key | Meaning |
|-----|---------|
| `unit_label` / `unit_label_plural` / `icon` | what a unit is called on the installation page and the fleet (Bootstrap icon name) |
| `glance` | the columns of a unit's row in the installation's unit table (at most 12) |
| `hero` | the values a unit's row shows on the fleet dashboard (at most 12) |
| `headline` | the installation's top line, from its **totals** (at most 6): `field` (a total's name), `label`, optional `hint` (e.g. the sign of a battery's power) |
| `alarms` | fields whose non-zero value **is** an active alarm (a BMS's alarm/protection/failure counts, an inverter's fault word): `{field, severity}`, severity `warning` (default) or `danger`. A fresh value counts when it is non-zero, true, or a word other than empty, `0`, `off`, `none`, `normal`, `ok`, `false` or `—`. They add to the fleet's alarm counts next to explicit thresholds; each installation unit carries `alarms` and the installation `units_alarming` (`GET /api/endpoints/{id}`); the unit row shows a pill, the unit page lists what is active |
| `sections` | per category, a widget for the dashboard's section: `grid` — tiles with the lowest and highest marked (a pack's weak cell at a glance); `tiles` is a regex on the field name choosing them (default: the fields sharing the section's main unit), `decimals` (0–6) how finely tiles **and** the section's rows in the tiles' unit read; other fields stay rows below. `active_only` — only rows whose value is active, *Nothing active* otherwise, the section header marked while something is |
| `bitmasks` | `{field: pattern}` — a mask reads as the bits that are set, numbered from 1: `"Cell {n}"` → *Cell 3, Cell 7*, `—` when clear (dashboard and unit page) |

`GET /api/registers/selected?device=…` returns the block as `display`
(for any device but the primary).

See also: [MANUAL.md](MANUAL.md) · [upgrade-guide.md](upgrade-guide.md) ·
[canonical-fields.md](canonical-fields.md) · [csv-import.md](csv-import.md) ·
[yaml-import.md](yaml-import.md) · [rtu-serial.md](rtu-serial.md) ·
[alerts-webhooks.md](alerts-webhooks.md) · [API.md](API.md)
