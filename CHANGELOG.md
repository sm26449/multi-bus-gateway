# Changelog

## 3.9.0

### 2026-08-13 — Redundant-source failover for virtual meters

A virtual-meter register can now be backed by an **ordered list of redundant
sources** instead of one — the reliability guard for the ESS feed. A single
source outage no longer stops the meter.

```yaml
source: { failover: ["_G_P_SUM3", "fronius.power_active_total"] }
```

- **First-fresh wins** — each rebuild serves the highest-priority candidate that
  is fresh (within that row's freshness bound); a missing/stale candidate is
  skipped in order. The primary is preferred whenever fresh, so recovery
  switches back automatically.
- **Degrades through `on_stale`** — if *no* candidate is fresh the row behaves
  exactly as a single stale source (under `fail` the read is refused — never a
  silently-stale value into the ESS). Composes with the existing freshness
  watchdog and quality block unchanged.
- **Observable** — a change of serving source logs one event (`warn` on drop to
  a lower-priority source, `info` on recovery); the `combined` alias matches
  evcc's name for the same idea. Editor + template save/load round-trip the new
  kind. +9 tests. Existing single-source meters are untouched.

## 3.8.0

### 2026-08-13 — Value-based alerting (threshold engine)

Turns each register's **visual** thresholds — the same `warningLow/dangerLow/
warningHigh/dangerHigh` limits that colour the dashboard — into alert **events**,
delivered over the existing alert path (MQTT + webhook + event log) and onward to
`pv-stack-alerts` (alertd) for Telegram/SMS. **Off by default**
(`alerts.signals.threshold: false`).

- **Hysteresis state machine** (`multibus/threshold_engine.py`) — *fast to alarm,
  slow to clear*: escalation fires on the raw limit (safety), while clearing to
  normal requires the value to retreat past the boundary by `threshold_deadband_pct`
  (default 2 %). A value hovering at a limit cannot flap. One band per value, so a
  higher severity inherently suppresses the lower one; events fire only on band
  transitions, never on steady state.
- **Reuses the existing `AlertManager`** — a crossing calls the same rate-limited
  `fire()` path as the device/sink/latency/buffer signals; no new channels. Point
  `alerts.webhook_url` at an alertd webhook input and the events flow to whatever
  channels alertd already has. alertd itself is unchanged.
- **Read-only, off the hot path** — evaluation runs in the existing 5 s event
  harvester over each device's live value store; the poll loop is untouched.
- **Live-tunable** from Settings → Alerts (`signals.threshold`,
  `threshold_deadband_pct`, `threshold_alert_on_start`); band state is pruned when
  a register or device goes away so a removed limit can't leave a stuck alarm.
- +36 tests (exhaustive band/hysteresis matrix + AlertManager integration).

## 3.7.0

### 2026-08-12 — Reliability batch: decode & transport hygiene

Correctness at the wire→value boundary and quieter, truthful transport
logging. Every item is **off by default or opt-in**, so existing deployments
behave exactly as before until a template or setting turns it on.

- **Not-available sentinel decode** — a register may declare `nan` (True = the
  data type's SunSpec not-implemented value such as `0x8000`/`0xFFFF`, or an
  explicit raw value / list). A match now reads as *missing* rather than a
  garbage number (e.g. −32768 °C), so a disconnected phase or an unpopulated
  SunSpec model no longer poisons a chart or an average. Float NaN/Inf are
  always dropped.
- **Cumulative-counter hygiene** — an energy register may declare
  `monotonic: true`. A downward glitch on a Wh/kWh/varh counter would look to
  the Home Assistant Energy Dashboard, Victron, or an InfluxDB `difference()`
  like a counter *reset* and inject a huge phantom delta; the guard now drops a
  single downward read (the cache keeps serving the last-good value) while still
  accepting a genuine, sustained reset (meter replaced/rebooted). Enabled on the
  cumulative import/export/total registers of the bundled meter templates (ABB
  B21/B23, Carlo Gavazzi EM24, Eastron SDM120/630, Schneider iEM3000, Fronius
  Smart Meter); `net` registers are left unguarded since they legitimately fall.
- **Edge-triggered reachability logging** — a device going unreachable now logs
  one WARN and emits one `unreachable` event; recovery logs one INFO and a
  `recovered` event; the noisy per-poll-group failure lines dropped to DEBUG. On
  flaky Wi-Fi / an RTU-over-network bridge the log tells you *when* a link
  flapped instead of burying it in repetition.
- **Publish max-interval heartbeat** — `mqtt.heartbeat_interval` (0 = off,
  default): in `changed` mode a steady value is republished after N seconds so
  it keeps a fresh timestamp and Home Assistant does not grey the entity out.
- **Explicit per-register Home Assistant typing** — a register may now declare
  `device_class`, `state_class`, `entity_category`, `enabled_by_default`,
  `icon`, and `suggested_display_precision`; each overrides the unit heuristic,
  and the literal `"none"` suppresses an inferred class. The two discovery
  builders now share one typing helper. Diagnostic registers (model id, firmware
  revision, serial) on the Fronius Smart Meter template are typed as
  `entity_category: diagnostic` with no `state_class` — a text serial no longer
  ships an invalid `measurement` state class. Reactive-energy counters (`kvarh`,
  `kVAh`) are now correctly `total_increasing` instead of `measurement`.

## 3.6.0

### 2026-08-12 — Canonical field naming, auto-canonicalize, reliability hardening

**Uniform field names across every device.** A register's `name` is now drawn
from a canonical dictionary (`docs/canonical-fields.md`, 56 fields) so the same
physical quantity is named the same everywhere — `voltage_l1_n` on every meter
instead of `ull_0` on one and `v_l1` on another. MQTT topics are hierarchical
(`voltage/l1_n`), the InfluxDB field stays the flat canonical name, and
auto-select applies both from the dictionary. A template opts in with
`"canonical": true`; non-canonical or duplicate names surface as load warnings.
All bundled vendor maps (ABB B21/B23, Carlo Gavazzi EM24, Eastron SDM120/630,
Schneider iEM3000) + the Fronius Smart Meter template are canonical.

- **Auto-canonicalize** — one click infers canonical names for a cryptic
  register map from each row's label/name/unit, via a conservative server-side
  classifier (`/api/canonical-fields/guess`) that returns *nothing* when unsure
  rather than a plausible-but-wrong name (verified 0 wrong renames across the
  real vendor maps). In the register + template editors: datalist autocomplete,
  an inline "did you mean …?" hint, and auto-filled MQTT topic + measurement;
  CSV import reports the canonical count.
- **Virtual-meter fail-safe** — `on_stale: fail` option so a vanished/renamed
  source register makes the meter fail safe instead of serving a stale/0 value
  as live into a downstream ESS.
- **Reliability** — the per-device connection lock is released during a read's
  retry backoff, so a slow poll group no longer stalls the realtime group;
  WebSocket broadcast fans out concurrently; DNS resolution is bounded by the
  device timeout; an HTTP fetch that overruns its interval backs off instead of
  tight-looping; HA discovery clears the retained config of a removed register
  (no ghost sensor).
- **Maintainability** — device commissioning + config endpoints extracted from
  `create_api` into `routes/` modules; the soft-delete lifecycle moved to a
  `TombstoneStore` collaborator; the config section setters collapsed to one
  helper. New test coverage for the backfill tool, the tombstone store, and the
  canonical classifier (adversarial cases).
- **Open-source readiness** — `SECURITY.md`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, and GitHub issue/PR templates; internal engineering
  notes kept out of the published tree.

## 3.5.0

### 2026-08-11 — Modbus RTU as a first-class citizen + serial-over-TCP bridge

RTU now has two modes, both shipped and validated live against a real Fronius
Smart Meter 65A-3 (rebranded Carlo Gavazzi EM24) on an FTDI USB-RS485 adapter.
See [`docs/rtu-serial.md`](docs/rtu-serial.md).

- **New `rtu-tcp` transport** (`modbus_client._build_client`): `ModbusTcpClient`
  + `ModbusRtuFramer` tunnels RTU frames over a raw TCP socket, so a device can
  reach an adapter through a network bridge instead of a local `/dev` line.
  Wired through device validation, the ad-hoc probe (Test connection) and
  bus-trace (decoded as RTU wire framing).
- **`serial-bridge` service** (ser2net + a Python supervisor): exposes each USB
  serial adapter as a **stable internal TCP endpoint**, keyed by USB serial or
  (serial-less CH340) physical port-path, persisted across replug/restart.
  Unprivileged (`/dev` bind + cgroup rules), hotplug-aware (udev + periodic
  reconcile), data ports internal-only, control API on localhost. A
  `BRIDGE_EXCLUDE` list guarantees an adapter owned by another service (e.g. a
  BMS) is **never** opened by the bridge.
- **MBG stays unprivileged**: the primary path no longer maps `/dev` — RTU goes
  over the bridge. Direct mode (`devices:` + `protocol: rtu`) remains for static
  setups.
- **Wizard**: Modbus RTU enabled with a mode sub-toggle — *Over network
  (auto-detect)* (default) with a **Scan** button + adapter dropdown that binds
  the device to an adapter's stable endpoint, and *Direct serial*. i18n EN+RO.
- **New endpoints**: `GET /api/bridge/adapters` (scan, degrades gracefully when
  the bridge is down), `GET /api/serial-ports` (local `/dev` for direct mode).

## 3.4.2

### 2026-08-01 — quality_block word-tearing fix (shared datastore lock)

Closes the last open P2 from the v3.4.0 audit. Under `quality_block` the vmeter
serves from a `ModbusSparseDataBlock`, which writes a multi-word value
**word-by-word**; the supervisor's block rebuild (`_push_to_ctx`) ran outside
any lock the server's read path (`getValues`) shared, so a consumer reading a
2-word float32 mid-rebuild could observe a torn hybrid (new high word + stale
low word → a wildly wrong value into an ESS loop).

- **Shared `_store_lock`** now guards both the write (whole block rebuild) and
  the read (`getValues`) — but **only when `quality_block` is on**. The default
  `ModbusSequentialDataBlock` writes each value as one atomic slice, so its hot
  read path stays **byte-identical and lock-free** (zero production impact —
  quality_block is opt-in and off in production).
- **Validated under load** (isolated harness, quality_block ON vs OFF, up to
  400 clients): the lock adds **no measurable latency** — quality_block-with-lock
  and quality_block-without-lock are statistically indistinguishable (p99
  ~40–72 ms, high variance, overlapping). The ~2× slowdown vs the sequential
  block at high client counts is the **sparse block's inherent cost** (dict vs
  slice), present with or without the lock, and irrelevant at production scale
  (~2 clients/vmeter → ~2 ms either way).
- **Deterministic race test** (`test_quality_block_no_word_tearing_under_concurrency`):
  drives the real `_push_to_ctx` + `_store_lock` with an injected inter-word
  delay; with the fix, zero torn reads; without the write-side lock, ~3M torn
  reads/second.

## 3.4.1

### 2026-07-31 — hardening pass from the full-system audit (2 independent audits, adjudicated)

Remediation of the confirmed findings from the v3.4.0 full-system audit (both
audit reports cross-checked claim-by-claim in an internal adjudication). No
unconditional P1 survived v3.4.0; this closes the cluster of conditional
fail-opens and data-integrity gaps.

**Freshness / ESS-safety (the wall-clock + unbounded gaps):**
- **Calc engine integrates on the monotonic clock** — `dt` for rate/integral
  formulas was `time.time()`-based, so a wall step (the 2026-07-28 chrony +138s
  class) between two runs corrupted the derived series written to InfluxDB. Now
  `time.monotonic()`. (No live calc used it yet, but a shipped preset would.)
- **Derived freshness bound is capped** (`MAX_DERIVED_STALE_S = 300s`) — a
  misconfigured huge poll interval could otherwise relax the auto-bound to
  hours and serve dead data as "fresh" to the ESS. Explicit per-row
  `stale_after_s` stays uncapped.
- **`update_instance` rejects non-finite bounds** (`math.isfinite`) like
  `add_instance` — an `inf` `stale_after_s` disabled the freshness watchdog.
- **Modbus write rejects NaN/Infinity** — they survived the min/max envelope
  (every comparison is False) and `struct.pack` would ship the bit pattern to a
  real device. Rejected 422 up front (the encoder's NaN sentinel path is
  untouched — it never comes from a write).

**Fail-closed security:**
- **MQTT TLS fails closed** on both directions (publisher + input) and both
  discovery probes — a `tls_set()` failure no longer falls through to a
  cleartext connect. (Production runs `tls_enabled: false`, so this is a no-op
  live.)
- **Operator role gets URL redaction** too — only admin now sees raw
  URL-embedded credentials from `/api/config`, `/api/config/influxdb`,
  `/api/devices` (was viewer-only).
- **Passkey import guard** — a merge-import (`/api/config/import`) no longer
  silently overwrites `passkeys.json` from a possibly-foreign bundle; only a
  full trusted restore installs it.

**Persistence / integrity:**
- `save_selected_registers` now holds `_file_lock` (the primary shares its file
  with the locked energy/calc writers).
- Backup bundle **includes deleted-device tombstones** (`devices/<id>/device.json`)
  and restores them — export→restore no longer loses delete→restore recovery.
- `events.jsonl` is **0600 + fsync** (was world-readable 0644, unsynced).
- Genuinely non-atomic writers fixed: user device templates and the LKG-meta
  sidecar now temp+fsync+rename.

**Deploy / resource:**
- `docker-compose.yml` uses `${UI_PORT:-8080}` — `docker compose up` no longer
  aborts on a clean host with an unset `.env`.
- WebSocket connections capped (`MAX_CONNECTIONS = 64`) — refused past the cap
  instead of accumulating to FD exhaustion.
- `Dockerfile.test` quotes the `pip install "pytest>=…"` pins (the shell was
  eating them as redirections).
- Corrected the false "no word-tearing" comment on the vmeter datastore write.

**Deferred (documented, not in this release):** HA retained-discovery orphan
tombstoning for removed vmeters/deselected registers (P2, device-delete is
already handled); non-root container + image-digest pinning (need a live-volume
`chown` at deploy — deploy-coordination); P3 docs/version stamps, ~5 dead
methods, 36 unused i18n keys. Verified false positives (rejected): Flux
injection in `/api/history` (validated/escaped) and forced org-admin InfluxDB
token (`ensure_bucket` fails gracefully).

## 3.4.0

### 2026-07-31 — automatic per-row freshness bound from the poll-group interval

The 3.3.4 per-row gate exposed a tuning burden: rows fed by a slow poll
group had to carry a hand-set `stale_after_s` or the meter flapped. 3.4.0
derives that bound automatically:

- **Drivers stamp the poll cadence**: Modbus and HTTP pollers add
  `interval` to every batch item; the store keeps it per entry (push/MQTT
  sources have none). Calculated registers inherit the SLOWEST input's
  interval (a result cannot refresh faster than its slowest input).
- **The provider derives the bound**: `_lookup` returns
  `(value, mono, 2.5 × interval)` — 2.5× tolerates one missed poll plus
  jitter. For dotted cross-device sources the device threshold and the
  cadence bound are combined with max() (both exist to prevent
  false-stale flapping).
- **The vmeter applies a loosening-only cascade** (`_row_bound`): an
  explicit row `stale_after_s` wins outright (may tighten or relax);
  otherwise a derived/source bound may only RELAX the instance floor —
  a realtime row's 0.625s cadence bound must never override the 15s
  instance bound, or a single hiccup would flap the meter.
- **Templates simplified**: the hand-tuned `stale_after_s: 150` rows added
  at the 3.3.4 deploy are removed — the derived bound computes the same
  150s for the 60s slow group, now automatically for any future row.

## 3.3.4

### 2026-07-31 — security-hardening audit remediation

Point-by-point remediation of the 2026-07-31 security-hardening audit
(all findings independently re-verified against v3.3.3 first):

- **P1 — legacy freshness is now fail-CLOSED per row.** The default
  `on_stale: legacy` mode encoded individually-stale rows and judged only the
  NEWEST timestamp, so "A fresh + B expired" kept the server up serving B's
  old words as live to an ESS. Every row that resolves to a value is now
  judged against its own bound cascade (row → source device → instance) and
  one stale/unstamped row stops the whole meter. Missing rows keep the pinned
  gap contract (the production EM24 depends on it); legacy still never
  refuses individual reads. `health_state` follows the supervisor's gate
  immediately instead of waiting for `_last_fresh_ts` to age out.
- **Export/import round-trip regression (introduced in 3.3.3) closed both
  ways**: the sanitized export now also redacts `rest_push.url` (device +
  root) and `influxdb.url`; the merge-import recognizes a URL that is exactly
  the live URL's redacted form and keeps the live original
  (`connection.url`, `rest_push.url`, `influxdb.url`) — a sanitized
  export→import no longer overwrites a working URL with `?api_key=***`.
- **Calculated values keep their timestamps end-to-end**: the calc batch now
  carries `ts`/`mono` (same shape as the poller batch), so InfluxDB stamps
  the point with the OLDEST input's time instead of falling back to `now()`
  for a stale result.
- **Lost-update races serialized**: virtual-meter instance mutations
  (add/update/remove/enable) run under a config lock; builder profile
  save/delete under a profile lock; `save_calculated`, `save_energy_fields`,
  `save_device_poll_groups` and `save_calculated_templates` now hold
  `_file_lock` like the register writer.
- **Template handling hardened**: `_start_one` and the two friendly-name
  fallbacks resolve templates through `_template_path` (a hand-edited
  `template: ../x` can no longer read outside the templates dir); the
  template editor's YAML dump escapes free text (name/source/note) with
  JSON-string quoting, so quotes/newlines can't corrupt the file.
- **Route validation**: discovery unit-sweep/SunSpec/MQTT-browse now
  range-check ports (1..65535) like the scan routes; builder `generate`
  validates `registers` as a list of strings and maps TypeError → 422;
  `package_import_url` restricted to http(s)/github without embedded
  credentials; artifact `file`/`download` params validated and the
  Content-Disposition filename sanitized; the builder stream's `port` accepts
  only `OTA` or a `/dev/...` serial path.
- **Secret redaction**: viewer-role `GET /api/config` and
  `/api/config/influxdb` redact URLs (same convention as `/api/devices`);
  env-override reporting redacts `INFLUXDB_URL`; audit entries redact URL
  VALUES (not just secret key names) and a URL-shaped `target`; the ESPHome
  dashboard URL rejects embedded credentials on save and is redacted in
  settings/status responses.
- **Cleanup**: the ad-hoc MQTT test tears down `loop_stop()`+`disconnect()`
  in a `finally`; per-poll-group `age_s` moved to the monotonic clock;
  http_client's write-only `last_success_ts` removed; `ClockStepGuard`'s dead
  `grace_s` parameter removed; stale "grace window"/"unix_ts" docstrings
  corrected; the vacuous `in_grace` source-text assertion replaced — freshness
  tests are now behavioral (legacy gate, future-stamp rejection in
  `_rebuild_block`/`json_view`/`health_state`).

Not addressed (deliberate): ~40 unused legacy i18n keys (en/ro key sets are
equal; removal is churn without behavior change).

## 3.3.3

### 2026-07-31 — dead-code + loose-ends cleanup (final audit pass)

A dead-code/consistency audit of the day's changes; every finding verified:

- **Restored docs/API.md** — it had been accidentally truncated to 0 bytes
  during the version bumps while six docs still linked to it.
- **Removed ClockStepGuard dead code** — after freshness went monotonic,
  `freshness_now`/`in_grace`/`_offset`/`_grace_until`/`_grace_ceiling`/
  `MAX_TOTAL_GRACE_MULT`/`grace_s` had no production callers; the guard is now
  a minimal step-detector for the diagnostic `clock_step` event.
- **http_client**: `last_success_mono` was stamped even on a FAILED connect
  (would read fresh after a failure) — now only in the success branch; dropped
  a dead `now = time.time()`.
- **`/api/meters` staleness** moved to the monotonic clock and now fails closed
  (was wall-clock ISO, and failed OPEN on a malformed timestamp).
- **`add_instance`** validates the template id through `_template_path`
  (blocks a `../` traversal that bypassed the id regex).
- **`forget_restorable_device`** now runs under the device-mutation lock
  (could race a concurrent restore on the same dir).
- Generated firmware and Modbus/MQTT discovery now range-check ports
  (1..65535); the ESPHome dashboard client closes its login error response;
  sanitized export redacts device connection URLs; builder settings/profiles
  no longer double-audit; several stale comments/docstrings corrected.

597 tests pass.

## 3.3.2

### 2026-07-31 — senior self-review of today's releases

A critical pass over the 3.1.x–3.3.1 changes caught the tail of the
clock-step class the monotonic switch had left behind:

- **Fixed a wall/monotonic mix the 3.3.0 age-display fix missed**: the stale
  event message ("last fresh N s ago") still subtracted a now-monotonic
  timestamp from `time.time()`, so a genuine stale event logged a ~1.7e9-second
  age. It now uses the monotonic clock.
- **Dropped the freshness grace window entirely.** With monotonic freshness a
  real stale is real regardless of any clock step, so gating the stop on the
  grace window was not just unnecessary but mildly harmful — it could delay a
  genuine fail-safe stop for a source that died during a step. The stop now
  fires immediately; the ClockStepGuard is kept only for its diagnostic
  `clock_step` event.
- **Closed the class on the SOURCE side too**: the Modbus/HTTP/MQTT drivers now
  judge their own staleness (and the device-down alert that follows) on the
  monotonic clock, so an NTP step no longer false-marks a healthy source
  down or fires a spurious device-down alert. Absolute `last_success_ts`
  stays wall for display.

604 tests pass, incl. adversarial tests that jump the wall clock and prove
both the virtual-meter freshness and the driver staleness are unaffected.

## 3.3.1

### 2026-07-31 — minor P3 batch (audit cleanup)

Closing the low-severity remainder of the audit, each verified in code:

- **Modbus poller** no longer publishes a late batch after `disconnect()` —
  it rechecks `self.running` between the read and the publish (matching the
  HTTP poller), so a slow read during an edit can't push a stale batch into
  the store the virtual meters serve.
- **ESPHome discovery** no longer reports a silent (timed-out) TCP service on
  port 6053 as an "encrypted ESPHome device"; a timeout is now distinguished
  from a connection reset and the host is dropped, not mislabeled.
- **Operator role** can no longer reach `DELETE /api/devices/restorable/<id>`
  when the id is literally `write`/`test`/`payload-sample` (the live-action
  matcher now excludes the admin-only `restorable` sub-tree).
- **Audit hygiene**: node-YAML config saves (which self-audit key-names only)
  are excluded from the generic body capture, so a pasted literal
  `wifi_password:`/OTA key inside the YAML never lands in `audit.jsonl`.
- **FD hygiene**: the ESPHome dashboard client closes HTTP error responses,
  and the InfluxDB backfill closes its urlopen response.

603 tests pass.

## 3.3.0

### 2026-07-31 — monotonic freshness + serialized device mutations

Two structural hardenings that close whole classes of finding for good.

**Monotonic freshness clock (definitive clock-step fix).** Every driver now
stamps a monotonic timestamp (`mono`) alongside the wall measurement time;
the store and calculated registers carry it (calc inherits the oldest input's,
None when none), and the virtual meter judges freshness as
`time.monotonic() - mono <= bound` at every site — the Modbus block, the
quality-block age, the supervisor's legacy stop, `json_view` and
`health_state`. Because `time.monotonic()` is unaffected by wall-clock/NTP
steps, freshness is now IMMUNE to a step of any size in either direction by
construction — no rebasing, no grace window needed for correctness. A value
with no monotonic stamp fails closed (not-fresh). The wall `ts`/`timestamp`
fields are unchanged (display/InfluxDB). The ClockStepGuard remains only to
log a `clock_step` diagnostic event; it no longer gates the freshness verdict.

**Serialized device mutations.** create/update/delete/restore each do a
check-then-act across validate → persist → client-swap → registry; they now
run under one lock (via a signature-preserving decorator), so two concurrent
requests can't orphan a client's poller threads or leave 2× pollers
double-publishing every value.

598 tests pass, incl. an adversarial test that lies about the wall clock and
proves the freshness verdict depends only on the monotonic clock.

## 3.2.0

### 2026-07-31 — Lot F: audit hardening (P2 + P3 batch)

Working through the thorough audit's remaining findings (each verified in
code; the two P1s shipped in 3.1.6). 597 tests pass.

**Security / secret hygiene**
- Viewer roles no longer see credential-bearing URLs: `http_url`, the REST
  sink url and `rest_push.url` are redacted in the device list; `/api/status`
  redacts `http_url` for every role; the alerts `webhook_url` is redacted for
  non-admins.
- Snapshot ZIPs, the LKG bundle and restored config files (config.yaml,
  passkeys.json) are written `0600` — no more world-readable secrets in
  `config/snapshots/`. Existing `audit.jsonl`/`passkeys.json` are tightened
  to `0600` on load.
- Importing a sanitized backup no longer wipes per-device secrets or the
  webhook token: the merge refills stripped `connection.password`/headers and
  `rest_push` headers per device, and keeps the live `webhook_url` when the
  imported one is its stripped prefix.
- The IP allowlist now also gates the unauthenticated SPA shell; device
  restore re-validates the tombstone through the create-time SSRF/LAN checks;
  a non-ASCII login username no longer crashes the auth path (which would
  have skipped lockout accounting).

**Reliability / correctness**
- The clock-step grace window is now bounded — a clock stuck in a correction
  loop can no longer suppress the freshness fail-safe indefinitely.
- `add_instance` rejects non-finite/non-positive `stale_after_s`/`max_hold_s`
  (an `inf` bound would defeat the watchdog), matching `update_instance`.
- Virtual-meter `stop()` joins its supervisor and the restart branch rechecks
  the stop flag — a disable/delete mid-tick can't orphan an unsupervised
  server on the port.
- Config-file writes hold the file lock (no half-written `config.yaml` from
  two concurrent saves). The HTTP-JSON `json_view` reads the step-rebased
  freshness clock. `PATCH` is now audited and snapshotted. ESPHome discovery
  validates the port range.

Deferred (noted for a dedicated change): full device-CRUD mutation
serialization (needs a careful handler refactor) and end-to-end monotonic
freshness timestamps (the definitive fix for the whole clock-step class).

## 3.1.6

### 2026-08-01 — two P1s from the thorough audit round

- **Path traversal → arbitrary file write via the Energy endpoint (P1).**
  `POST /api/energy/fields?device=<id>` fed the id straight into
  `device_registers_path`, which joined it raw — `?device=../../../tmp/x`
  wrote a JSON file outside the config tree (unauthenticated on the
  trusted-LAN default). The 3.1.3 `_safe_device_id` guard covered the
  tombstone callers but NOT this path. Fixed at the chokepoint:
  `device_registers_path` now validates the id as a single safe segment
  (protecting all seven callers), and the energy endpoints 404 an unknown
  device.
- **Missing-timestamp staleness laundering (P1).** A value with no driver
  measurement time had its store timestamp fabricated as `now()` (in both the
  poller store-write and the calculated-register path), which the vmeter then
  read as fresh — defeating the fail-safe. The store now carries a separate
  numeric freshness clock `ts` (None when the driver gave no time; calculated
  registers inherit the oldest input's), and the vmeter reads it and fails
  closed on None. Display timestamps are unchanged.

590 tests pass.

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
