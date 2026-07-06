# Importing a register map from CSV

Any Modbus (or HTTP/JSON) device becomes usable by importing its **register map**
as a CSV — the format vendors and communities already publish. No code, no
hand-written JSON. A device's Measurements view → **Import CSV** turns the CSV
into a device template and assigns it to that device.

## Format

The first row is the header. Column names are matched **loosely** (case-
insensitive, common aliases, `,` `;` or tab delimiter). Only two columns are
required:

| Field | Required | Aliases accepted | Notes |
|-------|----------|------------------|-------|
| `address` | yes* | `addr`, `reg`, `register`, `offset` | decimal or `0x` hex, 0–65535 |
| `name` | yes | `key`, `tag`, `signal`, `point` | the canonical register name |
| `label` | no | `description`, `desc`, `parameter`, `measurement` | human label |
| `unit` | no | `units`, `uom` | `V`, `A`, `W`, `Hz`, … |
| `data_type` | no | `type`, `format`, `dtype` | see types below (default: float) |
| `scale` | no | `factor`, `multiplier`, `gain` | engineering = raw ÷ scale |
| `category` | no | `group`, `cat` | grouping for the UI |
| `poll_group` | no | `poll`, `rate` | `realtime` / `normal` / `slow` |
| `access` | no | `rw`, `mode` | `RW`/`WR` → marks writable (informative) |
| `json_path` | yes* | `path`, `json` | for HTTP/JSON devices (no Modbus address) |

\* Provide **`address`** for Modbus devices, or **`json_path`** for HTTP/JSON
devices (then a synthetic address is assigned automatically).

**Data types:** `float`/`float32`, `double`, `int16`/`s16`, `uint16`/`u16`/`word`,
`int32`/`s32`/`dint`, `uint32`/`u32`/`dword`, `int64`, `uint64`, `string`. Unknown
types fall back to the chosen default with a warning.

## Example

```csv
address,name,label,unit,type,scale,category
0x0000,V_L1,Voltage L1-N,V,float,1,voltage
0x0002,V_L2,Voltage L2-N,V,float,1,voltage
40,P_total,Total active power,W,int32,10,power
19000,Freq,Frequency,Hz,uint16,100,frequency
```

The **Import CSV** dialog has a *download example* link with this content.

## How it behaves

- **Preview first** — it reports how many measurements parsed, which columns were
  recognized, and any **warnings** (rows skipped for a bad/missing address,
  duplicate address+name, unknown type coerced to the default). Warnings never
  abort the import; only a broken header (no `address`/`name`) is fatal.
- **Validation** — the resulting template is validated exactly like an uploaded
  JSON template; blocking problems (bad id, duplicate address+name, unsupported
  type, zero scale) must be fixed before Import is enabled.
- **Assign** — on import the template is saved to the library and assigned to the
  current device (name conflicts prompt before overwriting), then its pollers
  reload. You can further edit it in the Measurements register editor.

## Where to get vendor maps

Manufacturer Modbus manuals almost always include a register table (copy it into
a CSV), and communities publish maps for common meters/inverters (Eastron SDM,
Carlo Gavazzi, Schneider, ABB, SolarEdge/Huawei SunSpec, …). For SunSpec
int+scale-factor models, bake the fixed scale into the `scale` column.

> Note: the reader currently issues **FC3 (holding registers)**. Devices whose
> measurements live in **input registers (FC4)** or **coils** are on the roadmap
> (audit item P1) — importing their map is fine, but reads land once FC4 support
> ships.
