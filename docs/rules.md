# Rules: a working guide with recipes

A **rule** runs a device **command** when live values call for it. It
watches a signal, waits for it to settle, and asks the target for a value:
cap the inverters when the grid voltage climbs, restore them when it drops.
The reference (every field, the evaluation step by step) is
[MANUAL §14c](MANUAL.md#14c-rules--the-declarative-controller); this page is
the practical side: how to go about it, recipes to start from, and moving
rules between installations.

- [1. Before you write one](#1-before-you-write-one)
- [2. The safe way to bring a rule in](#2-the-safe-way-to-bring-a-rule-in)
- [3. Recipes](#3-recipes)
- [4. Export and import](#4-export-and-import)
- [5. When a rule does not do what you expect](#5-when-a-rule-does-not-do-what-you-expect)

## 1. Before you write one

A rule needs three things to exist first:

1. **A target that can be told something.** The device's template offers
   a **command** (a power limit, a setpoint…) and the command is
   **enabled** on that device or group. See
   [MANUAL §14b](MANUAL.md#14b-commands--named-writes-from-the-template).
   The rule editor only lists targets that qualify.
2. **The values it watches**, read live by some device. Any device works,
   on any protocol: a meter, the inverter itself, or an MQTT device that
   brings in a value from another system.
3. **Writes allowed** (`security.allow_writes`) and a login or API key.
   You need them only to **arm**; shadow rules need neither.

**Signals are expressions** over live values, written `device.field`:

```
max(pv-u1.voltage_l1_n, pv-u1.voltage_l2_n, pv-u1.voltage_l3_n)
umg512.power_active_total < -3000 and battery-bank.pack_average_soc >= 98
```

Functions: `min`, `max`, `avg`, `abs`, `round`, `sqrt`, `pow`, `floor`,
`ceil`, `clamp(x, lo, hi)`, with `+ - * /`, comparisons, `and` and `or`
(write a negation as the opposite comparison: `x <= 50`, not `not x > 50`).
Device ids with hyphens are fine. **Preview now** in the editor
evaluates it against this second's values.

**Two kinds:**

| Kind | Use it for | Shape |
|---|---|---|
| **steps** | a value that should fall in bands as a signal rises (protection) | `signal`, `steps: [{at, value}]`, `release_below`, `normal` |
| **condition** | a yes/no situation with an action for each answer | `when`, `then`, `else` |

## 2. The safe way to bring a rule in

1. **Save it in shadow.** A rule is always created in shadow: it evaluates,
   decides and logs what it *would* do. It writes nothing.
2. **Watch it on a day that matters.** The rule's card shows:
   - its state in words, the signal and its age;
   - for each unit, *want → actual*;
   - **Decisions**, the log of every notable choice and why.

   Compare with what you expected, or with the system it replaces.
3. **Arm it.** The **Arm…** button names the target and asks you to confirm.
   It is audited. **To shadow** takes it back; leaving armed releases the
   target to its safe value (`on_disable`).
4. While armed, the rule **owns** its target. Another system's command on it
   answers *owned by rule* unless that system asks for a pause. You can use
   **Pause…** on the card for the same purpose.

Two settings decide what happens when things go wrong:
- `on_stale`: the signal's inputs stopped updating. `hold` keeps the last
  decision; `safe` returns to the safe value; a number asks for that value
  ("fail closed").
- `signal_valid`: `{min, max, max_step}` makes an implausible reading (a
  0 V at night, a single +27 V glitch) leave the state unchanged instead of
  acting on it.

## 3. Recipes

The ids below (`pv-u1`, `umg512`, `battery-bank`, the `pv` installation and
its `inverters` group) are examples. Use your own, from the Devices page.

### 3.1 Over-voltage protection on one inverter (steps)

The grid voltage is high, so reduce feed-in in steps; release only once the
voltage is clearly back.

```yaml
- id: ov-u1
  label: Over-voltage protection · inverter 1
  target: { device: pv-u1, command: power_limit }
  kind: steps
  signal: "max(pv-u1.voltage_l1_n, pv-u1.voltage_l2_n, pv-u1.voltage_l3_n)"
  signal_valid: { min: 100, max: 350, max_step: 10 }   # ignore night zeros and glitches
  steps:
    - { at: 250.0, value: 80, label: Warning }
    - { at: 251.0, value: 70, label: Moderate }
    - { at: 252.5, value: 60, label: Severe }
    - { at: 253.0, value: 50, label: Emergency, fast: true }   # no debounce
  release_below: 248.0          # back to normal only under 248 V
  normal: { value: 100 }
  params: { revert_s: 0 }
  timing: { every_s: 2, debounce: 3, min_interval_s: 30, reassert_s: 120 }
  stale_after_s: 60
  on_stale: hold
  on_disable: safe
```

Copy it once per inverter (`ov-u2`… with its own target and signal), or
point one rule at the whole group with
`target: { endpoint: pv, group: inverters, command: power_limit }` and a
signal from the site meter.

### 3.2 Over-frequency derating for a whole group (steps)

```yaml
- id: of-derate
  label: Over-frequency derating
  target: { endpoint: pv, group: inverters, command: power_limit }
  kind: steps
  signal: "umg512.frequency"
  signal_valid: { min: 45, max: 55, max_step: 1 }
  steps:
    - { at: 50.2, value: 90, label: High }
    - { at: 50.5, value: 60, label: Very high }
    - { at: 51.0, value: 30, label: Critical, fast: true }
  release_below: 50.1
  normal: { value: 100 }
  timing: { every_s: 1, debounce: 2, min_interval_s: 10 }
  stale_after_s: 20
  on_stale: 100                 # signal lost → full power (or `hold`)
```

### 3.3 Battery full and exporting hard: curtail (condition)

```yaml
- id: full-and-exporting
  label: Battery full and exporting — curtail PV
  target: { endpoint: pv, group: inverters, command: power_limit }
  kind: condition
  when: "battery-bank.pack_average_soc >= 98 and umg512.power_active_total < -5000"
  then: { params: { value: 60 } }
  else: { params: { value: 100 } }
  timing: { every_s: 5, debounce: 6, min_interval_s: 120 }   # 30 s of agreement, at most every 2 min
  stale_after_s: 120
  on_stale: hold
```

The meter's sign convention matters here. Check on the device page whether
export reads negative or positive before writing the comparison.

### 3.4 Grid gone: restore full power (condition, one outcome)

```yaml
- id: grid-lost-restore
  label: Grid lost — release every limit
  target: { endpoint: pv, group: inverters, command: power_limit }
  kind: condition
  when: "umg512.frequency < 45 or umg512.voltage_ln_avg < 100"
  then: { params: { value: 100, revert_s: 0 } }
  else: null                    # when false: do nothing
  timing: { every_s: 2, debounce: 2, min_interval_s: 60 }
  stale_after_s: 30
```

### 3.5 A value from another system

Anything another system publishes over MQTT becomes a signal: add it as an
**MQTT device**, then use it as `device.field`. Its freshness and health
come with it. An example is a setpoint from a home-automation system, read by
a device `ha` with a row `export_limit` on topic `home/limits/export`:

```yaml
  when: "umg512.power_active_total < -ha.export_limit"
```

## 4. Export and import

**Rules → Export** downloads every rule as one YAML file. The download
button on a rule's card exports just that one. The file holds definitions
only: no live state, no decision history.

**Rules → Import**: paste the file or open it, then press **Check**. Every
rule is tried against **this** installation, and nothing is written yet:

| Status | Meaning |
|---|---|
| **new** | will be created |
| **replaces the one here** | an existing rule with this id will be overwritten (only with *Replace* ticked) |
| **already here** | same id exists; tick *Replace* to overwrite |
| **cannot import** | with the reason: the target device or command does not exist here, an expression names an unknown device, a field is out of range… |

**Import** then creates the valid ones. Two rules that keep this safe:

- **Imported rules always arrive in shadow**, even if the file says
  `armed`. The check says so. Arming is an act on the installation that
  runs the rule, after watching it there.
- **An armed rule here is never replaced by an import.** Put it in shadow
  first.

Typical uses: try a rule on a test gateway, then import it on the real one;
keep your rules in version control; copy a protection to a second site,
changing the device ids in the file first.

API: `GET /api/rules/export[?ids=a,b]`,
`POST /api/rules/import {yaml, apply, replace}` ([API.md](API.md)).

## 5. When a rule does not do what you expect

| On the card | Why | What to do |
|---|---|---|
| *cannot run* | the target or an input does not resolve (renamed device, command not enabled) | the tooltip names it; fix the device or the rule |
| state **stale** | an input is older than `stale_after_s` | check the device is reading; raise `stale_after_s` for slow sources |
| signal **ignored** | outside `signal_valid` or a jump bigger than `max_step` | expected for glitches; widen it if real values are rejected |
| wants a value, actual stays | in shadow (nothing is written), or the command is refused | arm it; see the command's last result on the device |
| changes too often | debounce/min interval too short | raise `debounce` (samples) and `min_interval_s` |
| reacts too slowly | debounce × every_s too long | lower them, or mark the emergency step `fast: true` |

Every decision is also an InfluxDB point (`rule_event`) and an MQTT state
(`mbg/rules/<id>/state`), so you can chart a rule next to the values it
watches.
