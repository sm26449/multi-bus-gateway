# Writing a device template

A **device template** is the map of one kind of equipment: which values it
has, where each one sits, how to decode it and what to call it. Write it once
from the manufacturer's register list, and every device of that kind reads
the same way: same names, same MQTT topics, same InfluxDB fields.

This guide takes you from a page of the manual to live, correct values. It
covers Modbus TCP/RTU, HTTP/JSON and MQTT. You need no JSON unless you want
the advanced extras at the end.

- [1. What you need from the manual](#1-what-you-need-from-the-manual)
- [2. Three ideas that make or break a map](#2-three-ideas-that-make-or-break-a-map)
- [3. Three ways to write it](#3-three-ways-to-write-it)
- [4. Example: a Modbus meter from its manual (CSV)](#4-example-a-modbus-meter-from-its-manual-csv)
- [5. Example: HTTP/JSON and MQTT devices](#5-example-httpjson-and-mqtt-devices)
- [6. Test it on the real device](#6-test-it-on-the-real-device)
- [7. Changing a template later](#7-changing-a-template-later)
- [8. Sharing a template](#8-sharing-a-template)
- [9. Advanced: what only the JSON format carries](#9-advanced-what-only-the-json-format-carries)
- [10. Troubleshooting](#10-troubleshooting)
- [11. Reference: every field of a register row](#11-reference-every-field-of-a-register-row)

---

## 1. What you need from the manual

For a **Modbus** device, find the register table (usually titled *Modbus
map*, *Register list*, or *Communication protocol*). For each value you want:

| You need | Manual says it as | Example |
|---|---|---|
| **Address** | Address, Register, Offset, Hex address | `0x0000`, `40001`, `30013` |
| **Function code** | Holding / input register, FC03 / FC04, Read Holding | holding = FC3 |
| **Data type** | Float, Int32, UINT16, U32, "2 registers" | float = 32-bit IEEE |
| **Scale** | ×0.1, /10, Resolution 0.01, Unit 0.1 V | ×0.1 → scale 10 |
| **Unit** | V, A, kWh, Wh, % | V |

You also need, once per device: the **word order** of 32-bit values (§2.2),
the default **unit (slave) ID**, and for RTU the **baud rate, parity and stop
bits**.

For an **HTTP/JSON** device: one sample response of the URL, so you can point
at each value. For **MQTT**: the topic(s) and one sample payload.

## 2. Three ideas that make or break a map

### 2.1 Addresses: protocol address, not "register number"

The gateway uses the **protocol address**, which counts from 0. Many manuals
print the older 1-based **register number** instead, with the table built into
the first digit:

| Manual says | Means | Write in the template |
|---|---|---|
| `40001` | holding register, first one | address `0`, FC3 |
| `40101` | holding register | address `100`, FC3 |
| `30001` | input register, first one | address `0`, FC4 |
| `0x0000`, `0` | already a protocol address | address `0` |

If **every** address of a CSV import lies between 40001 and 49999 (or 30001
and 39999), the import warns you. If the manual numbers its registers from 1
without the 4/3 prefix ("register 1 = voltage"), subtract 1.

> When values come out shifted (the voltage shows in the current's place, or
> everything reads 0), the base is usually off by one.

### 2.2 Word order: the most common reason for nonsense values

A Modbus register holds 16 bits, so a 32-bit value (float, int32) spans **two
registers**. Devices disagree about which one comes first. Take 230.5 V as a
float, whose four bytes are `43 66 80 00`:

| Order | Registers on the wire | Used by |
|---|---|---|
| **ABCD** (big) | `4366` `8000` | the Modbus convention; most meters |
| **CDAB** (little, word-swapped) | `8000` `4366` | many meters, PLCs and inverters ("LSW first") |
| **BADC** | `6643` `0080` | rare |
| **DCBA** | `0080` `6643` | rare |

Read with the wrong order, 230.5 V becomes something like `-4.2e-39` or
`2.2e+12`. **The order belongs to the template, not to each row:** set it once
under *How the device is read → Word / byte order* (or in the CSV/YAML import
form).

**Not sure?** Pick ABCD, add the device, then open **Diagnostics → Probe**,
enter the address of a value you know (the mains voltage is ideal), and read
it. The probe shows the value decoded all four ways, side by side. Press the
button of the column that shows 230 V, and the template is corrected. Devices
reading with it restart and pick it up within seconds.

### 2.3 Scale and offset

The value the device sends is often an integer you must divide:

```
engineering value = raw ÷ scale + offset
```

| Manual says | Raw on the wire | scale | Result |
|---|---|---|---|
| Voltage, unit 0.1 V | 2305 | `10` | 230.5 V |
| Power, ×0.01 kW | 150 | `0.1` | 1500 W, if you want W |
| Temperature, offset −40 | 65 | `1`, offset `-40` | 25 °C |
| Energy, Wh | 123456 | `1` | 123456 Wh |

Scale is a **divisor**. To turn kWh into Wh (the gateway's standard for energy
fields) use `scale: 0.001`.

## 3. Three ways to write it

| Way | Best for | Where |
|---|---|---|
| **Visual editor** | small maps, fixing or extending one | Templates → **New**, or a user template → **Edit** |
| **CSV import** | a register table copied from a PDF/spreadsheet | Templates → **Import CSV** |
| **YAML import** | community maps (Home Assistant, evcc, mbmd…), maps with states/bit fields | Templates → **Import YAML** |
| **JSON upload** | a template exported from another gateway; the full format | Templates → **Upload** |

Built-in templates are read-only. **Duplicate** one to start from it.

### The visual editor

1. **Id, Name, Vendor, Model.** The id (`a-z 0-9 - _`) is permanent; the rest
   is for people.
2. **How the device is read:**
   - **Ways to reach it:** tick Modbus TCP and/or RTU, RTU listen-only, HTTP,
     MQTT. Ticking HTTP or MQTT adds the **json_path** (and **topic**)
     columns to the rows.
   - **Word / byte order** (§2.2).
   - **Default unit ID** and **Max registers per read**. Lower it to 40 or so
     if the device rejects long reads with an *illegal data address*
     exception.
   - **Poll groups:** how often each group is read, in seconds. Rows say which
     group they are in. Use the fast group for power, the slow one for energy
     counters and identity.
3. **The rows:** address, name, label, unit, type, scale, offset, function
   code (FC3 holding, FC4 input, FC1 coil, FC2 discrete input), category,
   poll group, and for writable registers the bounds and a safe value.
   - The **states** button at the end of a row decodes a code into words
     (`{0: Off, 1: Running, 7: Fault}`) or a status word into named **bits**.
   - A name outside the canonical dictionary (below) is flagged, with a
     *did you mean…?* hint; **Auto-canonicalize** suggests standard names
     from labels and units.
4. **Save.** Errors come back per row. Example: *registers[12]: duplicate
   address 40*.

### Names: use the canonical dictionary

A register's **name** becomes its MQTT topic leaf and its InfluxDB field.
Standard names (`voltage_l1_n`, `power_active_total`, `energy_active_import`,
`soc`…) make every device of the same quantity look alike to Grafana, Node-RED,
the dashboards and virtual meters. Each one also comes with a **unit
contract**: an `energy_*` name promises Wh, and the editor flags a mismatch
in amber. The full list is in [canonical-fields.md](canonical-fields.md).

## 4. Example: a Modbus meter from its manual (CSV)

Say the manual of a three-phase meter lists:

| Parameter | Address (hex) | Type | Unit | Notes |
|---|---|---|---|---|
| Phase 1 voltage | 0x0000 | Float | V | low word first |
| Phase 2 voltage | 0x0002 | Float | V | |
| Phase 3 voltage | 0x0004 | Float | V | |
| Total active power | 0x0034 | Float | W | |
| Frequency | 0x0046 | Float | Hz | |
| Import active energy | 0x0048 | Float | kWh | |
| Relay 1 state | 0x0000 | coil | — | FC01 |

*"Low word first"* means **CDAB**. Write the CSV. Column names are
flexible: `addr`, `type`, `fc`… are recognized too.

```csv
address,name,label,unit,type,scale,fc,category,poll_group
0x0000,voltage_l1_n,Phase 1 voltage,V,float,1,fc3,voltage,realtime
0x0002,voltage_l2_n,Phase 2 voltage,V,float,1,fc3,voltage,realtime
0x0004,voltage_l3_n,Phase 3 voltage,V,float,1,fc3,voltage,realtime
0x0034,power_active_total,Total active power,W,float,1,fc3,power,realtime
0x0046,frequency,Frequency,Hz,float,1,fc3,frequency,normal
0x0048,energy_active_import,Import active energy,Wh,float,0.001,fc3,energy,slow
0x1000,relay_1,Relay 1 state,,uint16,1,coil,status,normal
```

Notes on the rows:
- **Energy:** the manual gives kWh; `scale 0.001` (÷0.001 = ×1000) delivers
  canonical Wh.
- **The coil:** it got address `0x1000`, not `0x0000`. Within one template
  each address is used once, whatever its function code. If a coil and a
  register share a number, give the coil its own row address or put it in a
  second template.

In **Templates → Import CSV**: paste it, fill **Id** (`acme_em3`) and
**Name**, choose **Read over: Modbus** and **Word order: CDAB · low word
first**, press **Check**. Then:
1. Review the count, the warnings and the canonical check.
2. Press **Import to library**.
3. Open it in the editor if you want to adjust poll intervals.

The same map in **YAML** carries the order in its own header:

```yaml
id: acme_em3
name: ACME EM3 three-phase meter
byte_order: CDAB            # or big / little / badc / dcba
transports: [tcp, rtu]
registers:
  - { address: 0x0000, name: voltage_l1_n, label: Phase 1 voltage, unit: V, data_type: float }
  - { address: 0x0034, name: power_active_total, unit: W, data_type: float }
  - { address: 0x0048, name: energy_active_import, unit: Wh, data_type: float, scale: 0.001 }
  - address: 0x0060
    name: operating_state
    data_type: uint16
    enum: { 0: Off, 1: Running, 7: Fault }
```

More on both formats: [csv-import.md](csv-import.md),
[yaml-import.md](yaml-import.md).

## 5. Example: HTTP/JSON and MQTT devices

### HTTP/JSON

The URL returns, for example:

```json
{ "Body": { "Data": { "PAC": { "Value": 1500, "Unit": "W" }, "UAC": { "Value": 231.2 } } } }
```

Each row points at a value with a **json_path**: dots between keys, `[0]` for
list items. No address is needed; one is assigned for you.

```csv
name,label,unit,json_path
power_active_total,AC power,W,Body.Data.PAC.Value
voltage_ln_avg,AC voltage,V,Body.Data.UAC.Value
```

Import with **Read over: HTTP/JSON**. When adding the device, give the URL.
For safety it must point at a private LAN address, unless
`security.allow_nonlan_http_devices` is set. **Test connection** reports
how many of the template's paths resolved in the live answer.

### MQTT

Values arrive on topics, as JSON or as a bare number:

| Payload on `home/meter/state` | Row |
|---|---|
| `{"p": 800, "soc": 55.5}` | `json_path: p` |
| `{"data": {"soc": 55.5}}` | `json_path: data.soc` |
| `55.5` (bare) | no json_path: the payload is the number |

A row may read its **own topic**: `topic: home/meter/soc`. A topic written as
`~/soc` is **relative** to the device's topic, so one template serves many
units (`battery/1/#`, `battery/2/#`…). Topics may use `+` and `#`.

```yaml
id: acme_bms_mqtt
name: ACME BMS over MQTT
transports: [mqtt]
registers:
  - { name: soc, unit: "%", topic: "~/soc" }                  # bare number
  - { name: power, unit: W, topic: "~/state", json_path: p }  # JSON field
```

When adding the device, give the broker, port, credentials and the
**subscribe topic** (e.g. `battery/1/#`). **Discover → MQTT topic browse**
shows what a broker is publishing, so you can pick rather than type.

## 6. Test it on the real device

1. **Devices → Add Device:** choose the protocol and the connection.
   - TCP: host, port, unit ID.
   - RTU: serial port or the serial bridge, baud, parity, stop bits.

   **Test connection** proves the device answers. For Modbus, any protocol
   answer counts, even an exception.
2. **Choose your template**, keep the suggested selection, **Create**.
3. Open the device. **Overview** shows every value with its age. Compare
   two or three with the device's own display.
4. Wrong values? Read §10. The usual suspects, in order:
   - word order (§2.2);
   - address base (§2.1);
   - scale (§2.3);
   - FC3 vs FC4.
5. **Diagnostics → Bus monitor** shows every request and answer on the wire,
   exceptions included. **Probe** reads any address and decodes it every way.

## 7. Changing a template later

- **How the device is read** (word order, max read size): save, and every
  device reading with that template restarts and uses it at once. The save
  message lists them.
- **Labels, units, descriptions, sections, totals, new calculated fields:**
  units made from the template keep their *copy* until you bring the change
  in. The installation page (and a standalone device's page) announces
  **The template has been updated** only when there is something to apply.
  **Review and apply** shows every change before writing it. What you
  decided on a unit (which values are read, outputs, thresholds, formulas)
  stays yours. See [MANUAL §5.3](MANUAL.md#53-how-a-unit-is-shown--the-template-decides).
- **Rows you add to a template** do not appear on existing devices by
  themselves. Add them on the device: *Measurements → Available*.

The **Templates** page shows, per template, which devices use it and where an
update is waiting. A template that is in use cannot be deleted. If a device's
template disappears (renamed, failed to load), its page says **Template not
available** and it keeps reading with its own copy.

## 8. Sharing a template

**Export** downloads the template as JSON. **Upload** on another gateway
imports it: it is validated row by row, and an existing id asks before
overwriting. A template is a plain file, safe to share. It holds no
addresses of your devices, no credentials, no values.

## 9. Advanced: what only the JSON format carries

The full format, field by field, is in
[config-reference.md](config-reference.md#template-only-extras). Beyond the
register rows it can carry:

| Block | What it does | Reference |
|---|---|---|
| `calculated` | formulas shipped with the map (a decoded status, power from V×I) — every unit gets them | [config-reference — calculated](config-reference.md#calculated--derived-measurements-a-template-ships), [MANUAL §7](MANUAL.md#7-calculated-registers) |
| `aggregates` on a row | how an installation of many units totals that field (`sum`, `avg`, `min`, `max`, `spread`, `mode`) | [config-reference — endpoints](config-reference.md#endpoints--n-units-of-the-same-device-behind-one-endpoint) |
| `display` | how a unit is shown: names, columns, alarms, cell grids, bitmasks | [config-reference — display](config-reference.md#display--how-a-unit-of-this-kind-is-shown) |
| `commands` | named, guarded writes (a power limit) | [MANUAL §14b](MANUAL.md#14b-commands--named-writes-from-the-template) |
| `scale_from`, `nan`, `monotonic` | SunSpec scale factors, "not available" markers, counter hygiene | [config-reference — per-register options](config-reference.md#per-register-options) |

The easiest way in: make the map in the editor, **Export** it, add the block
in a text editor, **Upload** it back. Validation names the exact offending
key.

A minimal complete template:

```json
{
  "device_template": {
    "schema_version": 1,
    "id": "acme_em3",
    "name": "ACME EM3 three-phase meter",
    "vendor": "ACME", "model": "EM3", "version": "1.0.0",
    "protocol": { "transports": ["tcp", "rtu"], "byte_order": "little",
                  "default_unit_id": 1, "max_registers_per_read": 60 },
    "poll_groups": { "realtime": { "interval": 1 }, "normal": { "interval": 5 },
                     "slow": { "interval": 60 } },
    "categories": { "voltage": { "label": "Voltage", "order": 1 },
                    "power":   { "label": "Power",   "order": 2 } },
    "registers": [
      { "address": 0, "name": "voltage_l1_n", "label": "Phase 1 voltage", "unit": "V",
        "data_type": "float", "category": "voltage", "poll_group": "realtime" },
      { "address": 52, "name": "power_active_total", "label": "Total active power",
        "unit": "W", "data_type": "float", "category": "power", "poll_group": "realtime" }
    ],
    "calculated": [
      { "name": "power_kw", "label": "Power (kW)", "unit": "kW",
        "expr": "power_active_total / 1000", "decimals": 2 }
    ]
  }
}
```

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Floats read as `-4.2e-39`, `2.2e+12` | word order | Diagnostics → Probe, press the sane column (§2.2) |
| Every value is shifted by one quantity | 1-based addresses | subtract 40001 / 30001 (§2.1) |
| Values are 10× or 1000× off | scale | §2.3; for energy, canonical names promise Wh |
| *Illegal data address* in the bus monitor | address does not exist, or the read is too long | check the address; lower *Max registers per read* |
| *Illegal function* | wrong function code | FC3 ↔ FC4 |
| Coil/discrete value missing | row on FC3 | set FC1/FC2 on the row |
| HTTP device: nothing reads | json_path does not match | Test connection shows how many paths resolved; check the sample |
| MQTT device: nothing reads | topic mismatch | the device's subscribe topic must cover each row's topic; use `~/leaf` |
| Value right, name flagged | non-canonical name | Auto-canonicalize, or pick from the list |
| Save refused: *duplicate address* | two rows, one address | each address once per template, whatever the function code |
| Change saved but a unit still shows the old label | units keep their copy | the page offers **Review and apply** (§7) |

## 11. Reference: every field of a register row

| Field | Default | Meaning |
|---|---|---|
| `address` | — | protocol address 0–65535 (§2.1); omit for HTTP/MQTT rows |
| `name` | — | canonical name, becomes the MQTT leaf and InfluxDB field |
| `label` | name | what people see |
| `unit` | `""` | V, A, W, Wh, %, °C… |
| `data_type` | `float` | `float`, `double`, `int16`, `uint16`, `int32`, `uint32`, `int64`, `uint64`, `sm16`, `sm32` (sign-magnitude), `string:N` (N registers of ASCII) |
| `register_type` | `holding` | `holding` (FC3), `input` (FC4), `coil` (FC1), `discrete` (FC2) |
| `scale` / `offset` | 1 / 0 | engineering = raw ÷ scale + offset |
| `category` | — | section it is shown under |
| `poll_group` | `normal` | which read rate |
| `enum` / `bits` | — | decode a code into a word, or a status word into named bits |
| `writable`, `write_min`, `write_max`, `write_allowed`, `write_safe` | — | the write envelope ([MANUAL §14](MANUAL.md#14-modbus-writes--dead-man-leases)) |
| `json_path` | — | HTTP/MQTT: where the value is in the payload |
| `topic` | — | MQTT: the row's own topic; `~/leaf` is relative to the device's |
| `description` | — | free text, shown as a tooltip |

Template-level fields (`protocol`, `poll_groups`, `categories`, …) and the
advanced blocks are in [config-reference.md](config-reference.md#per-register-options).
