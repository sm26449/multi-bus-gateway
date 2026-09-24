# Virtual Meter — wire-level specification

The behavioural contract a downstream Modbus consumer can rely on. UI-level
usage lives in the README; this file pins the wire semantics.

## Serving model (summary)

- One TCP server per instance, FC3/FC4 reads only. The port must be unique
  across instances and inside the published range (`VMETER_PORT_START` /
  `VMETER_PORT_END`, default 1502–1512, plus 502 if mapped).
- **Writes are always refused** (Modbus exception), and FC1/FC2
  (coils/discrete) are refused too — the block serves registers, never bits.
- The datastore spans the emulated meter's map **plus a 4-register pad above
  the top address** (the pad reads `0`); everything below the map base — and
  the void between the map and the quality block — answers *illegal data
  address*, exactly like the real meter being emulated (consumers such as
  the Fronius DataManager probe low addresses to identify meter models — a
  `0` there would mis-identify us).
- Staleness policies per instance:
  - `legacy` — a **per-row fail-closed watchdog**: every resolving data row
    is judged against its freshness bound; ONE stale row stops the whole
    server (individual reads are never refused). Gaps keep last words only
    for rows that resolved at least once; a row that **never** resolved
    (renamed source, deselect, typo) withholds the meter with an
    `unresolved` event — the zero-seeded block is never served.
  - `fail` — a read touching a stale row answers **Modbus exception 2
    (illegal data address)** — deliberately the same code as an unmapped
    probe, so treat any exception as "do not use this data".
  - `sentinel` — SunSpec N/A words (table below).
  - `hold` — last words up to `max_hold_s`, then like `fail`.
  Absence is never encodable as a plausible measurement.
- **All data stale ⇒ the server stops responding** (socket closed): the
  consumer's own meter-loss fail-safe must engage. It resumes automatically
  when a source is fresh again.
- **Freshness** is judged on a monotonic clock (a wall-clock/NTP step can
  never fake it; a source without a monotonic stamp is *not fresh*), against
  a per-row bound cascade: explicit row `stale_after_s` → the source
  device's own bound → 2.5× the producing poll group's interval (capped at
  300 s) → the instance `stale_after_s`. Derived bounds only ever *relax*
  the instance bound, never tighten it.

### Sentinel words (`on_stale: sentinel`)

| Type | N/A value on the wire |
|---|---|
| float / double | IEEE NaN |
| int16 / short | `0x8000` |
| uint16 | `0xFFFF` |
| int32 / int64 | type minimum (`0x8000_0000` …) |
| uint32 / uint64 | all-ones (`0xFFFF_FFFF` …) |
| sm16 / sm32 | all-ones = maximum negative magnitude (−32767 / −(2³¹−1)) — deliberately **not** "negative zero", which would decode as a plausible 0 |
| string | NUL bytes |

Integer sentinels are legal values to a non-SunSpec consumer — the gateway
logs a warning when `sentinel` is enabled on a map with integer rows;
prefer `fail` for integer-only maps.
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
| 61446   | u32  | age of the newest fresh value, seconds (`0xFFFFFFFF` = never had one). In `legacy` mode this is the newest *stamped* value's age (quality is not judged there) |

**Reading it:** one FC3 read of 8 registers at 61440. Addresses between the
meter map and 61440 still answer *illegal data address* (the real-meter
emulation used for consumer probing is preserved — the datastore has two
islands, not one stretched block).

**Consumer recipe (typical PLC guard):** accept the meter data only while
`state == 1` (or `state <= 2` with your own age bound via 61446); alarm on
`state == 3`. With `on_stale: legacy` the state reads `0` **and the three
count words (61442–61444) read 0** — per-register quality is only judged by
the fail/sentinel/hold policies, so pair the block with one of those for
composites.

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

* **`sum`** — the arithmetic sum of the named sources. It is **never
  partial**: any missing member makes the whole row missing. Its timestamp
  is the newest member's in `legacy` mode but the **oldest** member's in
  policy modes (the quality of a sum is its worst input), and its freshness
  bound is the tightest member bound.
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

## Instance-level redundant source device (`device_fallback`)

One field on the **instance** makes the *whole* meter fail over per register —
no per-register template edits, because canonical field names are identical
across devices (what's on one is on the other, if it exists):

```yaml
instances:
  - template: em24_av53
    port: 1502
    unit_id: 1
    device: grid_meter          # primary source device
    device_fallback: grid_b     # twin device — per-register failover
    on_stale: fail
```

Semantics (`add_instance` / `update_instance`; UI: the Add/Edit instance modal's
*Secondary source device (failover)* dropdown):

* **At start**, every bare-name `live` register of the template is rewritten
  into the ordered failover pair `[name, <fallback>.name]` and resolved by the
  same failover engine described above (first fresh wins, auto-recovers to the
  primary). Registers that are `const`, `sum`, already `failover`, or an
  explicit `device.register` source are left untouched — and so are
  **cumulative counters** (`energy_*` fields): a twin is a different physical
  meter, so its lifetime total would be a non-monotonic jump; counter rows
  are *pinned* to the primary instead (below).
* **Validation** — at save time the fallback must be a *known* device id and
  must differ from the source device (rejected with an error otherwise). At
  start it is re-checked: a mis-set value is **ignored with a warning** — a bad
  fallback never blocks a control-critical meter from starting. A
  configured-but-offline twin validates and *arms*; failover engages the moment
  the twin publishes.
* **Staleness composes unchanged for failover pairs** — a pair only ever
  serves a fresh candidate; when neither is fresh the row degrades through
  the instance's `on_stale` policy exactly like a single stale source.
* **Pinned counter rows are the one exception**: an `energy_*` row carries
  `pin_on_stale` and, once it has resolved at least once, **freezes at its
  last good words in every policy** — no exception under `fail`, no NaN
  under `sentinel`, no `max_hold_s` cap under `hold`, and the `legacy`
  all-fresh gate ignores it. A frozen counter is a true statement ("energy
  delivered so far"); it resumes with a legitimate forward jump when the
  primary returns. A pinned row that NEVER resolved still fails loudly
  (`unresolved`).
* **Observability** — a start-time coverage log lists which fields the twin
  already publishes vs. which are armed/waiting; every runtime switch is logged
  to the event ring and the process log (`warn` on drop to a lower-priority
  source, `info` on recovery), naming meter, register and both sources.

## Published state (`<mqtt prefix>/vmeter/<id>/state`, retained)

Beyond health/throughput, the retained state document carries the staleness
policy and its bounds — `on_stale`, `stale_after_s`, `max_hold_s` — the last
rebuild's per-register **quality** counts (`{fresh, stale, missing}`), and the
live **failover routing**: one entry per failover register with
`{addr, candidates, active, on_primary}` (`active` is `null` until the row
first binds; `on_primary` is true only once bound to the first candidate).
Note `active` names the candidate the resolver last *selected* — fresh, or
the highest-priority stale one when none is fresh — so under `fail` a row
can report an `active` source while its reads are being refused. Pinned
counter rows do not appear in the failover routing (they never fail over).
An external monitor can therefore see *how* a meter degrades and which
redundant source is feeding it — not just that it went stale. The same fields
appear in the instance's `status()` via the REST API.

## HTTP/JSON view (`/api/virtual-meters/<id>/values`)

The same map served as JSON, under the aggregator convention — safe for a
SCADA to read in one poll:

- `value` is **`null` for absence** (never 0/false); a stale row carries its
  `last_value` / `last_ts` separately.
- Each row has `quality`: `good` | `stale` | `missing` | `const`, plus
  `age_s`.
- The document-level `complete` flag is true only when every data row is
  good; `stale_fields[]` lists the offenders.

## Transport options (template `transport:`)

| key | default | meaning |
|---|---|---|
| `port` | `1502` | TCP port the meter listens on (the instance overrides it) |
| `bind` | `0.0.0.0` | interface to bind; `127.0.0.1` keeps the meter host-local |
| `unit_id` | `1` | the unit id shown to the operator and, with `strict_unit_id`, enforced |
| `strict_unit_id` | `false` | `true` → only the configured unit id is answered; every other id gets the gateway-path exception. The default answers any id (a mis-addressed consumer still gets data — the historical behaviour) (3.83.0) |
| `max_connections` | `16` | client connections per meter; the surplus is closed on the next tick, so a peer keeping sockets open cannot exhaust file descriptors (3.83.0) |
| `idle_timeout_s` | `300` | a connection with no request for this long is closed (`0` disables) |

The `hold` policy's window starts when a row goes **stale** (its stamp plus
its freshness bound), not at the stamp itself, so a row whose bound exceeds
`max_hold_s` still holds for the full window (3.83.0).
