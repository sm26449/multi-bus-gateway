# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A bus is as busy as its wire was held in the last minute — counted once per
lane, never once per slave waiting its turn (D21: "bus 1031 % busy"), and
over capacity is a state, never a percentage above 100."""
import threading
import time

import pytest

from multibus import modbus_client as mc
from multibus.modbus_client import BUSY_WINDOW_S, _WireMeter
from tests.test_devices_api import make_app, needs_tc


def test_a_meter_counts_held_time_over_the_window_only():
    m = _WireMeter(now=0.0)
    assert m.snapshot(now=3.0) is None                       # under 5 s: not measured yet
    for k in range(60):                                      # 0.2 s held every second
        m.add(0.2, now=float(k + 1))
    snap = m.snapshot(now=60.0)
    assert snap['window_s'] == 60.0
    assert snap['busy_s'] == pytest.approx(12.0)
    assert m.share(now=60.0) == pytest.approx(0.2)
    # a minute later with no traffic: everything has left the window
    assert m.snapshot(now=200.0)['busy_s'] == 0.0


def test_the_oldest_transaction_is_clipped_to_the_window():
    m = _WireMeter(now=0.0)
    m.add(10.0, now=65.0)                                    # held from 55 to 65
    snap = m.snapshot(now=120.0)                             # window 60–120
    assert snap['busy_s'] == pytest.approx(5.0)


def test_n_slaves_on_one_lane_count_the_wire_once(monkeypatch):
    # the measurement the card used to make: each slave's sweep took the
    # whole queue (~N × wire), so N slaves summed to ~N² × wire
    monkeypatch.setattr(mc, "_ARBITERS", {})
    arb = mc.endpoint_arbiter("192.0.2.60", 4196, 0)
    t0 = time.monotonic()
    arb.wire = _WireMeter(now=t0 - BUSY_WINDOW_S)
    n, wire_s = 10, 0.05
    for k in range(int(BUSY_WINDOW_S)):                      # each slave once a second
        for _ in range(n):
            arb.wire.add(wire_s, now=t0 - BUSY_WINDOW_S + k + 1)
    occ = mc.bus_occupancy("192.0.2.60", 4196)
    assert occ['share'] == pytest.approx(n * wire_s, rel=0.05)    # 0.5 — not 10 × 0.5
    assert occ['missed'] == 0


def test_lanes_and_buses_are_separate(monkeypatch):
    monkeypatch.setattr(mc, "_ARBITERS", {})
    t0 = time.monotonic()
    for host, port, lane, held in (("192.0.2.60", 4196, 0, 0.1), ("192.0.2.60", 4197, 0, 0.3),
                                   ("192.0.2.80", 502, 0, 0.2), ("192.0.2.80", 502, 1, 0.2)):
        a = mc.endpoint_arbiter(host, port, lane)
        a.wire = _WireMeter(now=t0 - BUSY_WINDOW_S)
        for k in range(int(BUSY_WINDOW_S)):
            a.wire.add(held, now=t0 - BUSY_WINDOW_S + k + 1)
    assert mc.bus_occupancy("192.0.2.60", 4196)['share'] == pytest.approx(0.1, rel=0.05)
    assert mc.bus_occupancy("192.0.2.60", 4197)['share'] == pytest.approx(0.3, rel=0.05)
    two = mc.bus_occupancy("192.0.2.80", 502)                # a gateway with two lanes
    assert two['lanes'] == 2 and two['share'] == pytest.approx(0.4, rel=0.05)
    assert mc.bus_occupancy("192.0.2.99", 502) is None


def test_lanes_that_overlap_never_add_up_past_the_whole(monkeypatch):
    monkeypatch.setattr(mc, "_ARBITERS", {})
    t0 = time.monotonic()
    for lane in range(3):
        a = mc.endpoint_arbiter("192.0.2.81", 502, lane)
        a.wire = _WireMeter(now=t0 - BUSY_WINDOW_S)
        for k in range(int(BUSY_WINDOW_S)):
            a.wire.add(0.6, now=t0 - BUSY_WINDOW_S + k + 1)
    assert mc.bus_occupancy("192.0.2.81", 502)['share'] == 1.0


def test_a_turn_given_up_is_counted_in_the_window(monkeypatch):
    monkeypatch.setattr(mc, "_ARBITERS", {})
    arb = mc.endpoint_arbiter("192.0.2.61", 4196, 0)
    arb.acquire(1.0)                                         # someone holds the bus
    with pytest.raises(TimeoutError):
        arb.acquire(0.01)                                    # the next one gives up
    arb.release(0.5)
    arb.wire._born -= BUSY_WINDOW_S                          # pretend a minute has been seen
    assert mc.bus_occupancy("192.0.2.61", 4196)['missed'] == 1


def test_the_gate_measures_wire_time_not_queue_wait():
    arb = mc._EndpointArbiter()
    conn = type("C", (), {})()
    conn._arbiter = arb
    conn._wire = _WireMeter()
    conn.config = type("Cfg", (), {"endpoint_wait_s": 5.0})()
    conn._note_bus_busy = lambda: None
    gate = mc.ModbusConnection._endpoint_gate
    arb.acquire(5.0)                                         # another slave holds the bus 0.3 s
    threading.Timer(0.3, arb.release, args=(0.3,)).start()
    with gate(conn) as ok:
        assert ok
        time.sleep(0.05)                                     # our transaction
    held = conn._wire._held[-1][1]
    assert 0.04 <= held < 0.25                               # the 0.3 s in the queue is not ours


def test_the_hot_path_costs_microseconds():
    m = _WireMeter()
    n = 20000
    t = time.perf_counter()
    for _ in range(n):
        m.add(0.01)
    per = (time.perf_counter() - t) / n
    print(f"\n_WireMeter.add: {per * 1e6:.2f} µs per transaction")
    assert per < 50e-6


@needs_tc
def test_the_bridge_card_says_one_value_per_bus_and_never_above_100(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "_ARBITERS", {})
    cfg, client = make_app(tmp_path)
    assert client.post("/api/bridges", json={"id": "ws", "name": "Panel", "type": "rtu_transparent",
                                             "host": "192.0.2.62", "ports": [{"port": 4196}]}).status_code == 200
    for unit in range(1, 5):
        assert client.post("/api/devices", json={
            "id": f"m{unit}", "template": "fronius_smart_meter_65a", "enabled": False,
            "connection": {"bridge": "ws", "bridge_port": 4196, "unit_id": unit}}).status_code == 200
    port = lambda: client.get("/api/bridges/ws").json()["ports"][0]  # noqa: E731
    assert port()["busy"] is None                            # nothing measured yet
    t0 = time.monotonic()
    arb = mc.endpoint_arbiter("192.0.2.62", 4196, 0)
    arb.wire = _WireMeter(now=t0 - BUSY_WINDOW_S)
    for k in range(int(BUSY_WINDOW_S)):                      # 4 slaves × 0.0925 s every second
        for _ in range(4):
            arb.wire.add(0.0925, now=t0 - BUSY_WINDOW_S + k + 1)
    b = port()["busy"]
    # value check: 4 × 0.0925 s / 1 s = 37 % of the wire
    assert b["pct"] == 37 and b["level"] == "ok" and b["over_capacity"] is False
    assert b["devices"] == 4 and b["capacity_left_pct"] == 63 and b["window_s"] == 60.0
    # overload: every turn held for longer than a second's worth, plus a turn given up
    arb.wire = _WireMeter(now=t0 - BUSY_WINDOW_S)
    for k in range(int(BUSY_WINDOW_S)):
        for _ in range(4):
            arb.wire.add(0.4, now=t0 - BUSY_WINDOW_S + k + 1)
    arb.wire.miss(now=t0 - 1)
    b = port()["busy"]
    assert b["pct"] == 100 and b["level"] == "full" and b["over_capacity"] is True
    assert b["missed_turns"] == 1 and b["capacity_left_pct"] == 0
