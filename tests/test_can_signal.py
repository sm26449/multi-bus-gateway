# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""CAN signal rows (ADR-0020): the decoder on captured bytes, the template
checks, the transport a CAN map declares.

Expected values come from outside our code: the bench capture's bytes read
against the Pylontech / Seplos documents by hand, and vectors decoded by
cantools 45.1.0 (raw, unscaled) — never from an encoder of ours."""
import json
from pathlib import Path

import pytest

from multibus.can_signal import (decode_signal, extract_raw, overlap_warnings,
                                 parse_can_id, parse_candump_line, template_errors)
from multibus.device_template import parse_template, template_transport, validate_template

FIX = Path(__file__).parent / "fixtures" / "can" / "deye_seplos_v2_20261026.candump"


def sig(sb, bl, bo="little", dt="uint16", **kw):
    return {"start_bit": sb, "bit_length": bl, "byte_order": bo, "data_type": dt, **kw}


# ── the bench capture, by hand against the documents ─────────────────────────

def frame(hexdata):
    return bytes.fromhex(hexdata)


def test_pylontech_0x351_limits_from_the_bench():
    d = frame("4002B80BDA16D001")
    assert decode_signal(d, sig(0, 16)) == 576            # 57.6 V
    assert decode_signal(d, sig(16, 16, dt="int16")) == 3000     # 300.0 A
    assert decode_signal(d, sig(32, 16, dt="int16")) == 5850     # 585.0 A
    assert decode_signal(d, sig(48, 16)) == 464           # 46.4 V


def test_pylontech_0x356_signed_current_and_voltage():
    d = frame("82142D00F500E300")
    assert decode_signal(d, sig(0, 16, dt="int16")) == 5250       # 52.50 V
    assert decode_signal(d, sig(16, 16, dt="int16")) == 45        # +4.5 A
    assert decode_signal(d, sig(32, 16, dt="int16")) == 245       # 24.5 °C
    # a discharge current is two's complement: 0xFF85 = -12.3 A
    assert decode_signal(frame("821485FFF500E300"), sig(16, 16, dt="int16")) == -123
    assert decode_signal(frame("821485FFF500E300"), sig(16, 16, dt="uint16")) == 0xFF85


def test_pylontech_0x35c_request_bits_and_0x359_count():
    d = frame("C000360100000000")
    assert decode_signal(d, sig(7, 1)) == 1               # charge enable
    assert decode_signal(d, sig(6, 1)) == 1               # discharge enable
    assert decode_signal(d, sig(5, 1)) == 0               # force charge I
    assert decode_signal(d, sig(16, 16)) == 310           # LuxPower cycle count
    d = frame("0000000003480307")
    assert decode_signal(d, sig(32, 8)) == 3              # modules
    assert decode_signal(d, sig(40, 16)) == 840           # LuxPower capacity, Ah
    assert decode_signal(d, sig(56, 8)) == 0b111          # Seplos: packs 0..2 online


def test_manufacturer_text():
    assert decode_signal(frame("5348456E65726779"), sig(0, 64, dt="string:4")) == "SHEnergy"
    assert decode_signal(frame("50594C4F4E202020"), sig(0, 64, dt="string:4")) == "PYLON"
    assert decode_signal(frame("4259440000000000"), sig(0, 64, dt="string:4")) == "BYD"


# ── cantools 45.1.0 vectors: DBC semantics, both byte orders ─────────────────
# messages from a test DBC: MOTO (Motorola, @0) and INTEL (@1); raw values as
# cantools decodes them (scaling=False)
MOTO = {"Enable": (7, 1, False), "AverageRadius": (6, 6, False), "Temperature": (0, 12, True),
        "Wide": (39, 20, True), "Tail": (51, 4, False)}
INTEL = {"A": (0, 16, True), "B": (19, 9, False), "C": (28, 4, True)}
VECTORS = [
    ("MOTO", "52F22665A60C12D2", {"Enable": 0, "AverageRadius": 41, "Temperature": 1937, "Wide": -368447, "Tail": 2}),
    ("MOTO", "89185D950EE88136", {"Enable": 1, "AverageRadius": 4, "Temperature": -1854, "Wide": 61064, "Tail": 1}),
    ("MOTO", "09166F6B113D178D", {"Enable": 0, "AverageRadius": 4, "Temperature": -1869, "Wide": 70609, "Tail": 7}),
    ("MOTO", "6C0FD3901FF239A1", {"Enable": 0, "AverageRadius": 54, "Temperature": 126, "Wide": 130851, "Tail": 9}),
    ("MOTO", "C006E00000000000", {"Enable": 1, "AverageRadius": 32, "Temperature": 55}),
    ("INTEL", "A095F20F9395650C", {"A": -27232, "B": 510, "C": 0, "F": 1.7686529144774417e-31}),
    ("INTEL", "F9380B8EDB224A6B", {"A": 14585, "B": 449, "C": -8, "F": 2.443676158595248e+26}),
    ("INTEL", "248A1E924E8FD0AE", {"A": -30172, "B": 67, "C": -7, "F": -9.484200924614328e-11}),
    ("INTEL", "2E1A9492A3305F18", {"A": 6702, "B": 82, "C": -7, "F": 2.8846629736471825e-24}),
]


@pytest.mark.parametrize("msg,data,expected", VECTORS)
def test_dbc_semantics_match_cantools(msg, data, expected):
    sigs = MOTO if msg == "MOTO" else INTEL
    bo = "big" if msg == "MOTO" else "little"
    for name, want in expected.items():
        if name == "F":
            got = decode_signal(bytes.fromhex(data), sig(32, 32, dt="float32"))
            assert got == pytest.approx(want, rel=1e-6)
            continue
        sb, bl, signed = sigs[name]
        dt = ("int32" if bl > 16 else "int16") if signed else ("uint32" if bl > 16 else "uint16")
        assert decode_signal(bytes.fromhex(data), sig(sb, bl, bo, dt)) == want, name


def test_short_frame_is_a_decode_error_not_a_zero():
    with pytest.raises(ValueError):
        extract_raw(b"\x01\x02", 16, 16)
    with pytest.raises(ValueError):
        extract_raw(b"\x01", 7, 16, "big")
    assert extract_raw(b"\x01\x02", 0, 16) == 0x0201


# ── ids and candump lines ────────────────────────────────────────────────────

def test_parse_can_id_forms():
    assert parse_can_id("0x351") == 0x351
    assert parse_can_id(849) == 849
    assert parse_can_id("849") == 849
    assert parse_can_id(True) is None and parse_can_id("x") is None and parse_can_id(None) is None


def test_candump_lines():
    assert parse_candump_line("(1793024703.092769) can0 351#4002B80BDA16D001") == \
        (1793024703.092769, "can0", 0x351, False, bytes.fromhex("4002B80BDA16D001"))
    assert parse_candump_line("(1.0) vcan0 18FF50E5#01")[2:4] == (0x18FF50E5, True)
    assert parse_candump_line("(1.0) can0 305#R")[4] == b""
    assert parse_candump_line("garbage") is None


def test_the_fixture_is_the_bench_capture():
    lines = FIX.read_text().splitlines()
    ids = {parse_candump_line(x)[2] for x in lines}
    assert ids == {0x305, 0x351, 0x355, 0x356, 0x359, 0x35C, 0x35E, 0x370, 0x371}
    assert len(lines) == 1071


# ── template checks ──────────────────────────────────────────────────────────

def tpl(rows, transports=("can",)):
    return {"device_template": {"id": "can_t", "name": "t",
                                "protocol": {"transports": list(transports)},
                                "registers": rows}}


def r(addr=1, **kw):
    base = {"address": addr, "name": f"s{addr}", "data_type": "uint16", "can_id": "0x351",
            "start_bit": 0, "bit_length": 16, "byte_order": "little"}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def errs(*rows, **kw):
    return validate_template(tpl(list(rows), **kw))


def test_a_good_can_row_validates():
    assert errs(r()) == []


@pytest.mark.parametrize("bad,needle", [
    (dict(start_bit=60, bit_length=8), "do not fit"),
    (dict(start_bit=64), "start_bit"),
    (dict(bit_length=0), "bit_length"),
    (dict(bit_length=65), "bit_length"),
    (dict(byte_order=None), "byte_order is required"),
    (dict(byte_order="motorola"), "byte_order"),
    (dict(can_id="0x800"), "can_id_extended"),
    (dict(can_id="0x20000000", can_id_extended=True), "29-bit"),
    (dict(can_id="zz"), "can_id must be a number"),
    (dict(can_id_extended="yes"), "true or false"),
    (dict(data_type="int16", bit_length=20), "wider type"),
    (dict(data_type="float32", bit_length=16), "32 IEEE bits"),
    (dict(data_type="sm16"), "not used on CAN"),
    (dict(data_type="string:2", start_bit=0, bit_length=64), "string:4"),
    (dict(data_type="string:4", start_bit=4, bit_length=64), "do not fit"),
    (dict(topic="a/b"), "no topic"),
    (dict(start_bit=48, bit_length=16, byte_order="big"), "do not fit"),
])
def test_bad_can_rows_are_named(bad, needle):
    e = errs(r(**bad))
    assert any(needle in x for x in e), e


def test_extended_ids_and_motorola_in_range():
    assert errs(r(can_id="0x18FF50E5", can_id_extended=True)) == []
    assert errs(r(start_bit=7, bit_length=16, byte_order="big")) == []
    assert errs(r(start_bit=55, bit_length=8, byte_order="big")) == []
    assert errs(r(bit_length=8, byte_order=None)) == []          # ≤ 8 bits: order not needed


def test_a_can_template_names_every_frame_and_others_refuse_can_rows():
    e = errs(r(), {"address": 2, "name": "x", "data_type": "uint16"})
    assert any("each name their frame" in x for x in e), e
    e = errs(r(), transports=("tcp",))
    assert any("declare protocol.transports: [can]" in x for x in e), e
    e = errs({"address": 2, "name": "x", "data_type": "uint16", "start_bit": 0})
    assert any("without can_id" in x for x in e), e


def test_bitrate_checked():
    t = tpl([r()])
    t["device_template"]["protocol"]["bitrate"] = 123
    assert any("bitrate" in x for x in template_errors(t["device_template"]))


def test_overlap_is_a_warning_not_an_error():
    rows = [r(1, bit_length=8), r(2, start_bit=4, bit_length=4)]
    assert errs(*rows) == []
    t = parse_template(tpl(rows))
    w = overlap_warnings(t.registers)
    assert len(w) == 1 and "'s2' overlaps 's1'" in w[0]
    # Motorola bytes 0-1 vs Intel-numbered bit 8 (byte 1's LSB): same bits
    assert overlap_warnings([r(1, start_bit=7, bit_length=16, byte_order="big"),
                             r(2, start_bit=8, bit_length=1)])


def test_template_transport_learns_can_and_rows_round_trip():
    t = parse_template(tpl([r(can_id=0x355, start_bit=16)]))
    assert template_transport(t) == "can"
    row = t.registers[0]
    assert (row.can_id, row.start_bit, row.bit_length, row.byte_order) == (0x355, 16, 16, "little")
    d = row.to_dict()
    assert d["can_id"] == "0x355" and d["start_bit"] == 16 and "can_id_extended" not in d
    # inferred from the rows when transports says nothing
    t2 = tpl([r()], transports=())
    assert template_transport(parse_template(t2)) == "can"


def test_existing_templates_are_untouched():
    for p in sorted(Path("multibus/device_templates").glob("*.json")):
        d = json.loads(p.read_text())
        assert validate_template(d) == [], p.name
        t = parse_template(d)
        if p.name != "pylontech_can_inverter.json":
            assert template_transport(t) != "can", p.name
            assert all(x.can_id is None for x in t.registers), p.name
