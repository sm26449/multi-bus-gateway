# Changelog

## 3.1.5

### 2026-08-01 — freshness guard: the 'hold' policy site too

A third independent audit (reviewing the 3.1.3 tree) confirmed the 3.1.4
future-timestamp fix and found one data-serving site it had missed: the
`hold` staleness policy served a last-good value while `now - held_ts <=
max_hold_s`, which a backward clock step turns negative — extending the hold
by the step size. That branch now uses the same `_is_fresh` guard
(`0 <= now - held_ts <= max_hold_s`), so a held stamp from before a backward
step can't stretch the hold. The audit also verified 3.1.4's other freshness
sites, tombstone/`_safe_device_id` path safety and the 0600 files as healthy.
587 tests pass.

## 3.1.4

### 2026-08-01 — freshness future-timestamp guard (ESS-critical)

Independent audits (running against an older checkout, but the finding holds
on HEAD) surfaced a residual in the virtual-meter clock-step handling: the
3.1.2 `freshness_now()` rebasing covers the grace window, but AFTER grace a
store timestamp captured *before a backward clock step* is in the future
relative to the new wall clock, so `now - ts < 0` slipped through as fresh —
a dead source could read as live to the ESS for up to the step size.

Every freshness verdict now rejects a future timestamp: fresh requires
`0 <= now - ts <= bound` (helper `_is_fresh`), applied in `_rebuild_block`,
the supervisor's legacy stop check, `json_view` and `health_state` — the
latter two now also read the step-rebased clock. Genuinely fresh data still
rides a step; a dead source stays stale in both step directions, verified
adversarially. 586 tests pass.

## 3.1.3

### 2026-07-31 — regression sweep (path-traversal fix)

A post-change review of the restore feature found — and fixed — a
**self-introduced path-traversal**: `DELETE /api/devices/restorable/<id>`
with an id of `..` mapped to `config/devices/..` = the config dir and
`shutil.rmtree`'d the ENTIRE configuration. Now every id that maps to a
`config/devices/<id>` path is validated as a single safe segment (no `..`,
`/`, `\`, NUL); the restore endpoint maps a bad id to 422, not 500.
Regression-tested so it can never come back.

Also verified clean after the 3.1.2 audit fixes: the virtual-meter freshness
guard is correct in BOTH clock-step directions (stale never looks fresh —
adversarially checked), audit-log rotation still produces 0600 files,
generated-YAML quoting leaves UTF-8 (Romanian diacritics) intact, and the
delete tombstone preserves connection secrets at 0600 so a restore is
complete.

## 3.1.2

### 2026-07-31 — second audit pass (verified fixes)

Another cross-checked audit (external models + in-code verification; false
positives rejected). Six genuinely-valid fixes; 581 tests pass.

- **Virtual-meter freshness is now fully clock-step immune** — the earlier
  guard only held the instance-level *stop*; the per-register freshness in
  `_rebuild_block` and the quality-block age still used raw wall-clock, so a
  jump could momentarily mark fresh rows stale or report a false age. The
  guard now exposes a rebased `freshness_now()` used across the whole verdict;
  the grace window is floored at 5 s so a sub-second meter still rides a step.
- **`passkeys.json` and `audit.jsonl` are created 0600** (identity material,
  matching how `config.yaml` is already handled).
- **Generated-YAML quoting escapes ALL control characters** (incl. NUL/DEL),
  not just newlines/tabs — a hostile label can't break out of the scalar.
- **ESPHome discovery rejects a truncated HelloResponse** (requires the full
  declared body) — no false-positive from a partial frame.
- **`MODBUS_STALE_AFTER_S`** documented in `.env.example` and compose.
- **Builder UI**: status fetch checks `response.ok` (a real HTTP error no
  longer renders as the "disabled" form), and settings labels are associated
  with their inputs (`for`/`id`).

Rejected as non-issues after verification: influx buffer in backups (runtime
state, by design), passkeys in the sanitized export (already correct — it
travels only with secret-bearing backups), ESPHome URL SSRF (admin-only,
consistent with the app's trusted-LAN model).

## 3.1.1

### 2026-07-30 — audit hardening pass (security · backup · reliability)

A cross-checked review (internal 5-domain audit + three independent external
models, every finding verified in code before acting) produced three fix lots.
Output stays byte-identical on the data path; 569 tests pass.

**Security & backup**
- **Sanitized config export** now strips `esphome.password` and the token
  from `alerts.webhook_url` (endpoint kept so a restore still works) — a
  "share-safe" backup no longer carries the ESPHome dashboard credential.
- **Backup/restore completeness**: snapshots and the export ZIP now include
  `builder_profiles.json`, the virtual-meter templates under
  `config/templates/`, and `calculated_templates.json`; `passkeys.json`
  travels with secret-bearing backups — a disaster restore no longer loses
  hardware profiles, emulated meters, or WebAuthn logins.
- **Adopt** no longer echoes the live MQTT broker password to the browser:
  `/api/builder/generate` returns a sentinel that `/api/devices` resolves
  server-side.

**Reliability & correctness**
- ESPHome discovery hardened: full-length varint protobuf lengths, exact
  3-byte header reads (a fragmented peer no longer aborts the sweep), a
  soft-fail per host, and IPv4/IPv6-correct result sorting.
- The Builder's blocking dashboard login moved off the asyncio event loop
  (WebSocket compile/flash streams no longer risk a stall).
- Module caches in the Builder routes are lock-guarded; virtual-meter
  quality age clamps at 0 across a backward clock step; generated-YAML
  scalar quoting escapes newlines/tabs.

**Devices — restore a deleted device**
- Deleting a non-primary device now keeps its FULL definition (a tombstone:
  connection + template + routing, beside the register selection), not just a
  stray registers file. A "Deleted devices (restorable)" card on the Devices
  page lists them with one-click **Restore** (rebuilds the exact device) and
  **Forget** (drops the kept settings for good — no more orphan dirs).
- Fixed a latent mismatch: re-adding an id whose kept registers belong to a
  DIFFERENT template now re-seeds from the assigned template instead of
  decoding against the wrong map.

**Ops & UI**
- Standalone `docker compose up -d` works on a fresh host (the external
  stack network moved to `docker-compose.override.yml.example`).
- `.env.example` + manual document `TZ` and the `ESPHOME_*` variables.
- Builder console caps its buffer (a huge compile log can't freeze the
  tab), the node list shows a loading state, icon-only actions carry
  `aria-label`s, and status-pill text colors come from theme variables.
- Manual §15 now spells out exactly what a backup does and does NOT include.

## 3.1.0

### 2026-07-24 — Device Builder (ESPHome-backed node firmware)

A new **Device Builder** card on the Devices page turns the gateway into a firmware authoring point
for remote ESP32/ESP8266 nodes — RS485/Modbus readers at other sites that
publish back over MQTT. An external, stock **ESPHome** container does the
compiling; the gateway drives it entirely over its HTTP/WS API (no shared
volume, no new Python dependencies), so enabling the feature is one URL in
Devices → Device Builder. Off by default; everything degrades gracefully without it.

- **Node management** — list (incl. mDNS-discovered adoptables), import
  YAML, editor with server-side validation, live-log console for
  compile / OTA flash / device logs (WebSocket relay), artifact downloads.
- **Generate from template** — the differentiator: pick any Modbus device
  template + register subset and get firmware YAML (uart/modbus/
  modbus_controller with correct value types, byte order, scale folded into
  filters, poll groups → update_interval/skip_updates) publishing scalars to
  explicit per-register topics.
- **One-click Adopt** — the same wizard also creates the PAIRED gateway
  side: a user device-template and an MQTT-input device with byte-identical
  topics, so data flows in with zero double configuration.
- **USB web flasher** — first-time flashing from the browser via a locally
  vendored esp-web-tools (no CDN/cloud), with Improv Wi-Fi provisioning over
  the same cable (`improv_serial:` is part of every generated firmware).
- **Hardware profiles** — shareable board/pin presets for the wizard
  (`config/builder_profiles.json`), plus two safe built-ins.
- **Fleet** — "Update all" streams ESPHome's rebuild+OTA of every outdated
  node through the same console.
- **Security** — YAML content and command streams are admin-only under
  auth; every state change and stream lands in the audit log; secrets are
  redacted everywhere (the secrets.yaml helper only ever appends missing
  keys and never logs values).
- Compose ships the `esphome` build-engine service by default (dashboard
  port unpublished — the gateway proxies everything); `ESPHOME_URL` seeds
  the feature on a fresh deploy, so it works with zero configuration.
- **LAN discovery for ESPHome nodes** — Discover devices gains an "ESPHome
  nodes (native API)" sweep: unicast probing of port 6053 with a pre-auth
  plaintext hello for identity (Docker-friendly — no mDNS required);
  adoptable nodes can be imported into the Builder in one click.

## 3.0.0

The 3.0.0 line — successor to the Janitza UMG 512 monitor under the
**Multi-Bus Gateway** name (same engine, new identity). Dated entries below,
newest first.

### 2026-07-10 — relicensed to AGPL-3.0

- **License change**: the project is now **GNU Affero General Public License
  v3.0** (was PolyForm Noncommercial 1.0.0). It is free and open-source under
  the AGPL — commercial use is allowed, but distributing a modified version, or
  offering it as a **network/SaaS service**, requires making the **complete
  source code** available to those users under the same license. The
  paid-commercial-license model no longer applies.
- `LICENSE` is the verbatim AGPL-3.0 text; the full FSF notice header was added
  to every first-party source file (90 files). README badges and license
  sections (EN + RO) updated. No code behavior change.

### 2026-07-10 — sessions, canonical address & illustrated guide

Post-hardening follow-ups within 3.0.0:
- **7-day sliding sessions** (was 12 h) — the session cookie renews on each
  request, so day-to-day operators aren't re-prompted for login constantly.
- **Canonical-address redirect** (`ui.canonical_url`) — a client-side steer
  onto the canonical HTTPS hostname so cookies, passkeys and HSTS bind to one
  origin; a `?local` escape hatch (sticky `mbg-stay-local` flag) keeps the raw
  IP reachable when DNS or the proxy is down.
- **Illustrated UI guide** (`docs/GHID-UI.md`) — a page-by-page walkthrough
  with per-page screenshots captured from the live production instance.
- Docs: MANUAL §18c (what consumers see when a source is lost), an R&D
  integration test plan & procurement list, and a pinned Zigbee coordinator
  recommendation.

### 2026-07-07 — hardening & performance pass

Pre-release hardening of 3.0.0: a nine-domain senior architecture review and a
seven-domain performance audit (both adversarially verified), then fixes.
Output remains byte-identical; 503 tests pass.

#### Correctness & safety (from the architecture review)
- **Virtual meters are read-only**: writes are refused with a Modbus exception
  so a consumer cannot inject a value (e.g. a false grid reading into an ESS
  control loop).
- **Restart equivalence**: the boot path resolves each device's template
  byte-order exactly like the runtime path — a non-big device no longer decodes
  word-swapped garbage after a restart.
- **Staleness is never laundered**: the value store carries the measurement
  time (not callback time); MQTT-in drops retained deliveries by default;
  calculated registers inherit the oldest contributing input's freshness.
- **Config lifecycle**: device edits are transactional (a failed edit can't
  drop the device); publishers are resolved live on shutdown so the InfluxDB
  buffer flushes; `update_ui_security` validates-then-commits.
- **Reconnection**: pollers restart unconditionally after a blip; the influx/
  mqtt monitor threads resume instead of dying on a stale stop flag; MQTT-in
  uses `connect_async` so a broker down at startup is retried.
- **Security**: response headers (CSP/HSTS/X-Frame/nosniff); audit-CSV
  formula-injection escape; session revocation on password change; operator
  role matcher segment-anchored; SSRF redirect policy (no HTTPS→HTTP downgrade,
  strip auth cross-host); passkey opaque-origin rejection; a pre-auth OOM via
  unbounded body buffering closed; snapshot download gated even when auth is off.
- **Drivers**: `reg.scale` applied consistently on HTTP/MQTT-in; NaN/inf
  rejected on every driver and never published; the InfluxDB unit heuristic
  tests most-specific first (VA is no longer voltage, varh no longer reactive
  power); poison replay chunks (4xx) are dropped instead of blocking the buffer.

#### Performance / constrained hardware (from the perf audit)
- GZip on responses (register catalog ~988 KB → ~40 KB over the LAN).
- `threading.stack_size(512K)` before spawning threads (virtual footprint at
  40 threads ~320 → ~32 MB).
- Poll-group interval floor (50 ms; `0` refused) — sub-second is preserved for
  the one control-loop register.
- Register catalog memoized + served with an ETag (304 on revalidation; no
  event-loop stall on UI boot / device switch); SPA shell cached on mtime.
- Calculated-register ASTs compiled once at load (−65 % per eval).
- One `TemplateRegistry` (was loaded twice); production image ships runtime
  deps only (227 → 213 MB).
- Documented constrained-hardware profile & capacity envelope (MANUAL §18b):
  an RPi 3 comfortably runs ~4–5 devices + ~3 virtual meters at realtime ≥ 1 s;
  the single GIL at a fast cadence — not RAM — is the only wall.

### 2026-07-06 — initial release under the Multi-Bus Gateway name

First release under the **Multi-Bus Gateway** name (successor of the
Janitza UMG 512 monitor; same engine, new identity). Highlights vs 2.7.0:
multi-device southbound (Modbus TCP/RTU-master, HTTP/JSON, MQTT-in),
device-template catalog, composite virtual meters with staleness policies
and an in-band quality block (61440, convention v1), calculated registers,
REST push, diagnostics page (frame-level bus monitor, register probe,
SunSpec walk, error taxonomy), MQTT topic browse + json_path picker,
config snapshots/rollback/semantic-diff/last-known-good, audit trail,
admin/operator/viewer roles, passkeys (WebAuthn), /metrics (Prometheus),
reverse-proxy support (ui.trusted_proxies), built-in HTTPS, MQTT TLS
(mutual), IP allowlist, and a gated Modbus write path with dead-man leases.
