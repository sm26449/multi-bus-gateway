#!/usr/bin/env python3
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Janitza UMG512-Pro → InfluxDB gap backfill.

Self-heals holes in the InfluxDB bucket by recovering data from the meter's
onboard 1-minute recording. The collector polls live values over Modbus and
publishes them; when its network path drops (e.g. a brief grid dip reboots the
cabinet switch), the live stream — and InfluxDB — gets a hole. But the meter is
powered by the grid it measures and keeps logging to its own flash throughout.
This job reads that flash via the meter's HTTP ``HIST_DATA`` API and writes the
missing points back, matching the live ``InfluxDBPublisher`` schema exactly
(measurement ``voltage``; the canonical per-name field — ``voltage_l1_n`` …,
post-Migration A — + ``value``; tags ``device/address/name/poll_group/phase``
+ ``type``|``connection``) so charts just fill in. A ``backfilled=1`` field marks recovered points for traceability.

Scope: L-N and L-L voltages at 1-minute resolution — the only parameters the
UMG512 records historically (current/power/frequency are live-only, never in
the recording, so a comms gap loses them permanently — backfill can't invent
them). Voltage is the power-quality record, so it is the right thing to recover.

Config comes from the same env the collector uses: ``INFLUXDB_URL``,
``INFLUXDB_TOKEN``, ``INFLUXDB_ORG``, ``INFLUXDB_BUCKET``, ``MODBUS_HOST`` (the
meter's HTTP API is on that host). Run it inside the collector container so all
of those — plus ``influxdb_client`` and the meter network path — are present.

Modes::

    python -m multibus.backfill                 # auto: detect trailing gap and heal it
    python -m multibus.backfill --window A B     # explicit UTC ISO window (past incident)
    python -m multibus.backfill --dry-run        # fetch + report, write nothing
    python -m multibus.backfill --verbose        # per-series logging

Idempotent: points land on exact minute boundaries keyed by
(measurement, tagset, field, timestamp); re-running overwrites identically.

Cron (host triggers the container every 10 min)::

    */10 * * * * /opt/multi-bus-gateway/tools/run-backfill.sh >> /var/log/mbg-backfill.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

# ── Config (env-overridable; same vars as the collector) ─────────────────────
METER_URL = os.environ.get("JANITZA_METER_URL") or f"http://{os.environ.get('MODBUS_HOST', '192.168.1.100')}"
INFLUX_URL = os.environ.get("INFLUXDB_URL", "http://influxdb:8086")
INFLUX_ORG = os.environ.get("INFLUXDB_ORG", "janitza")
INFLUX_BUCKET = os.environ.get("INFLUXDB_BUCKET", "janitza")
INFLUX_TOKEN = os.environ.get("INFLUXDB_TOKEN", "").strip()
MIN_GAP_SEC = int(os.environ.get("JANITZA_MIN_GAP_SEC", "180"))
MAX_LOOKBACK_H = int(os.environ.get("JANITZA_MAX_LOOKBACK_H", "48"))
TB = 60  # meter recording timebase (seconds) — 1-minute means

# hist_param -> register ADDRESS only. Everything else — name tag, field
# name, measurement, extra tags, poll_group — comes from the LIVE selection
# (config/selected_registers.json) through the publisher's own build_point,
# so backfilled points land in byte-identical series (external audit B3: the
# transcribed schema here drifted into a parallel series the history chart
# never saw, while the gap detector "confirmed" repairs that never landed).
PARAMS: list[tuple[str, int]] = [
    ("_ULN[0]", 19000),
    ("_ULN[1]", 19002),
    ("_ULN[2]", 19004),
    ("_ULL[0]", 19006),
    ("_ULL[1]", 19008),
    ("_ULL[2]", 19010),
]

REGISTERS_PATH = os.environ.get("JANITZA_REGISTERS_PATH",
                                "config/selected_registers.json")


def load_registers() -> dict[int, object]:
    """The live selection, keyed by address — the schema authority."""
    from .config import Config
    with open(REGISTERS_PATH) as fh:
        data = json.load(fh)
    return {r.address: r for r in Config._parse_selected_payload(data)}


def _ref_name() -> str:
    """Name tag of the reference series for gap detection — the FIRST param's
    register in the live selection (falls back to the canonical name so a
    missing selection degrades to the historical behaviour, not a crash)."""
    try:
        reg = load_registers().get(PARAMS[0][1])
        return reg.name if reg else "voltage_l1_n"
    except Exception:  # noqa: BLE001
        return "voltage_l1_n"


def _http_json(url: str, timeout: int = 12) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as _r:   # close the FD
        raw = _r.read().decode("latin1")
    return json.loads(raw)


def meter_tz_offset() -> int:
    """Device-local epoch = UTC epoch + offset. HIST_DATA timestamps are local."""
    d = _http_json(f"{METER_URL}/lib/get_time_info.html", timeout=8)
    return int(round(d["_SYSTIME"][0] - d["_UTCTIME"][0]))


def fetch_hist(param: str, start_utc: float, end_utc: float, tz: int) -> list[tuple[float, float]]:
    """Return [(value, utc_epoch), ...] from the meter recording for [start, end]."""
    start_local = int(start_utc) + tz
    cnt = int(math.ceil((end_utc - start_utc) / TB)) + 3
    qs = f"val$={param}&start={start_local}&cnt={cnt}&tb={TB}"
    arr = _http_json(f"{METER_URL}/hist_data/HIST_DATA.html?{qs}")[param][0]
    out: list[tuple[float, float]] = []
    for item in arr:
        if not item or len(item) < 2 or item[0] is None:
            continue
        val, local_ts = float(item[0]), float(item[1])
        utc = local_ts - tz
        if start_utc <= utc <= end_utc:
            out.append((val, utc))
    return out


def influx_latest_voltage_utc() -> float | None:
    """UTC epoch of the most recent live L1 L-N voltage point, or None."""
    from influxdb_client import InfluxDBClient

    flux = (
        f'from(bucket:"{INFLUX_BUCKET}") |> range(start:-{MAX_LOOKBACK_H}h) '
        '|> filter(fn:(r)=>r._measurement=="voltage" and r._field=="value" '
        f'and r.name=="{_ref_name()}") |> last() |> keep(columns:["_time"])'
    )
    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as c:
        for table in c.query_api().query(flux):
            for rec in table.records:
                return rec.get_time().timestamp()
    return None


def influx_interior_gap_utc(min_gap_sec: float) -> tuple[float, float] | None:
    """Earliest INTERIOR gap ≥ min_gap_sec in the reference series over the
    lookback window, as (start_utc, end_utc) — or None.

    Audit DP-14: the tail-gap check alone missed the classic incident shape
    'Influx down 14:00-15:00, live writes resumed 15:00' — by the next cron
    tick the tail looked fresh and the hour-long hole was never repaired.
    aggregateWindow(createEmpty) marks the empty minutes; we return the
    hole's bounds so the auto path can heal it."""
    from influxdb_client import InfluxDBClient

    every = max(60, int(min_gap_sec // 3))
    flux = (
        f'from(bucket:"{INFLUX_BUCKET}") |> range(start:-{MAX_LOOKBACK_H}h) '
        '|> filter(fn:(r)=>r._measurement=="voltage" and r._field=="value" '
        f'and r.name=="{_ref_name()}") '
        f'|> aggregateWindow(every: {every}s, fn: count, createEmpty: true) '
        '|> keep(columns:["_time","_value"])'
    )
    gap_start = None
    last_time = None
    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as c:
        for table in c.query_api().query(flux):
            for rec in table.records:
                t = rec.get_time().timestamp()
                empty = not rec.get_value()
                if empty and gap_start is None:
                    gap_start = t - every          # window END stamps the bucket
                elif not empty and gap_start is not None:
                    if (t - every) - gap_start >= min_gap_sec:
                        return gap_start, t
                    gap_start = None
                last_time = t
    # a gap running to the edge of the window is the TAIL gap — the caller's
    # existing latest-point check owns that case
    _ = last_time
    return None


def backfill(start_utc: float, end_utc: float, dry: bool, verbose: bool) -> int:
    from influxdb_client import InfluxDBClient  # noqa: F401
    from influxdb_client.client.write_api import SYNCHRONOUS

    from .influxdb_publisher import build_point

    tz = meter_tz_offset()
    by_addr = load_registers()
    written = 0
    client = None if dry else InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    try:
        wapi = None if dry else client.write_api(write_options=SYNCHRONOUS)
        for param, addr in PARAMS:
            reg = by_addr.get(addr)
            if reg is None:
                # deselected/renamed register: skipping is CORRECT — writing
                # with a transcribed schema would recreate the parallel series
                print(f"  {param:9} @{addr}: not in the live selection — skipped")
                continue
            pts = fetch_hist(param, start_utc, end_utc, tz)
            if verbose or dry:
                span = (f"{datetime.fromtimestamp(pts[0][1], timezone.utc):%H:%M}"
                        f"–{datetime.fromtimestamp(pts[-1][1], timezone.utc):%H:%M}") if pts else "—"
                print(f"  {param:9} {reg.name:11} -> {len(pts):3} pts  {span}")
            if dry:
                continue
            for val, utc in pts:
                # the publisher's own schema + the backfill marker field
                p = build_point(reg, float(val), float(utc),
                                poll_group=reg.poll_group).field("backfilled", 1)
                wapi.write(bucket=INFLUX_BUCKET, record=p)
                written += 1
        if wapi:
            wapi.close()
    finally:
        if client:
            client.close()
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backfill janitza InfluxDB gaps from the meter recording")
    ap.add_argument("--window", nargs=2, metavar=("START", "STOP"),
                    help="explicit UTC ISO window, e.g. 2026-06-03T11:18 2026-06-03T15:22")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    if not INFLUX_TOKEN:
        print("ERROR: INFLUXDB_TOKEN not set", file=sys.stderr)
        return 2

    def iso(s: str) -> float:
        # Naive input is treated as UTC (the --window contract). An explicit
        # offset (…+03:00, …Z) is honoured, not silently relabelled as UTC.
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    if args.window:
        start_utc, end_utc = iso(args.window[0]), iso(args.window[1])
        print(f"[manual] window {args.window[0]} .. {args.window[1]} UTC")
    else:
        now = time.time()
        latest = influx_latest_voltage_utc()
        if latest is not None:
            gap = now - latest
            if gap <= MIN_GAP_SEC:
                # tail is fresh — but an INTERIOR hole (outage that already
                # recovered) hides behind it (audit DP-14): scan for one
                hole = influx_interior_gap_utc(MIN_GAP_SEC)
                if hole is None:
                    print(f"[auto] no gap (last point {gap:.0f}s ago ≤ {MIN_GAP_SEC}s, "
                          f"no interior hole) — nothing to do")
                    return 0
                start_utc, end_utc = hole[0] - 120, hole[1] + 120
                print(f"[auto] interior hole detected: "
                      f"{datetime.fromtimestamp(hole[0], timezone.utc):%Y-%m-%d %H:%M} .. "
                      f"{datetime.fromtimestamp(hole[1], timezone.utc):%H:%M} UTC — healing")
                n = backfill(start_utc, end_utc, args.dry_run, args.verbose)
                print(f"{'[dry-run] would write' if args.dry_run else 'wrote'} "
                      f"{n} points across {len(PARAMS)} series")
                return 0
            print(f"[auto] gap detected: {gap / 60:.1f} min since last point "
                  f"({datetime.fromtimestamp(latest, timezone.utc):%Y-%m-%d %H:%M} UTC)")
            start_utc = max(latest - 120, now - MAX_LOOKBACK_H * 3600)
        else:
            print(f"[auto] no recent data in {MAX_LOOKBACK_H}h — backfilling full lookback")
            start_utc = now - MAX_LOOKBACK_H * 3600
        end_utc = now

    n = backfill(start_utc, end_utc, args.dry_run, args.verbose)
    print(f"{'[dry-run] would write' if args.dry_run else 'wrote'} {n} points across {len(PARAMS)} series")
    return 0


if __name__ == "__main__":
    sys.exit(main())
