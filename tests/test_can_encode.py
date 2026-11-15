# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Virtual BMS V0 (ADR-0037): the CAN frame builder is the exact inverse of the
decoder, proven against REAL Seplos→Deye frames captured on the Pi5 bench
(mbg-bench/captures/can0-deye-seplos-20261026.log), and it can never transmit."""
import json
import pathlib

import pytest

from multibus.codec.can_encode import (
    TRANSMITS,
    encode_frame,
    encode_signal,
    patch_frame,
    place_raw,
    roundtrip,
    uncovered_bits,
)
from multibus.codec.can_signal import decode_signal, extract_raw, parse_can_id

_TPL = pathlib.Path(__file__).resolve().parent.parent / "multibus" / "device_templates" / "pylontech_can_inverter.json"

# Real frames from can0-deye-seplos-20261026.log (Deye SUN-8K-SG01LP1 + Seplos V2).
CAPTURE = {
    0x351: bytes.fromhex("4002B80BDA16D001"),
    0x355: bytes.fromhex("1D006400D90CB40C"),
    0x356: bytes.fromhex("82142D00F500E300"),
    0x359: bytes.fromhex("0000000003480307"),
    0x35C: bytes.fromhex("C000360100000000"),
    0x35E: bytes.fromhex("5348456E65726779"),
    0x370: bytes.fromhex("F500E300D90CB40C"),
    0x371: bytes.fromhex("0200030003000300"),
}


def _rows_by_id():
    regs = json.loads(_TPL.read_text())["device_template"]["registers"]
    out = {}
    for r in regs:
        cid = parse_can_id(r.get("can_id"))
        if cid is not None and r.get("start_bit") is not None:
            out.setdefault(cid, []).append(r)
    return out


ROWS = _rows_by_id()


# ── 1. the template decodes the real frames into sane values ──────────────────
def test_template_decodes_the_capture():
    vals = {r["name"]: decode_signal(CAPTURE[0x351], r) for r in ROWS[0x351]}
    # raw ints; scale is applied downstream (CVL 576→57.6V, CCL 3000→300A, etc.)
    assert vals["charge_voltage_limit"] == 576 and vals["charge_current_limit"] == 3000
    assert vals["discharge_current_limit"] == 5850 and vals["discharge_voltage_limit"] == 464
    assert decode_signal(CAPTURE[0x35E], next(r for r in ROWS[0x35E] if r["name"] == "manufacturer")) == "SHEnergy"
    # signed current decodes positive here (charging) and the sign path works
    cur = next(r for r in ROWS[0x356] if r["name"] == "battery_current")
    assert decode_signal(CAPTURE[0x356], cur) == 0x002D


# ── 2. round-trip: decode → encode reproduces the modeled bits ────────────────
@pytest.mark.parametrize("cid", list(CAPTURE))
def test_roundtrip_reproduces_covered_bits(cid):
    rebuilt = roundtrip(CAPTURE[cid], ROWS.get(cid, []))
    assert len(rebuilt) == 8
    # every bit the template rows cover must come back identical; the only
    # differences allowed are bits no row models (vendor/reserved bytes).
    gap = uncovered_bits(CAPTURE[cid], rebuilt)
    modeled = set()
    from multibus.codec.can_signal import signal_bits
    for r in ROWS.get(cid, []):
        modeled |= set(signal_bits(int(r["start_bit"]), int(r["bit_length"]),
                                   str(r.get("byte_order") or "little").lower()))
    assert not (set(gap) & modeled), f"0x{cid:X}: modeled bits not reproduced: {sorted(set(gap)&modeled)}"


def test_fully_modeled_frames_are_byte_exact():
    # 0x351 (4×uint16 = all 8 bytes) and 0x35E (8-byte text) are covered end to
    # end, so the encoder reproduces them to the byte.
    for cid in (0x351, 0x35E):
        assert roundtrip(CAPTURE[cid], ROWS[cid]) == CAPTURE[cid], f"0x{cid:X} not byte-exact"


def test_coverage_report(capsys):
    # documents which frames carry bits the template does not model yet (L0 input):
    # a non-exact round-trip is expected on vendor-extended frames, never a crash.
    report = {f"0x{cid:X}": len(uncovered_bits(CAPTURE[cid], roundtrip(CAPTURE[cid], ROWS.get(cid, []))))
              for cid in sorted(CAPTURE)}
    with capsys.disabled():
        print("\n  V0 template coverage (uncovered bits per captured frame):", report)
    assert report["0x351"] == 0 and report["0x35E"] == 0   # the frames we rely on
    assert all(v >= 0 for v in report.values())


# ── 3. pure inverse property (no template needed) ─────────────────────────────
@pytest.mark.parametrize("bo", ["little", "big"])
@pytest.mark.parametrize("sb,bl,raw", [(0, 16, 0xABCD), (16, 16, 0x0001), (7, 1, 1), (32, 8, 0x5A), (24, 24, 0x123456)])
def test_place_is_inverse_of_extract(bo, sb, bl, raw):
    buf = bytearray(8)
    place_raw(buf, sb, bl, raw, bo)
    assert extract_raw(bytes(buf), sb, bl, bo) == (raw & ((1 << bl) - 1))


def test_full_frame_little_roundtrip():
    # a 64-bit little signal spans the whole frame (big/Motorola from bit 0 would
    # overflow the sawtooth numbering — extract_raw rejects it the same way)
    buf = bytearray(8)
    place_raw(buf, 0, 64, 0x0123456789ABCDEF, "little")
    assert extract_raw(bytes(buf), 0, 64, "little") == 0x0123456789ABCDEF


def test_signed_and_flag_roundtrip():
    cur = {"start_bit": 16, "bit_length": 16, "byte_order": "little", "data_type": "int16"}
    buf = bytearray(8)
    encode_signal(buf, cur, -45)                     # 4.5 A discharging
    assert decode_signal(bytes(buf), cur) == -45
    flag = {"start_bit": 7, "bit_length": 1, "byte_order": "little", "data_type": "uint16"}
    buf = bytearray(8)
    encode_signal(buf, flag, 1)
    assert bytes(buf)[0] == 0x80 and decode_signal(bytes(buf), flag) == 1


def test_patch_frame_changes_one_field_keeps_the_rest():
    # Virtual BMS replay: take the real 0x355, change only SOC, confirm SOH and
    # the vendor cell-voltage tail (bytes 4-7, which the template does NOT model)
    # stay byte-identical to the capture.
    base = CAPTURE[0x355]
    soc = next(r for r in ROWS[0x355] if r["name"] == "soc")
    soh = next(r for r in ROWS[0x355] if r["name"] == "soh")
    patched = patch_frame(base, [(soc, 55)])
    assert decode_signal(patched, soc) == 55                 # changed
    assert decode_signal(patched, soh) == decode_signal(base, soh)   # untouched
    assert patched[4:8] == base[4:8]                         # unmodeled vendor bytes kept
    assert patched[2:4] == base[2:4]                         # SOH bytes kept
    # and only the two SOC bytes differ from the original
    assert sum(a != b for a, b in zip(patched, base)) <= 2


def test_encode_frame_builds_351_from_scratch():
    # synthesize 0x351 from raw limit values → the exact bytes the Deye accepts
    rows = {r["name"]: r for r in ROWS[0x351]}
    frame = encode_frame([
        (rows["charge_voltage_limit"], 576), (rows["charge_current_limit"], 3000),
        (rows["discharge_current_limit"], 5850), (rows["discharge_voltage_limit"], 464)])
    assert frame == CAPTURE[0x351]


# ── 4. the hard no-transmit guarantee (ADR-0037) ──────────────────────────────
def test_module_cannot_transmit():
    assert TRANSMITS is False
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "multibus" / "codec" / "can_encode.py").read_text()
    for forbidden in ("import socket", "import can", "import serial", "AF_CAN",
                      "PF_CAN", ".send(", ".sendto(", "bus.write", "sock."):
        assert forbidden not in src, f"can_encode must not transmit — found {forbidden!r}"
