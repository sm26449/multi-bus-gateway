# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Units behind one master share a wire, never an identity.

A master device — a Fronius DataManager, a Modbus TCP/RTU gateway, an RS-485
bridge — fronts several units on ONE access point. Giving each unit its own
socket never made them independent (they still queue inside the master) and a
master that serves a handful of clients simply runs out of them. So the socket
belongs to the access point.

Everything that describes a UNIT stays with the unit: its counters, its
latency, its error taxonomy, its reachability verdict, its health. These tests
exist to keep that line from blurring.
"""
import threading

from multibus.modbus_client import (ModbusConfig, ModbusConnection, _TRANSPORTS,
                                    transport_for)


class _Result:
    def __init__(self, regs):
        self.registers = regs or []
        self._err = regs is None

    def isError(self):
        return self._err


class _Client:
    """A fake pymodbus client that remembers which unit each read asked for."""
    ok = True

    def __init__(self):
        self.asked = []
        self.closed = 0

    def is_socket_open(self): return True
    def connect(self): return True
    def close(self): self.closed += 1

    def read_holding_registers(self, address, count, device_id):
        self.asked.append(device_id)
        return _Result([0] * count) if _Client.ok else _Result(None)

    read_input_registers = read_holding_registers


def _conn(unit, host='192.0.2.90', **kw):
    cfg = ModbusConfig(host=host, port=502, unit_id=unit, retry_attempts=1,
                       retry_delay=0, **kw)
    c = ModbusConnection(cfg, trace_label=f'u{unit}')
    c._new_client = _Client
    return c


def _fresh():
    _TRANSPORTS.clear()
    _Client.ok = True


# ── the wire is shared ───────────────────────────────────────────────────────

def test_units_behind_one_master_share_one_socket():
    _fresh()
    a, b, c = _conn(1), _conn(2), _conn(3)
    assert a._tp is b._tp is c._tp
    assert a.lock is b.lock                 # and the lock that serializes it
    a.connect()
    assert a.client is not None
    assert b.client is a.client             # nobody opens a second one
    assert c.connected is True


def test_a_different_master_gets_its_own_socket():
    _fresh()
    a, other = _conn(1), _conn(1, host='192.0.2.91')
    assert a._tp is not other._tp


def test_sharing_can_be_turned_off():
    _fresh()
    a, b = _conn(1, share_transport=False), _conn(2, share_transport=False)
    assert a._tp is not b._tp


def test_a_directly_attached_serial_line_never_shares():
    """Two devices on one RS-485 line are a different problem (one master per
    line), and a serial port has no host:port to key on."""
    _fresh()
    a = _conn(1, protocol='rtu')
    b = _conn(2, protocol='rtu')
    assert a._tp is not b._tp


def test_every_transaction_carries_its_own_unit_id():
    _fresh()
    a, b = _conn(7), _conn(9)
    a.connect()
    assert a.read_registers(40071, 4) == [0, 0, 0, 0]
    assert b.read_registers(40071, 4) == [0, 0, 0, 0]
    assert a.client.asked == [7, 9]          # one socket, two identities


def test_a_reconnect_by_one_unit_is_seen_by_its_siblings():
    _fresh()
    a, b = _conn(1), _conn(2)
    a.connect()
    first = a.client
    a.connect()                              # forced reopen, say
    assert a.client is not first
    assert b.client is a.client              # not left holding the dead one


# ── the identity is not ──────────────────────────────────────────────────────

def test_one_units_failures_are_not_charged_to_another():
    _fresh()
    a, b = _conn(1), _conn(2)
    a.connect()
    _Client.ok = False
    for _ in range(a._reopen_after_fails):
        assert a.read_registers(40071, 2) is None
    assert a.failed_reads == a._reopen_after_fails
    assert b.failed_reads == 0               # b never asked for anything
    assert a._reachable is False and b._reachable is True
    assert a.error_counts and b.error_counts == {}
    assert [e['kind'] for e in a.events].count('unreachable') == 1
    assert list(b.events) == []


def test_each_unit_keeps_its_own_success_clock():
    _fresh()
    a, b = _conn(1), _conn(2)
    a.connect()
    assert a.read_registers(40071, 2) is not None
    assert a.successful_reads == 1 and a.last_success_ts is not None
    assert b.successful_reads == 0 and b.last_success_ts is None


# ── letting go ───────────────────────────────────────────────────────────────

def test_one_unit_leaving_does_not_take_the_master_down():
    _fresh()
    a, b = _conn(1), _conn(2)
    a.connect()
    client = a.client
    a.disconnect()
    assert client.closed == 0                # b is still reading through it
    assert b.client is client
    b.disconnect()
    assert client.closed == 1                # the last one out closes the door


def test_a_private_transport_closes_on_its_own_disconnect():
    _fresh()
    a = _conn(1, share_transport=False)
    a.connect()
    client = a.client
    a.disconnect()
    assert client.closed == 1


def test_the_pool_does_not_leak_masters():
    _fresh()
    a, b = _conn(1), _conn(2)
    assert len(_TRANSPORTS) == 1
    a.disconnect()
    b.disconnect()
    assert _TRANSPORTS == {}


def test_transport_for_counts_its_users():
    _fresh()
    tp = transport_for('10.1.1.1', 502, True)
    assert transport_for('10.1.1.1', 502, True) is tp
    assert tp.users == 2
    # an unshared caller is handed a fresh one every time, and pools nothing
    assert transport_for('10.1.1.1', 502, False) is not tp
    assert len(_TRANSPORTS) == 1


def test_concurrent_units_never_overlap_on_the_wire():
    _fresh()
    conns = [_conn(u) for u in (1, 2, 3, 4)]
    conns[0].connect()
    inside, overlap = [], []
    guard = threading.Lock()
    real = _Client.read_holding_registers

    def watched(self, address, count, device_id):
        with guard:
            inside.append(device_id)
            if len(inside) > 1:
                overlap.append(tuple(inside))
        try:
            return real(self, address, count, device_id)
        finally:
            with guard:
                inside.pop()

    _Client.read_holding_registers = watched
    try:
        ths = [threading.Thread(target=lambda c=c: [c.read_registers(40071, 2)
                                                    for _ in range(20)])
               for c in conns]
        [t.start() for t in ths]
        [t.join() for t in ths]
    finally:
        _Client.read_holding_registers = real
    assert overlap == []
