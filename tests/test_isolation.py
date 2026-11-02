# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The suite's own isolation (FLK-9): an app a test builds and drops leaves
no thread behind once the test ends."""
import threading
import time

from tests.conftest import stop_app_threads
from tests.test_devices_api import make_app, needs_tc

_PER_APP = ("event-harvester", "bridge-diag")


def _alive(apps_threads):
    return [t for t in apps_threads if t.is_alive()]


@needs_tc
def test_the_threads_of_a_dropped_app_end_with_the_test(tmp_path):
    before = set(threading.enumerate())
    for n in range(3):
        d = tmp_path / str(n)
        d.mkdir()
        make_app(d)                                   # built and dropped, no lifespan
    mine = [t for t in threading.enumerate() if t not in before and t.name in _PER_APP]
    assert len(mine) == 3 * len(_PER_APP), [t.name for t in mine]
    stop_app_threads()
    end = time.monotonic() + 15
    while _alive(mine) and time.monotonic() < end:
        time.sleep(0.05)
    assert not _alive(mine), [t.name for t in _alive(mine)]
