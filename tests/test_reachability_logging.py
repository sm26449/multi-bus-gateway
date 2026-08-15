# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Reachability is EDGE-triggered: one 'unreachable' event when a device drops
and one 'recovered' when it comes back — not one per failed batch × poll group.
Keeps the log/event ring readable through a flaky-link outage.
"""
from multibus.modbus_client import ModbusConnection, ModbusConfig


class _Result:
    def __init__(self, regs):
        self.registers = regs or []
        self._err = regs is None

    def isError(self):
        return self._err


class _Client:
    ok = False

    def is_socket_open(self): return True
    def connect(self): return True
    def close(self): pass

    def read_holding_registers(self, address, count, device_id):
        return _Result([0] * count) if _Client.ok else _Result(None)

    read_input_registers = read_holding_registers


def _conn():
    c = ModbusConnection(ModbusConfig())
    c.config.retry_attempts = 1          # one attempt → no backoff sleep
    _Client.ok = False
    c.client = _Client()
    c.connected = True
    # the wedge backstop force-closes and rebuilds the client mid-test —
    # keep it on the fake instead of a real pymodbus client
    c._new_client = lambda: _Client()
    return c


def _kinds(conn):
    return [e["kind"] for e in conn.events]


def test_reachability_is_edge_triggered():
    """Reachability is a LINK verdict (audit DP-8): it declares 'unreachable'
    only when the consecutive-failure run trips the wedge backstop — a single
    chronically-failing batch among healthy ones used to flap
    unreachable/recovered every cycle and rotate the event ring in ~25s."""
    conn = _conn()

    # isolated failures → batch_failures counted, but the LINK is not declared
    # down yet (no unreachable spam from one bad batch)
    for i in range(conn._reopen_after_fails - 1):
        assert conn.read_registers(0, 2) is None
    assert conn._reachable is True
    assert _kinds(conn).count("unreachable") == 0
    assert conn.batch_failures == conn._reopen_after_fails - 1

    # the run reaches the backstop → exactly one 'unreachable', state flips
    assert conn.read_registers(0, 2) is None
    assert conn._reachable is False
    assert _kinds(conn).count("unreachable") == 1

    # still down (another full run) → NO new 'unreachable' (quiet outage)
    for _ in range(conn._reopen_after_fails):
        assert conn.read_registers(0, 2) is None
    assert _kinds(conn).count("unreachable") == 1

    # recovery → exactly one 'recovered' event, state flips back
    _Client.ok = True
    assert conn.read_registers(0, 2) == [0, 0]
    assert conn._reachable is True
    assert _kinds(conn).count("recovered") == 1

    # stays reachable → no repeat 'recovered'
    assert conn.read_registers(0, 2) == [0, 0]
    assert _kinds(conn).count("recovered") == 1


def test_starts_reachable_and_a_clean_run_logs_nothing():
    conn = _conn()
    _Client.ok = True
    assert conn._reachable is True
    for _ in range(5):
        assert conn.read_registers(0, 2) == [0, 0]
    # no transitions at all → no unreachable/recovered noise
    assert not any(k in ("unreachable", "recovered") for k in _kinds(conn))
