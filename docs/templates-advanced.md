# Templates, advanced: everything beyond the register rows

A template's register rows say *what the device has*. The **Advanced**
section of the template editor holds everything else a template can carry.
Each part has a tab, or a dialog per row:

| Part | What it is for | In the editor |
|---|---|---|
| [Calculated fields](#calculated-fields) | values the device does not report, computed from the ones it does | Advanced → **Calculated** |
| [Row details](#row-details) | reading a row right: not-available markers, counters, scale factors, Home Assistant typing | the **⚙ sliders** button on a row |
| [Totals of an installation](#totals-of-an-installation) | how several units of this template add up | in a row's details, and on a calculated field |
| [Commands](#commands) | named, guarded writes: "power_limit = 60 %" | Advanced → **Commands** |
| [Channels](#channels-what-commands-control-together) | which commands control the same setting, and how often it may be written | Advanced → **Raw JSON** (`channels`) |
| [Display](#display) | how a unit of this kind is shown | Advanced → **Display** |
| [Scan recognition](#scan-recognition-identify) | how a bus scan knows this device | Advanced → **Scan recognition** |
| [Setup at connect](#setup-at-connect) | words written to the device before part of its map exists | Advanced → **Setup at connect** |
| [Rule examples](#rule-examples) | rules a device of this kind is commonly run with, offered in the rule editor | Advanced → **Rule examples** |
| [Notes for users](#notes-for-users) | what a user should know before relying on the template | Advanced → **Notes for users** |
| [Details and categories](#details-and-categories) | version, author, source; how the rows are grouped | Advanced → **Details & categories** |
| [Components](#components) | the parts a device holds (a GX's batteries and sensors), each its own device | Advanced → **Raw JSON** (`components`) |
| [Raw JSON](#raw-json) | the whole template as text | Advanced → **Raw JSON** |

How the editor behaves:
- Every field edits the template in the editor **at once**. Nothing is
  saved until **Save**.
- A moment after each change the gateway **checks the whole template**. The
  box under the tabs says *No problems*, or lists exactly what a save would
  refuse.
- **Built-in templates** cannot be changed. **Duplicate** one, then edit the
  copy.

Start with [device-templates.md](device-templates.md) if you have not made a
template yet. It covers addresses, word order, scale, and the register rows.

---

## Calculated fields

**What for.** Some values a device does not report directly:
- total power, when it reports only the three phases;
- a power factor;
- a status code turned into words;
- an alarm count.

A calculated field computes such a value from the device's own registers.
It lives in the template, so **every device made from the template gets
it**. It is published to MQTT, written to InfluxDB and shown on the
dashboard like any register.

**How it works.**
- **Inputs.** A formula reads the registers of the same device **by their
  name**, and the template's other calculated fields.
- **When.** It is computed each time its inputs are read. It can also run
  on a poll group of its own.
- **Missing inputs.** When an input is missing (the device did not answer,
  or the value is not-available), the result is missing too. It is never
  zero.

**The formula language:**

| | |
|---|---|
| Arithmetic | `+ - * / % ** //`, parentheses |
| Comparison and logic | `> < >= <= == !=`, `and`, `or` |
| Choice | `a if condition else b` |
| Functions | `min`, `max`, `avg`, `abs`, `round(x, n)`, `sqrt`, `pow`, `floor`, `ceil`, `clamp(x, lo, hi)`, `popcount(x, mask)` |
| Time (the installation's timezone) | `hour()`, `minute()`, `weekday()` (1 = Monday), `day()`, `month()`, `between("10:00", "16:00")` |
| Rates | `prev(x)` is x's previous value; `dt` is the seconds since the last computation |
| Fallback | `first(a, b, …)` is the first that has a value: `first(power_electric, power_electric_estimated)` takes a linked meter when there is one, else an estimate |
| Freshness | `fresh(x)`, `count_fresh(a, b, …)`, `age(x)`, `absent()`; see below |
| Steps | `steps(x, below, t1, v1, t2, v2, …)`; see below |
| Smoothing and thresholds | `ema(x, seconds)`, `hysteresis(x, low, high)`; see below |
| Installations | `battery-bank.pack_average_soc`: an installation's totals read like a device's values |
| The sun | `sun_elevation("pv")`, `sun_azimuth("pv")`, `is_day(5, "pv")`; see [attributes.md](attributes.md#the-sun-in-formulas-and-rules) |
| Attributes | `attr("umg512", "grid_connection", "max_export_kw")`; see [attributes.md](attributes.md#attributes-in-formulas) |
| Constants | `pi`, `e`, `true`, `false` |

**Is the value fresh? Steps instead of nested choices.** Five functions say
what a formula knows about its inputs, and one is a step function:

| Function | What it gives | Example |
|---|---|---|
| `fresh(x)` | 1 when every value `x` reads is there and fresh, else 0 | `pv-u1.power_active_total if fresh(pv-u1.power_active_total) else 0` |
| `count_fresh(a, b, …)` | how many of its arguments are fresh | inverters answering: `count_fresh(pv-u1.power_active_total, pv-u2.power_active_total, pv-u3.power_active_total, pv-u4.power_active_total)` |
| `absent()` | no value this round (nothing is published) | `(a + b) if count_fresh(a, b) == 2 else absent()` |
| `age(x)` | seconds since the oldest value `x` reads was measured; for a calculated value, since its oldest input | `age(house_load)`: alert above 30 |
| `steps(x, below, t1, v1, t2, v2, …)` | `below` under `t1`, `v1` from `t1`, `v2` from `t2`… (the thresholds rise) | `steps(soc, 0, 15, 30, 25, 50, 40, 75, 60, 100)` → 0 % under 15, 30 % from 15, 50 % from 25, 75 % from 40, 100 % from 60 |
| `ema(x, seconds)` | `x` smoothed: after `seconds`, a step has gone 63 % of the way; the first value is `x` itself | the house load without its spikes: `ema(house_load, 300)` |
| `hysteresis(x, low, high)` | 1 from `high` up, 0 from `low` down, and between them what it was (0 at the start) | the cabinet is hot: `hysteresis(ruuvi-cabinet.ambient_temp, 35, 40)` turns on at 40 °C and off only under 35 |

- **`ema()` and `hysteresis()` remember** between two computations, one
  memory per call in the formula (two calls do not share one). A gateway
  restart, a change of the formula itself, or deleting the device forgets:
  `ema()` starts again at the value, `hysteresis()` at 0. Editing or
  restarting the device keeps the memory of a formula that did not change.
  `prev()` and `dt` start again when what the formula reads through changes
  (the device's map or units, a device it names, its meters), so a rate
  never mixes two meters. The live preview answers as a first computation.
- **An installation's totals** are read by its id, like a device's values:
  `battery-bank.pack_average_soc`, `pv.power_active_total` (its first
  group). They are what the totals publisher last computed, fresh like
  them; a standby gateway computes none.
- **Fresh** means within the value's own bound: three times its poll group's
  interval, at least 60 s.
- A stale value that `first()`, `fresh()` or `count_fresh()` skips does not
  mark the result as old. A formula that leaves a dead input aside stays as
  fresh as the inputs it used.
- **An honest site value.** Leave it out when an inverter that should
  answer does not, but count the whole farm as 0 at night, when all of them
  sleep:

  ```text
  pv_total = (pv-u1.power_active_total + pv-u2.power_active_total + pv-u3.power_active_total + pv-u4.power_active_total)
               if count_fresh(pv-u1.power_active_total, pv-u2.power_active_total, pv-u3.power_active_total, pv-u4.power_active_total) == 4
               else (0 if count_fresh(pv-u1.power_active_total, pv-u2.power_active_total, pv-u3.power_active_total, pv-u4.power_active_total) == 0
                     else absent())
  ```

  A formula may span several lines; it is read as one.
- **The age next to the value**, for a consumer that must not act on old
  data: a second calculated field, `house_load_age = age(house_load)`.

**What a copy/paste brings is refused, with the reason.** A formula holds
plain ASCII only. On save, and under the field as you type, the gateway
names any other character, where it is, and what to type instead:
- a typographic minus `−` → type `-`;
- a no-break space → a normal space;
- curly quotes → `"`;
- `×` → `*`, `≤` → `<=`;
- an invisible character (a zero-width space, a direction mark) → delete
  it;
- a letter from another alphabet that looks Latin, or a diacritic → it would
  name a value that does not exist.

MQTT topics get the same check for invisible characters, look-alike spaces
and a space at either end; their text otherwise stays free (a zigbee2mqtt
name may have spaces and diacritics).

**In the editor:** Advanced → Calculated → **Add a calculated field**.
1. Give it a **name** (letters, digits, `_`; not the name of a register) and
   a **unit**.
2. Build the **expression** from the chips: the template's registers, the
   functions and the operators. You can also type it. Under the field the
   editor checks the formula against the template you are editing. It names
   a misspelt register (*not a register or calculated field of this
   template: powr_l2*) and a formula that reads itself.
3. Under **More**, optionally:
   - **Text for codes:** one `code = text` per line.
   - **MQTT topic** and **InfluxDB measurement**, if not the name.
   - Whether it goes to MQTT, to InfluxDB, and to the dashboard.
   - **Totals of an installation**.

**Examples:**

```text
power_active_total    power_l1 + power_l2 + power_l3                  W
power_factor          power_active_total / power_apparent_total
battery_state         1 if current > 0.5 else (2 if current < -0.5 else 0)
                      Text for codes:  0 = Idle · 1 = Charging · 2 = Discharging
power_from_energy     (energy_active_import - prev(energy_active_import)) / dt * 3600   W
cells_balancing       popcount(balancing_bits)
```

The last-but-one turns a Wh counter into average power between two reads.
Put it on a slow poll group, so the interval is long enough to be
meaningful.

> Reading **another device** (`meter.power_active_total`) is possible, but
> every device made from the template would then read that same device.
> The editor warns about it. Calculated fields on a single device (Devices →
> the device → **Calculated**) are the place for cross-device formulas.

---

> **Fields of a linked meter.** A formula may read `power_electric` (W)
> and `energy_electric`, which a device gets when another device measures
> its electricity ([MANUAL §5.4](MANUAL.md#54-electricity-measured-by-other-devices--meters-with-roles)).
> Without a link the formula is not computed. The MIM-B19N map uses it for
> `cop_estimated`.

## Row details

The **sliders** button at the end of a row opens everything a row can carry
beyond its address, type and scale. The button is highlighted when
something is set. Everything here is optional.

| Field | What it does | When you need it |
|---|---|---|
| **Description** | shown where the row is picked | always helps |
| **Access** | *read-only* or *read / write* (informative) | — writing is allowed by **Wr** on the row |
| **Scale factor from** | another register holds a power of ten; value × 10^SF | SunSpec devices: `W` with `W_SF` |
| **Means "not available"** | a raw value that means "no reading": `true` for the type's standard marker (0x8000, 0xFFFF, 0x80000000…), or the value(s) | SunSpec, BMS that report 0xFFFF for an absent sensor |
| **Counter that only grows** | ignores a reading that goes backwards | energy counters (Wh, kWh, varh) |
| **Day counter** | keeps the day's maximum and adopts only the midnight reset | "energy today" computed by the device |
| **Only these values may be written** | a writable row accepts only these | a mode register: 0, 1, 2 |
| **Totals of an installation** | see [below](#totals-of-an-installation) | installations of several units |
| **Home Assistant** | `device_class`, `state_class`, `entity_category`, shown by default, icon, decimals | when HA guesses wrong from the unit |

**Why "counter that only grows" matters.** An energy counter that reads 0
once (a glitch, a bad frame) looks to Home Assistant, InfluxDB and Victron
like a counter that was reset. Your daily totals then jump by the whole
lifetime energy. With the flag, the gateway drops a reading that goes down.
A real reset is adopted only when several coherent readings confirm it.

**Why "not available" matters.** SunSpec reports a missing value as 0x8000
(int16) or 0xFFFF (uint16). Without the marker, the gateway would publish
-32768 W or 65535 V. With it, the value is missing instead.

---

## Totals of an installation

When several units of the same template form one **installation** (eight
battery packs, four inverters behind a datalogger), the installation
publishes **totals**. The template teaches it how each field combines.

**How it works.** Each row, or calculated field, may declare one or more
totals: **a name**, and **how** the units' values combine.

| How | Result |
|---|---|
| sum | the units added up: power, current, energy |
| average | the mean: SOC, voltage, temperature |
| minimum / maximum | the lowest / highest: weakest cell, hottest pack |
| spread (max − min) | how far apart the units are: SOC balance |
| most common | the value most units report: a status |

**Example**, a battery pack template:

```text
row soc            pack_average_soc = average · pack_min_soc = minimum · pack_max_soc = maximum
                   pack_soc_spread = spread
row current        pack_total_current = sum
row cell_max_v     pack_max_cell_voltage = maximum
calculated power   pack_total_power = sum
```

The installation then publishes `pack_average_soc`, `pack_min_soc`… under
its own topic. These totals are what the [Display](#display) headline
shows.

In the editor: a row's **sliders** → *In an installation* → **Add a total**.
For a calculated field: **More** → *Totals of an installation*.

---

## Commands

**What for.** A **command** is a write with a name and rules: *"set the
power limit to 60 %"*. The caller does not need to know the registers. It
can be a controller, a rule, Home Assistant, the UI or the API. The template
says how it is done:
- which registers, in what order;
- what must be true before writing;
- how to check that it worked;
- what to restore when a temporary command expires.

**How it works**, in order:
1. **Parameters** are checked: required ones are present, values are within
   min/max, allowed values only. A value outside the bounds is refused
   before anything is written.
2. **Guard.** Registers are read. If one does not hold (a wrong model code,
   a scale factor outside what the formula expects), **nothing is written**
   and the answer is *rejected*.
3. **Writes** go out in order. Consecutive registers travel in one frame.
4. After the **settle time**, the **verify** registers are read back:
   - equal within tolerance: *success*;
   - different: *mismatch*;
   - not readable: *unverified*.
5. The **re-read group** is polled at once, so MQTT and Home Assistant show
   the new value within seconds.
6. A command sent with a **lease** (a timeout) is restored with its **safe**
   values when the lease expires. A power limit goes back to 100 % if the
   controller stops renewing it.

**Values in writes and checks:**

| Write | Meaning |
|---|---|
| `60` | the number |
| `${value}` | the parameter `value` |
| `{"*": ["${value}", 10]}` | value × 10 (also `/`, `+`, `-`) |
| `{"if": "${value} < 100", "then": 1, "else": 0}` | a choice |

**In the editor:** Advanced → Commands → **Add a command**.
1. **Name** (`power_limit`, which becomes an MQTT topic and a Home Assistant
   entity), **label**, and whether the UI asks for confirmation.
2. **Parameters.** The main one is called `value`; Home Assistant shows it
   as a number. Give it min/max, a default, *required*.
3. **First check (guard)**: a register and one condition per line: `=`, one
   of a list, `≥ min`, `≤ max`.
4. **Writes, in order**: a register (from the template) and a value.
5. **Verify**: a register, the expected value, a tolerance.
6. **Settle time**, **re-read group**, **safe values**.
7. **Write again every (s)**, `refresh_s`: for a setting the device forgets
   by itself. A Samsung heat pump drops a compressor-frequency limit after
   one hour. After a run, the gateway writes the same parameters again every
   `refresh_s`, until the command is run with its **safe** values or its
   lease expires. The command list shows `refreshing: true` meanwhile. A
   refresh needs safe values: they are what stops it.

A **shortcut** command runs another command with fixed parameters. For
example, *restore* = power_limit with value 100. Tick *A shortcut for another
command*.

**Example**, a SunSpec inverter's power limit:

```text
power_limit   "Active power limit"
  params   value      0 … 100 %, required
           revert_s   default 600 s
  guard    controls_model_id = 123
           wmaxlimpct_sf one of -2, -1, 0
  writes   power_limit_pct      = ${value}
           power_limit_revert_s = ${revert_s}
           power_limit_enabled  = {"if": "${value} < 100", "then": 1, "else": 0}
  verify   power_limit_pct = ${value} (± 1)
  safe     value = 100, revert_s = 0
  re-read  controls

restore      shortcut for power_limit {value: 100, revert_s: 0}
```

The checks on save:
- every register a command reads or writes exists in the template;
- every value evaluates with the parameters' defaults;
- min ≤ max, and the default lies within them;
- safe values name real parameters;
- the re-read group exists.

### Commands over MQTT

A device read over MQTT (a Victron GX, Tasmota, zigbee2mqtt) takes the same
commands. The difference is in how a row is written and read back:

- **Write topic** (`write_topic`, the row's column or details): where the
  new value is published. `{vars}` and `~/` work as in `topic`.
- **Payload** (`write_payload`, the row's details):
  - `${value}` is the number, or an enum's code;
  - `${label}` is an enum's text;
  - empty means the bare number; a text without either is sent as it is
    (Tasmota `Restart`).

  A Victron GX takes `{"value": ${value}}`.
- **The read-back** is what arrives on the row's `topic` after the publish.
  The **settle time** is the longest wait for it, not a fixed pause:
  - *success* as soon as it matches;
  - *mismatch* at the end if what came differs;
  - *unverified* if nothing came.
- **Already set:** when the device already holds the value and every check
  reads a written row, nothing is published. A setting kept in flash is
  spared the write.
- **Guards** read the rows' last values. A source that has gone quiet (or
  says it is offline) runs nothing.
- **A command is published once, QoS 0, never retained.** The read-back says
  whether it arrived. A queued message could go out minutes later, after a
  reconnect.

A write topic that the device's own subscription would hear is refused, as
is one under the gateway's own command topics. The gateway would read its
own command as the answer.

```text
ess_setpoint   "Grid setpoint"            (victron_gx_mqtt)
  params   value  -50000 … 50000 W, required
  guard    ess_mode one of 1, 2           (in mode 3 the GX ignores the setpoint)
  writes   ess_setpoint = ${value}        → W/{portal}/settings/0/Settings/CGwacs/AcPowerSetPoint
                                            payload {"value": ${value}}
  verify   ess_setpoint = ${value} (± 0.5), within 5 s
  channel  ess_setpoint, min_interval_s 60 (a setting in flash)
```

On save, an MQTT template's command must write rows that have a write topic.

The full design is in [commands-design.md](commands-design.md). How
commands are enabled per device, and who may run them, is in
[MANUAL §14b](MANUAL.md#14b-commands--named-writes-from-the-template).

---

### Channels: what commands control together

Several rules may drive one device ([rules.md §3.9](rules.md#39-several-rules-on-one-target-layers)).
What they compete for is a **channel**: commands that write a common
register are one. `"channel": "motor"` on a command joins commands that
are coupled without sharing a register (a mode and a set point).

The template says how a channel is shared and what the equipment must be
spared, whoever writes:

```json
"channels": {
  "motor": { "label": "Motor", "combine": "priority", "min_on_s": 60,
             "min_off_s": 5, "max_starts_per_h": 30, "min_interval_s": 0 }
}
```

- `combine`: `priority` (the highest layer wins), `min` (the lowest value
  wins — limits), or `max`.
- A command says when it turns the channel **on**, as a condition on its
  parameters: `"on": "${value} > 0"`. A command without `on` (a set point)
  neither starts nor stops it.

---

## Display

**What for.** How a unit of this kind is **shown**: in its installation, on
the fleet page, on its own page. The display is read live from the
template. Improving it reaches every existing unit at once, with nothing to
re-apply. Without it, the UI uses sensible defaults. Declare only what you
want different.

| Field | What it changes | Limit |
|---|---|---|
| **A unit is a… / several are…** | the words: "battery pack" / "battery packs" | 64 characters |
| **Icon** | a Bootstrap icon name without `bi-`: `battery-half`, `lightning`, `sun` | — |
| **Fields of a unit row** | the columns of a unit's row in its installation and the device list | 12 |
| **Fields on the fleet page** | its row on the fleet page (the first three show) | 12 |
| **Headline of an installation** | the installation's top line, from its **totals**, with a label and a hint ("+ = charging") | 6 |
| **Alarms** | a field that is non-zero when something is wrong; *warning* or *danger*; counted on the fleet and the installation, listed on the unit, and sent as an **alert** when it turns active (optional `for_s`: only once it has stayed active that long) | 50 |
| **Sections** | a category shown as a **grid of tiles** (cell voltages, temperatures) or **only its active rows** (alarm bits); tiles are a pattern of names (`^cell_\d+$`) with their decimals | — |
| **Bitmasks** | a field whose bits each mean one thing, shown as the list of what is set; the label carries `{n}`: "Cell {n}" | — |

**Example**, a battery pack:

```text
unit label   battery pack / battery packs · icon battery-half
unit row     soc, power, max_cell_temp, status
fleet row    soc, power
headline     pack_total_power "Battery power" (+ = charging) · pack_average_soc "SOC"
alarms       alarm_count → warning · protection_count → danger
             (a heat pump: error codes → danger; a calculated
              "ΔT over 8 K with the compressor running" → warning, for_s 1200)
sections     cells: grid of tiles, ^cell_\d+$, 3 decimals
             alarms: only active rows
bitmasks     balancing_bits → "Cell {n}"
```

---

## Scan recognition (identify)

**What for.** **Scan** on a bus ([rtu-over-network.md §5](rtu-over-network.md#5-finding-the-slaves-watching-the-bus-moving-things))
asks every unit ID who answers. When a slave matches this template's
recognition, the scan proposes the template, and **Add** opens the wizard
with it selected.

**How it works.** All the declared checks must hold:
- **Registers that must read** (up to 8): an address, its table (holding or
  input), its type, and a condition (`=`, one of a list, `≥ min`, `≤ max`).
  Use what identifies the model: a model code, a fixed marker, a
  manufacturer ID.
- **FC43**: what the device says about itself (Modbus device identification),
  as patterns (regular expressions) on the vendor and product names.

**In the editor:** Advanced → Scan recognition. Add the registers and/or the
FC43 patterns. Then **Try it on a device**: pick a running device and press
**Test**. The gateway asks it, through its own connection, and says whether
it is recognised.

**Examples:**

```text
Fronius Smart Meter 65A      register 11 (holding, uint16) one of 731
SunSpec device               register 40000 (holding, uint32) = 1400204883   ("SunS")
Eastron SDM630               FC43 vendor ^Eastron · product SDM630
a meter family               register 28 (input, uint16) ≥ 100 · ≤ 199
```

Choose values that **only this model** has. A register that reads 0 on
every device recognises everything.

---

## Setup at connect

**What for.** Some devices show part of their map only after being told
which values to expose, over the same bus. A Samsung MIM-B19N, for one,
reports the compressor frequency, the pressures or the water flow only once
the matching NASA message ids are written to its addresses 6000+ (outdoor)
and 7000+ (hydro unit). Until then those registers answer *illegal data
address*. Nothing guarantees the module keeps that setup over a power cut.

**How it works.**
- Each step is an address and a list of 16-bit words. The words are written
  as one write-multiple frame (FC16), in order.
- The gateway writes the steps when it first talks to the device. It writes
  them again after the device comes back from being unreachable, since it
  may have rebooted.
- It also writes them again when a register a step **provides** is refused
  as an illegal address: the device forgot.
- Runs are spaced: at least a minute after a failed run, two after a good
  one. A device that refuses for another reason is not hammered.
- It writes only where writes are allowed (Settings → Security → allow
  writes, and the device not write-locked). Otherwise the device's log says
  why.
- The rows of the registers a step provides are declared like any other row
  of the map.

**In the editor:** Advanced → Setup at connect → **Add a setup step**:
- a name, the address, the values (decimal or `0x8238`, comma or line
  separated);
- the range of registers it provides.

The device's statistics show whether the setup is in place, how many times
it ran and the last error.

**Example**, the Samsung MIM-B19N map shipped with the gateway
(`samsung_mim_b19n`):

```text
outdoor_messages   address 6000   values 0x8001, 0x8010, 0x8204, 0x8206, …   provides 4…22
indoor_messages    address 7000   values 0x4067, 0x406C, 0x4087, 0x40C4, …   provides 82…96
→ register 6 = outdoor temperature (0x8204), register 86 = water flow (0x42E9)…
```

> A setup step writes to the device. Declare only what its documentation
> asks for. On the MIM-B19N, the 6000/7000 words configure the interface
> module itself; they do not command the heat pump.

---

## Rule examples

**What for.** Some devices are almost always run with the same few rules: a
heat pump with a buffer thermostat, an inverter with an over-voltage
derating. The template can ship them as **examples**. The rule editor
offers them under **Start from** for every device made from the template,
with `{device}` replaced by that device's id. The user adjusts the
thresholds and saves. Nothing is created by itself, and a rule always
starts in **shadow** (it decides and logs; it writes nothing until it is
armed).

**Each example** is a rule exactly as in the [rules guide](rules.md), plus:
- `id`: short (`buffer`); the rule's id becomes `<device>-buffer`;
- `description` (optional): shown in the editor when the example is picked;
- `target.device` is always `"{device}"`; `target.command` must be a
  command of this template;
- every field it reads is written `{device}.<field>` and must be a row or
  a calculated field of this template.

A save checks each example as the rule editor would. At most 20.

**In the editor:** Advanced → Rule examples: the list as JSON, **Apply to
the editor**, then the usual check and Save.

**Example**, from the Samsung MIM-B19N map (`samsung_mim_b19n`), a buffer
thermostat with hysteresis (`release`):

```json
{"id": "buffer", "label": "Heat pump · on under 25 °C water in, off over 35 °C",
 "kind": "condition", "target": {"device": "{device}", "command": "power"},
 "when": "{device}.iu0_water_in_temperature < 25",
 "release": "{device}.iu0_water_in_temperature > 35",
 "then": {"params": {"value": 1}}, "else": {"params": {"value": 0}},
 "timing": {"every_s": 10, "debounce": 3, "min_interval_s": 900},
 "stale_after_s": 120, "on_stale": "hold", "on_disable": "hold"}
```

The same template also ships `discharge-guard`, a `steps` rule that lowers
the water set point while the compressor discharge runs hot.

> An example reads only its own device's fields. A rule that also needs
> another device (a grid meter for PV-surplus heating) is written in the
> rule editor, where any device's fields can be used.

---

## Notes for users

**What for.** Some things about a template are true only for some models,
or only after a step the user must take first. Examples: a value that reads
0 on one variant, a command the unit accepts but ignores, a sensor that
must be fitted. A note says it where the user will see it, instead of in a
manual nobody opens.

**Where it shows.**
- In the add-device wizard, under the template list once the template is
  picked. A template with a warning carries a ⚠ next to its name.
- At the top of every device made from the template.
- On the command a warning names: the Commands tab marks it, and the mark's
  tooltip is the note.

**Each note has:**
- a **level**: `info` or `warning`;
- the **text** (up to 1000 characters);
- optionally, the **fields or commands** it is about. A save refuses a name
  the template does not have;
- optionally, a **link** (http/https) to more detail.

At most 20 notes per template.

**In the editor:** Advanced → Notes for users → **Add a note**.

**Example**, from the Samsung MIM-B19N map (`samsung_mim_b19n`):

```json
"notes": [
  {"level": "warning",
   "text": "Quiet mode and the compressor limit (FR control) have no effect on an HE hydro kit: the write is accepted and nothing changes.",
   "fields": ["quiet", "compressor_limit"]},
  {"level": "info",
   "text": "The values at 4-22 and 82-96 appear only after the setup steps are written."}
]
```

> Write what you **measured**, and on which model. "Reads 0 on the HE kit,
> field-checked 2026-10-08" helps the next user. "May not work" does not.

---

## Details and categories

| Field | Why |
|---|---|
| **Version** | raise it when you change the template; devices made from it are offered the update ([device-templates.md §7](device-templates.md#7-changing-a-template-later)) |
| **Author**, **Source document** | who made it, from which manual and page, for the next person |
| **Description** | shown in the template library |
| **Categories** | the sections of a device's page and their order: an id (named on each row), a label and an order |

Renaming a category renames it on its rows. A category still used by a row
cannot be deleted.

---

## Components

A device that holds a varying number of things declares them as
**components**: a Victron GX holds batteries and temperature sensors.

- **One kind, any number of times:** a component is written once in the
  template and added to a device as many times as the device holds it, each
  time with its *instance*.
- **Each instance is a device:** it has its own values, history, rules and
  attributes.
- **No connection of its own:** it reads through its parent's.

```json
"components": {
  "battery": {
    "name": "Battery",
    "kind": "battery",
    "description": "A battery monitor or a BMS on the GX's CAN.",
    "instance": {"label": "Battery instance", "hint": "Settings → Services on the GX, e.g. 288"},
    "discover": {"topic": "N/{portal}/battery/+/ProductName",
                 "name_topic": "N/{portal}/battery/{instance}/CustomName"},
    "categories": {},
    "display": {},
    "registers": [
      {"address": 1, "name": "soc", "unit": "%", "data_type": "float",
       "topic": "N/{portal}/battery/{instance}/Soc", "json_path": "value"}
    ],
    "calculated": []
  }
}
```

### Rules

- **Every component topic contains `{instance}`.** It is filled with the
  instance the component was added with. Without it, every instance would
  read the same value, so a save refuses a row without it.
- **The parent's other topic variables apply too.** `{portal}` above is the
  parent's.
- **A component inherits the parent's `protocol`** (transport, keepalive,
  availability), its vendor and its version.
- **A component may carry:** `registers`, `calculated`, `categories`,
  `display`, `notes`, `poll_groups` and `kind`. Components are read-only
  for now, so they carry no commands, channels, rule examples, setup or
  identify.
- **`discover`** is optional. Its `topic` has exactly one `+` level, and that
  level is the instance. *Find on the device* listens there and on
  `name_topic`, and shows what it heard; the payload is the product.
- **Components need an MQTT template** for now.

Each component also becomes a template of its own, `<template>:<type>`, for
example `victron_gx_mqtt:battery`. This is what its devices name. It is
never offered by itself, and it is edited only through its parent. In the
editor, components are edited in the raw JSON below.

---

## Raw JSON

The whole template as JSON, editable in place:
- **Check** shows what a save would refuse, without touching the editor.
- **Apply to the editor** puts the JSON into the editor. The id of a saved
  template stays fixed. Nothing is saved until **Save**.
- **Reload from the editor** brings the JSON back in line after edits
  elsewhere.

Use it for what is quicker typed than clicked: pasting a block from a
colleague, or replacing many rows at once. Everything in it is also editable
through the tabs above. The format, field by field, is in
[config-reference.md](config-reference.md#template-only-extras).

---

## What a save checks

A save refuses, naming the row or block and the reason:
- two rows with one address **in one table** (coil 0 and holding 0 are two
  registers);
- an unknown data type, a zero scale, a write minimum above its maximum;
- a calculated field whose formula does not parse, or that shadows a register
  name;
- a total with an unknown way to combine (`total` instead of `sum`);
- a not-available marker that is not `true`, a number or a list of numbers;
- Home Assistant fields with values HA does not know (`state_class: gauge`);
- a command that writes or reads a register the template does not have, an
  expression that does not evaluate, bounds that contradict each other;
- display fields that do not exist, too many glance fields, a tiles pattern
  that does not compile;
- a recognition register without a condition.

A template file already on disk that breaks one of the checks added in 3.91
still **loads**: an upgrade never stops a device. The log names the problem,
and the next save from the editor asks for it to be fixed.
