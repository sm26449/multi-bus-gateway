# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A slow poll group can be swept on demand — a write must not leave MQTT
stale until the next scheduled sweep."""
import time

from multibus.config import ModbusConfig, SelectedRegister
from multibus.modbus_client import ModbusConnection, RegisterPoller
from multibus.register_parser import RegisterParser


def test_a_write_kicks_the_controls_sweep_instead_of_waiting_an_hour():
    """The controls block is read once an hour (every transaction on this
    datalogger is dear); a write must not leave MQTT stale for that long."""
    class _Conn(ModbusConnection):
        def read_registers(self, address, count, register_type='holding', stop_event=None):
            return [1] * count
    conn = _Conn(ModbusConfig(host="192.0.2.5")); conn.connected = True
    sel = [SelectedRegister(address=40231, name='controls_connected', label='c', unit='',
                            data_type='uint16', poll_group='controls', register_type='holding')]
    got = []
    p = RegisterPoller('controls', 3600, sel, conn, RegisterParser('big'),
                       lambda g, d: got.append(time.monotonic()), 'x')
    p.startup_jitter_s = 0
    p.start()
    deadline = time.monotonic() + 3
    while len(got) < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(got) == 1                       # the first sweep, then an hour of nothing
    t0 = time.monotonic()
    p.poll_now()
    while len(got) < 2 and time.monotonic() < t0 + 3:
        time.sleep(0.02)
    assert len(got) == 2 and got[1] - t0 < 2.0   # …unless kicked
    p.stop(); p.join(3)
    assert not p.is_alive()
