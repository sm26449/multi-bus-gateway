#!/usr/bin/env python3
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""How many sockets is this access point worth?

A master device — a Fronius DataManager, an RS-485-over-TCP bridge, a PLC front
end — fronts several units on one host:port, and there is no way to look up how
it behaves under concurrency. Some serialize everything internally: a second
socket buys nothing and costs a client slot, and the honest answer is one lane
with an orderly queue. Others run a real engine per line: two or three lanes cut
the sweep almost proportionally. The difference is worth minutes of cadence, and
it is a MEASUREMENT.

So this script measures it, against the real device, with the real register
block, at concurrency 1..N, and prints the number to put in
``connection.max_connections``.

    docker run --rm --network host --entrypoint python -v "$PWD":/app:ro -w /app \
      multi-bus-gateway:test scripts/calibrate_endpoint.py \
        --host 192.168.1.50 --units 1,2,3,4 --address 40072 --count 50

Two things to know before running it:

* It is READ-ONLY. Nothing is written to the device, and nothing is written to
  the gateway's config — the recommendation is printed, you apply it.
* It COMPETES with whatever else is polling that endpoint, which is the point:
  calibrating against an idle device tells you about an idle device. But the
  numbers describe the endpoint as it is loaded right now, so read them next to
  what is running. ``--solo`` prints the reminder and nothing else changes.
"""
import argparse
import logging
import statistics as stats
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymodbus.client import ModbusTcpClient  # noqa: E402


class DesyncCounter(logging.Handler):
    """Count the answers that arrived for a question we had stopped asking.

    When a master is overloaded it answers late: the read times out, the lane
    moves on, and the stale reply turns up against the next transaction's id.
    pymodbus drops it with "request ask for id=N but got id=M, Skipping" and
    carries on — so the NEXT read pays for the previous one's failure, and both
    look fine from the outside. A measurement that did not count these would
    quietly report a degraded endpoint as a healthy one, which is the exact
    mistake this script exists to prevent. Observed on a production Fronius
    DataManager at three connections.
    """

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.count = 0

    def emit(self, record):
        if "Skipping" in record.getMessage():
            self.count += 1


_DESYNC = DesyncCounter()


class Lane(threading.Thread):
    """One socket, serving its own slice of the units, round after round.

    This is exactly the shape the gateway runs: a lane owns a socket, the units
    assigned to it take turns on that socket, and lanes overlap with each other.
    Measuring anything else would measure a program we do not ship.
    """

    def __init__(self, host, port, units, address, count, deadline, timeout):
        super().__init__(daemon=True)
        self.host, self.port, self.units = host, port, units
        self.address, self.count = address, count
        self.deadline, self.timeout = deadline, timeout
        self.latencies, self.errors, self.sweeps = [], 0, []
        self.connect_error = ""

    def run(self):
        client = ModbusTcpClient(self.host, port=self.port, timeout=self.timeout)
        try:
            if not client.connect():
                self.connect_error = "connect refused"
                return
            while time.monotonic() < self.deadline:
                t_sweep = time.perf_counter()
                for unit in self.units:
                    t0 = time.perf_counter()
                    try:
                        rr = client.read_holding_registers(
                            self.address, count=self.count, device_id=unit)
                        # A short frame is not a read. Some masters answer a
                        # block they cannot serve with whatever they have.
                        bad = rr.isError() or len(getattr(rr, 'registers', [])
                                                  or []) != self.count
                    except Exception:  # noqa: BLE001 — a timeout IS the datum
                        bad = True
                    dt = time.perf_counter() - t0
                    # A read that outran the socket timeout did not succeed on
                    # time even if a frame eventually came back.
                    if bad or dt >= self.timeout:
                        self.errors += 1
                    else:
                        self.latencies.append(dt)
                self.sweeps.append(time.perf_counter() - t_sweep)
        finally:
            client.close()


def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else float('nan')


def probe(host, port, units, address, count, lanes, seconds, timeout):
    """Run one concurrency level and report what the endpoint gave back."""
    # Sticky by unit id, modulo the lane count — the same assignment
    # ModbusConnection makes, so the measurement predicts the deployment.
    slices = [[u for u in units if u % lanes == i] for i in range(lanes)]
    slices = [s for s in slices if s]
    deadline = time.monotonic() + seconds
    threads = [Lane(host, port, s, address, count, deadline, timeout)
               for s in slices]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0

    desync = _DESYNC.count
    _DESYNC.count = 0
    refused = [t.connect_error for t in threads if t.connect_error]
    lat = [x for t in threads for x in t.latencies]
    errors = sum(t.errors for t in threads)
    reads = len(lat) + errors
    # The number that matters: how long ONE full pass over every unit takes
    # when the lanes run side by side. Lanes finish at different times, so the
    # slowest lane's sweep is the sweep.
    sweep = max((stats.median(t.sweeps) for t in threads if t.sweeps),
                default=float('nan'))
    return {
        'lanes': lanes, 'reads': reads, 'errors': errors,
        'reads_per_s': reads / elapsed if elapsed else 0.0,
        'tx_p50': stats.median(lat) if lat else float('nan'),
        'tx_p95': _pct(lat, 0.95),
        'sweep_s': sweep,
        'refused': len(refused),
        'desync': desync,
    }


def recommend(rows, tolerance=0.10):
    """The FEWEST lanes that get within `tolerance` of the best sweep.

    Extra sockets are not free — a master that serves ten clients and is given
    four by us has six left for everyone else, and every lane is another thing
    to reconnect after an outage. So a lane has to EARN its place: the winner is
    the smallest one that is not meaningfully slower than the best, and a level
    that started refusing connections or erroring is not a candidate at all.
    """
    ok = [r for r in rows if not r['refused'] and r['reads']
          and r['errors'] <= max(1, r['reads'] * 0.02)
          and not r['desync']]
    if not ok:
        return None, ("every level errored, was refused, or answered late — "
                      "leave it at 1")
    best = min(ok, key=lambda r: r['sweep_s'])
    for r in ok:
        if r['sweep_s'] <= best['sweep_s'] * (1 + tolerance):
            if r['lanes'] == 1 and best['lanes'] > 1:
                return 1, ("this master serializes internally — extra sockets "
                           "do not make it faster, so one lane and a queue")
            gain = rows[0]['sweep_s'] / r['sweep_s'] if rows[0]['sweep_s'] else 1
            return r['lanes'], (f"{gain:.1f}x faster sweep than a single lane, "
                                f"and more lanes add nothing beyond it")
    return 1, "inconclusive"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--host', required=True)
    ap.add_argument('--port', type=int, default=502)
    ap.add_argument('--units', required=True,
                    help='unit ids behind this access point, e.g. 1,2,3,4')
    ap.add_argument('--address', type=int, required=True,
                    help='start of a real, readable block on these units')
    ap.add_argument('--count', type=int, default=50, help='registers per read')
    ap.add_argument('--max-lanes', type=int, default=4)
    ap.add_argument('--seconds', type=float, default=20.0,
                    help='how long to hold each concurrency level')
    ap.add_argument('--timeout', type=float, default=5.0)
    ap.add_argument('--rest', type=float, default=3.0,
                    help='pause between levels, so one does not taint the next')
    a = ap.parse_args()

    logging.getLogger('pymodbus').addHandler(_DESYNC)
    logging.getLogger('pymodbus').setLevel(logging.ERROR)
    units = [int(u) for u in a.units.replace(' ', '').split(',') if u]
    if not units:
        ap.error('--units needs at least one unit id')
    top = min(a.max_lanes, len(units))

    print(f"access point {a.host}:{a.port} · units {units} · "
          f"{a.count} registers from {a.address} · {a.seconds:.0f}s per level")
    print("This reads the live device and competes with anything else polling "
          "it.\n")
    print(f"{'conns':>5} {'reads':>7} {'err':>5} {'late':>5} {'reads/s':>8} "
          f"{'tx p50':>8} {'tx p95':>8} {'sweep':>8}")

    rows = []
    for lanes in range(1, top + 1):
        if rows:
            time.sleep(a.rest)
        r = probe(a.host, a.port, units, a.address, a.count, lanes,
                  a.seconds, a.timeout)
        rows.append(r)
        note = f"  ({r['refused']} connection(s) refused)" if r['refused'] else ""
        if r['desync']:
            note += "  (late answers — this level is over the master's head)"
        print(f"{r['lanes']:>5} {r['reads']:>7} {r['errors']:>5} "
              f"{r['desync']:>5} {r['reads_per_s']:>8.2f} {r['tx_p50']:>8.3f} "
              f"{r['tx_p95']:>8.3f} {r['sweep_s']:>8.2f}{note}")

    k, why = recommend(rows)
    print()
    if k is None:
        print(f"No recommendation: {why}")
        return 1
    print(f"Recommended: max_connections: {k} — {why}")
    floor = rows[k - 1]['sweep_s']
    # Headroom, not the bare sweep: a cadence with no slack overruns on the
    # first retry, and the numbers above are a quiet minute on this endpoint.
    advice = max(1.0, floor * 1.3)
    print(f"At {k} connection(s) a full sweep of {len(units)} unit(s) costs "
          f"{floor:.1f}s, so an interval below about "
          f"{advice:.0f}s is a promise this wire cannot keep.")
    print(f"\nendpoints:\n  - id: <your endpoint>\n    connection:\n"
          f"      host: {a.host}\n      port: {a.port}\n"
          f"      max_connections: {k}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
