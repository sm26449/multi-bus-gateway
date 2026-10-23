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
- Sequences: [§3.10](#310-sequences-commands-in-order)

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
umg512.power_active_total < -3000 and bms.soc >= 98
```

Functions: `min`, `max`, `avg`, `abs`, `round`, `sqrt`, `pow`, `floor`,
`ceil`, `clamp(x, lo, hi)`, `first(a, b, …)` (the first that has a value),
`fresh(x)`, `count_fresh(a, b, …)`, `age(x)`, `absent()`, `steps(x, below,
t1, v1, …)`, `ema(x, seconds)`, `hysteresis(x, low, high)`
([examples](templates-advanced.md#calculated-fields)), an installation's
totals by its id (`battery-bank.pack_average_soc`),
`sun_elevation("pv")`, `sun_azimuth("pv")`, `is_day(5, "pv")` and
`attr("umg512", "grid_connection", "max_export_kw")`
([attributes.md](attributes.md#using-them)),
with `+ - * /`, comparisons, `and` and `or`
(write a negation as the opposite comparison: `x <= 50`, not `not x > 50`).
Device ids with hyphens are fine. **Preview now** in the editor
evaluates it against this second's values.

`signal_unit` (steps and trim, optional, up to 40 characters) says what the
number means, for the card and the decisions: `W under the cap` turns
`656.9` into something an operator can read.

**Four kinds:**

| Kind | Use it for | Shape |
|---|---|---|
| **steps** | a value that should fall in bands as a signal rises (protection) | `signal`, `steps: [{at, value}]`, `release_below`, `normal` |
| **condition** | a yes/no situation with an action for each answer | `when`, `then`, `else`, optional `release` |
| **trim** | a value nudged up and down until a signal sits in a band (follow the solar surplus) | `signal`, `above`, `below`, `step`, `min`, `max`, optional `gain`, `start`, `off_value` |
| **schedule** | a value by the clock: days and times | `schedule: [{days, at, value}]`, optional `when` + `off_value` |

**Time and counters** in the same expressions:
- `hour()`, `minute()`, `weekday()` (1 = Monday … 7 = Sunday), `day()`,
  `month()`;
- `between("10:00", "16:00")`, true inside the window. A window that ends
  before it starts crosses midnight: `between("22:00", "06:00")`;
- a rule's counters, by its id: `run_today_h("pool")`, `run_total_h(…)`,
  `starts_today(…)`, `starts_last_h(…)`. A rule may read its own.

They use the installation's timezone (Settings → General → timezone,
`ui.timezone`). A rule whose expression reads only the clock and counters
is never stale: the clock has no age.

**Timers** (`timing:`), on any kind:

| Field | What it does |
|---|---|
| `for_s` | a change must be wanted this long, besides `debounce` samples: "over 55 °C **for 5 minutes**" |
| `min_on_s` | once started, the target runs at least this long |
| `min_off_s` | once stopped, it stays off at least this long |
| `max_on_s` | after this long on, it stops and rests `min_off_s`, whatever the signal says |
| `max_starts_per_h` | starts in any 60 minutes, at most |

"On" and "off" come from the value the command is asked for:
- off is the rule's `off_value`;
- else a condition's `else`;
- else a steps rule's `normal`.

The on/off timers need one of these. **The emergency path never waits for
a timer:** a stale signal asking for the safe values, a `fast` step, or a
rest goes out at once.

In `rules.yaml` written by hand, write `off_value`. YAML reads a bare
`off`, `on`, `yes` or `no` as true / false, so `off: 0` would silently
become a different key. A rule with such a key is refused with that reason.

**A schedule** is a list of entries:
- `days`: `Mon-Fri`, `Sat,Sun`, `1-5` (1 = Monday), a list `[6, 7]`, or
  `all`;
- `at`: `"HH:MM"`;
- `value` (or `params`);
- an optional `label`, which becomes the rule's state.

The entry in force is the **last one already reached**, looking back up to a
week. Monday 05:00 is still under Sunday's 23:00 entry. With `when`, the
schedule applies only while the expression holds; otherwise the rule asks
for `off_value`.

**Forcing (run for an hour, then back to automatic).**
- How: the card's **Force…**, `POST /api/rules/{id}/force {value, seconds}`,
  or MQTT `mbg/rules/<id>/set {"force": {"value": 30, "seconds": 3600}}`.
- For that long, the rule asks for the value **whatever its signal says**,
  then decides again on its own. `seconds: 0`, an empty answer or
  `{"force": null}` end it early.
- At most 24 h. It survives a restart and is audited.
- The operator's ceiling (clamp) still caps it, and the motor's timers
  (minimum stop, starts per hour) still hold a start.
- On a rule in shadow, it only says what it would do.

The card's **Today** column shows the run time and the starts of the day
(local midnight). In shadow, it shows what the rule would have done. The
counters survive a restart.

**Hysteresis on a condition (`release`).** Without it, `else` follows the
moment `when` turns false: a thermostat at 25 °C would switch at 24.9 and
25.1 all day. With `release`, the rule stays on `then` once `when` held,
until `release` holds; in between it keeps what it has. Make the two
exclusive (`< 25` and `> 35`, never both true at once).

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
   target to its safe value (`on_disable`). Every unit whose device does
   not read back its safe value is sent it — also right after a restart of
   the gateway, and when the device's state is not known yet. A unit that
   another armed rule drives is left to that rule, and a unit that another
   system set during an override is left to it (and an alert says so). Those writes go out in the
   background, one device after another: the page says which units, and
   the Events page says when each one is done (on a slow device such as a
   Fronius Datamanager, about 5 s per unit). With writes off, the page says
   that nothing was sent back to safe.
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

Between `release_below` and the first step (248–250 V here) the rule keeps
the step it is in. **After a gateway restart** it does not know that step
yet: it reads what the inverter holds and resumes the step with that value
(80 % → Warning), until the voltage is under 248 V. With no read-back yet it
writes nothing until the voltage crosses a threshold. (Earlier versions
started from `normal` and lifted a curtailment that a restart caught in the
band.)

Three optional keys change how it behaves around a restart and an urgent
write. All are off unless written; the rule editor has them under
**Advanced: after a restart, urgent sends**.

```yaml
  resume_on_boot: true            # after a restart, start from the step the inverter holds
  timing: { every_s: 2, debounce: 3, min_interval_s: 30, reassert_s: 120,
            boot_wait_s: 60,      # after a restart, wait up to 60 s for the read-back before "normal"
            urgent_retries: 3 }   # an Emergency write that did not get through: again after 5 s, up to 3 times
```

| Key | Range | What it does |
|---|---|---|
| `timing.boot_wait_s` | 0–600 s | After a gateway restart, a `normal` want waits for the inverter's read-back (and asks for it at once) instead of going out blind. If the inverter already holds it, nothing is written. A step or an urgent want never waits. In the dead band, the read-back is asked for at once. |
| `resume_on_boot` | true / false | After a restart, an inverter already at a step's value starts in that step; nothing is written. The voltage then moves it as it would any step. |
| `timing.urgent_retries` | 0–5 | An urgent want (an immediate step, a stale safe or fixed value) that could not be sent — still queued behind the inverter's last command, or failed — is sent again 5 s later instead of after `min_interval_s`. The same want only. |

The rule card lists the ones that are set. While it waits, the rule's
decision reads `after a restart: waiting up to 60 s for the read-back`.

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
  when: "bms.soc >= 98 and umg512.power_active_total < -5000"
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

### 3.5 A heat pump buffer thermostat (condition with a release)

Turn the heat pump on when the water returning from the house is under
25 °C, and off only once it is over 35 °C. The `samsung_mim_b19n` template
ships this rule as a preset (the rule editor offers it under **From the
device template**).

```yaml
- id: hp-buffer
  label: Heat pump · on under 25 °C, off over 35 °C
  target: { device: samsung-dvm, command: power }
  kind: condition
  when: "samsung-dvm.iu0_water_in_temperature < 25"
  release: "samsung-dvm.iu0_water_in_temperature > 35"
  then: { params: { value: 1 } }
  else: { params: { value: 0 } }
  timing: { every_s: 10, debounce: 3, min_interval_s: 900 }   # at most one switch per 15 min
  stale_after_s: 120
  on_stale: hold
```

Two cautions for any compressor:
- **Short cycling wears it.** `min_interval_s` is the shortest time between
  two switches; 15 minutes is a sensible floor. The unit has its own
  restart delay, but that only protects the next few minutes.
- **The unit already regulates while it is on.** It holds its water set
  point with its own thermostat. A rule like this one decides *whether* it
  runs (a buffer, a schedule, away); it should not fight the unit's own
  control.

### 3.6 A drive that soaks up the solar surplus (trim)

A pump or a fan on a frequency drive should use the export and nothing
more. `steps` jumps between fixed speeds. `trim` keeps nudging:
- while the export is over 500 W, it raises the speed by 2 Hz;
- while it is under 100 W, it lowers it;
- in between, it holds;
- under 15 Hz it stops (`off_value: 0`), and it restarts at 15 Hz when the export
  climbs past 500 W again.

```yaml
- id: vfd-surplus
  label: Drive follows the solar surplus
  target: { device: vfd1, command: run_at }   # Delta VFD-EL: 0 Hz = stop
  kind: trim
  signal: "-site-meter.power_active_total"    # export as a positive number
  above: 500                                  # raise while over this
  below: 100                                  # lower while under this; hold in between
  step: 2                                     # Hz per adjustment, at most
  # gain: 0.004                               # optional: 0.004 Hz per W past the threshold, capped at step
  min: 15
  max: 50
  start: 15                                   # where it starts from off
  off_value: 0                                # asked for under min
  timing: { every_s: 5, debounce: 2, min_interval_s: 10, reassert_s: 0 }
  stale_after_s: 60
  on_stale: safe                              # meter gone → stop
  on_disable: safe
```

How it moves:
- **Debounce, by sample.** The signal must stay over (or under) the band for
  `debounce` distinct readings before each adjustment. A one-reading blip
  never moves it.
- **One adjustment at a time.** Each one is sent (or, in shadow, logged)
  and its `min_interval_s` waited out before the next. It never piles up
  several steps while the device is slow to answer.
- **Proportional (`gain`).** The change is gain × the distance past the
  threshold, at most `step`: small corrections near the band, full steps
  far from it. Without `gain`, every adjustment is a full `step`.
- **It starts from what the device holds.** A rule armed on a drive
  already at 30 Hz goes on from 30, not from `start`. After a stale spell
  with `on_stale: safe`, it starts again from the read-back. Arming after a
  time in shadow does the same. After a gateway restart it waits for the
  first read-back (under a minute on a Fronius) instead of starting from
  `min`; a command with no read-back at all starts from `start` or `min`.
- **In shadow, one step from the device.** Nothing a shadow trim decides
  reaches the device, so the signal never answers it. Each would-be step is
  worked out from what the device holds: the card shows *want* one step
  away from *actual*, and stays there while the signal stays outside the
  band. (Earlier versions kept stepping from their own last would-be value,
  down to `min`, and arming sent that.)
- **A group moves together.** On `endpoint` + `group`, the first unit
  decides and every other unit asks for the same value. Units written one
  after another on a slow bus would otherwise step at different moments
  and drift apart.
- **States:** `raising`, `lowering`, `holding`, `at max`, `at min`, `off`.
  An event is logged on a change of state, not on every nudge.
- **The operator's clamp** caps it like any rule.

Pick the band wide enough for one step to fit inside it. A 2 Hz step on a
2.2 kW pump moves roughly a few hundred watts, so a 100–500 W band holds;
a 450–500 W band would hunt.

### 3.7 A pump on a schedule with a daily quota (condition, time, timers)

Run the pool pump at 30 Hz from 08:00 until it has run 6 hours, never at
night. Protect the motor with a minimum run and stop time and at most 4
starts an hour:

```yaml
- id: pool
  label: Pool pump · 6 h a day from 08:00
  target: { device: vfd1, command: run_at }      # Delta VFD-EL: 0 Hz = stop
  kind: condition
  when: 'between("08:00", "20:00") and run_today_h("pool") < 6'
  then: { params: { value: 30 } }
  else: { params: { value: 0 } }                 # also the off value of the timers
  timing: { every_s: 10, debounce: 1, min_interval_s: 0, reassert_s: 120,
            min_on_s: 900, min_off_s: 300, max_starts_per_h: 4 }
  on_stale: safe
```

It reads its own counter: at 14:00 the 6 hours are done and it stops. If the
day was interrupted, it goes on until 20:00 at the latest. Nothing in the
expression reads a device, so it is never stale.

Weekdays only: `weekday() <= 5 and …`. Write a window as the time it may
run in (`between("08:00", "20:00")`); a negation is written as the opposite
comparison, never with `not`.

### 3.8 The same pump on solar surplus, within a window (trim with time)

To follow the surplus only between 10:00 and 16:00, give the trim a signal
that falls under `below` outside the window. It then lowers the speed to
off by itself:

```yaml
- id: pool-solar
  label: Pool pump · solar surplus, 10:00-16:00
  target: { device: vfd1, command: run_at }
  kind: trim
  signal: '-site-meter.power_active_total if between("10:00", "16:00") else 0'
  above: 500
  below: 100
  step: 2
  min: 15
  max: 50
  start: 20
  off_value: 0
  timing: { every_s: 10, debounce: 2, min_interval_s: 20, reassert_s: 0,
            min_on_s: 600, min_off_s: 300, max_starts_per_h: 4 }
  on_stale: safe
```

**On a schedule instead of a quota:**

```yaml
- id: pool-week
  label: Pool pump · by the week
  target: { device: vfd1, command: run_at }
  kind: schedule
  schedule:
    - { days: Mon-Fri, at: "06:00", value: 25 }
    - { days: Mon-Fri, at: "22:00", value: 0 }
    - { days: "Sat,Sun", at: "08:00", value: 20, label: weekend }
    - { days: "Sat,Sun", at: "23:00", value: 0 }
  when: 'boiler.water_temperature < 60'          # optional: only while this holds
  off_value: 0
  timing: { every_s: 10, debounce: 1, min_interval_s: 0, reassert_s: 120,
            min_on_s: 900, min_off_s: 300 }
  on_stale: safe
```

The HMI button "run for 1 h now" is a force on this rule:
`{"force": {"value": 30, "seconds": 3600}}` on `mbg/rules/pool-week/set`.

`pool`, `pool-solar` and `pool-week` can all be armed on `vfd1` once each
has its own layer: see §3.9.

### 3.9 Several rules on one target: layers

Several armed rules may drive one target. A **referee** decides which one
writes.

**Channels: what is arbitrated.** It is not the command name but what the
commands control together, their **channel**:
- commands that write a common register are one channel (the drive's
  `run`, `run_at` and `set_frequency`);
- a template may also name channels.

`GET /api/channels/<device>` lists them, with the rules on each (winner
first) and the motor's state.

**Layers.** Each rule speaks from a layer:

| Layer | Priority | For |
|---|---|---|
| `safety` | 90 | protection: over-voltage, over-temperature, quiet hours |
| `manual` | 70 | a force (a forced rule speaks at least as manual) |
| `schedule` | 50 | schedules and quotas (the default) |
| `optimize` | 30 | following the surplus, tariffs |

`priority: 0-100` sets a rule's own value.

**How the channel combines** comes from its template:
- `priority` (motors) — the highest layer **with an opinion** wins;
- `min` — the lowest value wins: the Fronius power limit, where OV 70 %,
  export 60 % and SCADA 80 % send 60 %;
- `max` — the highest value wins.

**No opinion.** A rule lets the lower ones act while it has nothing to say:
- `else: null` / `then: null` on a condition;
- `normal: null` on steps (a protection that steps in only above its first
  threshold);
- `value: null` on a schedule entry ("from 22:00, nothing").

A steps rule with `normal: {value: 100}` keeps an opinion in normal, as
before.

**The one that loses yields.** It decides and logs as usual, writes nothing,
and its card says *yields to …*. Its want stays pending: the moment it wins
again it is sent, without a new debounce.

**Safety above manual.**
- An outside command (API, MQTT, Home Assistant) on an armed channel still
  needs `override_s`. That pauses every rule of the channel **except** the
  safety ones.
- While a safety rule has an opinion, the command is refused:
  `safety rule '…' holds this channel now`.
- A person on site uses the device's own keypad.

**The channel's own protection.** The template can say what the equipment
must be spared (the Delta drive: `min_off_s: 5`, `max_starts_per_h: 30`).
Every writer obeys it: rules, forces, outside commands. Only the emergency
path skips it (a stale safe, a `fast` step).

A command says what "on" means with `on: "${value} > 0"`. `run_at 0` stops
the motor; `set_frequency` neither starts nor stops it.

Two armed rules with the **same priority** on a `priority` channel are
refused: they could only fight. Give one another layer.

```yaml
- id: pool-quiet                 # safety: never at night
  layer: safety
  target: { device: vfd1, command: run_at }
  kind: condition
  when: 'between("22:00", "07:00")'
  then: { params: { value: 0 } }
  else: null                     # no opinion in the day
  timing: { every_s: 10, debounce: 1, min_interval_s: 0, reassert_s: 120 }
- id: pool-quota                 # schedule: 6 h a day, 30 Hz
  layer: schedule
  target: { device: vfd1, command: run_at }
  kind: condition
  when: 'hour() >= 8 and run_today_h("pool-quota") + run_today_h("pool-solar") < 6'
  then: { params: { value: 30 } }
  else: null
  timing: { every_s: 10, debounce: 1, min_interval_s: 0, reassert_s: 120 }
```

Add `pool-solar` from §3.8 with `layer: optimize`. It drives the pump while
there is surplus and nothing above it has an opinion; the quota and the
quiet hours win over it. An HMI button forces `pool-quota` for an hour,
and the quiet rule still wins at night.

### 3.10 Sequences: commands in order

A **sequence** runs commands one after another, with waits and checks,
possibly across devices. When a step fails or someone cancels, it undoes
what it started, in the order you write. It lives on the Rules page under
the rules, in `config/sequences.yaml`.

```yaml
id: start-pool
label: Open the valve, then start the pump
params:
  speed: { min: 15, max: 50, default: 30, unit: Hz }
max_duration_s: 600                # the whole run, at most
steps:
  - command: { device: valve1, name: open }
  - wait_until: 'valve1.position >= 95'
    timeout_s: 30                  # not open after 30 s → abort
  - command: { device: vfd1, name: run_at, params: { value: '${speed}' } }
  - wait_s: 10
  - check: 'vfd1.output_current > 0.5'   # the motor draws: not dry, belt not broken
on_abort:                          # in this order, every step tried
  - command: { device: vfd1, name: run_at, params: { value: 0 } }
  - command: { device: valve1, name: close }
```

**The steps:**
- `command`: any enabled command of a device, with parameters that may be
  `${param}`;
- `wait_s`: a pause;
- `wait_until` + `timeout_s`: wait for an expression;
- `check`: test an expression now.

`wait_until` and `check` take `else: continue` instead of the default
abort.

**How it runs:**
- **Each command meets every gate** of a command: writes allowed, bounds,
  the check after write, the audit, the channel's protection.
- **It writes from outside the rules.** It pauses the channel's lower rules
  for its run, and a **safety rule holding a device stops it** at that
  step, then undoes.
- **One run per sequence and one sequence per device at a time.** A second
  start is refused in words.
- **States** in words: *running*, *undoing*, *done*, *aborted* (with the
  step and the reason), *cancelled*.
- **A dry run** goes through every command without writing (each one still
  reads what it checks first) and shows the plan.
- A gateway restart in the middle of a run ends it; the steps are in the
  audit log.

**Started from:**
- the card's **Run…** (it asks for the parameters);
- `POST /api/sequences/<id>/run {params, dry_run}`;
- MQTT `mbg/sequences/<id>/run {"params": {...}}` (or `{"cancel": true}`),
  with its state on `mbg/sequences/<id>/state`. This is what an HMI button
  sends.

Running and cancelling are an operator's actions; writing a sequence is
an admin's.

### 3.11 A value from another system

Anything another system publishes over MQTT becomes a signal: add it as an
**MQTT device**, then use it as `device.field`. Its freshness and health
come with it. An example is a setpoint from a home-automation system, read by
a device `ha` with a row `export_limit` on topic `home/limits/export`:

```yaml
  when: "umg512.power_active_total < -ha.export_limit"
```

### 3.12 An export cap on a group of inverters (trim)

The grid connection allows at most 45 kW of export. The rule lowers the
inverters' power limit while the export is over the cap, and raises it
back once there is room.

```yaml
- id: export-cap
  label: Export cap 45 kW
  target: { endpoint: pv, group: inverters, command: power_limit }
  kind: trim
  # the room under the cap, in W; this meter reads export as negative:
  #   exporting 40 kW → 45000 − 40000 =  5000 (room)
  #   exporting 47 kW → 45000 − 47000 = −2000 (over)
  signal: "45000 + umg512.power_active_total"
  signal_unit: W under the cap
  below: 0          # over the cap: lower the limit
  above: 3000       # 3 kW of room: raise it back
  step: 10          # % per adjustment, at most
  gain: 0.002       # % per W past the threshold: 5 kW over → 10 %
  min: 0
  max: 100
  params: { revert_s: 300 }   # the inverter's own timer: back to 100 % if the gateway stops
  timing: { every_s: 5, debounce: 2, min_interval_s: 15, reassert_s: 120 }
  stale_after_s: 30
  on_stale: hold
  on_disable: safe  # disarmed: back to 100 %
```

- **Check the sign first.** Read the meter while exporting: here the
  export is negative, so the room is `cap + power`. A meter that counts
  export as positive needs `cap − power`.
- **`revert_s` and `reassert_s` together.** The inverter drops the limit by
  itself `revert_s` after the last write; `reassert_s` writes it again
  before that. If the gateway stops, the inverters are back at 100 % within
  5 minutes. With `revert_s: 0` the limit stays until something writes
  100 %.
- **Mind the bus.** A Fronius Datamanager takes ~5 s per write and
  collapses above ~26 transactions a minute: four units every 15 s is
  about 16 writes a minute.
- **With the over-voltage rule on the same inverters,** the template's
  channel `power` combines by `min`: the lower limit is the one written.
- **Another controller on the same export** (a Victron ESS holding a grid
  setpoint, an EMS) must not chase the same meter. One that follows the
  inverters' production is fine; one that holds the grid power constant
  would make up for every cut from its battery.

Tested on a 4-inverter plant on 2026-10-20 with a 20 kW cap, armed for
7 minutes, export 10–46 kW with clouds: mean 18.0 kW, 95th percentile
20.4 kW, the highest 21.2 kW (15 s over 20.5 kW), 33 commands, all
answered. That run found the shadow-to-armed and group fixes above.

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
| result **sent**, then **queued** | the device has not answered the last command yet; the next one waits for it (in order, never piled up) | normal for a slow link; if it stays, the device or its bus is away — after 3 failures an alert says so |
| alert *the rules engine has not run* | the rules thread stopped (the watchdog checks every 10 s) | look at the log; restart the gateway |

Every decision is also an InfluxDB point (`rule_event`) and an MQTT state
(`mbg/rules/<id>/state`), so you can chart a rule next to the values it
watches.
