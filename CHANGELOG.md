# Changelog

## 3.81.1

### 2026-09-23 — bundled broker: second start no longer fails

- The mosquitto start command created the password file with
  `mosquitto_passwd -c`, which refuses an existing file — the broker came up
  once and crash-looped on every restart. It now updates the `.env` user in
  the existing file (and creates it only the first time), so extra users
  added with `mosquitto_passwd` also survive restarts.

## 3.81.0

### 2026-09-23 — BREAKING: the bundled stack requires credentials

Only installs running the bundled `docker-compose.yml` stack are affected;
a gateway pointed at your own broker and InfluxDB is not. Migration steps:
`docs/upgrade-guide.md`, "3.81.0".

- **Mosquitto refuses anonymous clients.** `MQTT_USERNAME`/`MQTT_PASSWORD`
  in `.env` are required (`docker compose up` refuses to start the broker
  without them); the password file is regenerated from them on the data
  volume at every start; the gateway logs in with the same pair (they are
  passed through as its config overrides). Every other client of the
  broker — Home Assistant, Node-RED, Telegraf — needs the credentials.
- **No built-in secrets.** `DOCKER_INFLUXDB_INIT_PASSWORD`,
  `DOCKER_INFLUXDB_INIT_ADMIN_TOKEN` and `GF_SECURITY_ADMIN_PASSWORD` lost
  their `multibus-change-me` defaults; they must be set in `.env`.
- **MQTT Explorer** (a viewer with no login) starts only with
  `--profile debug`, listens on `127.0.0.1`, and is tag-pinned
  (`browser-1.0.3`) instead of `latest`.
- **`STACK_BIND`** (default `0.0.0.0`) prefixes the broker, InfluxDB and
  Grafana port mappings; `STACK_BIND=127.0.0.1` keeps them host-local. The
  mosquitto config directory is mounted read-only.

Why: the gateway refused blank and default passwords from its first release,
while the stack around it shipped an anonymous broker and literal default
credentials — and with `mqtt.allow_write_entities` on, an anonymous broker
publish was a hardware write for anyone on the LAN (the gateway refuses that
combination since 3.80.0; this closes the default that produced it).

## 3.80.2

### 2026-09-23 — first findings of the new scanners, and the rules' MQTT set gate

- Dependencies with published advisories bumped in the hashed lock:
  `anyio` 4.14.1 → 4.15.1 (CVE-2026-63374 critical, CVE-2026-63349),
  `cryptography` 49.0.0 → 50.0.1 (CVE-2026-69247) with `pyOpenSSL` 26.4.0,
  `pyasn1` 0.6.3 → 0.6.4 (three DoS advisories). `setuptools` and `wheel`
  leave the runtime image — nothing needs them there and their vendored
  copies carried two more flagged CVEs.
- `mbg/rules/<id>/set` (enable, clamp, pause a rule from the broker) is now
  behind the same gate as the command faces: refused while the broker
  session is anonymous. `docs/API.md` and the design note had described a
  gate that was not in the code.
- The serial-bridge image is also tagged with its minor line, so
  `MBG_VERSION=3.80` pulls both images; the four remaining mentions of the
  old overlay filename point at `docker-compose.external-network.yml`.

## 3.80.1

### 2026-09-23 — supply chain: hash-pinned installs, pinned actions, scanners in CI

- `requirements.lock` carries the hashes of every published file of each
  pinned version; the image and CI install with `--require-hashes`, so a
  substituted package is refused. Regeneration is documented in the file.
- The base image is pinned by digest (tag kept for humans); Dependabot
  refreshes it.
- Every GitHub Action is pinned by commit SHA. CI runs with a read-only
  token, gains `workflow_dispatch`, a `pip-audit` job over the lock and a
  Trivy scan of the built image (HIGH/CRITICAL, fixed-only, fails the run),
  and verifies the vendored UI assets against `ui/vendor/SHA256SUMS`.
- Release images carry SLSA provenance and an SBOM; `latest` no longer
  moves on a pre-release tag.

## 3.80.0

### 2026-09-23 — security fixes from the pre-publication review

- **Writes over MQTT need an authenticated broker session.** With
  `mqtt.allow_write_entities` on, a broker publish is a hardware write and
  carries no identity beyond the broker's ACLs; the command topics and HA
  write entities are now refused (logged once per attempt) while
  `mqtt.username` is empty.
- **Raw writes are an admin act.** `unguarded: true` on `/api/devices/{id}/write`
  bypasses the template envelope; the operator role no longer can.
- **Sessions**: 30-day absolute cap on top of the 7-day slide; the cookie
  is re-issued (hourly at most) as the session slides — a tab in daily use
  was logged out at exactly 7 days because the cookie's expiry was fixed
  at login. A stored password hash that declares fewer than 100 000 PBKDF2
  iterations is refused. Login and the passkey login POSTs no longer sit
  behind the `API_KEY` gate (API key + login was a dead end).
- **Headers on every response**: the security-headers middleware is the
  outermost one, so the guards' own 401/403 answers carry CSP, nosniff and
  frame protection too.
- **Egress**: `/api/devices/test` validates a caller-chosen broker host like
  every other probe; the ESPHome dashboard client refuses redirects (the
  session cookie could have followed a 3xx to another host).
- **Files**: `config.yaml.good`/`.bad` copies and `write_leases.json` are
  created 0600; the InfluxDB URL is redacted in the two log lines that
  printed it raw.
- **Login off the event loop**: the password and security-settings handlers
  run on the threadpool, so a 0.5-s PBKDF2 no longer stalls every other
  request and the WebSocket broadcast.
- **UI**: the stored API key is sent only to this origin and is removed on
  logout.
- **Container**: `setpriv --no-new-privs`; the compose service drops every
  capability except the five the entrypoint's root phase needs and sets
  `no-new-privileges`.
- **Audit** entries are mirrored as `AUDIT` lines in the process log.

## 3.79.0

### 2026-09-23 — release plumbing for the public repository

- `docker-compose.yml` names the published image
  (`ghcr.io/sm26449/multi-bus-gateway:${MBG_VERSION:-latest}`) next to
  `build:`, so `docker compose pull && up -d` runs a release without a local
  build and `docker compose build` still works; the RTU bridge does the same
  with `ghcr.io/sm26449/multi-bus-gateway-serial-bridge`. Its container is
  now `mbg-serial-bridge` (was `pv-stack-serial-bridge`) — the gateway's
  default `SERIAL_BRIDGE_URL` follows; set the env if you kept the old name.
- `docker-compose.pv-stack.yml` is `docker-compose.external-network.yml`:
  the overlay that joins an existing Docker network, named for what it does.
- `config/rules.yaml` is part of every snapshot/backup bundle (it was the one
  runtime file the bundle did not carry) and is ignored by git like the rest
  of the runtime state.
- `.dockerignore` excludes the whole `config/` tree except the shipped
  examples and templates (`sessions.json`, PQ state and the `.good/.bad`
  config copies could reach an image built from a tree the app had run in),
  and keeps `docs/` out of the build context except `modbus_data.json`.
- New `docs/releasing.md`: version bump, changelog entry, tag, the release
  workflow, verifying the multi-arch image. `docs/upgrade-guide.md` covers
  image consumers: pin a tag, roll back by tag.
- Changelog file: the title is the first line again (3.75–3.78 had been
  prepended above it) and the stray "Unreleased" heading is gone.

## 3.78.2

### 2026-09-19 — the rule state says how many samples were ignored or guarded (3.78.1 only had them in the API's live view)

- `mbg/rules/<id>/state` carries `ignored` (outside the valid range) and
  `guarded` (held by the plausibility guard) per unit, so a real OV day
  shows at a glance whether the guard held anything.

## 3.78.0

### 2026-09-19 — rules: a plausibility guard on the signal, debounce by sample

- `signal_valid.max_step`: a reading that differs from the last accepted one
  by more than the step is held until the next sample confirms it; a
  one-sample artefact never reaches a step, not even a `fast` one, and a
  real jump costs one poll interval. Seen live on 2026-09-18: one Solar API
  reading of 273/270/270 V on all three phases of an inverter (grid meter at
  241 V, the other inverters at 245 V) put the Emergency step on in shadow —
  armed, it would have cut to 50 % on an artefact. Reason in the decision
  (`implausible jump +27 against 246 (max step 10) — waiting for the next
  sample`), count in the state (`guarded`), field in the rule editor.
- The debounce counts distinct samples, not ticks: with `every_s: 2` over a
  5-s signal, three ticks over the same reading were three votes and
  `debounce: 3` was satisfied by one reading.

## 3.77.4

### 2026-09-19 — counter hygiene comes from the template

- `monotonic` and `daily` were added to the bundled templates after most
  device register sets had been written to disk, and a selection made
  before a flag existed kept it off forever: the 3.77.0 hold of
  `Site.E_Day` never ran in production (the site's `selected_registers.json`
  predates it), so the day counter still fell at sunset after the
  Datamanager's first evening outage (2026-09-18: 432.96 → 201.18 kWh at
  22:00) and Home Assistant read every drop as a meter reset. At load a flag
  the template sets is applied to the matching selected register (same
  name, else same address); a flag the selection sets itself is kept; the
  log says which registers it touched.

## 3.77.3

### 2026-09-16 — a dead session logs the page out

- When the session ended behind an open page (expired, revoked by a
  security save, or gone after a restart), the UI kept polling
  `/api/status` into 401s and re-dialling the WebSocket every 3 s into
  403s until the operator happened to click (281 rejects in 10 minutes
  from one idle tab on 2026-09-15). Every 401 on an API call and every
  refused WebSocket handshake now checks `/api/auth/status`; with login
  enabled and no role, the page stops its status poll and its WebSocket
  and shows the login screen with "Session expired" — the same end state
  as an explicit logout. A network outage still reconnects as before.

## 3.77.2

### 2026-09-15 — one read-back series per command, not one per send

- The read-back series after a command's revert timer (3.75.2) was armed
  for every command and never cancelled. Node-RED's OV re-sends the
  limit every 30–60 s while a step holds, so on the first real
  over-voltage day 514 commands stacked 1681 extra `controls` sweeps on
  the Datamanager in 3.5 h — ~50 Solar API ok→degraded flaps an hour and
  latency alerts even at 2500 ms. A new command restarts the inverter's
  own revert timer, so only the latest series matters: it replaces the
  pending one for the same device and command, and a callback of a
  superseded series sweeps nothing.

## 3.77.1

### 2026-09-15 — a device's "down" alert waits 45 s

- The Fronius Datamanager stalls for a few seconds several times a day
  (it also serves the Cerbo); the single-source site unit went down and
  back within one harvest and fired an error alert each time (three
  "PV installation: down — not responding" this morning, each recovered
  5 s later). The transition is still logged at once; the alert fires
  only if the device is still down after 45 s, and the recovery alert
  only after a down alert was sent.

## 3.77.0

### 2026-09-15 — the morning audit: a day counter that holds, HA availability per device

- `daily: true` on a register: a day counter the source recomputes from
  whatever is awake (Solar API `Site.E_Day` is the sum over the inverters
  still answering — it fell 272.8 → 199 → 137 → 125 kWh at sunset on
  2026-09-14, and Home Assistant's `total_increasing` statistics read every
  drop as a meter reset). `DailyCounterFilter` serves the day's maximum
  through such dips and adopts only the midnight reset (below 10 % of the
  held maximum). Wired on both pollers behind `apply_corrections`; the
  Solar API site template declares it on `energy_today`.
- HA discovery: a device's value entities now carry the DEVICE's own
  availability topic (`<prefix>/availability`, what
  `publish_device_availability` writes), not the gateway's primary status
  — every MBG entity used to follow the Janitza, so sleeping inverters
  showed as available all night.
- Production: `alerts.latency_ms` 1000 → 2500. The Datamanager is also
  polled by the Cerbo (~1 request/s per inverter); 1–3 s answers are its
  normal, and 75 latency alerts in two hours were noise.

## 3.76.0

### 2026-09-14 — rules: a fail-closed stale policy and a history in InfluxDB

- `on_stale` accepts a number: the value to ask for while the signal is
  stale (Node-RED's OV failed closed to 80 % when blind; `hold` only keeps
  a curtailment that is already on). The UI offers it as "ask for a fixed
  value (fail closed)".
  Production keeps `hold`: on this plant the phase voltages read 0 V once
  the inverters sleep, so a fixed value would be written to sleeping
  inverters every evening.
- Every notable decision (a state or want change, every command sent) is
  written to the unit's bucket as `rule_event` — tags device, rule, state,
  action, result; fields signal, want_value, actual, reason. This is the OV
  history pv-stack-ui's panels read once the rules own the limit (what
  Node-RED's `ov_event` rows were).

## 3.75.2

### 2026-09-14 — the post-revert read-back is a short series

- Measured with the controls block polled every 15 s: the Symo clears
  `power_limit_enabled` 114–127 s after a write with `revert_s` = 120, and a
  single read 5 s past the mark still saw it enabled once (its timer ticks
  coarsely). The read-back after the revert is now a series at +5, +20, +60
  and +180 s past the mark (`REVERT_REREAD_AT_S`), each sweep arming the next.

## 3.75.1

### 2026-09-14 — a command's revert timer is followed by a read-back

- A power limit sent with `revert_s` reverts on the inverter when that timer
  fires, with no write from the gateway — and the group that carries the
  limit (`controls`) is swept once an hour on purpose. Seen live on the 95 %
  test: the inverter was back at 100 % at 14:22:36, the gateway still
  published 95 % / enabled at 14:23:54, and would have for up to an hour.
  The command runner now arms a timer at `revert_s` + 3 s that sweeps the
  read-back group through the unit's Modbus part, so MQTT, InfluxDB, HA and
  the rules see the revert within seconds. Nothing is armed without a revert.

## 3.75.0

### 2026-09-14 — the per-phase AC block over the Solar API; SunSpec slows down

- New template `fronius_solar_api_inverter_3p`: one HTTP request per inverter
  (`DataCollection=3PInverterData`) for the three L–N voltages and phase
  currents, poll group `realtime` at 5 s. Its registers carry the SunSpec
  addresses of the same points, so the HA unique_ids and the Influx `address`
  tag do not change with the source that owns the field.
- Why: the over-voltage protection (Node-RED OV, the `ov-u*` rules, alertd's
  ANRE rules) keys on max(L1, L2, L3). Four inverters on one datalogger could
  not sweep the SunSpec `normal` block under ~25 s (21–33 s at a 20 s
  interval; sources ok↔degraded ~50 times per unit per 3 h; read latency
  alerts at 1.6–3.9 s). The Solar API answers from the web server's cache in
  ~54 ms without touching the Modbus side.
- Production: the `pv` endpoint's inverters declare `solar_api` (5 s — it was
  2 s; the web server's cache refreshes every ~2.6 s and at 2 s × 4 units
  the HTTP side itself flapped ok↔degraded), `solar_api_3p` (5 s) and then
  `sunspec`, whose `normal` group moved from
  20 s to 60 s (it still owns PF, VA/var, event flags, temperatures, the
  operating state and the MPPT block). Modbus traffic on the datalogger drops
  from ~12 to ~6 transactions a minute.

## 3.74.2

### 2026-09-14 — derived measurements are HA entities

- A device's calculated registers (the Fronius `status/text`, `status/alarm`,
  `status/active`) are published in HA discovery like its read registers,
  and cleared with them when the device is deleted.

## 3.74.1

### 2026-09-14 — HA discovery for units read through sources

- `Config.unit_registers(device)`: everything a unit reads across its
  sources, one entry per register name (the first source that declares it
  wins). HA discovery, the delete-time discovery clear and the device entry
  use it — a unit read through Solar API + SunSpec published only its
  connectivity sensor before, because the device-level file is empty.


## 3.74.0

### 2026-09-14 — rules: the declarative controller

The gateway can now decide *when* a command runs — narrowly, declaratively,
never through scripts. Design in [docs/rules-design.md](docs/rules-design.md).

- **Inputs are registers.** A rule reads `device.register` like a calculated
  register does (same safe grammar, hyphenated device ids allowed); a remote
  system enters through an `mqtt` device on its own broker. The oldest input's
  age is the signal's age.
- **Two kinds.** `steps` — ascending thresholds → a value, with a release
  threshold under the first step (the dead band), an optional immediate step
  (the emergency path), a valid signal range; `condition` — a boolean → run
  A / run B.
- **Time semantics, fixed fields.** Debounce, minimum interval between
  commands, re-command only when the read-back drifts from the want for
  `reassert_s` (closed loop, never a heartbeat), a missing or old read-back
  asks for a sweep instead of a blind write.
- **Fail closed.** A stale signal holds the last want (or asks for the
  command's `safe`); disabling, un-arming or deleting an armed rule that moved
  its target restores the safe values first. A want the device already holds
  is not sent.
- **Shadow and armed.** New rules are shadow: they decide, log and publish,
  write nothing. Arming needs `security.allow_writes` and authentication and
  is audited. An armed rule owns its target: other faces are refused with
  `owned by rule` unless `override_s` pauses it. Two armed rules cannot share
  a target.
- **Clamp and override.** A ceiling on the want with an expiry that never
  expires into a step; a pause for a stated time. Both over API and
  `mbg/rules/<id>/set` (with `mqtt.allow_write_entities`).
- **Faces.** `GET/POST/PUT/DELETE /api/rules`, `/validate` (what it would
  decide now), `/mode`, `/enable`, `/clamp`, `/override`, `/decisions`;
  retained `mbg/rules/<id>/state`, `…/event`. Every command a rule runs is
  audited as `via: rule:<id>`; three failures in a row raise an alert.
- **UI.** A *Rules* page: one card per rule with state in words, signal,
  want → actual per unit, last decision and its reason, Arm / To shadow,
  Clamp…, Pause…, Decisions, Edit, Delete; an editor built from the rule's
  parts with *Preview now*. English and Romanian.
- Storage: `rules.yaml` and `rules_state.json` next to `config.yaml`.


## 3.73.0

### 2026-09-13 — commands: the universal write path

The hard-wired power limit of 3.72.0 becomes the first *command preset*. A
controller says **what** it wants (`power_limit = 60 %`); the device's
template says **how** that is said to this device; the gateway applies it
safely, verifies and consigns it — and never decides *when*. Design in
[docs/commands-design.md](docs/commands-design.md).

- **Engine** (`multibus/commands.py`, vendor-blind). A command declares its
  parameters (bounds, defaults, units, legacy aliases), a guard (the recipe
  applies only when e.g. `controls_model_id == 123` and the scale factor is
  plausible), the registers it writes with tiny expressions
  (`${value}`, `if ${value} < 100 then 1 else 0`), a settle time, the
  read-back it verifies, its `safe` parameters, and the poll group to sweep
  after. Consecutive holding registers travel in one FC16 frame; the scale
  factor is read, never assumed. Verdicts: `success`, `mismatch`,
  `unverified`, `rejected`, `error`, `dry_run`. Unknown parameter names are
  refused, never ignored.
- **Templates and bindings.** `fronius_sunspec_inverter` ships
  `commands.power_limit` and `commands.restore` (an alias: `power_limit`
  100 %, revert 0). A preset is *offered* by the template and *enabled* by a
  binding on the group or device (`commands: [{name: power_limit}]`, with
  optional `faces`, `confirm`, `lease_s`); an inline recipe with `writes` is
  validated like a template's. Nothing unbound runs.
- **Faces.** API: `GET /api/devices/{id}/commands`, `POST …/commands/{name}`,
  `…/dry-run`, the group route, `GET /api/commands/history`. MQTT:
  `<unit|group prefix>/cmd/<name>` (a number or an object; `source` names the
  caller), `…/cmd/result`, and the retained `…/cmd/<name>/state`. Home
  Assistant: the number entity of a register a command writes first runs
  the command. The 3.72.0 routes and topics keep working as aliases
  (`limit_pct` → `value`). One gate set everywhere: `allow_writes`,
  write-lock, authentication, rate limit, `mqtt.allow_write_entities` for
  the broker; every run audited as `action: command` with parameters, frames,
  before/after and the face it came through.
- **Lease.** `lease_s` arms the gateway's dead-man: when it expires
  unrenewed, the command's `safe` parameters are run (100 %, enable cleared).
  A write of the safe value clears the lease.
- **UI.** The inverter card's *Limit…* becomes *Commands…*: one dialog built
  from the command's declared parameters, a scope (group or one unit), *Test*
  (dry run: the exact registers, nothing written) and *Run* with
  confirmation, verdicts in words per unit. The group editor gains *Commands
  this group accepts*. The unit page gains a *Commands* tab: each command
  with its parameters, Run / Test, the last result, and the recent history.
- **Removed.** `multibus/power_limit.py`; `tests/test_power_limit*.py`
  became `tests/test_commands.py` / `tests/test_commands_api.py` /
  `tests/test_poller_kick.py`.


## 3.72.0

### 2026-09-13 — the active power limit (SunSpec model 123)

The gateway now applies a power limit safely and consigns it. It never
decides to limit on its own: that is a controller's policy (over-voltage
protection, a schedule) — Node-RED keeps it, and speaks to the gateway.

- **Read back.** The `fronius_sunspec_inverter` template gains the model-123
  block — `power_limit_pct`, `power_limit_enabled`, `power_limit_revert_s`,
  `power_limit_ramp_s`, `controls_connected` — in its own poll group
  `controls` (one read, 40231..40250, once an hour — every power-limit write
  sweeps it at once, so the read-back reaches MQTT in seconds; every
  transaction on this datalogger is dear), published as `…/controls/*`. The installation page shows the limit
  per inverter.
- **The action.** `POST /api/devices/{id}/actions/power_limit`
  `{limit_pct, revert_s=600, ramp_s=0, lease_s=0}` and
  `POST /api/endpoints/{id}/groups/{gid}/actions/power_limit` (every unit,
  one result each): verifies model 123 and the scale factor (only {-2,-1,0},
  and the same as last time), writes WMaxLimPct…WMaxLim_Ena in ONE frame,
  clears the enable bit at 100 %, settles, reads back, and answers `success`
  / `mismatch` / `unverified` / `rejected` / `error` with before → after.
  `revert_s` is the inverter's own revert (default 600 s — a dead controller
  never leaves the plant throttled); `lease_s` is the gateway's dead-man that
  restores 100 % itself.
- **Every channel, one path, one audit.** MQTT `<unit>/cmd/power_limit`
  (a number or `{limit_pct, revert_s, ramp_s, source}`; the legacy
  `revert_timeout`/`ramp_time` are accepted), `<unit>/cmd/restore`, the
  group-wide `<group>/cmd/power_limit`, results on `<unit>/cmd/result`; the
  HA number entity for `power_limit_pct` is the action too, never a bare
  register write. Each write lands in the audit log (who, channel, unit,
  before → after, verdict) and in the unit's Logs.
- **UI.** *Limit…* on the inverter card: limit, reverts after, ramp, scope
  (the group or one unit), a confirmation, and the result in words per unit.
- Gates unchanged: `security.allow_writes`, `write_locked` per unit, login
  or API key, the rate limit; MQTT/HA additionally need
  `mqtt.allow_write_entities`.

Tests: `tests/test_power_limit.py` (the sequence), `tests/test_power_limit_api.py`
(every channel); e2e `tools/e2e/power_limit_e2e.mjs`.

## 3.71.0

### 2026-09-13 — the last three audit findings

- **Source and group dialogs in plain words.** *Read over* with protocol
  names (Modbus TCP, Solar API / HTTP JSON…) instead of `tcp`/`http`; only the
  address fields of the chosen way (the rows the page hid were shown anyway —
  `display:flex` beat `[hidden]`); the template list follows the kind of unit
  and the way it is read (every map of the transport when none is known for
  the kind); intervals as three labelled numbers — power/voltages/currents,
  energy counters, static data — instead of `normal=20, slow=120`; *Stale
  after* instead of *Yields after*; *Holds* with names (Inverters, Site
  totals, Grid meter…) instead of `Role —`; *Read this group / Read through
  this source*.
- **Dashboard chips grouped by installation**: standalone devices first, then
  each installation's units together under its name, each chip carrying only
  what distinguishes the unit.
- **`sources[].last_success_ts`** is set for HTTP and MQTT sources too (it was
  `null` while `successful_reads` climbed).

With this every finding of the installations UI audit (operator notes) is closed.

## 3.70.0

### 2026-09-13 — the Add Installation wizard asks what the operator knows

Fifth step of the audit. The wizard forced a Modbus host on a Solar-API-only
installation and tested Modbus alone; step 2 offered all fifteen templates to
an inverter group and asked for unit ids the datalogger knows; step 3 took
intervals as `normal=20, slow=120`; the review named `mbg/endpoints/…` while
the groups would publish elsewhere.

- **Step 1 — the installation.** Name (the id follows it), **how the
  datalogger is reached** — Solar API, Modbus TCP, or both (recommended) — its
  address, the Modbus port only when Modbus is in play, and **where it
  publishes** (`pv` → `pv/inverters/N/…`, `pv/site/…`). **Test** checks exactly
  what was ticked and says so in words; the Solar API check *is* the
  discovery.
- **Step 2 — what it holds.** What the datalogger reported, as ticks:
  Inverters (n, with their ids), Site totals, Grid meter (unticked on purpose —
  grid data is better read from the meter itself). Templates follow from role
  × transport; a group added by hand sees only templates for its kind and its
  way of being read. Manual fallback when the datalogger does not answer.
- **Step 3 — how often.** Labelled numbers per way of reading: Solar API
  `power, voltages, currents every 2 s · energy counters every 30 s`; Modbus
  `the complete reading every 20 s · counters and static data every 120 s`;
  the measured floor beside it when the datalogger is slow; one field for how
  long Solar API stays authoritative before Modbus takes its fields back.
- **Step 4 — review.** Group, units (ids), read via (source and interval),
  the real topics and the totals topic, the device ids; create lands on the
  installation page.
- A group nothing can read is not created; a bundled template is used only
  when the gateway has it; no Modbus fallback host is invented for a
  Solar-API-only installation.

`GET /api/fronius/discover` honours `security.allow_nonlan_http_devices`, so a
lab stand-in on loopback can be discovered (tools/e2e/fake_solar_api.mjs).

Tests: e2e `tools/e2e/plant_wizard_e2e.mjs` rewritten against the stand-in
(26 checks).

## 3.69.0

### 2026-09-13 — the Measurements tab: what is read, where, how often

Fourth step of the audit. The picker filed every Solar API field under
"other" while the Monitor grouped the same fields as `Power_active / Dc`,
showed invented numeric addresses for JSON paths, a dash in every VALUE cell
of a unit that was live, MONITORED/REALTIME badges, a delete cross on rows
that came from the template, and five buttons in a row.

- **One classification.** The category of a measurement comes from its
  canonical name on the server (`power`, `voltage`, `current`, `energy`,
  `frequency`, `dc`, `quality`, `site`, `temperature`, `status`), falling back
  to the unit, then `other`. The catalog groups by it; the selection carries
  it; the filter and the Selected tabs use the same labels.
- **Selected first**, then **All available** — the same columns in both:
  tick · Measurement (label + canonical name) · **Where** (the JSON path, the
  topic, or `40071 · uint16 · ×0.1`) · **Value** (live, joined by *name*, with
  its age) · **Interval** (the source's poll group in words: `every 2 s`) ·
  actions. Rows are grouped under category headers.
- **Ticking in place saves at once** in both views; every view of the
  selection follows (counts, picker census, Selected list). Template rows
  carry a lock — untick, don't delete; custom rows keep their delete.
- **Read from** picker: `solar_api · HTTP · every 2 s · 6/7 ticked`, one button
  per source with `aria-pressed`; the first source's map loads at once (it
  used to show an empty Selected list under a picker that said 3/7 ticked).
- **One menu** (`⋯ More`, `aria-haspopup`, arrow keys, Escape) for Import CSV,
  Upload map, Download map, Raw JSON and Write — Write only where the map
  declares something writable and the device is not locked; Query only for
  Modbus sources.

API: `/api/registers/all` files canonical fields under their category with a
label; `/api/registers/selected` registers carry `category`, and `sources[]`
carry `selected`, `catalog` and `interval_s`.

Tests: `tests/test_register_categories.py`; e2e `tools/e2e/measurements_e2e.mjs`
(24 checks); `source_registers_e2e` still green.

## 3.68.0

### 2026-09-13 — the unit page tells how the unit is read

Third step of the audit. A unit of an installation opened on an **Edit** tab
that described a Modbus connection nobody declared — empty host, `:502`,
timeout 3, `(no template)`, template intervals — for a unit read over HTTP
every two seconds. *Save intervals* was a silent no-op (the source's intervals
are the last word) and *Test connection* probed the imaginary host. Its back
arrow lost the installation.

- **Breadcrumb** `Devices › PV installation › Invertor 1`, every level a link;
  the back arrow returns to the installation; an **Open installation** button.
- **Overview**: health as a word, the unit's glance values by what it is
  (power, AC and DC voltage for an inverter), last read, then **Read via** —
  every source with protocol, address (with the unit id), interval, latency,
  five-minute failure rate and the fields it supplies right now — and where the
  unit publishes. **Live values** are grouped by what they measure, each with
  the source it came from when the unit is read more than one way.
- **Read via** tab replaces Edit on a unit: the sources table (order, protocol,
  address, template, interval, stale-after, fields provided / selected, live
  verdict) and the one thing that is the unit's own — its **name**. No
  connection form, no *Save intervals*, no *Test connection*. A standalone
  device keeps its Edit tab untouched.
- **Outputs** point to the installation, not to a dialog.
- **Devices list**: a unit row says `solar_api HTTP 2 s · sunspec Modbus TCP
  20 s · 50 measurements`; an installation row says what it holds and how it
  is read, with power now and units answering. A unit's measurement count is
  the union of its sources' selections.
- **Status → Polling & threads**: protocol is the sources' (`http + tcp`), and
  latency is coloured against the source's own timeout, not a fixed Modbus bar.
- **Footer** on a unit page: `read every 2 s (solar_api) · 20 s (sunspec)`;
  the gateway's groups everywhere else.
- A flat rename (`units: [{unit_id, id, name}]`) on a grouped installation now
  lands on the unit inside its group instead of being discarded with the flat
  list — the rename from both pages depends on it.

API: `GET /api/devices` entries of installation units gain `endpoint_name`,
`group_id`, `role`, `live`, `fields` and `read_via[]` (`id`, `protocol`,
`address`, `template`, `interval_s`, `timeout_s`, `stale_after_s`,
`registers`, `provides`, `status`, `latency_ms`, `reads_5m`, `failed_5m`,
`fail_pct_5m`); `/api/status` device rows gain `read_via` (`id`, `protocol`,
`timeout_s`).

Tests: `tests/test_endpoint_page_model.py`, `tests/test_endpoint_edit_guard.py`;
e2e `tools/e2e/unit_page_e2e.mjs` (23 checks).

## 3.67.0

### 2026-09-13 — the installation page, in the operator's order

The audit's second step. The page used to open with the fallback connection
(`TCP :502`, `Template —`, "no transactions measured yet") and a total summed
across groups; what an operator wants first was below the fold. It now reads
top to bottom: **is it producing** — **is every unit fine** — **how it is
read** — **where it publishes**.

- **Header**: status + census, then four numbers — producing now, today,
  autonomy, self-consumption — from the site group when the datalogger gives
  one, else the inverters' sum and a dash for the rest; nothing invented. Then
  **Read via**: every way the installation is read, each with its protocol,
  interval, latency, **failure rate over the last five minutes** (not a counter
  since boot) and units answering. Then the real per-group topics it publishes
  on.
- **Group cards** named by what they hold (Inverters, Grid meter, Site totals),
  with the unit table showing what matters for that kind of unit — power, AC
  and DC voltage for an inverter; power, imported, exported for a meter — plus
  health as a word, last read, and each source's verdict for that unit ("read
  fine over HTTP, Modbus side down" in one row). The group total sits under
  the table with its topic. **How it is read** (the sources table, the bus
  telemetry) is folded and stays open across the live tick.
- **Gone**: the cross-group "Endpoint output" total (inverter generation plus
  a meter's import describes nothing), the fallback-connection header, the
  endpoint default topic no group used, `rank / primary / owns / yields after`
  (now `#1`, `provides`, `stale after`). A group of ONE unit publishes no total
  — it was that unit republished under another name (`pv/site/summary`
  duplicated `pv/site`).
- Every icon button carries an `aria-label`; status dots are decorative next to
  their word; tables scroll inside their card on a phone.

API: `GET /api/endpoints/{id}` gains `headline`, `read_via`, `fields`,
`units[].live`, `units[].sources`, `groups[].outputs`, `groups[].aggregate_fields`;
per-source `reads_5m` / `failed_5m` / `fail_pct_5m` / `interval_s`; a grouped
installation's `aggregates` is empty. Romanian strings for the whole page.

Tests: `tests/test_endpoint_page_model.py`; e2e suites adjusted
(`endpoint_page`, `plant_groups`, `plant_wizard`), all green.

## 3.66.0

### 2026-09-13 — the installation page cannot lie or destroy

The installations UI audit (operator notes) found three things on the
installation page that were worse than confusing. This release closes them
before any of the page is redesigned.

**Edit → Save flattened the installation.** The Edit dialog sent the flat
shape it was born with — `units`, `connection`, no `groups` — and the server
took it literally: groups collapsed to one Modbus group, every source vanished,
the site unit was reborn as `pv-u0`. Two guards now: the server keeps the
stored `groups` and `sources` whenever an edit does not mention them (an edit
that sends them still replaces them), and the dialog of a grouped installation
edits only what it shows — the name and the output switches. Units, sources and
topics stay on the group cards where they are edited.

**Test units probed a Modbus host nobody declared.** It used the endpoint's
top-level connection, which a grouped installation does not have, and answered
"blocked: host required" for units that were being read fine over HTTP. It now
asks every unit over every source it is read through — the Solar API by URL
(judged by how many of the unit's paths the document carries), Modbus by one
read on the source's own connection; a pushed input says "not probed" rather
than pretending. The result appears under the header where the button is, one
block per source, and every verdict is a word ("answered", "no answer") with
the colour as a second cue, so it reads the same to a screen reader.

**A unit with nothing to read was green.** `data_health()` answered `ok` when
no register was selected. It is `idle` now — grey, named so, and not counted
online — in every driver; a source with nothing selected does not vote on its
unit's verdict. A unit that HAS a selection but whose poller never started
stays on the degraded path, as before: not idle, not green.

`GET /api/endpoints/{id}` unit health may now read `idle`; `POST
/api/endpoints/{id}/test` rows carry `source`, `protocol`, `where` and a
per-source `sources[]` census (`answered`/`probed`/`total`).

Tests: `tests/test_endpoint_edit_guard.py`, `tests/test_endpoint_test_probe.py`,
idle cases in `test_liveness.py`/`test_new_features.py`;
`tools/e2e/installation_safety_e2e.mjs` (22 checks).

## 3.65.0

### 2026-09-13 — ticking fields per source

A unit reached two ways has two maps: the SunSpec view reads holding registers,
the Solar API view reads JSON paths. They share no address, no shape and no
intervals. Editing them as one list made neither editable.

The Measurements tab now shows a **source picker** — and only when there is a
choice, so a single-source device looks exactly as it always did. Switching
source switches the catalog, the selected list and the poll groups together, and
a save lands in that source's file alone.

`/api/registers/all` and `/api/registers/selected` both take `?source=`, and the
selection response carries the unit's sources so the editor needs no second
call.

**Three defects found by building it, all silent:**

- `MultiSourceClient` had no `update_registers`, so saving a register selection
  from the UI would have raised on **every** device since 3.58.0. It now takes a
  `source_id`, applies to that source alone, and with several sources refuses an
  unaddressed update rather than pushing one source's map into all of them —
  each would then poll addresses that mean nothing to it. A source whose map
  changes also forgets what it owned, so fields it no longer reads fall to
  whoever else offers them.
- The register CATALOG resolved its template from the device, which is empty
  when the templates live on the sources. The picker came up blank with nothing
  to say why. It now resolves from the source being edited.
- Switching source loaded the new map but did not redraw the visible lists, so
  the operator kept looking at the previous source's fields — the most
  misleading thing that screen could do, since ticking a box then edits the
  wrong map.

Verified in a real browser, 11 checks: the picker appears only with a choice,
the selected list genuinely redraws (2045 characters of SunSpec fields against
196 of Solar API), and a per-source save moved one map from 6 fields to 3 while
the other stayed at 34.

## 3.64.0

### 2026-09-13 — the installation itself is a source, and topics have a root

**Measured first, on the production DataManager in daylight.** How fast a value
can honestly be read is a property of the source, so it was measured before
anything was designed:

| data | refreshes at source | useful poll |
|---|---|---|
| inverter power (per unit and site) | 2.5 – 3.3 s | 2 s |
| grid meter, all 37 fields | 1.1 s | 1 s |

Sampling twice a second for 40 s returned 62 samples and 17 distinct values: the
datalogger polls its own RS-485 side on its own rhythm and we cannot outrun it.
So a 1 s poll is real for the meter and **not** for the inverters — asking for it
there returns the same number two or three times.

**`fronius_solar_api_site`** reads `GetPowerFlowRealtimeData.fcgi`, one 89 ms
call describing the whole installation: PV generation, house load, grid flow,
battery, autonomy, self-consumption and the day/year/lifetime counters. House
load, autonomy and self-consumption are **not derivable** from the inverters —
nothing in an inverter's register map knows what the house consumed — so this is
data MBG simply could not produce before.

Its curated defaults say what a user should tick. Grid and battery power ship
UNTICKED: a meter wired at the grid connection reports the same flow first-hand
and faster (1.1 s against 2.5 s), so taking it from the site view would be a
slower second copy. Sign convention follows the source — grid negative while
exporting — because flipping it would make our number disagree with the
inverter's own display.

**Nine site concepts joined the canonical dictionary** (`power_pv`,
`power_grid`, `power_load`, `power_battery`, `energy_today/year/lifetime`,
`autonomy`, `self_consumption`) rather than being invented inside one template,
so any vendor's site view lands on the same names.

**Topics have a configurable root.** `mqtt.aggregate_prefix` on an installation,
with `${endpoint_id}` and `${group_id}`, so a tree can read `pv/inverters/summary`
instead of `mbg/endpoints/fronius/inverters` — and two sites on one gateway no
longer collide. The default is byte-identical to what exists today.

Two defects found by building it, both silent failures:

- A ratio matched no aggregation rule, so `autonomy` and `self_consumption` were
  dropped from the totals with nothing said. They are averaged now: two
  installations that are each 100 % autonomous are not 200 % autonomous.
- A topic pattern placing `${group_id}` mid-path collapsed to `pv//summary` for
  the first group, which is a DIFFERENT topic to a broker — subscribers to
  `pv/summary` would never have seen it.

## 3.63.0

### 2026-09-12 — Solar API is a way of reading, not an afterthought

The wizard treated HTTP as an add-on to Modbus: you could bolt a Solar API
source onto a Modbus group, but you could not build an installation read ONLY
that way. On a Fronius that is backwards — the Solar API answers in a 54 ms
median against 1945-2376 ms for Modbus on the same box, so for power, energy,
frequency and U/I it is simply the better resolution.

Step 3 now offers **two independent choices per group**, `Read over Modbus` and
`Read over HTTP / Solar API`, with at least one required. Either alone is a
complete installation; both together put the faster one first, so it owns every
field it offers and the other fills in the rest. With Modbus beside it the HTTP
source yields after 30 s of silence, which is automatic failover; alone it never
yields, because there is nothing to yield to.

**Presets fill the exact call.** Typing a Solar API URL by hand is how a plant
ends up silently reading nothing: one wrong query parameter still returns 200 OK
with an empty body. One click writes
`GetInverterRealtimeData.cgi?Scope=Device&DeviceId=${unit_id}&DataCollection=CommonInverterData`
or the meter equivalent, with `${unit_id}` in place and the matching template
selected.

The source is named `solar_api` rather than `http`, because that is what it is.

**Verified in a real browser, 26 checks**, including that a group with no way to
be read is refused before anything is created, that the preset fills the call
and picks the template, and that the created installation carries both sources
in precedence order. Separately confirmed against the live DataManager that an
installation declaring ONLY a Solar API source materializes with no Modbus
anywhere.

## 3.62.0

### 2026-09-12 — the Add Installation wizard

Adding a device asked three questions; adding a whole installation asked one.
That was backwards, and the old single form could not express what an
installation actually is. It is replaced — not sat beside — by a four-step
wizard, so there is one way to do this rather than two that differ.

1. **Installation** — id, name, and the master device's address, with a Test
   button that probes it before you go further.
2. **What it holds** — a card per group. It opens on the group nearly every
   plant has (inverters), and choosing a role names the group for you. Add the
   meter at the grid connection, a battery, sensors.
3. **How it is read** — intervals and timeout per group, and a checkbox to add a
   second, faster HTTP source. When ticked it is placed FIRST, so it owns every
   field it offers and Modbus fills in the rest; if it goes quiet for 30 s the
   fields fall back on their own.
4. **Review** — every group with its units, template, sources in precedence
   order and the topic it will publish on, plus the exact device ids that will
   be created.

The refusals happen BEFORE anything is created: a malformed id, a group without
units or template, and — the one that matters — a unit id claimed by two groups,
which is one address on the wire and would be two devices racing it.

`Add Endpoint` is now `Add Installation` in both languages, because "endpoint"
is our word, not the operator's.

**Verified in a real browser, 23 checks** (`tools/e2e/plant_wizard_e2e.mjs`),
which walk the wizard and then compare what was created against what the review
promised. The plant-groups suite is at 25/25 and the device-logs suite at 10/10;
one brittle assertion in the groups suite was made relative rather than
absolute, since an arrow raises a source by one place regardless of what else
the group holds.

## 3.61.0

### 2026-09-12 — the plant page: group cards, per-group sources, and the editors

The endpoint page now renders **one card per group**, each carrying its own
sources, its own units and its own totals topic. A card states what the group
IS (inverter, meter, battery), whether it polls, and where it publishes; the
first group is marked `primary` because it owns the endpoint's headline topic
and its untagged InfluxDB series.

Everything an operator does to a plant is now a click:

- **add / edit / delete a group**, with role, template and unit ids — a unit's
  hand-written id and name survive editing the id list, so editing a group never
  renames its devices or orphans their history
- **enable or disable a group on its own** — its units stay visible and
  editable, they simply stop polling
- **add / edit / delete a source within a group**, and **reorder it with
  arrows**, because order is the precedence
- the last source of a group, and the last group of an endpoint, cannot be
  removed: a unit needs a way to be read

The API grew `groups[]` on `/api/endpoints/{id}`, each with its units, sources,
aggregates and topic, and validation to match: a group needs units and a real
template, and **two groups may not claim the same unit id** — that is one
address on the wire, and two devices would race it.

Fixed while testing: an endpoint that declares groups no longer demands a
top-level `connection.host`. With groups the address belongs to each group, and
the top-level connection is only the shorthand for a single implicit one.

**Verified in a real browser, 25 checks** (`tools/e2e/plant_groups_e2e.mjs`)
walking that entire path, plus the two existing suites updated to the group
layout and passing at 22/22 and 10/10.

## 3.60.0

### 2026-09-12 — a plant is an installation, not one kind of device (P8, step 4)

A PV plant holds inverters AND the meter at its grid connection. Modelling that
as two endpoints was the same duplication we removed at the source level, one
level up: two aggregates, two pages, two things to keep in step, for one
physical installation.

An endpoint now holds **groups** — zero, one or many — each with its own role,
template, connection and sources:

```yaml
endpoints:
  - id: fronius
    name: Fronius PV
    groups:
      - { id: inverters, role: inverter, template: …, units: [1,2,3,4], sources: [...] }
      - { id: grid, role: meter, template: …, units: [{unit_id: 240, id: fronius-meter-240}] }
```

A plant may have no meter, one, or several, and a meter group reaches its units
over Modbus or the Solar API like any other — a group is just "these units, this
way, this map".

**Groups aggregate separately, and that is the point.** Inverters measure
generation; the meter at the grid connection measures import and export and is
negative while exporting. One sum over both is not a smaller truth, it is a
wrong number. A test pins it: 3000 W + 2000 W of generation stays 5000, the
meter's -4500 W stays on its own, and the meaningless 500 is never produced.

**Nothing existing moves.** An endpoint that declares no groups is one implicit
group holding its flat units, so every endpoint written before today keeps its
device ids, its topics and its totals to the digit. The FIRST group keeps the
bare `mbg/endpoints/<id>/…` path and its untagged InfluxDB series; later groups
publish under `mbg/endpoints/<id>/<group>/…` and carry a `group` tag. A tag
changes series identity, so adding one to the first group would have orphaned
its history — it does not get one.

A unit's hand-written id survives being grouped, so moving `fronius-meter-240`
into the plant keeps its topic, its tag and its history. A group can be disabled
on its own: its units stay visible and editable, they simply stop polling.

## 3.59.0

### 2026-09-12 — health on two levels, and the Sources card (P8, step 3)

**A unit read two ways is `ok` only when EVERY source is.** When the Modbus side
of an inverter dies we still get power and frequency over HTTP, but we have
silently stopped collecting power factor, reactive power, the event flags and
the MPPT strings. A green light there would be a lie, so the unit goes
`degraded` — `down` only when nothing is left. A single-source device judges
exactly as it always did: its one source's verdict IS the unit's.

Every source transition is recorded as an event (`source_down`, `source_ok`),
edge-triggered so a source that stays down is one entry rather than a stream,
and the message names what still reads: *"source sunspec ok -> down; still
reading through solar_api"*. Those reach the device Logs tab and from there the
alert harvester. A driver with no health verdict of its own is judged by its
socket rather than skipped — skipping would let a broken source hide behind a
healthy sibling, which is the one failure this scheme exists to prevent.

**The endpoint page gains a Sources card**: each source with its rank, protocol,
address, template, intervals, yield window, live census, latency, failed reads,
and how many fields it is currently authoritative for. That last number is the
one that tells an operator whether a source earns its place. Arrows reorder,
because order IS the precedence, and a reorder writes straight through. Add,
edit, enable and remove are there too; the last remaining source cannot be
removed, since a unit needs a way to be read.

`GET /api/endpoints/{id}` now carries `sources` as flat JSON — configuration and
live state in one object, because "which source is this value from" and "is that
source still alive" are the same question. Readable by a Node-RED http-request
node without unwrapping.

**Recorded in the Solar API template, measured at 19:31:** when an inverter
stops producing, the DataManager OMITS `PAC`, `FAC`, `UAC` and `IAC` from the
response entirely rather than returning zero, and the call still succeeds. Under
the source model this needs no handling at all — a field nobody offers falls to
whichever source still does, and a SunSpec source beside it supplies them all
night. The template says explicitly not to "fix" the absence by defaulting to
zero: a fabricated zero is indistinguishable from a real one.

## 3.58.0

### 2026-09-12 — the runtime reads a unit several ways at once (P8, step 2)

A device now starts one driver per SOURCE, all feeding one store under one
identity. Verified against the production DataManager with two sources on one
inverter:

| source | rank | latency | owns |
|---|---|---|---|
| `solar_api` (HTTP, 5 s) | 0 | 236 ms | 6 fields — power, frequency, AC/DC voltage and current |
| `sunspec` (Modbus, 20 s) | 1 | 794 ms | 31 fields — per-phase, energy, event flags, scale factors |

Zero handovers: each owns what it is best at. Declaring them the other way round
correctly gives the contested fields to Modbus instead — the order is the
operator's lever, and it is the only lever needed.

- **`FieldArbiter`** (`source_arbiter.py`) decides ownership per canonical field
  name, never per address: the two sources read `power_active_total` at Modbus
  40083 and at JSON address 1, so by address they would never collide and both
  would write it unopposed. A losing source is suppressed BEFORE the store, MQTT
  and InfluxDB, so it leaves no shadow value behind, and every stored value
  carries the `source` that produced it.
- **`MultiSourceClient`** presents exactly the surface a single driver does
  (`connect`, `start_polling`, `disconnect`, `get_stats`, `data_health`,
  `write_value`, `connection`), so the registry, the API, the alert harvester
  and the virtual meters are untouched. Counters sum, the freshest success wins,
  health is the best source's, and writes go to the first source that can
  perform them — a Solar API is read-only, a setpoint belongs on SunSpec.
- **One builder for boot and runtime.** There were two device constructors and
  they drifted invisibly until a restart brought a device back decoding with a
  different byte order. A test used to pin the copies together;
  `device_runtime.build_device_client` removes the second copy instead. Every
  device goes through `MultiSourceClient`, single-source ones included, so
  production never runs a rarely-exercised branch.
- **Per-source registers and seeding.** A source has its own template and
  address space, so it seeds into its own file under
  `devices/<device>/sources/<source>/`. A device that never declared sources has
  no such directory and falls back to its device-level file, so nothing existing
  moves. A source polls only the groups it declares.
- **`GET /api/devices/{id}/events`** now returns `sources` and `provenance` as
  flat JSON: how each source is doing, and which source owns each field right
  now. "Why does it say 1404 W" has an answer, and a Node-RED http-request node
  can read it without unwrapping anything.

## 3.57.0

### 2026-09-12 — an endpoint declares SOURCES, not a connection (P8, step 1)

A master device offers the same slave over several protocols. The Fronius
DataManager speaks Modbus TCP (complete, 1945-2376 ms a read) and a Solar API
over HTTP (partial, 54 ms, and it does not disturb the Modbus side). Declaring
that as two endpoints gave two device identities, two topic trees and two sets
of aggregates for ONE physical inverter. That was built and removed on the same
evening; this is the model that replaces it.

An endpoint is now an access point, N units, and **M ordered sources**:

```yaml
endpoints:
  - id: fronius
    units: [1, 2, 3, 4]
    sources:
      - id: sunspec
        protocol: tcp
        host: 192.168.1.50
        template: fronius_sunspec_inverter
        poll_groups: { normal: { interval: 20 }, slow: { interval: 120 } }
      - id: solar_api
        protocol: http
        url: "…?Scope=Device&DeviceId=${unit_id}&DataCollection=CommonInverterData"
        template: fronius_solar_api_inverter
        poll_groups: { realtime: { interval: 5 } }
        stale_after_s: 15
```

**Identity belongs to the unit.** `fronius-u1` keeps one topic prefix, one
bucket, one tag and one history, whichever source produced a value. A source is
only how it arrived, and nothing in the routing identity names one.

**Order is precedence.** The first source offering a field owns it; a later one
fills that field only once the earlier has gone stale past its `stale_after_s`.
That makes failover automatic when a cached HTTP view freezes, without the
flapping "freshest wins" would cause. `stale_after_s: 0` never yields, which is
the right setting for a counter — one that alternated between sources would go
non-monotonic.

Nothing existing changes: a plain `connection:` is the shorthand for exactly one
source named `default`, so every config written before today is a one-source
endpoint and `dev.connection` still means the first source's transport. The
ModbusConfig build is now shared between devices and sources rather than
duplicated, so a knob added once reaches both.

This step is the schema and the expansion only. The runtime still polls the
first source; several clients writing into one store, per-field provenance, and
the UI card follow.

## 3.56.0

### 2026-09-12 — an endpoint can front an HTTP master (Solar API, phase P7)

The Fronius DataManager answers its Solar API in a **54 ms median** and its
Modbus in 1945-2376 ms, for the same data off the same box. Twenty-five HTTP
calls over live Modbus polling caused zero overruns and left Modbus latency
untouched: the web server answers from the cache the DataManager fills off its
own RS-485 side, which refreshes about every 2.6 s.

- **`${unit_id}` / `${endpoint_id}` / `${device_id}` now substitute in an
  endpoint's CONNECTION**, not only in its routing identity. An HTTP master
  addresses its units by URL rather than by a unit id inside a frame, so without
  this an endpoint could not describe one at all and four inverters needed four
  hand-written devices. A connection with no placeholders is untouched, so the
  Modbus shape is unchanged.
- **`fronius_solar_api_inverter`**, a bundled template reading ONE request per
  inverter (`GetInverterRealtimeData.cgi?Scope=Device&DeviceId=${unit_id}
  &DataCollection=CommonInverterData`): active power, frequency, AC voltage and
  current, DC voltage and current at 5 s, the energy counter at 30 s. Every
  field lands on the canonical vocabulary the Modbus side already uses.

What the Solar API cannot give, and therefore stays on Modbus: power factor,
reactive and apparent power, the SunSpec event flags, and the per-string MPPT
block. Per-phase voltage and current live in a second collection
(`3PInverterData`) and would cost a second request, so they stay on Modbus too.

Verified end to end against the production DataManager: one endpoint definition,
four units, all four online, and the plant aggregate live — 1048 W total,
49.963 Hz and 234.175 V averaged, energy summed.


### 2026-09-12 — design note P7: Solar API alongside Modbus

The Fronius migration plan (operator notes) gains phase P7. No code yet; the note records
the measurements that make the case, so the decision is not re-litigated from
memory.

The Modbus path on this DataManager is saturated and not only by us. Measured
today on the same box in the same afternoon: four inverters at 20 s delivered
17.3 transactions a minute with zero errors and zero overruns, while 10 s
delivered 15.7-17.1 with 14-16 % errors. Past roughly 26 a minute it does not
slow down gracefully, it collapses — we asked for more and got less. The old
meter collector meanwhile holds three sockets open permanently and the old
inverter collector reopens its socket every five seconds, on a device that runs
out of client slots near ten.

The same DataManager's HTTP interface answers in a median of **54 ms** (25
consecutive calls, worst 197 ms) and returns **all four inverters in one
request**, against 1945-2376 ms for a single Modbus read at that moment. Those
25 calls ran on top of live Modbus polling with zero overruns, zero errors and
unchanged Modbus latency: the web server answers from the cache the DataManager
fills off its own RS-485 side. Sampling PAC once a second for 46 s caught 18
distinct values, so that cache refreshes about every 2.6 s.

So: HTTP every 5-10 s for power, energy, frequency, per-phase voltage and
current, and status; Modbus every 20-30 s for what only it has — power factor,
reactive and apparent power, the event flags, and the MPPT strings. Fresher data
than today on what matters, with less Modbus than the configuration that has
been collapsing.

The note also records what is already built (`fronius-solar` on `protocol: http`
with a JSON-path template), the one genuinely new piece (an endpoint-level HTTP
source that fans one response out to N units), and the risks — chiefly that a
frozen cache looks identical to a healthy one over HTTP, so a staleness detector
is required.

## 3.55.0

### 2026-09-12 — a master device needs room to breathe

Our own long-lived Fronius collector waits **1.0 s after every device** and
**200 ms between register blocks**, and forces a 300 ms pause before each
inverter's read. Nothing overrides those defaults in production, so its 20 s
cycle contains at least four seconds of deliberate idle. We wrote that, and we
presumably wrote it because we had to.

MBG had no such gap: the arbiter releases and the next unit takes its turn
instantly, so four inverters hit one DataManager back-to-back with no breathing
room. That is a plausible reason we collapse above ~26 transactions a minute
while the older, slower collector does not fall over at all.

`connection.endpoint_min_gap_s` (default `0.0`, so nothing changes for anyone)
makes the endpoint arbiter hold the next caller until the gap has elapsed since
the last release. It belongs to the ACCESS POINT, not the device: when two
endpoints share one datalogger the most cautious declaration wins, so a second
endpoint cannot quietly undo the first one's breathing room. `min_gap_s` and
the accumulated `gap_waited_s` are reported in the endpoint's bus telemetry, so
what it costs is visible rather than inferred.

**What the simulation does and does not show.** Against a master that owes its
RS-485 side 0.8 s after every reply (four units, 50 registers, 5 s interval):

| gap | reads/min | per-transaction p50 |
|---|---|---|
| 0.0 s | 48.0 | 1.149 s |
| 0.2 s | 48.0 | 0.950 s |
| 0.5 s | 48.0 | 0.650 s |
| 1.0 s | 45.0 | 0.351 s |

In a pure queueing model the gap moves the wait from inside the transaction to
outside it; throughput is unchanged until the gap is large enough to throttle.
So this proves the knob works and costs nothing up to about 0.5 s — it does NOT
prove it cures the real collapse, which would require starvation to compound
(stale buffers, dropped frames). Modelling that would have been assuming the
conclusion, so the model deliberately does not. The real test is the real
datalogger, on a morning when it is not already drifting.

## 3.54.2

### 2026-09-12 — the grid meter was being read twice

Unit 240 behind the Fronius datalogger measures the grid connection, and so
does the Janitza UMG512 the gateway already polls. Sampled in the same second
the two agree on sign and magnitude (-11766 W against -12636 W, the gap being
that the Fronius reading was up to 10 s stale while the grid swung 400 W in
two), and their energy accumulators agree to the decimal (181193.922 against
181194.0 Wh imported). The Janitza reports four times a second instead of once
per ten, with a fuller set: reactive inductive/capacitive, apparent energy, the
whole power-quality side.

Reading it twice bought nothing and cost 6 transactions a minute on an access
point that collapses above roughly 26. Measured today, three ways:

| inverters / meter | demanded | achieved | errors | device-down alerts |
|---|---|---|---|---|
| 20s / 20s | 17.1 | 17.3 | 0 % | 0 |
| 10s / 20s | 28.9 | 26.2 | 0.8 % | 0 |
| 10s / 10s | 32.0 | 21.6 | 6.5 % | 11 |

Asking for more returned less: past the knee this datalogger does not saturate,
it collapses. The `fronius-meter` endpoint is now `enabled: false` — configured
and functional, simply not consuming wire — and the four inverters run at 10 s.

What is genuinely given up is per-phase energy (import/export per line), which
the Janitza template publishes only as totals today. If a consumer needs it, the
UMG512 has the registers and the answer is to select them there, not to read a
second meter over a saturated wire.

`scripts/fronius_parity.py` no longer compares unit 240 (four pairs, not five),
with the reason written where the pair used to be. `PARITY_INCLUDE_METER=1`
restores it.

## 3.54.1

### 2026-09-12 — a healthy device has a story too

The new acquisition log recorded only TROUBLE, so opening a device that is
perfectly fine — the Janitza, 52189 reads and zero errors — showed an empty
page that reads as broken rather than as healthy. Starting a poll group now
records what the work looks like: which group, how many registers, how many
requests that costs on the wire, and how often. That line is also the baseline
an operator compares against when the device later misbehaves.

Fronius meter 240 returned to a 10 s `normal` interval, restoring decision D6
of the migration plan. It is a single transaction, and this afternoon's retune
had moved it to 20 s along with the inverters for no reason. It was also the
dominant source of parity mismatches, since the reference collector samples it
twice as often on a signal that swings hard.

## 3.54.0

### 2026-09-12 — a device's acquisition log, finally addressable

Every `ModbusConnection` has always kept a timestamped ring of its own
troubles — unreachable, recovered, forced reopen, bus busy. The only reader was
the alert harvester, which fired on them and dropped them. So diagnosing a
misbehaving endpoint meant grepping container logs by hand for facts the
process already had in memory and could not be asked for. That is exactly how
this afternoon's Fronius tuning was done, and it should not have been.

- **`GET /api/devices/{id}/events`** returns the ring newest-first, with
  `?limit=` and `?level=error,warn` to narrow it to the problems. Alongside it
  comes the live per-group state — interval, last sweep, reads per sweep,
  overruns, age — because "what happened" and "what it is doing now" are the
  same question when an endpoint is struggling.
- **A Logs tab on the device page**, matching the one virtual meters already
  had: live follow, a problems-only filter, error and warning rows tinted, and
  the per-group table above the log.
- **The ring is 50 entries no longer, but 500.** A datalogger that stalls in
  bursts every few minutes buried the old ring before anyone could open the
  page.
- **Two failures that only ever reached the logger now reach the ring**: a
  failed batch, recorded with the address and register count that were asked
  for rather than a bare "something failed", and a poll group's overrun
  episode, recorded with the group, the sweep time and the interval it broke.
  Both are edge-triggered exactly like their log lines, so a group that cannot
  keep up for an hour is one entry and does not evict the reason it started.

The poller reaches the device's ring through `RegisterPoller._record`, which
swallows everything: observability must never raise into the acquisition path,
and there is a test that says so.

## 3.53.0

### 2026-09-12 — how many sockets an access point is worth is measured, not assumed

A master device fronts its units on one `host:port`, and how it behaves when
several transactions arrive at once is a property of that master which nobody
can look up. Some serialize everything internally: a production Fronius
DataManager costs ~0.4 s per read when we are its only caller and ~3 s with
five racing, and starts refusing connections near ten — there a second socket
buys nothing and spends a client slot. Others run an engine per line, and two
or three sockets cut the sweep almost proportionally. Until now the gateway
assumed the first shape for everyone.

- **`connection.max_connections`** (default `1`, range 1..8) gives an access
  point K sockets. Units are distributed sticky by unit id, so a unit always
  rides the same one and its reconnects never disturb a sibling. Transactions
  overlap **across** connections and still queue **within** each, so the
  turnstile that keeps a serializing master orderly is not given up to get
  concurrency. The default changes nothing for anyone.
- **`scripts/calibrate_endpoint.py`** answers the question against the real
  device: it probes concurrency 1..N with the real register block, reports
  reads/s, transaction p50/p95 and sweep time per level, and recommends the
  FEWEST connections that get within 10 % of the best sweep — refusing to
  recommend a level that started erroring or being refused. Read-only, writes
  nothing to the device or the config.
- **The endpoint page now shows what its wire costs**: connections in use,
  seconds per transaction (p50 and p95), reads per sweep, and the resulting
  floor — the fastest honest cadence at that shape. An interval below the floor
  is a promise the wire cannot keep, and the page says so instead of leaving
  the operator to infer it from overruns. Missed turns appear next to it.
Measured against two simulated masters with identical wire protocols and
opposite concurrency behaviour (four units, 50 registers, 0.35 s interval,
12 s runs — `scripts/calibrate_endpoint.py` recommends 1 and 4 respectively):

| `max_connections` | serializing master | parallel master |
|---|---|---|
| 1 | 40 sweeps, cadence p50 1.21 s | 40 sweeps, cadence p50 1.21 s |
| 2 | 40 sweeps, cadence p50 1.20 s | 80 sweeps, cadence p50 0.60 s |
| 4 | 40 sweeps, cadence p50 1.20 s | 137 sweeps, cadence p50 0.35 s |

Where concurrency helps it is worth 3.4x the data and the cadence you actually
configured; where it does not, the knob is inert. That is the property worth
having: it cannot make a serializing datalogger worse.

Run against the production Fronius DataManager (four inverters, the 49-register
`normal` block, while both the gateway and the reference collector were
polling), the script declined to recommend anything at all: every level, one
connection included, produced timeouts and late answers. The access point has
no spare capacity even for a probe, which is the strongest form of the same
conclusion — it stays at one connection. A calibration that refuses to answer
is worth more than one that guesses.

- **The calibration counts what a naive probe would miss.** An overloaded
  master answers late: the read times out, the connection moves on, and the
  stale reply turns up against the next transaction's id, where pymodbus drops
  it and carries on. The next read then pays for the previous one's failure and
  both look healthy. Late answers and over-timeout reads are now counted and
  disqualify the level, and a short frame is no longer taken for a read. The
  first live run reported a clean 1.2x win for two connections; with the
  counters it reported the truth.
- **Fixed: editing an endpoint erased the rest of its connection block.** The
  modal rebuilt `connection` from the three fields it shows, so timeouts, retry
  budgets and illegal-register lists written in `config.yaml` disappeared on the
  first unrelated edit. The whole block is now carried through.

## 3.52.2

### 2026-09-12 — parity-harness fixes found by running it

- **Two independent clocks are not compared.** Both systems publish
  `runtime/last_seen` and each stamps its own poll: comparing them produced a
  mismatch on every publish — 44 of 48 in the first live run — drowning the
  real signal. Seeing the leaf on both sides is still recorded in the ledger.
- **The power-factor sign flips at unity.** The raw register alternates between
  +10000 and −10000 while the inverter sits at 1.00, and both systems were
  observed doing it independently. A sign flip at |PF| ≈ 1 is the device
  talking. The tolerance band is closed at BOTH ends, because `|x| >= 0.99`
  alone would have waved through 1.0 against 100.0 — the ±100 scaling fault
  this comparison exists to catch.
- `corruption_reason` joined the accounted-for list: it only reaches the wire
  when the collector's reconciliation fires, so it was missing from the
  captured inventory and surfaced as UNACCOUNTED on the first live run.

After the fixes, a 130-second live sample read 99.62 % agreement across 65
compared leaves, zero unaccounted, with the five remaining mismatches all being
the gateway's own restart transient.

## 3.52.1

### 2026-09-12 — migration fix: a template that curates nothing prunes nothing

`--prune-uncurated` read "this template has no curated subset" as "every
selected register is stale" and emptied the device. `device_seed` seeds such a
template WHOLE, so on it every register IS curated. Caught on the live config
during the cutover — it wiped a production meter's entire selection, which the
script's own backup put straight back, and the gateway had not been restarted
onto it.

## 3.52.0

### 2026-09-12 — the cutover parity harness proves nothing is lost

The shadow phase is over, so `scripts/fronius_shadow_parity.py` becomes
`scripts/fronius_parity.py` and compares the reference collector against the
gateway instead of the gateway against itself.

- **Pairs re-pointed** to `fronius/inverter/N/` ↔ `mbg/devices/fronius-uN/` and
  `fronius/meter/240/` ↔ `mbg/devices/fronius-meter-240/`, bridging the two
  vocabularies with the legacy leaf map, identity for leaves spelled the same
  on both sides, and the derived pairs P3 built for exactly this
  (`status/text` ↔ `status`, `status/alarm` ↔ `alarm`, `status/active` ↔
  `active`). Power factor is a normal comparison now that the templates
  normalize it, and a collector `True` and a gateway `1` are the same fact.
- **A coverage ledger**, because agreement on the leaves we compare says
  nothing about the leaves we forgot. Every leaf either side publishes is
  compared, deliberately-not-carried-over WITH THE REASON, or unaccounted —
  and the cutover gate now requires the unaccounted bucket to be empty
  alongside the agreement and coverage thresholds.
- Checked against the collector's real inventory, captured off the live broker:
  62 inverter leaves and 46 meter leaves, of which 44 and 44 are compared, 18
  and 2 are dropped with a written reason, and **none are unaccounted**. The
  four temperatures are in that list because the collector published a fake
  `0.0` for registers this hardware does not implement.
- `tests/test_fronius_parity.py` pins that inventory, so a template change that
  silently stops publishing something fails at build time rather than at
  cutover — and it rejects a reason too short to be one.

## 3.51.0

### 2026-09-12 — `plants` is now `endpoints`

The concept was never PV-specific. One template + one access point + N unit ids
describes a Fronius DataManager fronting inverters, a master fronting a bank of
meters, and a master fronting temperature/humidity/PWM slaves equally well —
and the name only fit the first. Renamed while nothing consumes it yet: after
the cutover, the MQTT namespace would have had readers.

- `plants:` → `endpoints:` in config.yaml, `/api/plants` → `/api/endpoints`,
  `mbg/plants/<id>/…` → `mbg/endpoints/<id>/…`, the InfluxDB tag value
  `aggregate=plant` → `aggregate=endpoint`, and the UI page and its strings.
- **A config written by an older version keeps loading**: `plants:` is read as
  `endpoints:` and the next save writes the new key.
- The name now lines up with the machinery underneath it: the arbiter that
  serializes a shared access point and the socket that serves it are keyed by
  exactly the thing an endpoint entry describes.

## 3.50.0

### 2026-09-12 — the Fronius map is what the hardware actually answers

Read straight off a production Symo Advanced 20.0-3-M. Eleven points answer the
SunSpec not-implemented sentinel, always: the model-103 DC current and voltage,
every temperature (cabinet, heatsink, transformer, other) and both MPPT probes,
with their three scale factors. The gateway polled them and published nothing —
which is exactly why the temperatures never showed up.

- **They leave the curated set, not the map.** A Primo or a GEN24 may well
  implement them, so they stay documented and selectable; they are simply not
  seeded, so nobody polls a hole by default. The AC read is unchanged at 50
  registers (the dead points sit *inside* a contiguous block, and registers are
  free — transactions are not).
- **The MPPT block polls on its own cadence.** It sits 135 registers past the
  AC block, so it can never share a read with it (Modbus caps one read at 125):
  leaving it in the fast group made every AC sweep cost two transactions. The
  template now declares `normal` 5 s (AC: currents, voltages, power, energy,
  status), `slow` 30 s (MPPT strings) and `static` 3600 s — one fast read and
  one slow read per inverter, never more.
- Measured on the datalogger, this is what the tiering is worth: a read costs
  the same whether it asks for 2 registers or 95 (p50 2.5 s vs 1.2 s — the
  small one was, if anything, slower). Splitting the AC block into tiers would
  have cost a transaction and bought nothing; moving the block that could never
  merge anyway costs nothing and buys the cadence.
- `scripts/migrate_p3_fronius_pf_status.py` carries it into devices seeded
  earlier: it reports the uncurated registers by default and removes them with
  `--prune-uncurated` (61 registers to 50 on the live units). Removing a
  register is the operator's call; a migration must not quietly shrink what a
  device measures.

## 3.49.0

### 2026-09-12 — one master, one socket

A master device — a Fronius DataManager, a Modbus TCP/RTU gateway, an RS-485
bridge — fronts its units on ONE access point. Giving each unit its own socket
never made them independent (they still queue inside the master), and a master
that serves a handful of clients runs out of them: measured on the production
DataManager, five of the gateway's own sockets plus five probes and fresh
connections started timing out.

- **The socket belongs to the access point**, not to the unit. Devices sharing
  a `host:port` share one connection and the lock that serializes it; the last
  one to let go closes it, so stopping one inverter cannot take its three
  siblings off the master. A directly-attached serial line never shares (one
  master per line is a different rule), and `share_transport: false` restores a
  socket per unit.
- **What describes a unit stays with the unit**: its counters, latency, error
  taxonomy, reachability verdict and health are untouched. Units share a wire,
  never an identity — `tests/test_shared_transport.py` exists to keep that line
  from blurring.
- Tests gained a fixture that clears the pooled access points between cases: a
  socket pooled per `host:port` outlives the connection objects that borrow it,
  which is the point in production and cross-contamination in a test file.

## 3.48.0

### 2026-09-12 — fresher data: the gateway queues instead of racing

Measured on the production Fronius DataManager serving five units. A
50-register read costs **~0.4 s** when the gateway is its only caller. With
five of the gateway's own pollers racing each other it costs **~3 s**, the
aggregate rate falls **below one read per second**, and fresh connections
start timing out — the device serializes internally and serves a handful of
clients, so concurrency there buys nothing and costs everything.

- **Endpoint arbiter**: a FIFO turnstile per `host:port`. Every device that
  shares a gateway — a plant's units, two devices behind one bridge — takes its
  turn instead of elbowing in. FIFO on purpose: a plain lock hands the turn to
  whoever the OS wakes, and under steady contention one poller can wait a very
  long time. On by default (`serialize_endpoint`); it costs nothing when a
  device has the endpoint to itself. A missed turn skips the cycle and is
  **never** counted against the link — a busy gateway must not masquerade as a
  dead one — with one `bus_busy` event per episode.
- **Polling is fixed-rate, not fixed-delay.** The wait is `interval − sweep`,
  so a 5 s interval means a reading every 5 s rather than every 5 s plus
  however long the bus took. The slower the endpoint, the further the old
  behaviour drifted from the freshness the config promised. A group that cannot
  keep up still gets a breather (10 % of its interval), counts `overruns`, and
  says so once per episode.
- **`/api/status` reports what a sweep costs**: per poll group, `cycle_s` and
  the number of batch reads next to the interval — the two numbers an interval
  is actually chosen from.
- `scripts/bench_endpoint_arbiter.py` reproduces the whole thing against a
  simulated slow gateway, no hardware needed. Five units, 3 s interval, 30 s:

  | | reads/s | cadence | sweep |
  |---|---|---|---|
  | arbiter off | 0.83 | 8.0 s | 7.7 s |
  | arbiter on | 1.67 | 3.0 s (as configured) | 1.2 s |

## 3.47.0

### 2026-09-12 — a plant is an entity, not a grouping

Migration phase P4 of
the Fronius migration plan (operator notes). Code only —
no live config, broker or container was touched.

- **The plant has its own page.** Opened from its row in the devices list:
  status (`online` / `partial` / `offline`) and unit census, the grid of totals
  it publishes on `mbg/plants/<id>/…` with canonical labels and units, a unit
  table (health, last read, poll rate, errors — with rename and a way into each
  unit), the plant-totals toggle, the per-unit probe, and an Outputs card. Until
  now a plant was a row and a modal: four healthy unit rows said nothing about a
  plant sitting at 2/4.
- **An edit stops dropping what the form does not send.** A plant edit REPLACES
  the stored entry, so `write_locked`, `aggregates`, `http_output`, `rest_push`
  and hand-written per-unit ids/names used to vanish — a locked plant unlocked
  itself one save after the operator locked it. Routing identity (topic prefix /
  bucket / device tag) is now pinned on update, exactly as for a device.
- **A settings-only edit keeps the pollers running.** Re-materializing every
  unit on every save punched a hole in acquisition for a rename or a toggle. The
  units are rebuilt only when something they are BUILT from changed.
- **A plant is one endpoint, so its sinks are declared once**: `http_output` and
  `rest_push` join the write lock at plant level and propagate to every unit.
  Their cards now render on a plant unit's Outputs tab (they used to be hidden,
  and the backend refused them with "not a configurable device"), each saying
  the switch applies plant-wide. The orphan Power Quality tab — which rendered
  on a plant unit with no way to enable the recorder — is gone.
- **`POST /api/plants/{id}/test`** probes every unit on the shared endpoint, one
  answer each: the only way to tell "the datalogger is deaf" from "unit 3 is not
  configured on it".
- **`GET /api/status` carries `plants`**, and the InfluxDB read path resolves a
  plant id like a device id (its own aggregate series). An id that is neither a
  device nor a plant now answers 404 instead of silently reading the primary's
  bucket — a typo used to come back with somebody else's data.
- **Liveness, again**: `data_health()` answers `ok` when nothing has been polled
  YET, which is not the same as healthy. A client that has never produced a
  reading now defers to the transport flag, so a plant of unreachable units no
  longer shows three green units underneath an `offline` plant.
- `tools/e2e/plant_page_e2e.mjs` drives the whole page in a real browser
  (19 checks, green) against a throwaway instance.

## 3.46.0

### 2026-09-12 — power factor is a fraction; templates ship derived measurements

Migration phase P3 of
the Fronius migration plan (operator notes). Code only —
no live config, broker or container was touched.

- **A fixed `scale` may now accompany `scale_from`.** It divides AFTER the
  dynamic exponent, as the unit conversion the exponent cannot express. SunSpec
  reports power factor as a PERCENTAGE, so a Fronius unit published ±100 on
  `power_factor/total` while the Janitza beside it published ±1 — one canonical
  topic meaning two different things. The Fronius SunSpec maps (v2.1.0) carry
  `scale: 100` and now publish the same fraction as every other device. The
  scaling chain also rounds ONCE, at the end, with the combined decimal
  exponent: dividing after the SF rounding re-introduced exactly the float
  noise that rounding exists to kill (99.99 / 100 = 0.9998999999999999).
- **Templates can ship derived measurements.** A top-level `calculated:` block
  is seeded into every device made from the template, so a vendor's decoded
  status or an alarm flag travels WITH the device map instead of being retyped
  per unit — the fifth inverter added to a plant would otherwise speak
  differently from the first four. An entry may pin an explicit MQTT `topic`
  (a derived value usually belongs under an existing branch) and an `enum`,
  which turns the computed code into text through the same decoder real status
  registers use. No `topic` means the flat name, exactly as before: routing
  identity never shifts under an upgrade.
- **The Fronius inverter template decodes its own status**: `status/text`
  (the reference collector's wording, verbatim, so a consumer moving over sees
  the same words), `status/alarm` and `status/active` — the vendor's alarm and
  producing code sets, pinned by test.
- Saving calculated registers from the UI **preserves** `topic`, `measurement`,
  `enum` and the per-sink flags. Rebuilding each entry from a fixed field list
  silently re-routed a template-shipped measurement and turned its text back
  into a bare code.
- `scripts/migrate_p3_fronius_pf_status.py` carries both changes into devices
  already seeded (a selection is a copy taken once). Dry run by default, backs
  up before writing, idempotent, never overwrites a hand-edited scale, and
  reports — rather than silently retires — a hand-made measurement the template
  now supersedes.

## 3.45.0

### 2026-09-12 — one liveness verdict; plant counters that never walk backwards

Migration phases P1 and P2 of
the Fronius migration plan (operator notes). Code only —
no live config, broker or container was touched.

- **ONE liveness verdict** (`device_registry.client_is_live`): a device is alive
  when its acquisition pipeline is PRODUCING, not when a socket happens to be
  open. `client.connected` clears only on an explicit disconnect or the wedge
  backstop, so a datalogger that went dark overnight kept reporting "online" to
  Home Assistant, to MQTT and to the alert log while its data froze.
  `data_health()` already owned the freshness verdict; it now drives
  `availability`, `runtime/status`, the `dev:<id>` alert transition and the
  plant's unit census alike (`degraded` counts as alive — slow, not gone).
  `/api/plants` units gain `health` next to `connected`.
- **`runtime/read_errors`** joins `runtime/status` and `runtime/last_seen`;
  a `None` component leaves its topic untouched instead of publishing empty.
- **`forced_reopen` is edge-triggered**: the backstop still reopens a wedged
  link on every run of failures, but the EVENT is one per outage, re-armed by
  the first successful read. A sleeping endpoint no longer rotates the 50-entry
  event ring in minutes and buries what matters.
- **Plant counters use last-known values** and publish only when EVERY expected
  unit has one. Freshness-gating a lifetime counter made the plant total jump
  BACKWARDS at dusk (~178 MWh → 113 MWh as three of four inverters went dark),
  poisoning every `increase()` downstream. An incomplete sum is a lie, so the
  leaf is withheld and the retained topic keeps the last COMPLETE total.
- **Plant power factor is derived**, `Σ active / Σ apparent`, bounded to ±1 —
  averaging ratios weighted a 1 kW inverter like a 20 kW one. `power_factor_l*`
  is no longer aggregated. (Until phase P3 normalizes the Fronius templates, a
  UNIT's power factor is still the raw SunSpec ±100 while the plant's is a true
  fraction — do not compare the two topics.)
- **The census always publishes**: `status` / `units_online` / `units_total` go
  out on every cycle, including the one where nothing is fresh — precisely what
  a consumer needs at nightfall. `units_total` now counts the units EXPECTED to
  contribute (the enabled ones), so a disabled unit can neither hold the plant's
  counters hostage nor make `online` unreachable.
- **Plant InfluxDB**: the aggregate's bucket resolves its placeholders to the
  plant (no more literal `fronius_${unit_id}` on the wire), and writes honour
  the same change-detection contract as every other sink — a plant standing
  still overnight no longer writes 8 640 identical points per field.
- Fixed a test stub whose `get_stats()` answered `False`, blowing up whichever
  5-second alert-harvester tick happened to land inside that test (an
  intermittent "event harvest error" with no bug behind it).

## 3.44.0

### 2026-09-11 — plants publish their own output; unit workspace unified

- **Plant aggregates**: a plant is a real entity, so it publishes its own
  data — sums (powers, currents, energies) and averages (voltages,
  frequency, power factor, temperatures) of its units' fresh values, on
  `mbg/plants/<id>/<canonical topic>` plus `units_online`/`units_total`,
  and into InfluxDB under the SAME canonical measurements tagged
  `device=<plant_id>, aggregate=plant`. Freshness-aware: a stalled unit
  drops out instead of freezing the total. Opt out with
  `aggregates: false` on the plants: entry. The devices list shows the
  live plant total (Σ kW) on the group row.
- **Unit workspace unified**: a plant unit's Edit/Outputs tabs are now
  the SAME layout as every other device — plant-owned fields (connection,
  template, routing, sink toggles) render locked with an "Edit plant"
  banner instead of a special panel; per-unit things (measurement
  selection, poll intervals, write-protection at plant level) stay
  editable in place. Plant dialog gains MQTT/InfluxDB/HA-discovery
  toggles.

### 2026-09-11 (evening) — device runtime heartbeat, plant status, migration plan

- **Device runtime heartbeat**: every device publishes retained
  `runtime/status` (`online`/`offline`) and `runtime/last_seen` (ISO
  timestamp of the last successful read) next to `availability`, change-only
  — the long-lived collector leaf names, so liveness watchers keep working
  on the new namespace.
- **Plant status**: `mbg/plants/<id>/status` = `online` (all units fresh) /
  `partial` / `offline`, MQTT-only (text never reaches InfluxDB).
- **Migration plan recorded**: the Fronius migration plan (operator notes)
  — verified state, findings (liveness verdict, non-monotonic plant energy,
  PF units, missing collector leaves, UI parity gaps) and the phased plan
  for running MBG in parallel with the legacy Fronius collector on its own
  `mbg/devices/*` + `mbg/plants/*` namespace (no compat aliases).

## 3.43.0

### 2026-09-11 — MQTT namespace + plant-grouped devices page

- **Namespace convention**: device values live under `mbg/devices/<id>/…`
  (`mbg/vmeter/<id>/…` reserved for virtual-meter MQTT publishing).
  `default_topic_pattern` seeds new devices accordingly; pre-existing
  devices keep their persisted prefixes (routing identity is fixed).
- **Devices page IA**: a plant's units now nest UNDER their plant as an
  expandable group (chevron, unit census `N units`, `X/N online`,
  plant edit/delete on the group row; expansion state persists). The
  separate Plants card is gone — one list, reality-shaped.
- Migration decision recorded: at the read cutover, consumers move to
  the canonical topics (no long-lived legacy alias layer); the
  compat_aliases stay a migration bridge only.

## 3.42.0

### 2026-09-11 — Fronius plants speak canonical (same output as every meter)

Operator call: no reinvented wheels — a plant's output must look exactly
like the Janitza reference. The canonical dictionary is extended with the
PV/inverter domain (dc/*, mppt/N/*, temperature/*, status/*,
energy/active/generated, diagnostic/manufacturer+model) and the Fronius
SunSpec templates (v2.0.0) now use canonical register names throughout:
topics, InfluxDB measurements and field names derive from the dictionary,
so meters/<unit>/power/active/total, measurement `power_active`, field
`power_active_total` — identical shape to every other MBG device.

The legacy SunSpec-collector tree (fronius/inverter/N/W …) is now what it
always should have been: a COMPATIBILITY LAYER, expressed as
`mqtt.compat_aliases` built from the emitted
scripts/fronius_legacy_leaves.json (canonical topic → collector leaf,
verified register-for-register in tests). During the shadow phase the
aliases feed mbg/fronius/…; at cutover the same map re-targets
fronius/… and no consumer changes.

The canonical-naming lint now skips unrouted plumbing registers (SunSpec
scale factors publish nowhere, so naming them canonically buys nothing).

## 3.41.0

### 2026-09-11 — writes foundation: capability first, guards opt-in (F3a)

The write trust model is inverted: the product no longer decides what a
user may write — the USER does, and every restriction they declare buys
them something back.

- **Raw writes (L0)**: any register on any Modbus device can be written,
  even undeclared in the template, by passing `unguarded: true` — the
  explicit statement that no envelope exists. The master arming switch,
  per-device lock, authentication, rate limit and audit still apply:
  safety by configuration, not prohibition.
- **Guards are opt-in (L1)**: `write_min`/`write_max` are no longer
  required on writable registers; new `write_allowed` (exact-value set)
  guard. Declared guards enforce on every write and drive the write
  dialog (bounds shown and pre-checked, allowed sets, unguarded
  acknowledgement checkbox).
- **Per-device write lock**: the primary's hardcoded read-only became
  `security.primary_write_locked` (default true — behavior preserved);
  every other device gets `write_locked` (devices[]/plants entry,
  default unlocked) with a Write-protection card in the Outputs tab
  and an audited toggle endpoint. Plant units lock at plant level.
- New `GET /api/devices/{id}/write-info/{rtype}/{address}` feeds the
  dialog: declared? guards? lock? — before anything touches the bus.

## 3.40.0

### 2026-09-11 — SunSpec dynamic scale factors + plants

Two foundation features for SunSpec fleets (inverters behind a
datalogger — Fronius today, Deye/Huawei tomorrow):

**`scale_from` (dynamic scale factors).** A register can reference a
sibling `*_SF` register whose raw value is a signed base-10 exponent:
engineering = raw × 10^SF. The exponent is read live per batch (the SF
conventionally sits at a higher address than its dependents, so the
poller prescans it before decoding) and bridged across batches/cycles
with the last good value. No valid SF → the value reads as MISSING —
a wrongly-scaled reading is worse than no reading. `scale` and
`scale_from` are mutually exclusive (template validation enforces it,
including dangling referents). Verified against live hardware: a raw
model-103 frame from each of 4 Fronius Symo units decodes identically
to the dedicated SunSpec collector on every field (known delta:
Fronius' out-of-spec power-factor scaling, handled at template level).

**`plants:` (one template × N unit ids behind one endpoint).** A plant
materializes N ordinary devices from one template + one connection +
a list of unit ids — each with its OWN socket (unit-switching on a
shared socket corrupts some gateway buffers, and units must fail
independently). `${unit_id}`/`${plant_id}`/`${device_id}` substitute
per unit in topic prefix, bucket, device tag and name. Units seed
their register selection from the template at boot and are managed
THROUGH the plant (device CRUD refuses them). New API:
`GET/POST /api/plants`, `GET/PUT/DELETE /api/plants/{id}` with
aggregated unit availability; new Plants card + Add Plant dialog in
the Devices tab. Config reference documents both features.

## 3.39.1

### 2026-09-11 — virtual meters: idle-connection reaping (live-peer leak)

Production finding after 15 days of uptime: the fronius_ts vmeter had
accumulated 4 connections from the SAME live host (the Venus/Ekrano
network scan opens sockets to :502 and never closes them — one more after
every host-side incident, ages up to 212 h). TCP keepalive can't help: it
only catches DEAD peers, and a live kernel answers probes forever for a
socket its application abandoned.

New application-level reaper on the existing 10 s supervisor sweep: reads
`TCP_INFO.tcpi_last_data_recv` per client socket (kernel-side idle time —
no request attribution needed) and closes connections silent for
`transport.idle_timeout_s` (default 300; 0 disables; Linux-only, other
platforms keep keepalive alone). Active consumers poll every 1-2 s and
never approach the threshold. Close is marshalled onto the server's own
event loop; every reap is a stats event (`idle_reap`).

## 3.39.0

### 2026-08-28 — dedicated PQ history bucket (long retention)

PQ history is small but forensically precious — grid disputes run on
months, while a device's telemetry bucket typically expires in weeks.
New optional `pq_recorder.bucket`: routes `pq_events`/`pq_counters`/
`pq_waveforms` (and the API reads + the read-through archive) into a
dedicated bucket, auto-created with infinite retention if missing.
Default unchanged (the device's normal bucket).

## 3.38.2

### 2026-08-28 — CRITICAL: reject foreign-window waveform data

Live finding on the UMG512: the meter's `mk_hww.html` serves **only its
newest capture window** — a request for any older `_hww_nr` answers with
the newest window's samples in the same JSON shape (wrong timestamps, no
error). The read-through fallback and the recorder's archive path
happily stored those as the requested event's waveform (all 31 backfilled
traces from the 2026-08-27 forensics were the same newest window,
mislabeled — since purged).

- `fetch_waveform_live` and `_archive_waveforms` now validate every
  sample against the requested window (`window-1 .. window+120 s`) and
  drop foreign data — an unfetchable old window yields an honest empty
  result instead of a wrong chart.
- Docs: operational note on the newest-window-only limitation + the
  recommendation to keep `poll_s` short (60 s) so a new event's trace is
  archived before the next event displaces it.

## 3.38.1

### 2026-08-27 — waveform fetch: 60 s timeout + one retry

The meter reconstructs a trace from its recording memory on demand —
older windows can take tens of seconds and a busy meter occasionally
times out once (observed live: same window, first call 3 s, second call
>30 s). `fetch_waveform_live` and the recorder's archive path now use a
60 s timeout with a single retry, so clicking an older event reliably
completes instead of surfacing a load error.

## 3.38.0

### 2026-08-27 — PQ waveforms: read-through to the meter + page polish

- **Waveform read-through fallback**: `GET /api/pq/waveform` now falls
  back to fetching the trace live from the meter when nothing is archived
  (ring events that predate the recorder being enabled — the exact state
  every fresh install starts in), and archives it in the same pass so the
  next view is served from InfluxDB. Response carries `source: "device"`
  when the fallback ran. Module helpers `fetch_json` /
  `fetch_waveform_live` / `device_base_url` shared by recorder and route.
- **Power Quality page redesign**: events grouped by day with a severity
  dot (outage red / excursion amber / RVC teal); event detail header with
  cause badges + duration/min/max/avg/trigger-bound; explicit
  loading/empty/error overlays on the chart; newest event auto-selected on
  open so the pane is never blank; tabular-numeric timestamps; the
  "fetched live from the meter" source note.
- **Fix**: after `POST /api/pq/config`, `/api/devices` kept serving the
  stale `pq_recorder` block — `set_pq_recorder` rebuilds `config.devices`
  with new objects while the registry kept the old instance. The route now
  swaps it via `registry.replace()` (same idiom as the rest-push handler).
- **E2E**: `tools/e2e/pq_e2e.mjs` — Playwright suite covering the whole
  feature in a real browser (19 checks: login, template gating, event
  list, waveform incl. live read-through, Outputs card, config
  round-trip, tab hidden on non-Jasic devices, zero console errors).

## 3.37.1

### 2026-08-27 — PQ tab: capability declared in the template + gating fix

- **Fix**: the Power Quality tab appeared (disabled) on EVERY device and
  stayed dead on the Janitza too — `_deviceDetailHtml` gates on the
  workspace's trimmed `data` object, which never carried `pq_supported`.
  The flag is now copied into `data`, and the tab is **hidden entirely**
  on devices without the capability instead of rendered disabled.
- **The capability now lives in the device template** (the proper source
  of truth): top-level `pq_recorder: "jasic"` on `device_template` —
  declared in the bundled `janitza_umg512_pro`, round-trips through
  template export, and lets community templates for other Jasic-family
  meters enable the feature without code changes. Resolution:
  `template_supports_pq(template, template_id)` (template field first,
  legacy id-set fallback for templates that predate the field) — used by
  `/api/devices`, `/api/pq/*` and the recorder manager.

## 3.37.0

### 2026-08-27 — PQ recorder: UI config + alert integration

Phase 2 follow-up of the PQ recorder (3.36.0): the feature is now fully
operable from the UI and wired into the alert channels.

- **Outputs tab → "PQ event recorder" card** (Jasic-family devices only):
  enable toggle, poll interval, waveform archiving, base-URL override —
  saves via `POST /api/pq/config` and live-restarts the poller. Live
  status line (running / last poll / lifetime counters / last error) from
  `/api/pq/status`.
- **Alerts**: every NEW PQ event also fires the AlertManager
  (`alert_severity_for`: outage → critical, voltage/current/frequency
  excursion → warning, bare rapid-voltage-change → info; per-device key
  `pq_<id>` so the manager's min-interval throttles event bursts into one
  notification on the MQTT `<prefix>/alert` topic + webhook).
- `/api/devices` entries now carry the full `pq_recorder` config block
  (for the card) alongside `pq_supported`.
- EN 50160 weekly-verdict scraping was considered and **descoped**: the
  meter exposes no computed verdict registers — its own EN50160 page
  derives the indices client-side from FFT registers. A compliance
  verdict belongs in downstream analytics over the now-archived data.

## 3.36.0

### 2026-08-27 — PQ event recorder (Janitza/Jasic)

New opt-in feature: per-device acquisition of the on-device power-quality
event recorder on Janitza UMG-series meters (Jasic web firmware — UMG
604/605/508/511/512). Born from a real forensics need: after a morning of
grid outages the UMG512's 32-entry event ring had already wrapped, and the
Modbus map only exposes lifetime counters — the records themselves are only
served by the meter's unauthenticated web endpoints.

- **`multibus/pq_recorder.py`** — per-device poller (`pq_recorder:` config
  block, primary flat section or `devices[]` entry): reads
  `/lib/events/getevt.html` + `json.do` counters, decodes the 64-bit reason
  bitmask (one nibble per cause, one bit per channel — validated against the
  firmware's own `events.js`), and archives idempotently to InfluxDB:
  `pq_events` (per cause+channel, timestamped at event start),
  `pq_counters`, and `pq_waveforms` — the ~50 s half-wave-RMS traces
  (10 ms steps) of the channels implicated in each NEW event, fetched via
  `hww.html`/`mk_hww.html` before the device's own few-day retention drops
  them. New events also go to MQTT (`<prefix>/pq/event`, retained) and the
  gateway event log. New-event detection via a persisted high-water mark;
  the first sync archives the ring without announcing it.
- **API** (`multibus/routes/pq.py`): `GET /api/pq/status`,
  `GET /api/pq/events`, `GET /api/pq/waveform`, `POST /api/pq/config`
  (persists via the new `Config.set_pq_recorder` and live-restarts the
  poller).
- **UI**: device workspace → **Power Quality** tab (gated on a Jasic-family
  template + InfluxDB output): archived event list with decoded causes,
  click-through waveform chart (reuses the shared canvas renderer),
  lifetime counters.
- **Publisher plumbing**: public `InfluxDBPublisher.write_point()` (custom
  points ride the same connected-or-buffered delivery as register data) +
  `query_pq_events`/`query_pq_waveform`; public
  `MQTTPublisher.publish_topic()` for full-topic feature publishes.
- Docs: `docs/pq-recorder.md`, config-reference + API tables. Tests:
  `tests/test_pq_recorder.py` (decode table, waveform channel selection,
  first-sync/new-event/high-water semantics).

## 3.35.10

### 2026-08-18 — end-to-end review pass (pre-ESPHome-test, pre-public)

Full-system sweep: live health (30 h of logs, sinks, real consumers),
complete reconciliation of every audit backlog, code sweep (one TODO in
the whole tree — the intentional generated-YAML hint), i18n parity
(1062 = 1062 keys), and a live rehearsal of the ESPHome Builder pipeline
(template → generate → secrets → dashboard save → validate over the WS
stream on a throwaway ESPHome 2026.5.3: "Configuration is valid!", exit 0).

- **WS ping-after-close no longer logs ERROR** — the keepalive ping raced
  client disconnects (~3/h of noise, the only error class in 30 h of
  production logs). The handler now checks the socket state before the
  ping and treats the ASGI close race as a debug-level disconnect.
- **Bounded container logs in the bundled compose** (perf audit C5): a
  shared `x-logging` anchor (json-file, 10 MB × 3) on all 7 services — an
  appliance host can no longer fill its disk with container logs.
- **Disaster-recovery runbook** added to MANUAL §15 (EN+RO): clone →
  compose up → import ZIP → `restart_required` nuance with >1 device →
  re-check, plus the `esphome-config` volume note.
- Audit bookkeeping: two stale "remaining" lists corrected (.audit) — the
  datapath audit line still advertised 15 open findings against a file
  where all 36 are closed.

## 3.35.9

### 2026-08-17 — clean-clone walkthrough (go-public audit item) + fixes

Followed the README literally on a fresh clone, as a stranger would:
`git clone → cp .env.example .env → docker compose up -d` (full bundled
stack), first-run credentials from the log, login, wizard test-connection,
first device (EM24-shaped sim), first virtual meter, MQTT/HA discovery
flowing, vmeter serving over Modbus TCP, and the documented Docker test
path (`Dockerfile.test` → 907 passed). Three real gaps found and fixed:

- **Bundled InfluxDB/Grafana host ports were hardcoded** (8086/3000) — the
  exact audience of the bundled stack (people already running one) hit a
  bind failure with no knob. New `INFLUXDB_HOST_PORT` / `GRAFANA_HOST_PORT`
  in compose + `.env.example`.
- **The `:502` mapping had no knob either** — now
  `MODBUS_502_HOST_PORT` remaps the host side instead of editing the file.
  (Docker note from the walkthrough: one port collision in an `up -d`
  batch can leave OTHER containers half-created — running but with no
  networks. `docker compose down && up -d` after fixing the port is the
  clean recovery.)
- **Loadtest sim served int32 word-swapped** (high-word-first) relative to
  the EM24 wire convention (`Reg_s32l`, low-word-first) that the
  `carlo_gavazzi_em24` template decodes. The decoded chain
  (store → MQTT → vmeter) carried garbage that LOOKED consistent to an
  equally-swapped test reader — caught because MQTT showed 15 MV. Sim now
  writes low-word-first; capacity §R metrics (rates/latencies/footprints)
  are unaffected, values were never asserted on.

## 3.35.8

### 2026-08-17 — str(e) review (go-public audit item)

Systematic sweep of every place raw exception text reaches an API client
(~40 sites classified). The contract now enforced:

- **Broad `except Exception` → 500 handlers return a generic detail**
  (`internal error (<ClassName>) — see the server log`) and log the full
  traceback server-side. Fixed: `/api/config/apply`, reload-registers,
  the modbus/mqtt/influx config-update routes, the language-pack loader
  and the selected-registers save path — an OSError there used to echo
  container filesystem paths to the client.
- **Deliberate validation errors keep passing through verbatim** — our own
  `ValueError` messages (422/400) are the operator's fix-it text; the
  probe/test endpoints (`/api/devices/test`, MQTT/TLS probes, register
  probe) stay fully detailed by design. Regression-tested both directions
  (`tests/test_error_disclosure.py`).
- **Internal service URLs are redacted in error strings**: the serial
  bridge "unreachable" diagnostic and all three ESPHome dashboard error
  paths now run their URL through `redact_url` (userinfo/secret query
  stripped, host kept for diagnosis) — a credentialed
  `SERIAL_BRIDGE_URL`/ESPHome URL can no longer echo its password.
- Reviewed the `except: pass` inventory: the remaining silent catches are
  typed, best-effort persistence paths (audit/event-log writes) —
  deliberate, left as-is.

## 3.35.7

### 2026-08-16 — runtime paths anchored to the config directory

Closes the dev-mode wart noted in 3.35.6: every runtime file now derives
from `config_path.parent`, so `python main.py -c /elsewhere/config.yaml`
is fully self-contained instead of spilling into `./config` of whatever
CWD it was launched from. In the container nothing changes (CWD=/app,
config dir `/app/config` — resulting paths are byte-identical).

- `create_api` passes the config dir to **EventLog** (`events.jsonl`) and
  **TemplateRegistry** (`device_templates/`); main.py does the same for
  its boot-path TemplateRegistry.
- **InfluxDBPublisher** gains `buffer_dir` — the replay-buffer snapshot
  (`influx_buffer.jsonl`) now really lives "next to config.yaml" as its
  comment always claimed; `INFLUX_BUFFER_PATH` still overrides.
- **VirtualMeterManager** gets `virtual_meters.yaml` + `templates/` from
  the config dir (a `-c` instance no longer reads/persists vmeters in the
  repo checkout).
- **TLS fallback paths** (`certs/ui.crt|key`) follow the config dir.
- Signature defaults keep the old CWD-relative values for back-compat
  (standalone tools, tests that construct pieces directly).
- Tests: the two suites that built a bare `Config()` through `create_api`
  (write-guard, corrected-field) now anchor it in a temp dir — the test
  suite itself no longer writes `config/audit.jsonl` into the checkout.
  Two full randomized runs leave `config/` untouched.

Verified live-style: an instance launched from the repo CWD with `-c` to a
scratch dir wrote `events/audit/influx_buffer/virtual_meters` beside that
config.yaml and nothing into the repo.

## 3.35.6

### 2026-08-16 — Monitor empty-state placeholder, layout-aware and wrapped

The last mobile quirk from the 3.35.5 sweep:

- **The empty-chart hint no longer clips on phones** — the canvas text now
  word-wraps to the chart width instead of drawing one long line.
- **Direction follows the layout**: below 1024 px the value list sits
  *under* the chart (the CSS flips the order), so both the canvas hint and
  the "Getting started" banner now say "the list below" there; wide
  touch/mouse layouts keep the "on the left" copy. New i18n keys
  `monitor.hintNarrow` / `monitor.emptyHintNarrow` (EN+RO); the old touch
  banner copy said "the list above", which was wrong everywhere.

Known dev-mode wart (noted, not fixed here): several runtime paths
(`config/influx_buffer.jsonl`, `config/audit.jsonl`, `config/events.jsonl`,
snapshots) are CWD-relative, so an instance launched with `-c` pointing at
another directory spills them into `./config` — harmless in the container
(CWD=/app) but it polluted the repo checkout and briefly flaked the test
suite during a local demo run.

## 3.35.5

### 2026-08-16 — mobile polish (post-deploy verification pass)

Full mobile sweep (390×844, Playwright) across all 9 pages on a demo
instance: no page-level horizontal scroll anywhere; wide tables scroll in
their own containers. Two real quirks found and fixed:

- **Login overlay said "Modbus Gateway"** — the overlay renders before the
  language pack loads, so the pre-rebrand hardcoded fallback showed on
  every cold login. Fallback updated to "Multi-Bus Gateway".
- **History toolbar crushed its selects on phones** (device select showed
  ~2 characters). The chart toolbar now wraps below 1024 px and each
  select keeps a readable minimum width.

## 3.35.4

### 2026-08-16 — capacity campaign re-run on the current version

The 2026-08-01 load campaign (measured on v3.4.1) was re-validated on
v3.35.3 — the staleness notice on the capacity report is gone.

- **Harness modernized** for the current codebase: `seed_via_api.py` sends
  the explicit device `id` the API now requires and clones the vmeter
  template per instance (one-instance-per-template rule), binding each
  vmeter to a distinct seeded device; the loadtest compose seeds an
  auth-off config on first boot (first-run provisioning would 401 the
  seeder), builds the sim from `loadtest/` (root `.dockerignore` excludes
  it from the main context), and the sim pins pymodbus 3.15.
- **Results (§R in `loadtest/RESULTS.md`): no regression.** Composed ramp
  to 51 devices + 12 vmeters (MQTT ON) at 9.5% CPU / 111 MiB; 200-client
  swarm 790 reads/s, 0 errors, p99 15.8 ms; 30-min soak with 50,827 flap
  reconnects, RAM flat, 12/12 vmeters fresh in all 88 samples; fail-safe
  verified end-to-end with socket-refused proof and **stale-recovery
  improved to 2–3 s** (was ~6 s on 3.4.1).
- `loadtest/CAPACITY-REPORT.md` refreshed (v3.35.3 header, re-validation
  summary, template-shaped cost coefficients, MQTT gap closed);
  `docs/reliability.md` now cites the measured fail-safe proof.

## 3.35.3

### 2026-08-16 — two quirks caught by the screenshot session, fixed

Both surfaced while building the demo environment for the UI-guide
re-shoot — small, real, and now regression-tested:

- **Explicit UI fields win over `ui_config`** — the register save path
  spread `**ui_config` LAST, so its stale round-tripped copy of
  `show_on_dashboard`/`widget` silently overrode the value the caller
  actually set: hiding a dashboard card kept reverting on every save.
  Extras still round-trip; the explicit fields now always win.
- **Pure-Modbus discovery accepts loopback** — the scan/sweep/SunSpec
  guards rejected `127.0.0.1`, blocking legitimate commissioning against
  local simulators and, notably, probing the gateway's **own virtual
  meters** (127.0.0.1:1502) as a self-test. A Modbus frame to a local
  port cannot exploit an HTTP service, so loopback is now allowed there —
  while every HTTP-fetch guard (HTTP devices, Fronius Solar-API
  discovery) still rejects it: over HTTP, loopback is SSRF into the
  gateway's own API and neighboring local services.

## 3.35.2

### 2026-08-16 — the last unverified docs, verified (and one real role-gate fix)

Three independent verification agents covered the documents no prior pass
had touched systematically. Route surface: **all 142 routes documented,
zero ghosts**. What they caught:

- **Operator role gate (CODE fix)** — the documented contract granted the
  operator the fire-once TEST actions, but the segment matcher missed
  three shapes: the ad-hoc `POST /api/devices/test` probe (commissioning,
  4 segments) and the `rest-push/test` / `calculated/test` actions (6
  segments) were de-facto admin-only. The matcher now allows exactly
  those, nothing else at those depths; regression test added.
- **virtual-meter-spec.md** substantially corrected: pinned cumulative
  counters (excluded from device_fallback, frozen in EVERY policy — the
  spec claimed uniform on_stale degradation), legacy is a per-row
  fail-closed watchdog with the E1 unresolved-row withhold (not a "single
  watchdog, gaps keep last words"), the 4-register pad above the map top,
  the `fail` exception code pinned (2, same as an unmapped probe), the
  legacy quality-block caveats (count words read 0), plus previously
  unwritten wire behavior: all-stale ⇒ socket closed, write/FC1/FC2
  refusal, the freshness bound cascade + monotonic clock, the full
  sentinel-word table (incl. sm16/sm32 all-ones), sum semantics, the
  HTTP/JSON view contract, and port range/uniqueness.
- **API.md**: query `corrected` documented (single + batch + example),
  ui-security session re-issue, the logout exemption (viewer, not
  operator¹), builder routes that answer without ESPHome, `port=` on the
  builder WS, `measurement=` on /api/history, `GET /` serves the login
  shell.
- **architecture.md**: sessions persist (was "in-memory"), 7-day TTL (was
  12h), 120 min/200k buffer (was 10/50k), first-run login exception to
  "off by default", the true store entry shape (mono/interval), the
  correct decode stage order, apply_corrections as the one pipeline, the
  bound-cascade tiers + 300 s cap, self-heal scope, ungated alert kinds.
- **alerts-webhooks.md**: the three ungated alert conditions; no-redirects
  applies to every webhook POST. **csv-import.md**: sm16/sm32 types, the
  Wh unit contract, real alias lists, the example now matches the shipped
  download, YAML-only decode options stated. **upgrade-guide.md**: the UI
  path is Backup & Restore → Snapshots & Rollback. loadtest/README
  verified clean.

## 3.35.1

### 2026-08-16 — docs-vs-compose verification pass (independent re-check)

A fresh verification agent re-read every deployment claim against the
final compose. Nine real mismatches fixed:

- `up -d multi-bus-gateway` now starts EXACTLY the gateway — the
  `depends_on: mosquitto` silently dragged a second broker (and its host
  ports) into the overlay scenario; removed (the boot network-probe
  handles ordering).
- `SERIAL_BRIDGE_URL` was documented as settable but never passed to the
  container — now wired through compose + `.env.example`.
- config-reference: influx `org` default is `multibus`, buffer defaults
  are 120 min / 200,000 (were 12× stale), plus a new **compose-stack
  variables** table (broker/explorer ports, Influx first-boot vars,
  Grafana login, `PV_STACK_NETWORK`, `BRIDGE_EXCLUDE`).
- rtu-serial: the portmap lives in the `serial-bridge-data` volume, not a
  repo path; architecture.md's process model now describes the bundled
  stack; MANUAL §1 no longer implies you must supply a broker/Influx;
  README project trees list `mosquitto/config/` and `serial-bridge/`;
  legacy `janitza-*` container names → `mbg-*`; `MQTT_BROKER_PORT`
  disambiguated from the gateway's `MQTT_PORT` override; `docker run`
  notes it ships no broker.
- New §16 note (both manuals): the bundled stack's own doors — anonymous
  broker default + credential recipe, Explorer, Influx, Grafana login —
  and what to lock down beyond a trusted LAN.

## 3.35.0

### 2026-08-16 — the complete stack out of the box + the documentation catches up

**Deploy: one command, everything included.** `docker compose up -d` now
brings up the COMPLETE solution — gateway + **bundled Mosquitto broker**
(the gateway's default broker host is `mosquitto`, so publishing works
from the first boot) + **MQTT Explorer** on :4000 (see the data flowing) +
InfluxDB (self-configured on first boot, org/bucket `multibus`) + Grafana
+ ESPHome. Nothing external to install; a user testing the product gets a
working pipeline end-to-end. Want less? `docker compose up -d
multi-bus-gateway mosquitto`. Prefer your own broker/Influx? Repoint from
the UI — the bundled ones are ordinary containers. The created network
carries the ecosystem name (`pv-stack-network`, overridable), so later
services join it by name; the `docker-compose.pv-stack.yml` overlay
remains for joining an EXISTING stack (start only the gateway services).
The `rtu-bridge` profile stays the one opt-in (it needs `/dev`). Broker
config ships in-repo (`mosquitto/config/`) with a two-line recipe for
adding credentials; `.env.example` seeds working (change-me) Influx and
Grafana credentials.

**Documentation: complete-coverage pass to 3.3x.** Driven by two full
inventories (feature-gap vs the four main docs; a code-level catalog of
every defensive mechanism):

- **New page: [`docs/reliability.md`](docs/reliability.md)** — the entire
  fail-safety catalog in one place: wire→value integrity, link
  robustness, virtual-meter fail-safety, MQTT/InfluxDB delivery
  guarantees, config safety, and every security enforcement point, each
  with what it protects against and where it lives. Linked from the
  READMEs, both manuals and SECURITY.md.
- **SECURITY.md** gains the 7-layer security-model summary (first-run
  login, roles/passkeys/sessions, API key incl. the WS subprotocol,
  allowlist, the hardware-write gating chain, browser and process
  hardening); the stale "change default credentials" advice is gone.
- **Both manuals** (EN + RO, mirrored): the unit contract (§6.1), the
  transport-uniform decode pipeline with the symmetric monotonic filter
  (§6.2), retained availability + retained-command guard + HA
  number/select + compat aliases (§8), the true Influx buffer numbers
  (120 min / 200k, data-relative) + auth detection + backfill
  schema-from-selection (§9), unresolved-row withhold + per-row bounds +
  pinned counters (§11), link-verdict reachability + the query `corrected`
  field (§13), offset-on-write (§14), the full self-heal scope (§15), and
  §16 rewritten around sessions re-issue, WS API key, CSRF and browser
  hardening; §2 describes the complete stack.
- **READMEs** (RO + EN brought to parity): complete-stack quick start,
  Dockerfile.test test command, 140+ endpoints, missing doc links, HA
  write entities, enum/unit-contract feature bullets, rtu-bridge profile.
- Defaults aligned with the bundled stack: `mqtt.broker: mosquitto`,
  `influxdb.url: http://influxdb:8086`, org/bucket `multibus`
  (config.example + config-reference updated; existing configs are
  explicit and unaffected).

## 3.34.3

### 2026-08-15 — the canonical unit is visible where scales are set

The canonical dictionary always carried a unit per field (the API exposed
it), but the template editor never showed it — so nothing warned when a
kWh row sat under an energy_* name that promises Wh, which is exactly how
the Fronius 65A shipped mislabeled. The editor's Unit cell now:

- shows the canonical unit as a tooltip whenever the row's name is
  canonical ("canonical unit is Wh");
- flags AMBER on a mismatch, live while typing, with the fix spelled out
  (adjust the scale to deliver the canonical unit, or rename the row) —
  the same affordance names already had.

Server-side the save-time warning (3.30.0) remains the hard backstop;
this puts the contract in front of the person typing the scale.

## 3.34.2

### 2026-08-15 — modal stacking + wide template editor (UI); Fronius 65A unit settled empirically

- **Fronius 65A energy unit: kWh×10, settled by measurement** — the day's
  two magnitude-based conclusions were BOTH wrong (first "kWh" looked
  impossible on the assumption the meter was days old; then "Wh" looked
  right until the owner noted the meter is an old unit re-wired — its big
  lifetime index is legitimate). The decisive test: 12.4 minutes at
  ~±300 W moved both energy counters by ZERO ticks — impossible for a Wh
  counter (tens of units expected), exactly right for kWh at 0.1
  resolution. The catalog template and the live selection now convert
  kWh×10 raw to canonical Wh via scale 0.01; the template description
  records the method. Lesson encoded: for counter units, magnitude
  arguments are not evidence — a rate test is.

- **Nested modals stack by open order** — every modal shared z-index 1000,
  so which of two OPEN modals painted on top was DOM order: the enum
  "Decode states" builder (declared early in index.html) rendered BEHIND
  the template editor that opened it. openModal now assigns an
  incrementing z-index (reset on close), so the latest-opened dialog
  always wins; closing a nested modal no longer unlocks the body scroll
  while its parent is still open.
- **Template editor fits without horizontal scroll on desktop** — the
  15-column register grid was capped at 1120px and scrolled sideways.
  Now min(1560px, 96vw) with tighter cell padding, shrinkable text cells
  and explicit select widths (FC no longer truncates). Verified with
  Playwright at 1536×864 and 1440×900 across three CSS density variants
  (scrollWidth == clientWidth; vertical scroll only); below desktop
  widths the overflow-x fallback remains.

## 3.34.1

### 2026-08-15 — a security save no longer logs out its own author

Live incident (caught by the operator minutes after the 3.34.0 deploy): disabling
login, then re-enabling it while setting passwords, revoked EVERY session —
including the caller's — so the very next write returned 401, which the UI
misread as "enter the API key" (a dead end when no key is configured).

- The `ui-security` save now RE-ISSUES a fresh admin session cookie to the
  caller whenever passwords rotate or login turns on (with login previously
  off, the caller just SET the admin password — they are the admin). Other
  sessions stay revoked, as intended.
- The UI's 401 handler now checks `/api/auth/status` first: login enabled
  with no live role = dead session → the login overlay ("Session expired"),
  NOT the API-key prompt. The key prompt remains for the case it was built
  for (auth off / role present but a configured key missing).
- Regression test drives the exact incident sequence end-to-end.

## 3.34.0

### 2026-08-15 — deploy split, first-run login, release pipeline (repo stays private)

The go-public checklist executed to the last pre-flip item — the repository
remains PRIVATE; only the actual flip work (history purge, screenshot
re-shoot, vendor-doc licensing decision, GHCR tag) is still pending.

- **Deploy compose split** — the base `docker-compose.yml` stays fully
  standalone (creates its own network); the new `docker-compose.pv-stack.yml`
  overlay repoints the shared alias at an EXISTING external network
  (`pv-stack-network`, overridable via `PV_STACK_NETWORK`), so the gateway
  joins a wider stack with `-f base -f overlay` and zero base-file edits.
  The serial bridge now ships as the opt-in `rtu-bridge` compose profile
  (read-only /dev, cgroup-scoped ttyUSB/ttyACM, non-root) — closing the
  docs/compose mismatch the external audit flagged.
- **First-run login (B5)** — a fresh install (no config.yaml) generates an
  admin password, stores it hashed with auth ENABLED, and prints it once to
  the log. Bare-metal bind default is now loopback (`ui.host: 127.0.0.1`,
  `UI_HOST` env override); the container image sets `UI_HOST=0.0.0.0`
  explicitly — exposure is governed by the compose port mapping.
- **Backfill uses the live schema (B3)** — the point-building logic
  (`get_measurement`/`get_tags`/`build_point`) is extracted from the
  publisher as module functions; backfill maps HIST params to ADDRESSES
  only and derives everything else from `config/selected_registers.json`,
  with a line-protocol equality test. A deselected address is skipped, not
  written with a guessed schema.
- **Catalog enums** — status/identity registers now decode: Fronius 65A
  `model_id` (731, field-verified) and the EM24 detection register 0x000B
  (1651, from the cited Victron map) map to text via `enum`; unknown codes
  read honestly as `unknown (n)`. (Enums are declared per register IN the
  device template — `enum`/`bits`/`mask`/`shift` — and flow through
  selection, decode and HA typing automatically.)
- **Packaging hygiene** — GHCR release workflow added (fires ONLY on a
  version tag; gateway + serial-bridge, multi-arch; no tag pushed yet),
  `NOTICE` for the vendored MIT/Apache-2.0 UI assets, the last two missing
  AGPL headers, `config.example.yaml` refreshed (dead blocks removed,
  defaults aligned, referenced from the config reference), load-test swarm
  ported to pymodbus 3.15 (`device_id=`) with a staleness notice on the
  capacity report.
- **Docs truth pass** (21 findings from a full consistency sweep): README
  (both languages), MANUAL (both), config-reference, API.md, architecture,
  CONTRIBUTING and the compose override example now describe the CURRENT
  behavior — first-run generated login, loopback default bind, persisted
  sessions (a restart keeps you logged in; only the lockout counter is
  in-memory), the `rtu-bridge` profile, the pv-stack overlay, `UI_HOST` and
  `JANITZA_REGISTERS_PATH` documented.
- **Privacy scrub (pre-flip)** — real deployment details genericized in
  code/comments (reverse-proxy hostname, BMS neighbour naming, personal
  lockfile command); the remaining flip-only items (production screenshots,
  history purge, vendor register-list licensing) are inventoried in
  `.audit/go-public-inventory.md`.
- Tests: 899 passed, 0 skipped; both compose configurations validate.

## 3.33.0

### 2026-08-15 — reconciliation vs the re-verified external open list

The external audit's "Open Audit Items" list (re-verified at 3.28.0) was
reconciled against 3.32.0: most entries were already closed by the §I work
(decode unification, Wh canonical + pinned counters, self-heal, sm16,
WS key, lifecycle serialization, test-quality). These are the residuals
that were still real:

- **Boot discovery hooks are write-aware (HIGH)** — `main.py` registered
  its own hooks that published HA discovery WITHOUT write rules, and the
  write-aware `_sync_device_discovery` only ran from the device CRUD
  routes. After a plain restart the write-blind hooks won the first MQTT
  connect: every HA number/select was republished as a plain sensor and
  its command topic unsubscribed — control worked until the first restart,
  then silently died. ONE owner now: `create_api` registers the
  write-aware set at boot; `main.py` registers nothing.
- **A `read_bits` success resets the shared fail counter** — register
  failures + bits successes on a mixed device interfered and could trip a
  bogus forced reopen.
- **`encode_string` no longer byte-swaps under badc/dcba** — the parser
  documents strings as byte-sequential (ordering is a numeric-only
  concern); the encoder disagreed, so 'ABCD' round-tripped as 'BADC'.
- **`.good` promotion refuses a devices-shrink** — a file cut at a section
  boundary can stay plausible (modbus/mqtt intact) while losing the
  devices tail; promoting it destroyed the only recovery copy. Load now
  skips the promotion (with a warning) when devices would shrink vs the
  snapshot; an INTENTIONAL removal refreshes `.good` via `save_yaml_config`
  directly, so the conservative gate never goes stale.
- **The test suite does no outbound network I/O** — the write-guard tests
  executed `/api/config/apply` on a default config and built a REAL
  MQTTPublisher that connected to 192.168.1.100 in a daemon thread
  (verified: "No route to host" during runs; on an unlucky LAN, a real
  broker). Stubbed; a full run now opens zero non-loopback sockets.
- **pytest.ini** (there was none): `testpaths`, `-rs` (skips visible with
  reasons, never silent dots), and `error::DeprecationWarning` for our own
  modules — which immediately caught a real one (`asyncio.get_event_loop`
  in the poller thread, deprecated 3.12/removed 3.14) — fixed. CI coverage
  now includes `main.py` (was invisible at ~10%); unused `pytest-asyncio`
  dev-dependency dropped; catalog-test file handles closed.
- Docs: upgrade-guide no longer contradicts itself about the
  `config_version` stamp; the README test command uses `Dockerfile.test`
  (the runtime image has no pytest); API.md documents the two deliberate
  login-off 403s (snapshot download, secrets export); config-reference /
  upgrade-guide / yaml-import linked from the README; CONTRIBUTING states
  the real CI gates (ruff, coverage floor, random test order).

Still deferred to the go-public window (unchanged, tracked in
.audit/BACKLOG.md): GHCR release pipeline (B6), first-run generated
password + bind default (B5), backfill schema-from-config (B3 — live is
canonical post-Migration A, the public example is not), config.example
refresh, AGPL headers + vendor NOTICE, capacity re-run (swarm still calls
`slave=`), serial-bridge compose/doc alignment.

- Tests: 5 new; suite at 896 passed, 0 skipped.

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
  renamed data dir, bundled
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
an external notification service for Telegram/SMS. **Off by default**
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
