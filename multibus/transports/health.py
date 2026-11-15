# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
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
"""Shared mechanics for a device client's ``data_health()`` verdict.

ADR-0033 step 4a (DUP-03 / ARCH-4). The eight clients each hand-roll the same
two things: the step-immune staleness age (``round(now - last_mono, 1)`` or
``None``), and a small result dict whose keys drifted apart over time
(``last_success_ts`` here, ``reason`` there, ``stale``+``staleness_age_s`` on
some and not others). This module owns the age idiom and the *key vocabulary*
in one place.

It deliberately does NOT unify the verdict *semantics*: a polled client's
ok/degraded/down ladder and a listen-only client's ok/stale/down ladder mean
different things and stay the responsibility of each client. ``health()`` only
assembles the dict — every field is optional and omitted unless supplied, so a
client keeps emitting exactly the keys it always did (pinned by
``tests/test_health_golden.py``).
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

_UNSET: Any = object()


def age_s(last_mono: Optional[float], now: Optional[float] = None) -> Optional[float]:
    """Seconds since a monotonic timestamp, rounded to 0.1 s, or ``None`` when
    nothing has been recorded yet. Monotonic so an NTP step can't make data look
    fresh or stale. ``now`` defaults to the current monotonic clock."""
    if last_mono is None:
        return None
    return round((time.monotonic() if now is None else now) - last_mono, 1)


def health(status: str, *,
           stale: Any = _UNSET,
           staleness_age_s: Any = _UNSET,
           last_success_ts: Any = _UNSET,
           connected: Any = _UNSET,
           reason: Any = _UNSET,
           **extra: Any) -> Dict[str, Any]:
    """Assemble a ``data_health()`` result. ``status`` is always present; every
    other field is emitted only when the client passes it, so the dict carries
    exactly the keys that client has always carried. ``extra`` is for a client's
    own fields (Modbus ``threshold_s``/``stale_groups``/``batch_failures``,
    MeterOnly ``sources``/``measured_by``, MQTT ``available``, …)."""
    out: Dict[str, Any] = {"status": status}
    if stale is not _UNSET:
        out["stale"] = stale
    if staleness_age_s is not _UNSET:
        out["staleness_age_s"] = staleness_age_s
    if last_success_ts is not _UNSET:
        out["last_success_ts"] = last_success_ts
    if connected is not _UNSET:
        out["connected"] = connected
    if reason is not _UNSET:
        out["reason"] = reason
    out.update(extra)
    return out
