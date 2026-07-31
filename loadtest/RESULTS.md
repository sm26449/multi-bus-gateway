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
- §6.3 vmeters → 2/11/25/50, watch thread count.
- §6.4 clients/vmeter → 1/20/100/…, watch p99 latency + FDs + `_conn_seen` (flap).

> Harness note: `collect_metrics.py` reads `data_health` as a string and uses
> per-device `staleness_age_s` (not `poll_groups_detail`, which isn't in the
> `/api/status` device summary) — fixed after the first baseline attempt.
