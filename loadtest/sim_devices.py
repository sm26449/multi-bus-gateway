#!/usr/bin/env python3
# Multi-Bus Gateway — load-test harness (S1: fake Modbus TCP device fleet).
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""S1 — fake Modbus TCP device fleet.

Serves N slave units on one TCP port, each with a realistic register map whose
values CHANGE every tick, so the gateway under test exercises real
change-detection + freshness (a static map would make MBG's job artificially
easy and overstate capacity — see the plan's "don't fool ourselves" note).

Run several ports for a bigger fleet:
    python sim_devices.py --port 6502 --units 8 --tick-ms 250
    python sim_devices.py --port 6503 --units 8 --tick-ms 250

Register map (holding, 0-based) mirrors a Janitza/EM24-style meter:
    0x0000.. voltages L1..L3 (int32 ×10)      — churn each tick
    0x000c.. currents L1..L3 (int32 ×1000)    — churn each tick
    0x0028   total power     (int32 ×10)       — churn each tick
    0x0100.. energy counters (int32, monotonic increasing)
Values move within realistic ranges so change-detection sees genuine deltas.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import struct

from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

# Deterministic pseudo-random walk WITHOUT Math.random-style nondeterminism:
# each unit's phase is derived from its id + tick, so runs are reproducible.
# BLOCK_SIZE is set from --block-size so the served range covers whatever the
# device template reads (e.g. janitza_umg512_pro reads up to addr ~19636, so a
# real-production-template device ramp needs ~20000). Unwritten addresses read
# 0 — reads still succeed, so the device stays fresh (poll-load is the axis).
BLOCK_SIZE = 512


def _i32(words_lo_hi_value: int) -> tuple[int, int]:
    """Encode a signed 32-bit int as (hi, lo) big-endian register words."""
    b = struct.pack(">i", int(words_lo_hi_value) & 0xFFFFFFFF if words_lo_hi_value >= 0
                    else struct.unpack(">I", struct.pack(">i", int(words_lo_hi_value)))[0])
    hi, lo = struct.unpack(">HH", b)
    return hi, lo


def _make_devices(units: int) -> list[SimDevice]:
    """One SimDevice per unit id; the shared block serves holding + input."""
    return [SimDevice(id=uid, simdata=[SimData(address=0, count=BLOCK_SIZE,
                                               values=0,
                                               datatype=DataType.REGISTERS)])
            for uid in range(1, units + 1)]


def _write_i32(regs: dict, uid: int, addr: int, value: int) -> None:
    hi, lo = _i32(value)
    # LOW word first — the EM24 wire convention (Reg_s32l) that the
    # carlo_gavazzi_em24 template decodes. Serving hi-first made the whole
    # decoded chain (store → MQTT → vmeter) carry word-swapped garbage while
    # LOOKING consistent to an equally-swapped test reader.
    regs[uid][addr:addr + 2] = [lo, hi]         # atomic slice into the live block


async def _churn(regs: dict, units: int, tick_ms: float) -> None:
    """Every tick, move each unit's live values within realistic ranges and
    advance its energy counters — so MBG sees fresh, changing data."""
    tick = 0
    dt = max(0.02, tick_ms / 1000.0)
    energy = {uid: 1_000_000 + uid * 1000 for uid in range(1, units + 1)}
    while True:
        for uid in range(1, units + 1):
            ph = (tick * 0.1) + uid            # per-unit phase, deterministic
            v = 230.0 + 5.0 * math.sin(ph)     # ~230 V ±5
            i = 5.0 + 2.0 * math.sin(ph * 1.3) # ~5 A ±2
            p = v * i * 3                       # rough 3-phase power
            _write_i32(regs, uid, 0x0000, int(v * 10))       # L1 voltage ×10
            _write_i32(regs, uid, 0x0002, int((v + 1) * 10))
            _write_i32(regs, uid, 0x0004, int((v - 1) * 10))
            _write_i32(regs, uid, 0x000c, int(i * 1000))     # L1 current ×1000
            _write_i32(regs, uid, 0x000e, int((i + 0.2) * 1000))
            _write_i32(regs, uid, 0x0010, int((i - 0.2) * 1000))
            _write_i32(regs, uid, 0x0028, int(p * 10))       # total power ×10
            energy[uid] += max(1, int(p * dt / 3600))       # Wh accrual
            _write_i32(regs, uid, 0x0100, energy[uid])       # import energy
        tick += 1
        await asyncio.sleep(dt)


async def main() -> None:
    ap = argparse.ArgumentParser(description="S1 fake Modbus TCP device fleet")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=6502)
    ap.add_argument("--units", type=int, default=8, help="slave unit-ids on this port")
    ap.add_argument("--tick-ms", type=float, default=250.0, help="value-churn period")
    ap.add_argument("--block-size", type=int, default=512,
                    help="holding registers per unit (use ~20000 for janitza maps)")
    args = ap.parse_args()

    global BLOCK_SIZE
    BLOCK_SIZE = args.block_size
    server = ModbusTcpServer(_make_devices(args.units),
                             address=(args.host, args.port))
    # live registers list per unit — churn mutates these in place
    regs = {uid: server.context.devices[uid].block["x"][2]
            for uid in range(1, args.units + 1)}
    asyncio.create_task(_churn(regs, args.units, args.tick_ms))
    print(f"[sim_devices] serving {args.units} units on {args.host}:{args.port} "
          f"(churn {args.tick_ms:.0f} ms)", flush=True)
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
