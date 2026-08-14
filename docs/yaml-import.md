# Importing a register map from YAML

A vendor or community register map in YAML becomes a full device template —
richer than [CSV import](csv-import.md), because per-register decode maps
(`enum`/`bits`), the write-safety envelope and thresholds carry through intact.
**Template Manager → Import YAML** previews the map, then saves it to the
template library.

## Accepted shapes

The importer finds the register list in any of these document shapes:

- a **bare list** of register mappings;
- a top-level **`registers:`** list — also accepted under `registry`, `points`,
  `signals`, `measurements`, `sensors`, `parameters`, `map` or `items`;
- an MBG-native **`device_template:`** wrapper (a template export re-imports
  at full fidelity).

Document-level metadata (`id`, `name`, `vendor`, `model`, `device`, `title`,
`description`) is picked up automatically and pre-fills the template header —
you can override id/name/vendor/model in the import dialog.

## Field mapping

Field names are matched **loosely** (case-insensitive; spaces, underscores,
hyphens and dots ignored), with the same aliases the CSV importer accepts:

| Canonical | Aliases accepted | Notes |
|-----------|------------------|-------|
| `address` | `addr`, `reg`, `register`, `offset`, `modbus_address` | decimal or `0x` hex, 0–65535 |
| `name` | `key`, `tag`, `variable`, `signal`, `point` | required |
| `label` | `description`, `desc`, `parameter`, `measurement`, `title` | defaults to `name` |
| `unit` | `units`, `uom` | |
| `data_type` | `type`, `format`, `dtype` | see types below (default: float, configurable) |
| `scale` | `factor`, `multiplier`, `gain` | engineering = raw ÷ scale (default 1) |
| `category` | `group`, `cat` | UI grouping |
| `poll_group` | `poll`, `rate` | |
| `register_type` | `regtype`, `fc`, `table`, `block` | `holding` (default), `input`/`fc4`, `coil`/`fc1`, `discrete`/`fc2` |
| `json_path` | `path`, `json` | for HTTP/JSON devices (instead of an address) |

**Data types:** `float`/`float32`, `double`, `int16`/`s16`, `uint16`/`u16`/`word`,
`int32`/`s32`/`dint`, `uint32`/`u32`/`dword`, `int64`, `uint64`, `string:N`
(N registers of ASCII). Unknown types coerce to the chosen default with a
warning.

### MBG-native fields (passed through verbatim)

This is what makes YAML higher-fidelity than CSV — when present on a register,
these are copied through untouched and validated at save time:

`enum`, `bits`, `mask`, `shift`, `offset`, `monotonic`, `nan`,
`writable`, `write_min`, `write_max`, `write_safe`, `thresholds`, `topic`,
`device_class`, `state_class`, `entity_category`, `enabled_by_default`,
`icon`, `suggested_display_precision`

(See [config-reference.md](config-reference.md) for what each one means.)

## Example

```yaml
name: ACME PM-300
vendor: ACME
model: PM-300
registers:
  - address: 0x0000
    name: voltage_l1_n
    label: Voltage L1-N
    unit: V
    type: uint16            # alias of data_type
    scale: 10               # raw 2305 -> 230.5 V
  - reg: 12                 # alias of address
    name: power_active_total
    desc: Total active power
    unit: W
    datatype: int32
    device_class: power
    state_class: measurement
  - addr: 30
    name: energy_active_import_total
    unit: kWh
    type: uint32
    scale: 100
    monotonic: true         # cumulative counter: reject downward glitches
    nan: true               # type's standard not-available sentinel
    state_class: total_increasing
  - address: 100
    name: operating_state
    type: uint16
    enum: {0: "Off", 1: Standby, 4: Running, 7: Fault}
    entity_category: diagnostic
```

Canonical field names ([canonical-fields.md](canonical-fields.md)) are
recommended — the `name` becomes the MQTT topic leaf and the InfluxDB field.

## How it behaves

- **Preview first** — nothing is saved on parse. The dialog reports how many
  registers parsed, per-row **warnings**, and any blocking
  **validation errors**.
- **Warnings** (row skipped or coerced, never fatal):
  - item is not a mapping → skipped
  - no `name` → skipped
  - unparseable address → skipped
  - no `address` *and* no `json_path` → skipped
  - duplicate address+name → skipped
  - unknown `data_type` → coerced to the default
  - bad `scale` → coerced to 1
- **Fatal errors** (the import is rejected): empty input, invalid YAML, or no
  register list found in any of the accepted shapes.
- **Validation** — the resulting template goes through the same validator as an
  uploaded JSON template. Blocking problems must be fixed before Import is
  enabled: invalid template id (a-z 0-9 `-` `_`, 2–64 chars), missing name, no
  registers, address outside 0–65535, **duplicate address** (each address must
  be unique — runtime state, MQTT and InfluxDB are keyed by address),
  unsupported data type, `string` without a length (`string:7`), zero scale,
  and a `writable` register without both `write_min` and `write_max`.
- **Save** — on Import the template lands in the user template library
  (`config/device_templates/`, listed alongside the built-ins; a user template
  cannot shadow a built-in id) and can then be assigned to any device from its
  Measurements view.

## API

`POST /api/device-templates/import-yaml` with
`{yaml, id?, name?, vendor?, model?, default_data_type?, default_poll_group?}`
(2 MB cap) returns the same preview shape as the CSV endpoint:
`{device_template, register_count, warnings, validation_errors}`. The reviewed
`device_template` is then saved via `POST /api/device-templates/upload`.

## YAML or CSV?

Both share the same alias/address/type machinery and the same preview → save
flow. Use **CSV** when copying a register table out of a vendor PDF — it's the
quickest path. Use **YAML** when the map already exists in a structured form or
when you need what CSV can't express: `enum`/`bits` decode, `nan` sentinels,
`monotonic` counters, Home Assistant typing, thresholds, or the write envelope.

See also: [csv-import.md](csv-import.md) ·
[config-reference.md](config-reference.md) ·
[canonical-fields.md](canonical-fields.md) · [device-catalog.md](device-catalog.md) ·
[MANUAL.md](MANUAL.md)
