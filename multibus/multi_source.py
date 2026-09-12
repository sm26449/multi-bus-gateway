# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""One unit, several ways of reaching it.

A master device offers the same slave over more than one protocol. A Fronius
DataManager answers Modbus TCP (complete: power factor, reactive power, event
flags, MPPT strings — measured 1945-2376 ms a read) and a Solar API over HTTP
(partial, but 54 ms and it does not disturb the Modbus side). Both are the SAME
inverter, so both must land on the same identity: one topic prefix, one bucket,
one device tag, one history.

``MultiSourceClient`` is that unit. It holds one real driver per source, feeds
them all into one value store, and resolves collisions through a
:class:`~multibus.source_arbiter.FieldArbiter` — first source to offer a field
owns it, a later one fills in only once the owner has gone quiet.

It deliberately presents the SAME surface as a single driver (``connect``,
``start_polling``, ``disconnect``, ``get_stats``, ``data_health``,
``write_value``, ``reload_registers``, ``connection``), so the registry, the API,
the alert harvester and the virtual meters keep working unchanged. A device with
one source is still wrapped, so there is one code path in production rather than
a rare second one that only runs on the unusual config.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from .source_arbiter import FieldArbiter, field_of

logger = logging.getLogger(__name__)


class MultiSourceClient:
    """A unit fed by an ordered list of sources."""

    def __init__(self, device_id: str, parts: List[Tuple[Any, Any]]):
        """`parts` is [(SourceConfig, driver), …] in DECLARATION ORDER, which is
        precedence order — index 0 wins ties."""
        self.device_id = device_id
        self.parts = parts
        self.arbiter = FieldArbiter()
        self._publish_callback: Optional[Callable] = None
        self._lock = threading.Lock()
        for rank, (src, drv) in enumerate(parts):
            drv.publish_callback = self._make_filter(rank, src)

    # ── the gate every value passes through ─────────────────────────────────

    def _make_filter(self, rank: int, src) -> Callable:
        """Wrap one source's callback so its values are checked for ownership
        before they reach the store, MQTT or InfluxDB.

        Suppression happens HERE, before any sink, so a losing source is
        genuinely silent — not writing a shadow value that a later reader
        stumbles on.
        """
        sid = src.id
        stale = float(getattr(src, 'stale_after_s', 0.0) or 0.0)

        def _filtered(poll_group: str, data: Dict[int, Dict]):
            cb = self._publish_callback
            if cb is None:
                return
            kept = {}
            for addr, item in data.items():
                field = field_of(item)
                if not field:
                    # No name to compete under (a raw/unnamed read): let it
                    # through rather than silently dropping data.
                    kept[addr] = item
                    continue
                if self.arbiter.claim(field, rank, sid, stale, item.get('mono')):
                    # Stamp provenance on the value itself: "why does it say
                    # 1404 W" must have an answer at the point of use.
                    item = dict(item, source=sid)
                    kept[addr] = item
            if kept:
                cb(poll_group, kept)

        return _filtered

    @property
    def publish_callback(self):
        return self._publish_callback

    @publish_callback.setter
    def publish_callback(self, cb):
        self._publish_callback = cb

    # ── the single-driver surface ───────────────────────────────────────────

    @property
    def sources(self) -> List:
        return [src for src, _drv in self.parts]

    @property
    def primary(self):
        """The first source's driver — what `connection`, writes and register
        reloads address, because rank 0 is the authoritative view of the unit."""
        return self.parts[0][1] if self.parts else None

    @property
    def connection(self):
        return getattr(self.primary, 'connection', None)

    def connect(self) -> bool:
        """Connected when ANY source is. One transport being down must not make
        the unit look absent while another is delivering."""
        ok = False
        for src, drv in self.parts:
            try:
                ok = bool(drv.connect()) or ok
            except Exception as e:  # noqa: BLE001 — one source must not stop another
                logger.warning("device %s source %s: connect failed — %s",
                               self.device_id, src.id, e)
        return ok

    def start_polling(self) -> None:
        for src, drv in self.parts:
            try:
                drv.start_polling()
            except Exception as e:  # noqa: BLE001
                logger.warning("device %s source %s: start failed — %s",
                               self.device_id, src.id, e)

    def disconnect(self) -> None:
        for src, drv in self.parts:
            try:
                drv.disconnect()
            except Exception as e:  # noqa: BLE001
                logger.debug("device %s source %s: disconnect — %s",
                             self.device_id, src.id, e)
            self.arbiter.forget(src.id)

    def reload_registers(self, *a, **kw):
        for _src, drv in self.parts:
            fn = getattr(drv, 'reload_registers', None)
            if callable(fn):
                try:
                    fn(*a, **kw)
                except Exception as e:  # noqa: BLE001
                    logger.warning("device %s: reload failed — %s", self.device_id, e)

    def write_value(self, *a, **kw):
        """Writes go to the first source that can perform them.

        A Solar API is read-only; the SunSpec side is where a setpoint lands. If
        no source can write, say so plainly rather than failing obscurely.
        """
        for src, drv in self.parts:
            fn = getattr(drv, 'write_value', None)
            if callable(fn):
                return fn(*a, **kw)
        return False, f"device {self.device_id}: no source supports writing", None

    # ── health and telemetry ────────────────────────────────────────────────

    def data_health(self, *a, **kw) -> Dict:
        """The unit is as healthy as its BEST source. A dead Modbus side while
        HTTP delivers is a degraded unit, not a down one — but `sources` below
        names which one fell over, so the loss is never silent."""
        best, rank = None, {"ok": 0, "degraded": 1, "down": 2}
        for _src, drv in self.parts:
            fn = getattr(drv, 'data_health', None)
            if not callable(fn):
                continue
            try:
                h = fn(*a, **kw)
            except Exception:  # noqa: BLE001
                continue
            if best is None or rank.get(h.get('status'), 3) < rank.get(best.get('status'), 3):
                best = h
        return best or {"status": "down", "stale": True, "staleness_age_s": None,
                        "last_success_ts": None, "connected": False}

    @property
    def connected(self) -> bool:
        return any(bool(getattr(drv, 'connected', False)) for _s, drv in self.parts)

    def get_stats(self) -> Dict:
        """One unit's statistics, with every source's contribution kept legible.

        Counters sum (the unit did that much work), the freshest success wins
        (the unit was last heard from then), and `sources` carries the per-source
        breakdown plus `provenance` — which source currently owns each field.
        Everything is plain JSON so a Node-RED http-request node can read it
        without unwrapping anything.
        """
        merged: Dict[str, Any] = {}
        per_source, groups, events = [], [], []
        for rank, (src, drv) in enumerate(self.parts):
            try:
                st = drv.get_stats() or {}
            except Exception:  # noqa: BLE001
                st = {}
            per_source.append({
                'id': src.id, 'rank': rank, 'protocol': src.protocol,
                'template': src.template,
                'stale_after_s': getattr(src, 'stale_after_s', 0.0),
                'connected': bool(st.get('connected')),
                'successful_reads': st.get('successful_reads'),
                'failed_reads': st.get('failed_reads'),
                'last_latency_ms': st.get('last_latency_ms'),
                'last_success_ts': st.get('last_success_ts'),
                'staleness_age_s': st.get('staleness_age_s'),
                'poll_rate': st.get('poll_rate'),
                'error_counts': st.get('error_counts') or {},
            })
            for g in (st.get('poll_groups_detail') or []):
                groups.append({**g, 'source': src.id})
            for e in (st.get('events') or []):
                events.append({**e, 'source': src.id})
            if not merged:
                merged = dict(st)
            else:
                for k in ('successful_reads', 'failed_reads', 'total_polls',
                          'total_registers', 'poll_rate'):
                    a, b = merged.get(k), st.get(k)
                    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                        merged[k] = round(a + b, 3)
                for k in ('last_success_ts', 'last_failure_ts'):
                    a, b = merged.get(k), st.get(k)
                    if b and (not a or b > a):
                        merged[k] = b
        merged['connected'] = self.connected
        merged['poll_groups_detail'] = groups
        events.sort(key=lambda e: e.get('ts') or 0)
        merged['events'] = events
        merged['sources'] = per_source
        merged['provenance'] = self.arbiter.snapshot()
        merged['field_handovers'] = self.arbiter.handovers
        return merged
