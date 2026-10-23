# Devices found over MQTT

Many devices announce themselves on a broker: the gateway's ESP nodes, Tasmota, and others. The gateway listens for
those announcements on its own broker and lists what it finds under **Devices → Found on MQTT**. **Nothing is added
by itself:** a found device waits until someone adopts it or ignores it.

## What is found

| Kind | Announcement | Read with |
|---|---|---|
| **ESP node** (multi-bus-gateway-esphome) | `mbg/nodes/<mac>/announce`, configured nodes only (a node with the basic image has nothing to read yet) | `esphome_node_<mac>`, the template the ESP nodes service makes for each node |
| **Tasmota** | `tasmota/discovery/<mac>/config` (Tasmota's own discovery, on by default since 9.x; `SetOption19 0`) | `tasmota_generic`: relays, energy monitor, Wi-Fi signal, uptime, online/offline |
| **Your own rules** | any topic you declare (below) | the template you name |

Announcements are retained, so a gateway that restarts finds them all again at once.

## Each found device

| State | Means | What you can do |
|---|---|---|
| new | not in the gateway yet | **Adopt**, **Ignore** |
| template missing | its template is not here (an ESP node not yet sent from the ESP nodes page) | send it from there, or **Ignore** |
| in the gateway | a device already reads these topics | nothing: counted under the list |
| ignored | someone chose not to see it | **Show again** (from *show them*) |

**Adopt** (admin) makes a device:
- named after the device, with an id made from that name (`-2` if taken);
- **off**: turn it on in its page when ready;
- with the template and topics from its announcement, on the gateway's own broker (`broker: "@gateway"`), so no
  broker password is copied;
- recorded in the audit log (`mqtt device adopted`).

**Ignore** is remembered (`config/mqtt_discovery.json`).

## Tasmota in detail

The announcement gives the device's topic (`t`), its full topic pattern (`ft`, `%prefix%/%topic%/` by default) and its
prefixes (`tp`: cmnd, stat, tele). The device's stat and tele topics become **topic variables** of the device:
`{stat}` = `stat/<topic>`, `{tele}` = `tele/<topic>`. The template reads:
- **relays:** `{stat}/POWER`, or `POWER1`–`POWER4` on a device with several;
- **energy:** `{tele}/SENSOR`, `ENERGY.Power`, `Voltage`, `Current`, `Total`, `Today`;
- **Wi-Fi signal and uptime:** `{tele}/STATE`;
- **online/offline:** `{tele}/LWT`, as its availability.

A relay's state arrives when it changes. To have it at once after the gateway restarts, set `PowerRetain 1` on the
device: its state is then retained, and taken while the device says it is online. Rows the device does not have stay
empty. Edit the template to remove them, or to add its own sensors (`{tele}/SENSOR`, a JSON path each).

**Shelly** devices have no standard announcement, so they are not found. Add them with **Add Device** and an MQTT
template.

## Topic variables (any MQTT device)

A template's register topics, and its availability topic, may contain `{name}`. Each device fills them from its
connection's `vars`:

```yaml
connection:
  protocol: mqtt
  broker: "@gateway"
  topic: tele/plug1            # the base for ~/… topics
  vars: {stat: stat/plug1, tele: tele/plug1}
```

- A variable's value is one or more topic levels, **never a wildcard** (`+`, `#`). A device with one is refused.
- At most 16 per device, named `a–z 0–9 _`.
- A `{name}` with no value stays as it is (and matches nothing).

## Your own rules

`config/mqtt_discovery.json`:

```json
{
  "enabled": true,
  "ignored": [],
  "rules": [
    {"id": "zb_sensor", "label": "Zigbee sensor", "topic": "zigbee2mqtt/+/info", "id_segment": 1,
     "name": "friendly_name", "match": {"type": "sensor"},
     "template": "zigbee2mqtt_sensor", "base_topic": "zigbee2mqtt/{id}",
     "vars": {"model": "definition.model"}}
  ]
}
```

| Key | Meaning |
|---|---|
| `id`, `label` | the rule's own (not a built-in rule's id) |
| `topic` | the announcement topic, `+` where the device's id is; no `#` |
| `id_segment` | which level of the topic is the device's id, from 0 |
| `name` | a JSON path in the announcement for the device's name (else its id) |
| `match` | `{JSON path: value}` the announcement must carry |
| `template`, `base_topic` | the template, and the device's topic; `{id}` is the device's id |
| `vars` | `{variable: JSON path}`: topic variables from the announcement |

A rule with a problem is skipped and reported (`rule_problems` in the API). The gateway reads the file when it
starts.

## API

| Method | Path | Role | What it does |
|---|---|---|---|
| GET | `/api/mqtt-discovery` | viewer | `{enabled, connected, error, items, rules, rule_problems}`; each item: `key`, `kind`, `name`, `template`, `connection`, `info`, `state` |
| POST | `/api/mqtt-discovery/{key}/adopt` `{id?, name?}` | admin | make the device, off; 409 when its template is missing |
| POST | `/api/mqtt-discovery/{key}/ignore` `{ignore}` | admin | hide it, or show it again |
| POST | `/api/mqtt-discovery/enabled` `{enabled}` | admin | listen or not, from the next start |
