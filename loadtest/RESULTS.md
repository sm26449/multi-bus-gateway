# MBG load-test results

## §6.0 Baseline — production-shape, LIVE data (2026-07-31)

**Setup:** isolated test-MBG (v3.4.1 image, `--cpus 2 --memory 1g`) polling the
**real Janitza UMG512** at 192.168.1.207:502 (read-only, multi-client-safe;
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

### Next ramps (from here)
- §6.1 devices → point many sim units at test-MBG, ramp 10/25/50/100, watch
  `worst_device_staleness_s` cross the poll interval.

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
