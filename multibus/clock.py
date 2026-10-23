# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The installation's local time.

Rules and calculated fields that speak of hours and days ("between 10:00 and
16:00", "run hours today") mean the time on the wall where the plant is, not
the container's UTC. One place holds that timezone (``ui.timezone``); the
clock itself can be replaced in tests.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, tzinfo
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_tz: Optional[tzinfo] = None
_now: Callable[[], float] = time.time


def set_timezone(name: str) -> bool:
    """Use the IANA zone ``name`` (``Europe/Bucharest``). An unknown name keeps
    the container's own local time and says so once."""
    global _tz
    try:
        from zoneinfo import ZoneInfo
        _tz = ZoneInfo(str(name)) if name else None
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("timezone %r unknown (%s) — using the container's local time", name, e)
        _tz = None
        return False


def set_now(fn: Optional[Callable[[], float]]) -> None:
    """Replace the wall clock (tests); None puts the real one back."""
    global _now
    _now = fn or time.time


def now() -> float:
    return _now()


def local(epoch: Optional[float] = None) -> datetime:
    """An epoch as the installation's local date and time."""
    t = _now() if epoch is None else float(epoch)
    return datetime.fromtimestamp(t, _tz) if _tz is not None else datetime.fromtimestamp(t).astimezone()


def day_key(epoch: Optional[float] = None) -> str:
    """The local calendar day, ``2026-10-19`` — what "today" resets on."""
    return local(epoch).date().isoformat()


def hours_of(text: str) -> float:
    """``"10:30"`` → 10.5. Raises ValueError on anything else."""
    h, m = str(text).strip().split(':')
    h, m = int(h), int(m)
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m):
        raise ValueError(f"not a time of day: {text!r}")
    return h + m / 60.0


def between(start: float, end: float, at: Optional[datetime] = None) -> bool:
    """True from ``start`` (inclusive) to ``end`` (exclusive), in hours of the
    local day; a window that ends before it starts runs past midnight
    (22 → 6). Equal ends: never."""
    d = at or local()
    h = d.hour + d.minute / 60.0 + d.second / 3600.0
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end
