# Virtual Meter — wire-level specification

The behavioural contract a downstream Modbus consumer can rely on. UI-level
usage lives in the README; this file pins the wire semantics.

## Serving model (summary)

- One TCP server per instance (port from the published range), FC3/FC4 reads.
- The datastore spans ONLY the emulated meter's map: reads below/above it
  answer *illegal data address*, exactly like the real meter being emulated
  (consumers such as the Fronius DataManager probe low addresses to identify
  meter models — a 0 there would mis-identify us).
- Staleness policies per instance: `legacy` (single watchdog, gaps keep last
  words), `fail` (exception on stale rows), `sentinel` (SunSpec N/A words),
  `hold` (bounded, then fail). Absence is never encodable as a plausible
  measurement.
---

## In-band quality block (gateway convention, v1)

**Problem it solves:** a downstream Modbus consumer (PLC, SCADA, GX device)
reading a virtual meter has no way to know whether the values it just read
are fresh, held, or synthesized — that knowledge lives in MQTT/UI land. The
quality block puts it **in-band**, on the same Modbus connection.

**Enabling:** per instance — UI → Virtual Meters → add/edit → *"In-band
quality block"* checkbox, or `quality_block: true` on the instance in
`virtual_meters.yaml`. **Default: off** (existing consumers see a
byte-identical meter). If the template's own map overlaps the block, the
block is refused and a warning is logged.

**Layout — identical for every virtual meter** (read-only, FC3, big-endian,
no floats; one layout to learn regardless of the emulated meter):

| Address | Type | Meaning |
|--------:|------|---------|
| 61440   | u16  | format version (`1`) |
| 61441   | u16  | state: `0` legacy (per-register quality not judged), `1` ok (all rows fresh), `2` degraded (some stale/held/missing), `3` stale (no fresh row) |
| 61442   | u16  | fresh rows |
| 61443   | u16  | stale rows |
| 61444   | u16  | missing rows |
| 61445   | u16  | total data rows (consts excluded) |
| 61446   | u32  | age of the newest fresh value, seconds (`0xFFFFFFFF` = never had one) |

**Reading it:** one FC3 read of 8 registers at 61440. Addresses between the
meter map and 61440 still answer *illegal data address* (the real-meter
emulation used for consumer probing is preserved — the datastore has two
islands, not one stretched block).

**Consumer recipe (typical PLC guard):** accept the meter data only while
`state == 1` (or `state <= 2` with your own age bound via 61446); alarm on
`state == 3`. With `on_stale: legacy` the state reads `0` — per-register
quality is only judged by the fail/sentinel/hold policies, so pair the block
with one of those for composites.

**Versioning promise:** the layout above never changes meaning within
version `1`; new fields may APPEND (61448+). A consumer should check 61440
once at commissioning.

## Register source kinds

A register's `source:` binds it to live data. Besides the single-source forms
(`live: <name>`, `const: <n>`, `const_str: <s>`) and `sum: [<names>]`, a row may
declare **redundant sources**:

```yaml
- { addr: 0x0028, type: int32, scale: 10,
    source: { failover: ["_G_P_SUM3", "fronius.power_active_total"] } }
```

* **`failover` (alias `combined`)** — an *ordered* candidate list. Each rebuild
  the meter serves the **first candidate that is fresh** (within that row's
  freshness bound); a candidate that is missing or stale is skipped in order.
  The primary is preferred whenever it is fresh, so recovery switches back
  automatically. If **no** candidate is fresh the row degrades through the
  meter's `on_stale` policy exactly as a single stale source would (under
  `fail` the read is refused — never a silently-stale value into the ESS).
  A change of the serving source logs one event (`warn` on drop to a
  lower-priority source, `info` on recovery) for the Status page / alerting.

This is the reliability guard for a control feed: bind the ESS grid-power row to
the accurate primary meter with a second meter (or a derived value) as backup,
and a single source outage no longer stops the meter.
