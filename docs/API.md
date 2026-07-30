# REST API Reference — Multi-Bus Gateway 3.1.1

Generated from the route definitions in `multibus/api.py` and `multibus/routes/`.
Base URL: `http://<gateway>:8080` (default port; `ui.port` / `UI_PORT`).

## Authentication & roles

Everything below is subject to three independent, **opt-in** gates (all off by
default on a trusted LAN):

1. **IP allowlist** (`security.allowlist`) — applies to every request,
   including `/health` and `/metrics`.
2. **API key** (`API_KEY` env, honoured as `JANITZA_API_KEY` for
   back-compat) — when set, every state-changing request
   (POST/PUT/PATCH/DELETE) must send `X-API-Key: <key>`. The two read-only
   query POSTs (`/api/query/register`, `/api/query/batch`) are exempt.
3. **Login** (`ui.auth.enabled`) — session-cookie auth (`janitza_session`,
   HttpOnly, SameSite=Lax, 7-day sliding TTL). Passwords are PBKDF2-SHA256
   (600 000 iterations). Login is rate-limited per client IP
   (`lockout_threshold` / `lockout_minutes`). Passkey (WebAuthn) login is an
   alternative to the password.

When login is enabled, three roles exist. The **Role** column in the tables
below is the *minimum* role required (with login disabled every endpoint is
open, modulo allowlist/API key):

| Role | May do |
|---|---|
| `viewer` | read-only: any GET/HEAD, plus the two read-only query POSTs |
| `operator` | viewer + live commissioning actions: bus trace, diagnostics probe, discovery, on-demand queries, device tests, payload samples, Modbus **writes** (within template bounds), alert test-fire, register reload, own passkey enrollment |
| `admin` | everything, including anything that lands in a config file (devices, registers, templates, virtual meters, settings, snapshots) and the audit trail |
| `—` (open) | reachable without a session even when login is on: login endpoints, `/api/auth/status`, `/health`, `/metrics`, `/static/*`, `/favicon.ico` |

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
| GET | `/api/auth/status` | Is auth on; caller's role; whether viewer/operator accounts and passkeys exist | — |
| POST | `/api/auth/login` | Log in (`{username, password}`); sets the session cookie; per-IP lockout | — |
| POST | `/api/auth/logout` | Invalidate the session, clear the cookie | operator¹ |
| POST | `/api/auth/passkey/register/begin` | Start WebAuthn enrollment for the logged-in account (needs a hostname, not an IP) | operator |
| POST | `/api/auth/passkey/register/finish` | Verify and store the new passkey | operator |
| GET | `/api/auth/passkeys` | List passkeys (admin sees all users', others their own) | viewer |
| DELETE | `/api/auth/passkeys/{cred_id}` | Delete a passkey (own; admin: any) | operator |
| POST | `/api/auth/passkey/login/begin` | Start a passkey login ceremony (shares the password lockout) | — |
| POST | `/api/auth/passkey/login/finish` | Verify the assertion; sets the session cookie | — |

¹ The write-guard middleware treats logout as an operator-class POST; a
viewer session ends when its cookie expires or is cleared client-side.

## Devices (southbound sources)

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/devices` | All devices with live health (device #1 first) | viewer |
| POST | `/api/devices` | Create a device: validate → persist → auto-select template registers → ensure Influx bucket → hot-start poller + HA discovery | admin |
| PUT | `/api/devices/{id}` | Update a device (stop poller, persist, restart). Primary editable too; routing identity (topic prefix / bucket / tag) stays fixed | admin |
| DELETE | `/api/devices/{id}` | Delete a non-primary device (register file kept on disk; blocked while a virtual meter sources it; clears retained HA discovery) | admin |
| POST | `/api/devices/test` | Ad-hoc connection probe for a not-yet-saved device (TCP/RTU/HTTP/MQTT) | operator |
| POST | `/api/devices/{id}/test` | Probe a saved device (uses its first selected register address) | operator |
| GET | `/api/devices/{id}/poll-groups` | Current poll-group intervals | viewer |
| POST | `/api/devices/{id}/poll-groups` | Update intervals (0.05–86400 s) and live-restart that device's pollers | admin |
| POST | `/api/devices/{id}/http-output` | Toggle the read-only JSON feed (`/api/meters/{id}`) | admin |
| POST | `/api/devices/{id}/rest-push` | Configure the periodic REST push sink (`{enabled,url,interval_s≥5,headers,format:native\|flat,verify_tls,timeout}`); header values masked on read, preserved on save | admin |
| POST | `/api/devices/{id}/rest-push/test` | Push once now and report the result | operator |
| POST | `/api/devices/{id}/payload-sample` | Fetch one full payload from a saved MQTT/HTTP device (for the `json_path` picker) | operator |

### Modbus writes (gated)

| Method | Path | Description | Role |
|---|---|---|---|
| POST | `/api/devices/{id}/write` | Write a value (FC5 coil / FC6·FC16 holding). Requires `security.allow_writes: true` **and** an authenticated caller (login or API key). Primary device always read-only; register must be declared writable in the template; `write_min`/`write_max` bounds enforced; encoding comes from the template row, never the caller; per-IP rate limit (`write_rate_limit_per_s`); read-back verification; audit-logged. Optional `lease_ms` arms a dead-man lease that reverts to the template's `write_safe` value | operator |
| GET | `/api/writes/leases` | Active write leases with time remaining | viewer |

## Registers & values

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/registers/all?device=` | Register catalog (from the device's template) | viewer |
| GET | `/api/registers/selected?device=` | Selected (polled) registers + poll groups | viewer |
| POST | `/api/registers/selected?device=` | Replace the selection; hot-reloads only that device's pollers | admin |
| POST | `/api/query/register` | On-demand single read (holding/input) on the primary | viewer |
| POST | `/api/query/batch` | On-demand batch read | viewer |
| GET | `/api/search?q=&category=` | Search the register catalog | viewer |
| GET | `/api/poll-groups` | Global poll-group definitions | viewer |
| GET | `/api/values?device=` | All current values of a device's live store | viewer |
| GET | `/api/values/{address}` | One current value (primary) | viewer |
| GET | `/api/meters` | Devices exposed as JSON feeds (http_output enabled) | viewer |
| GET | `/api/meters/{id}` | Live values of one device as JSON, keyed by register name, with a `stale` flag | viewer |
| GET | `/api/history/registers?device=` | Influx-enabled registers (+ calculated) for the history picker | viewer |
| GET | `/api/history?name=&start=&stop=&every=&fn=&device=` | Aggregated history read back from InfluxDB (`fn=all` → mean/min/max band) | viewer |
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
| GET | `/api/device-templates` | Template library (built-ins + user) with `used_by` info | viewer |
| GET | `/api/device-templates/{id}` | Full template | viewer |
| POST | `/api/device-templates` | Create/update a user template (built-in ids shielded; per-row validation errors) | admin |
| DELETE | `/api/device-templates/{id}` | Delete a user template (blocked while in use) | admin |
| GET | `/api/device-templates/{id}/export` | Download as JSON (round-trips through upload) | viewer |
| POST | `/api/device-templates/upload` | Validated save; 409 on id conflict unless `overwrite: true` | admin |
| POST | `/api/device-templates/import-csv` | Convert a CSV register map into a template **preview** (save via upload) | admin |

## Device Builder (ESPHome integration)

All routes 503 until `esphome.enabled` + `esphome.url` are configured
(Devices → Device Builder → ⚙). Node YAML can embed Wi-Fi/OTA credentials, so YAML
reads/writes, artifacts and command streams are **admin**-only while auth is
enabled; dashboard errors surface as 502 with the reason.

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/builder/status` | Feature switch + dashboard reachability/version | viewer |
| GET | `/api/builder/settings` | The `esphome:` block, password redacted | viewer |
| POST | `/api/builder/settings` | Persist URL/credentials (empty password = keep stored) | admin |
| GET | `/api/builder/nodes` | Node list: `configured` + mDNS `importable` | viewer |
| GET | `/api/builder/nodes/{name}/config` | Node YAML (raw) | admin |
| PUT | `/api/builder/nodes/{name}/config` | Save YAML; 409 unless `overwrite: true` | admin |
| DELETE | `/api/builder/nodes/{name}` | Archive on the dashboard (recoverable there) | admin |
| GET | `/api/builder/nodes/{name}/downloads` | Build artifact list | admin |
| GET | `/api/builder/nodes/{name}/download?file=` | Proxy one artifact (e.g. `firmware.factory.bin`) | admin |
| GET | `/api/builder/nodes/{name}/manifest` | esp-web-tools manifest for the browser USB flasher | admin |
| POST | `/api/builder/import` | Adopt an mDNS-importable node onto the dashboard (from the `importable` list) | admin |
| POST | `/api/builder/generate` | Template → firmware YAML + PAIRED template & device payload (pure preview) | admin |
| POST | `/api/builder/secrets/ensure` | Append MISSING secrets.yaml keys (never overwrites, values never logged) | admin |
| GET | `/api/builder/profiles` | Hardware profiles (built-ins + user) | viewer |
| POST | `/api/builder/profiles` | Save a user profile | admin |
| DELETE | `/api/builder/profiles/{id}` | Delete a user profile (built-ins protected) | admin |
| WS | `/api/builder/stream/{command}?configuration=` | Live relay of `compile` / `validate` / `upload` / `run` / `logs` / `clean` / `update-all` — frames `{event: line\|exit\|error}` | admin |

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
| GET | `/api/bus-trace?after=&limit=&device=` | Captured Modbus transactions (incremental polling by sequence number) | viewer |
| POST | `/api/bus-trace/config` | Enable/disable/resize/clear the frame trace (RAM-only, boots disabled) | operator |
| POST | `/api/diagnostics/probe` | One-shot read on any Modbus device (FC1–4 only), decoded as every data type × word order (ABCD/CDAB/BADC/DCBA) + hex + ASCII | operator |

## Virtual meters

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/virtual-meters` | Instances + live status + served values + port range | viewer |
| POST | `/api/virtual-meters` | Add an instance (`{template, port, unit_id, stale_after_s, device, on_stale, max_hold_s, quality_block, enabled}`) | admin |
| PATCH | `/api/virtual-meters/{template}` | Edit an instance (partial); restarts it live if running | admin |
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
| GET / POST | `/api/config/ui-security` | HTTPS + login/roles/lockout settings; passwords hashed on write, blank keeps current; enabling login requires a non-default admin password | viewer / admin |
| GET / POST | `/api/config/security` | IP allowlist (+ caller's own IP), `allow_writes`, `allow_nonlan_http_devices` | viewer / admin |
| POST | `/api/config/apply` | Reconnect all services with the saved config (creates publishers enabled after boot) | admin |
| POST | `/api/config/reload-registers` | Reload the register selection without a full reconnect | operator |

## Backup & snapshots

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/config/export?include_secrets=&include_identity=` | Download a ZIP backup (config.yaml, per-device registers, user templates, virtual_meters.yaml). Secrets and host/port identity are **stripped by default**; including them requires the admin role or the API key, and is audit-logged | viewer (sanitized) / admin (with secrets) |
| POST | `/api/config/import?apply=` | Restore a ZIP backup (raw body, ≤25 MB, ZIP-bomb guarded, path-traversal safe). Sanitized backups are **merged** over the live config so stripped secrets survive. Takes a `pre-import` snapshot first | admin |
| GET | `/api/config/snapshots` | Automatic + manual snapshots, newest first (LKG on top) | viewer |
| POST | `/api/config/snapshots` | Take a manual snapshot (`{note}`) | admin |
| GET | `/api/config/snapshots/{sid}/download` | Download a snapshot ZIP (full-fidelity, secrets included → admin or API key) | admin |
| GET | `/api/config/snapshots/{sid}/diff?against=live` | Semantic, secrets-masked diff vs live or another snapshot | viewer |
| POST | `/api/config/snapshots/{sid}/restore?apply=` | Roll back to a snapshot (takes a `pre-restore` snapshot first; config.yaml replaced verbatim) | admin |
| DELETE | `/api/config/snapshots/{sid}` | Delete a snapshot (`lkg` is protected) | admin |

## Status & observability

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/status` | System status: version, primary Modbus/MQTT/InfluxDB stats, per-device health (connected, reads, poll rate, error taxonomy, staleness, latency) | viewer |
| GET | `/api/status/resources` | Process footprint: CPU %, RSS, threads, FDs, TCP connections, uptime | viewer |
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
| GET | `/` | The single-page web UI | viewer |
| GET | `/static/*` | UI assets | — |
| WS | `/ws` | Real-time value stream (init snapshot + updates; ping/pong). Enforces the IP allowlist, the session cookie and a same-origin check itself | viewer |

---

### Examples

Read a register on demand:

```bash
curl -s -X POST http://gateway:8080/api/query/register \
  -H 'Content-Type: application/json' \
  -d '{"address": 19026, "data_type": "float"}'
# {"address":19026,"value":15230.4,"data_type":"float","register_type":"holding","timestamp":"..."}
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
