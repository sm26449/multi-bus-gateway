# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""P2/P3 close-out: signed-magnitude decode + wedged-link forced reopen."""
from unittest.mock import MagicMock

from multibus.register_parser import RegisterParser


# ── signed-magnitude decode ──────────────────────────────────────────────────

def test_sm16_sign_and_magnitude():
    p = RegisterParser()
    assert p.parse_value([0x0064], "sm16") == 100
    assert p.parse_value([0x8064], "sm16") == -100      # bit 15 = sign
    assert p.parse_value([0x0000], "sm16") == 0
    assert p.parse_value([0x8000], "sm16") == 0         # -0 → 0
    assert p.get_register_count("sm16") == 1


def test_sm32_sign_and_magnitude():
    p = RegisterParser()
    assert p.parse_value([0x0000, 0x0064], "sm32") == 100
    assert p.parse_value([0x8000, 0x0064], "sm32") == -100
    assert p.get_register_count("sm32") == 2


def test_sm_respects_byte_order():
    p = RegisterParser(byte_order="cdab")               # word-swap
    assert p.parse_value([0x0064, 0x8000], "sm32") == -100


# ── wedged-link forced reopen ────────────────────────────────────────────────

def test_wedged_but_open_link_forces_reopen(monkeypatch):
    from multibus.config import ModbusConfig
    from multibus.modbus_client import ModbusConnection
    monkeypatch.setattr("multibus.modbus_client.bus_trace.trace.commit", lambda *a: None)

    conn = ModbusConnection(ModbusConfig(host="x", retry_attempts=1, retry_delay=0))

    class _Wedged:
        def __init__(self): self.closed = 0
        def is_socket_open(self): return True            # port "open" but stuck
        def connect(self): return True
        def close(self): self.closed += 1
        def read_holding_registers(self, address, count, slave):
            r = MagicMock(); r.isError.return_value = True; r.registers = []
            return r

    wedged = _Wedged()
    conn.client = wedged
    conn.connected = True
    monkeypatch.setattr(conn, "_new_client", lambda: wedged)  # reconnect reuses it

    for _ in range(4):
        assert conn.read_registers(0, 2) is None
    assert conn.forced_reopens == 0                      # not yet
    assert conn.read_registers(0, 2) is None             # 5th consecutive failure
    assert conn.forced_reopens == 1 and wedged.closed >= 1
    assert conn.connected is False                        # forced closed → next read reopens


def test_success_resets_the_failure_streak(monkeypatch):
    from multibus.config import ModbusConfig
    from multibus.modbus_client import ModbusConnection
    monkeypatch.setattr("multibus.modbus_client.bus_trace.trace.commit", lambda *a: None)
    conn = ModbusConnection(ModbusConfig(host="x", retry_attempts=1, retry_delay=0))

    state = {"ok": False}

    class _Flaky:
        def is_socket_open(self): return True
        def connect(self): return True
        def close(self): pass
        def read_holding_registers(self, address, count, slave):
            r = MagicMock()
            if state["ok"]:
                r.isError.return_value = False; r.registers = [1, 2]
            else:
                r.isError.return_value = True; r.registers = []
            return r

    conn.client = _Flaky(); conn.connected = True
    monkeypatch.setattr(conn, "_new_client", lambda: conn.client)
    for _ in range(4):
        conn.read_registers(0, 2)
    state["ok"] = True
    assert conn.read_registers(0, 2) == [1, 2]           # a success clears the streak
    state["ok"] = False
    for _ in range(4):
        conn.read_registers(0, 2)
    assert conn.forced_reopens == 0                       # streak restarted, never hit 5
