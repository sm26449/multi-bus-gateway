"""Command lanes and bus locks (ADR-0018, ADR-0022).

A unit's commands and raw writes travel on one *lane* — the wire behind the
device — and take one *bus lock* on that wire, so a command's guarded
read → write → verify is never interleaved with another on a shared
datalogger, while a drive on a different bus no longer waits for them.

Extracted verbatim from ``create_api`` (P0.8b Lot C). The two Modbus/MQTT
driver resolvers are injected because they are ``create_api`` helpers; this
module imports nothing from ``multibus.api`` (layer rule: ``control`` must
not import the api layer).
"""
from __future__ import annotations

import contextlib
import threading
import time
from typing import Any, Callable, Dict, Optional


class BusBusy(Exception):
    """The bus stayed busy past a shutdown revert's deadline."""


class CommandLanes:
    """Per-lane command locks (ADR-0018).

    One command at a time PER ENDPOINT (a host:port, a serial port): units
    behind one datalogger share it and must not interleave a command's
    guard read → write → verify; a drive on another bus no longer waits for
    them (a device that does not answer used to hold every command on the
    gateway for its retries).
    """

    def __init__(self, *, modbus_driver_of: Callable, mqtt_driver_of: Callable) -> None:
        _modbus_driver_of = modbus_driver_of
        _mqtt_driver_of = mqtt_driver_of
        _BusBusy = BusBusy
        _cmd_locks: Dict[str, Any] = {}
        _cmd_locks_guard = threading.Lock()

        def _lane_key(dev_cfg, client) -> str:
            """The wire a unit's commands travel on — the dispatcher's lane and
            the commands' bus lock (ADR-0018). STABLE: the bus of a Modbus
            connection, else the device, else the device's MQTT source; never an
            object id, which split a lane and leaked a lock whenever an edit
            replaced the client (CMD-Q-10)."""
            did = getattr(dev_cfg, 'id', '?')
            drv = _modbus_driver_of(client) if client is not None else None
            if drv is not None:
                bus = str(getattr(getattr(drv, 'connection', None), 'bus', '') or '')
                return f"bus:{bus}" if bus else f"dev:{did}"
            sid = 'default'
            for src, part in getattr(client, 'parts', []) or []:
                if str(getattr(src, 'protocol', '')).lower() == 'mqtt' and hasattr(part, 'wait_for'):
                    sid = str(getattr(src, 'id', '') or 'default')
                    break
            # the connection the source actually uses: a component reads (and is
            # written) through its parent's link — one wire, one lane (CR2-10)
            mq = _mqtt_driver_of(client) if client is not None else None
            link = str(getattr(mq, 'link_name', '') or '') if mq is not None else ''
            return f"mqtt:{link or did}/{sid}"

        def _cmd_lock_for(client, dev_cfg=None) -> Any:
            if dev_cfg is not None:
                key = _lane_key(dev_cfg, client)
            else:
                # a caller that does not name the device (kept for the raw write
                # routes' older callers): the connection's bus, else the object
                drv = _modbus_driver_of(client) if client is not None else None
                conn = getattr(drv, 'connection', None) if drv is not None else getattr(client, 'connection', None)
                if conn is None:
                    mq = _mqtt_driver_of(client) if client is not None else None
                    key = f"mqtt:{id(mq if mq is not None else client)}"
                else:
                    bus = str(getattr(conn, 'bus', '') or '')
                    key = f"bus:{bus}" if bus else str(id(conn))
            with _cmd_locks_guard:
                lk = _cmd_locks.get(key)
                if lk is None:
                    lk = _cmd_locks[key] = threading.Lock()
                return lk

        @contextlib.contextmanager
        def _bus_lock(lock, deadline: Optional[float] = None):
            """The bus lock; with a deadline (a shutdown revert, ADR-0022 D8) at
            most until then — past it, nothing is written and the lease stays on
            disk for boot recovery (RC-5)."""
            if deadline is None:
                lock.acquire()
            elif not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                raise _BusBusy("the bus stayed busy past the shutdown budget — nothing written, "
                               "left for boot recovery")
            try:
                yield
            finally:
                lock.release()

        self._cmd_locks = _cmd_locks
        self._cmd_locks_guard = _cmd_locks_guard
        self.lane_key = _lane_key
        self.cmd_lock_for = _cmd_lock_for
        self.bus_lock = _bus_lock
