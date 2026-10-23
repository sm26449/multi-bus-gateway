# Asset attributes

What a piece of equipment **is**, **where**, and **how it is built**. These are
facts no measurement carries, and other systems (an EMS, an HMI, a SCADA) need
them:
- the plant is at 48.86 N, 2.35 E;
- this meter is the grid connection, with 10 kW of import and 8 kW of export
  allowed;
- this inverter's MPPT 1 has 24 panels of 450 Wp, facing south at 30°.

The attributes come from the gateway's **catalog**. You press **+** and pick
one; you never invent one. A name therefore means the same thing on every
installation, and a system reading it can trust it. **Everything is
optional:** set nothing, only the location, or everything, now or later, by
hand or through the API.


## Where

| Where | In the UI | What it typically holds |
|---|---|---|
| **The site** | Settings → **Site** | its location; who may see the coordinates |
| **An installation** | its page, **Attributes** | its own location if it is elsewhere; a battery bank's energy |
| **A device** | its page, tab **Attributes** | a meter's grid connection; an inverter's PV inputs and rating; a battery's limits |

**The location is inherited:** a device takes its installation's, else the
plant's. The device's Attributes tab says where it comes from. Nothing else
is inherited: for four identical inverters, use **Copy to…** (the values are
copied, not linked).

## The catalog

| Category | Attributes | On |
|---|---|---|
| **Location** | latitude, longitude (decimal degrees, north and east positive), altitude (optional) | the plant, an installation |
| **Grid connection** | role: connection point, sub-meter, generation, load, storage<br>largest import and export allowed (kW, ≥ 0)<br>phases: 1 or 3<br>main fuse (A per phase)<br>what the meter's positive power means: import or export | a device (a meter) |
| **PV input** (one row per orientation) | MPPT input, modules, module Wp, module make and model, cell technology (mono, poly, thin film), parallel strings, kWp, facing, tilt | a device (an inverter) |
| **Battery** | usable kWh; the system's largest charge and discharge power (kW) | a device or an installation (a bank) |
| **Rating** | rated AC power (kW, the nameplate) | a device (an inverter) |

**What is offered first.** After **+**, the categories that fit the
equipment come first; the others follow, marked "·". Nothing is blocked. The
equipment's kind is taken from:
1. its installation group's role;
2. else its template's `kind`;
3. else its measurement names (DC inputs → inverter, a SOC → battery,
   import/export energy → meter).

**Conventions:**
- **Facing (azimuth):** compass degrees, 0 = north, 90 = east, 180 = south,
  270 = west. The editor offers N…NW or exact degrees. pvlib uses the same.
  PVGIS's "aspect" is ours − 180.
- **Tilt:** 0 = flat, 90 = vertical.
- **kWp:** of the modules, not the inverter. With modules and module Wp set,
  kWp is computed. If you give it too, it must agree within 1 %.
- **MPPT input:** an inverter whose template shows its DC inputs
  (`power_dc_mppt1`, `…2`) warns about an input number above that count. It
  does not refuse it.

## Checks

On save, from the UI or the API:
- type, unit range and choices;
- the level each category may sit on;
- only a **connection point** may carry the grid limits;
- kWp agreeing with modules × Wp;
- unknown attributes are refused.

**Warnings that do not block:**
- two connection points on one plant;
- an MPPT input above the inverter's count;
- copying to a unit with fewer inputs.

## Coordinates are personal data

They locate a home, so the gateway is careful with them:

| Who or where | Sees |
|---|---|
| An admin (or the API key on a box without login) | exact, masked in the form with the eye |
| Everyone else, and anything published | as set under **Shown to viewers and published as**: coarse (2 decimals, about 1 km, the default), exact, or not at all |
| The audit log and the backup diff | coarse |
| A sanitised backup | left out; importing it keeps the live coordinates |

**Saving back rounded coordinates keeps the exact ones.** If you see them
rounded (a box without login), the form starts empty. Typing new ones
replaces them; leaving the form keeps them.

## The API

Reading is open to every role. Writing is configuration: an **admin**, or a
**service token with the admin role**. That is how an EMS writes. The API key,
when set, applies on top.

**Standby gateway:** it answers reads and refuses writes with 503. Its file
is replaced by the active one's at the next mirror.

**Conflicts:** a write may send `If-Match: <rev>`. A document changed
meanwhile answers 412 Precondition Failed with the current `rev`, and nothing is
written. `If-Match: *` means "whatever is stored".

| Method | Path | |
|---|---|---|
| GET | `/api/attributes/catalog` | the categories, fields, units, limits, choices, levels, kinds |
| GET | `/api/attributes` | everything held, the **orphans**, the plant-wide warnings |
| GET | `/api/attributes/site` | the plant's attributes |
| GET | `/api/attributes/installations/{id}` · `/api/attributes/devices/{id}` | one place's attributes, its effective location and where it comes from, what is offered |
| PUT | `/api/attributes/site/{category}` · `/api/attributes/{installations\|devices}/{id}/{category}` | replace one **whole** category (all rows of a repeatable one) |
| DELETE | the same paths | delete one category |
| DELETE | `/api/attributes/{installations\|devices}/{id}` | everything held for an id (an orphan) |
| POST | `/api/attributes/devices/{id}/{category}/copy` | copy to `{"to": ["pv-u2", "pv-u3"]}` |
| PUT | `/api/attributes/settings` | `{"location_publish": "coarse" \| "exact" \| "off"}` |

**Examples:**

```bash
# the plant
curl -X PUT -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
     -d '{"latitude_deg": 48.86, "longitude_deg": 2.35}' \
     http://gateway:8080/api/attributes/site/location

# the grid connection on the meter
curl -X PUT -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
     -d '{"role": "connection_point", "max_import_kw": 10, "max_export_kw": 8, "phases": 3, "main_fuse_a": 25, "positive_is": "import"}' \
     http://gateway:8080/api/attributes/devices/umg512/grid_connection

# an inverter's two inputs, then the same on its three siblings
curl -X PUT -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
     -d '[{"mppt": 1, "modules": 24, "module_wp": 450, "azimuth_deg": 180, "tilt_deg": 30},
          {"mppt": 2, "modules": 20, "module_wp": 450, "azimuth_deg": 90, "tilt_deg": 15}]' \
     http://gateway:8080/api/attributes/devices/pv-u1/pv_input
curl -X POST -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
     -d '{"to": ["pv-u2", "pv-u3", "pv-u4"]}' \
     http://gateway:8080/api/attributes/devices/pv-u1/pv_input/copy
```

## When equipment comes and goes

- **Deleting a device keeps its attributes.** It can be restored. Until
  then they are listed as **orphans** (Settings → Site).
- **They are dropped** when you forget the deleted device, when you delete
  them by hand, or when a **new** device is created at that id: nothing an
  earlier device kept carries over to a new one.
- **Deleting an installation keeps its units' attributes,** as it keeps their
  register files for a re-add.
- **An installation unit's id follows its unit number** (`pv-u1`). Changing
  the number makes it a new device, and its old attributes become orphans.

## Backup, snapshots, the HA pair

- `config/attributes.yaml` is in every snapshot, in a restore and in the HA
  pair's mirror.
- It is in the configuration export: without coordinates unless the export
  includes identity.
- A restore or an import is checked against the catalog before anything is
  written.

## Using them

### The sun in formulas and rules

| Function | Gives |
|---|---|
| `sun_elevation()` · `sun_elevation("pv")` | the sun's height in degrees, apparent (refraction included), at the plant or at an installation or device (its installation's location, else the plant's) |
| `sun_azimuth("pv")` | where the sun is, compass degrees (180 = south) |
| `is_day()` · `is_day(5)` · `is_day(5, "pv")` | between sunrise and sunset (the true elevation above −0.833°, as almanacs define them), or the sun as seen above the degrees given |

- **Accuracy:** NOAA's algorithm, checked against pvlib: within 0.014° above
  the horizon.
- **Time:** UTC, so the timezone does not matter.
- **No location set:** the value is missing, never wrong.
- **The place name** is checked when the formula is saved.
- **A rule that reads only the sun** is never stale, like one on the clock.

```text
pump only while the sun is above 10°:   when: 'is_day(10, "pv")'
the evening light on 30 min after sunset: when: 'sun_elevation() < -4'
```

### Attributes in formulas

`attr("umg512", "grid_connection", "max_export_kw")` reads one attribute:
three names in quotes, checked against the catalog on save. It reads only
numbers, and only what is set on that place: unlike the sun functions, it
does not inherit a location. It reads exact values, coordinates included;
formulas run inside the gateway. On a repeated category (PV inputs):
- a field that adds up gives the total: `attr("pv-u1", "pv_input", "kwp")`
  = the inverter's kWp;
- any other field gives the value every row shares.

Text values and unset attributes mean no value.

```text
room under the export limit (this meter counts + as import):
    attr("umg512", "grid_connection", "max_export_kw") * 1000 + power_active_total
specific yield of an inverter, W per kWp:
    power_active_total / attr("pv-u1", "pv_input", "kwp")
```

### Inverters asleep at dawn and dusk

An inverter group's totals count an inverter as **asleep** when it is silent
while the sun at its installation is below 5°, and either it went quiet with
the sun already that low or it is night (below −1°). Then:
- its power and current are 0, not unknown, and so is every total that sums
  them;
- it is counted in `units_asleep`;
- it does not make the installation `partial`.

An inverter that stopped at 14:00 stays **missing** through dusk until
night, so a fault is never hidden by the evening. At night, with all of them
asleep, the power reads 0 and `status` is `asleep`. This needs a location;
without one, nothing changes.

**What consumers see change:**
- the totals' power is 0 at night (MQTT and InfluxDB) instead of a gap;
- `status` has a new value, `asleep`;
- the installation page and the fleet show it as such.

### Published

- **MQTT, retained and versioned:**
  - `<device topic>/meta`;
  - `<installation totals root>/meta` (`mbg/endpoints/<id>/meta` by
    default);
  - `<gateway topic prefix>/site/meta`, on this gateway's own roots, so two
    gateways on one broker never share a topic.

  A place's topic is republished when its content changes, and all of them
  after a (re)connect, a restore or an import. Only the active gateway
  publishes. `rev` is the revision of the whole attributes document. See
  [mqtt-contract.md](mqtt-contract.md).
- **OPC UA:** `Devices/<device>/Attributes.<category>.<field>`. See
  [opcua.md](opcua.md#2-what-a-client-sees).

Both show coordinates as the publishing setting says.
