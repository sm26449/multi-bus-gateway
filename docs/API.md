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
| POST | `/api/mqtt-discovery/{key}/adopt` | Make it a device, off, with its template and topics (`{id?, name?}`) | admin |
| POST | `/api/mqtt-discovery/{key}/ignore` | `{ignore: true\|false}` | admin |
| POST | `/api/mqtt-discovery/enabled` | `{enabled}`, from the next start | admin |

## Devices (southbound sources)

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/devices` | All devices with live health (device #1 first). Each carries `kind` — what kind of equipment it is: meter, inverter, battery, site (a system controller: a Victron GX, a Fronius site API — not the attribute *level* `site`), heat_pump, drive, ups, charger, sensor, other — and `kind_source` (override, role, component, template, names, none). Optional filters, each a comma list of at most 20 values: `q` (up to 200 characters; every word must appear in the id, name, template — every source's too —, vendor, model, address or installation), `kind` (aliases such as `system` accepted), `state` (online, offline, disabled), `protocol`, `installation`. `protocol` takes one vocabulary on both lists: tcp, udp, rtu, ascii, rtu-tcp, ascii-tcp, rtu_tap, http, mqtt, or `modbus` for any of the Modbus ones; a unit read through sources matches its sources' protocols. `sort` = name, id, kind, state or protocol, one leading `-` for descending; ties stay in name order. Without parameters the list is unchanged. A wrong value is a 422 with `fields` | viewer |
| PATCH | `/api/devices/{id}/organise` | `{"kind": "ups"}` files the device under a kind of your choice; `{"kind": null}` derives it again. Never restarts the device; an ordinary edit keeps it. Not for the primary or an installation's unit (their kind comes from the template / the group's role). A `kind` sent with `POST /api/devices` is filed the same way. A stored value the gateway cannot read is ignored at load (event log) and cleared with `null`. `?dry_run=true` checks only | admin |
| POST | `/api/devices` | Create a device: validate → persist → auto-select template registers → ensure Influx bucket → hot-start poller + HA discovery. A device measured only by meters (a UPS with no communication of its own): `{id, name, kind, connection: {protocol: "none"}, meters: [...], options?}` — no template, no address; `kind` and at least one meter required (422 keyed `kind`, `meters`, `meters[i].…`; `options` only with an input and an output meter); it reads and writes nothing and is online when its meters are. Its view shows `connection: {protocol: "none"}` and `measured_by`; `PUT /api/devices/{id}` takes that shape back; a device's way of being read (meter-only or a connection) is fixed after creation (422 at `connection.protocol`); `POST /api/devices/{id}/test` answers "nothing to test — measured by …". `GET /api/protocols` lists it under `meter_only` | admin |
| PUT | `/api/devices/{id}` | Update a device (stop poller, persist, restart). Primary editable too; routing identity (topic prefix / bucket / tag) stays fixed | admin |
| DELETE | `/api/devices/{id}[?dry_run=true]` | Delete a non-primary device (register file kept on disk; clears retained HA discovery). **Refused (409) while anything uses it** — see *What uses it* below; disable it instead (`PUT … {"enabled": false}`), every reference stays. `dry_run` runs the same checks and answers `{status: "valid", would: "delete", dependents}` | admin |
| GET | `/api/devices/{id}/dependents` | What uses this device (`blocking`) and what goes with it (`follows`) — what a delete would be told | viewer |
| GET | `/api/dependents` | `{counts: {<id>: n}}` — how many things use each device and installation, in one pass (a list's *used by N*) | viewer |
| GET | `/api/devices/{id}/components` | The kinds of component the device's template declares and the ones it holds (a GX's batteries and sensors) | viewer |
| POST | `/api/devices/{id}/components` | Add one: `{type, instance, name?, id?, enabled?}`. It reads through the device's connection and starts at once | admin |
| PATCH | `/api/devices/{id}/components/{cid}` | `{name?, enabled?}` — rename it or switch it on/off | admin |
| DELETE | `/api/devices/{id}/components/{cid}[?dry_run=true]` | Remove it; its measurement selection stays on disk, so adding the same instance again resumes it. Refused (409) while anything uses it | admin |
| POST | `/api/devices/{id}/components/discover` | Ask the device what it holds (a read, nothing written): `[{type, instance, product, name, added, device_id}]` | admin |
| GET | `/api/devices/restorable` | Deleted devices whose settings were kept (id no longer active) | viewer |
| POST | `/api/devices/{id}/restore` | Rebuild a deleted device from its kept tombstone (connection + template + registers) | admin |
| DELETE | `/api/devices/restorable/{id}` | Permanently forget a deleted device's kept settings | admin |
| GET | `/api/serial-ports` | Local `/dev` serial lines visible to the container (direct RTU mode; empty when MBG has no `/dev`) | viewer |
| GET | `/api/bridge/adapters` | Live USB-adapter inventory from the serial bridge (RTU-over-network Scan); `available:false` with an `error` when the bridge is unreachable | viewer |
| GET | `/api/devices/{id}/events` | This device's acquisition log, newest first: unreachable / recovered / forced reopen / bus busy / failed batch (with the address and register count) / poll-group overrun episodes. `?limit=1..500`, `?level=error,warn` to narrow to the problems. Returns the live per-group state (interval, last sweep, reads, overruns, age) and the read counters alongside, because "what happened" and "what it is doing now" are the same question when an endpoint is struggling. 404 when the device is not running | viewer |
| POST | `/api/devices/test` | Ad-hoc connection probe for a not-yet-saved device (tcp, udp, rtu, ascii, rtu-tcp, ascii-tcp, http, mqtt). A bus a running device already reads (a serial port, a bridge's port) is asked through that device's connection. `rtu_tap` answers `ok: null` — a tap never transmits, there is nothing to ask | operator |
| POST | `/api/devices/{id}/test` | Probe a saved device (uses its first selected register address). For an `rtu_tap` device: what the tap has HEARD, naming the silent layer — port not open (`open_error`), port open but no valid frame, bus live but nothing for this unit, or `hearing unit N — W windows, last X s ago` | operator |
| GET | `/api/devices/{id}/poll-groups` | Current poll-group intervals | viewer |
| POST | `/api/devices/{id}/poll-groups` | Update intervals (0.05–86400 s) and live-restart that device's pollers | admin |
| POST | `/api/devices/{id}/http-output` | Toggle the read-only JSON feed (`/api/meters/{id}`) | admin |
| POST | `/api/devices/{id}/outputs` | `{mqtt?, influxdb?, ha_discovery?}` (booleans) — switch the MQTT / InfluxDB output or Home Assistant discovery at once, no poller restart. A `devices[]` entry only: 422 for the primary and an endpoint's units (set on the installation) | admin |
| POST | `/api/devices/{id}/publishing` | `{mqtt?: {publish_mode, heartbeat_interval}, influxdb?: {publish_mode, write_interval}}` — the device's own publishing (`changed`/`all`, seconds); `null` = back to Settings, a key left out stays; applies at the next reading. An endpoint unit sets its group; 422 for the primary (it follows Settings). A device's `publishing` says `own`, `settings` and `effective` | admin |
| POST | `/api/devices/{id}/opcua-output` | `{enabled, fields?}` — serve the device on the OPC UA server, every value or only `fields`; an endpoint unit sets its group ([opcua.md](opcua.md#which-devices-and-values-are-served)) | admin |
| POST | `/api/devices/{id}/rest-push` | Configure the periodic REST push sink (`{enabled,url,interval_s≥5,headers,format:native\|flat,verify_tls,timeout}`); header values masked on read, preserved on save | admin |
| POST | `/api/devices/{id}/rest-push/test` | Push once now and report the result | operator |
| POST | `/api/devices/{id}/payload-sample` | Fetch one full payload from a saved MQTT/HTTP device (for the `json_path` picker) | operator |
| POST | `/api/devices/{id}/refresh-from-template[?add_new=true]` | Update from template: carry the template's `label`/`unit`/`description`/`category`/`aggregates` (registers) and `label`/`unit` (template calculated fields) over to the rows the device already has, matched by name; selection, UI flags, sinks, thresholds and formulas are kept, no row is added unless `add_new=true`, which also adds the template's calculated fields the device lacks (counted in `added`); the device restarts so the pipeline sees the new metadata. Answers `{status, device, registers, calculated, files}` (rows changed). 422 for a unit of an installation (refresh the installation) and for the primary | admin |

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
| GET | `/api/devices/{id}/commands` | What this unit can be told: the template's command presets and the unit's own recipes — each with `params` (`min`, `max`, `default`, `unit`, `aliases`), `writes`, `guard`, `verify`, `safe`, `enabled` (bound on the device or its group), `faces`, `confirm`, `mqtt_topic`, `last` result. A controller discovers this instead of hard-coding registers | viewer |
| POST | `/api/devices/{id}/commands/{name}` | Run a command. Body: its parameters (`{"value": 60, "revert_s": 300}`; legacy aliases such as `limit_pct` accepted, unknown names refused), optional `lease_s` (the gateway runs the command's `safe` parameters itself when the lease expires unrenewed). Reads the guard registers, refuses when the recipe does not apply, writes consecutive registers in one frame, settles, reads back. Answers `{status: success\|mismatch\|unverified\|rejected\|error, params, guard, before, after, scale_factors, frames, frames_sent, reason, ms, via, by, ts}` — 200 for the first three, 422 rejected, 502 error, 403 gates / not enabled, 404 unknown, 409 no Modbus source. Audited (`action: command`) | operator |
| POST | `/api/devices/{id}/commands/{name}/dry-run` | The guard's current readings and the exact frames a run would write; nothing on the wire beyond the reads. Allowed with writes off and on a command not enabled yet; not audited | operator |
| PUT | `/api/devices/{id}/energy-meter` | `{device, power, energy}` — the device that measures this one's electricity, with its power field and energy counter (either may be omitted); `{}` removes the link. The device then carries `power_electric` (W) and `energy_electric`. It sets only the `consumption` link of `/meters` and keeps that link's `sign`. Audited | admin |
| GET | `/api/meter-roles` | The roles a meter may have on a device (`consumption`, `input`, `output`; later ones listed with `available: false`) and the `sign` values (`auto`, `as_is`, `invert`); `options` (each threshold's default and range); `meter_only_kinds: {first, more}` — what a device measured only by its meters may be (never `meter`, never `other`) | viewer |
| PUT | `/api/devices/{id}/meters` | `{meters: [{role, device, power, energy, sign}]}` — the whole list of the meters that measure this device, one per role; `[]` removes them all (refused on a meter-only device: it is its meters — delete it instead). `null` is refused (send `[]`), so a client bug never wipes the links. `power` must read W/kW/MW and `energy` Wh/kWh/MWh on the meter; the meter is a device of its own (not an installation unit); a chain of meters may not loop back; `consumption` and `input`/`output` do not go together. `?dry_run=true` checks and stores nothing. 422 carries `fields` keyed `meters[i].power`. Answers `{status: updated\|unchanged, meters, options}`; nothing restarts. With an `input` and an `output` meter the device carries `power_in`, `power_out` (W), `energy_in` / `energy_out` (when the link names a counter; the meter's unit), `power_loss` (W), `efficiency_pct` and, when it has a `rating` attribute (`rated_ac_kw`), `load_pct` — computed whenever one of its meters reports, whether or not the device reads anything itself. Loss and efficiency are published only when both readings are fresh (≤ 3 × their meter's poll interval), measured within `max_skew_s` of each other (default: the slower meter's poll interval), the input steady (moved < 5 % since its previous reading) and the output at most `tolerance_pct` (default 2) above the input; the efficiency also needs an output of at least `min_load_w` (default 100). Never clamped; an absent value is removed from the live values and its retained MQTT topic cleared (empty retained payload). `options: {min_load_w, tolerance_pct, max_skew_s}` sets them for a device with an input and an output meter (absent: kept; `{}`: the defaults; null refused; refusals keyed `options.<key>`; options on a device without a pair: 422 at `options`; removing the pair drops its options). In Home Assistant a pair value carries `expire_after` (≥ 60 s) and is re-sent while present, so it turns unavailable when absent. The device in `GET /api/devices` carries `meter_options` (every threshold, the device's own over the defaults), `meter_options_own` (the device's own only — what an editor sends back) and `meters_status` — the LAST evaluation's verdict: `{power_loss, efficiency_pct, load_pct: {state: published\|absent\|waiting, reason?}, thresholds, age_s}`, reason one of `no_reading`, `no_time`, `stale`, `skew`, `unsteady`, `below_min_load`, `out_exceeds_in`, `negative`, `no_rating`, `not_evaluated` | admin |
| GET | `/api/rule-presets` | the rule examples the templates of the configured devices ship, made concrete per device (`{device}` replaced, `mode: shadow`) | viewer |
| GET | `/api/rule-fields?device=<id>` | the names a formula may read on a device or an installation — the set a rule's inputs are judged against: `{device, fields: [{name, unit}]}`; `fields: null` while it cannot be told yet (nothing running, an MQTT input before its first message); 404 for an id that is neither | viewer |
| PUT | `/api/devices/{id}/commands/{name}/enabled` | `{enabled: true\|false}` — enable one of the template's commands on a device of its own (an installation's units are enabled in their group). Nothing is written to the device; disabling stops a refresh. Audited | admin |
| POST | `/api/endpoints/{id}/groups/{gid}/commands/{name}` | The same command on every unit of a group, one result each; a unit that refuses does not stop the others, nothing is rolled back. `{ok, units: [{device, unit_id, http_status, status, …}]}` | admin |
| GET | `/api/commands/history?device=&limit=100` | Past invocations from the audit log, newest first, `detail` decoded (`params`, `via`, `frames`, `before`, `after`, `reason`, `ms`) | admin |
| POST | `/api/devices/{id}/actions/power_limit`, `/api/endpoints/{id}/groups/{gid}/actions/power_limit` | 3.72.0 aliases of the `power_limit` command (`limit_pct` → `value`); the device alias follows the command role (operator), the group alias stays admin | operator / admin |
| GET | `/api/registers/all?device=&source=` | The catalog of the map being edited, grouped by category — a template register with no category (or `other`) whose name is canonical is filed under its canonical category (`power`, `voltage`, `current`, `energy`, `frequency`, `dc`, `quality`, `site`, `temperature`, `status`), each with a `name` label | viewer |
| GET | `/api/registers/selected?device=&source=` | The selection of one source's map; every register carries `category` (canonical name → unit → `other`); `sources[]` carry `selected`, `catalog` (template size) and `interval_s` (fastest group the selection uses) | viewer |
| GET | `/api/devices` (installation units) | Besides the device fields, a unit of an installation carries `endpoint_name`, `group_id`, `role`, `glance` (the fields of its row: the template's `display.glance`, else by role), `live` (those values, fresh only), `alarms` (`{danger, warning, active[]}` from the template's `display.alarms`), `fields` (label/unit per live name) and `read_via[]` — every source that reads it: `id`, `protocol`, `address` (with the unit id; URL redacted for viewers), `template`, `interval_s`, `timeout_s`, `stale_after_s`, `registers` (selected from it), `provides` (fields it supplies now), `status`, `latency_ms`, `reads_5m`, `failed_5m`, `fail_pct_5m`. `selected_registers` is the union across sources | viewer |
| GET | `/api/endpoints` | All endpoints with their units (id, health, last seen, poll rate, errors, `glance` + `live` values — the template's `display.glance`, else by role —, `alarms`, `sources` verdict per source), `units_alarming`, `headline_items` (the template's `display.headline` resolved against the totals: `{field, label, hint, value, unit}`), `headline` (`power_now`, `energy_today`, `autonomy`, `self_consumption` — from the site group when there is one, else the inverters' sum, `null` when not measured), `read_via` (every distinct source across groups: `interval_s`, `latency_ms`, `reads_5m`, `failed_5m`, `fail_pct_5m`, `units_ok/units_total`, `status`), `fields` (label/unit per live name), `groups[]` (each with `aggregates` + `aggregate_fields`, `topic`, `outputs` = the real per-unit topic prefix / bucket / tag, `sources[]` with the same 5-minute window). A grouped installation has NO whole-installation `aggregates` (empty), and a group of one unit has no total (`aggregates: {}`, `topic: null`) | viewer |
| GET | `/api/endpoints/{id}` | One endpoint, same shape, plus how its totals are made: `totals` (the operator's own, `{name: {op, from}}`), `totals_declared` (what the units' templates declare, as the units carry it) and `unit_fields` (every field name the units carry — what a total can be built from); `influxdb` includes `tags` and `outputs`; each unit's `alarms` lists `active: [{field, label, severity, value}]` | viewer |
| POST | `/api/endpoints` | Create: validate → persist → materialize N devices → seed each from the template → hot-start | admin |
| PUT | `/api/endpoints/{id}` | Update. An edit that changes what the units are built from re-materializes them; an edit that only changes endpoint-level settings (name, `aggregates`, unit display names, `totals`, `influxdb.outputs`) keeps every poller running. Routing identity (topic prefix / bucket / device tag) stays fixed, and what the form omits (`write_locked`, `aggregates`, `http_output`, `rest_push`, `totals`, `influxdb.tags`, `influxdb.outputs`, per-unit ids/names) is preserved; sending `totals: {}` / `outputs: []` / `tags: {}` clears them. Malformed `totals` / `tags` / `outputs` → 422 with the offending field named (`influxdb.outputs[0].fields[1].scale`) | admin |
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
| POST | `/api/pq/config?device=` | Set `{enabled, poll_s, archive_waveforms, base_url}` and (re)start the device's poller | admin |

### Modbus writes (gated)

| Method | Path | Description | Role |
|---|---|---|---|
| POST | `/api/devices/{id}/write` | Write a value (FC5 coil / FC6·FC16 holding). Requires `security.allow_writes: true` **and** an authenticated caller (login or API key). Primary device read-only unless unlocked; register must be declared writable in the template; `write_min`/`write_max` bounds enforced; encoding comes from the template row, never the caller; a register with a live scale factor (`scale_from`) or one a command writes first (WMaxLimPct) is refused with 422 — write it through the command; `unguarded: true` (no envelope) is admin only; per-IP rate limit (`write_rate_limit_per_s`); read-back verification; audit-logged. Optional `lease_ms` arms a dead-man lease that reverts to the template's `write_safe` value | operator |
| POST | `/api/devices/{id}/write-lock` | `{locked: true|false}` — the per-device write lock (the primary ships locked via `security.primary_write_locked`); every face honours it. Audited | admin |
| GET | `/api/devices/{id}/write-info/{register_type}/{address}` | What a write to this register would be allowed to do: the lock state and the register's envelope (writability, bounds, allowed values, safe value) — the UI's write dialog reads it | operator |
| GET | `/api/writes/leases` | Active write leases with time remaining | viewer |

### Bridges (the boxes a serial bus is reached through — [rtu-over-network.md](rtu-over-network.md))

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/bridge-types` | The kinds of bridge (`multibus/bridge_types/*.json`): framing `rtu`/`modbus_tcp`, discovery `api`/`manual`, serial config, health, default ports, set-up steps | viewer |
| GET | `/api/bridges` | Every bridge (token never included; `has_token`) with `state` (`online`/`offline`/`degraded`/`checking`/`unknown`, `detail`, `source`: the box's own `/health` asked in the background, or what its devices see), `protocol`, and `ports[]` with the `devices` on each (`id, name, unit_id, endpoint_id, health, bus_share`) and `busy`: how busy the bus's wire was in the last minute, measured once per bus (`{pct 0–100, level: ok\|high\|full, over_capacity, busy_s, window_s, missed_turns, devices, capacity_left_pct}`; `pct` never exceeds 100, `over_capacity` is true when turns were given up or the wire is ≥ 95 % held; null until 5 s are measured). `bus_share` is a device's own share of the wire (0–1) over the same minute | viewer |
| GET | `/api/bridges/{id}` | One bridge, same shape | viewer |
| POST / PUT / DELETE | `/api/bridges`, `/api/bridges/{id}` | `{id, name, type, host, ports: [{port, label?, serial?}], max_connections?, control_port?}`. Our bridge type gets a generated token. Devices on it follow a changed host/port at once (`restarted`). Delete refused (409) while a device uses it | admin |
| GET | `/api/bridges/{id}/scan` | Buses: asked of our bridge (`port, key, label, dev, serial_params, serial_text, available, excluded`), declared for a converter | viewer |
| POST | `/api/bridges/{id}/ports/{key}/serial` | `{baud, parity, databits, stopbits}` for one bus of our bridge | admin |
| GET | `/api/bridges/{id}/setup` | What to set on the box; for our bridge the `docker run` command with its token (the token only to an admin when login or an API key is on) | viewer |
| POST | `/api/bridges/{id}/probe` | `{port, unit_id, address?}` — one register asked as RTU then as Modbus TCP: `rtu`, `modbus_tcp`, `speaks`, `verdict` (right type, wrong type, serial settings, no answer). Opens its own connection | admin |
| POST | `/api/bridges/{id}/ports/{port}/scan` | `{from?, to?, timeout?}` (1-247, 0.3 s): sweep unit ids on one bus through its shared connection, in the background. Returns `{job, total}` | admin |
| GET / DELETE | `/api/bus-scan/{job}` | Progress `{state: running\|done\|cancelled\|failed, done, total, found: [{unit_id, fc43, sunspec, matches, suggested, device}]}` / cancel | viewer / admin |
| GET | `/api/bridges/export` | Every bridge as YAML (tokens only for an admin, or a box with no login and no API key) | viewer |
| POST | `/api/bridges/import` | `{yaml, apply, replace}` — each bridge `new\|replace\|exists\|invalid` with the reason; a file that drops a bus devices use here is refused | admin |
| POST | `/api/bridges/discover` | `{cidr, unit_id?}` (LAN only, at most a /24): the ports each kind's `lan_discovery` declares. Our bridge is named by its `/health`; a converter by one register asked of `unit_id` (default 1) as RTU and as Modbus TCP. `found: [{host, port, type, type_name, version?, detail, candidates?, bridge}]`; a host already added comes back as `bridge` + `already added`, not probed | admin |

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
and Scan / Check mode answer 409.

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
| PUT | `/api/attributes/settings` | `{"location_publish": "coarse"\|"exact"\|"off"}` |

## Registers & values

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/registers/all?device=` | Register catalog (from the device's template) | viewer |
| GET | `/api/registers/selected?device=` | Selected (polled) registers + poll groups; each register carries its template category, and the device its template's `display` block | viewer |
| POST | `/api/registers/selected?device=` | Replace the selection; hot-reloads only that device's pollers | admin |
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
| POST | `/api/energy/fields?device=` | Save which cumulative counters the Energy tab totals | admin |
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
| GET | `/api/device-templates` | Template library (built-ins + user) with `used_by` (devices naming the map, through a source too) and `pending_updates` (`[{kind: endpoint\|device, id, name}]` — where an update from it is waiting). Each also carries `used_by_count`, `device_kind` and `device_kind_source` — the kind a device using it is filed under, in the same words as a device's `kind`. The template's own `kind` stays as it was (the vocabulary of the attributes and of the installation wizard's grouping; empty when the template declares none). Optional filters, each a comma list: `q`, `kind` (matched against `device_kind`), `protocol` (the device-list vocabulary: a Modbus protocol or `modbus` → Modbus maps, `http`, `mqtt`), `origin` (builtin, custom); `sort` = name, id, vendor, kind (`device_kind`) or used_by (`-used_by` for descending). Without them the list is unchanged | viewer |
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
| POST | `/api/discover/modbus/scan` | Scan a private CIDR on a port (default 502) for Modbus devices; read-only, LAN-restricted | operator |
| POST | `/api/discover/esphome` | Sweep a private CIDR on the ESPHome native-API port (6053); identity via pre-auth plaintext hello, encrypted-API nodes flagged. Docker-friendly (unicast, no mDNS needed) | operator |
| POST | `/api/discover/modbus/units` | Sweep unit/slave IDs on one endpoint (TCP host or RTU serial line) | operator |
| POST | `/api/discover/sunspec` | Walk the SunSpec model chain on one endpoint (SunS marker + declared models, identity included; read-only FC3) | operator |
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
| GET / POST | `/api/config/modbus` | Primary Modbus connection settings | viewer / admin |
| GET / POST | `/api/config/mqtt` | Global MQTT sink settings (TLS incl.; password never echoed) | viewer / admin |
| GET / POST | `/api/config/influxdb` | Global InfluxDB sink settings (token never echoed) | viewer / admin |
| GET / POST | `/api/config/general` | Report timezone (IANA-validated) + default widget colors | viewer / admin |
| GET / POST | `/api/config/alerts` | Alert/webhook settings (header values masked, preserved on save); applied live | viewer / admin |
| GET / POST | `/api/config/ui-security` | HTTPS + login/roles/lockout settings, `canonical_url`, `nodes_url` (a path like `/nodes/` or an `http(s)://` URL, empty = no link; anything else 422); passwords hashed on write, blank keeps current; enabling login requires a non-default admin password. A password change revokes ALL sessions but re-issues the caller's own cookie in the response — rotating your own password never logs you out | viewer / admin |
| GET / POST | `/api/config/security` | IP allowlist (+ caller's own IP), `allow_writes`, `allow_nonlan_http_devices` | viewer / admin |
| POST | `/api/config/apply` | Reconnect all services with the saved config (creates publishers enabled after boot) | admin |
| POST | `/api/config/reload-registers` | Reload the register selection without a full reconnect | operator |

## Backup & snapshots

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/config/export?include_secrets=&include_identity=` | Download a ZIP backup (config.yaml, per-device registers, user templates, virtual_meters.yaml). Secrets and host/port identity are **stripped by default** — wherever they sit: device connection/http/mqtt_in/rest_push blocks, every source, every installation source; including them requires the admin role (the API key stands in only while login is off), and is audit-logged | viewer (sanitized) / admin (with secrets) |
| POST | `/api/config/import?apply=` | Restore a ZIP backup (raw body, ≤25 MB, ZIP-bomb guarded, path-traversal safe). Sanitized backups are **merged** over the live config so stripped secrets survive. Takes a `pre-import` snapshot first. A bundle that rotates identity revokes every session (the calling admin's is re-issued in the response cookie) and prunes passkeys of gone accounts | admin |
| GET | `/api/config/snapshots` | Automatic + manual snapshots, newest first (LKG on top) | viewer |
| POST | `/api/config/snapshots` | Take a manual snapshot (`{note}`) | admin |
| GET | `/api/config/snapshots/{sid}/download` | Download a snapshot ZIP (full-fidelity, secrets included → admin; the API key stands in only while login is off) | admin |
| GET | `/api/config/snapshots/{sid}/diff?against=live` | Semantic, secrets-masked diff vs live or another snapshot | viewer |
| POST | `/api/config/snapshots/{sid}/restore?apply=` | Roll back to a snapshot (takes a `pre-restore` snapshot first; config.yaml replaced verbatim). Identity rotation revokes every session (caller re-issued), reloads the passkey registry from the restored file and prunes gone accounts | admin |
| DELETE | `/api/config/snapshots/{sid}` | Delete a snapshot (`lkg` is protected) | admin |

## Status & observability

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/status` | System status: version, primary Modbus/MQTT/InfluxDB stats, per-device health (connected, reads, poll rate, error taxonomy, staleness, latency) | viewer |
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


## Rules (the declarative controller)

A rule decides *when* a command runs — see [rules-design.md](rules-design.md).
New rules are **shadow** (they decide and say so, never write); arming needs
`security.allow_writes` and an authenticated caller. Rules live in `rules.yaml`.

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/opcua` | The OPC UA server: `enabled`, `running`, `endpoint`, `devices`, `variables`, `error`, its settings, `auth_enabled` | viewer |
| PUT | `/api/opcua` | `{enabled, port, host, encryption: required\|optional\|off, allow_anonymous, update_s, public_url}` (`public_url`: `opc.tcp://<balancer>:<port>/<path>` or empty) — saved, the server restarts with them ([opcua.md](opcua.md)) | admin |
| GET | `/api/opcua/certificate` | The server's certificate (DER), to trust in a client | viewer |
| GET | `/api/rules` | Every rule with `live`: `state` in words (`normal`, a step label, `true`/`false`, `stale`), `signal`, per-unit `want` / `commanded` / `actual` (+ age) / `clamp` / `paused_until` / `failures`, `last` decision, `error` when it cannot run | viewer |
| GET | `/api/rules/{id}` | One rule, same shape | viewer |
| POST / PUT / DELETE | `/api/rules`, `/api/rules/{id}` | Create, edit, delete. Validated in words: target exists and offers the command (enabled, not an alias), signal parses and names its device (`device.register`, hyphenated ids allowed), steps ascending with a `release_below` under the first, `normal` present, timing bounds, one armed owner per target+command. Deleting or un-arming a rule that moved its target away from the command's `safe` values restores them first (`on_disable: safe`) | admin (armed: + writes on) |
| POST | `/api/rules/validate` | The rule as a body → what it would see and want **now**: `signal`, `signal_age_s`, `stale`, `state`, `want`, per-unit `actual`. No state kept | admin |
| GET | `/api/rules/export[?ids=a,b]` | The rules as a YAML file (definitions only — no live state); 404 names an unknown id | viewer |
| GET | `/api/rules/export[?ids=a,b]` | The rules as a YAML file — definitions only, no live state; an import always brings them in as shadow | viewer |
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
| `<unit prefix>/cmd/result` | `{command, device, status, params, before, after, frames_sent, reason, ms, via, by, ts}` (not retained) | the outcome, one per unit |
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
