# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""How many sockets an access point is worth is configuration, not doctrine.

One socket with an orderly queue is the right answer for a master that
serializes internally — a Fronius DataManager, measured at ~0.4 s per read
alone and ~3 s with five callers racing. It is the WRONG answer for a master
with an engine per line, where two or three connections cut the sweep
proportionally. So the gateway runs K lanes per access point, units stick to a
lane, transactions overlap ACROSS lanes and queue WITHIN one, and K comes from
a measurement (``scripts/calibrate_endpoint.py``), not from a default that
happens to suit one vendor.

What must not change with K: a unit always rides the same socket, its counters
and health stay its own, and two units on the SAME lane never overlap.
"""
import threading
import time

from multibus.modbus_client import (ModbusConfig, ModbusConnection, _TRANSPORTS,
                                    endpoint_arbiter, endpoint_bus_stats,
                                    transport_for)


class _Result:
    def __init__(self, regs):
        self.registers = regs or []

    def isError(self):
        return False


class _Client:
    delay = 0.0

    def __init__(self):
        self.asked = []

    def is_socket_open(self): return True
    def connect(self): return True
    def close(self): pass

    def read_holding_registers(self, address, count, device_id):
        self.asked.append(device_id)
        if _Client.delay:
            time.sleep(_Client.delay)
        return _Result([0] * count)

    read_input_registers = read_holding_registers


def _conn(unit, lanes=1, host='192.0.2.80', **kw):
    cfg = ModbusConfig(host=host, port=502, unit_id=unit, retry_attempts=1,
                       retry_delay=0, max_connections=lanes, **kw)
    c = ModbusConnection(cfg, trace_label=f'u{unit}')
    c._new_client = _Client
    return c


# ── the lanes ────────────────────────────────────────────────────────────────

def test_one_lane_is_the_default_and_everyone_shares_it():
    a, b, c = _conn(1), _conn(2), _conn(3)
    assert a.lane == b.lane == c.lane == 0
    assert a._tp is b._tp is c._tp


def test_units_spread_across_the_lanes_they_are_given():
    conns = [_conn(u, lanes=2) for u in (1, 2, 3, 4)]
    assert [c.lane for c in conns] == [1, 0, 1, 0]
    assert len({id(c._tp) for c in conns}) == 2          # two sockets, not four
    assert conns[0]._tp is conns[2]._tp                  # odd units together
    assert conns[1]._tp is conns[3]._tp


def test_a_unit_always_comes_back_to_the_same_lane():
    """Sticky by unit id: a reconnecting unit must not migrate onto a sibling's
    socket and drag its queue along."""
    first = _conn(7, lanes=3)
    first.disconnect()
    again = _conn(7, lanes=3)
    assert again.lane == first.lane


def test_more_lanes_than_units_costs_nothing_extra():
    conns = [_conn(u, lanes=4) for u in (1, 2)]
    assert len(_TRANSPORTS) == 2          # only the lanes actually used exist


def test_each_lane_keeps_its_own_queue():
    a = endpoint_arbiter('192.0.2.80', 502, 0)
    b = endpoint_arbiter('192.0.2.80', 502, 1)
    assert a is not b
    assert endpoint_arbiter('192.0.2.80', 502, 1) is b


def test_transactions_overlap_across_lanes_and_queue_within_one():
    """The whole point of a second connection, and the whole point of keeping
    the turnstile on each."""
    _Client.delay = 0.05
    try:
        conns = [_conn(u, lanes=2) for u in (1, 2, 3, 4)]
        conns[0].connect()
        conns[1].connect()
        inside, seen = {}, []
        guard = threading.Lock()
        real = _Client.read_holding_registers

        def watched(self, address, count, device_id):
            lane = next(c.lane for c in conns if c.config.unit_id == device_id)
            with guard:
                inside[lane] = inside.get(lane, 0) + 1
                seen.append((dict(inside), max(inside.values())))
            try:
                return real(self, address, count, device_id)
            finally:
                with guard:
                    inside[lane] -= 1

        _Client.read_holding_registers = watched
        try:
            ths = [threading.Thread(target=lambda c=c: [c.read_registers(40071, 2)
                                                        for _ in range(6)])
                   for c in conns]
            [t.start() for t in ths]
            [t.join() for t in ths]
        finally:
            _Client.read_holding_registers = real

        # never two units at once on ONE lane ...
        assert max(m for _, m in seen) == 1
        # ... but both lanes were busy together at least once
        assert any(len([v for v in snap.values() if v]) == 2 for snap, _ in seen)
    finally:
        _Client.delay = 0.0


def test_an_unshared_connection_ignores_the_lane_count():
    a = _conn(3, lanes=3, share_transport=False)
    assert a.lane == 0 and a._tp.key == ''


# ── what a turn costs, measured ──────────────────────────────────────────────

def test_the_endpoint_reports_what_a_transaction_actually_costs():
    """Every interval decision is a division by this number, so it is measured
    on the live wire rather than assumed from a datasheet."""
    _Client.delay = 0.02
    try:
        a = _conn(1)
        a.connect()
        for _ in range(5):
            a.read_registers(40071, 2)
        bus = endpoint_bus_stats('192.0.2.80', 502)
        assert bus['lanes'] == 1 and bus['samples'] == 5
        assert bus['tx_p50_s'] >= 0.02 and bus['tx_p95_s'] >= bus['tx_p50_s']
        assert bus['turns'] == 5 and bus['missed_turns'] == 0
    finally:
        _Client.delay = 0.0


def test_an_endpoint_nobody_has_polled_claims_no_numbers():
    bus = endpoint_bus_stats('192.0.2.99', 502)
    assert bus['lanes'] == 1                 # the honest floor, not zero
    assert bus['tx_p50_s'] is None and bus['samples'] == 0


def test_the_cost_is_summed_over_every_lane_of_the_endpoint():
    _Client.delay = 0.01
    try:
        conns = [_conn(u, lanes=2) for u in (1, 2)]
        for c in conns:
            c.connect()
            c.read_registers(40071, 2)
        bus = endpoint_bus_stats('192.0.2.80', 502)
        assert bus['lanes'] == 2 and bus['turns'] == 2
    finally:
        _Client.delay = 0.0


def test_a_lane_is_its_own_socket_key():
    tp0 = transport_for('10.2.2.2', 502, True, 0)
    tp1 = transport_for('10.2.2.2', 502, True, 1)
    assert tp0 is not tp1
    assert transport_for('10.2.2.2', 502, True, 1) is tp1
    assert tp1.users == 2


# ── the knob ─────────────────────────────────────────────────────────────────

def test_max_connections_defaults_to_one_and_never_goes_below():
    assert ModbusConfig().max_connections == 1
    assert _conn(5, lanes=0).lane == 0        # a nonsense 0 is not a crash


def test_an_endpoint_hands_its_lane_count_to_every_unit(tmp_path):
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml="""
endpoints:
  - id: roof
    connection: { protocol: tcp, host: 192.0.2.70, max_connections: 2 }
    units: [1, 2, 3]
""")
    units = cfg.endpoint_devices('roof')
    assert len(units) == 3
    assert all(d.connection.max_connections == 2 for d in units)


# ── the shape the endpoint page reads ────────────────────────────────────────

def test_the_budget_numbers_are_read_from_the_real_stats_shape():
    """`poll_groups_detail` is a LIST of groups, and the endpoint page adds up
    its reads, cycle times and overruns to say what the wire costs. A stub in a
    test cannot vouch for that shape — this asks the real object, because
    reading it as a dict made every endpoint detail 500 until the browser run
    found it."""
    from multibus.config import PollGroup, SelectedRegister
    from multibus.modbus_client import ModbusClient

    reg = SelectedRegister(address=40072, name='w', label='W', unit='W',
                           data_type='int16', register_type='holding',
                           poll_group='normal')
    client = ModbusClient(ModbusConfig(host='192.0.2.81', port=502, unit_id=1),
                          registers=[reg],
                          poll_groups={'normal': PollGroup(interval=5)})
    client.connection._new_client = _Client
    try:
        client.start_polling()
        time.sleep(0.3)
        detail = client.get_stats()['poll_groups_detail']
        assert isinstance(detail, list) and detail
        # exactly the keys the endpoint page sums over
        assert {'reads', 'cycle_s', 'overruns'} <= set(detail[0])
        assert sum(int(g.get('reads') or 0) for g in detail) >= 1
    finally:
        client.disconnect()
