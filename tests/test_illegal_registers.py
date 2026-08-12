# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Per-device illegal-register skip-list: the batch reader never bridges a
merged read across an address the slave rejects (exception 02), and drops a
selected register sitting on one — so one unmapped hole can't poison a block."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from multibus.config import SelectedRegister, parse_address_list
from multibus.modbus_client import RegisterPoller
from multibus.register_parser import RegisterParser


def _reg(addr, dt="uint16"):
    return SelectedRegister(address=addr, name=f"r{addr}", label="", unit="",
                            data_type=dt, poll_group="normal")


def _poller(regs, illegal=(), max_gap=10):
    conn = MagicMock()
    conn.config = SimpleNamespace(max_gap=max_gap, illegal_registers=list(illegal))
    return RegisterPoller("normal", 5, regs, conn, RegisterParser(),
                          publish_callback=lambda *a: None)


def _spans(poller):
    return [(g["start"], g["end"]) for g in poller._read_groups]


# ── parse_address_list ────────────────────────────────────────────────────────

def test_parse_address_list_accepts_int_and_hex_and_drops_junk():
    assert parse_address_list([10, "0x0A", "20", "oops", 70000, -1]) == [10, 20]


# ── batch splitting ───────────────────────────────────────────────────────────

def test_merges_across_gap_without_illegal():
    # 0 and 3 within max_gap 10 → one merged read [0,4)
    assert _spans(_poller([_reg(0), _reg(3)])) == [(0, 4)]


def test_illegal_in_gap_forces_a_split():
    # address 2 (unselected gap-fill) is illegal → must NOT bridge 0..3
    p = _poller([_reg(0), _reg(3)], illegal=[2])
    assert _spans(p) == [(0, 1), (3, 4)]              # two separate reads


def test_selected_register_on_illegal_is_dropped():
    p = _poller([_reg(0), _reg(3), _reg(9)], illegal=[3])
    addrs = sorted(r.address for g in p._read_groups for r in g["registers"])
    assert addrs == [0, 9]                            # 3 skipped entirely


def test_multi_register_span_hitting_illegal_is_dropped():
    # a uint32 at 4 spans 4..6; 5 is illegal → the whole register is skipped
    p = _poller([_reg(0), _reg(4, dt="uint32")], illegal=[5])
    addrs = sorted(r.address for g in p._read_groups for r in g["registers"])
    assert addrs == [0]


def test_no_illegal_list_is_unchanged():
    p = _poller([_reg(0), _reg(1), _reg(2)])
    assert _spans(p) == [(0, 3)]


# ── config round-trip ─────────────────────────────────────────────────────────

def test_illegal_registers_round_trip_through_config(tmp_path):
    import yaml
    from multibus.config import Config
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "modbus": {"host": "1.2.3.4", "illegal_registers": [50, "0x40", 50]},
    }))
    c = Config(str(cfg_path))
    assert c.modbus.illegal_registers == [64, 50] or c.modbus.illegal_registers == [50, 64]
    assert sorted(c.modbus.illegal_registers) == [50, 64]     # deduped + parsed
    assert sorted(c.to_dict()["modbus"]["illegal_registers"]) == [50, 64]
