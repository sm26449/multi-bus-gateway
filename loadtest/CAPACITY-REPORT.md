# MBG Capacity Report

**System:** Multi-Bus Gateway v3.35.3  **Date:** 2026-08-16
**Basis:** 7-phase load/stress campaign on v3.4.1 (§6.0–§6.8, 2026-08-01),
**re-validated on v3.35.3** (§R, 2026-08-16) after the pymodbus 3.15
serving-core rewrite (3.24.x) and the 3.25–3.35 hardening series. Full data
in [`RESULTS.md`](RESULTS.md).
**Test hardware:** container capped at **2 CPU / 1 GiB**, `nofile`=1024, on a
12-core host. Isolated stack; the baseline used the **real Janitza** (live data),
the ramps used the sim fleet. Production was never touched.

---

## Headline

MBG is **featherweight** relative to any real deployment. Across every
dimension the binding limit is a **container resource ceiling (file descriptors,
then CPU/threads)** — never MBG's own logic. Production runs at **~2% of two
cores**; the tested envelope reaches **~20× production scale at 36% CPU** with
every SLO green. The freshness watchdog **fails safe** under source loss and the
system shows **no leak** over sustained connection churn.

**Re-validation (v3.35.3, §R):** the current version holds the same envelope —
51 devices + 12 vmeters + 200 clients (**with MQTT publishing ON**, unlike the
original ramps) at 24.5% CPU / 117 MiB / 615 FDs, 790 reads/s with 0 errors
(p99 15.8 ms); a 30-min soak with 50k+ flap reconnects left RAM flat at
138 MiB with 12/12 vmeters fresh in every sample; and the fail-safe verified
end-to-end again, with **stale-recovery improved to 2–3 s** (was ~6 s).
No regression from the 3.24 serving-core rewrite or the hardening series.

## Baseline (§6.0) — production shape, live Janitza data

| | value |
|---|---|
| Config | 1 device (67 regs, 0.25/5/60 s), 2 vmeters, 1 ESS client |
| CPU | **2.4%** of 2 cores | 
| RAM | 66 MiB | Threads | 14 | FDs | 31 |
| P0 | vmeters fresh (worst 0.5 s), client p99 **1.8 ms** |

## Per-dimension ceilings

| dimension | tested to | limiting resource | ceiling | at ceiling |
|---|---|---|---|---|
| **Polled devices** (§6.1) | 100 | **FDs** (~10/device) | **~100** | polling still crisp (median staleness 0.5 s), 19% CPU |
| **Virtual meters** (§6.3) | 50 | threads (2/vmeter) + FDs (4) | **~150–200** | 50 = 18% CPU, 111 threads, all fresh |
| **Clients / vmeter** (§6.4) | 800 | **FDs** (1/client) | **~990 total** | 800 = p99 42 ms, 0 err, ~1 core |
| **Combined** (§6.6) | 30+30+300 | **FD budget (additive)** | see model | 36% CPU, 733 FDs, all SLOs green |
| **Combined re-run** (§R, v3.35.3) | 51+12+200, MQTT ON | FD budget (additive) | see model | 24.5% CPU, 615 FDs, 0 errors, P0 green |

## Resource cost model (the planning tool)

Derived from the ramps; use it to size any config:

```
FDs      ≈  23  + 10·devices + 4·vmeters + 1·client_connections   ≤ 1024 (nofile) ← BINDING
threads  ≈  11  +  3·devices + 2·vmeters
RAM(MiB) ≈  60  + 0.5·devices + 2.6·vmeters   (+ ~buffers)
CPU      scales with total read rate; 20× production ≈ 36% of 2 cores
```

Per-device coefficients are **template-shaped**: the 10 FDs / 3 threads per
device above are the Janitza shape (67 regs, 3 poll groups). The v3.35.3
re-run with the EM24 shape (16 regs, 2 poll groups) measured **~7 FDs /
2 threads per device** — one poller thread + connection per poll group.
Budget with your template's group count; the model's structure is confirmed
on both shapes.

- **FDs are the binding constraint** and compose **additively** (confirmed in
  §6.6: predicted 743, measured 733). Everything else has far more headroom.
- Threads and CPU are sub-additive under combined load — no GIL blowup.

## Reliability findings

- **No leak (§6.7 + §R.5):** 27.7 min soak with **523,394 connection churn
  cycles** on v3.4.1, re-confirmed on v3.35.3 with a 30-min soak (478,952
  reads + 50,827 flap reconnects, 0 errors) → RAM flat (138.2 → 138.3 MiB
  across thirds), FDs/threads constant. The `_conn_seen` leak question
  (audit) is closed — refuted on both versions.
- **Fails safe (§6.8 + §R.4):** on source loss, vmeters serve last-good only
  within the 15 s freshness bound, then the supervisor **stops the servers**.
  Clients get connection-refused, **never stale-as-fresh** (`err%`=0
  throughout; socket probes during the stale window confirm
  `ConnectionRefusedError`). Recovery when the source returns: ~6 s on
  v3.4.1, **2–3 s on v3.35.3**. No data beats stale data for the ESS.

## Safe operating envelope (this hardware: 2 CPU / 1 GiB / nofile 1024)

| scenario | devices | vmeters | clients | FDs | CPU | headroom |
|---|---:|---:|---:|---:|---:|---|
| **Production (actual)** | 1 | 2 | ~2 | ~45 | ~2% | ~950 FDs spare |
| **Comfortable** | 20 | 20 | 200 | ~600 | ~30% | fine |
| **Tested worst-case** | 30 | 30 | 300 | 733 | 36% | all SLOs green |
| **Hard ceiling** | — | — | — | **1024** | — | FD exhaustion |

Production sits at **~4% of the FD budget** — effectively unlimited headroom for
this deployment.

## Recommendations

1. **No action needed for production.** The current 1-device / 2-vmeter config
   uses ~45 of 1024 FDs and ~2% CPU. Enormous margin.
2. **To scale to large fleets, raise the container FD limit** (`ulimits: nofile`
   in compose) — it is the single binding constraint. Everything else (CPU,
   threads, RAM) has more room.
3. **Investigate ~10 FDs/device** (§6.1) — likely one Modbus connection per poll
   group (×3) plus per-thread fds. Reusing one connection per device across its
   poll groups would roughly **triple the device ceiling** (~100 → ~300). Low
   priority given real device counts; a clean efficiency win if ever needed.
4. **Optional:** an explicit vmeter connection cap (graceful refuse near the FD
   ceiling, mirroring the WebSocket cap added in 3.4.1) so an FD-exhaustion edge
   returns a clean close instead of accept failures. Low priority.
5. **Container sizing:** 2 CPU / 1 GiB comfortably handles ~20× production; size
   future hardware against the cost model above, not guesswork.

## Not run (documented gaps)

- §6.2 InfluxDB write-load ramp — still deferred. The MQTT half is
  effectively covered now: the §R re-run ran **with MQTT publishing ON**
  (51 devices + 12 vmeter state topics against a live sim that churns
  values every 250 ms) and the whole envelope held — publish cost is
  included in the §R figures.
- §6.8 Scenario B (broker down live) — low-risk; the code already handles it
  (paho auto-reconnect + LWT). Scenario C (clock step) — unit-tested.
- A full overnight 2–4 h soak — the 28-min trend was already conclusive.
