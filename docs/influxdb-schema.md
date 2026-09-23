# InfluxDB schema

What the gateway writes to InfluxDB 2.x and how to query it: buckets,
measurement naming, tags, fields, timestamps, write cadence, the
store-and-forward buffer, and the feature measurements (`rule_event`,
`endpoint`, `pq_*`). Everything below is derived from
`multibus/influxdb_publisher.py` (the one line-protocol schema — the backfill
tool reuses its `build_point`, so offline writes land in byte-identical
series) and the feature modules named per section. Configuration keys are in
[config-reference.md](config-reference.md); the canonical field names in
[canonical-fields.md](canonical-fields.md).

## Buckets

| Writer | Bucket | Knob |
|---|---|---|
| primary device | `influxdb.bucket` (default `multibus`) | Config → InfluxDB |
| every other device | `devices[].influxdb.bucket`; seeded for a **new** device from `influxdb.default_bucket_pattern` (`{device}` = device id, default → the device id itself) | fixed once persisted |
| endpoint units | `endpoints[].influxdb.bucket` with `${unit_id}` / `${device_id}` / `${endpoint_id}` substituted per unit | |
| endpoint aggregates | the same key with every placeholder resolved to the **endpoint id**; no key → a bucket named after the endpoint id (never the global default) | `endpoints[].aggregates` |
| rule events | the target unit's bucket | |
| PQ recorder | `pq_recorder.bucket` if set, else the device's bucket | `pq_recorder.bucket` |

A missing bucket is created on first use with a **90-day** `expire`
retention (`InfluxDBPublisher.ensure_bucket`); an existing bucket is never
altered, so set retention yourself for anything that must outlive 90 days.
Per-device `influxdb.enabled: false` stops that device's points, its
aggregates and its rule events.

## Register points (the telemetry schema)

One point per register per write, built by `build_point()`:

| Part | Value | Source |
|---|---|---|
| measurement | see below | `get_measurement()` |
| tag `device` | `devices[].influxdb.device_tag` (default: the device id); the primary writes the fixed tag `janitza_umg512` | `get_tags()` |
| tag `address` | the register address as a string (`"19026"`) | |
| tag `name` | the register name exactly as selected (`power_active_total`) | |
| tag `poll_group` | the poll group that produced the read (`realtime`, `normal`, `slow`, or a per-device group); calculated registers carry their own group (`normal` if unset) | `write_register_data` |
| extra tags | per-register `influxdb.tags` | |
| field `<field name>` | the value as **float**; the field name is the register name lower-cased, `[`→`_`, `]` removed, a leading `_g_` stripped — for a canonical name this is the name itself | |
| field `value` | the same float, duplicated under a fixed name so a query can address any register uniformly (`r["_field"] == "value"`) | |
| timestamp | the **poll time** of the read, nanosecond precision (`WritePrecision.NS`) — not the flush time, so batching latency and outage replay never skew a series | |

A **text** value (enum label, status text) is written as a string in the
named field only, without the `value` twin. Non-finite numbers are skipped.

### Measurement naming

`get_measurement()` decides in this order:

1. the register's own `influxdb.measurement` override;
2. the canonical dictionary (`canonical_fields.measurement_for`): a canonical
   name maps to its family — `voltage`, `current`, `power_active`,
   `power_reactive`, `power_apparent`, `power_factor`, `frequency`,
   `energy_active`, `energy_reactive`, `energy_apparent`, `thd`,
   `diagnostic`, `dc`, `mppt`, `temperature`, `status` (the 16 measurements
   of [canonical-fields.md](canonical-fields.md));
3. a unit heuristic for non-canonical names, most specific first: `varh` →
   `energy_reactive`, `wh` → `energy_active`, `var` → `power_reactive`,
   `va` → `power_apparent`, `hz` → `frequency`, `w` → `power_active`, `v` →
   `voltage`, `a` → `current`, `%` → `percentage`;
4. otherwise `janitza`.

So `voltage_l1_n` from any device lands in measurement `voltage`, field
`voltage_l1_n` (and `value`), tagged with that device's `device` tag — a
dashboard filters by tag, never by bucket layout.

## Write cadence, change detection, batching

| Mechanism | Behaviour | Knob |
|---|---|---|
| minimum interval | a given (device, address) is written at most once per `write_interval` seconds, whichever poll group read it | `influxdb.write_interval` (5) |
| change detection | in `changed` mode an unchanged value (exact equality on the raw value) is skipped | `influxdb.publish_mode` (`changed` / `all`) |
| batching | the client batches 100 points or 10 s (2 s jitter), retries a failed batch with backoff up to 5 min, then hands it to the replay buffer | fixed |
| delivery truth | `/api/status` reports `writes_confirmed` / `last_confirm_age_s` (server acknowledgement) next to `writes_total` (enqueue); `auth_failed` flags 401/403/404 | |

Consequences for queries: a steady reading produces **no points** until it
changes; a series with one point per hour is a still meter, not an outage.
Use `last()` / `fill(usePrevious: true)` rather than `count()` to judge
liveness, and never `mean()` over a raw window assuming even sampling.

### Held samples are not written

A sample that the value pipeline **holds** never reaches any sink: the
`MonotonicFilter` (a counter regressing, or jumping implausibly, until
confirmed), the `DailyCounterFilter` (a `daily: true` counter reading lower
than the day's maximum — a Solar API `E_Day` that drops as inverters fall
asleep — held until the midnight reset below 2 % of the maximum), a missing
scale factor, an undecodable enum. The register is simply absent from that
poll (`multibus/value_decode.py` `apply_corrections`; `multibus/counter_filter.py`).
In InfluxDB this looks the same as change detection: the last written point
stands until a new accepted value arrives. A day counter therefore shows its
evening plateau as one point followed by nothing until the next morning; the
midnight reset is written the moment it is adopted.

## Store-and-forward buffer (outages and restarts)

Points that cannot be delivered — InfluxDB down, batch retries exhausted,
token rotated, bucket missing — go to a replay buffer as `(poll timestamp,
bucket, line protocol)` and are replayed **with their original timestamps**
when the connection returns. InfluxDB dedupes on (measurement, tags,
timestamp), so replay is idempotent.

| Knob / file | Default | Meaning |
|---|---|---|
| `influxdb.buffer_minutes` | 120 | age window, measured from the **newest buffered point** (a window of data, not of wall time) |
| `influxdb.buffer_max_points` | 200 000 | hard cap, drop-oldest |
| `influxdb.buffer_persist` | `true` | snapshot the buffer to disk on the 30 s monitor tick while it grows, and at shutdown |
| `INFLUX_BUFFER_PATH` | `config/influx_buffer.jsonl` (next to `config.yaml`) | one JSON array `[ts, bucket, line]` per line; loaded at boot, pruned by the same age window, deleted once drained |

Replay drains in chunks of up to 5000 points grouped by bucket via a
synchronous write. A chunk rejected with HTTP 400/422 (malformed line,
field-type conflict) is dropped and logged; 401/403/404 and transport errors
re-buffer and stop the drain until the next tick. Whenever points are
dropped, the change-detection cache is invalidated so a still-steady value is
written again and the gap closes. Stats: `buffer_points`, `buffered_total`,
`replayed_total`, `dropped_total`, `recovered_total` in `/api/status`.

## `rule_event` — decisions of the rules engine

Written by `multibus/rules_runtime.py` `_influx_event` into the **target
unit's bucket**, on every state/want change and every command the rule sends
(`run`, `reassert`).

| Part | Value |
|---|---|
| measurement | `rule_event` |
| tags | `device` (the unit's device tag), `rule` (rule id), `state` (`normal`, `stale`, a step label…), `action` (`run`, `reassert`, `shadow`, `hold`, `stale`, `idle`, `ignored`, `sweep`), `result` (`success`, `mismatch`, `rejected`, `error`, or empty when nothing was sent) |
| fields | `reason` (string, ≤200 chars, always), `signal` (float, the evaluated signal), `want_value` (float, the commanded parameter), `actual` (float, the read-back), `result_reason` (string, when the command failed) |
| timestamp | the decision time, ns |

## `endpoint` and aggregated measurements

`multibus/endpoint_aggregator.py` writes an endpoint's combined values every
10 s (on change only in `changed` mode; no `write_interval` gate) into the
endpoint's bucket:

| Part | Value |
|---|---|
| measurement | the canonical measurement of the field (`power_active`, `energy_active`, …); `endpoint` for the census fields |
| tags | `device` = **endpoint id**, `aggregate` = `endpoint`; `group` = group id on every group but the first (the first group's series predate groups and stay untagged) |
| fields | the canonical name (`power_active_total`, `energy_active_import`…) plus `units_online`, `units_total` (floats). **No `value` twin, no `address`/`name`/`poll_group` tags** — filter these by `aggregate == "endpoint"` |
| not written | `status` (text; MQTT only) |

## PQ recorder measurements

`multibus/pq_recorder.py` archives a Janitza/Jasic meter's on-device
power-quality recorder into `pq_recorder.bucket` (else the device bucket),
tag `device` = the device's tag. Timestamps are **millisecond** precision.

| Measurement | Tags | Fields | Timestamp |
|---|---|---|---|
| `pq_events` | `cause` (`over_voltage_ln`, `under_voltage_ln`, `voltage_outage_ln`, `over_current`, `over_voltage_ll`, `under_voltage_ll`, `voltage_outage_ll`, `rapid_voltage_change_ln`, …), `channel` (`L1`…`L4` for L-N causes, `L1-L2`, `L2-L3`, `L3-L1` for L-L) | `duration_ms`, `bound`, `vmax`, `vmin`, `vavg` (floats), `reason_lo`, `reason_hi` (ints) | event start — one point per cause+channel; re-reading the ring overwrites in place |
| `pq_counters` | — | `evt_count`, `flag_count`, `trans_count` (ints, lifetime) | poll time (`pq_recorder.poll_s`) |
| `pq_waveforms` | `event` (event start in ms, as a string), `channel` | `value` (half-wave RMS, 10 ms steps) | sample time |

## Flux examples

All examples assume bucket `multibus`; substitute the device's bucket. The
`device` tag values are the ones you configured (`sdm630_garage` below).

Last value of every field of one device:

```flux
from(bucket: "multibus")
  |> range(start: -1h)
  |> filter(fn: (r) => r["device"] == "sdm630_garage" and r["_field"] == "value")
  |> last()
  |> keep(columns: ["_time", "_measurement", "name", "_value"])
```

Phase voltages, 1-minute means:

```flux
from(bucket: "multibus")
  |> range(start: -6h)
  |> filter(fn: (r) => r["_measurement"] == "voltage" and r["device"] == "sdm630_garage")
  |> filter(fn: (r) => r["_field"] == "voltage_l1_n" or r["_field"] == "voltage_l2_n" or r["_field"] == "voltage_l3_n")
  |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
```

Daily energy from a lifetime counter (`spread` = max − min inside each day;
valid because the counter never regresses — the monotonic filter holds
glitches back):

```flux
import "timezone"
option location = timezone.location(name: "Europe/Bucharest")
from(bucket: "multibus")
  |> range(start: -30d)
  |> filter(fn: (r) => r["name"] == "energy_active_import" and r["_field"] == "value" and r["device"] == "sdm630_garage")
  |> aggregateWindow(every: 1d, fn: spread, createEmpty: false)
  |> map(fn: (r) => ({ r with _value: r._value / 1000.0 }))   // Wh → kWh
```

A day with no point at all is a day the counter did not change (or was
entirely held), not a missing day; `spread` yields nothing for it. For the
exact month total the UI uses `last − first` over the month
(`energy_report` in `influxdb_publisher.py`).

Endpoint totals for a plant (aggregates only, not its units):

```flux
from(bucket: "fronius")
  |> range(start: -1d)
  |> filter(fn: (r) => r["aggregate"] == "endpoint" and r["device"] == "fronius" and not exists r["group"])
  |> filter(fn: (r) => r["_field"] == "power_active_total" or r["_field"] == "units_online")
```

Rule events of one rule, newest first:

```flux
from(bucket: "fronius")
  |> range(start: -7d)
  |> filter(fn: (r) => r["_measurement"] == "rule_event" and r["rule"] == "ov_limit")
  |> pivot(rowKey: ["_time", "device", "state", "action", "result"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 100)
```

Steady-state series filled forward (a value that did not change was not
written — carry it instead of leaving `null`):

```flux
from(bucket: "multibus")
  |> range(start: -24h)
  |> filter(fn: (r) => r["name"] == "power_active_total" and r["_field"] == "value" and r["device"] == "sdm630_garage")
  |> aggregateWindow(every: 1m, fn: last, createEmpty: true)
  |> fill(usePrevious: true)
```

Gaps that are real (no accepted read for longer than the fastest poll group
could explain — here 10 minutes, on a register that changes constantly):

```flux
from(bucket: "multibus")
  |> range(start: -7d)
  |> filter(fn: (r) => r["name"] == "power_active_total" and r["_field"] == "value" and r["device"] == "sdm630_garage")
  |> elapsed(unit: 1s)
  |> filter(fn: (r) => r.elapsed > 600)
  |> keep(columns: ["_time", "elapsed"])
```

PQ events of the last month with their waveform key (`event` tag of
`pq_waveforms` = the event's `_time` in ms):

```flux
from(bucket: "multibus")
  |> range(start: -30d)
  |> filter(fn: (r) => r["_measurement"] == "pq_events" and r["device"] == "janitza_umg512")
  |> pivot(rowKey: ["_time", "cause", "channel"], columnKey: ["_field"], valueColumn: "_value")
  |> map(fn: (r) => ({ r with event: string(v: int(v: r._time) / 1000000) }))
  |> sort(columns: ["_time"], desc: true)
```

---

Verified against `multibus/__init__.py` `__version__ = "3.80.1"`.
