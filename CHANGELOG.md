# Changelog

## 3.32.0

### 2026-08-15 — lifecycle serialization + test-quality (backlog §I CLOSED)

The last items of the externally-audited backlog section. Nothing here
touches the served vmeter frames; the theme is "no doubled work, no
hammering, no lying tests".

- **Poller lifecycle is serialized + idempotent** — `start_polling` /
  `reconnect` / `reload_registers` / `disconnect` used to interleave under
  concurrent Applies, leaving a DOUBLED poller set (every group polled
  twice — double bus load). A lifecycle RLock serializes them on both the
  Modbus and HTTP clients, and a second `start_polling` stops the existing
  set first instead of appending. MQTT input clears its monotonic filters
  on a live register swap (a re-mapped address must not inherit a stale
  counter baseline).
- **`read_bits` shares `read_registers`' failure tail** — the coil/discrete
  path had NO wedge backstop (a wedged-but-open link never forced a
  reopen) and declared "unreachable" on EVERY failed batch — the exact
  event-ring flapping the register path was cured of in DP-8.
- **Connect-refused backoff follows `modbus.retry_delay`** — was a
  hardcoded 100 ms, hammering a down device with connects at 10/s per
  poll group (live config: 1 s).
- **vmeters routes are deliberately sync** — they join server threads
  (stop/reload can block seconds); as `async def` those joins ran ON the
  event loop, freezing every request incl. `/ws`. Sync `def` routes run
  in FastAPI's threadpool; a module note guards against re-asyncing.
- **Shutdown disconnects devices in parallel** (bounded 8 s join) —
  sequential 5 s-per-device joins could outlast docker's stop grace
  period, and the SIGKILL lost the InfluxDB replay buffer flush.
- **Test-quality** — the interval-clamp test exercises the real
  constructor (it used to re-implement the expression and assert on its
  own copy); `test_input_registers_rejected` was a PERMANENT skip (the
  fixture never had a non-primary device) and now actually proves FC4
  writes are refused; 5 of 8 `inspect.getsource` string-asserts replaced
  with behavioral tests — both authorization-boundary ones (operator
  write-matcher segment anchoring, tombstone-forget edge) now drive a
  real app with a real operator session (403 = the role gate refused),
  plus esphome error-body close, poller no-publish-after-stop and
  json_view monotonic aging (the 3 kept ones assert non-security wiring
  markers). **CI runs with `pytest-randomly`** — order-dependence (the E8
  class) now fails loudly; verified locally: 3 randomized full runs
  green, 891 passed.

## 3.31.0

### 2026-08-15 — self-heal on truncated configs, sm16 encode, WS API-key (backlog §I)

Three more externally-audited gaps closed — all latent, none touch the
live serving path (vmeter frames stay byte-identical to a real meter).

- **Self-heal covers valid-but-truncated files** — YAML/JSON that PARSES
  can still be a husk (empty file, bare scalar, cut before the sections
  every save writes unconditionally). config.yaml now has a plausibility
  gate (dict with `modbus`/`mqtt`) so a husk routes through the existing
  .bad + heal-from-.good path instead of being loaded "successfully" and
  then CLOBBERING the .good snapshot with itself. selected_registers.json
  (primary AND per-device) gains the same .good/.bad contract it never
  had — a truncated selection used to silently empty every poller; now it
  heals from the last known-good snapshot. Deselect-all (`registers: []`
  key present) is still a legitimate save shape and loads normally.
- **Sign-magnitude encode + fail-loud** — `sm16`/`sm32` decode existed,
  but encode fell into the generic `_split32` fallback: TWO words for a
  one-register type, corrupting the adjacent register on write. Proper
  sign-magnitude branches added (round-trip proven against the parser in
  all four byte orders), NA sentinels defined (all-ones = max negative
  magnitude — NOT "negative zero", which decodes to a plausible 0), and
  an unhandled data type now raises instead of silently emitting int32
  words — every caller already catches and degrades the row loudly.
- **Builder stream requires the API key** — `_write_guard` is an HTTP
  middleware, so with login off the `/api/builder/stream` WebSocket
  (which can flash firmware OTA) accepted anyone on the IP allowlist even
  when a key was configured. The key is now enforced on the stream like
  on HTTP writes: `X-API-Key` header (scripts) or the
  `mbg-api-key.<base64url(key)>` subprotocol (browsers can't set custom
  WS headers; a query param would leak into access logs). The UI sends
  the subprotocol automatically from its stored key.
- Tests: 13 new; suite at 883 passed.

## 3.30.0

### 2026-08-15 — canonical Wh + counters never fail over (backlog §I)

Closes the two design gaps the external audit flagged around virtual-meter
energy semantics. Both were LATENT (no fallback twin configured, catalog
templates unused live) but armed the moment a second meter arrived.

- **Wh is the canonical energy unit** — the live ESS-critical chain
  (umg512 selection → vmeter templates → Influx/MQTT/NR/alertd/HA) was
  already Wh, while the docs, `canonical_fields.py` and all 7 catalog
  templates said kWh: the same canonical name carried units 1000× apart
  across devices. Canonical map + generated docs flipped to the Wh family
  (Wh/varh/VAh); the 7 catalog templates convert natively-kWh maps in the
  selection scale (mechanical `scale/1000`, descriptions updated); a
  conformance test pins the contract for future templates. Save-time
  validation WARNS (loosening-only) when a canonical name declares a
  non-canonical unit. New helpers: `canonical_unit_for`,
  `is_cumulative_field`. The one live offender (`fronius_rtu`, own bucket,
  no consumers) is patched to Wh in the overnight window.
- **Cumulative counters are pinned, never failed over** — device_fallback
  used to rewrite ALL live rows to the twin, including lifetime energy
  totals: a switch to a different physical meter is a non-monotonic jump
  that corrupts Victron/DataManager kWh statistics. Counter rows
  (`is_cumulative_field`) now stay on the primary with `pin_on_stale`:
  on outage they freeze at last-good (a frozen counter is a true
  statement — and an unavailable span would fail the DataManager's whole
  block read) in EVERY policy incl. the legacy gate, while instantaneous
  rows fail over; recovery resumes with a legitimate forward jump.
  Never-resolved rows still fail loudly (E1 intact).
- Tests: 9 new; suite at 870 passed.

## 3.29.0

### 2026-08-15 — ONE decode pipeline for every transport (backlog §I)

The wire→value correction logic existed in six drifted copies (the external
audit's highest-leverage finding): the Modbus poll path had every stage,
while HTTP/JSON and MQTT inputs silently skipped nan/enum/bits/monotonic
and the on-demand query returned raw-with-caller-scale only.

- **`value_decode.apply_corrections()`** is now the single pipeline —
  nan sentinel on the RAW value → enum/bits decode → raw/scale + offset →
  optional monotonic counter filter — with an `info['stage']` diagnostic
  (`sentinel` | `decode_failed` | `filter_drop`) preserving the DP-6/DP-10
  edge-triggered warnings. Every stage engages only when the register
  declares it, so existing configs decode bit-identically (verified: zero
  live HTTP/MQTT registers declare any of these features today).
- **Modbus poller** now calls the shared helper (behavior unchanged —
  golden tests pinned before the refactor).
- **HTTP/JSON poller + MQTT input** gain the full pipeline: a declared
  nan sentinel is held instead of published as a real measurement, enums
  decode to text, and a monotonic energy counter finally gets rollover
  protection on non-Modbus sources (per-register `MonotonicFilter`, owned
  by the poller — stateful stage stays with polling callers only).
- **Query now (single + batch)** returns an ADDITIVE `corrected` field
  when the queried address is a selected register — the exact value the
  poll path would publish (stateless: a debug read never advances filter
  state). The raw `value` contract is unchanged.
- Tests: 21 new (pipeline contract, per-transport feature tests, query
  `corrected`); suite at 861 passed.

## 3.28.0

### 2026-08-15 — data-path audit CLOSED (batch 3) + external audit criticals

Closes every remaining item of the internal data-path audit (DP-2, 18, 19,
21-27, 29-33, 35) and folds in the verified criticals of an independent
external audit (run against 3.25.1; adjudication:
`.audit/audit-2026-08-15-external-adjudication.md`).

- **Vmeter zero-serving closed (external E1, CRITICAL):** a template row
  whose source NEVER resolved (rename/deselect/typo) was skipped with "keep
  last value" — but there was no last value: the zero-seeded block served a
  plausible 0 W to the inverter while the meter reported healthy, violating
  the spec's own "absence is never encodable" rule. Such a row now fails the
  freshness verdict (meter withheld LOUDLY, `unresolved` event) until it
  resolves once; the pinned-gap contract still holds for rows seen before.
- **One master per bridged line (external E2, CRITICAL + DP-19):** two
  rtu-tcp devices on one bridge endpoint were accepted and then evicted each
  other forever (bridge is kickolduser). The device validator now enforces
  endpoint uniqueness like the plain-RTU branch does for serial ports, and
  Test-connection refuses an endpoint a running device is polling.
- **Retained commands never actuate hardware (external E7):** a retained
  MQTT command (one `mosquitto_pub -r` test, one HA `retain: true`)
  re-wrote the device on EVERY reconnect, passing all validation. Retained
  deliveries on command topics are dropped and cleared at subscribe.
- **Test suite no longer green by filename luck (external E8):** an
  API_KEY env leak made the auth/RBAC suite order-dependent (reproduced:
  25-82 failures under shuffling). Autouse isolation fixture; their exact
  failing order now passes.
- **Ship closed-er:** default `auth_password` "" (matches the shipped
  example) and `AuthState.reload` refuses to enable login with an
  empty/default password — covering hand-edit/import/restore, not just the
  UI route. MQTT-TLS test probe fails CLOSED. `canonical_url` escaped
  against `</script>` stored XSS. Security headers applied to the
  login-shell short-circuit. TLS private key 0600 from creation.
- **Write path is now the exact inverse of the read path:** `offset` is
  applied on every write (raw = (value−offset)×scale), threaded through
  the HA write, the manual route, the dead-man lease revert and both
  read-backs — a scale:10/offset:−40 setpoint written as 30 °C used to
  land as −10 °C, including on the safety revert.
- **MonotonicFilter is symmetric:** an implausible UPWARD step (>50%)
  needs the same coherent confirmation as a reset — a flipped high word
  no longer injects a phantom mega-delta and poisons the baseline.
- **MQTT correctness tail (DP-2/21..25):** the whole
  check→publish→confirm sequence holds the cache lock (no more racing
  same-topic writers desyncing broker vs cache); floats compare rounded
  (changed-mode no longer degenerates to all on noisy registers);
  update_config reconnects on connection-identity change (re-arms the LWT
  on the new prefix, clears the old retained status) and rebuilds
  compat_aliases; alias counters split on rc; failed HA-discovery clears
  retry; outage drops + command drops are counted.
- **Influx tail (DP-26/27/29):** replay groups per bucket (no more tiny
  interleaved writes); `_g_` stripped as a PREFIX only (interior matches
  collapsed two names into one field — verified identical on every live
  name); write_api snapshot non-blocking under the swap lock.
- **Bridge tail (DP-18/30/31/32):** dead-PID lockfile sweep runs on every
  reconcile (an orphan lock beside a LIVE ser2net used to kill the bus
  indefinitely); /health reports the DATA path (ser2net dead = unhealthy,
  and the container healthcheck uses it); portmap fsync + loud corrupt
  load + entry validation; threaded control API with client timeouts;
  zero-adapter reconciles stop rewriting config every 10s; serial params
  env-overridable (BRIDGE_SERIAL_PARAMS).
- **Store/diag (DP-35/33):** the HA write read-back store update is a
  whole-dict swap; bus_trace re-points the TransactionManager's captured
  send so /api/bus-trace works again on pymodbus 3.15.
- **WebSocket init serializes a snapshot** of the live store, not the live
  dict (a poller insert mid-dumps dropped the socket); the MQTT-input
  fan-out is exception-guarded so one poisoned payload can no longer
  silently kill the source's network thread; the boot path passes
  allow_nonlan_http_devices like the other two constructors.
- Suite: 840 passed; the external report's exact order-dependent failure
  case re-run green. Remaining external items adjudicated to backlog
  (decode-pipeline unification, vmeter unit/counter failover semantics,
  self-heal on truncated-valid YAML, sm16 encode, ws API-key gating) and
  go-public (GHCR workflow, example config, license headers, capacity
  re-run) — see the adjudication file.


## 3.27.0

### 2026-08-15 — data-path audit batch 2 (DP-5..14, 16 + DP-34/36)

- **DP-5 — orphan pollers eliminated.** The stop event is now checked
  between read-groups AND between retry attempts (M1 covered only the
  outer loop), so a stopping poller exits within one in-flight
  transaction; a config reconnect additionally RETIRES the old connection
  (reads/writes refuse it) — a timed-out join can no longer leave a second
  concurrent Modbus master reopening the old client on the same endpoint.
- **DP-6 — enum/bits decode failure holds last-good.** A corrupt
  enum/bits config (unparsable mask/shift) used to overwrite the last good
  value with a FRESH None — the vmeter never fail-closed and MQTT
  published "None". Now: skip the write (cache holds), warn once per
  register, recover silently.
- **DP-7/34 — short and empty responses are failures.** A non-error reply
  with fewer registers than requested counted as success (tail registers
  silently dropped, freshness refreshed); an empty reply retried with no
  backoff and no error count. Both now fail the batch and retry.
- **DP-8 — partial loss is visible.** Reachability is a LINK verdict tied
  to the consecutive-failure backstop (one chronically-failing batch no
  longer flaps unreachable/recovered every cycle, rotating the event ring
  in ~25s); `data_health` gains per-group staleness (`stale_groups`
  degrades the verdict — the connection timestamp is driven by the fastest
  group and hid a frozen slow group) and a `batch_failures` counter.
- **DP-10 — counter resets must be COHERENT.** Three unrelated corrupt
  reads could be adopted as a "reset" baseline (the exact phantom-delta
  the filter exists to stop). Confirming reads must now stay within 5% of
  the drop (a real post-reset counter grows minutely vs the drop); the
  adoption — previously the one traceless transition — logs a WARNING.
- **DP-11 — shutdown flush order.** close() closes the batching write_api
  FIRST (its failures land in the replay buffer), then drains, THEN
  persists — the old order snapshotted before the flush, losing the final
  batch and even unlinking the snapshot right before points landed in it.
  Reconnect join raised 2s→15s (a drain in progress raced close()).
- **DP-12/28 — the buffer window is a window of DATA.** Age pruning is now
  relative to the newest buffered point, not the wall clock — wall-relative
  pruning silently capped real outage protection at ~10 minutes (during
  the outage AND at restore, where a delayed restart threw the whole valid
  snapshot away). Defaults raised deliberately: buffer_minutes 10→120,
  buffer_max_points 50k→200k (hard RAM bound ~40 MB).
- **DP-13 — drops invalidate the change cache.** A stable value whose
  buffered point died was never written again until it physically changed;
  any drop (prune or poison chunk) now clears last-written state so
  post-recovery values re-write.
- **DP-14 — backfill heals interior holes.** The auto path only checked
  the TAIL gap, so the classic incident (Influx down an hour, live writes
  resumed before the next cron tick) was never repaired.
  aggregateWindow(createEmpty) now scans the lookback for interior holes
  and backfills the earliest one with padded bounds. (+DP-36: stale
  docstring aligned with the canonical schema.)
- **DP-16 — HA command worker survives reconnect()** (it was joined in
  disconnect() and never restarted; post-reconnect commands filled the
  queue and blamed "queue full").
- **DP-9 — identity collisions rejected at save.** Duplicate
  (register_type, address) — distinct Modbus address spaces collapse onto
  one store key, last-write-wins — and duplicate names (nondeterministic
  vmeter binding + shared MQTT topic + Influx series; the class caught
  live at Migration B) now 400 with the exact collision. Span overlaps
  only WARN: live maps legitimately read overlapping windows (fronius_rtu
  serves int32@10 alongside uint16@11) and HTTP/MQTT addresses are
  synthetic — verified against all 8 live selections before shipping.
- +13 tests (suite 840+).

## 3.26.0

### 2026-08-15 — data-path audit batch 1 (DP-1/3/4/15/17/20) + sessions + UI a11y

- **DP-1/DP-15 (MQTT liveness):** the PRIMARY device now publishes a
  retained `{prefix}/availability` topic like every other device — it was
  the only one without a liveness signal, so a dead Janitza was
  indistinguishable on the broker from a steady one, indefinitely.
  Availability now confirms AFTER a successful publish (a dropped `offline`
  retries next tick instead of leaving a false `online` forever) and the
  availability cache clears on reconnect (a broker that lost retained state
  gets fresh values). Operationally: `mqtt.heartbeat_interval: 30` enabled
  on the live config — unchanged values republish every 30s, so retained
  data can no longer be hours-old without anyone noticing.
- **DP-3 (InfluxDB silent loss):** `/ping` is unauthenticated in InfluxDB
  2.x, so a rotated/revoked token (or deleted bucket) kept `connected:true`
  while 100% of writes failed 401/403/404 and replay dropped chunks of up
  to 5000 points as "permanent". Write errors now classify 401/403/404
  explicitly: `auth_failed` flag (fires an operator alert), connected
  dropped (reconnect also re-creates a missing bucket), and replay
  re-buffers on those instead of dropping — only 400/422 (genuinely
  malformed data) still drop. New confirmed-delivery counters
  (`writes_confirmed`, `last_confirm_age_s`) from the write_api success
  callback — the enqueue counters stay green during an outage; these don't.
- **DP-4/DP-17 (serial-bridge):** the reconcile signature now includes the
  DEV PATH — an unplug+replug inside one 10s window with kernel
  renumbering left ser2net opening a dead `/dev` (or the wrong bus)
  forever. The main reconcile loop and the udev-event path are individually
  guarded: a transient OSError no longer kills the supervisor (and a
  healthy ser2net with it); the udev log message no longer blames udev for
  reconcile errors. Docstring now states the real topology: uevents do not
  reach a bridge-network netns — the 10s reconcile IS the hotplug path.
- **DP-20 (compounded timeouts):** all pymodbus clients are built with
  `retries=0` — the library default (3) multiplied with MBG's own
  retry_attempts, turning a wedged link into 12s per call / ~38s per batch
  / ~76s per 0.25s realtime cycle. MBG owns the retry policy.
- **Sessions persist across restarts** (see 04d6c52): a container
  restart/upgrade no longer logs every browser out — sessions live in
  config/sessions.json (SHA-256 hashes only, 0600), pruned at load.
- **UI accessibility/visual batch 1** (see c170a06): AA contrast inks on
  both themes, 118 label associations, 325 decorative icons hidden from
  SRs, real login dialog, keyboard theme toggle, skip-link + landmark,
  reduced-motion, one reconnect toast per outage, human-readable error
  toasts, empty-state contract.
- Full data-path audit report (36 adjudicated findings, 5 tracks):
  `.audit/audit-2026-08-15-datapath.md`. +10 tests (suite 830+).

## 3.25.1

### 2026-08-15 — non-root ergonomics + docs debt for the 3.24/3.25 wave

- **chown-then-drop entrypoints (MBG + serial-bridge).** A fresh install
  mounts a root-owned config volume, and the non-root app could read but
  never write (snapshots/saves failed with PermissionError — caught by a
  fresh-volume smoke test). The entrypoint now starts as root only to chown
  the mounted volume (`/app/config` / `/data`), then drops via `setpriv`
  (`--init-groups` keeps serial-bridge's dialout membership); the long-lived
  process never runs as root, and no manual chown is needed on install or
  upgrade.
- **Docs debt closed:** shipped docker-compose.yml + both README `docker
  run` examples gain the `ip_unprivileged_port_start=0` sysctl (with the
  drop-them-together note for :502); upgrade-guide gains a "Non-root
  containers (3.24.1+)" section (symptoms included); config-reference gains
  the top-level `config_version` row and keeps the `compat_aliases` row.

## 3.25.0

### 2026-08-15 — Migration B: `mqtt.compat_aliases` (topic-migration dual-publish)

- **New `mqtt.compat_aliases`:** every publish whose topic starts with `from`
  is ALSO published under `to`, with per-leaf renames — old consumers keep
  receiving byte-identical topics during a prefix migration (zero-gap). One
  choke point in `_publish` covers register data, vmeter state, data_health,
  status and alerts; `publish_state` now funnels through `_publish` for
  exactly that reason. Counted in stats (`messages_aliased`); survives a
  config load→save round-trip. +6 tests; documented in config-reference.md.
- Purpose-built for the janitza→meters prefix flip (the live config change
  is operational, not in this repo): primary prefix `janitza/umg512` →
  `meters/umg512`, four energy leaves canonicalized (`consumed`→`import`,
  `delivered`→`export`, `iqh4`/`cqh4`→`reactive/import|export`), and the
  latent per-phase energy leaf collision (six disabled registers all
  pointing at the total's topic) fixed while in there.

## 3.24.2

### 2026-08-15 — config version stamp (audit MEDIUM-2)

- **Every save stamps `config_version`** (the writing gateway's version) as
  the first key of config.yaml. Loading a file stamped by a NEWER gateway —
  a downgrade — warns loudly, raises a rate-limited `config-downgrade`
  alert, and surfaces in `/api/status` → `config_status.written_by[_newer]`:
  the next save from the older version silently drops every setting the
  newer one introduced, and that could previously happen with no trace.
  Load itself is never blocked (fail-open, malformed stamps count as 0).
  +4 tests; documented in upgrade-guide.md.

### 2026-08-15 — stack ops (docker-setup, same window)

- Memory limits: esphome 2g (firmware compiles spike), grafana 1g,
  image-renderer 1g (influxdb already capped at 6g since 2026-07-15).
- Image pins: esphome → 2026.5.3, grafana → 13.0.2 (both match the running
  versions), image-renderer → by digest (the Go renderer publishes no
  readable version tag). No more surprise majors via watchtower on :latest.

## 3.24.1

### 2026-08-15 — non-root containers (audit P2)

- **MBG runs as uid 10001 (`mbg`), non-root.** The app tree stays root-owned
  read-only; the only writable path is the mounted `/app/config` (host dir
  chown'd once to 10001). The privileged `:502` bind is solved with the
  compose-level per-container sysctl `net.ipv4.ip_unprivileged_port_start=0`
  — scoped to the container's own network namespace, no capability grant,
  no host-wide sysctl.
- **serial-bridge runs as uid 10002 (`bridge`), non-root, in `dialout`**
  (gid 20 matches the host's group on the bound tty nodes). Its writable
  paths got explicit ownership: `/etc/ser2net/` (config moved out of `/etc`
  proper — the atomic rewrite needs directory write permission;
  `SER2NET_CFG` env-overridable), the UUCP lock dirs (stale-lock sweep +
  ser2net's own lockfiles), `/data` (state volume, host-chown'd). The `/dev`
  bind is now **read-only** (blocks node creation from inside; tty I/O on
  existing nodes is unaffected) on top of the existing tty-only
  device_cgroup_rules. Hotplug verified non-root (udev watch + the periodic
  reconcile fallback); Seplos exclusion intact.
- Both smoked as throwaway containers before the live window; live-verified
  after: meters fresh, EM24 decodes, consumers reconnected, volume writes
  land under the new uids.

## 3.24.0

### 2026-08-15 — pymodbus 3.6.9 → 3.15.0: vmeter serving core on SimData/SimDevice

- **Dependency:** pymodbus pinned to 3.15.0 (line 3.6 unmaintained ~2 years;
  MBG exposes Modbus TCP servers on the LAN, and the 3.7+ line carries the
  fully rewritten framing/transaction layer those servers should be parsing
  with). Client-side renames: `slave=` → `device_id=`, `ModbusRtuFramer` →
  `FramerType.RTU`.
- **Vmeter server core rewritten on the SimData/SimDevice model.** 3.13+
  reduced the classic datastore (`ModbusSlaveContext`/data blocks) to a
  deprecated shim that deep-copies its values at server build — live block
  updates would silently never reach clients (a frozen meter, exactly the H1
  failure class). The serving core now builds a `SimDevice` directly:
  - live updates mutate the runtime's registers list in place — one atomic
    slice per value, read back as one slice, so the tear-free contract holds
    lock-free even under quality_block (the old sparse path needed a
    read/write lock);
  - the two-island map (meter map + quality block) uses SimData's native
    undefined-address refusal — reads below/outside/between islands get
    illegal-address, preserving real-meter probing (DataManager
    disambiguation);
  - `SimDevice(id=0)` answers on any unit id (the old `single=True`);
  - one instrumentation choke point (wrapped `async_getValues`/`setValues`):
    per-read stats, the 'fail'-policy span refusal, the read-only write
    refusal, and a new coils/discrete refusal (the shared block would
    otherwise serve register bits where a real meter refuses).
  Verified semantics-equivalent on the wire by a 12-point prototype before
  the rewrite; the P2 word-tearing and read-only tests were rebuilt against
  the real new mechanisms (granularity + end-to-end client writes).
- Load-test device fleet (`loadtest/sim_devices.py`) migrated to the same
  model; churn now mutates the runtime registers directly.
- Suite: 806 passed on 3.15.0, ruff clean, coverage gate met.

## 3.23.7

### 2026-08-14 — audit M3+M4: `enabled` default symmetry, HA writes off the paho thread

- **M3 — a config row missing `enabled` now counts as ON on every path.**
  Boot (`start_all`) has always defaulted a missing `enabled` key to on, but
  `_reload_instance` and `update_instance` read the key with no default: a
  hand-edited/legacy row started fine at boot, then the next template save
  silently STOPPED the meter (reload stops first, checks after), and an
  instance PATCH refused to live-restart it. One predicate
  (`_inst_enabled`, default on) is now the single source of truth for all
  seven readers. Also **`set_enabled(on=True)` rolls back on start failure**:
  a bad port/bind used to answer HTTP 500 with `enabled: true` already
  persisted — the next boot tripped over the same bad config; the flag now
  reverts (mirroring `update_instance`'s revert) and the route answers 400
  ("enable reverted"), keeping 404 for an unknown instance. +2 tests.
- **M4 — HA write commands run on a dedicated worker, not paho's network
  thread.** `_mqtt_write_command` does blocking Modbus I/O (write + read-back)
  under the connection lock shared with the pollers; executed inside
  `on_message`, one slow/retrying RTU device stalled the entire MQTT sink —
  publishes, subscriptions and keepalives included. `_on_message` now only
  decodes and enqueues; a single `mqtt-command-worker` thread drains commands
  in arrival order (writes stay serialized). The queue is bounded (32): a
  broker flood drops commands with a warning instead of building an unbounded
  backlog of stale writes. Worker joins on `disconnect()`. +4 tests
  (including an off-thread execution proof).

## 3.23.6

### 2026-08-14 — audit M1+M2: poller stop race, live-store ghosts (evening batch)

- **M1 — lost-stop race in the pollers.** A `stop()` landing between
  `Thread.start()` and the first line of `run()` was overwritten by
  `running = True`; with the stop event ALREADY SET, `wait(interval)` returned
  instantly — an unkillable zombie poller issuing back-to-back Modbus reads
  through the OLD callback (worst on the WiFi/RTU bridge). The stop event is
  now the single source of truth in both `RegisterPoller` and the HTTP
  `_JsonPoller` (`running` stays as an observability mirror). The HTTP poller
  also drops its uninterruptible `time.sleep` (audit L2): a 60 s group's
  thread no longer outlives its device by a full interval. +5 tests.
  (The jitter tests stopped their poller by flipping the `running` flag
  directly — under the new contract that no longer stops anything, and the
  first full-suite run OOM'd at 14 GB from a poller spinning against a
  call-recording MagicMock. They now use the real `stop()` primitive —
  exactly the single-stop-primitive discipline M1 introduces.)
- **M2 — live-store entries at deselected addresses are purged on register
  reload.** A template re-select that moves a register to a new address while
  keeping its canonical name left the OLD entry frozen in the store;
  name-based lookup binds the first match in insertion order, so vmeter rows
  read the ghost's frozen timestamp and fail-stopped the whole meter until a
  process restart. `purge_deselected()` now runs on every reload path
  (per-device re-select, primary re-select, /api/config/reload-registers,
  apply) — mirroring the calc-address purge that already existed. +4 tests.
- **Failover/lifecycle test coverage** for the 3.21–3.23 surface that shipped
  thinnest (manager 49%, routes 27%): instance add/edit/toggle/delete with
  `device_fallback` through the REST routes, template editor round-trip, the
  3.23.0 published-state contract (policy+bounds+quality+routing), and an
  end-to-end primary-stale→twin→recovery switch. +8 tests.

## 3.23.5

### 2026-08-14 — audit P1 batch: reproducible builds, gates, self-contained UI, docs debt

- **Reproducible builds:** `requirements.lock` (exact pins, the set the live
  image runs) is now what the Dockerfile and CI install; `requirements.txt`
  stays the intent file. Dependabot watches pip/docker/actions weekly.
- **CI gates:** ruff lint (config in `ruff.toml`; F-rules enforced, deliberate
  compact-style E7s excluded), coverage floor 72% (`--cov-fail-under`),
  per-test `--timeout=120` so a hung server fails in minutes not hours.
- **Self-contained UI again:** bootstrap-icons vendored under
  `ui/vendor/bootstrap-icons/` (was a cdn.jsdelivr.net runtime dependency —
  broken icons on air-gapped installs, a supply-chain surface, and a CDN
  beacon from every operator's browser); CSP tightened back to self-only.
- **Ad-hoc Modbus probe LAN-guarded:** `/api/devices/test` now applies the
  same LAN-egress policy as every discovery route — it doubled as an internal
  TCP port-scanner (audit L1). +2 tests.
- **Durability:** fsync-before-rename in the three atomic writers that lacked
  it (`virtual_meters.yaml`, snapshot index, builder profiles) — power loss
  can no longer zero them.
- **sm16/sm32 end-to-end:** accepted by the template validator and the CSV
  alias table (they decoded fine but a community map using them previewed OK
  and then failed on save). +1 test.
- **Healthchecks:** the image healthcheck follows `UI_PORT` (a non-8080 deploy
  was permanently "unhealthy"); serial-bridge Dockerfile gains its own
  healthcheck (`GET /adapters`).
- **Docs debt closed:** `alerts-webhooks.md` rewritten (it denied the 3.8.0
  threshold engine); MANUAL RO/EN at full parity (Device Builder ported to EN,
  failover/device_fallback/staleness state, opt-in decode+transport options);
  `architecture.md` describes the 3.23-era system; new `config-reference.md`,
  `upgrade-guide.md`, `yaml-import.md`; API.md gains canonical-fields,
  import-yaml, `device_fallback`; READMEs tell the truth about env
  pass-through (compose ships those lines commented) and count 11 templates.
- Lint sweep: unused imports/variables removed across the tree (ruff --fix).

## 3.23.4

### 2026-08-14 — build hygiene + janitza-monitor name retired

- **`.dockerignore` added** (audit 2026-08-14). The build context shrinks from
  ~54 MB to the shippable tree, and — the structural part — `COPY config/`
  can no longer bake runtime state into an image: a tree where the app has run
  carries `config.yaml` (live credentials), `passkeys.json`, snapshots and
  audit logs, and the previous image had 51 snapshot ZIPs + a test-run audit
  log baked in. Only `config/templates/` and the `*.example.*` files ship.
- **Legacy `janitza-monitor` references retired** across the repo: HA discovery
  `manufacturer` is now `multi-bus-gateway` (identifiers/via_device untouched —
  those are stable HA registry keys), tool paths and doc-comments point at the
  migrated data dir (`/docker-storage/pv-stack/multi-bus-gateway/`), bundled
  template author renamed. Deployment side (same date): data dir renamed,
  compose paths/env (`MBG_UI_PORT`/`MBG_API_KEY`), legacy network alias
  dropped (no consumer left), backfill cron path fixed (was silently dead —
  called a script location that no longer existed), MBG config added to the
  nightly stack backup (it was in no backup scope at all).

## 3.23.3

### 2026-08-14 — security: close the S4 redaction residual (audit)

- **`Cookie`/`session` values are now masked** in the audit log and everywhere
  else `redact_obj`/`_SECRET_KEYS` applies: an HTTP-input device configured
  with a `Cookie:` auth header landed verbatim in `audit.jsonl`.
- **Scalar URL leaves in the snapshot diff are masked.** `_mask()` deep-redacted
  dict/list subtrees but returned scalar strings raw, so a changed
  `https://user:token@host` device/influx/webhook URL surfaced its userinfo in
  `GET /api/config/snapshots/{sid}/diff` — an endpoint a **viewer** can read.
  Scalars at url-ish leaves now pass through `redact_url` like everything else.

## 3.23.2

### 2026-08-14 — vmeter: a text-valued row can no longer disarm the fail-safe

- **An unencodable source value (text enum/bits/string on a numeric row)
  degrades that ROW to missing instead of aborting the whole block rebuild.**
  Before, one such row raised inside `_rebuild_block` on every supervisor tick
  — freshness was never computed again, the stale-stop branch was unreachable,
  and the meter kept serving the last-built frame as live indefinitely:
  precisely the frozen-data-into-a-control-loop failure the staleness watchdog
  exists to prevent. Edge-triggered warn/recovery events per register
  (`encode`), so a misbound row is visible in Logs without flooding.
- **`json_view()`** applies the same rule (a text member makes a `sum` row
  missing, never a 500 on the JSON feed).
- **Template save validation:** binding a source that currently carries text to
  a numeric row is rejected with a clear error at save time (string-typed rows
  still accept text sources). Runtime guard remains for sources that turn into
  text later (template edit on the source device, enum added).
- Conformance tests now derive their synthetic sources from the template's own
  rows keyed by register ADDRESS (the protocol contract), so canonical-name
  migrations can't silently break them again.

## 3.23.1

### 2026-08-13 — serial-bridge: survive unclean shutdowns (stale UUCP lock + dead ser2net)

- **Stale UUCP lockfiles are cleared before (re)starting ser2net.** A host
  freeze or container kill leaves `/run/lock/LCK..ttyUSB*` behind; container
  PIDs restart from low numbers, so the stale lock's PID can match a live
  process and gensio then refuses every serial open with GE_INUSE ("Object was
  already in use") — surviving even a host reboot, because the container layer
  persists. Incident 2026-08-13: `fronius_rtu` unreadable for ~7h. Locks are
  only touched while ser2net is not running, when any lock in the container is
  stale by definition.
- **A dead ser2net is now respawned by the periodic reconcile.** The
  unchanged-adapter-set early return only short-circuits while the ser2net
  process is actually alive; before, a crashed ser2net stayed down until the
  next adapter hotplug event.

## 3.23.0

### 2026-08-13 — Staleness policy + routing in the published meter state

- The retained vmeter state (`vmeter/<id>/state`) now carries the **staleness
  policy and its bounds** (`on_stale`, `stale_after_s`, `max_hold_s`), the last
  rebuild's **quality** counts (fresh/stale/missing), and live **failover**
  routing (per register: candidates + which source is active + on_primary). A
  monitor (alertd / HA / dashboards) can now see HOW a meter degrades and which
  redundant source is feeding it — not just that it went stale. `status()` gains
  `stale_after_s` / `max_hold_s` to back this. Additive; no electrical data
  duplicated.

## 3.22.0

### 2026-08-13 — Instance-level redundant source device (device_fallback)

- **A virtual-meter instance can now name a secondary "twin" source device.**
  One field on the instance (`device_fallback`) makes the WHOLE meter transparently
  fail over per register when its source goes stale — no per-register template
  edits, because canonical field names are identical across devices (`what's on
  one is on the other, if it exists`). At start, each bare-name `live` register is
  rewritten into a failover pair `[name, <fallback>.name]`, reusing the tested
  failover resolver (first fresh wins, auto-recovers to the source). Const, sum,
  already-failover, and explicit `device.register` registers are left untouched.
- **Where:** the Add/Edit **instance** modal (🎚) gains a *Secondary source device
  (failover)* dropdown next to the source device — the instance is where physical
  sources are bound, so redundancy belongs there. The per-register **template**
  failover (3.21.0) stays as the granular tool (different fallback per register /
  arbitrary pairing); the instance field is the one-switch ergonomic layer on top.
  Both drive the same engine — nothing is removed.
- **Thorough observability (so a problem is traceable after the fact):** a
  start-time coverage log per instance (`device_fallback X → Y — N registers
  wired; twin has now: […]; armed/waiting: […]`); every runtime switch logs to
  BOTH the event ring (UI Logs / alertd) AND the process log (grep-able), naming
  the meter, register and both sources — warn on drop to a lower-priority source,
  info on recovery; and `status()` exposes live `failover` routing (per register:
  candidates + which is active + on_primary), surfaced as an on-card badge
  (`✓ on primary` / `⇢ n/m on fallback`).
- **Safety:** the fallback device is validated at save (must be a known device,
  must differ from the source) and re-checked at start (a mis-set value is ignored
  with a warning, never blocks a control-critical meter from starting). A
  configured-but-offline twin validates and arms; failover engages the moment it
  publishes. Off by default, fully back-compatible.

## 3.21.0

### 2026-08-13 — Redundant-source failover, editable from the UI

- **Failover is now a first-class source kind in the vmeter template editor.**
  The measurement-source dropdown gains **Failover (live…)** alongside
  Const/Live/Sum; the value field takes a comma-separated list in priority order
  (`primary, fallback, …`, `device.register` for a cross-device source). The
  editor round-trips it as `source: {failover: […]}` — the runtime resolver
  (3.9.0) serves the first *fresh* candidate, auto-switches to the next when the
  primary goes stale, and switches back the instant the primary recovers, logging
  each switch as a vmeter event.
- **Fixes a data-loss trap:** before this, a template already carrying a
  `failover:` source had no matching dropdown option, so opening it in the editor
  silently reset the row to `const` on save. The option now binds correctly, and
  the soft "unknown source device" check extends to each name in a sum/failover
  list (catches a typo'd `device.register` prefix on any redundant source).
- **Decode panel** now renders a failover register as `⇢ a → b → c` (priority
  order) instead of mislabeling the source list as a constant.
- Backend (normalize/validate/serialize/resolve) already supported failover; this
  release wires it end-to-end through the operator UI. Additive — existing
  templates and the const/live/sum paths are unchanged.

## 3.20.0

### 2026-08-13 — Community YAML template import

- **YAML register-map import** — a vendor/community device map in YAML becomes an
  MBG device-template preview (reviewed, then saved), symmetric with the existing
  CSV import. Richer than CSV: per-register `enum`/`bits`/`mask`/`shift`/`offset`/
  `thresholds` and the full write envelope (`writable`, `write_min/max/safe`) pass
  through untouched, so a map that already speaks MBG imports at full fidelity.
- Accepts a bare list, a `registers:` list (also `points`/`signals`/`sensors`/…),
  or a `device_template:` wrapper, and matches field names loosely (`reg`/`addr`,
  `type`/`datatype`, `factor`/`gain`, `desc`→`label`) — reusing the CSV importer's
  alias/address/type machinery. `id`/`name`/`vendor`/`model` fall back to the
  document's own meta. Rows with no address (and no `json_path`), bad addresses, or
  duplicates are skipped with a warning; unknown types coerce to the default.
- New `POST /api/device-templates/import-yaml` (2 MB cap) returns the same preview
  shape as import-csv (`register_count`/`warnings`/`validation_errors`). The
  Template Manager gets an **Import YAML** button; the shared import modal is now
  format-aware. Additive — no change to existing templates or the CSV path.

## 3.19.0

### 2026-08-13 — Backlog close-out (3/3): HA connectivity + coverage

- **Per-device connectivity binary_sensor** — each non-primary device now
  publishes a retained `…/availability` (online/offline, only on change, driven
  by the health harvester) and a matching HA `binary_sensor` with
  `device_class: connectivity`, so a source device going down is visible in Home
  Assistant as a diagnostic entity, not just in the gateway's Status page.
- Closes the Home-Assistant polish set: explicit `enabled_by_default` (3.7.0),
  enum/text sensors flow as text with numeric typing suppressed (3.10.0), and now
  connectivity. The `value_template |default` guard is N/A — MBG publishes raw
  values to the state topic, not templated payloads.

## 3.18.0

### 2026-08-13 — Backlog close-out (2/3): config self-healing, alert hygiene

- **Config self-healing** — a good load now keeps a `config.yaml.good` snapshot;
  a later corrupt edit falls back to that **last-known-good** config (not bare
  defaults), so the primary keeps polling the right host through a bad edit. The
  broken file is preserved as `.yaml.bad`, saves stay disabled until it's fixed,
  and the condition is surfaced in `/api/status` (`config.healthy`) and raised as
  an alert instead of only a log line.
- **Threshold alerts suppressed on stale data** — the threshold engine no longer
  evaluates a register the device has stopped refreshing (down/frozen); the band
  holds and resumes cleanly on reconnect, so a comms loss can't fire a phantom
  crossing.
- **Connection-sharing audit** — verified MBG opens **one** Modbus socket per
  device, shared across its poll groups (not one per group/vmeter); no change
  needed.

## 3.17.0

### 2026-08-13 — Backlog close-out (1/3): decode, write-refresh, wedged link

- **Signed-magnitude decode** — data types `sm16` / `sm32` (top bit = sign,
  not two's complement) for meters that encode a direction bit that way.
- **Write-then-refresh** — after a successful write (HTTP API *and* HA
  write-entity), the register's read-back value is pushed into the live store,
  so the virtual meters / UI reflect a setpoint immediately instead of at the
  next poll (a slow-group setpoint could otherwise lag 60 s).
- **Wedged-link forced reopen** — a serial/PTY port (or a stuck ser2net bridge)
  can stay "open" while every read errors, so `is_socket_open()` never trips and
  the normal reconnect never fires. After 5 consecutive failed polls the
  connection now force-closes so the next read reopens a fresh client; the event
  is recorded and counted (`forced_reopens`). TCP already self-heals — this is
  the belt-and-braces for RTU.

## 3.16.0

### 2026-08-13 — Data-readiness gate (drop all-zero frames)

- **`modbus.drop_all_zero`** — a sleepy device (an inverter at night) can answer
  a read with an ALL-ZERO frame instead of an error; published as-is that reads
  as real 0 V / 0 W data and pollutes charts + averages. With this on, a poll
  group whose numeric values are **all exactly zero** (and there are ≥2 of them)
  is dropped — the cache keeps the last-good values until the device wakes. The
  ≥2 guard means a lone legitimate zero is never withheld, and voltage/frequency
  are never 0 on an awake device, so an all-zero measurement frame is a reliable
  asleep signature (a cumulative energy group stays non-zero, so it isn't
  affected). Per-device; off by default; +5 tests.

## 3.15.0

### 2026-08-13 — Per-device illegal-register skip-list

- **`modbus.illegal_registers`** — a per-device list of addresses the slave
  answers with ILLEGAL DATA ADDRESS (exception 02). The batch reader now never
  bridges a merged read across one of them (splitting the block instead) and
  drops a selected register that sits on one, so a single unmapped hole *inside*
  a contiguous run can no longer poison the whole block — the case `max_gap: 0`
  can't fix. Accepts decimal or `0x…`; a per-device override lives under that
  device's `connection:`. Round-trips through config; +7 tests. Empty by
  default — no behaviour change.

## 3.14.0

### 2026-08-13 — Home Assistant write-entities (number / select)

A **writable** register on a non-primary device can now be *controlled* from
Home Assistant, not just read. **Off by default and double-gated** — turning
broker-publish access into hardware-write access is deliberate.

- **Discovery** — with `mqtt.allow_write_entities` on, a writable holding
  register is published as an HA **`number`** (bounds from the template's
  `write_min`/`write_max`, `mode: box`) or, if it has an `enum` map, a
  **`select`** (options = the enum labels). Its command topic (`…/set`) is
  subscribed. Everything else stays a plain sensor; the primary is never
  writable.
- **Command handler** — an incoming command is executed as a Modbus write only
  after **re-validating everything** (the broker is not a trusted caller):
  `mqtt.allow_write_entities` **and** `security.allow_writes` both on, the
  register writable in its **template** (the allowlist — never the saved
  register), the value within bounds (a select label is mapped to its code and
  bounds-checked too), a per-device **rate limit**, and an **audit record**.
- The write runs on the MQTT thread through the same `write_value` path (takes
  the connection lock, serialized with the poller). Toggling the gates
  re-publishes the affected entities and drops stale command subscriptions.
- +12 tests (discovery number/select, command routing, and every gate on the
  handler). Live behaviour unchanged unless both gates are enabled.

## 3.13.0

### 2026-08-13 — Per-register offset

- **`offset`** — engineering value is now `raw / scale + offset`, so a register
  with a zero-point or unit shift decodes correctly (e.g. Kelvin×10 → °C with
  `scale: 10, offset: -273.15`, or a sensor whose 0 isn't the electrical 0).
  Applied after scale on **every** transport (Modbus / HTTP / MQTT), so a
  register's offset means the same thing everywhere; skipped for enum/bitfield
  registers (they decode to text). An **Offset** column sits next to Scale in
  the template editor. Threaded template → autoselect → save → load; +7 tests.
  Default `0` — purely additive.

## 3.12.0

### 2026-08-13 — Enum / bitfield builder in the template editor

The decode maps added in 3.10.0 were JSON-only; now they have a UI. Each
register row in the template editor gets a **States** button (badged when a map
is set) that opens a builder modal:

- Toggle **Enum** (value → label) or **Bitfield** (bit position → name); add /
  remove rows in a small table instead of hand-writing JSON.
- **Validation on Apply** — integer keys (decimal or `0x…`), unique, in range
  (bits 0-63), non-empty labels — so an invalid map can never be saved.
- Enum **Advanced**: `mask` + `shift` for a packed sub-field.
- Writes `enum`/`bits`/`mask`/`shift` onto the register; **Clear** removes them.
  Persists through the existing save path (parse → to_dict → disk); +1 test
  pins the round-trip.

## 3.11.0

### 2026-08-13 — Poller startup jitter

- **`polling.startup_jitter_s`** (0 = off, default) — each poll group waits a
  random delay in `[0, min(interval, startup_jitter_s)]` before its **first**
  read, so several devices/groups don't fire in lock-step and hammer a shared
  transport (notably the RTU-over-TCP serial bridge) at boot. The jitter is
  capped at the group's own interval, so it can never delay a group by more than
  one of its cycles — a slow-group energy row is never withheld from the virtual
  meters longer than its cadence already allows. A per-device
  `modbus.startup_jitter_s` overrides the global default. +6 tests.

## 3.10.0

### 2026-08-13 — Enum / bitfield decode for status registers

A register can now turn a raw **status** value into meaning instead of serving a
bare number that means nothing to a human, to Home Assistant, or to the
threshold engine.

- **`enum`** — `{code: label}` maps one state to text (`7` → `Fault`); an
  unmapped value becomes `"unknown (<n>)"`, never a silent wrong label and never
  the bare number. Optional `mask` + `shift` extract a packed sub-field first
  (`(raw & mask) >> shift`) for status words.
- **`bits`** — `{bit: name}` expands a status word into the joined names of its
  set bits (`0b1101` → `overvoltage, overtemp, overcurrent`); no bit set → the
  idle label (default empty).
- Decoded on the Modbus poll path right after parsing; the value becomes a
  **string** and flows through MQTT / InfluxDB / HA exactly like the existing
  string registers (scale and the monotonic filter are numeric-only and skipped).
- **Pairs with HA typing + alerting** — an enum/bits register is recognised as a
  text sensor, so the invalid numeric `state_class`/`device_class`/unit
  inference is suppressed automatically; and the threshold engine / alertd can
  now act on a decoded state instead of a raw code.
- Threaded through template → autoselect → save → load; +13 tests. Purely
  additive — a register without `enum`/`bits` is unchanged.

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
