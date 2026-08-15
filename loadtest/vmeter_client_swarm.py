#!/usr/bin/env python3
# Multi-Bus Gateway — load-test harness (S3: virtual-meter client swarm).
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""S3 — virtual-meter client swarm (the key measurement tool).

Opens K concurrent Modbus TCP master connections to MBG's virtual-meter port(s)
and reads in a loop at a target cadence (default 250 ms, like a Victron ESS).
Measures per-read latency (p50/p95/p99), success rate, connection setup time.

Modes:
    --targets host:port[,host:port...]   one or more vmeter endpoints
    --clients K                          concurrent connections (round-robin over targets)
    --interval-ms 250                    read cadence per client (0 = as fast as possible)
    --count 10 --start 0                 registers to read per request
    --flap                               reconnect EVERY cycle (stresses _conn_seen leak)
    --duration-s 60                      run length (0 = until Ctrl-C)

Prints a metrics line every --report-s seconds and a final summary — so a run
is self-describing and the numbers are trustworthy (no hidden sampling).
"""
from __future__ import annotations

import argparse
import asyncio
import time
from collections import deque

from pymodbus.client import AsyncModbusTcpClient


class Stats:
    def __init__(self) -> None:
        self.ok = 0
        self.err = 0
        self.connects = 0
        self.connect_fail = 0
        self._lat = deque(maxlen=200_000)   # recent read latencies (ms)
        self._conn = deque(maxlen=50_000)   # connect setup times (ms)

    def read(self, latency_ms: float, ok: bool) -> None:
        if ok:
            self.ok += 1
            self._lat.append(latency_ms)
        else:
            self.err += 1

    @staticmethod
    def _pct(sorted_vals, q):
        if not sorted_vals:
            return 0.0
        i = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
        return sorted_vals[i]

    def snapshot(self) -> dict:
        lat = sorted(self._lat)
        conn = sorted(self._conn)
        total = self.ok + self.err
        return {
            "reads": total, "ok": self.ok, "err": self.err,
            "err_pct": round(100.0 * self.err / total, 3) if total else 0.0,
            "p50_ms": round(self._pct(lat, 0.50), 2),
            "p95_ms": round(self._pct(lat, 0.95), 2),
            "p99_ms": round(self._pct(lat, 0.99), 2),
            "max_ms": round(lat[-1], 2) if lat else 0.0,
            "connects": self.connects, "connect_fail": self.connect_fail,
            "conn_p99_ms": round(self._pct(conn, 0.99), 2),
        }


def _parse_targets(s: str) -> list[tuple[str, int]]:
    out = []
    for part in s.split(","):
        host, _, port = part.strip().partition(":")
        out.append((host, int(port or 502)))
    return out


async def _client(cid: int, target: tuple[str, int], args, stats: Stats,
                  stop: asyncio.Event) -> None:
    host, port = target
    interval = max(0.0, args.interval_ms / 1000.0)
    client = None
    while not stop.is_set():
        try:
            if client is None or not client.connected:
                t0 = time.perf_counter()
                client = AsyncModbusTcpClient(host, port=port, timeout=3)
                await client.connect()
                stats._conn.append((time.perf_counter() - t0) * 1000)
                if client.connected:
                    stats.connects += 1
                else:
                    stats.connect_fail += 1
                    await asyncio.sleep(0.5)
                    continue
            t0 = time.perf_counter()
            rr = await client.read_holding_registers(
                address=args.start, count=args.count, device_id=args.unit)
            lat = (time.perf_counter() - t0) * 1000
            stats.read(lat, ok=(rr is not None and not rr.isError()))
        except Exception:      # noqa: BLE001 — count as a failed read, keep looping
            stats.read(0.0, ok=False)
            try:
                if client:
                    client.close()
            except Exception:  # noqa: BLE001
                pass
            client = None
            await asyncio.sleep(0.2)
            continue
        if args.flap:          # reconnect every cycle → stress _conn_seen tracking
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            client = None
        if interval:
            await asyncio.sleep(interval)
    if client:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


async def _reporter(stats: Stats, args, stop: asyncio.Event, t_start: float) -> None:
    while not stop.is_set():
        await asyncio.sleep(args.report_s)
        s = stats.snapshot()
        rate = s["reads"] / max(0.001, time.perf_counter() - t_start)
        print(f"[swarm] t={time.perf_counter() - t_start:6.1f}s reads={s['reads']:>8} "
              f"rate={rate:7.0f}/s err%={s['err_pct']:.3f} "
              f"p50={s['p50_ms']:.1f} p95={s['p95_ms']:.1f} p99={s['p99_ms']:.1f} "
              f"max={s['max_ms']:.1f}ms conn={s['connects']} connfail={s['connect_fail']}",
              flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser(description="S3 vmeter client swarm")
    ap.add_argument("--targets", default="127.0.0.1:1502")
    ap.add_argument("--clients", type=int, default=20)
    ap.add_argument("--unit", type=int, default=1)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--interval-ms", type=float, default=250.0)
    ap.add_argument("--flap", action="store_true")
    ap.add_argument("--duration-s", type=float, default=60.0)
    ap.add_argument("--report-s", type=float, default=5.0)
    args = ap.parse_args()

    targets = _parse_targets(args.targets)
    stats = Stats()
    stop = asyncio.Event()
    t_start = time.perf_counter()
    print(f"[swarm] {args.clients} clients over {len(targets)} target(s) "
          f"{targets} interval={args.interval_ms:.0f}ms flap={args.flap}", flush=True)

    tasks = [asyncio.create_task(_client(i, targets[i % len(targets)], args, stats, stop))
             for i in range(args.clients)]
    tasks.append(asyncio.create_task(_reporter(stats, args, stop, t_start)))

    if args.duration_s > 0:
        await asyncio.sleep(args.duration_s)
        stop.set()
    else:
        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            stop.set()
    await asyncio.sleep(0.5)
    for t in tasks:
        t.cancel()

    s = stats.snapshot()
    dur = time.perf_counter() - t_start
    print("\n=== swarm summary ===")
    print(f"clients={args.clients} duration={dur:.1f}s reads={s['reads']} "
          f"({s['reads']/dur:.0f}/s) ok={s['ok']} err={s['err']} err%={s['err_pct']}")
    print(f"latency ms: p50={s['p50_ms']} p95={s['p95_ms']} p99={s['p99_ms']} max={s['max_ms']}")
    print(f"connects={s['connects']} connect_fail={s['connect_fail']} "
          f"conn_setup_p99={s['conn_p99_ms']}ms")


if __name__ == "__main__":
    asyncio.run(main())
