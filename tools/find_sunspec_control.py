#!/usr/bin/env python3
"""Find a SunSpec inverter's power-limit control register (model 123).

read_sunspec.py decodes a *meter* (models 213/203) at a fixed offset. An
inverter's controls live in model **123** ("Immediate Controls"), which sits
somewhere in the model chain after the common model — its address is
device-specific, so we WALK the chain to find it, then apply model 123's fixed
field offsets to locate WMaxLimPct, WMaxLim_Ena and the WMaxLimPct scale factor.

Read-only (no writes). Prints the two addresses you need + a ready-to-paste
device-template snippet with the real numbers.

    python tools/find_sunspec_control.py <host> [port] [unit] [base]

port default 502 (inverter Modbus-TCP), base default 40000 (SunSpec map base).
"""
import struct
import sys

from pymodbus.client import ModbusTcpClient

# model 123 field offsets (registers from the model's DATA start = header+2)
OFF_WMAXLIMPCT = 3
OFF_WMAXLIM_ENA = 7
OFF_WMAXLIMPCT_SF = 21
MODEL_123_LEN = 24


def _i16(w):
    return w - 0x10000 if w >= 0x8000 else w


def find_control(host, port=502, unit=1, base=40000):
    c = ModbusTcpClient(host, port=port)
    if not c.connect():
        raise SystemExit(f"cannot connect to {host}:{port}")

    def rd(addr, count):
        rr = c.read_holding_registers(addr, count=count, slave=unit)
        if rr.isError():
            raise SystemExit(f"read error @{addr}: {rr}")
        return rr.registers

    # 1) SunS marker at base (2 regs) — 0x53756e53 = "SunS"
    m = rd(base, 2)
    marker = "".join(chr((w >> 8) & 0xff) + chr(w & 0xff) for w in m)
    if marker != "SunS":
        raise SystemExit(f"no SunSpec marker at {base} (got {marker!r}); "
                         f"try a different base, e.g. 50000, or 40001")

    # 2) walk the model chain: header (ID, L) at addr, common model first at base+2
    addr = base + 2
    chain = []
    m123 = None
    for _ in range(64):                         # safety bound
        mid, mlen = rd(addr, 2)
        if mid == 0xFFFF:                       # end marker
            break
        chain.append((mid, addr, mlen))
        if mid == 123:
            m123 = (addr, mlen)
            break
        addr += mlen + 2                        # next header

    print(f"  SunSpec map @ {base} — model chain:")
    for mid, a, ln in chain:
        tag = "  <-- controls" if mid == 123 else ""
        print(f"    model {mid:>4}  header@{a}  len {ln}{tag}")

    if m123 is None:
        raise SystemExit("\nmodel 123 (Immediate Controls) not present — this "
                         "inverter/firmware may not support Modbus power control, "
                         "or it must be enabled in the inverter settings.")

    hdr_addr, mlen = m123
    data = hdr_addr + 2
    if mlen != MODEL_123_LEN:
        print(f"  ! model 123 length is {mlen} (expected {MODEL_123_LEN}) — "
              f"offsets assume the standard layout; double-check.")

    wmaxlimpct = data + OFF_WMAXLIMPCT
    wmaxlim_ena = data + OFF_WMAXLIM_ENA
    sf_addr = data + OFF_WMAXLIMPCT_SF
    sf = _i16(rd(sf_addr, 1)[0])                # signed exponent
    # our template scale is a DIVISOR: value = raw/scale ; SunSpec: value = raw*10^SF
    scale = round(10 ** (-sf))
    cur_pct_raw = rd(wmaxlimpct, 1)[0]
    cur_ena = rd(wmaxlim_ena, 1)[0]
    c.close()

    print(f"\n  WMaxLimPct      address {wmaxlimpct}   (currently raw {cur_pct_raw} "
          f"= {cur_pct_raw * 10 ** sf:g} %)")
    print(f"  WMaxLim_Ena     address {wmaxlim_ena}   (currently {cur_ena})")
    print(f"  WMaxLimPct_SF   address {sf_addr}   value {sf}  ->  template scale {scale}")
    print(f"\n  Template registers (VERIFY, then set write_safe from your fuse math):")
    print(f"""    {{"address": {wmaxlimpct}, "name": "WMaxLimPct", "label": "Active power limit",
     "unit": "%", "data_type": "uint16", "register_type": "holding", "scale": {scale},
     "writable": true, "write_min": 0, "write_max": 100, "write_safe": 50}},
    {{"address": {wmaxlim_ena}, "name": "WMaxLim_Ena", "label": "Limit enable",
     "data_type": "uint16", "register_type": "holding",
     "writable": true, "write_min": 0, "write_max": 1, "write_safe": 1}}""")
    return {"WMaxLimPct": wmaxlimpct, "WMaxLim_Ena": wmaxlim_ena,
            "WMaxLimPct_SF_addr": sf_addr, "SF": sf, "scale": scale}


if __name__ == '__main__':
    host = sys.argv[1] if len(sys.argv) > 1 else '127.0.0.1'
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 502
    unit = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    base = int(sys.argv[4]) if len(sys.argv) > 4 else 40000
    find_control(host, port, unit, base)
