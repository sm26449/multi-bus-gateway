# Rules — declarative control inside the gateway (design, 2026-09-14)

**Status: agreed 2026-09-14; phase 1 shipped in 3.74.0** (engine, runtime,
API, MQTT, Rules page; `steps` + `condition`). Phases 2–3 (§9) open. Builds on
[commands-design.md](commands-design.md) (3.73.0): a rule is *what decides
when* a command runs. With rules, MBG is a gateway **and** a small,
declarative controller — never a scripting host.

## 1. The principle

Everything MBG knows is a **register in the live store**: a Modbus register,
an HTTP field, a value that arrived on an MQTT topic (the existing `mqtt`
device protocol subscribes to any broker, local or remote, with per-register
topics and JSON paths). A rule therefore has no input type of its own — it
reads `device.register` exactly like a calculated register does, inherits the
oldest input's freshness, and never launders a dead device's value as fresh.

A rule is three declared things and nothing else:

```
inputs  →  a signal / a condition with TIME semantics  →  a command with parameters
           (steps, hysteresis, debounce, hold, rate limit, staleness)
```

What it is **not**: a place for arithmetic policy of arbitrary shape. The
signal grammar is the calculated-register grammar (`expressions.py`: safe
arithmetic, comparisons, `min/max/avg/abs/round`, `prev()`); the time
semantics are fixed fields. Anything beyond that stays in Node-RED, which
speaks to the same commands.

Two guarantees no other layer gives:

- **Closed loop.** The read-back of the commanded register is already in the
  store. A rule compares *want* with *actual* and re-commands on drift; it does
  not blindly re-send on a heartbeat.
- **Fail closed.** A stale signal never relaxes a limit. Disabling a rule that
  holds a device away from its safe state runs the command's `safe`.

## 2. Concepts

| Term | Meaning |
|---|---|
| **Rule** | A named, declared controller for one target: inputs, kind, timing, safety, mode. |
| **Target** | A device or a group of an installation, plus the command it drives (`pv-u1 · power_limit`, or `pv/inverters · power_limit` fanned out per unit). |
| **Signal** | A numeric expression over live registers (`max(pv-u1.voltage_l1_n, pv-u1.voltage_l2_n, pv-u1.voltage_l3_n)`). |
| **Kind** | How the signal becomes parameters: `steps` (thresholds → value, with a release threshold), `condition` (boolean → run A / run B), `setpoint` (linear map, phase 2). |
| **Want** | The parameters the rule currently asks for. |
| **Actual** | The read-back of the command's value register, from the store, with its age. |
| **Decision** | One evaluation's outcome, with its reason: `run`, `hold` (debounce 2/3, rate limit, dead band), `shadow` (would run), `stale-hold`, `reassert`, `clamp`. Kept per rule, published, logged. |
| **Mode** | `shadow` — evaluates, decides, publishes, **never writes**; `armed` — writes. New rules are shadow. |
| **Owner** | The one thing allowed to command a target: a rule, or `external` (API/MQTT/HA callers). |
| **Clamp** | An operator ceiling on a numeric want, with an expiry (`at most 60 % for 2 h`) — Node-RED's *manual floor*, generalised. |

## 3. Data model

Rules live in `rules.yaml` next to `config.yaml` (they span devices; the
device files stay the devices'). Saved through the API and the UI.

### 3.1 `steps` — thresholds with a release (the over-voltage protection)

```yaml
rules:
  - id: ov-u1
    label: Over-voltage protection · inverter 1
    enabled: true
    mode: shadow                                # shadow | armed
    target: { device: pv-u1, command: power_limit }
    kind: steps
    signal: "max(pv-u1.voltage_l1_n, pv-u1.voltage_l2_n, pv-u1.voltage_l3_n)"
    signal_valid: { min: 100, max: 350, max_step: 10 }   # outside min/max → ignored; a change > max_step
                                                # against the last accepted reading is held until the
                                                # next sample confirms it (one-sample artefacts never act)
    stale_after_s: 60                           # oldest input older than this → stale
    steps:                                      # ascending; the highest matching step wins
      - { at: 250.0, value: 80, label: Warning }
      - { at: 251.0, value: 70, label: Moderate }
      - { at: 252.5, value: 60, label: Severe }
      - { at: 253.0, value: 50, label: Emergency, fast: true }   # fast: no debounce
    release_below: 248.0                        # normal only under this; 248..250 holds the step
    normal: { value: 100 }                      # what "no step" asks for
    params: { revert_s: 0, ramp_s: 10 }         # sent with every want (the rule is the revert clock)
    timing:
      every_s: 2                                # evaluation tick
      debounce: 3                               # consecutive samples that must agree
      min_interval_s: 30                        # between two commands on this target
      reassert_s: 120                           # re-command when actual ≠ want for this long
    on_stale: hold                              # hold | safe   (hold = fail closed)
    on_disable: safe                            # safe | hold
```

`value` lands on the command's value parameter (`param:` overrides the name).
Step `label`s are the rule's **states** (`normal`, `Warning`, …, `stale`), shown
in words and published.

### 3.2 `condition` — a boolean with two outcomes

```yaml
  - id: grid-lost-restore
    label: Restore full power when the grid is gone
    target: { endpoint: pv, group: inverters, command: power_limit }
    kind: condition
    when: "pv-meter.frequency < 45 or pv-meter.voltage_ln_avg < 100"
    then: { params: { value: 100, revert_s: 0 } }
    else: null                                  # nothing when false
    timing: { every_s: 2, debounce: 2, min_interval_s: 60 }
    stale_after_s: 30
    on_stale: hold
```

### 3.3 `setpoint` — a linear map (phase 2)

```yaml
    kind: setpoint
    signal: "site.export_power"
    map: { from: [4000, 6000], to: [100, 0], clamp: true }   # W → % limit
    deadband: 2                                                # % change worth a command
```

### 3.4 Inputs from another system

Nothing new: an `mqtt` device on any broker.

```yaml
devices:
  - id: cerbo
    protocol: mqtt
    mqtt_in: { broker: 192.168.1.5, port: 1883, username: …, password: … }
    registers:
      - { name: grid_setpoint, topic: "N/…/Settings/CGwacs/AcPowerSetPoint", json_path: "value", unit: W }
```

A rule then reads `cerbo.grid_setpoint`. Freshness, health and the unit page
come for free. A Node-RED flow that today computes a limit and publishes it
needs no rule at all — it publishes to `…/cmd/power_limit` as in 3.73.0.

### 3.5 Ownership

A `(target, command)` has at most one owner. Saving a rule on a target owned by
another armed rule is refused. While a rule is **armed**, a command on its
target from API/MQTT/HA answers `rejected: owned by rule ov-u1`, unless the
caller passes `override_s: 900` — the rule pauses for that long, audited, and
the pause is visible on the Rules page. In **shadow** the target stays
`external`, so the current Node-RED OV keeps working during the trial.

## 4. The engine — one evaluation

`multibus/rules.py`, pure (no I/O, no clock): `RuleState.evaluate(now, signal, actual) → Decision`.

1. **Resolve** the signal through the calc engine's resolver (`device.register`),
   note the oldest input's age. Missing or `signal_valid` violated → the sample
   is ignored (counted; the UI says so). Age > `stale_after_s` → **stale**:
   `hold` keeps the last want; `safe` wants the command's `safe`.
2. **Desired**: `steps` — the highest step with `at <= signal`; none and signal
   `< release_below` → `normal`; in between → keep the current state (dead
   band). `condition` — `then` / `else`. Fold the **clamp**: `want = min(want,
   clamp)` while it has not expired; a clamp never expires *into* a step (it
   holds until the signal is under `release_below`, like the OV node).
3. **Debounce**: a desired that differs from the current state needs
   `debounce` consecutive agreeing **samples** (the rule ticks faster than
   the signal is polled; several ticks over the same reading are one vote),
   except a step marked `fast` (emergency path). Decision `hold (debounce
   2/3)` meanwhile. Before all this, the **plausibility guard**: with
   `signal_valid.max_step`, a reading that differs from the last accepted
   one by more than the step is held (`ignored: implausible jump …`) until
   the next sample agrees with it — a lone artefact (2026-09-18: one Solar
   API reading of 273 V on all three phases, grid meter at 241 V) never
   reaches a step, fast or not; a real jump costs one poll interval.
4. **Rate limit**: a command less than `min_interval_s` after the previous one
   is `hold (rate limit)`; the desired is kept and applied on the next tick
   that allows it.
5. **Closed loop**: with nothing new to ask, compare `actual` (the read-back
   register's value and age) with the want. Off by more than the command's
   verify tolerance for longer than `reassert_s` → `reassert`. This replaces
   the heartbeat: a lost write self-heals, an untouched one is left alone. If
   the read-back itself is older than its poll interval × 3 the rule asks the
   controls group to be swept (`poll_now`) instead of writing blind.
6. **Act**: in `armed`, run the command through `_run_named_command(...,
   via='rule:ov-u1')` — same gates, same audit, same result topics. In
   `shadow`, record `shadow (would run power_limit value 60)` and nothing
   else. A verdict other than `success` counts as a failure; three in a row
   raise an alert through `AlertManager` (`rule ov-u1 cannot apply its
   command`) and the rule keeps trying at `min_interval_s`.
7. **Record**: every decision that changes state, want, or outcome is a
   `Decision` (ring of 200 per rule, event log, MQTT event); a retained
   **state** carries mode, state, signal, want, actual, since, clamp, last
   decision and reason.

The runtime (`rules_runtime.py`) is one thread ticking at the smallest
`every_s`, evaluating every enabled rule against the store, then executing
armed decisions serially under the command lock. Group targets fan out per
unit with per-unit actual and per-unit decisions; the rule's state is the
worst unit's.

Startup: a rule restores nothing from disk except its clamp (with expiry);
its first evaluations rebuild the state from live values, debounced as usual.
`alert_on_start`-style surprise is avoided by design: the first want that
differs from actual is applied like any other, after debounce.

## 5. Faces

### 5.1 API (admin)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/rules` | every rule with its live state, want, actual, mode, owner, last decision |
| POST / PUT / DELETE | `/api/rules`, `/api/rules/{id}` | create, edit, delete — validated (target exists and offers the command, signal parses and resolves, steps ascending, `release_below` under the first step, timing bounds); a delete of an armed rule holding a non-safe want runs `on_disable` first |
| POST | `/api/rules/{id}/mode` | `{mode: shadow\|armed}` — arming is an explicit, audited act |
| POST | `/api/rules/{id}/enable` | `{enabled: bool}` — disabling applies `on_disable` |
| POST | `/api/rules/{id}/clamp` | `{max: 60, expires_s: 7200}`; `DELETE` clears |
| POST | `/api/rules/{id}/override` | `{seconds: 900}` — pause the owner for a manual command |
| GET | `/api/rules/{id}/decisions?limit=` | the decision ring, newest first |
| POST | `/api/rules/validate` | the rule as a body → what it would decide **now** against the live store (signal value, matched step, want, actual), no state kept |

### 5.2 MQTT

| Topic | Payload | |
|---|---|---|
| `mbg/rules/<id>/state` (retained) | `{mode, enabled, state, signal, want, actual, actual_age_s, since, clamp, decision, reason, ts}` | on every change |
| `mbg/rules/<id>/event` | `{from, to, signal, want, reason, ts}` | on state transitions and failures |
| `mbg/rules/<id>/set` | `{enabled}`, `{mode}`, `{clamp: {max, expires_s}}` | with `mqtt.allow_write_entities` + an authenticated broker session; replaces Node-RED's `pv-stack/nodered/ov/<n>/enable` and `…/manual_floor` |

### 5.3 Home Assistant (phase 2)

A `switch` per rule (enabled), a `sensor` for its state, a `number` for the
clamp — from the same discovery path as devices.

## 6. What the operator sees

**Rules page** (main navigation). A list, one row per rule: label, target,
mode pill (*Shadow* / *Armed*), state in words (*Normal*, *Warning*, *Stale*),
signal with unit, **want → actual** with the actual's age, last decision and
its reason in words, owner. A stale or failing rule is said, not only
coloured.

**Rule editor**: kind first; target picked from the installations (device or
group) and the command from what that target offers (3.73.0's list) — the
value parameter's bounds and unit follow; signal field with a live preview
(`/api/rules/validate`: "now 231.4 V → Normal, want 100, actual 100");
steps as a small table with labels; release threshold with the dead-band
explained; timing fields with defaults and one-line hints; safety fields with
their default *hold*. **Shadow is on for a new rule**; *Arm* is a separate
button with a confirmation that names the target.

**Rule page**: the decision log (when, signal, state, want, actual, decision,
reason, result), *Clamp for…* (value + duration), *Override for…*, *Arm* /
*Shadow*, *Disable*. In shadow, an agreement line: "in the last 24 h the rule
wanted what the device had 96 % of the time; 3 disagreements" — the evidence
for arming.

Vocabulary: *Rule*, *Shadow*, *Armed*, *Want / Actual*, *Decision*, *Clamp*,
*Override*. Every icon has a label; colour is never the only cue.

## 7. Migrating the over-voltage protection

Today: `pv-stack-ov-protection` nodes in Node-RED, one per inverter, fed by
`fronius/inverter/N/PhVph{A,B,C}` (the legacy collector), steps 250/251/252.5/253
→ 80/70/60/50 %, restore under 248 V, debounce 3, stale 60 s → hold,
revert_timeout 0, ramp 10 s, rate limit 30 s, heartbeat 120 s, manual floor
with expiry, staggered per inverter.

1. **Shadow.** Four rules `ov-u1..4` as in §3.1, signal from MBG's own
   SunSpec per-phase voltages, mode shadow. Node-RED keeps control. The
   Rules page shows want vs actual (actual = what Node-RED did) for a week.
2. **Sampling.** SunSpec `normal` is 20 s on this datalogger — slower than
   the collector's ~2–30 s cadence the OV node sees today. If the trial shows
   late reactions, add the Solar API `3PInverterData` collection as a source
   (one cheap HTTP call per inverter, per-phase voltages at 5 s) rather than
   pushing Modbus faster. Open point, measured in the trial.
3. **Cutover**, at a low-voltage moment: disable the Node-RED OV nodes
   (`pv-stack/nodered/ov/N/enable false`), arm the four rules. Node-RED's
   manual floor becomes the clamp; its enable topic becomes
   `mbg/rules/ov-uN/set`.
4. **Retire** the Node-RED nodes after a week armed.

## 8. Tests

- `tests/test_rules.py` (pure engine): step selection and the dead band,
  release hysteresis, debounce and the fast step, rate limit, stale hold vs
  safe, clamp fold and expiry-not-into-a-step, reassert on drift only, shadow
  never acts, condition then/else, invalid samples ignored, group worst-unit
  state.
- `tests/test_rules_api.py`: CRUD + validation, validate-now, mode changes
  audited, ownership refusal and override, `on_disable` runs safe, MQTT state
  retained / set topic gated, alert after three failures.
- e2e: the Rules page on the safety fixture (list, editor with live preview,
  arming confirmation names the target, decisions in words).

## 9. Phases

1. Engine (`steps`, `condition`) + `rules.yaml` + API + MQTT state/event/set +
   Rules page (list, editor, decision log) + shadow/armed + ownership +
   clamp/override API. The four OV rules created **in shadow** on production.
2. `setpoint` kind, HA entities, clamp/override in the UI, the agreement line,
   alerts on repeated failure.
3. OV cutover (§7), then retire the Node-RED nodes.

## 10. Decisions to confirm

1. Inputs are registers of devices — a remote system enters through an
   `mqtt` device on its broker. No separate input type.
2. Phase 1 kinds: `steps` and `condition`; `setpoint` in phase 2.
3. One owner per target+command; an armed rule refuses other faces unless
   overridden for a stated time.
4. Closed loop: re-command on read-back drift after `reassert_s`, not on a
   heartbeat.
5. Stale defaults to `hold` (fail closed); disable defaults to `safe`.
6. New rules are shadow; arming is a separate, confirmed, audited act.
7. Storage in `rules.yaml`; topics `mbg/rules/<id>/state|event|set`.
8. Vocabulary: Rule / Shadow / Armed / Want / Actual / Decision / Clamp / Override.
