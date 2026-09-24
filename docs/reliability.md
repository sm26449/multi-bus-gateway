# Reliability & fail-safety

Multi-Bus Gateway feeds **control-critical consumers** — an ESS grid meter, a
Fronius DataManager, Home Assistant automations — so its defensive design is
a first-class feature, not an afterthought. This page catalogs every
protection mechanism in the codebase: what it protects against, where it
lives, and how you see it working.

Five principles run through everything:

1. **Fail closed.** A missing timestamp, an unresolved source, a corrupt
   config, a broken TLS setup and a never-polled group all resolve to
   *unavailable* — never to a plausible number.
2. **Absence is never encodable as a measurement.** `None`, a Modbus
   exception or a SunSpec NA sentinel — never `0` (0 is a valid reading).
3. **Freshness is judged on the monotonic clock.** Wall time is for display
   only; an NTP step can never make dead data look live (or vice versa).
4. **Edge-triggered observability.** One warning on a transition, silence in
   between, a bounded event ring per subsystem — so a real incident is never
   buried under flapping.
5. **Every persisted file is atomic and fsync'd**; the ones that carry
   secrets (`config.yaml`, sessions, passkeys, leases, snapshots) are
   `0600` from creation, and the files that can brick a boot carry a
   `.good`/`.bad` self-heal pair.

These claims are load-tested, not just designed: the capacity campaign
([`loadtest/CAPACITY-REPORT.md`](../loadtest/CAPACITY-REPORT.md), re-validated
on v3.35.3) drives the fail-safe end-to-end — source killed under load,
virtual meters go stale exactly at the freshness bound, their sockets refuse
connections rather than serve stale data, and recovery takes 2–3 s — plus
soak/leak and swarm phases with zero errors.

The tables below are grouped by layer, wire to consumer. File references
point into `multibus/` unless noted.

## 1. Wire → value integrity

| Mechanism | Protects against | Lives in |
|---|---|---|
| Not-available sentinel table (opt-in per register: `nan: true`/value/list) | Publishing a vendor "not available" code (−32768 / 65535 / …) as a real measurement | `register_parser.py` |
| Float/double NaN & Inf drop (always on) | A corrupt IEEE-754 word becoming `nan`/`inf` downstream (unparseable by HA, poisons Influx batches) | `register_parser.py` |
| Parse exception → `None`, never a guess; per-type length guards | A malformed/short register block decoding as a plausible number | `register_parser.py` |
| Unambiguous byte-order names only (`le` deliberately not an alias; unknown → big) | The classic Modbus word-order footgun silently decoding swapped garbage | `register_parser.py` |
| String decode strips non-printables; strings never byte/word-swapped (encoder matches) | Control bytes or swapped ASCII in a serial/model register | `register_parser.py`, `encoder.py` |
| **MonotonicFilter** — a downward step is held until confirmed | One corrupt read looking like a counter reset → a phantom mega-delta in HA Energy / Influx `difference()` | `counter_filter.py` |
| Symmetric upward guard (>1.5× baseline needs the same confirmation) | A flipped high word jumping 1 MWh → 2.1 GWh and poisoning the baseline | `counter_filter.py` |
| Coherent-reset rule (confirming reads must agree within 5% of the drop) + loud WARN on adoption | Unrelated garbage reads being adopted as a new baseline | `counter_filter.py` |
| Filter state is per-poller, re-seeded on reload, cleared on live register swap | A stale baseline rejecting a legitimately re-mapped counter | `modbus_client.py`, `http_client.py`, `mqtt_input.py` |
| Enum decode never returns a bare number (`unknown (n)`); non-integer map keys dropped | An unmapped status code reading downstream as a measurement; a bad hand-edited map crashing decode | `value_decode.py` |
| Edge-triggered decode-failure warning + hold last-good | A corrupt `mask`/`shift` silently blanking a status register | `modbus_client.py` |
| Textual registers suppress numeric HA typing | HA rejecting a numeric state_class on a text sensor | `value_decode.py`, `mqtt_publisher.py` |
| **`apply_corrections`** — ONE staged pipeline (sentinel → enum/bits → scale+offset → monotonic) on every transport; stateful stage only for pollers; per-stage drop reasons | Drifted per-transport copies; a debug read advancing filter state; "value disappeared" with no cause | `value_decode.py` + all input paths |
| Short/empty non-error response counted as a failure and retried | Silently dropping the tail registers of a batch | `modbus_client.py` |
| All-zero frame gate (`drop_all_zero`, ≥2 numeric values) | A sleepy inverter's all-zero answer published as real 0 V / 0 W | `modbus_client.py` |
| Illegal-register skip-list; merged reads never bridge a rejected address; ≤120-register reads; per-type address spaces never merged | One exception-02 address failing a whole batch; over-long spans; wrong address space | `modbus_client.py` |
| Register identity validation (duplicate address/name hard-rejected, span overlap warned) | Two registers collapsing onto one store key; nondeterministic vmeter binding / MQTT topic / Influx series | `config.py` |
| Canonical-unit contract check (warn on mismatch) | A kWh value under an `energy_*` name = a silent ×1000 error at the first vmeter/twin/dashboard | `config.py`, `canonical_fields.py` |
| `coil_truthy` (`"false"`/`"off"`/`"0"` are OFF) | `bool("false") == True` switching a coil ON | `modbus_client.py` |
| HTTP non-numeric values never go downstream | A string type-conflicting a whole Influx batch; a vmeter stalling on an unencodable row | `http_client.py` |
| Encoder offset is the exact inverse of decode; integer-space multiply for large counters; over-range clamp is never silent; unsupported types raise | A setpoint landing shifted; float mantissa corrupting int64 counters; silent wrap; two words emitted for a one-word type corrupting the neighbor | `encoder.py` |

## 2. Link robustness

| Mechanism | Protects against | Lives in |
|---|---|---|
| The application owns retries (`pymodbus retries=0`; `retry_attempts` × `retry_delay` from config on every failure path, connects included) | Library retries multiplying with app retries (~38 s/batch); hammering a down device at 10 connects/s | `modbus_client.py` |
| Connection lock released during backoff sleeps | A slow group holding the shared connection for its whole retry budget, stalling `realtime` | `modbus_client.py` |
| **Wedge backstop**: N consecutive failed batches → forced close+reopen (registers *and* bits paths; success on either resets the shared counter) | A serial/ser2net link staying "open" while every read fails — normal reconnect never triggers | `modbus_client.py` |
| Reachability is a **link verdict** (only a backstop-length run declares unreachable), with `batch_failures` + per-group `stale_groups` for per-batch loss | One chronically-failing batch flapping unreachable/recovered and burying real dropouts; one frozen group hidden behind a healthy fast group | `modbus_client.py` |
| Monotonic-clock staleness everywhere; cold-start grace | An NTP step faking freshness; a false `down` right after boot | `modbus_client.py`, `http_client.py` |
| Startup jitter; 50 ms interval floor | Lock-step hammering of a shared bridge at boot; a zero interval spinning the loop | `modbus_client.py` |
| **Per-device acquisition log** — a 500-entry ring on every connection (unreachable, recovered, forced reopen, bus busy, failed batch with its address and size, poll-group overrun episodes), read through `/api/devices/{id}/events` and the device page's Logs tab | Facts the process already had but nobody could ask for: diagnosing a struggling endpoint meant grepping container logs by hand. The ring was also 50 entries, which a datalogger stalling in bursts buried before anyone opened the page | `modbus_client.py`, `api.py` |
| **Endpoint arbiter** — a FIFO turnstile per `host:port`, so devices sharing one gateway queue instead of racing (`serialize_endpoint`, on by default; a missed turn skips the cycle and is never counted against the link) | A cheap gateway that serializes internally being hit by N of its own devices at once: measured on a production DataManager, five racing pollers turned a 0.4 s read into 3 s and dropped the aggregate below 1 read/s, with fresh connections timing out | `modbus_client.py` |
| **Fixed-rate polling** — the wait is `interval − sweep`, with a 10 % floor and an `overruns` counter | The configured interval silently becoming `interval + bus time`: on a slow endpoint the data drifted ever further from the freshness the config promised | `modbus_client.py` |
| **One socket per access point** (`share_transport`), with per-unit counters, health and reachability kept separate; the last unit to let go closes it | A master device running out of client slots (measured: it began refusing connections at ~10); a unit going away taking its siblings off the master with it | `modbus_client.py` |
| **Connection lanes** (`max_connections`, default 1) — K sockets per access point with units sticky-assigned across them, each keeping its own FIFO turnstile; plus per-endpoint bus telemetry (seconds per transaction p50/p95, reads per sweep, achievable floor) on the endpoint page | Guessing a master's concurrency instead of measuring it — in both directions: racing a serializing datalogger, and leaving a multi-engine bridge on one socket. `scripts/calibrate_endpoint.py` probes 1..N against the real device and names the number | `modbus_client.py`, `scripts/calibrate_endpoint.py` |
| Poller stop-event contract (single source of truth, checked between groups/retries and before publishing) | A zombie poller publishing a late batch through the OLD callback | `modbus_client.py`, `http_client.py` |
| **Retired connections** — read/write paths refuse a connection that was replaced | An orphan poller reopening the old client = two concurrent Modbus masters on one endpoint (catastrophic on RTU) | `modbus_client.py` |
| Lifecycle serialization (RLock) + idempotent start | Two concurrent Applies leaving a doubled poller set (double bus load) | `modbus_client.py`, `http_client.py` |
| Pollers always start after reconnect/reload; boot waits for the network then starts regardless | A device briefly down at apply-time being left unpolled forever; docker networking delay = dead on arrival | `modbus_client.py`, `main.py` |
| Bounded DNS resolution; HTTP fetch back-off; 8 MiB response cap | A wedged `getaddrinfo` freezing a poller; hammering a slow endpoint; an endpoint streaming GBs | `http_client.py` |
| Serial bridge: dev-path-aware reconcile, stale/dead-PID lock sweeps, respawn with exit codes, reconcile that can never kill the supervisor, periodic reconcile as the real hotplug path, threaded control API with timeouts, `/health` reporting the DATA path, validated persistent port map, `BRIDGE_EXCLUDE`, `max-connections: 1` + `kickolduser` | Replugged adapters bound to dead/wrong `/dev` nodes; stale UUCP locks refusing every open; a transient error taking down a healthy ser2net; missed uevents in a container netns; a wedged client failing the healthcheck; port assignments silently reshuffling; stealing another service's adapter; two clients interleaving frames on one RS-485 line | `serial-bridge/supervisor.py` |
| Bounded **parallel** shutdown; explicit uvicorn server so SIGTERM runs the graceful path; live publishers resolved at stop | Sequential joins outlasting docker's grace → SIGKILL losing the Influx replay buffer; a rebound publisher's buffer never flushed | `main.py` |

## 3. Virtual-meter fail-safety

| Mechanism | Protects against | Lives in |
|---|---|---|
| Freshness watchdog: stale source → the server **stops responding** | Feeding silently-stale data into an ESS loop — the consumer's own meter-loss fail-safe must engage | `virtual_meter.py` |
| Freshness on the monotonic clock; missing/future timestamps fail closed; the clock-step guard is diagnostic-only | A chrony step dropping (or falsely reviving) both meters' consumers | `virtual_meter.py` |
| Per-row bound cascade (row `stale_after_s` → source-device bound → 2.5× poll-group interval → instance bound; derived bounds may only relax, capped at 300 s) | A slow-group/BLE row flapping the whole meter; a dead source reading "fresh" for hours | `virtual_meter.py`, `virtual_meter_manager.py` |
| `on_stale` policies — `legacy` (all-or-nothing), `fail` (Modbus exception), `sentinel` (SunSpec NA words), `hold` (time-bounded) — absence never served as 0/false | Serving frozen or invented words as live | `virtual_meter.py`, `encoder.py` |
| **Unresolved-row withhold** — a row whose source never resolved fails the verdict with an `unresolved` event | The zero-seeded block serving a plausible **0 W** to an ESS while the meter looks healthy | `virtual_meter.py` |
| Sums are never partial; policy-mode sums take the OLDEST member timestamp | A partial or mixed-age sum as silent corruption | `virtual_meter.py` |
| Failover rows serve the first FRESH candidate; transitions evented (warn on drop, info on recovery) | A silent switch to a lower-priority source | `virtual_meter.py` |
| **Pinned counters** — cumulative energy rows never fail over to a twin; they freeze at last-good and resume on recovery | Another meter's lifetime total = a non-monotonic jump corrupting Victron/DataManager kWh statistics | `virtual_meter_manager.py`, `virtual_meter.py` |
| Guarded row resolve: an unencodable value degrades that ROW to missing, never aborts the block rebuild | A frozen served frame with the stale-stop fail-safe disarmed | `virtual_meter.py` |
| Tear-free serving (one slice write per value, one slice read per response); read-only server (all writes refused); FC1/FC2 refused; block starts at the map base | A consumer reading half-old/half-new words; a poisoned value injected into a pad; probe addresses answered with fake zeros | `virtual_meter.py` |
| Optional in-band quality block (state / fresh / stale / missing / age at 0xF000) | A PLC consumer with no MQTT access unable to judge the data it just read | `virtual_meter.py` |
| Crash self-heal (real thread liveness + serving probe on the configured bind, restart with backoff); the supervisor loop can never die; stop joins the supervisor first | A dead or wedged server thread leaving the meter silently down; an orphaned unsupervised listener serving a frozen block | `virtual_meter.py` |
| TCP keepalive on accepted sockets | A consumer vanishing without FIN leaking one FD per wake cycle | `virtual_meter.py` |
| Missing source device → EMPTY store (fail-safe), never another device's values | A control consumer silently reading the wrong meter | `virtual_meter_manager.py` |
| Validation: port uniqueness + published-range check; register-span overlap rejection; text-source-on-numeric-row rejection; non-finite lifecycle params rejected; `device_fallback` strict at save / lenient at start; one-master-per-line for RTU and bridged endpoints; rollback of `enabled` on a failed start | Two meters on one port; rows overwriting each other; `inf`/`nan` defeating the watchdog; a mis-set fallback blocking a control meter; two masters evicting each other on one line; a broken config re-tripping every boot | `virtual_meter_manager.py`, `api.py` |
| Health mapping: `stale` → degraded (HTTP 200), only `down` → 503 | A container probe restarting a meter that is *correctly* fail-safing | `virtual_meter_manager.py`, `Dockerfile` |

## 4. Data-delivery guarantees

### MQTT

| Mechanism | Protects against | Lives in |
|---|---|---|
| Publish-confirm under one lock; the cache stores the ROUNDED value actually sent; NaN/Inf guarded before change detection | Racing threads leaving the broker and the cache disagreeing forever; float jitter degrading `changed` mode to `all`; publishing the text `"nan"` | `mqtt_publisher.py` |
| Heartbeat republish of unchanged values (opt-in) | A stable value's timestamp going stale for HA | `mqtt_publisher.py` |
| Retained availability confirmed only after a successful publish; LWT re-armed on identity change; OLD prefix's retained topics cleared; retained `offline` flushed before disconnect | A dead device staying `online` in HA indefinitely; a prefix change leaving the death notice where nobody watches | `mqtt_publisher.py` |
| Cache + heartbeat clocks cleared on (re)connect → full republish; HA discovery re-published on reconnect and re-asserted periodically; stale discovery clears retried until they land | A broker restart leaving topics empty for days and ghost HA entities alive forever | `mqtt_publisher.py` |
| **Retained-command drop** + retained clear at subscribe | The broker replaying a past write into hardware on every reconnect | `mqtt_publisher.py` |
| Command worker thread (never paho's network thread), bounded queue with drop accounting, restarted after reconnect | Blocking Modbus I/O stalling every publish; a flood of stale writes; post-reconnect commands vanishing | `mqtt_publisher.py` |
| Gateway liveness (LWT/online) QoS 1 retained, per-device availability at the configured `mqtt.qos`; TLS setup failure **fails closed**; `publish_drops` accounting | Losing the death notice; silent cleartext fallback; an outage invisible in stats | `mqtt_publisher.py` |

### InfluxDB

| Mechanism | Protects against | Lives in |
|---|---|---|
| Auth-failure detection (401/403/404 → `auth_failed` + alert) with confirmed-delivery counters (`writes_confirmed`), self-clearing on recovery | `/ping` being unauthenticated in Influx 2.x — a rotated token failing 100 % of writes while health stays green | `influxdb_publisher.py` |
| Store-and-forward buffer with ORIGINAL timestamps; failed batches recovered into it; **data-relative** age window; hard point cap with drop accounting that invalidates the change cache; disk persistence with atomic snapshots; drain per bucket with poison-chunk policy (only 400/422 ever dropped) and front-of-queue retry | Losing outage data; wall-clock pruning discarding data mid-outage; a stable value's gap never healing; a restart losing the buffer; one malformed chunk blocking everything (or one token error destroying good points) | `influxdb_publisher.py` |
| Close order write_api → drain → persist, monitor joined 15 s; lock never held across network I/O (hot path non-blocking) | Flush failures lost at shutdown; pollers stalling behind a slow reconnect | `influxdb_publisher.py` |
| Proactive ping every 30 s; non-blocking startup; points stamped with poll time; per-device cache keys; bucket auto-create (90 d); Flux input validation/whitelisting | Late outage detection; a down Influx stalling boot; batching latency skewing series; cross-device cache collisions; history going nowhere; Flux injection | `influxdb_publisher.py` |
| Backfill: tail **and interior** gap detection; schema derived from the live selection via the publisher's own `build_point`; deselected addresses skipped; `backfilled=1` marker; idempotent; `--dry-run` | Permanent holes; repairs landing in a parallel invisible series; guessed schemas; duplicates on re-run | `backfill.py` |

## 5. Config safety

| Mechanism | Protects against | Lives in |
|---|---|---|
| Atomic writes everywhere (`tmp` → `fsync` → `rename`), `0600` from creation for every file carrying secrets, one shared file lock across config writers | Torn files on power loss; secrets world-readable on a bind mount; two concurrent saves publishing a half-written mix | `config.py`, `snapshots.py`, `auth.py`, `passkeys.py`, … |
| **Plausibility gate**: a config.yaml that parses but is a truncated husk routes through `.bad` + heal; `.good` is never refreshed by a file that lost devices (an intentional save refreshes it) | A cut file "loading successfully" and destroying the only recovery copy | `config.py` |
| `.bad` copy + `.good` self-heal + **save blocking** until repaired, surfaced in `/api/status` and as an alert | A corrupt config silently becoming defaults, then a save permanently overwriting the user's real file | `config.py` |
| The same `.good`/`.bad` contract on `selected_registers.json` — primary and per-device | A truncated selection silently emptying every poller | `config.py` |
| `config_version` stamp + downgrade warning | An older version's save dropping a newer version's settings unnoticed | `config.py` |
| **Boot seatbelt**: config load failure → restore the last-known-good bundle → retry once; LKG marked only after ~5 min of *proven* health | A bad edit bricking an unattended box; promoting a broken config to "known good" | `snapshots.py`, `main.py` |
| Debounced auto-snapshots on config mutations (bounded store, 50 kept); snapshot failures never break the request; deterministic ordering | Losing pre-change state; a snapshot bug 500-ing a save | `api.py`, `snapshots.py` |
| Device **tombstones** (full definition kept on delete, restorable, snapshot-included) | An accidental delete losing a device's configuration | `tombstone_store.py` |
| Import safety: path validation (no `..`), 25/50 MB caps, secret re-injection on merge (a sanitized backup never wipes live credentials), `passkeys.json` honored only on a full replace, pre-import snapshot | ZIP traversal/bombs; a shared backup nuking passwords or swapping the WebAuthn registry; a bad import with no way back | `snapshots.py`, `api.py` |
| Semantic snapshot diff with secrets masked at emit time | A diff leaking a password while showing it changed | `snapshots.py` |

## 6. Security enforcement

The full model is summarized in [SECURITY.md](../SECURITY.md); the
mechanisms, layer by layer:

| Mechanism | Protects against | Lives in |
|---|---|---|
| Auth-guard middleware (session required when login is on; minimal open set), roles admin/operator/viewer with a segment-anchored operator allowlist, identity attached before any deny | Open routes on a reachable port; a viewer mutating anything; path tricks like a device named `write`; anonymous audit rows for refused requests | `api.py` |
| First-run provisioning (generated admin password, hashed, auth ON, printed once) + the default-credential guard in `AuthState.reload` (covers hand-edits, imports, restores) | Shipping open by default; any path to a gateway that *looks* locked but accepts `admin/admin` | `main.py`, `auth.py` |
| PBKDF2-SHA256 (600k), constant-work auth with a decoy hash, per-IP lockout shared by password and passkey paths | Offline cracking; username enumeration by timing; brute force | `auth.py` |
| Sessions as SHA-256 token hashes (0600, atomic), sliding expiry, revoke-all on rotation, the caller's own session re-issued on a security save | Usable tokens on disk; stolen cookies outliving a rotation; the admin logging themselves out with their own save | `auth.py`, `routes/config.py` |
| Passkeys: RP-ID/origin validation, one-shot server-side challenges, sign-count tracking, review-on-enable of passkeys enrolled while auth was off | Replay; a LAN attacker's passkey silently becoming a live admin credential | `passkeys.py`, `routes/auth_routes.py`, `routes/config.py` |
| CSRF (`Sec-Fetch-Site`/`Origin` incl. port), opt-in CORS with credentials off, API key on all mutations and on the OTA-capable builder WebSocket (header or `mbg-api-key.<b64url>` subprotocol), WS connection cap + per-send timeouts | Drive-by state changes; wildcard-CORS liability; unauthenticated writes; key leakage via query params; FD exhaustion | `api.py`, `routes/builder_routes.py` |
| **Write gating chain**: `allow_writes` → authenticated caller → per-IP rate limit → device write lock (the primary ships locked) → template allowlist (encoding from the template, never the caller) → NaN/Inf rejected → bounds → read-back verification → audit; the same chain re-applied to MQTT/HA commands; write entities advertised only when doubly enabled | An unattributable, unbounded, caller-encoded write to real hardware — from any path, including the broker | `api.py` |
| **Dead-man write leases**: TTL auto-revert to the template's safe value, per-lease revert threads, revert failures retried loudly, leases persisted and fired on boot | A crashed controller leaving a dangerous setpoint standing — even across a gateway crash | `write_lease.py`, `api.py` |
| Audit trail (every mutation incl. denials; bounded body capture; redaction; rotation at 0600; CSV formula-injection guard; admin-only) | No record of who changed what; the audit becoming where secrets leak; OOM via a giant body; spreadsheet injection | `audit.py`, `api.py`, `routes/system.py` |
| Redaction everywhere (`redact_url`, role-based device redaction, sanitized exports, masked env overrides, builder YAML excluded from body capture) | Tokens/passwords in logs, exports, or the audit | `redact.py`, `api.py`, `snapshots.py` |
| TLS in-process (key 0600) or via a trusted proxy (proxy headers honored ONLY from `ui.trusted_proxies`); cookies HttpOnly/SameSite/Secure; cleartext-login warning; canonical-URL output escaping + scheme validation + client-side redirect with `?local` escape hatch; security headers on every response including the login shell; CSP `script-src 'self'` with no inline handlers in the UI (3.82.0); fully vendored UI (no CDN) | Session hijack over cleartext; header spoofing defeating lockout/allowlist/audit; stored XSS via the canonical URL; a credential page without CSP; operator browsers beaconing a CDN | `main.py`, `api.py`, `routes/auth_routes.py` |
| IP allowlist (CIDR, IPv6-mapped normalized, loopback always allowed, invalid entries non-fatal, enforced on the shell short-circuit too) | The UI shell leaking past the allowlist; a typo locking everyone out | `api.py` |
| SSRF guards: HTTP devices resolve-once + pinned-IP connect, all-private requirement, redirect re-validation (cross-origin and HTTPS→HTTP refused), LAN-only discovery/probe, webhooks refuse all redirects, per-device TLS verify default-on | DNS-rebind and redirect games; the gateway as an internal port scanner; webhook credentials replayed off-site | `http_client.py`, `routes/discovery_routes.py`, `alerts.py` |
| Safe expression evaluator (strict AST whitelist, no `eval`, arity checked at save) | Code execution via a calculated-register formula | `expressions.py` |
| Id regexes + resolved-path containment for devices/templates/downloads; built-in templates read-only; bus trace runtime-only; diagnostics read-only on the bus | Path traversal; overwriting shipped maps; raw frames persisted; a "probe" mutating hardware | `api.py`, `virtual_meter_manager.py`, `bus_trace.py` |
| Non-root containers (chown-then-drop via `setpriv`); privileged port via a container-scoped sysctl, not capabilities; serial bridge with `/dev` read-only + tty-only cgroup rules + unpublished data ports; pinned dependency lockfile; stdlib healthchecks that follow `UI_PORT` | Root processes; capability grants; raw Modbus reachable from the LAN; supply-chain drift; false-unhealthy deploys | `Dockerfile`, `docker-compose.yml`, `serial-bridge/` |
