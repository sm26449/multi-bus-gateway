# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""One endpoint, one queue.

A cheap gateway — a Fronius DataManager, an RS-485-over-TCP bridge —
serializes Modbus internally and serves a handful of clients. Measured on a
production DataManager with five units behind it: a 50-register read costs
~0.4 s when the gateway is the only caller and ~3 s when five of its own
pollers race, with the aggregate rate falling below one read per second and
fresh connections starting to time out. Concurrency there buys nothing and
costs everything, so the gateway queues instead of racing.
"""
import threading
import time

import pytest

from multibus.modbus_client import (ModbusConfig, ModbusConnection,
                                    _EndpointArbiter, endpoint_arbiter)


# ── the turnstile ────────────────────────────────────────────────────────────

def test_only_one_caller_holds_the_endpoint():
    arb = _EndpointArbiter()
    inside, overlap = [], []
    lock = threading.Lock()

    def worker():
        with arb.turn(5):
            with lock:
                inside.append(1)
                if len(inside) > 1:
                    overlap.append(1)
            time.sleep(0.02)
            with lock:
                inside.pop()

    ths = [threading.Thread(target=worker) for _ in range(5)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert overlap == []
    assert arb.turns == 5


def test_turns_are_handed_out_in_order():
    """FIFO on purpose: a plain Lock hands the turn to whoever the OS wakes,
    and under steady contention one poller can wait a very long time."""
    arb = _EndpointArbiter()
    order, ready = [], []
    arb.acquire(1)                       # hold it so everyone queues behind

    def worker(i):
        ready.append(i)
        with arb.turn(5):
            order.append(i)

    ths = []
    for i in range(4):
        t = threading.Thread(target=worker, args=(i,))
        ths.append(t)
        t.start()
        while i not in ready:             # deterministic enqueue order
            time.sleep(0.005)
        time.sleep(0.03)
    arb.release()
    [t.join() for t in ths]
    assert order == [0, 1, 2, 3]


def test_a_turn_that_never_comes_times_out_and_leaves_no_trace():
    arb = _EndpointArbiter()
    arb.acquire(1)
    with pytest.raises(TimeoutError):
        arb.acquire(0.05)
    assert arb.timeouts == 1
    arb.release()
    # the abandoned waiter must not block the queue behind it
    with arb.turn(1):
        pass
    assert arb.turns == 2


def test_one_arbiter_per_endpoint():
    a = endpoint_arbiter('10.0.0.5', 502)
    assert endpoint_arbiter('10.0.0.5', 502) is a
    assert endpoint_arbiter('10.0.0.5', 503) is not a
    assert endpoint_arbiter('10.0.0.6', 502) is not a


# ── the connection gate ──────────────────────────────────────────────────────

def _conn(**kw):
    cfg = ModbusConfig(host='192.0.2.77', port=502, retry_attempts=1,
                       retry_delay=0, **kw)
    return ModbusConnection(cfg)


def test_devices_sharing_an_endpoint_share_the_queue():
    a, b = _conn(), _conn()
    assert a._arbiter is not None
    assert a._arbiter is b._arbiter          # the plant's units queue together
    other = ModbusConnection(ModbusConfig(host='192.0.2.78', port=502))
    assert other._arbiter is not a._arbiter   # a different gateway, its own queue


def test_serialization_can_be_turned_off():
    assert _conn(serialize_endpoint=False)._arbiter is None


def test_a_missed_turn_skips_the_cycle_without_blaming_the_device():
    """A busy gateway must never masquerade as a dead one: no failed read, no
    error count, no step toward declaring the link unreachable."""
    c = _conn(endpoint_wait_s=0.05)
    c.client = object()                       # never touched — we never get in
    c._arbiter.acquire(1)                     # somebody else holds the endpoint
    try:
        assert c.read_registers(40071, 50) is None
    finally:
        c._arbiter.release()
    assert c.failed_reads == 0
    assert c._consecutive_fail == 0
    assert c._reachable is True
    assert [e['kind'] for e in c.events] == ['bus_busy']


def test_congestion_is_announced_once_per_episode():
    c = _conn(endpoint_wait_s=0.02)
    c._arbiter.acquire(1)
    try:
        for _ in range(4):
            assert c.read_registers(40071, 2) is None
    finally:
        c._arbiter.release()
    assert [e['kind'] for e in c.events].count('bus_busy') == 1
    # recovery re-arms it for the next episode
    c._reachable = False
    c._note_reachable()
    assert c._bus_busy_logged is False
