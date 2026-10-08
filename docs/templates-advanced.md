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
| [Display](#display) | how a unit of this kind is shown | Advanced → **Display** |
| [Scan recognition](#scan-recognition-identify) | how a bus scan knows this device | Advanced → **Scan recognition** |
| [Details and categories](#details-and-categories) | version, author, source; how the rows are grouped | Advanced → **Details & categories** |
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
| Rates | `prev(x)` is x's previous value; `dt` is the seconds since the last computation |
| Constants | `pi`, `e`, `true`, `false` |

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

The full design is in [commands-design.md](commands-design.md). How
commands are enabled per device, and who may run them, is in
[MANUAL §14b](MANUAL.md#14b-commands--named-writes-from-the-template).

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
| **Alarms** | a field that is non-zero when something is wrong; *warning* or *danger*; counted on the fleet and the installation, listed on the unit | 50 |
| **Sections** | a category shown as a **grid of tiles** (cell voltages, temperatures) or **only its active rows** (alarm bits); tiles are a pattern of names (`^cell_\d+$`) with their decimals | — |
| **Bitmasks** | a field whose bits each mean one thing, shown as the list of what is set; the label carries `{n}`: "Cell {n}" | — |

**Example**, a battery pack:

```text
unit label   battery pack / battery packs · icon battery-half
unit row     soc, power, max_cell_temp, status
fleet row    soc, power
headline     pack_total_power "Battery power" (+ = charging) · pack_average_soc "SOC"
alarms       alarm_count → warning · protection_count → danger
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
