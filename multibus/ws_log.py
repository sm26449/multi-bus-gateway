# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""One log line per client per minute for refused WebSocket handshakes (WS-LOG-1).

A refused ``/ws`` handshake (the endpoint closes before accepting) makes
uvicorn log two INFO lines on ``uvicorn.error``::

    192.168.1.140:0 - "WebSocket /ws" 403        (uvicorn's websocket protocol)
    connection rejected (403 Forbidden)        (the websockets library, same logger)

A browser tab that keeps re-dialling every 3 s wrote 1,295 of them an hour
and buried the lines worth reading. This filter keeps the first refusal per
client host and drops the repeats for ``window_s``; the next refusal after
the window carries the count of what was dropped. The ``connection rejected``
line repeats the one before it without naming the client, so it is dropped
always. Every other record passes untouched, the accepted handshake among them.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List

_WS_PREFIX = '%s - "WebSocket %s" '
_REJECTED = "connection rejected (%d %s)"
LOGGERS = ("uvicorn.error",)          # both lines are logged here (uvicorn 0.50)


def _refusal_client(record: logging.LogRecord):
    """The client host of a refused-handshake record, else None."""
    msg, args = record.msg, record.args
    if not (isinstance(msg, str) and msg.startswith(_WS_PREFIX)
            and isinstance(args, tuple) and len(args) >= 2):
        return None
    tail = msg[len(_WS_PREFIX):]
    if tail == "403":
        pass
    elif tail == "%d" and len(args) >= 3 and isinstance(args[2], int) and args[2] >= 400:
        pass                                       # a denial response the app sent
    else:
        return None                                # [accepted], or not a refusal
    addr = str(args[0])
    host, sep, port = addr.rpartition(":")
    return host if sep and port.isdigit() else addr


class WsRejectFilter(logging.Filter):
    """Rate-limit refused-WebSocket records to one per client per window."""

    def __init__(self, window_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic,
                 max_clients: int = 4096):
        super().__init__()
        self.window_s = window_s
        self._clock = clock
        self._max = max_clients
        self._seen: Dict[str, List[float]] = {}  # host -> [window start, suppressed]
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg == _REJECTED:
            return False
        host = _refusal_client(record)
        if host is None:
            return True
        now = self._clock()
        with self._lock:
            seen = self._seen.get(host)
            if seen is not None and now - seen[0] < self.window_s:
                seen[1] += 1
                return False
            if seen is not None and seen[1]:
                record.msg = f"{record.msg} (%d more rejections from %s in the last %d s)"
                record.args = tuple(record.args) + (int(seen[1]), host, round(now - seen[0]))
            if len(self._seen) >= self._max:
                self._prune(now)
            self._seen[host] = [now, 0]
        return True

    def _prune(self, now: float) -> None:
        for h in [h for h, s in self._seen.items() if now - s[0] >= self.window_s]:
            del self._seen[h]
        while len(self._seen) >= self._max:      # all live: forget the oldest
            del self._seen[min(self._seen, key=lambda h: self._seen[h][0])]


def install(window_s: float = 60.0) -> None:
    """Attach the filter to uvicorn's loggers once (idempotent)."""
    for name in LOGGERS:
        lg = logging.getLogger(name)
        if not any(isinstance(f, WsRejectFilter) for f in lg.filters):
            lg.addFilter(WsRejectFilter(window_s))
