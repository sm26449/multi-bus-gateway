#!/usr/bin/env python3
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Seplos collector -> MBG cutover: one config transform, reversible.

Rewrites a hand-maintained config.yaml (text surgery, comments preserved):
  - REMOVES the collector-era MQTT reader devices (seplos-p1..8, seplos-bank)
    and the standalone validation device seplos-tap-p1 (its id is reborn as a
    unit of the endpoint below);
  - APPENDS a `battery-bank` endpoint whose 8 units materialize as rtu_tap
    devices (one shared listen-only reader on the pack bus), publishing the
    collector's exact topics (seplos/battery_N/*, seplos/pack/*) and writing
    InfluxDB bucket `seplos`;
  - RETIRES the stale seeded register dirs so the units reseed fresh from
    seplos_bms_v3_rtu_tap >= 1.5.0 at next boot.

Dry-run by default; --apply writes (with a timestamped backup). Rollback =
restore the backup + start the collector.
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import shutil
import sys

REMOVE_DEVICES = [f"seplos-p{i}" for i in range(1, 9)] + ["seplos-bank", "seplos-tap-p1"]

ENDPOINT_BLOCK = """- id: battery-bank
  name: Battery bank
  enabled: true
  template: seplos_bms_v3_rtu_tap
  mqtt:
    enabled: true
    ha_discovery: false
    topic_prefix: seplos/battery_${unit_id}
    aggregate_prefix: seplos/pack
  influxdb:
    enabled: true
    bucket: seplos
    device_tag: battery_${unit_id}
  connection:
    protocol: rtu_tap
    serial_port: /dev/ttySEPLOS
    baudrate: 19200
    stale_after_s: 60
  units:
""" + "".join(f"  - {{unit_id: {i}, id: seplos-tap-p{i}, name: Seplos pack {i}}}\n"
              for i in range(1, 9))


def split_device_blocks(devices_body: str):
    """Split the devices: section body into top-level `- id: ...` blocks."""
    blocks, cur = [], []
    for line in devices_body.splitlines(keepends=True):
        if re.match(r"^- id:", line) and cur:
            blocks.append("".join(cur))
            cur = []
        cur.append(line)
    if cur:
        blocks.append("".join(cur))
    return blocks


def transform(text: str) -> str:
    m = re.search(r"(?ms)^devices:\n(.*?)(?=^endpoints:)", text)
    if not m:
        raise SystemExit("devices:/endpoints: layout not recognized — aborting")
    body = m.group(1)
    blocks = split_device_blocks(body)
    kept, removed = [], []
    for b in blocks:
        bid = re.match(r"^- id:\s*([^\s#]+)", b)
        if bid and bid.group(1) in REMOVE_DEVICES:
            removed.append(bid.group(1))
            continue
        kept.append(b)
    missing = [d for d in REMOVE_DEVICES if d not in removed
               and d != "seplos-tap-p1"]            # tap-p1 may already be gone
    if missing:
        print(f"note: not present (already removed?): {missing}")
    text = text[:m.start(1)] + "".join(kept) + text[m.end(1):]
    if "id: battery-bank" in text:
        raise SystemExit("battery-bank endpoint already present — aborting")
    # the endpoints list runs until the next top-level key; append before it
    m2 = re.search(r"(?m)^alerts:", text)
    if not m2:
        raise SystemExit("alerts: anchor not found — aborting")
    text = text[:m2.start()] + ENDPOINT_BLOCK + text[m2.start():]
    print(f"removed devices: {removed}")
    print("added endpoint: battery-bank (8 rtu_tap units)")
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("config", help="path to config.yaml")
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()
    cfg = pathlib.Path(args.config)
    text = cfg.read_text()
    out = transform(text)
    if not args.apply:
        print("-- dry-run: no changes written --")
        return 0
    stamp = datetime.date.today().strftime("%Y%m%d")
    backup = cfg.with_name(cfg.name + f".pre-seplos-cutover-{stamp}")
    shutil.copy2(cfg, backup)
    cfg.write_text(out)
    print(f"written; backup at {backup}")
    # retire seeded dirs so units reseed fresh from the 1.5.0 template
    devdir = cfg.parent / "devices"
    retired = devdir / f"_retired-{stamp}"
    moved = []
    for d in REMOVE_DEVICES:
        p = devdir / d
        if p.is_dir():
            retired.mkdir(exist_ok=True)
            shutil.move(str(p), str(retired / d))
            moved.append(d)
    print(f"retired register dirs: {moved} -> {retired.name}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
