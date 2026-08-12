# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Data-readiness gate: an all-zero frame (a sleepy inverter answering a read)
is dropped so 0 V/0 W never reads as real data. Off by default; needs >=2
numeric values all exactly zero."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from multibus.config import SelectedRegister
from multibus.modbus_client import RegisterPoller
from multibus.register_parser import RegisterParser


def _regs(n, dt="uint16"):
    return [SelectedRegister(address=a, name=f"r{a}", label="", unit="V",
                             data_type=dt, poll_group="realtime") for a in range(n)]


def _poller(regs, drop_all_zero=False):
    conn = MagicMock()
    conn.config = SimpleNamespace(drop_all_zero=drop_all_zero, max_gap=10,
                                  illegal_registers=[])
    return RegisterPoller("realtime", 1, regs, conn, RegisterParser(),
                          publish_callback=lambda *a: None)


def test_all_zero_frame_dropped_when_enabled():
    p = _poller(_regs(3), drop_all_zero=True)
    p.connection.read_registers.return_value = [0, 0, 0]      # asleep
    assert p._poll_registers() == {}


def test_any_nonzero_publishes_normally():
    p = _poller(_regs(3), drop_all_zero=True)
    p.connection.read_registers.return_value = [0, 230, 0]    # voltage present → awake
    out = p._poll_registers()
    assert set(out) == {0, 1, 2} and out[1]["value"] == 230


def test_off_by_default_keeps_zeros():
    p = _poller(_regs(3), drop_all_zero=False)
    p.connection.read_registers.return_value = [0, 0, 0]
    out = p._poll_registers()
    assert set(out) == {0, 1, 2}                              # published as-is


def test_single_register_zero_is_not_dropped():
    # a lone legitimate 0 (a single-register group) must not be withheld
    p = _poller(_regs(1), drop_all_zero=True)
    p.connection.read_registers.return_value = [0]
    assert set(p._poll_registers()) == {0}


def test_config_round_trip(tmp_path):
    import yaml
    from multibus.config import Config
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"modbus": {"host": "1.2.3.4", "drop_all_zero": True}}))
    c = Config(str(cfg_path))
    assert c.modbus.drop_all_zero is True
    assert c.to_dict()["modbus"]["drop_all_zero"] is True
