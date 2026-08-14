# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""M1 (audit 2026-08-14): the lost-stop race. A stop() landing between
Thread.start() and the first line of run() used to be overwritten by
`running = True` — and with the stop event ALREADY SET, every
wait(interval) returned instantly: an unkillable zombie poller issuing
back-to-back reads through the old callback. The stop event is now the
single source of truth for both poller classes."""
import threading
import time

from multibus.modbus_client import RegisterPoller
from multibus.register_parser import RegisterParser


def _mk_poller(publish):
    return RegisterPoller('g', 0.05, [], None, RegisterParser('big'), publish)


def test_stop_before_run_never_polls():
    """stop() before run() (the race, deterministically): run() must return
    immediately — zero poll iterations, no publish, running stays False."""
    polls = []
    p = _mk_poller(lambda *a: polls.append(a))
    p._poll_registers = lambda: {"x": 1}      # would publish if the loop ran
    p.stop()
    t0 = time.monotonic()
    p.run()                                    # direct call — deterministic
    assert time.monotonic() - t0 < 0.5         # returned at once, no hot loop
    assert polls == []
    assert p.running is False


def test_stop_between_start_and_run_kills_thread():
    """The real-thread flavor: hold the poller in a gate so stop() lands
    before its loop begins; the thread must exit promptly on its own."""
    gate = threading.Event()
    p = _mk_poller(lambda *a: None)
    real_run = p.run

    def gated_run():
        gate.wait(2)
        real_run()
    p.run = gated_run
    p.start()
    p.stop()                                   # lands before run() proper
    gate.set()
    p.join(timeout=2)
    assert not p.is_alive()
    assert p.running is False


def test_stop_interrupts_interval_wait():
    """A running poller with a long interval must exit within ~ms of stop(),
    not after the interval (the stop event interrupts the wait)."""
    p = RegisterPoller('g', 60, [], None, RegisterParser('big'), lambda *a: None)
    p._poll_registers = lambda: {}
    p.start()
    time.sleep(0.1)                            # let it enter the interval wait
    t0 = time.monotonic()
    p.stop()
    p.join(timeout=2)
    assert not p.is_alive()
    assert time.monotonic() - t0 < 1.0


def test_http_poller_stop_before_run_never_polls():
    from multibus.http_client import _JsonPoller

    class _Owner:
        def _note_success(self, n): pass
        def _note_failure(self): pass

    fetches = []
    p = _JsonPoller('g', 0.05, [], lambda: fetches.append(1) or {}, None, _Owner())
    p.stop()
    t0 = time.monotonic()
    p.run()
    assert time.monotonic() - t0 < 0.5
    assert fetches == []
    assert p.running is False


def test_http_poller_stop_interrupts_long_interval():
    from multibus.http_client import _JsonPoller

    class _Owner:
        def _note_success(self, n): pass
        def _note_failure(self): pass

    p = _JsonPoller('g', 60, [], lambda: {}, None, _Owner())
    p.start()
    time.sleep(0.1)                            # first fetch done → in the wait
    t0 = time.monotonic()
    p.stop()
    p.join(timeout=2)
    assert not p.is_alive()
    assert time.monotonic() - t0 < 1.0         # was up to a full 60 s (L2)
