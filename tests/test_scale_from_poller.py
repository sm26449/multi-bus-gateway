# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""SunSpec dynamic scale factors on the Modbus poll path (F0.2).

SunSpec integer models publish measurements as int16 raw + a sibling *_SF
register holding a signed base-10 exponent: engineering = raw × 10^SF.
The SF conventionally sits at a HIGHER address than its dependents, so the
poller prescans each batch for referenced SF registers before decoding —
otherwise every dependent would scale with last cycle's exponent (or be
missing on the first cycle entirely).
"""
from unittest.mock import MagicMock

from multibus.config import SelectedRegister
from multibus.modbus_client import RegisterPoller
from multibus.register_parser import RegisterParser


def _reg(**kw):
    base = dict(name="t", label="t", unit="", data_type="uint16",
                poll_group="normal")
    base.update(kw)
    return SelectedRegister(**base)


def _poller(regs, frames):
    """frames: list of raw-word lists returned by successive batch reads."""
    conn = MagicMock()
    conn.read_registers.side_effect = frames
    return RegisterPoller("normal", 5, regs, conn, RegisterParser(),
                          publish_callback=lambda *a: None)


def test_sf_at_higher_address_scales_same_batch_dependents():
    # W @10, VA @11, W_SF @12 = -1 → one contiguous batch [1000, 2000, 0xFFFF]
    regs = [_reg(address=10, name="w", scale_from="w_sf"),
            _reg(address=11, name="va", scale_from="w_sf"),
            _reg(address=12, name="w_sf", data_type="int16")]
    p = _poller(regs, [[1000, 2000, 0xFFFF]])       # int16 0xFFFF = -1
    out = p._poll_registers()
    assert out[10]["value"] == 100.0
    assert out[11]["value"] == 200.0
    assert out[12]["value"] == -1                    # the SF itself publishes raw


def test_sf_last_good_bridges_a_sentinel_cycle():
    # cycle 1 seeds SF=-1; cycle 2 the SF word reads as int16 sentinel-ish
    # garbage (|SF|>10) → dependents keep scaling with the last-good exponent
    regs = [_reg(address=10, name="w", scale_from="w_sf"),
            _reg(address=11, name="w_sf", data_type="int16")]
    p = _poller(regs, [[1000, 0xFFFF], [1500, 500]])   # 500 → |SF| > 10
    assert p._poll_registers()[10]["value"] == 100.0
    out2 = p._poll_registers()
    assert out2[10]["value"] == 150.0                  # bridged by last-good


def test_no_sf_ever_means_value_missing_not_unscaled():
    regs = [_reg(address=10, name="w", scale_from="w_sf"),
            _reg(address=11, name="w_sf", data_type="int16")]
    p = _poller(regs, [[1000, 500]])                   # never a valid SF
    out = p._poll_registers()
    assert 10 not in out                               # missing, NOT raw 1000


def test_registers_without_scale_from_skip_the_prescan_entirely():
    regs = [_reg(address=10, name="v", scale=10.0)]
    p = _poller(regs, [[2305]])
    assert p._sf_refs == set()
    assert p._poll_registers()[10]["value"] == 230.5
