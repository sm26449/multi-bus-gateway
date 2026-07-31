#!/usr/bin/env python3
# Multi-Bus Gateway — load-test harness (S5: metrics collector).
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""S5 — metrics collector.

Samples, every --interval-s, into a CSV that later graphs the whole run:
  * container CPU% / mem / net / PIDs   via `docker stats --no-stream`
  * per-poll-group age_s + poll_count   via GET /api/status
  * per-vmeter state + freshness_age_s  via GET /health
  * FD count + thread count             via /proc (if --pid given)

Deliberately lightweight so it does not perturb what it measures (the whole
point of "don't fool ourselves"): one docker-stats call + two small HTTP GETs
per sample. Point it at the TEST MBG, never production.

    python collect_metrics.py --base http://127.0.0.1:18080 \
        --container loadtest-mbg --out run.csv --interval-s 2
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
import urllib.request


def _get_json(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:      # noqa: BLE001 — a missed sample must not kill the run
        return {"_error": str(e)}


def _docker_stats(container: str) -> dict:
    try:
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.CPUPerc}};{{.MemUsage}};{{.NetIO}};{{.PIDs}}", container],
            capture_output=True, text=True, timeout=10)
        cpu, mem, net, pids = (out.stdout.strip().split(";") + ["", "", "", ""])[:4]
        return {"cpu": cpu, "mem": mem, "net": net, "pids": pids}
    except Exception as e:      # noqa: BLE001
        return {"cpu": "", "mem": "", "net": "", "pids": "", "_err": str(e)}


def _proc_counts(pid: int) -> dict:
    try:
        import os
        fds = len(os.listdir(f"/proc/{pid}/fd"))
        threads = 0
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("Threads:"):
                    threads = int(line.split()[1]); break
        return {"fds": fds, "threads": threads}
    except Exception:           # noqa: BLE001
        return {"fds": "", "threads": ""}


def main() -> None:
    ap = argparse.ArgumentParser(description="S5 metrics collector")
    ap.add_argument("--base", default="http://127.0.0.1:18080")
    ap.add_argument("--container", default="")
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--out", default="loadtest_run.csv")
    ap.add_argument("--interval-s", type=float, default=2.0)
    ap.add_argument("--duration-s", type=float, default=0.0)
    args = ap.parse_args()

    fields = ["t", "cpu", "mem", "net", "pids", "fds", "threads",
              "worst_pollgroup_age_s", "any_device_stale",
              "vmeters_total", "vmeters_ok", "worst_vmeter_fresh_s"]
    t0 = time.time()
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        print(f"[collect] -> {args.out} (base={args.base} container={args.container})",
              flush=True)
        while True:
            row = {"t": round(time.time() - t0, 1)}
            if args.container:
                row.update(_docker_stats(args.container))
            if args.pid:
                row.update(_proc_counts(args.pid))

            st = _get_json(f"{args.base}/api/status")
            worst_age, any_stale = 0.0, False
            for d in (st.get("devices") or []) if isinstance(st, dict) else []:
                for pg in (d.get("poll_groups_detail") or []):
                    a = pg.get("age_s")
                    if a is not None:
                        worst_age = max(worst_age, a)
                if str(d.get("data_health", {}).get("status", "")) not in ("ok", ""):
                    any_stale = True
            row["worst_pollgroup_age_s"] = round(worst_age, 1)
            row["any_device_stale"] = any_stale

            hl = _get_json(f"{args.base}/health")
            ms = (hl.get("meters") or []) if isinstance(hl, dict) else []
            row["vmeters_total"] = len(ms)
            row["vmeters_ok"] = sum(1 for m in ms if m.get("state") == "ok")
            row["worst_vmeter_fresh_s"] = round(
                max((m.get("freshness_age_s") or 0.0) for m in ms), 1) if ms else 0.0

            w.writerow(row); fh.flush()
            print(f"[collect] t={row['t']:.0f} cpu={row.get('cpu','')} "
                  f"mem={row.get('mem','')} pids={row.get('pids','')} "
                  f"fds={row.get('fds','')} thr={row.get('threads','')} "
                  f"worst_poll_age={row['worst_pollgroup_age_s']}s "
                  f"vmeters={row['vmeters_ok']}/{row['vmeters_total']} "
                  f"worst_vm_fresh={row['worst_vmeter_fresh_s']}s", flush=True)

            if args.duration_s and (time.time() - t0) >= args.duration_s:
                break
            time.sleep(args.interval_s)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
