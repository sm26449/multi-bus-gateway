# API by example

Everything the web UI does goes through the REST API. A script, Node-RED,
Home Assistant or your own program can do the same. This page is the
practical side: copy-paste recipes. The reference (every route, its body and
its minimum role) is [API.md](API.md). The gateway also serves an
interactive, generated reference at `http://<gateway>:8080/docs` (Swagger)
and `/redoc`.

The examples use a heat pump read through the `samsung_mim_b19n` template
(device id `samsung-dvm`) and a grid meter (`umg512`). Use your own ids from
the Devices page.

- [1. Log in](#1-log-in)
- [2. Read](#2-read)
- [3. Commands](#3-commands)
- [4. Rules](#4-rules)
- [5. Device settings](#5-device-settings)
- [6. Templates](#6-templates)
- [7. MQTT instead of HTTP](#7-mqtt-instead-of-http)
- [8. A Python client](#8-a-python-client)
- [9. Answers and what they mean](#9-answers-and-what-they-mean)

## 1. Log in

```bash
B=http://192.168.1.10:8080          # your gateway
KEY=...                             # only if API_KEY is set on the gateway

# a session cookie, kept in ./jar for the next calls
curl -s -c jar -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"..."}' $B/api/auth/login
```

- **Roles.** Every route has a minimum role: *viewer* reads, *operator* runs
  commands, *admin* changes configuration and rules.
- **API key.** When the gateway has an `API_KEY`, every write (POST, PUT,
  PATCH, DELETE) must also carry `X-API-Key: $KEY`. The key does not replace
  the login; it proves a machine client meant the write.

The examples below define two shell helpers:

```bash
get()  { curl -s -b jar "$B$1"; }
post() { curl -s -b jar -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d "$2" "$B$1"; }
put()  { curl -s -b jar -X PUT -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d "$2" "$B$1"; }
```

## 2. Read

```bash
get /api/status | jq .devices                       # every device, live or not
get /api/devices | jq '.devices[] | {id, template, energy_fields, template_notes}'

# the live values of one device: {key: {name, value, unit, ts, …}}
get "/api/values?device=samsung-dvm" | jq '.values[] | {name, value, unit}'

# one value
get "/api/values?device=samsung-dvm" | jq -r '.values[] | select(.name=="iu0_water_out_temperature") | .value'

# history from InfluxDB: one field, aggregated per window
get "/api/history?device=samsung-dvm&name=iu0_water_out_temperature&start=-24h&every=5m&fn=mean"
```

- **Values.** Values of state registers come as text (`"On"`, `"Heat"`,
  `"Normal"`).
- **Missing values.** A value the device does not have (a missing sensor, a
  not-available marker) is absent rather than reported as 0.

## 3. Commands

A command is a named, checked write a template declares (`power`, `mode`,
`water_out_set`, an inverter's `power_limit`). The gateway checks the range,
writes, waits, reads back and records it.

```bash
# what the device can be told: parameters with limits, enabled or not, last result
get /api/devices/samsung-dvm/commands | jq '.commands[] | {name, enabled, params, last}'

# a dry run: the exact frames it would write and the current readings; nothing written
post /api/devices/samsung-dvm/commands/water_out_set/dry-run '{"value": 35}'

# run it
post /api/devices/samsung-dvm/commands/water_out_set '{"value": 35}' | jq '{status, before, after, reason}'
# → {"status": "success", "before": {"iu0_water_out_set": 42.0}, "after": {"iu0_water_out_set": 35.0}}

# on / off, mode (the allowed values are in the command's params)
post /api/devices/samsung-dvm/commands/power '{"value": 1}'
post /api/devices/samsung-dvm/commands/mode  '{"value": 4}'

# the last commands, from the audit log (admin)
get "/api/commands/history?device=samsung-dvm&limit=20" | jq '.history[] | {ts, status, detail}'
```

- **A dead-man lease.** Add `"lease_s": 600` to the body: if no new command
  comes within 10 minutes, the gateway runs the command's `safe` parameters
  itself. This is for a controller that may die.
- **A command a rule owns.** An armed rule that targets the command answers
  `409 owned by rule <id>`. Add `"override_s": 3600` to pause that rule for
  an hour and send your value.
- **Writes must be allowed.** Settings → Security → allow writes, the device
  not write-locked, and the command enabled on the device (see
  [5](#5-device-settings)).

## 4. Rules

A rule runs a command when live values call for it. It starts in **shadow**:
it decides and logs, and writes nothing until it is armed.

```bash
# every rule with its live picture: state, signal, want → actual, last decision, error
get /api/rules | jq '.rules[] | {id, mode, state: .live.state, error: .live.error}'

# why it decided what it decided (newest first)
get "/api/rules/hp-floor-pv-on/decisions?limit=20" | jq '.decisions[] | {ts, action, reason}'

# what a rule WOULD do now, before saving it
post /api/rules/validate '{
  "id": "try", "kind": "condition",
  "target": {"device": "samsung-dvm", "command": "power"},
  "when": "umg512.power_active_total < -10000",
  "release": "umg512.power_active_total > -2000",
  "then": {"params": {"value": 1}}, "else": {"params": {"value": 0}}}'
```

**Start from a template's example.** Templates ship the rules their devices
are usually run with, adapted to each device:

```bash
get /api/rule-presets | jq '.presets[] | {device, preset, label}'
# take one and create it (always in shadow)
get /api/rule-presets | jq '.presets[] | select(.device=="samsung-dvm" and .preset=="buffer") | .rule' > buffer.json
post /api/rules "$(cat buffer.json)"
```

**Write one yourself.** This is a heat pump buffer thermostat with
hysteresis: on under 25 °C, off only over 35 °C, at most one switch per
15 minutes.

```bash
post /api/rules '{
  "id": "hp-buffer", "label": "Heat pump · on under 25 °C, off over 35 °C",
  "kind": "condition", "target": {"device": "samsung-dvm", "command": "power"},
  "when": "samsung-dvm.iu0_water_in_temperature < 25",
  "release": "samsung-dvm.iu0_water_in_temperature > 35",
  "then": {"params": {"value": 1}}, "else": {"params": {"value": 0}},
  "timing": {"every_s": 10, "debounce": 3, "min_interval_s": 900},
  "stale_after_s": 120, "on_stale": "hold", "on_disable": "hold"}'
```

**Run it, pause it, cap it.**

```bash
post /api/rules/hp-buffer/mode     '{"mode": "armed"}'                # it acts from now on (audited)
post /api/rules/hp-buffer/override '{"seconds": 7200}'                # 2 h of manual control; 0 lifts it
post /api/rules/hp-buffer/clamp    '{"max": 40, "expires_s": 86400}'  # a ceiling on a numeric want
post /api/rules/hp-buffer/mode     '{"mode": "shadow"}'               # back to watching
post /api/rules/hp-buffer/enable   '{"enabled": false}'               # stops evaluating
```

**Move rules between installations.**

```bash
get /api/rules/export > rules.yaml                                   # definitions only
post /api/rules/import "$(jq -Rs '{yaml: ., apply: false}' rules.yaml)"   # preview: new / replace / invalid
post /api/rules/import "$(jq -Rs '{yaml: ., apply: true}' rules.yaml)"    # save; they arrive in shadow
```

Rules from another installation name its device ids. The preview lists the
ones that do not exist here.

## 5. Device settings

```bash
# enable a template's command on a device of its own (nothing is written to the device)
put /api/devices/samsung-dvm/commands/water_out_set/enabled '{"enabled": true}'

# the meter on the device's supply: it gets power_electric (W) and energy_electric
put /api/devices/samsung-dvm/energy-meter '{"device": "iem3150", "power": "power_active_total", "energy": "energy_active_import"}'
put /api/devices/samsung-dvm/energy-meter '{}'                       # remove the link

# bring the device in line with a newer template; add_new also adds the template's new calculated fields
post "/api/devices/samsung-dvm/refresh-from-template?add_new=true" '{}'

# its calculated fields (a formula per entry; water_flow_assumed is a plain number to set)
get /api/devices/samsung-dvm/calculated | jq '.calculated[] | {name, expr}'
```

## 6. Templates

```bash
get /api/device-templates | jq '.templates[] | {id, version, commands, rules}'
get /api/device-templates/samsung_mim_b19n > mim.json        # export one
post /api/device-templates/check "$(cat my_template.json)"   # every problem a save would report, without saving
post /api/device-templates "$(cat my_template.json)"         # save a template of your own (a built-in id is refused: use your own id)
```

The template format is described in [device-templates.md](device-templates.md)
and [templates-advanced.md](templates-advanced.md).

## 7. MQTT instead of HTTP

With **Settings → Security → allow writes** and `mqtt.allow_write_entities`
on, every device with an enabled command listens on its own topics:

```bash
mosquitto_pub -h broker -u user -P pass -t mbg/devices/samsung-dvm/cmd/water_out_set -m 35
mosquitto_pub -h broker -u user -P pass -t mbg/devices/samsung-dvm/cmd/power -m '{"value": 1, "source": "my-script"}'

mosquitto_sub -h broker -u user -P pass -t 'mbg/devices/samsung-dvm/cmd/#' -v   # results (+ the last one, retained)
mosquitto_sub -h broker -u user -P pass -t 'mbg/rules/+/state' -v              # every rule's live state
```

- **Values.** Live values arrive on `mbg/devices/<id>/<field>`.
- **Rules over MQTT.** A rule accepts `{enabled}`, `{clamp: …}` and
  `{override_s}` on `mbg/rules/<id>/set`. **Arming is not possible over
  MQTT**, on purpose: it is done from the UI or the API only.

## 8. A Python client

```python
import requests

B, KEY = "http://192.168.1.10:8080", "..."
s = requests.Session()
s.post(f"{B}/api/auth/login", json={"username": "admin", "password": "..."}).raise_for_status()
s.headers["X-API-Key"] = KEY                     # only if the gateway has an API_KEY


def value(device, name):
    vals = s.get(f"{B}/api/values", params={"device": device}).json()["values"].values()
    return next((v["value"] for v in vals if v["name"] == name), None)


def command(device, name, **params):
    r = s.post(f"{B}/api/devices/{device}/commands/{name}", json=params)
    res = r.json()
    if r.status_code != 200 or res.get("status") != "success":
        raise RuntimeError(f"{name}: {r.status_code} {res.get('status')} {res.get('reason')}")
    return res["after"]


print("water out:", value("samsung-dvm", "iu0_water_out_temperature"))
print(command("samsung-dvm", "water_out_set", value=35))
```

## 9. Answers and what they mean

| Code | Meaning | What to do |
|---|---|---|
| 200 `success` | written and read back as asked | — |
| 200 `mismatch` | written, but the read-back differs | the device refused or changed it; see `after` |
| 200 `unverified` | written, the read-back did not answer | check the device |
| 401 | not logged in | `/api/auth/login` |
| 403 | writes are off, the device is write-locked, the command is not enabled on it, or the role is too low | the message names which |
| 404 | no such device or command | check the ids |
| 409 | a rule owns the target (`owned by rule …`), or no Modbus source is running | `override_s`, or wait for the device |
| 422 | the parameters are outside the command's limits | the message names the limit |
| 429 | the write rate limit | slow down |
| 502 | the device did not answer the write | the bus or the device |

Every command and every rule action is in the audit log (Settings → Audit
Trail, `/api/audit`, or `/api/commands/history` for commands), with who sent
it and through which face.
