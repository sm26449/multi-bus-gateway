# MBG load-test harness (Phase A)

Instruments for finding MBG's capacity limits. **Isolated from production** — see
Rule Zero in the compose file.

## Components

| File | Role |
|---|---|
| `sim_devices.py` | **S1** — fake Modbus TCP device fleet, changing values (validated) |
| `vmeter_client_swarm.py` | **S3** — concurrent Modbus client swarm reading vmeters (validated) |
| `collect_metrics.py` | **S5** — samples CPU/RAM/FD/thread + poll age + vmeter freshness → CSV |
| `seed_via_api.py` | seeds test-MBG with devices→sim + vmeters via MBG's own API |
| `docker-compose.loadtest.yml` | isolated stack: test-MBG + throwaway broker + sim, capped |
| `Dockerfile.sim` | image for the simulators |

S1 + S3 are **validated end-to-end standalone** (10 clients @ 100 ms → 100 reads/s,
0 errors, p99 < 4 ms, values confirmed churning). They need no MBG to smoke-test:

```bash
python loadtest/sim_devices.py --port 6599 --units 4 &        # S1
python loadtest/vmeter_client_swarm.py --targets 127.0.0.1:6599 --clients 10 \
    --count 20 --interval-ms 100 --duration-s 12               # S3 reads S1
```

## Full run (isolated stack)

```bash
# 1. bring up test-MBG + throwaway broker + sim fleet (capped 2 CPU / 1 GiB)
docker compose -f loadtest/docker-compose.loadtest.yml up -d --build

# 2. pick templates whose register map matches the sim (0x0000 V, 0x000c I,
#    0x0028 P, 0x0100 energy — sim serves a full 0..511 block so reads succeed)
python loadtest/seed_via_api.py --base http://127.0.0.1:18080     # lists templates

# 3. seed devices (→ sim) + vmeters
python loadtest/seed_via_api.py --base http://127.0.0.1:18080 \
    --devices 10 --device-template <T> --vmeters 4 --vmeter-template <VT>

# 4. start the collector (own terminal)
python loadtest/collect_metrics.py --base http://127.0.0.1:18080 \
    --container loadtest-mbg --out runA.csv --interval-s 2

# 5. drive load — ramp clients per the plan (§6.4): 1 → 20 → 100 → cliff
python loadtest/vmeter_client_swarm.py --targets 127.0.0.1:21502 --clients 20 \
    --interval-ms 250 --duration-s 300

# tear down (removes the throwaway config volume)
docker compose -f loadtest/docker-compose.loadtest.yml down -v
```

## Ramp cheatsheet (map to the plan §6)

- **Devices (§6.1):** re-seed `--devices 10/25/50/100`; watch `worst_pollgroup_age_s`
  in the collector — when it exceeds the poll interval, that's the client-side knee.
- **Vmeters (§6.3):** `--vmeters 2/11/25/50` (range already 21502-21562 = 61 ports);
  watch thread count.
- **Clients per vmeter (§6.4):** swarm `--clients 1/20/100/...` on ONE `--targets`
  port, then spread across ports; watch p99 latency + FD count.
- **_conn_seen leak (§6.4):** swarm `--flap` (reconnect every cycle) for a soak;
  watch RSS + FD trend in the collector.
- **WS cap:** open 65 websockets to `/ws`; expect a clean close at #65 (cap 64).

## Pass/fail (the P0 invariant)

The headline SLO: **no vmeter goes false-stale.** In the collector, `vmeters_ok`
must equal `vmeters_total` and `worst_vmeter_fresh_s` must stay below the
configured `stale_after_s` for the whole run. The load level where that first
breaks is the supported ceiling for that config.

## Not yet done (next steps)

- The seeded full campaign has NOT been run here — it needs the stack up on a
  test host / quiet window (running 100-client swarms on the prod host must be
  resource-capped and scheduled).
- Optional refinements: artificial per-device response latency in S1 (models a
  slow/serial device — blocks that server's units, so document its semantics);
  disposable InfluxDB wired in for write-load measurement (§6.2, commented in
  the compose).
