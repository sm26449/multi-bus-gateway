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
import time
from collections import deque
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
        # A source changing state is an EVENT, not just a number. The whole
        # danger of reading a unit two ways is that one way dies quietly while
        # the other keeps the lights green — so every transition is recorded,
        # lands in the device's log, and reaches the alert harvester.
        self.events: deque = deque(maxlen=200)
        self._seen: Dict[str, str] = {}
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

    def _source_health(self, *a, **kw) -> List[Tuple[Any, Dict]]:
        out = []
        for src, drv in self.parts:
            fn = getattr(drv, 'data_health', None)
            if not callable(fn):
                # A driver with no verdict of its own (a push input) is judged
                # by its socket rather than skipped: skipping would let a
                # broken source hide behind a healthy sibling, which is the one
                # failure this whole two-level scheme exists to prevent.
                out.append((src, {"status": "ok" if getattr(drv, 'connected', False)
                                  else "down"}))
                continue
            try:
                out.append((src, fn(*a, **kw) or {}))
            except Exception:  # noqa: BLE001
                out.append((src, {"status": "down"}))
        return out

    def data_health(self, *a, **kw) -> Dict:
        """The unit's verdict, and it does NOT hide a dead source.

        A unit read two ways is ``ok`` only when EVERY source is ok. If one has
        fallen over while another still delivers, the unit is ``degraded`` — not
        ``ok`` — because the loss is real: when the Modbus side of an inverter
        dies we still get power and frequency over HTTP, but we have silently
        stopped collecting power factor, reactive power, the event flags and the
        MPPT strings. A green light there would be a lie.

        Only when nothing is left is the unit ``down``. A single-source device
        behaves exactly as it always did: its one source's verdict IS the unit's.
        """
        parts = self._source_health(*a, **kw)
        if not parts:
            return {"status": "down", "stale": True, "staleness_age_s": None,
                    "last_success_ts": None, "connected": False, "sources": {}}
        order = {"ok": 0, "degraded": 1, "down": 2}
        best = min(parts, key=lambda p: order.get(p[1].get('status'), 3))[1]
        worst = max(order.get(h.get('status'), 3) for _s, h in parts)
        status = best.get('status', 'down')
        if status == 'ok' and worst > 0:
            status = 'degraded'
        out = dict(best)
        out['status'] = status
        out['sources'] = {src.id: h.get('status', 'down') for src, h in parts}
        out['sources_down'] = [src.id for src, h in parts
                               if h.get('status') == 'down']
        return out

    def note_source_transitions(self) -> None:
        """Record a source changing state.

        Driven from the stats path, which the health harvester already runs on a
        timer, so a transition is noticed without a thread of its own. Every
        source is sampled ONCE per call: asking twice could report a source as
        both down and alive within one message.

        Edge-triggered, so a source that stays down is one entry rather than a
        stream — and a source that flaps says so, which is the signal.
        """
        parts = self._source_health()
        live = [s.id for s, h in parts if h.get('status') != 'down']
        new_events = []
        # check-then-set under the lock: several API threads call get_stats()
        # concurrently and would otherwise both see the transition and log it
        with self._lock:
            for src, h in parts:
                now = h.get('status', 'down')
                was = self._seen.get(src.id)
                if was == now:
                    continue
                self._seen[src.id] = now
                if was is None:
                    continue      # first observation is not a transition
                lvl = {'ok': 'info', 'degraded': 'warn'}.get(now, 'error')
                others = [x for x in live if x != src.id]
                detail = (f"source {src.id} {was} -> {now}"
                          + (f"; still reading through {', '.join(others)}"
                             if others and now == 'down' else ""))
                ev = {"ts": round(time.time(), 3), "level": lvl,
                      "kind": "source_" + now, "message": detail,
                      "source": src.id}
                self.events.append(ev)
                new_events.append((lvl, detail))
        for lvl, detail in new_events:      # log outside the lock
            logger.log(logging.WARNING if lvl != 'info' else logging.INFO,
                       "device %s: %s", self.device_id, detail)

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
        self.note_source_transitions()
        events.extend(self.events)
        merged['connected'] = self.connected
        merged['poll_groups_detail'] = groups
        events.sort(key=lambda e: e.get('ts') or 0)
        merged['events'] = events
        merged['sources'] = per_source
        merged['provenance'] = self.arbiter.snapshot()
        merged['field_handovers'] = self.arbiter.handovers
        return merged
