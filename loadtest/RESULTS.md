# MBG load-test results

## §R Re-run — v3.35.3 (2026-08-16)

Re-validation of the 2026-08-01 campaign on the current version, after the
pymodbus 3.15 / serving-core rewrite (3.24.x) and the 3.25–3.35 hardening
series. Same isolated stack (`docker-compose.loadtest.yml`, 2 CPU / 1 GiB,
`nofile` 1024), rebuilt from the current tree. Differences vs the original
campaign, stated up front:

- **Sim fleet is EM24-shaped**, not Janitza-shaped: 50 units, 16 registers
  each via the `carlo_gavazzi_em24` template (2 poll groups, ~1 s effective
  poll rate) — vs 67 regs / 3 groups / 0.25 s realtime in §6.1. Per-device
  cost coefficients therefore differ from the historical model (fewer
  threads/FDs per device); the historical Janitza numbers stay valid for
  that shape.
- **MQTT publishing was ON** the whole time (per-device topics + vmeter
  state to the throwaway mosquitto) — the original ramps ran with MQTT off,
  so this run *includes* the publish cost. InfluxDB off, as before.
- **Vmeters honour the one-instance-per-template rule** (new since 3.2x):
  the seeder now clones `em24_av53` into `lt_em24_av53_NN` templates, one
  instance each, each sourcing a **distinct seeded device** (round-robin),
  not the primary.

### §R.1 Composed ramp (devices + vmeters together)

90 s steady-state collection at each level (24 samples @ 3 s):

| level | devices | vmeters | CPU avg (max) | RAM | threads | FDs | worst dev staleness | worst vm freshness | P0 |
|---|---:|---:|---|---:|---:|---:|---:|---:|---|
| L1 | 10+primary | 4 | 3.0% (9.6%) | 77 MiB | 40 | 103 | 0.6 s | 1.6 s | 4/4 every sample |
| L2 | 25+primary | 8 | 5.8% (14.4%) | 92 MiB | 78 | 224 | 1.0 s | 1.4 s | 8/8 every sample |
| L3 | 50+primary | 12 | 9.5% (12.4%) | 111 MiB | 136 | 415 | 1.0 s | 1.6 s | 12/12 every sample |

Scaling is linear and cheap: **threads ≈ 10 + 2·dev + 2·vm** (EM24 template
= 2 poll groups → 2 poller threads/device; the historical 3/device was the
3-group Janitza shape), **FDs ≈ ~7/device + ~4/vmeter + base**, RAM
≈ 0.65 MiB/device. Staleness is flat vs N (1.0 s tail at 51 devices = the
~1 s poll cadence, not lag). MQTT publishing included at every level.

### §R.2 Client swarm — 200 clients @ 250 ms over 12 vmeter ports

150 s sustained: **118,951 reads (790/s), 0 errors, 0 connect failures**;
latency p50 0.9 / p95 7.5 / **p99 15.8** / max 54 ms, conn-setup p99 130 ms.
Container during load: CPU avg 24.5% (max 34%), RAM 117 MiB, FDs 615
(415 + 200 clients — additive, as the historical model predicts), threads
constant 136. **P0 held: 12/12 vmeters fresh (≤2.0 s) and worst device
staleness 1.0 s in every sample** — client load does not starve the pollers.

### §R.3 Flap — 30 clients reconnecting every cycle

90 s: **7,622 clean connects (85/s), 0 failures**, p99 7.6 ms, conn-setup
p99 110 ms. RAM flat (117.0–118.0 MiB), FDs oscillated 415–443 and returned
to 415, threads constant. The connection path still does not accumulate.

### §R.4 Failure injection — source loss under load (ESS-critical)

12 vmeters live, sim killed at t≈31 s, restored ~20 s after full-stale:

| t | phase | observed |
|---|---|---|
| 0–31 s | baseline | 12/12 ok, worst freshness 1.6–2.0 s |
| 31 s | **sim killed** | freshness ages linearly 3.1 → 13.1 s |
| ~47 s (≈16 s after kill) | **threshold (15 s) crossed** | **0/12 ok — all stale together** |
| stale window | socket probe | **ports 21502/21510 REFUSE connections** (`ConnectionRefusedError`) — servers stopped, no stale data served |
| restore | recovery | **12/12 ok in 2–3 s**, ports accept again |

Same fail-safe as §6.8A — serve last-good only within the freshness bound,
then **stop the servers** (clients get connection-refused, never
stale-as-fresh) — with recovery now **2–3 s** vs ~6 s on 3.4.1 (the
supervisor respawn path got faster through the serving-core rewrite).
Verified twice (timeline run + socket-proof run).

### §R.5 Soak — 30 min, 50 dev + 12 vm + 60 steady clients + 10 flap

88 samples @ 20 s. Steady swarm: **428,125 reads (238/s), 0 errors**, p99
11.0 ms. Flap swarm: **50,827 reconnects, 0 failures**, p99 6.8 ms.

| metric | behaviour |
|---|---|
| RAM | **flat**: first-third mean 138.2 MiB, last-third 138.3 MiB (span 137.9–139.6) |
| FDs | 415–485 oscillating with flap, no trend (means 478 → 474) |
| threads | **136, constant** |
| CPU | avg 20.7%, max 28.4% |
| P0 | **12/12 fresh in all 88 samples**; worst freshness 2.0 s, worst device staleness 1.0 s |

### Verdict (§R)

**No regression.** The 3.24 serving-core rewrite plus the 3.25–3.35
hardening series kept the whole envelope intact — linear scaling, zero
errors under swarm/flap/soak, no leak — and *improved* stale-recovery
(2–3 s vs ~6 s). The historical per-dimension ceilings below (measured on
v3.4.1, Janitza-shaped) remain valid as lower bounds; this run adds the
EM24-shape coefficients and confirms the fail-safe and the FD-additivity
model on the current code.

---

> The sections below are the **original 2026-07/08 campaign on v3.4.1** —
> kept as the full-depth reference (per-dimension ceilings ramped to their
> knees). Re-validated at composed scale on v3.35.3 in §R above.

## §6.0 Baseline — production-shape, LIVE data (2026-07-31)

**Setup:** isolated test-MBG (v3.4.1 image, `--cpus 2 --memory 1g`) polling the
**real Janitza UMG512** at its LAN address, port 502 (read-only, multi-client-safe;
production keeps polling it independently — no interference). Exact production
config: `janitza_umg512_pro` template, 67 registers (addr 3813–19636), poll
groups realtime 0.25s / normal 5s / slow 60s; two virtual meters
(`em24_av53` + `fronius_ts_native`) on isolated ports 21503/21502. MQTT +
InfluxDB OFF (pure poll+serve footprint; their load is §6.2). Load driver: one
ESS-like Modbus client reading `em24_av53` every 250 ms. Duration 120 s.

**Resource footprint (21 samples @ 5 s):**

| Metric | Value |
|---|---|
| CPU | 1.67–3.63%, **avg 2.43%** of a 2-core cap (~5% of one core) |
| RAM | **66.3–66.9 MiB**, flat over the run (no baseline leak) |
| Threads | **14**, constant |
| FDs | **31–32**, stable |

**P0 invariant (no vmeter false-stale):** ✅ **PASS**
- `vmeters_ok == vmeters_total` (2/2) for **every** sample.
- Worst vmeter freshness **0.5 s** vs `stale_after_s` 15 s — huge margin.
- Worst Janitza device staleness **0.3 s** — poller trivially keeping up.

**Client read latency (1 ESS client, 250 ms):**
- 477 reads / 120 s (4/s), **0 errors**, p50 1.44 ms / p95 1.60 ms / **p99 1.79 ms**,
  max 2.13 ms, connection setup p99 0.47 ms.

**Reference standalone swarm capacity (sim, not MBG):** 50 clients → 912 reads/s,
0 err, p99 5.25 ms; flap mode → 2917 clean reconnects. Confirms the *driver* has
ample headroom above what MBG will need — a slow result later is MBG's limit,
not the harness's.

### Read of the baseline

Production shape is **featherweight** on this hardware: ~2.4% of two cores, 66 MB
RAM, 14 threads. The knees we're hunting (§6.1–6.5) are far above this — there is
enormous headroom, so the ramps should push aggressively. The harness measures
correctly (footprint stable, P0 tracked, latency sane), so the numbers from here
on are trustworthy.

---

## §6.1 Number of polled devices (2026-08-01)

**Setup:** isolated test-MBG polling a **sim fleet** (128 units on one port,
20000-register blocks so the real `janitza_umg512_pro` template — 67 regs, addr
3813–19636, poll groups 0.25/5/60 s — reads succeed). Devices added via the API,
each pointing at a sim unit. Ramped 1→10→25→50→100 total. `--cpus 2 --memory 1g`,
container FD limit 1024. Measured at steady state.

| devices | worst staleness | threads | FDs | RAM | CPU (2-core cap) | all connected |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0.20 s | 11 | 23 | 59 MiB | 0.5% | 1/1 |
| 10 | 0.90 s | 39 | 113 | 65 MiB | 4% | 10/10 |
| 25 | 0.80 s | 84 | 263 | 72 MiB | 5% | 25/25 |
| 50 | 1.00 s | 159 | 513 | 85 MiB | 18% | 50/50 |
| 100 | 1.00 s | 309 | **1013** | 108 MiB | 19% | 100/100 |

**Findings:**
- **MBG keeps up with 100 devices.** At N=100 the staleness *distribution*
  across all 100 was min 0.00 / **median 0.50** / max 0.90 s — most devices
  fresh at ~the realtime interval; the "worst 1.0 s" is just the tail across 100
  samples, NOT systemic lag. Staleness is **flat vs N** (0.9 s at N=10 →
  1.0 s at N=100) and CPU only 19% — MBG is not the device-count bottleneck;
  there is headroom well beyond 100 on CPU/thread terms.
- **Sim was NOT the bottleneck** (don't-fool-ourselves check): direct sim read
  latency *while 100 MBG devices polled it* was p50 1.1 ms / max 1.6 ms. The
  ~1 s tail is normal poll-cycle jitter, not sim queueing.
- **Threads = 11 + ~3·N** — one poller thread per poll group (3 groups). 309 at
  N=100.
- **FDs ≈ 10 per device** — the hard ceiling: **N=100 → 1013/1024 FDs**. One
  more device (or client) would hit the limit. **~100 devices is the FD cap.**

### Verdict (§6.1)
The device-count ceiling is **~100 devices ≈ the container FD limit (1024)** at
~10 FDs each — NOT CPU, NOT staleness (polling stays crisp, median 0.5 s, 19%
CPU). Real deployment has 1–2 Modbus devices → ~200 FDs of headroom before this
even matters.

**Recommendations:**
1. **~10 FDs/device is high** — likely one Modbus connection per poll group
   (3) plus per-thread fds. Reusing one connection per device across its poll
   groups would multiply the device ceiling; worth investigating (low priority
   given real device counts).
2. Raise `ulimits: nofile` to lift the ceiling for large fleets.

**Joint FD budget (all three ramps):**
`~23 base + 10·devices + 4·vmeters + 1·client_connections ≤ 1024`.
Production (1 device, 2 vmeters, ~2 clients) ≈ 45 FDs — ~950 to spare.

---

## §6.6 Combined worst-case (2026-08-01)

**Setup:** everything at once — **30 sim-backed devices** (janitza template,
67 regs, 0.25/5/60 s groups) + **30 vmeters** (sourcing the primary) +
**300 client connections** (10 per vmeter across 30 ports, 250 ms cadence).
MQTT/Influx off (their load is additive, §6.2 — the sim doesn't churn the
janitza read addresses, so publish would be unrealistically low here). 180 s
sustained. `--cpus 2 --memory 1g`.

**During-load footprint (collector, every 5 s):**

| Metric | Value |
|---|---|
| CPU | max 42.5%, **avg 36%** of 2-core cap (~0.7 core, 58% idle) |
| FDs | **733** (23 base + ~300 dev + 120 vmeter + 300 client — additive) |
| Threads | 159 (11 + 3·30 dev + 2·30 vmeter) |
| RAM | 162 → 179 MiB peak |
| Client reads | 213,860 / 180 s = **1184/s, 0 errors**, p50 1.0 / p95 8.9 / **p99 19.4** / max 87 ms |

**P0 — held under combined load:**
- vmeters **30/30 fresh** the entire run, worst freshness **0.5 s** (vs 15 s).
- worst device staleness **1.0 s** — identical to §6.1 in isolation, NOT
  degraded by the concurrent vmeter-serve + 300-client-read load.

**Findings — the envelopes compose cleanly:**
- **No emergent GIL blowup.** Individual CPU (interpolated: ~10% for 30 dev +
  ~13% for 30 vmeter + ~30% for 300 clients ≈ 53% naive sum) vs **combined 36%**
  — sub-additive, not super-linear. Loading all three at once did NOT move the
  knee below the individual limits.
- **FDs compose additively** (733 ≈ sum of the per-dimension costs) — confirming
  the joint FD budget is the real binding constraint, and it is predictable.
- Device polling stayed crisp (1.0 s tail) while 300 clients hammered the
  vmeters at 1184 reads/s — the client-read path does not steal from the poller.

### Verdict (§6.6)
A **~20× production-scale** combined load (30 dev + 30 vmeter + 300 clients)
runs at **36% CPU, 733/1024 FDs, all SLOs green**. The individual capacity
envelopes hold together; plan by the **additive FD budget**
(`~23 + 10·dev + 4·vmeter + 1·client ≤ 1024`), which binds before CPU. Raise
`ulimits: nofile` to scale past it.

---

## §6.7 Soak — slow-leak detection (2026-08-01)

**Setup:** 15 devices + 15 vmeters + 100 steady clients (250 ms) **+ 20 clients
in continuous flap** (reconnect every cycle). Ran **27.7 min** sampled every
20 s (called early — the trend was conclusive). The flap swarm alone made
**523,394 reconnections** — connection-path churn far beyond weeks of normal
operation.

**Trend (steady state, after ~2 min warmup):**

| Metric | Behaviour |
|---|---|
| RAM | **mean 121.4 MiB, flat** (119.6–122.0 across 25+ min; one transient 130 blip, immediately back). No monotonic growth. |
| FDs | 323–343, oscillating with flap churn, **no upward trend** |
| Threads | **83–84, constant** |
| vmeters ok | **15/15 every single sample**; worst freshness ≤ 0.5 s |
| device staleness | 0.4–1.0 s, stable |

### Verdict (§6.7)
**No leak.** Half a million connection churn cycles left RAM, FDs, and threads
flat, and P0 held throughout. This is the definitive confirmation of the §6.4
flap result over the long haul — the `_conn_seen` / connection-handling paths do
not accumulate. (A full overnight 2–4 h soak could be scheduled for extra
assurance, but the trend here is already conclusive.)

---

## §6.8 Failure injection (2026-08-01)

### Scenario A — source failure fail-safe (the ESS-critical test)
**Setup:** 5 devices + 5 vmeters + 30 clients reading at 250 ms,
`stale_after_s`=15. Killed the data source (sim) under load at t=30 s, restored
it at t=66 s; sampled `/health` every 2 s and the client swarm every 10 s.

**Timeline:**

| t (s) | phase | vmeters | worst freshness | client swarm |
|---:|---|---|---|---|
| 0–30 | baseline | **5 ok** | 0.2–0.4 s | 120 reads/s, 0 err, 0 connfail |
| 30 | **source killed** | 5 ok | — | still reading |
| 30–40 | aging | 5 ok | 0.4 → 10.6 s | still serving last-good (within the 15 s bound) |
| **42** | **threshold crossed** | **0 ok / 5 stale** | 12.7 s | — |
| 42–66 | source dead | 0 ok / 5 stale | 20 → 41 s | **reads STOP, connfail climbs 420→1620** |
| 66 | **source restored** | 5 stale | 41 s | — |
| **72** | **recovered (~6 s)** | **5 ok** | 0.3 s | reads resume, connfail plateaus |
| 72–120 | healthy | 5 ok | 0.2–0.4 s | 89 reads/s, recovering |

**The fail-safe is exactly right:**
- On source loss, vmeters serve last-good **only within the 15 s freshness
  bound**, then transition to stale and the supervisor **STOPS the servers**.
- Clients then get **connection-refused, NOT stale data**: `err%` stayed
  **0.000** the whole time — the reads that returned were always fresh; the
  failure mode is "can't connect" (connfail climbs), never "served a wrong
  value." For an ESS this is the correct choice: **no data beats stale data.**
- **Recovery in ~6 s** after the source returned — vmeters fresh, clients
  resume, zero manual intervention.

This live test validates the whole freshness-watchdog architecture (3.3.4
per-row gate, monotonic freshness, fail-closed) under a real source failure:
**the system fails SAFE.**

### Scenario B — MQTT broker down (not run live)
MQTT is a sink; the code already handles broker loss via paho auto-reconnect +
LWT + `publish_if_changed` guarded on `connected`, so acquisition and vmeter
serving continue regardless. A live confirmation is a low-risk follow-up.

### Scenario C — clock step under load (covered by unit tests)
Freshness is on the monotonic clock (immune to wall steps) and calc `dt` was
moved to monotonic in 3.4.1. Covered by `test_freshness_is_immune_to_wall_clock_steps`
and `test_calc_dt_uses_monotonic_clock_not_wall`; a live container clock step
needs CAP_SYS_TIME and is not worth the risk given the unit coverage.

---

## quality_block shared-lock — latency validation (3.4.2, 2026-08-01)

The 3.4.2 word-tearing fix adds a `_store_lock` on the quality_block read/write
paths. The question: does the lock hurt the hot read path? Isolated 3-way
comparison, one vmeter, 400 clients @ 250 ms (multiple runs each):

| variant | p50 | p99 |
|---|---:|---:|
| **A** — sequential block (default/production) | 1.2 ms | **~21 ms** |
| **B** — quality_block + lock (the fix) | 6–8 ms | **~39–59 ms** |
| **C** — quality_block, lock disabled (isolation) | 7–13 ms | **~44–72 ms** |

**Conclusion:** B and C are statistically indistinguishable (overlapping, noisy)
— **the lock adds no measurable latency.** The ~2× gap vs the sequential block
is the `ModbusSparseDataBlock`'s inherent cost (dict lookups vs list slice),
present with or without the lock. At production scale (~2 clients/vmeter) all
variants are ~2 ms. The fix is free; quality_block is opt-in and off in
production regardless.

---

## §6.3 Number of virtual meters (2026-07-31)

**Setup:** isolated test-MBG (real Janitza), N cloned `em24_av53` vmeters all
sourcing the primary, on ports 21502+. Booted fresh per N; measured at steady
state (5 samples over 20 s). `--cpus 2 --memory 1g`, container FD limit 1024.

| N vmeters | threads | FDs | RAM | CPU avg (2-core cap) | vmeters fresh | worst fresh |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | 15 | 31 | 65 MiB | ~2.5% | 2/2 | 0.6 s |
| 11 | 33 | 67 | 90 MiB | ~6% | 11/11 | 0.6 s |
| 25 | 61 | 123 | 130 MiB | ~11% | 25/25 | 0.5 s |
| 50 | 111 | 223 | 197 MiB | ~18% | 50/50 | 0.6 s |

**Findings:**
- **P0 held at every level** — all N vmeters stayed fresh (0.4–0.6 s) even with
  50 supervisors + 50 asyncio servers on a 2-core cap. Zero failures.
- **Threads = 11 + 2·N exactly** (supervisor + asyncio server per vmeter) —
  hypothesis confirmed. This is the dominant scaling cost.
- **FDs ≈ 4 per vmeter** (listen socket + poll conn + epoll + misc).
- **RAM ≈ 2.6 MiB per vmeter** (flat coefficient at scale).
- **CPU ≈ 0.36% of 2 cores per vmeter** (each supervisor ticks at
  `update_interval_s`=0.25 s + serves).

**Extrapolated ceilings on the 2-core / 1-GiB cap:** RAM ~370 vmeters, CPU
~250–280, FDs ~248 (but FDs are SHARED with client connections — see §6.4).
Practical ceiling where threads+CPU+FD converge: **~150–200 vmeters**.

### Verdict (§6.3)
MBG runs **50 vmeters trivially** (18% CPU, 197 MiB, 111 threads, all fresh) —
far beyond the default port range (1502–1512 = 11 vmeters, itself trivial at
33 threads / 6% CPU). To exceed 11, widen `VMETER_PORT_END` (documented).

**Cross-link with §6.4:** the container's 1024 FD budget is SHARED between
vmeter overhead (~4/vmeter) and client connections (~1/client):
`~23 + 4·vmeters + 1·total_clients ≤ 1024`. E.g. the default 11 vmeters use
~67 FDs, leaving ~950 for client connections. Plan the two jointly; raise
`ulimits: nofile` to lift the shared ceiling.

---

## §6.4 Clients per vmeter (2026-07-31)

**Setup:** same isolated test-MBG (real Janitza), one vmeter (`em24_av53`,
port 21503). Swarm ramped 1→800 concurrent Modbus clients, each reading 30 regs
every 250 ms (ESS cadence). Container FD soft limit = **1024**; host 12 cores,
test-MBG capped at 2. Metrics sampled every 3 s during each 45 s level.

| clients | reads/s | p50 | p95 | **p99** | max | conn-setup p99 | container FDs | threads | CPU (2-core cap) | vmeter fresh | errors |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 4 | 1.4 | 1.7 | **2.0** | 2.3 | 0.4 ms | 32 | 15 | 2.8% | 0.6 s | 0 |
| 20 | 79 | 1.9 | 4.7 | **6.8** | 9.2 | 2.8 ms | 51 | 15 | 5.3% | 0.6 s | 0 |
| 50 | 197 | 1.6 | 4.3 | **7.6** | 15.7 | 6.5 ms | 81 | 15 | 9.9% | 0.5 s | 0 |
| 100 | 393 | 1.6 | 5.5 | **14** | 21 | 13.6 ms | 131 | 15 | 16.7% | 0.5 s | 0 |
| 200 | 783 | 1.3 | 6.5 | **18** | 40 | 28 ms | 231 | 15 | 26.5% | 0.5 s | 0 |
| 400 | 1561 | 1.3 | 10.4 | **20** | 41 | **1021 ms** | 431 | 14 | 39.4% | 0.5 s | 0 |
| 800 | 3067 | 2.7 | 18 | **42** | 62 | 120 ms | 831 | 14 | 52.6% | 0.5 s | 0 |

**Findings:**
- **P0 held at every level.** vmeter freshness stayed 0.5–0.6 s even at 800
  clients / 3067 reads/s — heavy client reads do NOT starve the Janitza poller.
  This is the safety result that matters: the ESS never sees the meter go stale
  under client load.
- **Zero errors, zero connect failures** up to 800 clients.
- **Threads are constant (~15)** — the vmeter is one asyncio server, no
  thread-per-connection. Threads are NOT a limit (hypothesis disproven).
- **FDs scale ~1 per client** (31 baseline + K). Container soft limit 1024 ⇒
  **hard ceiling ≈ 990 concurrent client connections**. **Important:** all
  vmeters share the ONE process's FD table, so this ceiling is TOTAL across all
  vmeters, not per-vmeter — spreading clients over more ports distributes CPU /
  accept-loop load but does NOT raise the FD ceiling.
- **CPU scales with read rate:** 800 clients ≈ 1 full core; the 2-core cap would
  saturate ~6000 reads/s (~1500 clients) — but FDs cap first.
- **Connection-setup stampede:** at 400 simultaneous connects, setup p99 spiked
  to ~1 s (single accept loop). Steady-state is fine; a mass simultaneous
  reconnect (network blip → all consumers reconnect at once) briefly queues.

**`_conn_seen` leak (audit question) — REFUTED.** Flap mode (50 clients
reconnecting every cycle) drove **69,139 connections in 90 s** (764/s): RAM
**77.6 → 77.3 MiB (flat)**, FDs oscillated 31–66 and always returned to 31, no
monotonic growth. No leak.

### Verdict (§6.4)
One MBG serves **~800 concurrent ESS-cadence clients** on a single vmeter with
p99 42 ms, 0 errors, meter stays fresh, ~1 core. **Hard ceiling ≈ 990 total
client connections** = the container `nofile` limit (1024), NOT MBG logic. Real
deployment has ~2 clients/vmeter → **~250× headroom.**

**Recommendations:**
1. To serve many clients, raise the container FD limit (`ulimits: nofile` in
   compose) — that moves the only hard ceiling. Spreading across ports does not.
2. Optional: an explicit vmeter connection cap (graceful refuse, like the WS
   cap) so that at the FD ceiling new clients get a clean close instead of an
   accept failure. Low priority given the headroom.
3. Close the audit's `_conn_seen` leak question — not a leak.

> Harness note: `collect_metrics.py` reads `data_health` as a string and uses
> per-device `staleness_age_s` (not `poll_groups_detail`, which isn't in the
> `/api/status` device summary) — fixed after the first baseline attempt.
