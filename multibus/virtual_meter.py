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
"""Virtual Modbus meter engine.

Presents the live Janitza values in the register map a consumer expects (e.g.
a Carlo Gavazzi EM24 for Victron, a Fronius Smart Meter for the Fronius
DataManager), so one physical meter serves many. Template-driven: a meter =
a YAML template + source bindings; no code change to add a meter.

Design (see docs/virtual-meter-spec.md):
- Each instance runs an isolated pymodbus TCP server in its own thread — a
  UI/MQTT crash never stops metering.
- A supervisor loop refreshes the register block from the live values and runs
  a FRESHNESS WATCHDOG: if the source goes stale, the server STOPS responding
  (socket closed) so the consumer's own grid-meter-loss fail-safe triggers —
  we never feed silently-stale data into a control loop.
- Multi-register values are written atomically (no word-tearing).
"""
from __future__ import annotations

import asyncio
import logging
import math
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import yaml

from .encoder import RegisterEncoder

logger = logging.getLogger(__name__)

# value_provider(source_name) -> (engineering_value, monotonic_ts) or None if
# unknown — the timestamp is a time.monotonic() stamp (the freshness clock),
# NOT wall time.
ValueProvider = Callable[[str], Optional[tuple]]

# Bit-oriented function codes (coils / discrete inputs). A meter has no bit
# objects; the SimDevice shared block would otherwise serve register BITS to a
# coil read instead of the illegal-address refusal a real meter gives.
_BIT_FCS = (1, 2)


# A push source (MQTT-in, HTTP) stamps on its own thread; a stamp taken a
# moment after the rebuild read its clock is a race, not a corrupted stamp.
# Anything further ahead than this is still refused (F-31, 3.83.0).
FUTURE_TOLERANCE_S = 1.0


@dataclass
class RegisterDef:
    addr: int
    type: str
    scale: float = 1.0
    order: str = "big"
    source_kind: str = "const"          # const | const_str | live | sum | failover
    source: Any = 0                      # const value, string, or live source name;
                                         # a list for sum/failover (ordered candidates)
    length: int = 1                      # registers, for strings
    note: str = ""                       # human note (round-trips through the editor)
    # Composite (multi-source) staleness: per-row freshness bound override.
    # None → the source device's own bound (provider-supplied), else the
    # instance's stale_after_s. Lets a 60s BLE row coexist with a 250ms row.
    stale_after_s: Optional[float] = None
    # Cumulative-counter absence semantics (set by device_fallback wiring, not
    # template-authored): when the source goes stale, keep serving the LAST
    # GOOD value indefinitely instead of failing the row after max_hold_s. A
    # frozen counter is a true statement ("energy delivered so far"); an
    # unavailable span would fail the DataManager's whole block read, and
    # switching to another meter's lifetime total is a non-monotonic jump
    # that corrupts downstream kWh statistics. Never-resolved rows still
    # fail loudly (E1) — there is nothing true to pin.
    pin_on_stale: bool = False


@dataclass
class Template:
    id: str
    name: str
    kind: str = "flat"
    transport: dict = field(default_factory=dict)
    registers: list[RegisterDef] = field(default_factory=list)


def _parse_source(reg: dict) -> tuple[str, Any]:
    src = reg.get("source")
    if isinstance(src, dict):
        if "live" in src:
            return "live", src["live"]
        if "const" in src:
            return "const", src["const"]
        if "const_str" in src:
            return "const_str", src["const_str"]
        if "sum" in src:
            return "sum", src["sum"]          # sum of several live sources
        # ordered redundant sources: serve the first FRESH one, auto-switch on
        # staleness. `combined` is an alias (evcc's name for the same idea).
        if "failover" in src:
            return "failover", src["failover"]
        if "combined" in src:
            return "failover", src["combined"]
    # bare value → const
    return "const", src if src is not None else 0


def load_template(path: str) -> Template:
    with open(path) as f:
        d = yaml.safe_load(f)["template"]
    regs = []
    default_order = d.get("byte_order", "big")
    for r in d.get("registers", []):
        kind, src = _parse_source(r)
        if kind in ("sum", "failover"):
            if not isinstance(src, (list, tuple)) or not src or \
                    not all(isinstance(s, str) and s for s in src):
                raise ValueError(
                    f"register 0x{int(r['addr']):04x}: {kind} source must be a "
                    f"non-empty list of source names")
        row_stale = r.get("stale_after_s")
        regs.append(RegisterDef(
            addr=int(r["addr"]), type=r["type"], scale=float(r.get("scale", 1)),
            order=r.get("order", default_order), source_kind=kind, source=src,
            length=int(r.get("length", 1)), note=str(r.get("note", "")),
            stale_after_s=float(row_stale) if row_stale is not None else None,
        ))
    return Template(id=d["id"], name=d.get("name", d["id"]), kind=d.get("kind", "flat"),
                    transport=d.get("transport", {}), registers=regs)


class VMeterStats:
    """In-RAM observability for one virtual meter: a bounded query log plus
    counters. Records EVERY Modbus read the consumer issues (address, count,
    response sample, latency) and illegal-address attempts — the same data we
    used to reverse-engineer consumers, now a first-class feature. RAM only."""

    LOG_SIZE = 1024          # last N queries kept (ring buffer)
    RATE_SIZE = 300          # per-second rate buckets kept (5 min)
    EVENT_SIZE = 50          # last N lifecycle/error events kept (ring buffer)

    def __init__(self) -> None:
        self.queries: deque = deque(maxlen=self.LOG_SIZE)
        self.total = 0
        self.errors = 0
        self.bytes_rx = 0
        self.bytes_tx = 0
        self.by_addr: dict[int, int] = {}
        self.rate: deque = deque(maxlen=self.RATE_SIZE)   # (epoch_sec, count)
        # Engine lifecycle/error events — the "what happened" timeline (crash,
        # restart-failed, wedged, stale→stopped, supervise error). Kept apart
        # from the per-read query log so a noisy consumer probing illegal
        # addresses can't drown out the rare-but-important meter events.
        self.events: deque = deque(maxlen=self.EVENT_SIZE)
        self._cur_sec = 0
        self._cur_cnt = 0
        self.first_ts: Optional[float] = None
        self.last_ts: Optional[float] = None
        self._lock = threading.Lock()

    def record(self, fc: int, addr: int, count: int, resp: Optional[list],
               lat_us: float, ts: float, err: bool = False) -> None:
        with self._lock:
            self.total += 1
            if self.first_ts is None:
                self.first_ts = ts
            self.last_ts = ts
            if err:
                self.errors += 1
            self.bytes_rx += 12                       # MBAP(7)+fc(1)+addr(2)+count(2)
            self.bytes_tx += 9 if err else 9 + count * 2
            self.by_addr[addr] = self.by_addr.get(addr, 0) + 1
            self.queries.append({
                "ts": round(ts, 3), "fc": fc, "addr": addr, "count": count,
                "resp": list(resp)[:8] if resp is not None else None,
                "lat_us": int(lat_us), "err": err,
            })
            sec = int(ts)
            if sec != self._cur_sec:
                if self._cur_sec:
                    self.rate.append((self._cur_sec, self._cur_cnt))
                self._cur_sec, self._cur_cnt = sec, 0
            self._cur_cnt += 1

    def record_event(self, level: str, kind: str, message: str,
                     ts: Optional[float] = None) -> None:
        """Append an engine lifecycle/error event. level: error|warn|info.
        Thread-safe; never raises (observability must not break metering)."""
        try:
            with self._lock:
                self.events.append({
                    "ts": round(ts if ts is not None else time.time(), 3),
                    "level": level, "kind": kind,
                    "message": str(message)[:300],
                })
        except Exception:  # noqa: BLE001
            pass

    def last_error(self) -> Optional[dict]:
        """Most recent non-info event (error or warn), or None — surfaced in
        status()/MQTT so alertd can rule on it."""
        with self._lock:
            for e in reversed(self.events):
                if e["level"] in ("error", "warn"):
                    return dict(e)
        return None

    def req_rate(self, window_s: int = 10) -> float:
        """Average requests/sec over the last window_s seconds. Silent seconds
        count as zero (buckets only exist for seconds that had traffic)."""
        with self._lock:
            buckets = list(self.rate)
            if self._cur_sec:
                buckets.append((self._cur_sec, self._cur_cnt))
        if not buckets:
            return 0.0
        cutoff = buckets[-1][0] - window_s + 1
        recent = sum(c for (s, c) in buckets if s >= cutoff)
        return round(recent / window_s, 2)

    def snapshot(self, limit: int = 200) -> dict:
        with self._lock:
            rate = list(self.rate)
            if self._cur_sec:
                rate.append((self._cur_sec, self._cur_cnt))
            top = sorted(self.by_addr.items(), key=lambda kv: -kv[1])[:15]
            ql = list(self.queries)[-limit:]
            events = list(self.events)
            return {
                "total": self.total, "errors": self.errors,
                "bytes_rx": self.bytes_rx, "bytes_tx": self.bytes_tx,
                "first_ts": self.first_ts, "last_ts": self.last_ts,
                "rate": rate, "top_addrs": top, "queries": ql,
                "events": events,
            }


# ── in-band quality block (gateway-wide convention) ─────────────────────────
# A fixed, read-only block a downstream Modbus consumer can poll to judge the
# data it just read — without MQTT access. One layout for EVERY virtual meter
# (opt-in per instance), all values uint16/uint32 big-endian (PLC-friendly):
#   +0 u16 format version (=1)
#   +1 u16 state: 0=legacy (per-register quality not judged), 1=ok (all fresh),
#                 2=degraded (some stale/held/missing), 3=stale (none fresh)
#   +2 u16 fresh rows   +3 u16 stale rows   +4 u16 missing rows
#   +5 u16 total data rows
#   +6 u32 age of the newest fresh value in seconds (0xFFFFFFFF = never)
QUALITY_BASE = 61440          # 0xF000 — far above any real meter map
QUALITY_SPAN = 8


class VirtualMeter:
    """One emulated meter: template + value provider + a TCP server."""

    def __init__(self, template: Template, value_provider: ValueProvider,
                 stale_after_s: float = 15.0, update_interval_s: float = 1.0,
                 debug_reads: bool = False,
                 on_stale: str = "legacy", max_hold_s: float = 30.0,
                 quality_block: bool = False):
        self.t = template
        self.provider = value_provider
        self.stale_after_s = stale_after_s
        self.update_interval_s = update_interval_s
        self.debug_reads = debug_reads          # log every client read request
        # Staleness policy (composite/aggregator meters). 'legacy' = the exact
        # pre-composite behavior (gap keeps last words; ONE instance-level
        # freshness watchdog) — the default for existing instances, so meters
        # already feeding a control loop (Victron EM24) are byte-identical.
        # Policy modes add PER-REGISTER quality:
        #   fail     → reads touching a stale/missing register get a Modbus
        #              exception (consumer's meter-loss fail-safe triggers)
        #   sentinel → SunSpec NA words (float NaN, int16 0x8000, …)
        #   hold     → keep last value up to max_hold_s, then behave like fail
        # Server-up rule in policy modes: serving while AT LEAST ONE live source
        # is fresh; all-stale still stops the server (nothing trustworthy left).
        self.on_stale = on_stale if on_stale in ("legacy", "fail", "sentinel", "hold") else "legacy"
        # sentinel encodes SunSpec NA words — unambiguous for float (NaN) but for
        # an INTEGER row the NA sentinel (0x8000 / 0xFFFF) reads as a plausible
        # value to a non-SunSpec consumer. Warn so the operator picks 'fail' for
        # integer maps feeding a plain PLC.
        if self.on_stale == "sentinel":
            _int_rows = [r.addr for r in template.registers
                         if str(r.type).lower() in ("int16", "uint16", "int32",
                                                    "uint32", "int64", "uint64", "short")]
            if _int_rows:
                logger.warning("vmeter[%s] sentinel policy on %d integer row(s) — a "
                               "non-SunSpec consumer may read the NA sentinel as a real "
                               "value; consider 'fail' for integer maps", template.id, len(_int_rows))
        self.max_hold_s = float(max_hold_s)
        self._last_good: dict[int, tuple[list[int], float]] = {}   # addr → (words, ts) for hold
        self._unavail_spans: list[tuple[int, int]] = []            # [start, end) refused when policy=fail
        self._quality = {"fresh": 0, "stale": 0, "missing": 0}     # last rebuild summary (status)
        self._policy_fresh = False                                 # supervisor verdict (all modes)
        self._legacy_all_fresh = False                             # legacy gate: every stamped row fresh
        self._regs_out: list[tuple[int, list[int]]] = []   # (addr, words) per register
        # Block spans only [min_addr, max_addr] of the template. Starting the
        # block at the map base (not 0) makes reads BELOW the map return a Modbus
        # illegal-address exception — exactly like a real meter, which has no
        # registers there. Consumers (e.g. the Fronius DataManager) probe low
        # addresses (11=CG model, 768, 1706) to disambiguate meter types; a real
        # meter excepts, so returning 0 from a 0-based block mis-identifies us.
        def _span(r: RegisterDef) -> int:
            return max(1, r.length if r.type == 'string'
                       else RegisterEncoder.REGISTER_COUNTS.get(r.type.lower(), 2))
        self._block_base = min((int(r.addr) for r in template.registers), default=0)
        self._block_size = (max((int(r.addr) + _span(r) for r in template.registers),
                                default=self._block_base + 16) - self._block_base) + 4
        # in-band quality block: refuse silently-overlapping maps (a template
        # that really uses 0xF000+ keeps its own registers; quality stays off)
        self.quality_block = bool(quality_block)
        if (self.quality_block
                and self._block_base < QUALITY_BASE + QUALITY_SPAN
                and self._block_base + self._block_size > QUALITY_BASE):
            logger.warning(f"[{template.id}] template map overlaps the quality "
                           f"block at {QUALITY_BASE} — quality block disabled")
            self.quality_block = False
        self._lock = threading.Lock()
        # The live serving surface: the SimRuntime's registers list, taken by
        # reference once the server is built. Every value is pushed as ONE
        # slice assignment and read by the server as one slice — atomic under
        # the GIL, so the hot path is lock-free and tear-free by construction
        # (the old sparse-block word-by-word path needed a read/write lock).
        self._sim_regs: Optional[list] = None
        self._sim_base = 0
        self._stop = threading.Event()
        self._sup_thread: threading.Thread | None = None
        self._server = None
        self._server_thread: threading.Thread | None = None
        self._server_loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._last_fresh_ts = 0.0
        self._started_ts = 0.0                  # current serving session start (for uptime)
        self._conn_seen: dict[str, float] = {}  # connection ident -> first-seen ts (uptime)
        self._conn_lock = threading.Lock()      # guards _conn_seen across threads
        self.stats = VMeterStats()              # in-RAM query log + counters
        self._failover_active: dict[int, str] = {}   # addr → active source name
        self._encode_failed: set[int] = set()        # addrs warned unencodable (edge-triggered)
        self._cap_warned = False                     # connection-cap warning (edge-triggered, F-60)
        # rows that have resolved at least once since this meter was built —
        # a never-resolved row has no last value to hold, so legacy mode must
        # fail the freshness verdict for it (external audit E1)
        self._row_seen: set[int] = set()
        self._row_unresolved_warned: set[int] = set()
        # Freshness is judged on the MONOTONIC clock (driver 'mono' stamps vs
        # time.monotonic()), so it is immune to wall-clock/NTP steps by
        # construction. This guard is kept only to emit a diagnostic clock_step
        # event; it no longer participates in the freshness verdict.
        self._clock_guard = self.ClockStepGuard()

    # ── value resolution + encoding ────────────────────────────────────────
    @staticmethod
    def _got3(got) -> tuple[Any, Optional[float], Optional[float]]:
        """Normalize a provider result to (value, ts, bound) — providers may
        return 2-tuples (legacy) or 3-tuples (bound = source device's own
        staleness threshold). A non-finite number (inf/nan from an MQTT
        publisher or a runaway calculation) is no measurement: it becomes
        MISSING here, so the on_stale policy judges it instead of a consumer
        reading a float32 infinity as a plausible value (3.83.0)."""
        if not got:
            return None, None, None
        value = got[0]
        if isinstance(value, float) and not math.isfinite(value):
            value = None
        if len(got) >= 3:
            return value, got[1], got[2]
        return value, got[1], None

    def _span(self, reg: RegisterDef) -> int:
        return max(1, reg.length if reg.type == 'string'
                   else RegisterEncoder.REGISTER_COUNTS.get(reg.type.lower(), 2))

    def _resolve(self, reg: RegisterDef) -> tuple[Optional[list[int]], Optional[float], Optional[float]]:
        """Guarded resolve: a row whose source carries an unencodable value
        (a text enum/bits/string bound to a numeric row) degrades to MISSING —
        it must never abort the whole block rebuild, because the supervisor
        computes freshness only after a full rebuild and an aborted one leaves
        the served frame frozen with the stale-stop fail-safe disarmed."""
        try:
            words, ts, bound = self._resolve_row(reg)
        except (TypeError, ValueError, OverflowError, struct.error) as e:
            # OverflowError: an infinite or 1e300 source value (an MQTT
            # publisher can send one) → float32 / int conversion; struct.error:
            # a word outside the packer's range. Either one escaping here
            # froze the served frame with the stale-stop disarmed (3.83.0).
            if reg.addr not in self._encode_failed:
                self._encode_failed.add(reg.addr)
                msg = (f"0x{reg.addr:04x}: unencodable source value ({e}) — "
                       "row treated as missing")
                self.stats.record_event("warn", "encode", msg)
                logger.warning("virtual meter %s: %s", self.t.id, msg)
            return None, None, None
        if words is not None and reg.addr in self._encode_failed:
            self._encode_failed.discard(reg.addr)
            self.stats.record_event("info", "encode",
                                    f"0x{reg.addr:04x}: source value encodable again")
        return words, ts, bound

    def _resolve_row(self, reg: RegisterDef) -> tuple[Optional[list[int]], Optional[float], Optional[float]]:
        """Return (register words, source_ts, source_bound) — ts/bound None for
        const/unknown. For ``sum`` the ts follows the policy mode: legacy keeps
        the historical newest-member ts; policy modes use the OLDEST member
        (quality of a sum = its worst input — a partial/mixed-age sum is silent
        corruption, exactly what the policies exist to prevent)."""
        enc = RegisterEncoder(reg.order)
        if reg.source_kind == "const":
            return enc.encode(reg.source, reg.type, reg.scale), None, None
        if reg.source_kind == "const_str":
            return enc.encode_string(str(reg.source), reg.length), None, None
        if reg.source_kind == "sum":
            total, newest, oldest, tightest = 0.0, None, None, None
            for name in reg.source:
                value, ts, bound = self._got3(self.provider(name))
                if value is None:
                    return None, None, None      # any missing → no sum (never partial)
                total += float(value)
                if ts:
                    newest = max(newest or 0.0, ts)
                    oldest = min(oldest if oldest is not None else ts, ts)
                if bound is not None:
                    tightest = min(tightest if tightest is not None else bound, bound)
            eff_ts = newest if self.on_stale == "legacy" else oldest
            return enc.encode(total, reg.type, reg.scale), eff_ts, tightest
        if reg.source_kind == "failover":
            value, ts, bound, name = self._resolve_failover(reg)
            if value is None:
                return None, None, None
            self._note_failover(reg, name)
            return enc.encode(value, reg.type, reg.scale), ts, bound
        # live
        value, ts, bound = self._got3(self.provider(reg.source) if reg.source else None)
        if value is None:
            return None, None, None
        return enc.encode(value, reg.type, reg.scale), ts, bound

    def _resolve_failover(self, reg: RegisterDef):
        """Pick the highest-priority FRESH candidate from an ordered redundant
        source list; if none is fresh, fall back to the first candidate that has
        a value at all (stale) so the on_stale policy judges it exactly as it
        would a single stale source. Returns (value, ts, bound, active_name) or
        (None, None, None, None) when no candidate is even known."""
        now = time.monotonic()
        fallback = None
        for name in reg.source:
            value, ts, bound = self._got3(self.provider(name))
            if value is None:
                continue
            if fallback is None:
                fallback = (value, ts, bound, name)     # first known → stale path
            if self._is_fresh(now, ts, self._row_bound(reg, bound)):
                return value, ts, bound, name           # first fresh wins
        return fallback if fallback is not None else (None, None, None, None)

    def _note_failover(self, reg: RegisterDef, name: str) -> None:
        """Record + log which source currently feeds a failover row, so a switch
        is traceable long after it happened. The first bind logs the initial
        routing (process log only — no event, to keep the ring quiet at boot). A
        later change writes BOTH an event (UI Logs / alertd via last_error) AND a
        process-log line (container logs, grep-able), naming the meter, register
        and both sources: warn when it drops to a lower-priority source, info
        when it recovers toward the primary. Steady state is silent."""
        prev = self._failover_active.get(reg.addr)
        if prev == name:
            return
        self._failover_active[reg.addr] = name
        tag = f"vmeter '{self.t.id}' 0x{reg.addr:04x}"
        if prev is None:                                  # first bind — no event
            logger.info("%s: failover bound to '%s' (priority: %s)",
                        tag, name, " > ".join(reg.source))
            return
        try:
            recovering = reg.source.index(name) < reg.source.index(prev)
        except ValueError:
            recovering = False
        self.stats.record_event(
            "info" if recovering else "warn", "failover",
            f"0x{reg.addr:04x}: source {prev} → {name}"
            + (" (recovered)" if recovering else " (failover)"))
        if recovering:
            logger.info("%s: failover RECOVERED  %s → %s", tag, prev, name)
        else:
            logger.warning("%s: FAILOVER  %s → %s  (higher-priority source stale/missing)",
                           tag, prev, name)

    def _row_bound(self, reg: RegisterDef, src_bound: Optional[float]) -> float:
        """Effective freshness bound for one row. An explicit row stale_after_s
        wins outright (it may tighten or relax). Otherwise a source-derived
        bound (device threshold or 2.5× the row's poll-group interval) may only
        RELAX the instance floor — a cadence-derived bound below the instance
        bound must never make serving stricter, or a fast row's single hiccup
        would flap the whole meter."""
        if reg.stale_after_s is not None:
            return reg.stale_after_s
        if src_bound is not None:
            return max(src_bound, self.stale_after_s)
        return self.stale_after_s

    @staticmethod
    def _is_fresh(now: float, ts: Optional[float], bound: float) -> bool:
        """Fresh iff a real, non-future timestamp within ``bound``. A FUTURE
        stamp (now-ts < 0 — a corrupted or mis-sourced monotonic stamp) is
        NEVER fresh: serving such frozen values as live into an ESS control
        loop is exactly the failure the freshness watchdog exists to prevent."""
        return bool(ts) and -FUTURE_TOLERANCE_S <= (now - ts) <= bound

    def _rebuild_block(self) -> float:
        """Recompute (addr, words) for every register. Returns newest live ts.

        LEGACY mode (default): a register with no value leaves a gap — the
        block keeps its last words (pre-composite contract; the production
        Victron EM24 depends on it). Freshness is fail-CLOSED per row since
        3.3.4: every row that resolves to a value is judged against its own
        bound cascade (row bound → source-device bound → instance bound), and
        one stale row fails the whole instance — the supervisor then stops the
        server (the meter goes silent) instead of serving that row's old words
        as live. Legacy still never refuses individual reads; the fail-safe is
        all-or-nothing. (Legacy ``sum`` rows keep their historical newest-member
        ts — use a policy mode for worst-input sum quality.)

        Policy modes (fail/sentinel/hold) judge freshness PER REGISTER against
        the same bound cascade and apply the configured treatment; the returned
        ts is the newest FRESH one, so the supervisor's server-up rule becomes
        "at least one live source fresh".
        """
        if self.on_stale == "legacy":
            now = time.monotonic()
            out: list[tuple[int, list[int]]] = []
            newest = 0.0
            all_fresh = True
            for reg in self.t.registers:
                regs, ts, src_bound = self._resolve(reg)
                if regs is None:
                    # "leave gap (keep last value)" assumes a last value
                    # EXISTS. A row that has NEVER resolved (renamed source,
                    # deselected register, template typo) has none — the
                    # zero-seeded block would serve a hard 0 as a plausible
                    # measurement (external audit E1; the spec's own rule:
                    # "absence is never encodable as a plausible
                    # measurement"). Until it resolves once, it fails the
                    # verdict like a stale row — the meter stays down LOUDLY
                    # instead of feeding 0 W to an ESS.
                    if (reg.source_kind not in ("const", "const_str")
                            and reg.addr not in self._row_seen):
                        all_fresh = False
                        if reg.addr not in self._row_unresolved_warned:
                            self._row_unresolved_warned.add(reg.addr)
                            msg = (f"0x{reg.addr:04x}: source never resolved — "
                                   "meter withheld (would serve 0 otherwise)")
                            self.stats.record_event("error", "unresolved", msg)
                            logger.error("virtual meter %s: %s", self.t.id, msg)
                    continue                          # leave gap (keep last value)
                self._row_seen.add(reg.addr)
                self._row_unresolved_warned.discard(reg.addr)
                out.append((reg.addr, [w & 0xffff for w in regs]))
                if reg.source_kind in ("const", "const_str"):
                    continue                          # consts are never stale
                if ts:
                    newest = max(newest, ts)
                # ts None (source without a mono stamp) fails closed here too
                if not self._is_fresh(now, ts, self._row_bound(reg, src_bound)):
                    # A pinned cumulative counter (device_fallback wiring) is
                    # ALLOWED to be older than the block: freezing it is its
                    # defined absence semantics, and it must not gate off a
                    # meter whose instantaneous rows are healthy on the twin.
                    # (It reaches here only after resolving once — E1 intact.)
                    if not reg.pin_on_stale:
                        all_fresh = False
            self._legacy_all_fresh = all_fresh and newest > 0.0
            if self.quality_block:
                out.append((QUALITY_BASE, self._quality_words(newest)))
            with self._lock:
                self._regs_out = out
            return newest

        now = time.monotonic()   # freshness on the MONOTONIC clock (mono stamps)
        out = []
        unavail: list[tuple[int, int]] = []
        quality = {"fresh": 0, "stale": 0, "missing": 0}
        newest_fresh = 0.0

        def _mark_unavailable(reg: RegisterDef):
            if self.on_stale == "sentinel":
                enc = RegisterEncoder(reg.order)
                out.append((reg.addr, [w & 0xffff
                                       for w in enc.sentinel_words(reg.type, reg.length)]))
            else:                                     # fail (and hold past its cap)
                unavail.append((reg.addr, reg.addr + self._span(reg)))

        for reg in self.t.registers:
            words, ts, src_bound = self._resolve(reg)
            if reg.source_kind in ("const", "const_str"):
                if words is not None:
                    out.append((reg.addr, [w & 0xffff for w in words]))
                continue
            bound = self._row_bound(reg, src_bound)
            if words is not None and self._is_fresh(now, ts, bound):
                out.append((reg.addr, [w & 0xffff for w in words]))
                self._last_good[reg.addr] = ([w & 0xffff for w in words], ts)
                quality["fresh"] += 1
                newest_fresh = max(newest_fresh, ts)
                continue
            quality["stale" if (words is not None or reg.addr in self._last_good) else "missing"] += 1
            if reg.pin_on_stale:
                held = self._last_good.get(reg.addr)
                if held:
                    # Cumulative counter: serve the last good value with NO
                    # hold cap, in every policy — a frozen counter is true
                    # ("energy so far"), a sentinel/unavailable span would
                    # fail the DataManager's whole block read, and max_hold_s
                    # is sized for instantaneous rows. Resumes (forward jump,
                    # legitimate) when the primary returns. Never-resolved
                    # rows have no held value and fall through (E1).
                    out.append((reg.addr, held[0]))
                    continue
            if self.on_stale == "hold":
                held = self._last_good.get(reg.addr)
                # the hold window starts when the row went STALE (its stamp
                # plus its freshness bound), not at the stamp itself — measured
                # from the stamp, a row whose bound was >= max_hold_s never
                # held at all (F-62, 3.83.0). Same future-timestamp guard: a
                # held stamp from before a backward clock step must not extend
                # the hold past max_hold_s.
                if held and held[1] <= now and (now - (held[1] + (bound or 0.0))) <= self.max_hold_s:
                    out.append((reg.addr, held[0]))   # bounded hold
                    continue
            _mark_unavailable(reg)

        self._quality = quality                       # before the words are built
        if self.quality_block:
            out.append((QUALITY_BASE, self._quality_words(newest_fresh)))
        with self._lock:
            self._regs_out = out
        self._unavail_spans = unavail                 # atomic swap (read by server thread)
        return newest_fresh

    def _quality_words(self, newest_fresh_ts: float) -> list[int]:
        q = self._quality
        total = sum(1 for r in self.t.registers
                    if r.source_kind not in ("const", "const_str"))
        if self.on_stale == "legacy":
            state = 0
        elif q["fresh"] and not (q["stale"] or q["missing"]):
            state = 1
        elif q["fresh"]:
            state = 2
        else:
            state = 3
        age = (int(min(max(0.0, time.monotonic() - newest_fresh_ts), 0xFFFFFFFE))
               if newest_fresh_ts else 0xFFFFFFFF)
        return [1, state, q["fresh"] & 0xffff, q["stale"] & 0xffff,
                q["missing"] & 0xffff, total & 0xffff,
                (age >> 16) & 0xffff, age & 0xffff]

    # ── server lifecycle (isolated thread + own loop) ─────────────────────
    def _start_server(self) -> None:
        from pymodbus.constants import ExcCodes
        from pymodbus.server import ModbusTcpServer
        from pymodbus.simulator import DataType, SimData, SimDevice

        host = self.t.transport.get("bind", "0.0.0.0")
        port = int(self.t.transport.get("port", 1502))
        unit = int(self.t.transport.get("unit_id", 1))
        if self.debug_reads:
            # Full pymodbus frame logging — shows every received PDU + our
            # response/exception, so a 'timeout' (request we don't answer) is
            # visible: the failing function code / address.
            try:
                logging.getLogger("pymodbus").setLevel(logging.DEBUG)
                logging.getLogger("pymodbus").propagate = True
            except Exception:  # noqa: BLE001
                pass
        # One island over [base, base+size) so range reads spanning row gaps
        # succeed; every undefined address (below base, above the map, the void
        # before the quality island) refuses with illegal-address — real-meter
        # behaviour. Consumers (e.g. the Fronius DataManager) probe unmapped
        # addresses to disambiguate meter types, so the refusal is load-bearing.
        simdata = [SimData(address=self._block_base, count=self._block_size,
                           values=0, datatype=DataType.REGISTERS)]
        if self.quality_block:
            simdata.append(SimData(address=QUALITY_BASE, count=QUALITY_SPAN,
                                   values=0, datatype=DataType.REGISTERS))
        # id=0 → respond on ANY device id (a client may poll unit 240 etc.);
        # SimCore routes every unknown id to device 0. That is the historical
        # default (a mis-addressed consumer still gets an answer). With
        # transport.strict_unit_id: true only the configured unit_id answers
        # (F-61, 3.83.0) — every other id gets the gateway-path exception.
        strict = bool(self.t.transport.get("strict_unit_id", False))
        device = SimDevice(id=unit if strict else 0, simdata=simdata)

        # ── always-on instrumentation: every read the consumer issues is
        #    recorded (addr, count, response sample, latency) into stats; a
        #    refused read/write is recorded as an error. This is the data that
        #    let us reverse-engineer consumers — a first-class observability
        #    feature. Cost: one in-RAM append per request.
        _stats, _dbg, _tid = self.stats, self.debug_reads, self.t.id

        def _wire_runtime(server) -> None:
            """Take the live registers reference + instrument the runtime.

            The SimRuntime serves every request from ONE mutable registers
            list; the supervisor pushes fresh values into it by slice
            assignment. The async_getValues/async_setValues wrappers are the
            single choke point for policy: stats, the 'fail' staleness spans,
            the no-coils refusal and the read-only refusal all live here."""
            runtime = server.context.devices[0]
            start = runtime.block["x"][0]
            self._sim_regs, self._sim_base = runtime.block["x"][2], start
            _orig_get = runtime.async_getValues

            async def _instrumented_get(func_code, address, count):
                if func_code in _BIT_FCS:              # a meter has no coils
                    try:
                        _stats.record(int(func_code), int(address), int(count),
                                      None, 0.0, time.time(), err=True)
                    except Exception:  # noqa: BLE001
                        pass
                    return ExcCodes.ILLEGAL_ADDRESS
                t0 = time.perf_counter()
                vals = await _orig_get(func_code, address, count)
                refused = isinstance(vals, ExcCodes)
                if not refused and self._unavail_spans:
                    # 'fail' staleness policy: a read touching an unavailable
                    # (stale/missing-source) register is REFUSED with a Modbus
                    # exception instead of serving frozen/invented words — the
                    # consumer's own meter-loss fail-safe takes over. Spans are
                    # swapped atomically by the supervisor each tick.
                    a, c = int(address), int(count)
                    for s, e in self._unavail_spans:
                        if a < e and s < a + c:
                            vals, refused = ExcCodes.ILLEGAL_ADDRESS, True
                            break
                try:
                    _stats.record(int(func_code), int(address), int(count),
                                  None if refused else vals,
                                  (time.perf_counter() - t0) * 1e6,
                                  time.time(), err=refused)
                    if _dbg:
                        logger.warning("vmeter[%s] %s addr=%s count=%s", _tid,
                                       "ERR refused-read" if refused else "READ",
                                       address, count)
                except Exception:  # noqa: BLE001
                    pass
                return vals

            async def _instrumented_set(func_code, address, values):
                # READ-ONLY: writes are refused (a real meter has no writable
                # data registers) so a consumer cannot inject values into the
                # block — pushed registers would be overwritten within
                # update_interval_s, but pads/gaps would keep an injected value
                # forever (a poisoned grid reading straight into the ESS loop).
                try:
                    _stats.record(int(func_code), int(address), len(values),
                                  None, 0.0, time.time(), err=True)
                    logger.warning("vmeter[%s] REFUSED write fc=%s addr=%s count=%s",
                                   _tid, func_code, address, len(values))
                except Exception:  # noqa: BLE001
                    pass
                return ExcCodes.ILLEGAL_ADDRESS

            runtime.async_getValues = _instrumented_get
            runtime.async_setValues = _instrumented_set

        def _run():
            loop = asyncio.new_event_loop()
            self._server_loop = loop
            asyncio.set_event_loop(loop)

            async def _serve():
                # ModbusTcpServer.__init__ needs a RUNNING loop → build it here.
                self._server = ModbusTcpServer(device, address=(host, port))
                _wire_runtime(self._server)
                self._push_to_ctx()          # seed before the first request
                await self._server.serve_forever()

            try:
                loop.run_until_complete(_serve())
            except Exception as e:  # noqa: BLE001
                logger.info("virtual meter %s server loop ended: %s", self.t.id, e)
                # A genuine crash (not an intended shutdown, which returns
                # cleanly). Record it so the UI can show why the meter dropped.
                if threading.current_thread() is self._server_thread:
                    self.stats.record_event("error", "server_exit",
                                            f"server loop crashed: {e}")
            finally:
                # Only relinquish the running flag if WE are still the active
                # server thread — a newer restart may already own the slot, and
                # clobbering it would drop a healthy server. The supervisor then
                # restarts us on the next tick if data is still fresh.
                if threading.current_thread() is self._server_thread:
                    self._running = False
                    self._server = None
                try:
                    loop.close()
                except Exception:  # noqa: BLE001
                    pass

        self._server_thread = threading.Thread(target=_run, daemon=True,
                                               name=f"vmeter-{self.t.id}")
        self._server_thread.start()
        self._running = True
        self._started_ts = time.time()
        self.stats.record_event("info", "started",
                                f"listening on {host}:{port} unit {unit}")
        logger.info("virtual meter %s LISTENING on %s:%d unit %d",
                    self.t.id, host, port, unit)

    def _alive(self) -> bool:
        """True only if the server thread is genuinely up — not just flagged."""
        return (self._running and self._server_thread is not None
                and self._server_thread.is_alive())

    def _serving_ok(self, port: int) -> bool:
        """Liveness probe: a thread can be alive() but wedged (not accepting
        connections). A quick TCP connect confirms the listener actually serves.
        Probe the CONFIGURED bind — a server bound to a specific interface is not
        reachable on 127.0.0.1, so a loopback probe would force-restart a healthy
        meter in a loop."""
        bind = str(self.t.transport.get("bind", "0.0.0.0") or "0.0.0.0")
        host = "127.0.0.1" if bind in ("0.0.0.0", "", "::") else bind
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except Exception:  # noqa: BLE001
            return False

    # keepalive timers: probe after 60s idle, then every 10s, 3 misses → dead.
    # A vanished peer is reaped by the kernel in ~90s; a live consumer polling
    # sub-second never even reaches the idle threshold.
    _KEEPALIVE_OPTS = (
        [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
        + ([(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)] if hasattr(socket, "TCP_KEEPIDLE") else [])
        + ([(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)] if hasattr(socket, "TCP_KEEPINTVL") else [])
        + ([(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)] if hasattr(socket, "TCP_KEEPCNT") else [])
    )

    # struct tcp_info: 8 one-byte fields, then u32s — tcpi_last_data_recv is
    # the 12th u32 (offset 52). The struct is append-only in the Linux ABI, so
    # the offset is stable; value = milliseconds since the peer last sent DATA
    # on this socket (a silent connection grows it from the handshake on).
    _TCPI_LAST_DATA_RECV_OFF = 52
    IDLE_TIMEOUT_DEFAULT_S = 300
    MAX_CONNECTIONS_DEFAULT = 16    # per meter; transport.max_connections (F-60, 3.83.0)

    @staticmethod
    def _idle_ms_from_tcp_info(info: bytes) -> Optional[int]:
        """Extract tcpi_last_data_recv (ms) from a raw TCP_INFO buffer."""
        off = VirtualMeter._TCPI_LAST_DATA_RECV_OFF
        if len(info) < off + 4:
            return None
        return int.from_bytes(info[off:off + 4], "little")

    def _reap_idle_connections(self) -> None:
        """Close client connections that sent no request for idle_timeout_s.

        Companion to keepalive, which only catches DEAD peers: a LIVE host
        whose application abandoned the socket answers kernel probes forever.
        Observed in production (2026-09-11): the Venus/Ekrano network scan
        opens connections to the :502 vmeter and never closes them — 4
        accumulated over 9 days, one per host-side incident. TCP_INFO's
        tcpi_last_data_recv gives per-socket idle time kernel-side, so no
        request attribution is needed. Active consumers poll every 1-2 s and
        never approach the threshold. transport.idle_timeout_s per vmeter
        (default 300; 0 disables). Close is marshalled onto the server's own
        event loop (this runs on the supervisor thread)."""
        if not hasattr(socket, "TCP_INFO"):
            return                          # non-Linux — keepalive still applies
        try:
            timeout_s = float(self.t.transport.get(
                "idle_timeout_s", self.IDLE_TIMEOUT_DEFAULT_S) or 0)
        except (TypeError, ValueError):
            timeout_s = self.IDLE_TIMEOUT_DEFAULT_S
        srv, loop = self._server, self._server_loop
        if not srv or not loop:
            return
        # connection cap: pymodbus keeps an unbounded active_connections; a
        # peer opening sockets and keeping each one just alive would hold FDs
        # forever and starve the UI, MQTT and the Modbus client to the real
        # meter. Beyond the cap, the surplus is closed (newest first).
        try:
            cap = int(self.t.transport.get("max_connections", self.MAX_CONNECTIONS_DEFAULT) or 0)
        except (TypeError, ValueError):
            cap = self.MAX_CONNECTIONS_DEFAULT
        handlers = list(getattr(srv, "active_connections", {}).values())
        if cap > 0 and len(handlers) > cap:
            for handler in handlers[cap:]:
                tr = getattr(handler, "transport", None)
                if tr is None:
                    continue
                peer = tr.get_extra_info("peername")
                if not getattr(self, '_cap_warned', False):
                    self._cap_warned = True
                    msg = f"more than {cap} client connections — closing the surplus ({peer} first)"
                    self.stats.record_event("warn", "conn_cap", msg)
                    logger.warning("virtual meter %s: %s", self.t.id, msg)
                try:
                    loop.call_soon_threadsafe(tr.close)
                except RuntimeError:
                    pass
        elif getattr(self, '_cap_warned', False) and cap > 0 and len(handlers) <= cap // 2:
            self._cap_warned = False
        if timeout_s <= 0:
            return
        try:
            for handler in handlers[:cap] if cap > 0 else handlers:
                tr = getattr(handler, "transport", None)
                sock = tr.get_extra_info("socket") if tr is not None else None
                if sock is None:
                    continue
                try:
                    info = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 104)
                except OSError:
                    continue                # closing under us — kernel wins
                idle_ms = self._idle_ms_from_tcp_info(info)
                if idle_ms is None or idle_ms < timeout_s * 1000:
                    continue
                peer = tr.get_extra_info("peername")
                self.stats.record_event(
                    "info", "idle_reap",
                    f"closed idle connection {peer} "
                    f"(no request for {idle_ms / 1000:.0f}s)")
                logger.info("virtual meter %s: reaping idle connection %s "
                            "(no request for %.0fs)", self.t.id, peer,
                            idle_ms / 1000)
                loop.call_soon_threadsafe(tr.close)
        except Exception:  # noqa: BLE001 — reaping is best-effort hygiene
            pass

    def _apply_keepalive(self) -> None:
        """Enable TCP keepalive on every accepted client socket. A Modbus server
        never sends unsolicited data, so a consumer that vanishes without FIN/RST
        (e.g. the Fronius DataManager sleeping at dusk) would otherwise leave its
        connection ESTABLISHED forever — leaking one FD per wake-up cycle. With
        keepalive the kernel probes the dead peer, asyncio gets connection_lost
        and pymodbus drops the handler. Idempotent; swept periodically so new
        connections are always covered. Never raises."""
        srv = self._server
        if not srv:
            return
        try:
            for handler in list(getattr(srv, "active_connections", {}).values()):
                tr = getattr(handler, "transport", None)
                sock = tr.get_extra_info("socket") if tr is not None else None
                if sock is None:
                    continue
                try:
                    for level, opt, val in self._KEEPALIVE_OPTS:
                        sock.setsockopt(level, opt, val)
                except OSError:
                    pass                    # socket already closing — kernel wins
        except Exception:  # noqa: BLE001
            pass

    def _stop_server(self, reason: str = "") -> None:
        if self._server and self._server_loop:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._server.shutdown(),
                                                       self._server_loop)
                fut.result(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self._running = False
        if reason:
            self.stats.record_event("warn", "stopped", reason)
        logger.info("virtual meter %s STOPPED responding (stale/shutdown)", self.t.id)

    def _push_to_ctx(self) -> None:
        """Write each register's words into the live serving list — ONE slice
        assignment per VALUE (never per word). A slice store is atomic under
        the GIL and the server reads each response as one slice, so a consumer
        sees a multi-word value all-old or all-new, never torn — including
        under quality_block (the old sparse path needed a read/write lock for
        this; validated under load as the P2 word-tearing fix)."""
        regs = self._sim_regs
        if regs is None:
            return
        base = self._sim_base
        with self._lock:
            out = list(self._regs_out)
        for addr, words in out:
            off = addr - base
            regs[off:off + len(words)] = words

    @property
    def serving_block_ready(self) -> bool:
        """True once the server's live registers list exists (kept after a
        stale-stop so the decode view still shows the last served words)."""
        return self._sim_regs is not None

    def served_words(self, addr: int, count: int) -> list[int]:
        """Raw words exactly as currently served (decode/inspect path)."""
        regs, off = self._sim_regs, int(addr) - self._sim_base
        if regs is None or off < 0 or off + count > len(regs):
            raise IndexError(f"address {addr} outside the served block")
        return list(regs[off:off + count])

    # ── supervisor ─────────────────────────────────────────────────────────

    class ClockStepGuard:
        """Detects wall-clock steps (NTP/chrony) — now DIAGNOSTIC ONLY.

        History: this once rebased freshness so a wall-clock jump couldn't
        fail-safe-stop a meter over fresh data (seen live 2026-07-28: a chrony
        install stepped the clock +138s and both meters briefly dropped their
        consumers). As of 3.3.0 freshness is judged on the MONOTONIC clock
        (driver 'mono' stamps vs time.monotonic()), so it is immune to steps by
        construction and no longer depends on this guard. The guard is kept only
        to emit a `clock_step` event so operators can see "system clock adjusted"
        in the timeline; it plays no part in the freshness verdict."""

        STEP_THRESHOLD_S = 5.0

        def __init__(self):
            self._drift_prev = None
            self.last_step_s = 0.0

        def tick(self) -> bool:
            """Call once per supervisor pass; True when a wall-clock step was
            detected on THIS tick (the caller logs a clock_step event once).
            Purely diagnostic — the freshness verdict is on the monotonic clock
            and does not consult this guard."""
            drift = time.time() - time.monotonic()
            stepped = (self._drift_prev is not None
                       and abs(drift - self._drift_prev) >= self.STEP_THRESHOLD_S)
            if stepped:
                self.last_step_s = drift - self._drift_prev
            self._drift_prev = drift
            return stepped

    def _supervise(self) -> None:
        """Reliability core, runs every update_interval_s:
        - fresh data + server down/crashed -> (re)start it (uptime guard)
        - fresh data + server up           -> refresh the register block
        - stale data                       -> stop responding (consumer fail-safe)
        A crashed server thread is detected via _alive() (not just the running
        flag), so an unexpected loop death self-heals on the next tick. The whole
        body is wrapped so the supervisor itself can never die."""
        restart_fails = 0
        probe_fails = 0
        tick = 0
        port = int(self.t.transport.get("port", 1502))
        clock_guard = self._clock_guard
        while not self._stop.is_set():
            try:
                if clock_guard.tick():
                    msg = (f"system clock stepped {clock_guard.last_step_s:+.1f}s — "
                           "freshness unaffected (judged on the monotonic clock)")
                    self.stats.record_event("warn", "clock_step", msg)
                    logger.warning("virtual meter %s: %s", self.t.id, msg)
                newest = self._rebuild_block()
                # legacy: fail-closed per row — _rebuild_block set the gate;
                # one stale row stops the whole meter. Policy modes: freshness
                # was judged per register — newest is the newest FRESH ts, so
                # any fresh row keeps the server up.
                fresh = ((newest > 0) if self.on_stale != "legacy"
                         else self._legacy_all_fresh)
                self._policy_fresh = fresh            # health_state's policy-mode signal
                if fresh:
                    self._last_fresh_ts = newest
                    # a stop() may have landed after the while-check — never
                    # (re)start a server the manager just retired, or it orphans
                    # an unsupervised listener on the port
                    if not self._alive() and not self._stop.is_set():
                        if self._running and not (self._server_thread and self._server_thread.is_alive()):
                            logger.warning("virtual meter %s server thread died — restarting", self.t.id)
                            self.stats.record_event("error", "crash",
                                                    "server thread died — restarting")
                        # after three straight failures (a port already in use,
                        # an interface that does not exist) retry every ~30 s,
                        # and say so on the first failure and every tenth — not
                        # on every tick into the event ring (F-32, 3.83.0)
                        if restart_fails < 3 or tick % 30 == 0:
                            try:
                                self._start_server()
                                if restart_fails:
                                    logger.warning("virtual meter %s: server restarted after %d failed attempts",
                                                   self.t.id, restart_fails)
                                restart_fails = 0
                            except Exception as e:        # noqa: BLE001
                                restart_fails += 1
                                self._running = False
                                if restart_fails == 1 or restart_fails % 10 == 0:
                                    self.stats.record_event("error", "restart_failed",
                                                            f"restart attempt {restart_fails} failed: {e}")
                                    logger.error("virtual meter %s restart failed (%d): %s",
                                                 self.t.id, restart_fails, e)
                    else:
                        self._push_to_ctx()
                        # liveness probe (~every 10s): a thread can be alive but
                        # wedged. If it stops accepting connections for 3 probes
                        # (~30s) while data is fresh, force a restart.
                        tick += 1
                        if tick % 10 == 0:
                            self._apply_keepalive()   # dead-peer reaping (same cadence)
                            self._reap_idle_connections()  # live-peer idle sockets
                            if self._serving_ok(port):
                                probe_fails = 0
                            else:
                                probe_fails += 1
                                if probe_fails >= 3:
                                    logger.warning("virtual meter %s alive but not serving — force-restarting", self.t.id)
                                    self._stop_server("alive but not serving (~30s) — force-restarting")
                                    probe_fails = 0
                else:
                    # Freshness is judged on the MONOTONIC clock now, so a real
                    # stale is real regardless of any wall-clock step — stop
                    # immediately, no grace window (grace could otherwise delay
                    # a genuine fail-safe stop for a source that died during a
                    # step). The clock-step guard remains only for the diagnostic
                    # event it logs above.
                    if self._alive():
                        stale_for = (time.monotonic() - newest) if newest else None
                        reason = (f"source stale >{self.stale_after_s:.0f}s "
                                  f"(last fresh {stale_for:.0f}s ago) — stopped responding"
                                  if stale_for else
                                  f"no fresh source yet — not responding (>{self.stale_after_s:.0f}s)")
                        self._stop_server(reason)     # stale → stop responding
            except Exception as e:  # noqa: BLE001
                self.stats.record_event("error", "supervise", f"supervise loop error: {e}")
                logger.warning("virtual meter %s supervise error: %s", self.t.id, e)
            # back off the poll a little after repeated restart failures (e.g. port
            # held in TIME_WAIT) so we don't hot-loop; normal cadence otherwise.
            self._stop.wait(self.update_interval_s * (3 if restart_fails > 2 else 1))

    def start(self) -> None:
        self._sup_thread = threading.Thread(target=self._supervise, daemon=True,
                                            name=f"vmeter-sup-{self.t.id}")
        self._sup_thread.start()

    def stop(self) -> None:
        self._stop.set()
        # join the supervisor BEFORE tearing the server down, so an in-flight
        # tick can't (re)start a listener after we stop it — that would leave an
        # orphaned, unsupervised server on the port serving a frozen block.
        sup = self._sup_thread
        if sup and sup.is_alive() and sup is not threading.current_thread():
            sup.join(timeout=max(2.0, self.update_interval_s * 4))
        self._stop_server()

    def json_view(self) -> dict:
        """The meter's map as an HTTP-JSON feed, under the SAME staleness
        convention as the Modbus block (the aggregator contract):
          value: null when unavailable — NEVER 0/false for absence;
          quality: good | stale | missing (+ const for fixed rows);
          age_s per row; a stale row carries last_value/last_ts SEPARATELY.
        ``complete`` is False when any live row is not good.
        """
        now = time.monotonic()   # freshness on the MONOTONIC clock (mono stamps)
        values: dict[str, dict] = {}
        stale_fields: list[str] = []
        for reg in self.t.registers:
            key = (reg.source if isinstance(reg.source, str) and reg.source_kind == "live"
                   else f"addr_{reg.addr}" if reg.source_kind in ("sum", "failover")
                   else None)
            if reg.source_kind in ("const", "const_str"):
                values[f"addr_{reg.addr}"] = {"value": reg.source, "quality": "const",
                                              "age_s": None, "addr": reg.addr}
                continue
            if reg.source_kind == "sum":
                total, oldest, missing = 0.0, None, False
                for name in reg.source:
                    v, ts, _b = self._got3(self.provider(name))
                    if v is None:
                        missing = True
                        break
                    try:
                        total += float(v)
                    except (TypeError, ValueError):
                        missing = True                # text value → row missing,
                        break                         # never a 500 on the feed
                    if ts:
                        oldest = min(oldest if oldest is not None else ts, ts)
                val, ts, src_bound = (None if missing else total), oldest, None
            elif reg.source_kind == "failover":
                val, ts, src_bound, _active = self._resolve_failover(reg)
            else:
                val, ts, src_bound = self._got3(self.provider(reg.source) if reg.source else None)
            bound = self._row_bound(reg, src_bound)
            age = round(now - ts, 1) if ts else None
            entry: dict = {"addr": reg.addr}
            if val is not None and self._is_fresh(now, ts, bound):
                entry.update({"value": val, "quality": "good", "age_s": age})
            elif val is not None:
                # ts is MONOTONIC — derive the absolute wall time from the age
                # (wall_now − age) for a human-readable last_ts
                _lts = (datetime.fromtimestamp(time.time() - age).isoformat()
                        if ts else None)
                entry.update({"value": None, "quality": "stale", "age_s": age,
                              "last_value": val, "last_ts": _lts})
                stale_fields.append(key)
            else:
                entry.update({"value": None, "quality": "missing", "age_s": None})
                stale_fields.append(key)
            values[key] = entry
        return {"id": self.t.id, "name": self.t.name,
                "on_stale": self.on_stale,
                "complete": not stale_fields,
                "stale_fields": stale_fields,
                "ts": datetime.now().isoformat(),
                "values": values}

    def preview(self) -> dict:
        """Live engineering values currently feeding this meter (source → value)."""
        out = {}
        for reg in self.t.registers:
            if reg.source_kind == "live" and reg.source:
                got = self.provider(reg.source)
                out[reg.source] = got[0] if got else None
            elif reg.source_kind in ("sum", "failover"):
                for name in reg.source:
                    got = self.provider(name)
                    out[name] = got[0] if got else None
        return out

    def connections(self) -> list[dict]:
        """Active client connections (ip, port, connected_s) — best-effort from
        the pymodbus server's active_connections map. Per-connection uptime is
        tracked here (first-seen registry keyed by ip:port): a consumer that
        flaps shows a small, resetting connected_s — a clear trouble signal.
        Read-only snapshot (cross-thread safe enough for display)."""
        srv = self._server
        now = time.time()
        # _conn_seen is read/written from both the supervisor thread and the
        # API thread (status()); guard it so concurrent calls can't corrupt the
        # dict or raise 'changed size during iteration'.
        with self._conn_lock:
            if not srv:
                self._conn_seen.clear()
                return []
            current: dict[str, tuple] = {}
            try:
                conns = getattr(srv, "active_connections", {}) or {}
                for key, handler in list(conns.items()):
                    peer = None
                    tr = getattr(handler, "transport", None)
                    if tr is not None:
                        try:
                            peer = tr.get_extra_info("peername")
                        except Exception:  # noqa: BLE001
                            peer = None
                    if peer and len(peer) >= 2:
                        if peer[0] in ("127.0.0.1", "::1", "localhost"):
                            continue                   # our own liveness probe — not a real consumer
                        current[f"{peer[0]}:{peer[1]}"] = (peer[0], peer[1])
                    else:
                        current[str(key)] = (str(key), None)
            except Exception:  # noqa: BLE001
                pass
            # update the first-seen registry: drop gone, stamp new
            for ident in [i for i in self._conn_seen if i not in current]:
                del self._conn_seen[ident]
            out: list[dict] = []
            for ident, (ip, port) in current.items():
                seen = self._conn_seen.setdefault(ident, now)
                out.append({"ip": ip, "port": port, "connected_s": int(now - seen)})
            return out

    def health_state(self) -> str:
        """ok · stale · down — the single classification the UI/MQTT/health share.

        Freshness is checked FIRST: when the source is stale the supervisor has
        (correctly) stopped the server, so the meter is NOT alive — but that is a
        `stale` fail-safe, not a `down` fault. Only a meter whose source is fresh
        yet is not serving is genuinely `down` (crashed / failed to start)."""
        if self.on_stale != "legacy":
            # Policy modes: freshness was judged per register (row/source bounds
            # may exceed the instance bound) — trust the supervisor's verdict.
            if not getattr(self, "_policy_fresh", False):
                return "stale"
            return "ok" if self._alive() else "down"
        # Legacy: the supervisor's per-row gate is the verdict; the age check on
        # _last_fresh_ts is a backstop should the supervisor ever stop ticking.
        lf = self._last_fresh_ts
        fresh = (getattr(self, "_policy_fresh", False)
                 and self._is_fresh(time.monotonic(), lf, self.stale_after_s))
        if not fresh:
            return "stale"
        return "ok" if self._alive() else "down"

    def status(self) -> dict:
        now = time.time()
        conns = self.connections()
        lf = self._last_fresh_ts               # MONOTONIC (step-immune freshness)
        # age is a monotonic duration; the absolute last-fresh wall time is
        # derived from it (now_wall − age), since lf itself is not wall time
        _age = round(time.monotonic() - lf, 1) if lf else None
        _last_fresh_wall = (now - _age) if _age is not None else None
        return {"id": self.t.id, "name": self.t.name, "running": self._running,
                "state": self.health_state(),
                "bind": self.t.transport.get("bind", "0.0.0.0"),
                "port": self.t.transport.get("port"),
                "unit_id": self.t.transport.get("unit_id", 1),
                "registers": len(self.t.registers),
                "last_fresh": (datetime.fromtimestamp(_last_fresh_wall).isoformat()
                               if _last_fresh_wall is not None else None),
                "freshness_age_s": _age,
                "uptime_s": int(now - self._started_ts) if self._started_ts else None,
                "connections": conns, "conn_count": len(conns),
                # flat CSV of connected client IPs — lets a monitor (alertd)
                # match a specific consumer with contains() without parsing the
                # connections list (e.g. alert when an expected IP drops, or an
                # unexpected one appears).
                "peers": ",".join(c["ip"] for c in conns),
                "requests": self.stats.total, "req_rate": self.stats.req_rate(),
                "errors": self.stats.errors,
                "bytes_rx": self.stats.bytes_rx, "bytes_tx": self.stats.bytes_tx,
                "last_error": self.stats.last_error(),
                # Composite staleness policy + its bounds + last rebuild's
                # per-register quality (legacy instances report policy='legacy'
                # and all-zero quality). max_hold_s is only meaningful for 'hold'.
                "on_stale": self.on_stale,
                "stale_after_s": self.stale_after_s,
                "max_hold_s": self.max_hold_s,
                "quality": dict(self._quality),
                # Live redundant-source routing: for each failover register, its
                # candidates in priority order and which one is feeding it now —
                # so an operator can see at a glance if the meter is on primary or
                # a fallback. Empty for meters with no failover rows.
                "failover": self.failover_routes()}

    def failover_routes(self) -> list[dict]:
        """Per-failover-register routing: the candidate sources in priority order
        and the one currently live (None until the row first binds)."""
        out = []
        for reg in self.t.registers:
            if reg.source_kind != "failover" or not isinstance(reg.source, list):
                continue
            active = self._failover_active.get(reg.addr)
            out.append({
                "addr": reg.addr, "candidates": list(reg.source), "active": active,
                # on_primary is True only once bound AND on the first candidate;
                # None-active (not yet bound) is reported as not-on-primary.
                "on_primary": bool(active) and active == reg.source[0],
            })
        return out
