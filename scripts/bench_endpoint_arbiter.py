#!/usr/bin/env python3
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Why the gateway queues instead of racing on a shared endpoint.

Stands up a slow, serializing Modbus TCP slave — the shape of a Fronius
DataManager or an RS-485-over-TCP bridge — and runs five pollers against it
with and without the endpoint arbiter. No hardware, no network: the point is
to show what the CODE does, on a device model taken from a real measurement.

The model comes from a production DataManager serving five units: a
50-register read costs ~0.4 s when the gateway is its only caller, and the
device loses roughly another 0.3 s for every extra caller it is juggling.

    docker run --rm --entrypoint python -v "$PWD":/app:ro -w /app \
      multi-bus-gateway:test scripts/bench_endpoint_arbiter.py

Measured 2026-09-12 (five units, 3.0 s interval, 30 s run):

    ARBITER OFF: 25 reads in 30s (0.83/s) | cadence p50 8.00s | sweep 7.70s
    ARBITER ON : 50 reads in 30s (1.67/s) | cadence p50 3.00s | sweep 1.20s

Twice the data, and the configured interval is the cadence you actually get.
"""
import socket
import statistics as st
import struct
import sys
import threading
import time
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))

SERVICE_S = 0.40          # what one 50-register read costs when nobody else asks
THRASH_S = 0.30           # what EACH extra in-flight caller costs the device

_dev_lock = threading.Lock()
_sockets = 0        # how many clients the master had to serve
_inflight = 0
_inflight_lock = threading.Lock()


def _serve(conn):
    global _inflight, _sockets
    with _inflight_lock:
        _sockets += 1
    try:
        while True:
            hdr = conn.recv(12)
            if len(hdr) < 12:
                return
            tid, _pid, _ln, unit, fc, addr, count = struct.unpack('>HHHBBHH', hdr)
            with _inflight_lock:
                _inflight += 1
                others = _inflight - 1
            with _dev_lock:                       # the device does one at a time
                time.sleep(SERVICE_S + THRASH_S * others)
            with _inflight_lock:
                _inflight -= 1
            payload = struct.pack('>%dH' % count, *([0] * count))
            body = struct.pack('>BBB', unit, fc, len(payload)) + payload
            conn.sendall(struct.pack('>HHH', tid, 0, len(body)) + body)
    except Exception:
        return
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def start_slave():
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 15502)); srv.listen(16)
    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=_serve, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return srv


def run(serialize, seconds=30, interval=3.0, share=None):
    from multibus.config import ModbusConfig, SelectedRegister
    from multibus.modbus_client import (ModbusConnection, RegisterPoller,
                                        _ARBITERS, _TRANSPORTS)
    from multibus.register_parser import RegisterParser
    global _sockets
    _ARBITERS.clear()
    _TRANSPORTS.clear()
    _sockets = 0
    share = serialize if share is None else share
    regs = [SelectedRegister(address=40071 + i, name=f'r{i}', label='r', unit='',
                             data_type='uint16', poll_group='normal') for i in range(50)]
    stamps = {}
    pollers = []
    for u in (1, 2, 3, 4, 240):
        cfg = ModbusConfig(host='127.0.0.1', port=15502, unit_id=u, timeout=10,
                           retry_attempts=1, retry_delay=0,
                           serialize_endpoint=serialize, endpoint_wait_s=30,
                           share_transport=share)
        conn = ModbusConnection(cfg)
        stamps[u] = []
        def cb(group, data, _u=u): stamps[_u].append(time.monotonic())
        p = RegisterPoller('normal', interval, regs, conn, RegisterParser('big'), cb, f'u{u}')
        pollers.append((p, conn))
    for p, _ in pollers:
        p.start()
    time.sleep(seconds)
    for p, c in pollers:
        p.stop()
        c.disconnect()
    for p, _ in pollers:
        p.join(timeout=5)

    gaps, cycles = [], []
    for u, ts in stamps.items():
        gaps += [b - a for a, b in zip(ts, ts[1:])]
    for p, _ in pollers:
        if p.last_cycle_s is not None:
            cycles.append(p.last_cycle_s)
    total = sum(len(v) for v in stamps.values())
    label = 'ARBITER ON ' if serialize else 'ARBITER OFF'
    if gaps:
        print(f'{label}: {total} reads in {seconds}s ({total/seconds:.2f}/s) | '
              f'cadence p50 {st.median(gaps):.2f}s max {max(gaps):.2f}s | '
              f'sweep {st.median(cycles):.2f}s | sockets the master served: {_sockets}')
    else:
        print(f'{label}: {total} reads — nothing completed')
    return total


srv = start_slave()
time.sleep(0.3)
print(f'fake slave: {SERVICE_S}s per read, +{THRASH_S}s per extra caller in flight')
print('5 units, interval 3.0s, 30s run\n')
run(False, share=False)          # a socket per unit, all racing — the old shape
time.sleep(3)                    # let the previous run's sockets finish closing
run(True)                        # one socket, one queue
