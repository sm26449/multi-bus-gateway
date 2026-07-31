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
"""Bounded, persisted cross-subsystem event log for the Status page.

A small ring buffer of notable events (Modbus read failures, MQTT/InfluxDB
connect/disconnect transitions, virtual-meter lifecycle). It is persisted to a
JSONL file so a *short* history survives a container restart, and bounded so it
never grows without limit. Thread-safe: a background harvester appends to it
while the API reads it.
"""

import json
import logging
import os
import threading
import time
from collections import deque
from typing import List, Optional

logger = logging.getLogger(__name__)


class EventLog:
    def __init__(self, path: str = "config/events.jsonl", maxlen: int = 300):
        self.path = path
        self.maxlen = maxlen
        self._events: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._adds = 0
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            self._events.append(json.loads(line))
                        except ValueError:
                            continue
                logger.info("event log: loaded %d events from %s", len(self._events), self.path)
        except OSError as e:
            logger.warning("event log: could not load %s (%s)", self.path, e)

    def add(self, level: str, source: str, message: str,
            kind: str = "", ts: Optional[float] = None) -> dict:
        ev = {"ts": round(ts or time.time(), 1), "level": level,
              "source": source, "message": message, "kind": kind}
        with self._lock:
            self._events.append(ev)
            self._adds += 1
            try:
                # 0600: the event log can carry operational detail (and, from a
                # careless caller, a secret) — it must not sit world-readable.
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            except OSError:
                pass
            # Rewrite the file to the bounded window periodically so it can't grow
            # unbounded (append-only would keep every event ever logged on disk).
            if self._adds % 50 == 0:
                self._compact_locked()
        return ev

    def _compact_locked(self) -> None:
        try:
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for ev in self._events:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            logger.debug("event log: compact failed (%s)", e)

    def recent(self, n: int = 100) -> List[dict]:
        with self._lock:
            items = list(self._events)
        items.sort(key=lambda e: e.get("ts", 0), reverse=True)
        return items[:n]
