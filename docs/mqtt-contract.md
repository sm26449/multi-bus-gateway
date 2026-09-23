# MQTT contract

Every topic the gateway publishes or subscribes to, in one place: pattern,
direction, retain/QoS, payload, when it fires and which `config.yaml` key
controls it. Derived from the code (file paths are given per family so a
maintainer can verify). Consumers — Node-RED, Home Assistant, alertd, another
gateway — should build on this document, not on what MQTT Explorer happens to
show today. Configuration keys are described in
[config-reference.md](config-reference.md); the topic leaves of measured values
are the canonical names in [canonical-fields.md](canonical-fields.md).

## Prefixes and global knobs

| Knob | Default | Effect |
|---|---|---|
| `mqtt.topic_prefix` | `multibus/umg512` | the **primary** device's prefix; also the root of the gateway-level topics (`status`, `alert`, `vmeter/…`, `data_health`) |
| `devices[].mqtt.topic_prefix` | seeded from `mqtt.default_topic_pattern` | every other device's prefix (`${device_id}` substituted); fixed once persisted |
| `mqtt.default_topic_pattern` | `mbg/devices/{device}` | pattern for **new** devices only |
| `mqtt.retain` | `true` | retain flag of every value/liveness/discovery publish that goes through `MQTTPublisher._publish` |
| `mqtt.qos` | `0` | QoS of the same publishes; a few topics are hard-wired to QoS 1 (marked below) |
| `mqtt.publish_mode` | `changed` | `changed` = a value topic publishes only when the rounded value differs from the last confirmed publish; `all` = every poll |
| `mqtt.heartbeat_interval` | `0` (off) | in `changed` mode, republish an unchanged value after this many seconds |
| `mqtt.compat_aliases` | `[]` | dual-publish: every topic starting with `from` is also published under `to` (with `leaves` renames); see below |
| `mqtt.ha_discovery.enabled` / `.prefix` | `true` / `homeassistant` | Home Assistant discovery |
| `mqtt.allow_write_entities` | `false` | gate for every inbound write topic (commands, HA `…/set`) together with `security.allow_writes`; refused while `mqtt.username` is empty |

Throughout this document `<gw>` is `mqtt.topic_prefix`, `<dev>` is one
device's prefix (for the primary, `<dev>` = `<gw>`), and `<ha>` is the
discovery prefix. Example values use `mbg/devices/sdm630_garage` and
`gateway.example.lan` as the broker.

The Last Will is armed at client setup with the current `<gw>`; changing the
prefix forces a reconnect and clears the old `<gw>/status`
(`mqtt_publisher.py`, `update_config`).

## Gateway liveness — `<gw>/status`

| | |
|---|---|
| Pattern | `<gw>/status` |
| Direction | gateway publishes (Last Will and Testament) |
| Retained / QoS | yes / **1** (hard-wired) |
| Payload | `online` or `offline` |
| When | `online` on every (re)connect; `offline` on clean shutdown (flushed before the loop stops) and by the broker's LWT on an unclean drop |
| Code | `multibus/mqtt_publisher.py` — `_setup_client`, `_on_connect`, `disconnect` |

This is the `availability_topic` of the primary's HA sensors and of every
virtual-meter and connectivity entity.

## Device values — `<dev>/<topic>`

| | |
|---|---|
| Pattern | `<dev>/<canonical topic>` — e.g. `mbg/devices/sdm630_garage/power/active/total`; a register's `mqtt.topic` override replaces the leaf, otherwise the leaf is the register name lower-cased (`[`→`_`, `]` and `_g_` stripped) |
| Direction | gateway publishes |
| Retained / QoS | `mqtt.retain` / `mqtt.qos` |
| Payload | bare value: a number (floats rounded to 3 decimals, `1234.5`), or decoded text for enum/bitfield/status registers (`Running`). Never `nan`/`inf` — non-finite values are dropped |
| When | after each poll group read, per `mqtt.publish_mode` (+ heartbeat). Calculated registers (`status/text`, `status/alarm`, energy deltas…) follow the same path |
| Off switch | `devices[].mqtt.enabled: false` (whole device), per-register `mqtt.enabled: false` |
| Code | `multibus/mqtt_publisher.py` — `_build_topic`, `publish_register_data`, `publish_if_changed`; routed from `multibus/api.py` (`_on_poll_data`, ~line 626) and `multibus/calc_engine.py` |

A **held** sample — a monotonic-counter glitch, a day counter dipping at
sunset (`DailyCounterFilter`), a missing scale factor, an undecodable enum —
is not published at all; the retained topic keeps its last good value.

Cache semantics: on every broker (re)connect the change-detection cache is
cleared, so every value is republished once (a restarted broker lost its
retained state).

## Device liveness — `<dev>/availability`, `<dev>/runtime/*`

| Topic | Payload | Notes |
|---|---|---|
| `<dev>/availability` | `online` / `offline` | HA connectivity `binary_sensor` state |
| `<dev>/runtime/status` | `online` / `offline` | same verdict, legacy-collector leaf name |
| `<dev>/runtime/last_seen` | ISO timestamp of the last successful read (`2026-09-23T14:02:11.318452`, local time, no zone) | left untouched while no read succeeded yet |
| `<dev>/runtime/read_errors` | cumulative failed reads (`17`) | |

Direction: gateway publishes. Retained `true` (hard-wired), QoS `mqtt.qos`.
Evaluated every **5 s** by the health harvester, published **only on
change**; the verdict is data freshness (`ok`/`degraded` = online, `down` =
offline), not socket state. The primary publishes these too, under `<gw>`.
Code: `multibus/mqtt_publisher.py` — `publish_device_availability`,
`publish_device_runtime`; caller `multibus/api.py` (~line 855).

## Endpoint aggregates — `mbg/endpoints/<endpoint>/…`

| | |
|---|---|
| Pattern | `mbg/endpoints/<endpoint_id>/<canonical topic>` for the endpoint's first group; `mbg/endpoints/<endpoint_id>/<group_id>/<canonical topic>` for later groups. Override the root with `endpoints[].mqtt.aggregate_prefix` (`${endpoint_id}`, `${group_id}` substituted) |
| Census leaves | `…/units_online` (`3`), `…/units_total` (`4`), `…/status` (`online` / `partial` / `offline`) |
| Direction | gateway publishes |
| Retained / QoS | `mqtt.retain` / `mqtt.qos` |
| When | every **10 s**, through the same change detection as device values (so census leaves republish only on change, plus heartbeat); nothing is published for a group with fewer than 2 enabled units |
| Off switch | `endpoints[].aggregates: false` |
| Code | `multibus/endpoint_aggregator.py` — `aggregate_topic`, `EndpointAggregator._publish_mqtt` |

Sum/average/counter rules and the withheld-counter behaviour are described in
[config-reference.md](config-reference.md#endpoints--n-units-of-the-same-device-behind-one-endpoint).

## Virtual meters — `<gw>/vmeter/<id>/state`, `<gw>/data_health`

| | |
|---|---|
| Pattern | `<gw>/vmeter/<template_id>/state` |
| Direction | gateway publishes |
| Retained / QoS | yes / `mqtt.qos` |
| Payload | JSON health of the served Modbus TCP meter — no electrical data: `{"id": "site_meter", "name": "Site meter", "bind": "0.0.0.0", "port": 1502, "unit_id": 1, "registers": 42, "enabled": true, "running": true, "state": "serving", "connections": ["192.168.1.20:51234"], "conn_count": 1, "peers": "192.168.1.20", "requests": 18234, "req_rate": 2.1, "errors": 0, "bytes_rx": 1, "bytes_tx": 1, "last_fresh": 1758630131.2, "freshness_age_s": 0.8, "uptime_s": 86400, "last_error": null, "on_stale": "hold", "stale_after_s": 30, "max_hold_s": 300, "quality": {}, "failover": [], "ts": 1758630132}` |
| When | every **10 s**, unconditionally (no change detection). Cleared with an empty retained payload when the meter is removed or disabled |
| Code | `multibus/virtual_meter_manager.py` — `_publish_states`, `remove`, `set_enabled` |

`<gw>/data_health` is published on the same tick: the **primary** device's
acquisition health as JSON (`{"status": "ok", "stale": false,
"staleness_age_s": 1.2, "last_success_ts": 1758630131.0, "connected": true,
"ts": 1758630132}`), retained.

## Commands — `<dev>/cmd/<name>`

The command faces of a device's template (`multibus/commands.py`). Only
commands whose binding has `faces.mqtt: true` (default) get a topic.

| Topic | Direction | Retained / QoS | Payload |
|---|---|---|---|
| `<dev>/cmd/<name>` | gateway **subscribes** | — (retained deliveries are ignored; the gateway clears the topic on subscribe) | a bare number (`80` → `{"value": 80}`) or a JSON object of parameters; optional `source` names the caller in the audit trail; `command`, `device_id`, `ts` envelope keys are ignored |
| `<group cmd prefix>/cmd/<name>` | gateway subscribes | same | same payload, fans out to every unit of the group |
| `<dev>/cmd/result` | gateway publishes | no / **1** | `{"command": "power_limit", "status": "success", "params": {"value": 80}, "before": {...}, "after": {"WMaxLimPct": 80}, "ms": 412, "device": "fronius-u1", "via": "mqtt", "by": "node-red", "ts": 1758630132.418}`; a rejected payload answers `{"command": …, "status": "rejected", "reason": "…", "device": …, "via": "mqtt"}` |
| `<dev>/cmd/<name>/state` | gateway publishes | yes / **1** | the last applied command: `{"status": "success", "params": {"value": 80}, "after": {"WMaxLimPct": 80}, "ts": 1758630132.418, "via": "mqtt", "by": "node-red"}` |

`status` is one of `success`, `mismatch`, `unverified`, `rejected`, `error`.
The group prefix is the units' common parent when they sit at
`<parent>/<unit_id>` (e.g. `pv/inverters` for `pv/inverters/1`…`/4`),
otherwise `mbg/endpoints/<endpoint_id>/<group_id>`. Subscriptions are rebuilt
at boot, on every device/endpoint change and on reconnect.

Gates (all re-checked on every message, the broker is not trusted):
`security.allow_writes`, `mqtt.allow_write_entities`, a non-empty
`mqtt.username`, the command enabled with an MQTT face, the device not
`write_locked`, parameters inside their bounds. Commands run on a dedicated
worker (queue of 32; a flood drops, never backs up).

Code: `multibus/api.py` — `_sync_command_topics`, `_mqtt_command`,
`_publish_command_result`, `_group_cmd_prefix`; `multibus/mqtt_publisher.py`
— `register_command`, `_on_message`.

## Home Assistant write entities — `<dev>/<topic>/set`

When `mqtt.allow_write_entities` **and** `security.allow_writes` are on, a
non-primary Modbus device's writable holding registers are advertised as HA
`number` (or `select`, when the register has an enum map) entities with
`command_topic` = `<state topic>/set`. The gateway subscribes to it; the
payload is the number, or the option label for a select. Every write is
re-validated (writability, bounds, rate limit) and audited; a register that a
template command writes first (e.g. `WMaxLimPct`) is executed *through* that
command. Retained commands are ignored and cleared. Code: `multibus/api.py`
— `_mqtt_write_command`; `multibus/mqtt_publisher.py` — `_write_entity_config`.

## Rules — `mbg/rules/<rule_id>/…`

| Topic | Direction | Retained / QoS | Payload / when |
|---|---|---|---|
| `mbg/rules/<id>/state` | gateway publishes | yes / **1** | `{"id": "ov_limit", "label": "Overvoltage limit", "mode": "armed", "enabled": true, "state": "step1", "signal": 253.4, "units": {"fronius-u1": {"state": "step1", "want": {"value": 60}, "clamp": null, "paused_until": null, "last_action": "run", "ignored": false, "guarded": false}}, "decision": "run", "reason": "voltage_ln_avg 253.4 > 252.0", "ts": 1758630132.418}` — on change of anything but `ts`; `{}` when the rule is deleted |
| `mbg/rules/<id>/event` | gateway publishes | no / **1** | `{"rule": "ov_limit", "device": "fronius-u1", "state": "step1", "signal": 253.4, "want": {"value": 60}, "actual": 100, "action": "run", "reason": "…", "ts": 1758630132.418}` — on every per-unit state change |
| `mbg/rules/<id>/set` | gateway **subscribes** | — | `{"enabled": false}`, `{"clamp": {"max": 50, "expires_s": 3600}}`, `{"clamp": null}`, `{"override_s": 900}`, optional `"source"`. Arming (`mode`) is API/UI only |

Code: `multibus/rules_runtime.py` — `MQTT_ROOT`, `_publish_state`, `_event`,
`_sync_mqtt`, `_mqtt_set`, `delete`. The InfluxDB side of the same decisions
is the `rule_event` measurement ([influxdb-schema.md](influxdb-schema.md)).

## Power-quality events — `<dev>/pq/event`

| | |
|---|---|
| Direction | gateway publishes |
| Retained / QoS | yes / `mqtt.qos` |
| Payload | `{"start": 1758630131.42, "end": 1758630131.61, "duration_ms": 190.0, "bound": 207.0, "vmax": 231.2, "vmin": 188.4, "vavg": 210.7, "causes": [{"cause": "under_voltage_ln", "channel": "L1"}]}` |
| When | once per **new** event found in the meter's recorder ring (`pq_recorder.poll_s`); the retained topic always shows the latest event |
| Knob | `pq_recorder.enabled` (per device; Janitza/Jasic firmware only) |
| Code | `multibus/pq_recorder.py` — `_announce` |

## Alerts — `<gw>/alert`

| | |
|---|---|
| Direction | gateway publishes |
| Retained / QoS | **no** / `mqtt.qos` |
| Payload | `{"ts": 1758630132.4, "severity": "warn", "source": "Garage SDM630", "message": "device down", "key": "dev:sdm630_garage", "host": "gateway"}` — `severity` is `info` / `warn` / `error` |
| When | on each alert (device down/recovered, sink lost, latency, buffer backlog, thresholds, rule failures, PQ events), rate-limited per `key` by `alerts.min_interval_s` |
| Knobs | `alerts.enabled`, `alerts.mqtt` |
| Code | `multibus/alerts.py` — `AlertManager.fire`, `_publish_mqtt` |

## Home Assistant discovery — `<ha>/…/config`

All retained, QoS `mqtt.qos`, JSON per the HA MQTT discovery schema.
Published on every (re)connect, when a device is added or edited, and (virtual
meters) about every 5 minutes. Configs that are no longer wanted are deleted
by publishing an **empty retained payload**; a failed delete is retried on the
next republish. Knobs: `mqtt.ha_discovery.*`, `devices[].mqtt.ha_discovery`.

| Topic | Component | `unique_id` | Availability |
|---|---|---|---|
| `<ha>/sensor/multibus/<addr>_<name>/config` (primary) | `sensor` | `janitza_umg512_<addr>_<name>` | `<gw>/status` |
| `<ha>/<component>/mbg_dev_<id>/<addr>_<name>/config` (other devices) | `sensor`, or `number` / `select` for write entities | `mbg_dev_<id>_<addr>_<name>` | `<dev>/availability` |
| `<ha>/binary_sensor/mbg_dev_<id>/connectivity/config` | `binary_sensor` (`connectivity`, diagnostic) | `mbg_dev_<id>_connectivity` | `<gw>/status` |
| `<ha>/<component>/janitza_vmeter/<id>_<key>/config` | one `binary_sensor` + 8 `sensor`s reading `<gw>/vmeter/<id>/state` with `value_template` | `janitza_vmeter_<id>_<key>` | `<gw>/status` |

`<name>` is the register name lower-cased with `[`→`_`, `]` removed (and
`_g_` removed for non-primary devices). Device blocks: the primary is
`identifiers: ["janitza_umg512"]`; every other device is
`identifiers: ["mbg_dev_<id>"]`, `via_device: janitza_umg512`. `device_class`
/ `state_class` come from the register's explicit HA typing or from the unit
heuristic (`apply_ha_typing`); text registers carry neither.
Code: `multibus/mqtt_publisher.py` — `publish_ha_discovery`,
`publish_device_discovery`, `publish_vmeter_discovery`.

## What the gateway subscribes to

| Subscription | Purpose | Code |
|---|---|---|
| `<dev>/cmd/<name>`, `<group>/cmd/<name>` | template commands (above) | `multibus/api.py` |
| `<dev>/<topic>/set` | HA write entities (above) | `multibus/mqtt_publisher.py` |
| `mbg/rules/<id>/set` | rule control (above) | `multibus/rules_runtime.py` |
| MQTT **device sources** (`connection.protocol: mqtt`) | a separate paho client per device, on `connection.broker`: subscribes to `connection.topic` and to every register's own `topic` (`+`/`#` wildcards allowed). A register with `json_path` extracts from a JSON payload; without one the whole payload must be a number. **Retained deliveries are dropped** unless `accept_retained: true` — a retained value has no trustworthy measurement time | `multibus/mqtt_input.py` |
| commissioning probes | short-lived subscriptions while the UI samples a topic or discovers a source | `multibus/discovery.py`, `multibus/routes/diagnostics.py` |

## Compatibility aliases

`mqtt.compat_aliases` republishes, with the same payload/retain/QoS, every
topic that starts with `from` under `to`, renaming individual tails through
`leaves`. It covers everything routed through `MQTTPublisher._publish`
(values, liveness, aggregates, vmeter state, PQ, alerts, discovery) but not
the QoS-1 direct publishes (`status`, `cmd/result`, `cmd/<name>/state`,
`mbg/rules/*`). Alias delivery is counted separately (`messages_aliased`,
`aliases_failed` in `/api/status`). Remove an entry once nothing subscribes to
the old prefix — and clear its retained topics (below).

## Subscribing from Node-RED / Home Assistant / another gateway

Minimal wildcard subscriptions (assuming the default `mbg/…` patterns and
`<gw>` = `multibus/umg512`):

| Want | Subscribe to |
|---|---|
| every device value + liveness | `mbg/devices/#` (plus `<gw>/#` for the primary) |
| one quantity across devices | `mbg/devices/+/power/active/total` |
| endpoint totals | `mbg/endpoints/#` |
| is the gateway alive | `<gw>/status` (retained; `offline` arrives via LWT) |
| device up/down | `mbg/devices/+/availability` |
| alerts | `<gw>/alert` |
| rule decisions | `mbg/rules/+/state`, `mbg/rules/+/event` |
| command outcomes | `mbg/devices/+/cmd/result`, `mbg/devices/+/cmd/+/state` |
| virtual-meter health | `<gw>/vmeter/+/state` |

Home Assistant needs no subscription of its own: enable
`mqtt.ha_discovery`, point HA at the same broker and the entities appear.
Another gateway can consume this one as an **MQTT device source**
(`connection.protocol: mqtt`, one register per topic) — leave `accept_retained`
off, so a stale retained value is not laundered as fresh.

Treat every payload here as data: values are bare numbers or text, everything
else is JSON. Do not act on a retained command echo; the gateway itself never
does.

## Retained-topic cleanup on removal

What the gateway clears by itself:

| Event | Cleared (empty retained payload) |
|---|---|
| device deleted | `<ha>/sensor/mbg_dev_<id>/*/config` for its MQTT-enabled registers (`multibus/api.py`, `_teardown_device`) |
| register removed/disabled, device edited | its discovery config, on the next discovery republish |
| virtual meter removed or disabled | `<gw>/vmeter/<id>/state` |
| rule deleted | `mbg/rules/<id>/state` (published as `{}`) |
| `mqtt.topic_prefix` changed | old `<gw>/status` |

What stays behind and must be cleared by hand (the gateway does not track
it): a deleted device's **value topics**, `availability`, `runtime/*`,
`cmd/<name>/state`, `pq/event`, its `number`/`select` discovery configs and
its `binary_sensor/…/connectivity` config; an endpoint's `mbg/endpoints/<id>/#`;
anything published under a retired `compat_aliases` prefix. With mosquitto:

```sh
# list what is retained under a prefix
mosquitto_sub -h gateway.example.lan -u user -P pass -v --retained-only -W 3 -t 'mbg/devices/sdm630_garage/#'
# clear one topic (empty retained payload)
mosquitto_pub -h gateway.example.lan -u user -P pass -r -n -t 'mbg/devices/sdm630_garage/availability'
# clear every retained topic under a prefix
mosquitto_sub -h gateway.example.lan -u user -P pass --retained-only -W 3 -t 'mbg/devices/sdm630_garage/#' -F '%t' \
  | while read -r t; do mosquitto_pub -h gateway.example.lan -u user -P pass -r -n -t "$t"; done
```

Never clear `<dev>/cmd/<name>` with a *non-empty* payload: the gateway
ignores retained commands, but another subscriber might not.

---

Verified against `multibus/__init__.py` `__version__ = "3.80.1"`.
