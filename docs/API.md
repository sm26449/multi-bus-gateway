# REST API Reference — Multi-Bus Gateway

Hand-maintained reference for the routes defined in `multibus/api.py` and `multibus/routes/`.
Copy-paste recipes (log in, read, commands, rules, MQTT, a Python client):
[api-examples.md](api-examples.md). The gateway also serves a generated,
interactive reference at `/docs` (Swagger) and `/redoc`.
Base URL: `http://<gateway>:8080` (default port; `ui.port` / `UI_PORT`).

## Authentication & roles

Everything below is subject to three independent gates. The IP allowlist and
the API key are opt-in; **login is enabled automatically on a fresh install**
(the first run generates an admin password and prints it once to the log):

1. **IP allowlist** (`security.allowlist`) — applies to every request,
   including `/health` and `/metrics`.
2. **API key** (`API_KEY` env, honoured as `JANITZA_API_KEY` for
   back-compat) — when set, every state-changing request
   (POST/PUT/PATCH/DELETE) must send `X-API-Key: <key>`. The exempt POSTs
   are the read-only queries (`/api/query/register`, `/api/query/batch`),
   the login flows (`/api/auth/login`, the passkey `login/begin` and
   `login/finish`) and `/api/auth/logout` (ending your own session is not
   a state change) — see `_open_writes` in `multibus/api.py`.
3. **Login** (`ui.auth.enabled`) — session-cookie auth (`janitza_session`,
   HttpOnly, SameSite=Lax, 7-day sliding TTL, 30-day absolute cap). Passwords are PBKDF2-SHA256
   (600 000 iterations). Login is rate-limited per client IP
   (`lockout_threshold` / `lockout_minutes`). Passkey (WebAuthn) login is an
   alternative to the password.

**Programs** (a companion service, a script) sign in with a **service
token** instead of a person's password: `Authorization: Bearer mbgst_…`.
An admin makes one in Settings → Security → **Service tokens**, named after
who uses it and with one role; it is shown once. The token acts with its
role, never gets a cookie, needs no `X-API-Key`, appears in the audit log as
`token:<name>`, and can be revoked alone. Only its SHA-256 is stored
(`config/service_tokens.json`, 0600). Besides viewer, operator and admin, a
token can have the role **`ha`**: a high-availability companion service
(available in MBG Pro) that
reads like a viewer and may set the gateway's role and raise alerts — nothing
else.

When login is enabled, three roles exist. The **Role** column in the tables
below is the *minimum* role required. With login disabled, endpoints are open
(modulo allowlist/API key) with two deliberate exceptions: snapshot download
and secrets/identity export return 403 unless a valid API key proves intent
— a config bundle full of tokens must never be one anonymous GET away. With
login enabled those two, and raw (`unguarded`) writes, are **admin only**:
the API key is a gate for machine clients, never a substitute for a role —
every operator must present it on every write, so it cannot also unlock
what the operator role withholds (3.83.0).

| Role | May do |
|---|---|
| `viewer` | read-only: any GET/HEAD, plus the two read-only query POSTs |
| `operator` | viewer + live commissioning actions: bus trace, diagnostics probe, discovery, on-demand queries, device tests, payload samples, Modbus **writes** (within template bounds), alert test-fire, register reload, own passkey enrollment |
| `admin` | everything, including anything that lands in a config file (devices, registers, templates, virtual meters, settings, snapshots) and the audit trail |
| `—` (open) | reachable without a session even when login is on: login endpoints, `/api/auth/status`, `/api/auth/forward` (checks the session itself), `/health`, `/metrics`, `/static/*`, `/favicon.ico` |

Cross-site browser requests are rejected (Sec-Fetch-Site / Origin checks) on
all mutating endpoints and on `/ws`.

Errors are JSON: `{"detail": "..."}"` or `{"detail": {"errors": [...]}}` with
conventional status codes (401 unauthenticated, 403 forbidden, 404 not found,
409 conflict, 413 too large, 422 validation, 429 throttled/locked out,
502/503 upstream/subsystem unavailable).

---

## Sessions & passkeys

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/auth/status` | Is auth on; caller's role; whether viewer/operator accounts and passkeys exist; `nodes_url` (`ui.nodes_url`) and `modules` (Settings → Modules, the ESP nodes service included) — both empty before login | — |
| GET | `/api/auth/forward` | Traefik **forwardAuth** for a companion service on the same host (the ESP nodes service of MBG Pro, under `/nodes`): **200** with `X-Mbg-User` / `X-Mbg-Role` for a live session (the proxy copies them onto the forwarded request, replacing whatever the client sent); without a session a browser navigation (`Accept: text/html`, an `X-Forwarded-Uri` outside `/api/`) gets a **302** to the login page, anything else **401**; login disabled → 200 as `admin`. `?need=viewer\|operator\|admin` answers **403** to a lower role — for a service that cannot read roles itself (ESPHome Device Builder under `/esphome` uses `need=admin`); an unknown value is **400** | — |
| POST | `/api/auth/login` | Log in (`{username, password}`); sets the session cookie; per-IP lockout | — |
| POST | `/api/auth/logout` | Invalidate the session, clear the cookie | viewer |
| POST | `/api/auth/passkey/register/begin` | Start WebAuthn enrollment for the logged-in account (needs a hostname, not an IP) | operator |
| POST | `/api/auth/passkey/register/finish` | Verify and store the new passkey | operator |
| GET | `/api/auth/passkeys` | List passkeys (admin sees all users', others their own) | viewer |
| DELETE | `/api/auth/passkeys/{cred_id}` | Delete a passkey (own; admin: any) | operator |
| POST | `/api/auth/passkey/login/begin` | Start a passkey login ceremony (shares the password lockout) | — |
| POST | `/api/auth/passkey/login/finish` | Verify the assertion; sets the session cookie | — |
| GET | `/api/auth/service-tokens` | The service tokens: name, role, made, last used (never the token) | admin |
| POST | `/api/auth/service-tokens` | Make one: `{name, role}` → `{name, token}`; the only time the token is shown. A token cannot call this | admin |
| DELETE | `/api/auth/service-tokens/{name}` | Revoke one: the program using it is refused at once | admin |

## Devices found over MQTT ([mqtt-discovery.md](mqtt-discovery.md))

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/mqtt-discovery` | Devices announcing themselves on the gateway's broker, each `new` / `template_missing` / `in_mbg` / `ignored` | viewer |
| POST | `/api/mqtt-discovery/{key}/adopt` | Make it a device, off and write-locked, with its template and topics (`{id?, name?}`) | admin |
| POST | `/api/mqtt-discovery/{key}/ignore` | `{ignore: true\|false}` | admin |
| POST | `/api/mqtt-discovery/enabled` | `{enabled}`, from the next start | admin |

## Devices (southbound sources)

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/devices` | All devices with live health (device #1 first). Each carries `kind` — what kind of equipment it is: meter, inverter, battery, site (a system controller: a Victron GX, a Fronius site API — not the attribute *level* `site`), heat_pump, drive, ups, charger, sensor, converter, other — and `kind_source` (override, role, component, template, names, none). It also carries `type` and `type_label` (the device's type from the [device-type catalog](#device-types): a built-in or a user type, whose `kind` is its `behaves_like`) and `labels` when its identity attribute has some (left out when empty). Optional filters, each a comma list of at most 20 values: `q` (up to 200 characters; every word must appear in the id, name, template — every source's too —, vendor, model, address or installation), `kind` (aliases such as `system` accepted), `type` (a built-in or a user type), `label` (any of the identity labels), `state` (online, offline, disabled), `protocol`, `installation`. `protocol` takes one vocabulary on both lists: tcp, udp, rtu, ascii, rtu-tcp, ascii-tcp, rtu_tap, http, mqtt, or `modbus` for any of the Modbus ones; a unit read through sources matches its sources' protocols. `sort` = name, id, kind, state or protocol, one leading `-` for descending; ties stay in name order. Without parameters the list is unchanged. A wrong value is a 422 with `fields` | viewer |
| GET | `/api/devices/{id}` | One device, as in the list (with `connection_spec`, `summary` and `locks`, below). Answers an `ETag` header: a hash of the stored entry, stable across a reload that changes nothing | viewer |
| PATCH | `/api/devices/{id}/organise` | `{"type": "ups"}` (or `{"kind": …}`, the older word) files the device under a type of your choice — a built-in kind or a user type; `{"type": null}` derives it again. The type is stored under the device's `kind:` key; the answer carries `kind` (the wire kind), `kind_source`, `type` and `type_label`. Never restarts the device; an ordinary edit keeps it. Not for the primary or an installation's unit (their kind comes from the template / the group's role). A `type` (or `kind`) sent with `POST /api/devices` is filed the same way; `POST /api/devices` also takes `attributes` (`{category: value}`, e.g. `identity`), checked with the device: refused (422), nothing is created. A stored value the gateway cannot read is ignored at load (event log) and cleared with `null`. `?dry_run=true` checks only | admin |
| POST | `/api/devices[?dry_run=true[&only=connection]]` | Create a device: validate → persist → auto-select template registers → ensure Influx bucket → hot-start poller + HA discovery. **`?dry_run=true`** stores nothing and answers `{status: valid, would: create, entry}` — `entry` is the would-be device: `id, name, template, enabled, type, protocol, wire` (the connection spec), `topic_prefix`, `bucket`, `device_tag`, `ha_node` (resolved), `write_locked: true`, `opcua_enabled`, `setup` (`pending` when its template has setup writes, else `none`), `bus` (`{key, where, role: master\|listen, pollers, listeners}` — the bus it joins and the enabled devices already on it; `null` off a bus) and `components`. **`&only=connection`** judges the connection alone — its fields, the matrix and edit locks, the bus claims, the Seplos mode rule, the advanced keys — with no id, name or template (a `template` sent is used: a map written for listening decides the role); `{status: valid, would: create, only: connection, bus}` or 422 naming only the connection's fields, with `bus` beside them; the bus claim is judged whatever else is refused, and `bus` is `"unknown"` when the connection cannot be told. No probe device is ever made. **`check: ok|failed|skipped`** and **`check_job`** (the preview job's id) say how Add's Check ended: written into the create's `device created` audit line (`"not said"` without it), never stored on the device; any other word is a 422 at `check`. **`components: [{type, instance, name?, id?, enabled?}]`** are created with the device, checked as `POST /api/devices/{id}/components` checks one (keyed `components[i].…`), all or nothing; the answer lists them under `components`. **Advanced connection keys** are checked when a save sets them to a new value (a value kept as stored is not judged again): `retry_attempts` 0..10, `retry_delay` 0..60 s, `stale_after_s` 0..86400, `max_gap` 0..125, `illegal_registers` (addresses 0..65535, decimal or `0x…`, up to 1000), `drop_all_zero`/`serialize_endpoint`/`share_transport` booleans, `startup_jitter_s` 0..3600 (empty = the gateway's), `max_connections` 1..8, `endpoint_wait_s` 0.1..600, `endpoint_min_gap_s` 0..60, `connect_settle_s` 0..30 (empty = the template's) — 422 at `connection.<key>` (`multibus/conn_advanced.py` holds the ranges and defaults). A new device is stored **write-locked** (`write_locked: true`) whatever the payload says; an admin unlocks it with `/write-lock`. A template that only listens (`protocol.transports: ["rtu_tap"]` or `protocol.listen_only`) is refused on any protocol but `rtu_tap` (422 at `connection.protocol`). A connection that would poll a bus an enabled device only listens to, or listen on a bus another enabled device polls, is refused at its field (never two masters on a bus, never a poll on a listened bus); the same holds for `POST/PUT /api/endpoints` (connection, groups, sources). A device measured only by meters (a UPS with no communication of its own): `{id, name, kind, connection: {protocol: "none"}, meters: [...], options?}` — no template, no address; `kind` and at least one meter required (422 keyed `kind`, `meters`, `meters[i].…`; `options` only with an input and an output meter); it reads and writes nothing and is online when its meters are. Its view shows `connection: {protocol: "none"}` and `measured_by`; `PUT /api/devices/{id}` takes that shape back; a device's way of being read (meter-only or a connection) is fixed after creation (422 at `connection.protocol`); `POST /api/devices/{id}/test` answers "nothing to test — measured by …". `GET /api/protocols` lists it under `meter_only` | admin |
| PUT | `/api/devices/{id}[?dry_run=true]` | Update a device (stop poller, persist, restart). Primary editable too; routing identity (topic prefix / bucket / tag) stays fixed. The edit locks (one place on the server): a device keeps its protocol class (Modbus, Seplos V2, CAN, HTTP/JSON, MQTT — reach and framing may change within it), the primary stays Modbus TCP, a device holding components keeps its template and stays MQTT, meter-only is fixed after creation, a template's family must match; each refusal is a 422 with `fields`. `If-Match: <etag>` (from `GET /api/devices/{id}`): a stale one answers **412** `{errors, current, etag}` with the stored entry, nothing written; no header = as before. `dry_run` runs every check a save runs and stores nothing | admin |
| PATCH | `/api/devices/{id}[?dry_run=true][&intent=replace]` | Edit a device in **sections**: `{sections: {connection?, polling?: {enabled, poll_groups}, identity?: {name, type}, outputs?: {mqtt, influxdb, ha_discovery, http_output, opcua, rest_push}, model?: {template}}}`. `connection` is merged with the stored one as a `PUT` merges it; `outputs.mqtt` / `outputs.influxdb` are `true`/`false` or `{enabled, publish_mode, heartbeat_interval[, write_interval]}`, `http_output` a boolean, `opcua` `{enabled, fields}`, `rest_push` the `/rest-push` body; `identity.type` follows `/organise`; `polling.poll_groups` the `/poll-groups` body. Each section is judged by the validator its own route uses (422 with `fields`, keyed `<section>.<field>`). **`If-Match` is required**: none → **428**, stale → 412. One config write (the poll intervals, when sent, go first into the device's register file as `/poll-groups` writes them; when that file cannot be written, 422/503 and nothing is stored), one audit record, one restart decision: acquisition restarts only when what it runs on changes (connection, template, enabled); the name, the type, the outputs and the poll intervals apply live; a device that should run and does not reconnects when the PATCH carries its connection, as with `PUT`. Answers `{status: updated\|unchanged, restarted, changes, device}`. `?dry_run=true` (If-Match not needed) stores nothing and answers `{status: valid, would: unchanged\|update\|update_restart, changes: [{section, field, from, to}] (secrets masked), restart_required, effects: [text], locks}` — `locks` the same list `GET /api/devices/{id}` answers (`[{field, code, text}]`). `?intent=replace` (Replace hardware) also appends an energy-counter reset marker `{at, ts, reason: "replace"}` to the device's `energy_counter_resets` (kept by every later save; the device view shows it when there is one). Not a section: the write lock (`/write-lock`), the id, components. 422 for a component and an installation's unit. **The primary meter** takes the same body through an adapter onto its flat config: `connection` → `modbus:` (`protocol` Modbus TCP only, `host`, `port`, `unit_id`, `timeout`, `retry_attempts`, `retry_delay` — the checks of its `PUT` and of `POST /api/config/modbus`; whole numbers stored as int, delays as float), `outputs.http_output` / `opcua` / `rest_push` → its own sections (held in the form the file keeps: a disabled push without a URL, OPC UA on with every value are no change), `polling.poll_groups` → its register file; `identity.name`, `identity.type`, `model.template`, `polling.enabled`, `outputs.mqtt` / `outputs.influxdb` and `outputs.ha_discovery` (the gateway's own switch: Settings → MQTT; `PUT`'s `ha_discovery_enabled` still maps it) set to another value, and any other connection key not said as GET serves it, are refused per field in the words of its `primary` locks (the same value said again is no change); a rebuild that fails after the save answers 503 `{errors, partial: {saved, failed: [apply]}}` (restart the gateway); `?intent=replace` is refused. One `config.yaml` write; the meter reconnects only when its connection changes. A PATCH that says what a `PUT` says writes the same file. What each section takes for a device is its `edit_sections` (below) | admin |
| DELETE | `/api/devices/{id}[?dry_run=true]` | Delete a non-primary device (register file kept on disk; clears retained HA discovery). **Refused (409) while anything uses it** — see *What uses it* below; disable it instead (`PUT … {"enabled": false}`), every reference stays. `dry_run` runs the same checks and answers `{status: "valid", would: "delete", dependents}`. `If-Match` as for PUT (412 with the current entry). Answers `{status: "deleted", restorable, deleted_ts, restore_resets, restore_resumes}`: `restorable: false` (and `deleted_ts: null`) when its settings could not be kept — it cannot be restored; `deleted_ts` names this delete for an Undo; `restore_resets` is what a restore (the page's Undo) would set back to safe instead of bringing back as it was — `{write_locked: true}` when it was unlocked, `{commands: [names]}` for the bindings that were on (`#i` for one without a name), `{components: [ids]}` for its components that were unlocked; `{}` when a restore brings it back exactly; `restore_resumes`: what starts again with it, `[{what: "rest_push", device, host}]` (host only) | admin |
| GET | `/api/devices/{id}/dependents` | What uses this device (`blocking`) and what goes with it (`follows`) — what a delete would be told | viewer |
| GET | `/api/dependents` | `{counts: {<id>: n}}` — how many things use each device and installation, in one pass (a list's *used by N*) | viewer |
| GET | `/api/devices/{id}/components` | The kinds of component the device's template declares and the ones it holds (a GX's batteries and sensors) | viewer |
| POST | `/api/devices/{id}/components` | Add one: `{type, instance, name?, id?, enabled?}`. It reads through the device's connection and starts at once | admin |
| PATCH | `/api/devices/{id}/components/{cid}` | `{name?, enabled?}` — rename it or switch it on/off | admin |
| DELETE | `/api/devices/{id}/components/{cid}[?dry_run=true]` | Remove it; its measurement selection stays on disk, so adding the same instance again resumes it. Refused (409) while anything uses it | admin |
| POST | `/api/devices/{id}/components/discover` | Ask the device what it holds (a read, nothing written): `[{type, instance, product, name, added, device_id}]` | admin |
| POST | `/api/devices/components/discover` | The same **before saving**: `{template, connection (MQTT), seconds?}` — a short-lived connection of its own to the broker, subscribe only, for `seconds` (1..10, default 4); nothing is published (a GX nobody keeps alive says nothing: `found` is then empty). `{template, found: [{type, instance, product, name}]}`; 422 for a template that does not say how to find its components or a connection that is not MQTT; 409 when the broker cannot be reached within 5 s — the same answer after the same wait whatever the port did; admin only (403 otherwise); two at a time (429 with `Retry-After` beyond) | admin |
| GET | `/api/devices/restorable[?check=true]` | Deleted devices whose settings were kept (id no longer active). `?check=true`: each also says `problems` — what a restore would refuse, in the restore's own words (`[]`: it can come back; e.g. another device now claims its bus; kept settings that cannot be read are said in their own row, the list stands) — `resets` and `resumes` (as the delete's). Nothing is restored by asking; the checks run outside the device lock | viewer |
| POST | `/api/devices/{id}/restore[?deleted_ts=]` | Rebuild a deleted device from its kept tombstone: its entry, its measurement selection, its attributes (kept on delete), its components, its Home Assistant node; its REST push starts again (`resumed`, as the delete's `restore_resumes`) and its attributes `…/meta` is published again. It comes back write-locked, its components write-locked, with every command binding `enabled: false`; `reset` says which of those changed it (`{write_locked, commands, components}`, `{}` when nothing did). `deleted_ts` (an Undo): the delete it undoes — another delete's kept settings answer 409 `code: "tombstone_changed"`. 409 `code: "exists"` when the id is active again (already back), 422 when a check refuses it or the kept settings cannot be read (as `?check=true` says) | admin |
| DELETE | `/api/devices/restorable/{id}` | Permanently forget a deleted device's kept settings | admin |
| GET | `/api/protocols` | What a connection may be: the protocol lists per editor (`device`, `source`, `endpoint`, `group`, `meter_only`, `serial`, `network`), `matrix` (the valid combinations), `labels`, `invalid` (the combinations no device may have, with their reason) and `defaults` (every advanced connection key with its effective default and its source) — see *Connection spec* below | viewer |
| GET | `/api/serial-ports[?all=1]` | The serial ports of this host, read from `/sys/class/tty` (the same enumeration as the serial bridge's). `{ports: [...], hidden: n}`; each port: `name`, `path` (`/dev/<name>`), `stable_path` (the path this container has the port under: a USB adapter's `/dev/serial/by-id` link when visible, else its own node, else an alias; `path` when none; `""` when `/dev/<name>` in this container is another device), `path_taken` (that case: a renamed mapping; the fix line then maps the port under a non-colliding path), `by_id`, `aliases` (other `/dev` nodes with its device number: a udev alias, a renamed mapping), `in_dev`, `kind` (`usb` \| `hat` \| `board_uart` \| `other`), `vendor_id`, `product_id`, `serial`, `manufacturer`, `model`, `port_path`, `channel`, `driver`, `chip`, `label`, `dev_num`, `rs485` (the device tree's `linux,rs485-enabled-at-boot-time`; `null` when unknown), `bluetooth`, `console`, `mapped` (some node of this container's `/dev` is this device, matched by `major:minor` with `stat`), `state` (`free` \| `polled` \| `listened` \| `busy` \| `not_mapped` \| `unknown` \| `console`) with `reason`, `base_state` (the same without any device's claim), `polled_by` / `listened_by` (enabled device ids, from the bus claims; a by-id link or an alias is the same port), `processes` (the programs that hold it open, by name; never the gateway itself), `fix` (`not_mapped`: `{docker_run, compose}` lines to add, plus `note` for USB: the ttyUSB name can change on a replug). The keys answered before (`dev`, `vendor_id`, `product_id`, `serial`, `model`) stay. Console ports are left out (`hidden` counts them) unless `?all=1` (or `true`). A failure answers `{ports: [], error}` with a generic message (details in the log). Read-only: sysfs, `/dev` and `/proc`; no port is opened | viewer |
| GET | `/api/buses` | Every bus the gateway knows, one read model each: `{buses: [{id, kind, where, path, host, port, bridge, protocols, framing, devices, pollers, listeners, units_in_use, role, role_conflict, line, line_conflict, lines, load}]}`. `id` is the bus claims' key (`chr:major:minor` or the real path of a serial line, the lower-cased `host:port`, `can:<interface>`); `kind` `serial` (a port here), `bridge` (a declared port of a bridge record; a converter reached at the same host:port is the same bus), `address` (any other host:port) or `can`. `devices`: every device on it, enabled or not (`{id, name, enabled, installation, component_of, unit, protocol, role: master\|listen-only, line}`; `installation` / `component_of` name who sets its line when not itself); `pollers` / `listeners`: the enabled ones that transmit / only listen; `units_in_use`: `[{unit, device}]` sorted, every device counted (a save refuses a unit taken by a disabled one too); `role`: `listen-only` when anyone listens, `master` when someone polls, else null; `role_conflict`: true when the bus is both polled and listened to (a save refuses it now; a stored config may hold it). `line` (`{baud, parity, bytesize, stopbits}`) only for a serial bus whose devices agree; when they do not, `line: null`, `line_conflict: true` and `lines: [{line, devices}]`. `framing`: the bridge type's (`rtu`, `ascii`, `mbap`) or the devices' protocol's. `load`: the measured wire time of the last minute as on the bridge cards (`{pct, level: ok\|high\|full, over_capacity, busy_s, window_s, missed_turns, devices, capacity_left_pct}`; `devices` counts every device that polls the bus, enabled or not, the bridge cards' rule), null before anything was measured. A device that cannot be read out is skipped and logged, never fails the list. `bridge`: `{id, name, type, framing, port, client}` — `client` is the address the MBG serial bridge reports as connected to that port (null when it says nothing). Read only | viewer |
| GET | `/api/buses/{bus}/line` | The line a bus has now, for Edit bus line…: `{bus, kind, line: {baudrate, parity, bytesize, stopbits}\|null, source: devices\|bridge\|unknown}` — its devices' line when they agree, the MBG serial bridge port's (asked from the bridge), else `unknown` (never a guess) | viewer |
| PATCH | `/api/buses/{bus}/line` | **Edit bus line…**: one line for every device on a serial bus, `{baudrate, parity, bytesize, stopbits}` (all four; parity `N`/`E`/`O`, data bits 5–8, stop bits 1–2, baud 300–4000000). `{bus}` is the bus's `id` from `GET /api/buses`, URL-encoded (`%2Fdev%2FttySC1`, `chr%3A4%3A65`, `10.0.0.5%3A7001`). `?dry_run=true` stores nothing and answers `{status: valid\|unchanged, bus, kind, where, line: {from, to}, bridge, devices: [{id, name, from, to, changed, restart}], restart: [ids]}`. Applied: `{…, status: updated\|unchanged, restarted: [ids], restart_failed: [ids]}` (a restart that fails after the write is named there; the line is stored). On our bridge the port's current line is asked first (never under a lock): already that line → `unchanged`, nothing restarts. Units of one installation share one entry lock; a device that joined the bus between the check and the locks → `409`. Snapshotted like a device change. A serial port of this host: every device's entry on it gets the line in **one config write** (all or nothing), then they restart. A port of the MBG serial bridge (`serial_config: api`): the bridge port's line is set through the bridge's `POST /adapters/{key}/serial` in the same operation (`{baud, parity, databits, stopbits}`; the adapter recorded for the port, else the one serving it now), then its devices restart; the bridge unreachable or refusing → `502`/its 4xx and nothing restarts. Refused `422`: a converter, a transparent bridge or a Modbus TCP gateway (*the line is set on the box*), a bus with no serial line (Ethernet, CAN), a unit of an installation (set on the installation's connection), a component. `409` while a write to one of its devices is in flight (each device's entry lock is tried, never waited for); nothing changed. `404` for an unknown bus. One audit record. No `If-Match`: the devices' ETags change | admin |
| GET | `/api/can-interfaces` | The CAN interfaces of this host: `{interfaces: [{name, kind, up, operstate, bitrate, listen_only, state, devices}]}`, from sysfs (`/sys/class/net/*/type` = 280) and rtnetlink (`kind` `can`/`vcan`; `bitrate`, `listen_only` and `state` — `error-active`, `error-warning`, `error-passive`, `bus-off`, `stopped`, `sleeping` — null for a vcan or when the kernel does not say). `devices`: the CAN devices reading it. Nothing is opened on the bus. Read only | viewer |
| GET | `/api/bridge/adapters` | Live USB-adapter inventory from the serial bridge (RTU-over-network Scan); `available:false` with an `error` when the bridge is unreachable | viewer |
| GET | `/api/devices/{id}/events` | This device's acquisition log, newest first: unreachable / recovered / forced reopen / bus busy / failed batch (with the address and register count) / poll-group overrun episodes. `?limit=1..500`, `?level=error,warn` to narrow to the problems. Returns the live per-group state (interval, last sweep, reads, overruns, age) and the read counters alongside, because "what happened" and "what it is doing now" are the same question when an endpoint is struggling. 404 when the device is not running | viewer |
| POST | `/api/devices/test` | Ad-hoc connection probe for a not-yet-saved device (tcp, udp, rtu, ascii, rtu-tcp, ascii-tcp, http, mqtt). A bus a running device already reads (a serial port, a bridge's port) is asked through that device's connection. `rtu_tap` (or a template that only listens) answers `ok: null` — a tap never transmits, there is nothing to ask. On a bus a device only listens to (an enabled `rtu_tap`, Seplos V2 in `listen` mode, or a device whose template only listens) it answers **409** `{errors, listeners, bus}` and sends nothing | operator |
| POST | `/api/devices/{id}/test` | Probe a saved device (uses its first selected register address). For an `rtu_tap` device: what the tap has HEARD, naming the silent layer — port not open (`open_error`), port open but no valid frame, bus live but nothing for this unit, or `hearing unit N — W windows, last X s ago`. A polled device whose bus a device only listens to: 409, as above | operator |
| POST | `/api/previews` | **The read preview** (Add Device → **Check**): `{connection, template, unit?, device_id?, seconds?}` → **202** `{job, state, mode: poll\|listen, transport: shared\|own\|tap, where, template, unit, shared_with, seconds, budget: {max_requests, listen_s}, warnings}`; the reading runs in the background. **Reads only**: every Modbus request goes through a guard that refuses any function code but 3/4 (FC43/14 only when the template's `identify` asks for it), a read longer than the template's `max_registers_per_read`, and any request past `preview.max_requests` (`GET /api/config/preview`); no setup write, no keepalive write to the device. Seplos V2 asks its read commands only; a tap (`rtu_tap`, a template that only listens), a Seplos bus in `mode: listen` and CAN only receive (a tap joins the bus's running reader, or opens a receive-only one for the preview); MQTT subscribes, sends the template's *broker* keepalive once as the running device would (said in `warnings`) and reads the component discover topics in the same window. `seconds`: 1..15, at most `preview.listen_s` (its default). **A bus a running device of this gateway polls** is read through that device's own running connection (`transport: shared`, `shared_with`: its id): its socket, lock, lane and turn, `endpoint_min_gap_s` kept, the line handed back between two of the preview's requests; its line and timeout are used and said (`shared_line: {baudrate, parity, stopbits, bytesize, timeout}`), and a request that names another line or timeout is a 422 with `holder` and `line: {<key>: the holder's value}` (a form takes them and asks again). **One silent unit costs one request**: the first request without an answer stops the job ("no answer from unit N — check the address"); a preview's timeout never counts towards the client's own close-after-N-silences, and nothing is sent on a client already missing answers. A gateway that goes standby stops its previews; every job has a deadline, and a wall time past which the job ends `failed` even when a request is stuck (its worker is abandoned, its result ignored, its bus and its slot freed; a device's lock is never touched). A bus a device of this gateway holds but has no live connection on right now (starting, reconnecting) is a 409 *"busy starting <device> — retry in a moment"* (Modbus and Seplos). **`confirm_open: true`**: open a connection of the preview's own although nobody can say whether another program holds that port — a serial bus over TCP (`rtu-tcp`, `ascii-tcp`, a tap or a Seplos bus through a bridge), plain `tcp` to a port other than 502 (it may be a raw serial server that serves one client), a declared bridge port when our bridge was not asked, its answer is older than 90 s, its diagnostics failed, or the converter cannot say, or a local serial port another process has open; audited. Our bridge's word is a sample at most **90 s old**: *absent* may still be a client that connected within those 90 s. **409**: a bus any device listens to, **enabled or not** (the same body as a Test's: `{errors, listeners, bus}`, an alias of the line included); a bus read in another protocol; a holder with `share_transport: false` (`holder`); another program connected to that bridge port (`client`: its address — never overridden); `client: "unknown"` without `confirm_open` (`unknown`: why); a CAN interface not in listen-only mode (asked again once the socket is open: a job that finds it changed fails, nothing read); a preview of that bus already running (`job`). **429** with `Retry-After`: 4 previews already running. **422**: the connection as a save judges it (the `only=connection` dry run), a template not found, `seconds` out of range, a Seplos `mode` not said, unit 0 (the broadcast address) on a serial bus, an MQTT keepalive topic that a command writes to (`W/…`). An HTTP preview counts every GET (the test read's retry too) in the budget. Every refusal leaves a `preview` audit line. `device_id`: the stored connection is merged under the one sent, as a save merges it (a blank password stays the stored one); the connection is then judged as a save of **that** device (its own address is no clash with itself, its edit locks apply) | operator |
| GET | `/api/previews/{job}` | The preview's answer: `{state: running\|done\|cancelled\|failed, mode, transport, where, template, unit, values: [{name, label, value, unit, quality, group, address+register_type \| path \| can_id \| command, note?}], groups: {total, asked, answered, heard}, identity: {template, fingerprint: match\|mismatch\|unknown\|none, suggested, suggested_name, fc43, sunspec: {manufacturer, model, version, serial}}, components: [{type, instance, product, name}], budget: {requests, registers, max_requests, ms}, warnings, load: {share_now, share_added, basis}, error?}`. `quality`: `ok`, `no_answer` (not answered, not asked within the budget, not heard), `illegal_address` (Modbus exception 2), `out_of_range` (a declared `min`/`max`, a unit's physical bounds — °C −60..250 — or \|v\| > 1e9), `byte_swap_suspect` (NaN/Inf, or a 32-bit value that reads sane in the other word order), `sentinel` (the row's declared `nan`: **no value**, never e.g. −100 °C). The identity is read first (the template's `identify` registers, the SunSpec common model); a mismatch reads the other templates' fingerprints within the budget and names the one that matches. `load.share_added`: the share of the bus the template's poll groups would take (11 bits per byte at the line's baud rate, 3.5 characters each way; the measured time per transaction on a TCP endpoint); `share_now` as `/api/buses` says it. Finished answers are kept 10 minutes; 404 after | viewer |
| DELETE | `/api/previews/{job}` | Stop it at its next request or listening step: `{job, cancelled}` (`false` when it had finished) | operator |
| POST | `/api/devices/{id}/preview` | The device page's preview of its **edited** connection, before Save: `{connection?, template?, unit?, seconds?}` — the connection sent is merged over the stored one; with the stored template, the device's own selection is read. The connection is judged as a save of **that** device judges it: its own address is not a clash with itself (another device's still is), its edit locks apply. Nothing is stored; the same rules and answers as `POST /api/previews`. A runtime action (never 412, the `ETag` said, `MBG-If-Match: ignored`) | operator |
| POST | `/api/bus-scan` | **The bus scan** (Add Device → **Many of the same on one bus**): `{bus \| connection, mode?: poll\|listen, from?, to?, timeout?, fc43?, seconds?, confirm_open?}` → **202** `{job, state, mode, transport: shared\|own\|tap, where, bus, shared_with, shared_line?, confirm_open?, total, from, to, timeout, fc43 \| seconds, warnings}`. `bus`: an id of `GET /api/buses` (its port and line, a bridge port, an address); else `connection` as a save takes it (`unit_id` ignored). **`mode: poll` (Scan this bus)**: asks each unit `from`..`to` (default **1–32**, at most 1–247; 0 is refused) one at a time — `timeout` 0.2..3 s per question (default 0.5), put on the client for that one question and put back. **Reads only**, through the read preview's own guard and turns (FC3/FC4; FC43/14 when `fc43`, default true): an answer (data or a Modbus exception) proves a unit; then its identity — FC43 vendor/product/version, the SunSpec marker at 40000, each template's `identify` fingerprint — within 8 requests per unit. **One socket**: on a bus a running device of this gateway polls, through that device's running connection (`transport: shared`; its line used, a request naming another line is a 422; the wait is the larger of `timeout` and the device's own, never shorter); after every request (an identify's included) the line goes back to the devices, and a cancel or a standby stops it before the next; after a silent request the receive side is drained under the lock before the turn is handed back; an address a device of this gateway has on that bus is listed (`answer: known`, `asked: false`) and never asked; otherwise a connection of its own, with the preview's rules (`confirm_open`, a foreign client of our bridge, a tty another process holds). A silent unit costs one question; nothing is sent on a client already missing answers; a standby stops it. **`mode: listen` (Listen for addresses)**: joins the bus's tap reader or Seplos V2 listener (or opens a receive-only one) for `seconds` (1..15, at most `preview.listen_s`) and lists every unit the frames name — **never transmits**; RTU frames (a tap) or Seplos V2; not Modbus TCP. **409**: a scan on a bus any device listens to, **enabled or not**, whatever name reaches it (`{errors, listeners, bus, hint}` — listen there instead); a listen on a bus a device of this gateway polls (`pollers` — scan there instead); a foreign client / `client: "unknown"` without `confirm_open` (as a preview); a preview or a scan of that bus already running (`job`, one job per bus, previews and scans alike); a scan of another bus running (one scan at a time over every bus; previews keep their cap). **Slower polling**: on a bus a device of this gateway polls, the 202 answer carries `slow: {devices, reads_per_cycle, per_read_s, worst_per_cycle_s, rules}` and a warning (each of their reads may wait up to the scan's wait + 0.3 s); when an **armed rule** reads or drives a device on that bus (or the rules cannot be read) the scan is a **409** naming `rules` and `slow` unless `confirm_slow: true` (audited). **503**: the bus list (which addresses are in use) cannot be read — never a scan with nothing known. **429**: 4 previews or scans already running. **422**: the range, the timeout, `fc43`, a Seplos bus to scan (listen instead), CAN/HTTP/MQTT, the connection as a save judges it. **404**: an unknown `bus`. Every refusal and every finished scan leaves a `bus-scan` audit line (`{mode, transport, shared_with, confirm_open, range \| seconds, found, requests}`) | operator |
| GET | `/api/bus-scan/{job}` | Progress and answer: `{job, state: running\|cancelling\|done\|cancelled\|failed, mode, transport, where, bus, shared_with, done, total, found, budget: {requests, registers, max_requests, ms}, warnings, error}`. Scan: `found: [{unit_id, answer: data\|exception, fc43: {vendor, product, version}, sunspec, matches: [{id, name}], suggested, suggested_name, device, requests}]` (`device`: the device of this gateway that already has that address). Listen: `found: [{unit_id, asked, answered, exceptions, writes, fcs, pattern: ["FC3 @0 x2" \| "0x42", …], every_s, device}]`, `heard: {frames, units, writes_seen, cycle_s}`. Kept 10 minutes; 404 after (a preview's id is not a scan) | viewer |
| DELETE | `/api/bus-scan/{job}` | Stop it at its next unit (a listen at its next step): `{job, cancelled}` | operator |
| GET | `/api/devices/{id}/poll-groups` | Current poll-group intervals | viewer |
| POST | `/api/devices/{id}/poll-groups` | Update intervals (0.05–86400 s) and live-restart that device's pollers | admin |
| POST | `/api/devices/{id}/http-output` | Toggle the read-only JSON feed (`/api/meters/{id}`) | admin |
| POST | `/api/devices/{id}/outputs` | `{mqtt?, influxdb?, ha_discovery?}` (booleans) — switch the MQTT / InfluxDB output or Home Assistant discovery at once, no poller restart. A `devices[]` entry only: 422 for the primary and an endpoint's units (set on the installation) | admin |
| POST | `/api/devices/{id}/publishing` | `{mqtt?: {publish_mode, heartbeat_interval}, influxdb?: {publish_mode, write_interval, heartbeat_interval}}` — the device's own publishing (`changed`/`all`, seconds; `influxdb.heartbeat_interval` whole seconds 0..86400); `null` = back to Settings, a key left out stays; applies at the next reading. An endpoint unit sets its group; 422 for the primary (it follows Settings). A device's `publishing` says `own`, `settings` and `effective` | admin |
| POST | `/api/devices/{id}/opcua-output` | `{enabled, fields?}` — serve the device on the OPC UA server, every value or only `fields`; an endpoint unit sets its group ([opcua.md](opcua.md#which-devices-and-values-are-served)) — every unit of the group says it at once in `GET /api/devices` (as `/publishing` for a unit's group, `/http-output` and `/rest-push` for its installation: the sibling units are refreshed, a REST push started for each) | admin |
| POST | `/api/devices/{id}/rest-push` | Configure the periodic REST push sink (`{enabled,url,interval_s≥5,headers,format:native\|flat,verify_tls,timeout}`); header values masked on read, preserved on save | admin |
| POST | `/api/devices/{id}/rest-push/test` | Push once now and report the result | operator |
| POST | `/api/devices/{id}/payload-sample` | Fetch one full payload from a saved MQTT/HTTP device (for the `json_path` picker) | operator |
| POST | `/api/devices/{id}/refresh-from-template[?add_new=true]` | Update from template: carry the template's `label`/`unit`/`description`/`category`/`aggregates` (registers) and `label`/`unit` (template calculated fields) over to the rows the device already has, matched by name; selection, UI flags, sinks, thresholds and formulas are kept, no row is added unless `add_new=true`, which also adds the template's calculated fields the device lacks (counted in `added`); the device restarts so the pipeline sees the new metadata. Answers `{status, device, registers, calculated, files}` (rows changed). 422 for a unit of an installation (refresh the installation) and for the primary | admin |

### Connection spec, the one validator, lost updates

**Connection spec (read only).** A stored `connection.protocol` packs the
port, the reach, the protocol and the mode into one string. Every device entry
reads it out as `connection_spec`:

| Axis | Values |
|---|---|
| `port` | `serial` · `ethernet` · `can` · `http` · `mqtt` (null: a meter-only device, not a connection) |
| `reach` | `here` (a port on this host) · `bridge` (a bridge record) · `address` (host:port) · `socketcan` (CAN's carrier); null for HTTP/MQTT |
| `protocol` | `modbus` · `seplos_v2` · `can_signals` · `json_http` · `json_mqtt` |
| `framing` | `rtu` · `ascii` · `mbap` (Modbus only; a bridge's type says it) |
| `udp` | Modbus TCP over UDP |
| `mode` | `poll` · `listen` — the configured mode, not the live HA role. A Seplos V2 bus without a stored mode is `poll` |
| `writable` | derived: Modbus polled, or MQTT (commands) |
| `dangling` | the connection names a bridge that does not exist (the other axes are the stored keys) |

It is output only: `POST`/`PUT /api/devices` never take one, and a
`connection_spec` (or `connection.spec`, `source_specs`) sent back is dropped
before anything reads the payload — it never reaches config.yaml, and an echo
of a whole GET entry is still `unchanged`.

**`GET /api/protocols`** keeps every key it had (`device`, `source`,
`endpoint`, `group`, `meter_only`, `serial`, `network`) and adds `matrix` —
per context (`device`, `source`, `endpoint`, `group`) the valid cells, each
`{port, reach, protocol, framing, udp, mode, writable, status, connection, note?}`
where `status` is `today` (accepted now), `new` or `not_yet` (shown, refused
by the API: the matrix never widens what a save accepts) and `connection` is
the legacy stub the cell is saved as (`{"protocol": "rtu-tcp"}`, `{}` for a
polled bridge, `{"protocol": "seplos_v2", "mode": "listen"}`) — and `labels`,
the words for each axis value. The role axis (`mode`) reads **Master**
(`poll`: the gateway polls) and **Listen-only** (`listen`: another master
polls, the gateway is a passive monitor); the stored values are unchanged.

It also adds:
- `invalid` — per context (`device`) the combinations no device may have,
  each `{port, reach, protocol, framing, udp, mode, code, text}`: a form keeps
  the option visible (`aria-disabled`) with `text` as its reason. Codes:
  `listen_ascii` (listening decodes RTU), `listen_mbap` (a Modbus TCP server
  has no traffic to hear), `listen_gateway_bridge` (a Modbus TCP gateway
  bridge does not pass the bus's traffic on), `can_poll` (CAN is
  listen-only). It is disjoint from `matrix`: Seplos V2 through a bridge
  record and CAN over a bridge stay `not_yet` cells there, with their `note`.
- `defaults` — per protocol axis (`modbus`, `seplos_v2`, `can_signals`,
  `json_http`, `json_mqtt`) every advanced connection key with
  `{value, source, note?}`; `source` is `code` (the gateway's built-in
  default), `gateway` (its own setting, e.g. `modbus.startup_jitter_s`),
  `template` (the template's `protocol` decides; `value` null) or `none`
  (never defaulted: a Seplos V2 `mode` is always chosen).

**`summary` and `locks`** sit beside `connection` in every
entry of `GET /api/devices` and `GET /api/devices/{id}`, output only like
`connection_spec` (a save never stores them).
- `summary`: `{port, reach, protocol, framing, udp, role, where, line,
  bridge, unit, address_kind, poll_s, bitrate, endpoint_path, stored_protocol,
  text}`. `role` is `master`, `listen-only` (a tap, a listening Seplos V2, a
  template that only listens, CAN) or null (HTTP, MQTT, measured by its
  meters). `where`: the serial port, `host:port`, the CAN interface, the HTTP
  host or the MQTT broker (`gateway broker` for `@gateway`). `line` (`{baud, parity, bytesize, stopbits}`) for a
  serial port on this host, else null; `bridge` (`{id, port, type, framing,
  host}`) for a device reached through a bridge record. `unit` with
  `address_kind` `unit` or `pack` (Seplos V2). `poll_s`: `[fastest, slowest]`
  interval of the poll groups it reads (null for a listener). `endpoint_path`:
  the HTTP path (no query) or the MQTT topic. `text`: one line, e.g.
  `Modbus RTU · /dev/ttySC1 · 9600 8E1 · unit 1 · Master · 2 s/10 s`,
  `Seplos V2 · /dev/ttySC0 · 9600 8N1 · pack 3 · Listen-only`,
  `Modbus RTU · bridge ws port 4196 · unit 9 · Master`,
  `CAN · can0 · 500 kbit/s · Listen-only`, `Measured by its meters`. Never a
  credential: an HTTP URL gives its host and path only.
- `locks`: `[{field, code, text, params}]`, the fields an edit may not change and
  why. `params`: the values `text` names (`devices`, `where`,
  `line`, `own`, `disagree`, `bridge`, `template`, `installation`, `parent`,
  `device`, `unit`, `component`, `speaks`, `n` — those of its code), so a
  page says the lock in its own language; `text` stays the English. A component or an installation's unit is not edited as a device: its
  first and only lock is `{field: "device", code: "component", parent}` or
  `{field: "device", code: "installation_unit", installation}`, worded as a
  save refuses it (a unit on a serial bus adds `connection.line`
  `line_installation`: its installation sets the line). Otherwise:
  `connection.protocol` (`primary`, `meter_only`, `components`,
  `protocol_fixed`) and `template` (`components`), worded as the save
  refuses them (`connection_spec.edit_errors`); the primary meter also
  `identity.name`, `identity.type`, `template`, `polling.enabled`,
  `outputs.mqtt`, `outputs.influxdb`, `outputs.ha_discovery` (`primary`: what it keeps whatever is
  sent, worded as its PATCH refuses a change); `connection.line`
  (`line_shared` — `set by the bus: meter2 reads it at 9600 8N1. Edit bus
  line… …`, quoting every other device on the serial line, enabled or not,
  with its own line, and saying when the devices disagree; `line_bridge`; `line_remote` —
  a converter's line is set on it); `connection.role` (`template_listens`,
  `can_listen`, `role_bus_listened`, `role_bus_polled` — another enabled
  device on the bus decides). `connection.line` covers `baudrate`,
  `parity`, `bytesize` and `stopbits`; `connection.role` is stored as the
  protocol (`rtu` / `rtu_tap`) or `connection.mode`.
- `edit_sections`: what each `PATCH /api/devices/{id}` section takes for
  this device: `{connection: [keys] | null, polling: [...],
  identity: [...], outputs: [...], model: [...]}`; `null` = any key its
  connection has (a `devices[]` entry); `{}` = a component or an
  installation's unit (edited elsewhere — its `locks` say where). The primary
  meter: `connection` its seven Modbus TCP keys, `polling: ["poll_groups"]`,
  `identity: []`, `outputs: ["http_output", "opcua", "rest_push"]`,
  `model: []`.
- `writable_registers`: how many holdings and coils an operator may write
  through `POST /api/devices/{id}/write`: declared writable,
  not written by a command, not scaled by a live factor; 0 for a device
  written over MQTT (commands only). A raw (`unguarded`) write is the admin's.
- `components_declared`: its template declares components (a GX's batteries).
- A `line_bridge` lock carries `settable` (the bridge takes its line from the
  gateway: Edit bus line… sets it). A device on a serial line that an
  installation's unit or a component is on carries a `connection.line` lock
  `line_bus_installation` (`installation`) or `line_bus_component` (`parent`) — `line_installation` stays a unit's own lock.

`POST /api/devices/{id}/write` refuses what can never be written (HTTP, a
read-only bus) before the write lock: a locked tap answers 400 read-only.

`GET /api/status` carries `writes: {enabled, credential}`: the
master switch `security.allow_writes`, and whether a write can be attributed
(a login is on, or an `API_KEY` is set). A write is refused without both; the
device menus say why before a form is typed.

**One validator.** `POST /api/devices?dry_run=true` and
`PUT /api/devices/{id}?dry_run=true` run every check a save runs — the
fields, the edit locks, the bus claims and the topic prefix — and
store nothing: `{status: "valid", would: create | unchanged | update | update_restart}`,
or the same 422 / 409 a real save answers.

**Topic prefixes.** A prefix being set — a new device's `mqtt.topic_prefix`
(sent or from `mqtt.default_topic_pattern`), a new component's, an
installation's `mqtt.aggregate_prefix` — is refused with a 422 at that field
when it is empty, has an empty level or a leading or trailing `/`, contains
`+`, `#` or NUL, equals another device's, component's or installation's prefix
or the gateway's own `mqtt.topic_prefix`, or is a path prefix of one of them
or has one as its path prefix (`a/b` against `a/b/c`; `a/bc` is not inside
`a/b`). An installation's own units may sit under its root. A prefix is fixed
after creation, so an edit never judges it; stored prefixes that break the
rule are reported once in the log and kept.

**Lost updates.** `GET /api/devices/{id}` answers an
`ETag` — an HMAC of the stored entry, keyed per process (it says "changed or
not", never lets a secret be guessed against it; one held across a restart is
stale). **Every** write that changes a device's stored entry honours
`If-Match`: a value that is not the stored entry's answers **412**
`{errors, current, etag}` and changes nothing; `*` matches any existing
device; no `If-Match` behaves as before. These are `PUT`/`DELETE`
`/api/devices/{id}` and, under it, `write-lock`, `outputs`, `http-output`,
`opcua-output`, `publishing`, `rest-push`, `poll-groups`, `calculated`,
`organise`, `meters`, `energy-meter`, `commands/{name}` (binding, `PUT`), `PATCH /api/devices/{id}` (where it is **required**: 428 without it),
`commands/{name}/enabled`, `channels/{key}`, `components` (add, `PATCH`,
`DELETE`), `refresh-from-template` and `restore`; and `POST
/api/pq/config?device=` (the PQ recorder lives in the entry). A component's
routes take the parent's tag.

Every answer of those routes — a 2xx, a refusal, a 412 — carries `ETag:` (the
tag after the write, or the current one) and `MBG-If-Match: checked`. A client
that sent `If-Match` and got a 2xx may keep that tag for its next write.
Runtime actions store nothing and never answer 412, but say the current tag
with `MBG-If-Match: ignored` (a client must not take it: it may be newer than
what the client has seen): `/write`, running a command (`POST
commands/{name}`, `/dry-run`, `actions/power_limit`), `/test`,
`/payload-sample`, `rest-push/test`, `calculated/test`, `components/discover`.
A route-table test keeps a new device write from skipping the rule.
A checked write holds its stored entry's lock (a component's parent's, a
unit's installation's) around the check, the write and the tag, so two
writes sent with one tag at the same time never both go through: one
answers 412. With `CORS_ALLOW_ORIGINS` set, `ETag` and `MBG-If-Match` are
exposed to the other origin.

A device's **measurement selection** (`/api/registers/selected?device=&source=`)
and its **Energy fields** (`/api/energy/fields?device=`) live in their own files,
not in the entry: their `GET` answers their own `ETag` (a hash of the stored
rows; the Calculated tab's pseudo-rows are not part of it), and their `POST`
honours `If-Match` the same way (412 `{errors, current, etag}`). They never
change the device's tag. The selection's rows carry their template's
category, so editing the device's template (a category, a register) also
changes the selection's tag: a view holding the old one re-reads it. An installation's units are edited on the
installation (`PUT /api/endpoints/{id}`), which has no `If-Match` yet — except a unit's
name: `PATCH /api/endpoints/{id}/units/{unit}` (below) is checked against the unit's
own tag.

**Unknown protocol strings** read as `port: "unknown"` (every other axis
null), never as meter-only; a save with one answers the usual
`connection.protocol: must be one of …`.

### What uses it (deletion protection)

Nothing is deleted while something depends on it. A dependents answer is
`{blocking, follows, warnings}`; each entry is `{type, id, label, where,
ref: {type, id, tab?}, of}` — what kind of thing uses it, the field in it
that names the object, how to open it, and which id it names.
- `blocking` — what would break: a rule that commands or reads it, a
  sequence step, another device's calculated field, a device it measures, a
  virtual meter it feeds, a template formula that names it. A delete answers
  **409** `{errors: [<summary>], dependents}` until each is changed or
  removed.
- `follows` — what goes with it: its components, an installation's units,
  its attribute entries, a saved formula preset that names it.
- `warnings` — what keeps working but should be known.

When a source of references cannot be read (a broken `rules.yaml`, a
calculated-fields file), the answer is **503** — never "nothing uses it".

### Installations — `/api/endpoints` (several units behind one connection)

An installation (an *endpoint* in the API and in `config.yaml`) is an entity, not a grouping: it has its own settings, its own health
and its own output. Its units are ordinary devices, managed THROUGH the endpoint —
device create/edit/delete refuses their ids.

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/devices/{id}/commands` | What this unit can be told: the template's command presets and the unit's own recipes — each with `params` (`min`, `max`, `default`, `unit`, `aliases`), `writes`, `guard`, `verify`, `safe` (+ `safe_from`: `device` / `template`), `lease_s`, `enabled` (bound on the device or its group), `faces`, `confirm`, `channel`, `min_interval_s` (effective) and `template_min_interval_s`, `max_age_s`, `channel_settings` (the effective overrides), `counters` (`superseded`, `expired`, `held`), `mqtt_topic`, `last` result; `orphan_channels` lists overrides whose channel the template no longer has. A controller discovers this instead of hard-coding registers | viewer |
| POST | `/api/devices/{id}/commands/{name}` | Run a command. Body: its parameters (`{"value": 60, "revert_s": 300}`; legacy aliases such as `limit_pct` accepted, unknown names refused), optional `lease_s` (the gateway runs the command's `safe` parameters itself when the lease expires unrenewed; the result says `lease_armed`, and `lease_reason` when there is no safe value), optional `id` (≤ 64 characters, echoed). The command waits for its bus at most its channel's `max_age_s` (30 s) — `expired` otherwise; a held channel answers 409 `{status: rejected, held: true, retry_after_s}`; a value the device already holds answers `already_set: true, frames_sent: 0`. Results carry `queued_ms`. Every write a device is told goes through one lane per wire (a Modbus bus, an MQTT source) and one slot per device and channel; a run may also answer **409** `superseded` (a newer value of the same setting replaced it before it ran — nothing written), `expired` (it waited longer than `max_age_s` for the bus), `cancelled` (the device was disabled or the gateway is stopping), **429** (8 commands already wait on that channel, or the write rate limit), **503** (a standby gateway, or the gateway stopping) or **504** (still running when the wait ended — the outcome goes to the log and the audit). Reads the guard registers, refuses when the recipe does not apply, writes consecutive registers in one frame, settles, reads back. Answers `{status: success\|mismatch\|unverified\|rejected\|error, params, guard, before, after, scale_factors, frames, frames_sent, reason, ms, via, by, ts}` — 200 for the first three, 422 rejected, 502 error, 403 gates / not enabled, 404 unknown, 409 no Modbus source. Audited (`action: command`) | operator |
| POST | `/api/devices/{id}/commands/{name}/dry-run` | The guard's current readings and the exact frames a run would write; nothing on the wire beyond the reads. Allowed with writes off and on a command not enabled yet; not audited | operator |
| PUT | `/api/devices/{id}/energy-meter` | `{device, power, energy}` — the device that measures this one's electricity, with its power field and energy counter (either may be omitted); `{}` removes the link. The device then carries `power_electric` (W) and `energy_electric`. It sets only the `consumption` link of `/meters` and keeps that link's `sign`. Audited | admin |
| GET | `/api/meter-roles` | The roles a meter may have on a device (`consumption`, `input`, `output`; later ones listed with `available: false`) and the `sign` values (`auto`, `as_is`, `invert`); `options` (each threshold's default and range); `meter_only_kinds: {first, more}` — what a device measured only by its meters may be (never `meter`, never `other`) | viewer |
| PUT | `/api/devices/{id}/meters` | `{meters: [{role, device, power, energy, sign}]}` — the whole list of the meters that measure this device, one per role; `[]` removes them all (refused on a meter-only device: it is its meters — delete it instead). `null` is refused (send `[]`), so a client bug never wipes the links. `power` must read W/kW/MW and `energy` Wh/kWh/MWh on the meter; the meter is a device of its own (not an installation unit); a chain of meters may not loop back; `consumption` and `input`/`output` do not go together. `?dry_run=true` checks and stores nothing. 422 carries `fields` keyed `meters[i].power`. Answers `{status: updated\|unchanged, meters, options}`; nothing restarts. With an `input` and an `output` meter the device carries `power_in`, `power_out` (W), `energy_in` / `energy_out` (when the link names a counter; the meter's unit), `power_loss` (W), `efficiency_pct` and, when it has a `rating` attribute (`rated_ac_kw`), `load_pct` — computed whenever one of its meters reports, whether or not the device reads anything itself. Loss and efficiency are published only when both readings are fresh (≤ 3 × their meter's poll interval), measured within `max_skew_s` of each other (default: the slower meter's poll interval), the input steady (moved < 5 % since its previous reading) and the output at most `tolerance_pct` (default 2) above the input; the efficiency also needs an output of at least `min_load_w` (default 100). Never clamped; an absent value is removed from the live values and its retained MQTT topic cleared (empty retained payload). `options: {min_load_w, tolerance_pct, max_skew_s}` sets them for a device with an input and an output meter (absent: kept; `{}`: the defaults; null refused; refusals keyed `options.<key>`; options on a device without a pair: 422 at `options`; removing the pair drops its options). In Home Assistant a pair value carries `expire_after` (≥ 60 s) and is re-sent while present, so it turns unavailable when absent. The device in `GET /api/devices` carries `meter_options` (every threshold, the device's own over the defaults), `meter_options_own` (the device's own only — what an editor sends back) and `meters_status` — the LAST evaluation's verdict: `{power_loss, efficiency_pct, load_pct: {state: published\|absent\|waiting, reason?}, thresholds, age_s}`, reason one of `no_reading`, `no_time`, `stale`, `skew`, `unsteady`, `below_min_load`, `out_exceeds_in`, `negative`, `no_rating`, `not_evaluated` | admin |
| GET | `/api/rule-presets` | the rule examples the templates of the configured devices ship, made concrete per device (`{device}` replaced, `mode: shadow`) | viewer |
| GET | `/api/rule-fields?device=<id>` | the names a formula may read on a device or an installation — the set a rule's inputs are judged against: `{device, fields: [{name, unit}]}`; `fields: null` while it cannot be told yet (nothing running, an MQTT input before its first message); 404 for an id that is neither | viewer |
| PUT | `/api/devices/{id}/commands/{name}/enabled` | `{enabled: true\|false}` — enable one of the template's commands on a device of its own (an installation's units are enabled in their group). Nothing is written to the device; disabling stops a refresh. Audited | admin |
| PUT | `/api/devices/{id}/commands/{name}` | The binding of one of the template's commands on a device of its own: any of `{enabled, lease_s, safe, faces, confirm}`, merged into what is stored (null removes a field; off keeps the binding). `safe` is checked through the command's bounds (422 with `fields.safe`); a `lease_s > 0` without any safe value is refused (422, it would never fire). An installation's unit: 422, set it in its group. Answers `{binding, entry}`. Audited `command binding` old / new; nothing is written to the device | admin |
| PUT | `/api/devices/{id}/channels/{key}` | This device's override of a template channel — `key` is a declared channel name or one of its commands' names: `{min_interval_s, min_on_s, min_off_s, max_starts_per_h, max_age_s}` (0..86400; `max_age_s` ≥ 1; null removes one, the template's again; `combine` stays the template's). Unknown key → 422. Audited `channel override` old / new / template | admin |
| POST | `/api/endpoints/{id}/groups/{gid}/commands/{name}` | The same command on every unit of a group, one result each; a unit that refuses does not stop the others, nothing is rolled back. `{ok, units: [{device, unit_id, http_status, status, …}]}` | admin |
| GET | `/api/commands/history?device=&limit=100` | Past invocations from the audit log, newest first, `detail` decoded (`params`, `via`, `frames`, `before`, `after`, `reason`, `ms`) | admin |
| POST | `/api/devices/{id}/actions/power_limit`, `/api/endpoints/{id}/groups/{gid}/actions/power_limit` | 3.72.0 aliases of the `power_limit` command (`limit_pct` → `value`); the device alias follows the command role (operator), the group alias stays admin | operator / admin |
| GET | `/api/registers/all?device=&source=` | The catalog of the map being edited, grouped by category — a template register with no category (or `other`) whose name is canonical is filed under its canonical category (`power`, `voltage`, `current`, `energy`, `frequency`, `dc`, `quality`, `site`, `temperature`, `status`), each with a `name` label | viewer |
| GET | `/api/registers/selected?device=&source=` | The selection of one source's map; every register carries `category` (canonical name → unit → `other`); `sources[]` carry `selected`, `catalog` (template size) and `interval_s` (fastest group the selection uses) | viewer |
| GET | `/api/devices` (installation units) | Besides the device fields, a unit of an installation carries `endpoint_name`, `group_id`, `role`, `glance` (the fields of its row: the template's `display.glance`, else by role), `live` (those values, fresh only), `alarms` (`{danger, warning, active[]}` from the template's `display.alarms`), `fields` (label/unit per live name) and `read_via[]` — every source that reads it: `id`, `protocol`, `address` (with the unit id; URL redacted for viewers), `template`, `interval_s`, `timeout_s`, `stale_after_s`, `registers` (selected from it), `provides` (fields it supplies now), `status`, `latency_ms`, `reads_5m`, `failed_5m`, `fail_pct_5m`. `selected_registers` is the union across sources | viewer |
| GET | `/api/endpoints` | All endpoints with their units (id, health, last seen, poll rate, errors, `glance` + `live` values — the template's `display.glance`, else by role —, `alarms`, `sources` verdict per source), `units_alarming`, `headline_items` (the template's `display.headline` resolved against the totals: `{field, label, hint, value, unit}`), `headline` (`power_now`, `energy_today`, `autonomy`, `self_consumption` — from the site group when there is one, else the inverters' sum, `null` when not measured), `read_via` (every distinct source across groups: `interval_s`, `latency_ms`, `reads_5m`, `failed_5m`, `fail_pct_5m`, `units_ok/units_total`, `status`), `fields` (label/unit per live name), `groups[]` (each with `aggregates` + `aggregate_fields`, `topic`, `outputs` = the real per-unit topic prefix / bucket / tag, `sources[]` with the same 5-minute window). A grouped installation has NO whole-installation `aggregates` (empty), and a group of one unit has no total (`aggregates: {}`, `topic: null`) | viewer |
| GET | `/api/endpoints/{id}` | One endpoint, same shape, plus how its totals are made: `totals` (the operator's own, `{name: {op, from}}`), `totals_declared` (what the units' templates declare, as the units carry it) and `unit_fields` (every field name the units carry — what a total can be built from); `influxdb` includes `tags` and `outputs`; each unit's `alarms` lists `active: [{field, label, severity, value}]` | viewer |
| POST | `/api/endpoints` | Create: validate → persist → materialize N devices → seed each from the template → hot-start. A new installation is stored write-locked | admin |
| PUT | `/api/endpoints/{id}` | Update. An edit that changes what the units are built from re-materializes them; an edit that only changes endpoint-level settings (name, `aggregates`, unit display names, `totals`, `influxdb.outputs`) keeps every poller running. Routing identity (topic prefix / bucket / device tag) stays fixed, and what the form omits (`write_locked`, `aggregates`, `http_output`, `rest_push`, `totals`, `influxdb.tags`, `influxdb.outputs`, per-unit ids/names) is preserved; sending `totals: {}` / `outputs: []` / `tags: {}` clears them. Malformed `totals` / `tags` / `outputs` → 422 with the offending field named (`influxdb.outputs[0].fields[1].scale`) | admin |
| PATCH | `/api/endpoints/{id}/units/{unit}` | Rename one unit (`{unit}`: its device id or its unit address; an address written in several groups is refused, 422 naming the ids): `{name}` (`''` = the installation's naming again; up to 80 characters; the only key). Only that unit's entry changes; nothing restarts. `If-Match` with the **unit's** `ETag` (the tag its page holds; 412 when stale); the answer carries its new tag. `{status, endpoint_id, unit, device, restarted}` | admin |
| DELETE | `/api/endpoints/{id}[?dry_run=true]` | Delete the installation and stop its units (register files kept). Refused (409) while anything uses it or one of its units; `dry_run` as for a device | admin |
| GET | `/api/endpoints/{id}/dependents` | What uses this installation or one of its units | viewer |
| POST | `/api/endpoints/{id}/test` | Probe every unit on the shared endpoint, one answer each. Opens another Modbus client — dataloggers serve only a few at once | admin |
| GET | `/api/endpoints/{id}/template-changes` | What Update from template would do — nothing is written: `{pending, units_total, units_affected, templates: [{id, name, version}], changes: [{field, label, key, before, after, units}], new: [{field, label, description, units}], missing_templates: [id]}` (a template the units name that is not in the library — nothing to update from), one line per distinct change with how many units it touches. The page offers the update only when `pending`; `GET /api/endpoints[/{id}]` carries the short form as `template_update: {pending, changes, new, units_affected, missing_templates}` | viewer |
| GET | `/api/devices/{id}/template-changes` | The same for a standalone device (the primary: never pending) | viewer |
| POST | `/api/endpoints/{id}/refresh-from-template[?add_new=true]` | The same for every unit of the installation (they stop, refresh, restart): `{status, units: [ … one result each ]}` | admin |

The per-unit sinks (`/api/devices/{unit}/http-output`, `/rest-push`) and the
write lock are declared once and apply to the whole endpoint: one endpoint, one
declaration. `GET /api/status` carries a `endpoints` array alongside `devices`,
and `?device=<endpoint id>` resolves on the history and energy reads (the endpoint's
own aggregate series); an id that is neither a device nor an endpoint answers 404
rather than silently reading the primary's bucket.

### Power quality (Janitza/Jasic PQ recorder — see [pq-recorder.md](pq-recorder.md))

| Method | Path | Description | Role |
|--------|------|-------------|------|
| GET | `/api/pq/status` | Recorder support/enabled/health + lifetime counters per device | viewer |
| GET | `/api/pq/events?device=&start=-30d&limit=200` | Archived PQ events from InfluxDB, newest first | viewer |
| GET | `/api/pq/waveform?event=<ms>&channel=UL2&device=` | One archived half-wave-RMS trace of one event | viewer |
| POST | `/api/pq/config?device=` | Set `{enabled, poll_s, archive_waveforms, base_url}` and (re)start the device's poller. `poll_s`: a number of seconds, at least 30 — anything else (a word, a boolean, null, less) is refused 422 `{errors, fields: {poll_s}}`, nothing stored. `If-Match` with the device's `ETag` | admin |

### Modbus writes (gated)

| Method | Path | Description | Role |
|---|---|---|---|
| POST | `/api/devices/{id}/write` | Write a value (FC5 coil / FC6·FC16 holding). Requires `security.allow_writes: true` **and** an authenticated caller (login or API key). Primary device read-only unless unlocked; register must be declared writable in the template; `write_min`/`write_max` bounds enforced; encoding comes from the template row, never the caller; a register with a live scale factor (`scale_from`), or **any register any command of the device writes, at any position, on the same slave** (WMaxLimPct, WMaxLim_Ena, …; a word count overlapping one counts), is refused with 422 naming the command and its channel — write it through the command; no bypass, `unguarded` included; a device read over MQTT is refused with 422 (commands only); `unguarded: true` (no envelope) is admin only; per-IP rate limit (`write_rate_limit_per_s`); read-back verification; audit-logged. Optional `lease_ms` arms a dead-man lease that reverts to the template's `write_safe` value. Since 3.95 the write travels the device's lane (always since 3.97): the 200 body adds `queued_ms`; a refusal keeps `{"detail": {"errors": [why], "status": …}}` with **409** `superseded` (a newer value of the same register and encoding — from HA too — replaced it before it ran), `expired` (waited > 30 s for the bus), `cancelled` (the gateway stopping), **429** (8 writes already wait on that register), **503** (the gateway became standby), **504** (still running — its outcome is in the log and the audit), **502** (written, failed) | operator |
| POST | `/api/devices/{id}/write-lock` | `{locked: true|false}` — the per-device write lock (the primary ships locked via `security.primary_write_locked`); every face honours it. Audited | admin |
| GET | `/api/devices/{id}/write-info/{register_type}/{address}` | What a write to this register would be allowed to do: the lock state and the register's envelope (writability, bounds, allowed values, safe value), and `through_command` / `channel` when a command writes it (then it is never written raw) — the UI's write dialog reads it | operator |
| GET | `/api/writes/leases` | Active write leases with time remaining | viewer |

### Bridges (the boxes a serial bus is reached through — [rtu-over-network.md](rtu-over-network.md))

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/bridge-types` | The kinds of bridge (`multibus/bridge_types/*.json`): framing `rtu`/`modbus_tcp`, discovery `api`/`manual`, serial config, health, default ports, set-up steps | viewer |
| GET | `/api/bridges` | Every bridge (token never included; `has_token`) with `state` (`online`/`offline`/`degraded`/`checking`/`unknown`, `detail`, `source`: the box's own `/health` asked in the background, or what its devices see), `protocol`, and `ports[]` with the `devices` on each (`id, name, unit_id, endpoint_id, health, bus_share`) and `busy`: how busy the bus's wire was in the last minute, measured once per bus (`{pct 0–100, level: ok\|high\|full, over_capacity, busy_s, window_s, missed_turns, devices, capacity_left_pct}`; `pct` never exceeds 100, `over_capacity` is true when turns were given up or the wire is ≥ 95 % held; null until 5 s are measured). `bus_share` is a device's own share of the wire (0–1) over the same minute | viewer |
| GET | `/api/bridges/{id}` | One bridge, same shape | viewer |
| POST / PUT / DELETE | `/api/bridges`, `/api/bridges/{id}` | `{id, name, type, host, ports: [{port, label?, serial?}], max_connections?, control_port?}`. Our bridge type gets a generated token. Devices on it follow a changed host/port at once (`restarted`). Delete refused (409) while a device uses it. A type change away from RTU framing is refused (409, `listeners` named) while a device only listens on one of its ports | admin |
| GET | `/api/bridges/{id}/scan` | Buses: asked of our bridge (`port, key, label, dev, serial_params, serial_text, available, excluded`), declared for a converter | viewer |
| POST | `/api/bridges/{id}/ports/{key}/serial` | `{baud, parity, databits, stopbits}` for one bus of our bridge | admin |
| POST | `/api/bridges/{id}/ports/{key}/enable` | `{enabled: true\|false}` — serve (or stop serving) one of the board's own UARTs on our bridge (opt-in: a program using it without a lock cannot be seen); 422 when `enabled` is not a boolean | admin |
| GET | `/api/bridges/{id}/diagnostics` | Line health of every bus of our bridge, from the gateway's cache (never a fetch): the bridge's line and transport counters, the gateway's own counters over the same window, the diagnosis (`ok`, `line_noise`, `timeouts`, `network`, `device_silent`, `wiring`, `host_overrun`, `collecting`, `no_data`, `unsupported`), the bridge's recent events (`{fetched_ts, error, supported, ports, events, poll_s}`). 422 for a kind of bridge with no API | viewer |
| GET | `/api/bridge-guard` | `{problems}` — every bus the gateway keeps off the wire because its adapter moved to another port of the bridge or is missing, with where the adapter went | viewer |
| POST | `/api/bridge-guard/follow` | `{host}` — follow the adapters of one bridge: every bus whose adapter is now on another port moves there at once (two swapped buses swap), and its devices restart. `{moved, devices}`; 409 when nothing is to follow | admin |
| GET | `/api/bridges/{id}/setup` | What to set on the box; for our bridge the `docker run` command with its token (the token only to an admin when login or an API key is on) | viewer |
| POST | `/api/bridges/{id}/probe` | `{port, unit_id, address?}` — one register asked as RTU then as Modbus TCP: `rtu`, `modbus_tcp`, `speaks`, `verdict` (right type, wrong type, serial settings, no answer). Opens its own connection | admin |
| POST | `/api/bridges/{id}/ports/{port}/scan` | `{from?, to?, timeout?, fc43?, confirm_open?}` (default 1-247, 0.3 s): `POST /api/bus-scan` on that bridge port — the same job, rules and refusals (a converter that cannot say who is connected asks `confirm_open`). Returns `{job, total, transport, shared_with}`; progress at `GET /api/bus-scan/{job}` (see Devices) | admin |
| GET | `/api/bridges/export` | Every bridge as YAML (tokens only for an admin, or a box with no login and no API key) | viewer |
| POST | `/api/bridges/import` | `{yaml, apply, replace}` — each bridge `new\|replace\|exists\|invalid` with the reason; a file that drops a bus devices use here is refused | admin |
| POST | `/api/bridges/discover` | `{cidr, unit_id?}` (LAN only, at most a /24): the ports each kind's `lan_discovery` declares. Our bridge is named by its `/health`; a converter by one register asked of `unit_id` (default 1) as RTU and as Modbus TCP. `found: [{host, port, type, type_name, version?, detail, candidates?, bridge}]`; a host already added comes back as `bridge` + `already added`, not probed; a host:port a device only listens to is not asked (`skipped: [{host, port, listeners}]`) | admin |

A device on a bridge: `connection: {bridge, bridge_port, unit_id}`; host,
port, protocol (`rtu-tcp`, `ascii-tcp` or `tcp`, from the kind's framing) and connection limit come from the bridge.
`GET /api/devices` carries `bridge` / `bridge_port`. Several devices may share
a bus (one connection, taking turns); one unit id twice on a bus is refused.
`POST /api/devices/test` on a bus a running device already reads goes through
that shared connection (`shared_with`).

**Listening through a bridge:** `connection: {protocol: rtu_tap, bridge,
bridge_port, unit_id}` taps the bus behind a transparent bridge or our serial
bridge (a Modbus TCP gateway is refused: it only answers questions). A bus is
polled or tapped, never both; on a tapped bus `ports[].tapped` is true,
`ports[].wire` is `{frames, crc_errors, error}`, devices carry `tap: true`,
and Scan / Check mode answer 409 `{errors, listeners, bus}` while a listener
is enabled. A tap stays a tap whatever the bridge frames: on a Modbus TCP
gateway it hears nothing, but it never polls.

## Asset attributes ([attributes.md](attributes.md))

Facts about the equipment, from the gateway's catalog.
- **Reads:** every role; coordinates are coarse for non-admins.
- **Writes:** admin or an admin service token; `If-Match: <rev>` → 412 on
  a conflict; a standby answers 503.

| Method | Path | |
|---|---|---|
| GET | `/api/attributes/catalog` | categories, fields, units, limits, choices, levels, kinds |
| GET | `/api/attributes` | everything, the orphans, the plant-wide warnings |
| GET | `/api/attributes/site` · `/api/attributes/{installations\|devices}/{id}` | one place's attributes, its effective location, what is offered |
| PUT / DELETE | `/api/attributes/site/{category}` · `/api/attributes/{installations\|devices}/{id}/{category}` | replace or delete one whole category |
| DELETE | `/api/attributes/{installations\|devices}/{id}` | everything held for an id |
| POST | `/api/attributes/devices/{id}/{category}/copy` | `{"to": [ids]}` |
| PUT | `/api/attributes/settings` | `{"location_publish": "coarse"\|"exact"\|"off"}` and/or `{"publish_serials": true\|false}` (default off: `identity.serial_number` is shown to signed-in users and left out of MQTT meta and OPC UA) |

The `identity` category (device and installation level): `description` (≤ 500 characters, line breaks allowed), `location` (where it is mounted, ≤ 120), `labels` (≤ 16 slugs a-z 0-9 - _, ≤ 32 characters each, kept once each), `manufacturer`, `model`, `serial_number`, `firmware` (≤ 64 each), `installed_on` (`YYYY-MM-DD`). The catalog marks the categories a kind needs with `recommended_for` (e.g. `rating` for an inverter); nothing refuses a device that lacks them.

## Device types

One catalog for every list, filter and menu. Built-ins carry behaviour: meter, inverter, battery, site, heat_pump, drive, ups, charger, sensor, converter, other. User types live in `config.yaml` (`device_types:`, [config-reference.md](config-reference.md#device_types)).

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/device-types` | `{types: [{id, builtin, labels: {en, ro…}, icon, icon_class, behaves_like, templates, recommended_attributes}], icons: {name: class}, behaves_like: [built-ins]}`; `templates` counts the templates `GET /api/device-templates?type=<id>` returns | viewer |
| POST | `/api/device-types` | `{id, labels: {en (required), ro…}, icon?, behaves_like?}` (icon from `icons`, default `other`; behaves_like a built-in, default `other`). A clash with a built-in id or one of its aliases, a duplicate, a malformed field → 422 with `fields` | admin |
| PUT | `/api/device-types/{id}` | new labels, icon or behaves_like (the id stays; a built-in → 422) | admin |
| DELETE | `/api/device-types/{id}` | refused (409 `{errors, devices, templates, groups}`) while a device, a deleted device that can be restored, a template (its `types`) or an installation group (its `role`) uses it | admin |

Installation group roles are checked against the catalog when they change: a built-in other than `other`, or a user type, exactly as the catalog writes it (a role already stored is kept).

## Registers & values

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/registers/all?device=` | Register catalog (from the device's template) | viewer |
| GET | `/api/registers/selected?device=` | Selected (polled) registers + poll groups; each register carries its template category, and the device its template's `display` block | viewer |
| POST | `/api/registers/selected?device=` | Replace the selection; hot-reloads only that device's pollers. `If-Match` (the selection's own `ETag`): a stale one answers 412 | admin |
| POST | `/api/query/register` | On-demand single read (holding/input) on the primary | viewer |
| POST | `/api/query/batch` | On-demand batch read | viewer |
| GET | `/api/search?q=&category=` | Search the register catalog | viewer |
| GET | `/api/poll-groups` | Global poll-group definitions | viewer |
| GET | `/api/canonical-fields` | The canonical field dictionary (name → measurement, unit, MQTT topic, description) that drives editor autocomplete and topic/measurement derivation | viewer |
| POST | `/api/canonical-fields/user` | Add or change one of this gateway's own canonical fields: `{name, category, unit, topic?, description?, counter?}`. A field a device already reads keeps its category and topic (moving them would split its history) | admin |
| DELETE | `/api/canonical-fields/user/{name}` | Remove one of your own canonical fields | admin |
| POST | `/api/canonical-fields/guess` | Batch canonical-name inference for the editor's **Auto-canonicalize** button: `{registers: [{name, label, unit, description}]}` → `{guesses: [name\|null]}`, index-aligned (null where not confident) | admin |
| GET | `/api/values?device=` | All current values of a device's live store, keyed by **store key**: the address in its table — holding as is, input + 100000, coil + 200000, discrete + 300000 (calculated fields from 8000000). Each entry carries its `address` and `register_type`; the register lists (`/api/registers/selected`) give each register's `key` | viewer |
| GET | `/api/values/{key}` | One current value (primary), by store key | viewer |
| GET | `/api/meters` | Devices exposed as JSON feeds (http_output enabled) | viewer |
| GET | `/api/meters/{id}` | Live values of one device as JSON, keyed by register name, with a `stale` flag | viewer |
| GET | `/api/history/registers?device=` | Influx-enabled registers (+ calculated) for the history picker | viewer |
| GET | `/api/history?measurement=&name=&start=&stop=&every=&fn=&device=&group=` | Aggregated history read back from InfluxDB (`fn=all` → mean/min/max band). For an installation's total, `group` names a later group (empty = the first) | viewer |
| GET | `/api/energy/fields?device=` | Energy-tab counter selection + auto-detected candidates | viewer |
| POST | `/api/energy/fields?device=` | Save which cumulative counters the Energy tab totals. `If-Match` (the selection's own `ETag`): a stale one answers 412 | admin |
| GET | `/api/energy/monthly?year=&month=&device=` | Monthly totals (counter deltas) + per-day breakdown, in the configured timezone | viewer |

## Calculated registers

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/calculated/presets` | Built-in parameterized formula presets | viewer |
| GET | `/api/calculated/functions` | Allowed functions + operators | viewer |
| GET | `/api/calculated/templates` | User-saved reusable presets | viewer |
| POST | `/api/calculated/templates` | Save/replace a reusable preset (expression validated) | admin |
| DELETE | `/api/calculated/templates/{tid}` | Delete a preset | admin |
| GET | `/api/devices/{id}/calculated` | The device's calculated registers | viewer |
| POST | `/api/devices/{id}/calculated` | Replace them (every expression AST-validated before save; runtime refreshed in place) | admin |
| POST | `/api/devices/{id}/calculated/test` | Evaluate an expression against live values (UI preview; stateful `prev()`/`dt` reported as such) | operator |

## Device templates

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/device-templates` | Template library (built-ins + user) with `used_by` (devices naming the map, through a source too) and `pending_updates` (`[{kind: endpoint\|device, id, name}]` — where an update from it is waiting). Each also carries `used_by_count`, `device_kind` and `device_kind_source` — the kind a device using it is filed under, in the same words as a device's `kind`. The template's own `kind` stays as it was (the vocabulary of the attributes and of the installation wizard's grouping; empty when the template declares none). A template that lists user types carries `types`. Optional filters, each a comma list: `q`, `kind` (matched against `device_kind`), `type` (a device type: the templates whose `device_kind` is the type's built-in — a user type's `behaves_like` — and the ones that list it), `protocol` (the device-list vocabulary: a Modbus protocol or `modbus` → Modbus maps, `http`, `mqtt`), `origin` (builtin, custom); `sort` = name, id, vendor, kind (`device_kind`) or used_by (`-used_by` for descending). Each summary also carries what connecting to it takes: `speaks` (`modbus`, `seplos_v2`, `can_signals`, `json_http`, `json_mqtt`), `listen_only` (a map only ever heard: `protocol.listen_only`, `rtu_tap` alone, or CAN), `transports` (as declared, `[]` when not), `default_unit_id` (or null), `setup` (`{count, writes: [{fc, address}]}`, FC16 or FC6 for a single-register template), `identify` (`{fingerprint, sunspec}`: how a bus scan recognises it — its `identify` block, or the SunSpec marker for a SunSpec map, judged from the map: `protocol.sunspec: true`, or a row or identify check at 40000/40001), `ports` (`serial`, `ethernet`, `can`, `http`, `mqtt`), `line` (`protocol.line` with the gateway's defaults filled in, or null), `http_path` (`protocol.http.path`, or null) and `master_hint` (or null). Without them the list is unchanged | viewer |
| GET | `/api/device-templates/{id}` | Full template | viewer |
| POST | `/api/device-templates` | Create/update a user template (built-in ids shielded; per-row validation errors). When its `protocol` changed (word order, read size), every running device reading with it restarts at once: `restarted: [device ids]` | admin |
| DELETE | `/api/device-templates/{id}[?dry_run=true]` | Delete a user template. A built-in one is read-only (422); one a device or an installation reads with is refused (409) until they use another | admin |
| GET | `/api/device-templates/{id}/dependents` | The devices and installations that read with this template | viewer |
| POST | `/api/device-templates/check` | `{device_template: {...}}` → every problem a save would report, nothing saved | admin |
| POST | `/api/device-templates/check-expression` | A calculated field's formula against the map being edited: syntax, and whether every name it reads is a register or a calculated field of that template | admin |
| POST | `/api/device-templates/identify-test` | Does a draft's `identify` block recognise a real device? Asked through that device's own connection | admin |
| GET | `/api/device-templates/{id}/export` | Download as JSON (round-trips through upload) | viewer |
| POST | `/api/device-templates/upload` | Validated save; 409 on id conflict unless `overwrite: true` | admin |
| POST | `/api/device-templates/import-csv` | Convert a CSV register map into a template **preview** (save via upload). `{csv, id?, name?, vendor?, model?, default_data_type?, byte_order?: big\|little\|badc\|dcba (ABCD/CDAB accepted), transports?: [tcp\|rtu\|rtu-tcp\|rtu_tap\|http\|mqtt]}`; an `fc`/`register_type` column takes FC1–FC4 aliases (coil, DI, input…); warns when every address looks like 1-based 40001/30001 notation | admin |
| POST | `/api/device-templates/import-yaml` | Convert an upstream/community YAML register map into a template **preview** (`{yaml, id?, name?, vendor?, model?, default_data_type?, default_poll_group?, byte_order?, transports?}` — the YAML's own header may carry `byte_order`/`word_order` and `transports` too; richer than CSV — enum/bits/thresholds/write envelope pass through; save via upload) | admin |

## Gateway role (active / standby)

A gateway is **active** unless a high-availability companion service (available
in MBG Pro) makes it **standby**. A
standby keeps listening where it only listens (an `rtu_tap` bus tap, an MQTT
input) and keeps those values, but:
- puts no frame on a device's wire: no poll, no write, no probe or scan;
- refuses every write, from every face (API, commands, MQTT, OPC UA, rules,
  sequences) with **503**;
- runs no rule (the stall watchdog knows the pause is on purpose) and starts
  no sequence;
- publishes nothing: no MQTT values or discovery, no InfluxDB, no OPC UA
  server, no virtual meters, no JSON feed, no REST push, no PQ recorder;
- raises no "device down" for the devices it does not poll on purpose;
- drops, without reverting, the write leases it held while active;
- **listens to the RTU buses it would poll** (`rtu`, `rtu-tcp` sources
  become bus taps on the same port while standby), so it hears its partner
  master them and `/api/role/bus` can say when it last did.

**Becoming active again** (from standby):
- the rules wait 10 s before their first write, to act on readings of its
  own and not only on its partner's copy;
- the write leases its partner held (`/api/state/mirror`) go on here with
  the time they had left;
- a sequence its partner was running is said (event and alert), never
  resumed.

The gateway never changes its role by itself. The role and its **epoch** (a
number that only grows; a change needs a higher one, an older one is
refused) are kept in `config/role.json`, so a standby that restarts is still
a standby. Every page shows a standby banner (since when, the epoch, who set
it, why); `/health` stays healthy on a standby and reports the role.

| Method | Path | What | Role |
|---|---|---|---|
| GET | `/api/role` | `{role, epoch, since, set_by, reason}`; **200 active, 503 standby** — what a load balancer (HAProxy) asks | none (no login) |
| PUT | `/api/role` | `{role: active\|standby, epoch, reason}`; 409 for an older epoch, or the same epoch with another role; audited, alerted. Needs login or an API key to exist | admin, or an `ha` token |
| GET | `/api/role/bus` | The buses: those this gateway listens to (`listened`: frames heard, seconds since the last valid frame — on a standby, its partner talking) and, on an active gateway, those it polls (`polled`: devices and how many answer) | any role |
| GET | `/api/state/mirror?since=` | What an active gateway hands a standby partner: the values of the devices it polls (only those read after `since`, a wall time, when given), each with its age instead of the process's monotonic stamp; every rule's state and channel's protection (instants as ages); the write leases with their time left; the running sequences | any role |
| POST | `/api/state/mirror` | A standby takes that: values onto its own clock (one it holds newer, from a device it hears itself, is kept), rule and channel state; the leases and sequences are kept for the moment it takes over. 409 on an active gateway | admin, or an `ha` token |
| GET | `/api/state/config?hash_only=` | This gateway's configuration bundle (base64 ZIP, **secrets included**) and its hash — of the configuration, not of the ZIP | admin, or an `ha` token |
| POST | `/api/state/config` | `{bundle, hash, keep_local, from}` — a standby takes its partner's configuration but keeps its own machine's paths: by default its serial adapters (`devices.*.connection.serial_port`, sources, endpoints, groups), `modbus.serial_port` and `ui` host, port, TLS, proxies, public URL; `keep_local` adds paths (keys with dots, a list entry by its `id`, `*` for any). A snapshot of its own first (`pre-partner-config`), then it restarts to run it (Docker's restart policy). The same configuration twice: `same`. 409 on an active gateway | admin, or an `ha` token |
| POST | `/api/alerts/fire` | `{severity: info\|warning\|error\|critical, key, source, message, transition?}` — a service raises an alert through the gateway's channels (rate-limited per key); `sent` says whether alerting is on | admin, or an `ha` token |

## Modules (Settings → Modules)

`modules:` in `config.yaml` lists the modules shown in **Settings → Modules**
(a tab that appears when there is one), once signed in — separate services,
not part of the open-source gateway: `[{id, name, icon, url}]` (id `a-z0-9-`, a Bootstrap icon
name, a `/path` on this host or an `http(s)://` address). A service on the
same host sits behind this gateway's login through `/api/auth/forward`.
Optional per entry: `description` (what it is, how it is installed) and
`health` (a path on this host the page asks, to show whether it answers). A
service set only the old way, `ui.nodes_url`, is listed too.
`GET /api/auth/status` returns them as `modules`.

## ESP nodes (moved out)

The Device Builder (`/api/builder/*`) was removed: ESP flashing from the
browser and the node list are available in **MBG Pro**, which sits behind
this gateway's login through `/api/auth/forward`
and is listed in Settings → Modules (`ui.nodes_url` or a `modules:` entry). LAN discovery of ESPHome nodes
(`/api/discover/esphome`, below) stays here.

## Discovery

| Method | Path | Description | Role |
|---|---|---|---|
| POST | `/api/discover/modbus/scan` | Scan a private CIDR on a port (default 502) for Modbus devices; read-only, LAN-restricted. A host:port a device only listens to is not asked (`skipped: [{host, listeners}]`), nor one a device of this gateway polls, nor our bridge's port with a foreign or unknown client unless `confirm_open` (`skipped: [{host, pollers \| client, reason}]`) | operator |
| POST | `/api/discover/esphome` | Sweep a private CIDR on the ESPHome native-API port (6053); identity via pre-auth plaintext hello, encrypted-API nodes flagged. Docker-friendly (unicast, no mDNS needed) | operator |
| POST | `/api/discover/modbus/units` | Sweep unit/slave IDs on one endpoint (TCP host or RTU serial line), waited for: `{protocol, units, job}`. `protocol`: rtu, ascii, tcp, udp, rtu-tcp, ascii-tcp (kept; anything else 422); `known: [{unit, device}]` lists the addresses devices have (never asked); past 120 s it answers **202** `{protocol, job, state: running}` to poll; a client that goes away stops the job. Runs as a `POST /api/bus-scan` job (FC3, no FC43) — an RTU line held by a device is swept through that device's connection, never a serial client of its own beside it; the same 409s (a listened bus, `client: "unknown"` without `confirm_open`) | operator |
| POST | `/api/discover/sunspec` | Walk the SunSpec model chain on one endpoint (SunS marker + declared models, identity included; read-only FC3). 409 on a bus a device only listens to, on a host:port a device of this gateway polls (`pollers`), and on our bridge's port with a foreign client (`client`) or one nobody can name (`client: "unknown"`, unless `confirm_open`) | operator |
| POST | `/api/discover/mqtt/browse` | Collect a broker's topics with payload previews (retained tree + live listen window) | operator |
| GET | `/api/fronius/discover?host=&port=` | Enumerate inverters/meters behind a Fronius DataManager via its Solar API (SSRF-guarded, LAN-only, redirects refused) | viewer |

## Diagnostics

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/bus-trace?after=&limit=&device=&bus=` | Captured Modbus transactions (incremental polling by sequence number). Each carries the `device` that asked (on a shared bus, each unit its own) and its `bus` (serial port or `host:port`) — filter by either | viewer |
| POST | `/api/bus-trace/config` | Enable/disable/resize/clear the frame trace (RAM-only, boots disabled) | operator |
| POST | `/api/diagnostics/probe` | One-shot read on any Modbus device (FC1–4 only), decoded as every data type × word order (ABCD/CDAB/BADC/DCBA) + hex + ASCII. `unit_id` asks another slave on the device's bus, through the device's own shared connection | operator |

## Virtual meters

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/virtual-meters` | Instances + live status + served values + port range | viewer |
| POST | `/api/virtual-meters` | Add an instance (`{template, port, unit_id, stale_after_s, device, device_fallback, on_stale, max_hold_s, quality_block, enabled}`) | admin |
| PATCH | `/api/virtual-meters/{template}` | Edit an instance (partial: `port`, `unit_id`, `stale_after_s`, `update_interval_s`, `device`, `device_fallback`, `on_stale`, `max_hold_s`, `quality_block`, `ha_mode: failover\|balanced`); restarts it live if running; a standby starts or stops it as the mode says | admin |
| DELETE | `/api/virtual-meters/{template}` | Remove an instance | admin |
| POST | `/api/virtual-meters/{template}/toggle?on=` | Enable/disable (persists + starts/stops live) | admin |
| GET | `/api/virtual-meters/{template}/values` | The map as JSON under the staleness convention (`value: null` + `quality` + `age_s`; `last_value` separate) | viewer |
| GET | `/api/virtual-meters/{template}/stats?limit=` | Query log (last 1024), counters, rates, per-register reads | viewer |
| GET | `/api/virtual-meters/{template}/decode?addr=&count=` | Decode a register range → values + source variables | viewer |
| GET | `/api/virtual-meters/sources?device=` | Live registers of a source device (editor picker) + valid types | viewer |
| GET | `/api/virtual-meters/templates` | Available meter templates | viewer |
| GET | `/api/virtual-meters/template/{id}` | Full editor view of a template | viewer |
| PUT | `/api/virtual-meters/template/{id}` | Create/overwrite a template | admin |
| DELETE | `/api/virtual-meters/template/{id}` | Delete a template file (refused while an instance uses it) | admin |
| POST | `/api/virtual-meters/templates/import` | Import a template from YAML (validated; 409 on conflict) | admin |
| GET | `/api/virtual-meters/template/{id}/export` | Export a template's raw YAML | viewer |

## Configuration

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/config` | Current configuration summary | viewer |
| GET | `/api/config/env-overrides` | Env overrides in effect (secret values masked) | viewer |
| GET / POST | `/api/config/modbus` | Primary Modbus connection settings. POST is judged and written as `PUT` / `PATCH /api/devices/{primary}` (one validator: port 1..65535, unit 0..255 whole numbers, timeout, retries 0..10 / 0..60 s; a refusal is a 422 with `fields`; an unchanged save writes nothing; no reconnect until `/api/config/apply`) | viewer / admin |
| GET / POST | `/api/config/preview` | The read preview's budget, `{max_requests, listen_s}`: reads one preview may send (whole number 1..20, default 4) and seconds it listens on a bus another master polls (whole number 1..60, default 10); anything else (another key, a fraction, text) 422. The `preview:` section is written into `config.yaml` on first load when missing, once, with one INFO line; Settings → Modbus | viewer / admin |
| GET / POST | `/api/config/mqtt` | Global MQTT sink settings (TLS incl.; password never echoed). `default_topic_pattern`: `''` = the identity scope's default; a pattern equal to either scope's default is that default; otherwise the prefix it gives must be a topic prefix and `{gateway}` needs a gateway id (422 at the field) | viewer / admin |
| GET / POST | `/api/config/influxdb` | Global InfluxDB sink settings (token never echoed). `default_bucket_pattern` as the MQTT pattern. `heartbeat_interval`: in `changed` mode an unchanged value is written again once it has not been written for this many seconds (whole seconds 0..86400, 0 = off, default 600 — written into `config.yaml` on first load; anything else 422); a device, an installation or a group may set its own (`/api/devices/{id}/publishing`) | viewer / admin |
| GET / POST | `/api/config/general` | Report timezone (IANA-validated) + default widget colors | viewer / admin |
| GET / POST | `/api/config/identity` | `{gateway_id, identity_scope}` — this gateway's id (`^[a-z0-9][a-z0-9-]{1,31}$`) and how **new** devices are named: `gateway` (`mbg/<gateway>/devices/<device>`, bucket `<gateway>-<device>`) or `legacy` (`mbg/devices/<device>`, bucket `<device>`). GET adds `defaults`, the effective `default_topic_pattern` / `default_bucket_pattern`, `explicit` (the patterns chosen, `''` = the scope's) and `example` (what a device created now gets). 422 with `fields` for a bad or reserved id (`gateway`, or one whose topics would sit under `mqtt.topic_prefix`), a bad scope, the `gateway` scope without an id, or clearing an id a chosen pattern uses. Only devices created afterwards are named the new way — topic, bucket and HA node (`mqtt.ha_node`, stored per device); existing devices keep all three, nothing is re-announced. Audited | viewer / admin |
| GET / POST | `/api/config/alerts` | Alert/webhook settings (header values masked, preserved on save); applied live | viewer / admin |
| GET / POST | `/api/config/ui-security` | HTTPS + login/roles/lockout settings, `canonical_url`, `nodes_url` (a path like `/nodes/` or an `http(s)://` URL, empty = no link; anything else 422); passwords hashed on write, blank keeps current; enabling login requires a non-default admin password. A password change revokes ALL sessions but re-issues the caller's own cookie in the response — rotating your own password never logs you out | viewer / admin |
| GET / POST | `/api/config/security` | IP allowlist (+ caller's own IP), `allow_writes`, `allow_nonlan_http_devices` | viewer / admin |
| POST | `/api/config/apply` | Reconnect all services with the saved config (creates publishers enabled after boot) | admin |
| POST | `/api/config/reload-registers` | Reload the register selection without a full reconnect | operator |

## Backup & snapshots

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/config/export?include_secrets=&include_identity=` | Download a ZIP backup (config.yaml, per-device registers, user templates, virtual_meters.yaml). Secrets and host/port identity are **stripped by default** — wherever they sit: device connection/http/mqtt_in/rest_push blocks, every source, every installation source; including them requires the admin role (the API key stands in only while login is off), and is audit-logged | viewer (sanitized) / admin (with secrets) |
| POST | `/api/config/import?apply=&restore_identity=` | Restore a ZIP backup (the live `gateway.id` and `identity_scope` are kept unless `restore_identity=true`) (raw body, ≤25 MB, ZIP-bomb guarded, path-traversal safe). Sanitized backups are **merged** over the live config so stripped secrets survive. Takes a `pre-import` snapshot first. A bundle that rotates identity revokes every session (the calling admin's is re-issued in the response cookie) and prunes passkeys of gone accounts | admin |
| GET | `/api/config/snapshots` | Automatic + manual snapshots, newest first (LKG on top) | viewer |
| POST | `/api/config/snapshots` | Take a manual snapshot (`{note}`) | admin |
| GET | `/api/config/snapshots/{sid}/download` | Download a snapshot ZIP (full-fidelity, secrets included → admin; the API key stands in only while login is off) | admin |
| GET | `/api/config/snapshots/{sid}/diff?against=live` | Semantic, secrets-masked diff vs live or another snapshot | viewer |
| POST | `/api/config/snapshots/{sid}/restore?apply=&restore_identity=` | Roll back to a snapshot (the live `gateway.id` and `identity_scope` are kept unless `restore_identity=true`) (takes a `pre-restore` snapshot first; config.yaml replaced verbatim). Identity rotation revokes every session (caller re-issued), reloads the passkey registry from the restored file and prunes gone accounts | admin |
| DELETE | `/api/config/snapshots/{sid}` | Delete a snapshot (`lkg` is protected) | admin |

## Status & observability

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/status` | System status: version, `ui_build`, primary Modbus/MQTT/InfluxDB stats, `config` (whether the file loaded and saves are allowed), per-device health (connected, reads, poll rate, error taxonomy, staleness, latency, `health`, `up_since_s`; a CAN device adds `can`: frames, unknown ids, decode errors, error frames, listen-only), `endpoints` (each installation's units online and total power), `writes: {enabled, credential}` (below), `rules_drift_held` (rules whose correction the channel's pace has held for more than 5 minutes now) and `command_lanes` (a wire whose oldest waiting command is older than half its `max_age_s`). `rules_recheck` and `raw_lanes` are gone (their switches were removed in 3.97) | viewer |
| GET | `/api/status/resources` | Process footprint: CPU %, RSS, threads, FDs, TCP connections, uptime | viewer |
| GET | `/api/fleet` | Fleet overview: one row per device — health, staleness, explicit-threshold alarm counts, up to three hero metrics (first dashboard registers) — plus the endpoint name map | viewer |
| GET | `/health` | Container/monitor probe. HTTP 503 only when an enabled virtual meter is genuinely `down`; a stale Modbus source degrades the body only (a restart can't fix an unreachable meter) | — |
| GET | `/metrics` | Prometheus exposition (text 0.0.4): `gateway_device_*`, `gateway_mqtt_*`, `gateway_influx_*`, `gateway_vmeter_*` series | — |
| GET | `/api/events?limit=` | Recent events (persisted across restarts) | viewer |
| GET | `/api/alerts?limit=` | Alerting config/status + recently fired alerts | viewer |
| POST | `/api/alerts/test` | Fire a synthetic alert over the configured channels. Requires login or API key; ~10 s cooldown; webhook redirects refused | operator |
| GET | `/api/audit?limit=&q=&user=` | The audit trail (who changed what). **Admin-only** when auth is on | admin |
| GET | `/api/audit/export.csv` | Full audit trail as CSV | admin |
| GET | `/api/languages` | Available UI languages (scans `ui/languages/*.json`) | viewer |
| GET | `/api/languages/{code}` | One language's translation map | viewer |

## UI & streaming

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/` | The single-page web UI (serves the login shell to an unauthenticated, allowlisted client; data needs a session) | — |
| GET | `/static/*` | UI assets | — |
| WS | `/ws` | Real-time value stream (init snapshot + updates; ping/pong), values keyed by store key like `/api/values` (a register outside the holding table also carries its `address` and `register_type`). Enforces the IP allowlist, the session cookie and a same-origin check itself | viewer |

---

### Examples

Read a register on demand:

```bash
curl -s -X POST http://gateway:8080/api/query/register \
  -H 'Content-Type: application/json' \
  -d '{"address": 19026, "data_type": "float"}'
# {"address":19026,"value":15230.4,"corrected":15230.4,"data_type":"float","register_type":"holding","device_id":null,"timestamp":"..."}
```

Write with a 5-second dead-man lease (auth + allow_writes required):

```bash
curl -s -X POST http://gateway:8080/api/devices/sdm630/write \
  -H 'Content-Type: application/json' -H 'X-API-Key: <key>' \
  -d '{"address": 40100, "value": 4500, "lease_ms": 5000}'
# reverts to the template's write_safe value if not renewed within 5 s
```

Scrape metrics:

```bash
curl -s http://gateway:8080/metrics | grep gateway_device_up
```


## OPC UA server ([opcua.md](opcua.md))

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/opcua` | The OPC UA server: `enabled`, `running`, `endpoint`, `devices`, `variables`, `error`, its settings, `auth_enabled` | viewer |
| PUT | `/api/opcua` | `{enabled, port, host, encryption: required\|optional\|off, allow_anonymous, update_s, public_url}` (`public_url`: `opc.tcp://<balancer>:<port>/<path>` or empty) — saved, the server restarts with them ([opcua.md](opcua.md)) | admin |
| GET | `/api/opcua/certificate` | The server's certificate (DER), to trust in a client | viewer |

## Rules (the declarative controller)

A rule decides *when* a command runs — see [rules-design.md](rules-design.md).
New rules are **shadow** (they decide and say so, never write); arming needs
`security.allow_writes` and an authenticated caller. Rules live in `rules.yaml`.

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/rules` | Every rule with `live`: `state` in words (`normal`, a step label, `true`/`false`, `stale`), `signal`, per-unit `want` / `commanded` / `actual` (+ age) / `clamp` / `paused_until` / `failures`, `last` decision, `error` when it cannot run | viewer |
| GET | `/api/rules/{id}` | One rule, same shape | viewer |
| POST / PUT / DELETE | `/api/rules`, `/api/rules/{id}` | Create, edit, delete. Validated in words: target exists and offers the command (enabled, not an alias), signal parses and names its device (`device.register`, hyphenated ids allowed), steps ascending with a `release_below` under the first, `normal` present, timing bounds, one armed owner per target+command. Deleting or un-arming a rule that moved its target away from the command's `safe` values restores them first (`on_disable: safe`) | admin (armed: + writes on) |
| POST | `/api/rules/validate` | The rule as a body → what it would see and want **now**: `signal`, `signal_age_s`, `stale`, `state`, `want`, per-unit `actual`. No state kept | admin |
| GET | `/api/rules/export[?ids=a,b]` | The rules as a YAML file — definitions only, no live state; an import always brings them in as shadow; 404 names an unknown id | viewer |
| POST | `/api/rules/import` | `{yaml, apply: false\|true, replace: false\|true}` — a `rules:` list or one rule. Without `apply` nothing is written: `{rules: [{id, label, kind, status: new\|replace\|exists\|invalid, errors, was_armed}], ok, total}`; with it, valid rules are saved (`created`/`replaced`). Imported rules always arrive in **shadow**; an armed rule here is never replaced | admin |
| POST | `/api/rules/{id}/mode` | `{mode: shadow\|armed}` — arming is explicit and audited (`rule armed`); shadow releases the target | admin |
| POST | `/api/rules/{id}/enable` | `{enabled: bool}` — disabling applies `on_disable` | admin |
| POST / DELETE | `/api/rules/{id}/clamp` | `{max, expires_s}` — a ceiling on the numeric want; never expires *into* a step (holds until the signal is under `release_below`) | admin |
| POST | `/api/rules/{id}/override` | `{seconds}` — pause the rule so another face may command its target; 0 lifts it | admin |
| GET / POST | `/api/sequences` | Every sequence with its live run and the last ones; create one (JSON, or `{yaml: "..."}`): steps `command` / `wait_s` / `wait_until`+`timeout_s` / `check`, `on_abort`, `params`, `max_duration_s` — validated in words (devices and their enabled commands exist, expressions parse) | viewer / admin |
| GET / PUT / DELETE | `/api/sequences/{id}` (`/yaml` for the text) | One sequence; edit; delete (refused while it runs) | viewer / admin |
| POST | `/api/sequences/{id}/run` | `{params, dry_run}` — starts a run (one per sequence, one per device); a dry run returns the whole plan, nothing written. Each command goes through the command write path (gates, audit, the channel's referee and protection); a safety rule holding a channel aborts it | operator |
| POST | `/api/sequences/{id}/cancel` | Cancels the run; `on_abort` runs | operator |
| GET | `/api/channels/{device}` | The device's channels (commands that control the same registers, or a channel the template names): `combine`, the template's protection, the rules on each (winner first: `layer`, `priority`, `forced`, `want`, `yielding_to`), the `winner`, and the motor's state (`on`, `on_for_s`, `off_for_s`, `starts_last_h`) | viewer |
| POST / DELETE | `/api/rules/{id}/force` | `{value, seconds}` (or `params`) — the rule asks for this for so long (≤ 86400 s) whatever its signal says, then decides again; `seconds: 0` or DELETE ends it. The clamp still caps it, the on/off timers still hold a start; persisted, audited (`rule force`) | admin |
| GET | `/api/rules/{id}/decisions?limit=` | The decision ring (200), newest first: `action` (run, reassert, shadow, hold, stale, idle, sweep, ignored), `reason`, `state`, `signal`, `want`, `actual`, `result` | viewer |

While a rule is **armed**, `POST /api/devices/{id}/commands/{name}` (and the
MQTT / HA faces) on its target answers `409 rejected: owned by rule <id>`
unless the body carries `override_s` — which pauses the rule for that long.

MQTT: retained `mbg/rules/<id>/state` `{mode, enabled, state, signal, units,
decision, reason, ts}`; `mbg/rules/<id>/event` on transitions; `mbg/rules/<id>/set`
accepts `{enabled}`, `{clamp: {max, expires_s}}` / `{clamp: null}`,
`{override_s}` and `source` (gated like the command faces: `security.allow_writes`,
`mqtt.allow_write_entities` and an authenticated broker session). Arming is
not available over MQTT.

## MQTT commands (installations)

With `security.allow_writes` and `mqtt.allow_write_entities` on, every unit
with an enabled command that has the `mqtt` face subscribes:

| Topic | Payload | Effect |
|-------|---------|--------|
| `<unit prefix>/cmd/<command>` | a number (the `value` parameter), or an object of parameters — `{"value": 60, "revert_s": 600, "ramp_s": 0, "source": "ov"}`; a preset's legacy aliases (`limit_pct`, `revert_timeout`, `ramp_time`) accepted; `source` names the caller in the audit | that command on that unit |
| `<group prefix>/cmd/<command>` | as above | every unit of the group that has it enabled |
| `<unit prefix>/cmd/result` | `{command, device, status, params, before, after, frames_sent, reason, ms, via, by, ts, id, queued_ms}` (not retained); `status` also `superseded` / `expired` / `cancelled`, `held: true` + `retry_after_s` when the channel's pace holds it | every outcome of a command that came over MQTT / HA, refusals included; one per unit; none from a standby. Since 3.95 also every outcome of a **raw HA entity write**: `command: raw:holding@<address>`, `register`, `params: {value}`, `status` (`success` / `mismatch` / `unverified` / `error` / `rejected` / `superseded` / `expired`), `read_back`, `verified`, `queued_ms`, `via: ha` — the retained `cmd/<name>/state` is a command's only |
| `<unit prefix>/cmd/<command>/state` | `{status, params, after, ts, via, by}` (retained) | the last applied command — a controller that restarts learns what the device was told without asking the wire |

The `fronius_sunspec_inverter` template ships `power_limit` (`value` 0..100 %,
`revert_s` default 600, `ramp_s`) and `restore` (= `power_limit` 100 %, revert 0).
Their read-back is published continuously as `<unit prefix>/controls/power_limit_pct`,
`…/controls/power_limit_enabled`, `…/controls/power_limit_revert_s`,
`…/controls/power_limit_ramp_s`, `…/controls/connected`. Retained commands are
never executed (a replay is not an order). What a device offers is declared
in its template (`commands:`) and enabled by a binding on the group or device
(`commands: [{name: power_limit}, {name: restore, faces: {ha: false}}]`) —
see [commands-design.md](commands-design.md).
