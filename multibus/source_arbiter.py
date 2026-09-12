# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Which source owns a field right now.

A unit can be reached several ways at once — a Fronius inverter answers Modbus
TCP (complete, slow) and a Solar API over HTTP (partial, fast) — and both carry
``power_active_total``. Two sources writing one field would publish two values to
one topic, alternating, with no way to tell which is which.

The rule is the declaration order: **the first source that offers a field owns
it**. A lower-ranked source fills that field only once the owner has gone silent
past the owner's ``stale_after_s``. Deterministic, inspectable, and it makes
failover automatic when a cached HTTP view freezes — without the flapping that
"freshest wins" produces when two sources disagree by a hair.

``stale_after_s = 0`` means the owner NEVER yields. That is the correct setting
for an energy counter: two sources that disagree by a few Wh would make it walk
backwards every time ownership changed hands.

Every decision is recorded, because a two-source device is undebuggable
otherwise: "why does it say 1404 W" has to have an answer, and that answer is
``owner_of(field)``.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Tuple


class FieldArbiter:
    """Per-device ownership of canonical fields across its sources.

    Thread-safe: sources poll on their own threads and resolve against one map.
    """

    __slots__ = ("_owner", "_lock", "_handovers")

    def __init__(self) -> None:
        # field -> (rank, mono, source_id, stale_after_s)
        self._owner: Dict[str, Tuple[int, float, str, float]] = {}
        self._lock = threading.Lock()
        self._handovers = 0

    def claim(self, field: str, rank: int, source_id: str,
              stale_after_s: float, mono: Optional[float] = None) -> bool:
        """May `source_id` (at `rank`, 0 = highest) write `field` now?

        Returns True and records ownership, or False to drop the value — dropped
        means not stored, not published, not written to InfluxDB, so a suppressed
        source is genuinely silent rather than quietly racing.
        """
        now = mono if mono is not None else time.monotonic()
        with self._lock:
            cur = self._owner.get(field)
            if cur is None or cur[0] == rank:
                self._owner[field] = (rank, now, source_id, stale_after_s)
                return True
            cur_rank, cur_mono, cur_src, cur_stale = cur
            if rank < cur_rank:
                # A higher-ranked source reclaims the moment it speaks again.
                self._handovers += 1
                self._owner[field] = (rank, now, source_id, stale_after_s)
                return True
            # Lower-ranked: only when the owner has actually gone quiet, and only
            # if the owner declared a window at all (0 = never hand over).
            if cur_stale > 0 and (now - cur_mono) > cur_stale:
                self._handovers += 1
                self._owner[field] = (rank, now, source_id, stale_after_s)
                return True
            return False

    def owner_of(self, field: str) -> Optional[str]:
        """Which source last wrote this field, or None if nobody has."""
        with self._lock:
            cur = self._owner.get(field)
            return cur[2] if cur else None

    def snapshot(self) -> Dict[str, Dict]:
        """Provenance for every field: who owns it and how old that claim is.
        This is what the Logs tab and the API render."""
        now = time.monotonic()
        with self._lock:
            return {f: {"source": src, "rank": rank,
                        "age_s": round(now - mono, 2),
                        "stale_after_s": stale}
                    for f, (rank, mono, src, stale) in self._owner.items()}

    @property
    def handovers(self) -> int:
        """How many times a field changed hands. A number that climbs steadily
        means two sources are fighting and one of them should be narrowed."""
        return self._handovers

    def forget(self, source_id: str) -> None:
        """Drop a departing source's claims so a survivor can take over at once
        rather than waiting out a staleness window that will never elapse."""
        with self._lock:
            for f in [f for f, v in self._owner.items() if v[2] == source_id]:
                del self._owner[f]


def field_of(item: Dict) -> str:
    """The name a value competes under.

    Sources address the same measurement differently — Modbus reads
    `power_active_total` at 40083, the Solar API reads it from a JSON path at
    address 1 — so ownership is decided by the REGISTER NAME, never the address.
    Deciding by address would let both write the same field unopposed, which is
    the exact duplication this exists to prevent.
    """
    reg = item.get("register")
    return getattr(reg, "name", "") or ""
