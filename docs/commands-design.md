# Commands — a universal write path for a gateway (design, 2026-09-13)

**Status: proposal, to be agreed before code.** Supersedes the hard-wired
power-limit action of 3.72.0, which becomes the first *command preset*.

## 1. The principle

MBG is a gateway. It reads devices in their own language and publishes in one
language; it must write the same way — **any input face, one internal path,
the vendor's language declared in the template, never in code.** A controller
(Node-RED, Home Assistant, a SCADA, a script) says *what* it wants
(`power_limit = 60 %`); the gateway knows *how* that is said to this device
(which registers, in which order, in one frame or three, verified how).
MBG never decides *when* — policy stays outside.

What exists today and is kept: the write gates (`security.allow_writes`,
`write_locked` per device, authentication, rate limit), the audit log, the
dead-man lease, the template's write envelope (`writable`, `write_min/max`,
`write_allowed`, `write_safe`), the MQTT command worker with its
retained-replay guard, the HA write entities, the shared-endpoint arbiter, the
per-source poll groups with `poll_now()`. What is new is one object and one
engine.

## 2. Concepts

| Term | Meaning |
|------|---------|
| **Command** | A named, parameterised write an operator can invoke on a device: `power_limit`, `restore`, `set_power_factor`, `battery_mode`… Declared once (in a template as a preset, or by hand on a device); bound to a device or to a group. |
| **Recipe** | *How* the command is spoken to this device: `guard` (reads that must hold before anything is written) → `writes` (register names + values) → `settle` → `verify` (reads compared to what was asked). Register **names** refer to the device's template, which owns address, type, scale, scale-factor, byte order, bounds. |
| **Face** | Where an invocation comes from: **API**, **MQTT** topic, **HA** entity, **Modbus TCP server** register (a "virtual controller"). Every face lands on the same engine; the face is only recorded in the audit as `via`. |
| **Invocation** | One run of one command on one device: parameters → verdict. |
| **Verdict** | `success` (verify matched), `mismatch` (written, device holds another value), `unverified` (written, verify did not answer), `rejected` (refused before touching the wire — with the reason), `error` (a write failed on the wire). Same words on every face. |
| **Result** | The invocation's record: verdict, before/after, what was written (frames), timing, `via`, who. Returned by the API, published on `…/cmd/result`, appended to the audit log, appended to the unit's Logs. |

## 3. Data model

### 3.1 In a template (presets)

```yaml
commands:
  power_limit:
    label: Active power limit
    params:
      value:    { unit: "%", min: 0, max: 100, required: true }
      revert_s: { unit: s, default: 600, min: 0, max: 65535 }
      ramp_s:   { unit: s, default: 0,   min: 0, max: 65535 }
    guard:
      - { read: model123_id, expect: 123 }            # the block is where we think
      - { read: wmaxlimpct_sf, in: [-2, -1, 0] }      # datalogger garbage never scales a write
    writes:                                            # consecutive addresses → ONE FC16
      - { register: power_limit_pct,      value: "${value}" }
      - { register: power_limit_win_s,    value: 0 }
      - { register: power_limit_revert_s, value: "${revert_s}" }
      - { register: power_limit_ramp_s,   value: "${ramp_s}" }
      - { register: power_limit_enabled,  value: { if: "${value} < 100", then: 1, else: 0 } }
    settle_s: 1
    verify:
      - { read: power_limit_pct,     expect: "${value}", tolerance: 1 }
      - { read: power_limit_enabled, expect: { if: "${value} < 100", then: 1, else: 0 } }
    safe: { value: 100 }                               # what a lease restores
    readback_group: controls                           # sweep now after a write
  restore:
    label: Restore full power
    alias: { command: power_limit, params: { value: 100, revert_s: 0 } }
```

A preset is *offered*, not active: the operator enables it on a device (one
click), and may edit a copy. A template without `commands` offers nothing;
nothing is ever assumed from the vendor name.

### 3.2 On a device (or a group of an installation)

```yaml
devices / endpoints.groups[].commands:
  - name: power_limit
    from_template: true              # preset, unchanged (or a full recipe, overriding it)
    enabled: true
    faces:
      api: true                      # POST /api/devices/{id}/commands/power_limit
      mqtt: { topic: "pv/inverters/${unit_id}/cmd/power_limit" }   # default derived from the unit's prefix
      ha: { entity: number }         # number / select / button, from the params
      modbus_server: { instance: controller-1, register: 1000 }    # phase 3
    confirm: true                    # the UI asks before running
    lease_s: 0                       # dead-man: 0 = rely on the device's own revert
```

A command bound to a **group** fans out to every unit of the group, one
invocation each, results per unit; `ok` only when every unit succeeded. A
partial failure never rolls back the others (a limit applied to three
inverters out of four is still the safer state).

### 3.3 Value expressions — deliberately tiny, never `eval`

- literals: `0`, `1`, `100`, `"text"`
- parameters: `"${value}"`, `"${revert_s}"` (defaults from `params`)
- one conditional: `{ if: "<expr> <op> <expr>", then: <v>, else: <v> }` with
  `< <= == != >= >`
- arithmetic on two operands: `{ "*": ["${value}", 10] }`, `{ "/": … }`,
  `{ "+": … }`, `{ "-": … }`
- register values in guards/verify: by name, decoded through the template
  (scale, SF, enum) — the engine compares engineering values, not raw words.

Anything more becomes a policy engine, which is the controller's job.

### 3.4 Where the vendor's knowledge lives — all in the template

| Knowledge | Where |
|-----------|-------|
| address, register type, data type, byte order | template register |
| scale, `scale_from` (SunSpec SF), offset | template register — **the write encoder applies them too** (today writes know a fixed `scale` only) |
| bounds, allowed values, safe value | template register (`write_*`) and command `params` |
| which registers, in what order, in one frame | command `writes` |
| "am I talking to the right block" | command `guard` |
| what proves it took | command `verify` |

`model123_id` (40227) and `power_limit_win_s` (40233) join the Fronius template
as ordinary registers (unrouted: neither published nor stored — plumbing).

## 4. The engine — one invocation

1. **Gates** (all faces): `security.allow_writes`; device `write_locked`; the
   command `enabled`; the face allowed for this command; authentication (API:
   login/API key; MQTT/HA: `mqtt.allow_write_entities`; Modbus server: the
   instance's allowlist); rate limit per `(face, device)`.
2. **Parameters**: parse (number or object; legacy names accepted by alias
   maps declared in the command), defaults, bounds → `rejected` with the
   reason, nothing on the wire.
3. **Driver**: the device's Modbus source (a Solar API cannot write; a unit
   without a Modbus source is `rejected: no writable source`).
4. **Guard** reads: each `read` decoded through the template; any mismatch →
   `rejected`, nothing written. A guard's raw values (e.g. the SF) feed the
   encoding of the writes that follow.
5. **Encode**: each write's value evaluated → engineering value → raw words
   through the template's type, scale, `scale_from` (live SF from the guard
   phase or the store), offset, byte order. Bounds from the register's
   `write_*` envelope enforce here too.
6. **Frames**: consecutive holding registers become one FC16 (a vendor that
   needs a half-written block to never exist gets exactly that); coils FC5/15;
   non-consecutive writes go in declaration order. If a frame fails, the
   invocation stops → `error`, with what did and did not go out in the result.
7. **Settle** `settle_s`, then **verify** reads → `success` / `mismatch` /
   `unverified`.
8. **Read-back**: verified values are pushed into the unit's live store at
   once; the command's `readback_group` is swept now (`poll_now`) so MQTT,
   InfluxDB and HA state follow within seconds.
9. **Record**: audit (`user`, `ip`, `via`, `device`, `command`, `params`,
   `frames`, `before`, `after`, `verdict`, `ms`), the unit's Logs
   (`command` event), `…/cmd/result` on MQTT, the API response.
10. **Lease**: `lease_s > 0` arms the dead-man with the command's `safe`
    params; expiry runs the same command with `safe` (audited as
    `via: lease-revert`). A `safe`-valued invocation clears the lease.

Concurrency: one invocation at a time per gateway **endpoint** (the shared
datalogger), queued FIFO; a second invocation for the same device while one
runs waits, it is never merged. Every read/write in the engine goes through the
endpoint arbiter like a poll does.

Night and outages: an unreachable device → `error: no answer` after the
driver's normal timeout; nothing is retried after a frame went out (writes are
not safely idempotent in a sequence).

## 5. The faces

### 5.1 API
- `POST /api/devices/{id}/commands/{name}` `{…params}` → result (200 for
  success/mismatch/unverified, 422 rejected, 502 error, 403 gates, 409 no driver).
- `POST /api/endpoints/{id}/groups/{gid}/commands/{name}` → per-unit results.
- `GET /api/devices/{id}/commands` → what this device offers (params with
  bounds/defaults, faces, enabled, last result) — what a UI or a Node-RED flow
  discovers instead of hard-coding.
- `POST …/commands/{name}/dry-run` → the frames that *would* be written and
  the guard's current readings, nothing on the wire (the UI's *Test*).
- `GET /api/commands/history?device=` → invocations from the audit log.
- The 3.72.0 routes `…/actions/power_limit` stay as aliases of the command.

### 5.2 MQTT
- `<unit prefix>/cmd/<name>` — payload: a bare number (the `value` param) or
  an object of params; `source` optional (lands in the audit as the user).
- `<group prefix>/cmd/<name>` — the group fan-out.
- `<unit prefix>/cmd/result` — the result, not retained.
- `<unit prefix>/cmd/<name>/state` — retained, the last applied params and
  verdict, so a controller that restarts knows what the plant was told.
- Retained commands are never executed (a replay is not an order); commands
  run on the command worker, never on paho's thread.

### 5.3 Home Assistant
From the command's `params`: one `value` param with bounds → a `number`
entity; `allowed` values → `select`; no `value` param → a `button`. The
entity's command topic is the MQTT face; its state is the read-back register
(`controls/*`). The `power_limit_pct` register's number entity of 3.72.0 is
exactly this.

### 5.4 Modbus TCP server — the "virtual controller" (phase 3)
The virtual-meter server (`multibus/virtual_meter.py`, a custom asyncio
Modbus TCP server that today answers FC1/2/3/4) learns FC6/FC16. A *controller
instance* is a vmeter template with **writable** registers, each bound to a
command param of a target device or group:

```yaml
virtual_meters:
  - id: controller-1
    port: 1510
    template: controller_power_limit
    bindings:
      - { register: 1000, target: { endpoint: pv, group: inverters }, command: power_limit, param: value }
      - { register: 1001, target: { endpoint: pv, group: inverters }, command: power_limit, param: revert_s }
      - { register: 1010, mirror: { device: pv-u1, register: power_limit_pct } }   # read-back
      - { register: 1011, status: { device: pv-u1, command: power_limit } }        # last verdict code
```

A write to 1000 runs the command (with the other bound params' current
values); the reply to the writer is Modbus-correct (exception 4 = server
device failure when the verdict is `error`, illegal data value when `rejected`);
the status register carries the verdict code; the mirror registers show the
read-back. Allowlist per instance; every write audited `via: modbus-server`
with the client's IP.

## 6. What the operator sees

**Unit page → Commands tab** (installation page: the group card's *Commands*):
- the commands this device offers: name, label, faces (API / MQTT topic /
  HA / Modbus), enabled, last invocation (verdict, when, by whom);
- **Run**: a form built from `params` (bounds, defaults, units), a confirmation,
  the result in words per unit;
- **Test (dry run)**: the guard's current readings and the exact frames that
  would go out — no wire;
- **Enable a preset** from the template in one click; **Add a command by
  hand**: name, params, writes chosen from the template's writable registers
  (name → address/type/scale shown, never typed), guard/verify rows, faces;
- **History**: the audit's invocations for this device.

Vocabulary: *Command* (not action, not write), *Run*, *Test*, *Result*.
Every icon button has a label; results are words with colour as a second cue.

## 7. Migration from 3.72.0

- `multibus/power_limit.py` → the generic engine (`multibus/commands.py`); its
  SF check and "Ena travels with the percent" become the Fronius preset's
  `guard` and `writes`. `test_power_limit.py` moves to the preset (same cases).
- The Fronius template gains `model123_id` (40227), `power_limit_win_s`
  (40233) and the `commands.power_limit` / `commands.restore` presets; the
  production units enable the preset (config migration, idempotent).
- `…/actions/power_limit` and `…/cmd/power_limit` keep working (aliases), so
  tomorrow's live test and the Node-RED re-pointing are unaffected.
- The write encoder learns `scale_from` (the only new low-level capability).

## 8. Tests

- expressions: every operator, defaults, a rejected unknown name;
- encoding with `scale_from` (SF −2/−1/0), bounds, byte orders;
- frame grouping: consecutive → one FC16, gaps → several, coils;
- engine verdicts on a fake device: guard mismatch (nothing written), write
  failure mid-sequence (result says what went out), settle+verify, mismatch,
  unverified, lease revert with `safe`;
- faces: API (device, group, dry-run, history), MQTT (number/object/legacy
  names, retained ignored, result + state topics), HA entity generation,
  Modbus server writes with exception codes;
- e2e: Commands tab (run with confirm, test, enable preset, add by hand),
  results in words;
- live protocol (Fronius): 95 % on one unit with revert 120 s → read-back →
  automatic revert → 95 % again → `restore` → group fan-out; power observed.

## 9. Phases

1. **Engine + template presets + API/MQTT/HA faces + Commands tab** (replaces
   3.72.0's action; live test runs on it).
2. **Dry run, history, add-by-hand editor.**
3. **Modbus TCP server face** (virtual controller) — with the first consumer.
4. Second vendor (SolarEdge…) = a template with a recipe, on real hardware.

## 10. Decisions to confirm

1. Commands live on the **device/group** (bound) and presets in the
   **template** — no global command registry.
2. Expression grammar as in §3.3 — tiny on purpose.
3. Group fan-out semantics: per-unit results, no rollback.
4. MQTT: `…/cmd/<name>`, `…/cmd/result`, retained `…/cmd/<name>/state`.
5. HA mapping (number / select / button) from `params`.
6. The Modbus-server face's reply codes and status register (§5.4).
7. Naming: *Command / Run / Test / Result* in the UI.
