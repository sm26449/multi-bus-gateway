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
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import yaml

from .encoder import RegisterEncoder

logger = logging.getLogger(__name__)

# value_provider(source_name) -> (engineering_value, unix_ts) or None if unknown
ValueProvider = Callable[[str], Optional[tuple]]

# Write function codes (single/multiple coil+register, mask, read/write).
_WRITE_FCS = (5, 6, 15, 16, 22, 23)


def _read_only_slave_context():
    """Return a ModbusSlaveContext subclass that refuses writes. Built lazily so
    pymodbus is imported only when a meter actually starts (keeps import light
    and testable)."""
    from pymodbus.datastore import ModbusSlaveContext

    class ReadOnlySlaveContext(ModbusSlaveContext):
        """A meter has no writable data registers. pymodbus calls
        ``context.validate(fc, addr, count)`` before every write, so returning
        False for a write function code yields an ILLEGAL DATA ADDRESS exception
        — the refusal a real meter gives — without ever mutating the block."""

        def __init__(self, *a, on_write_refused=None, **kw):
            super().__init__(*a, **kw)
            self._on_write_refused = on_write_refused

        def validate(self, fc_as_hex, address, count=1):
            if fc_as_hex in _WRITE_FCS:
                if self._on_write_refused:
                    self._on_write_refused(fc_as_hex, address, count)
                return False
            return super().validate(fc_as_hex, address, count)

    return ReadOnlySlaveContext


@dataclass
class RegisterDef:
    addr: int
    type: str
    scale: float = 1.0
    order: str = "big"
    source_kind: str = "const"          # const | const_str | live
    source: Any = 0                      # const value, string, or live source name
    length: int = 1                      # registers, for strings
    note: str = ""                       # human note (round-trips through the editor)
    # Composite (multi-source) staleness: per-row freshness bound override.
    # None → the source device's own bound (provider-supplied), else the
    # instance's stale_after_s. Lets a 60s BLE row coexist with a 250ms row.
    stale_after_s: Optional[float] = None


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
    # bare value → const
    return "const", src if src is not None else 0


def load_template(path: str) -> Template:
    with open(path) as f:
        d = yaml.safe_load(f)["template"]
    regs = []
    default_order = d.get("byte_order", "big")
    for r in d.get("registers", []):
        kind, src = _parse_source(r)
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
        self._policy_fresh = False                                 # supervisor verdict (policy modes)
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

    # ── value resolution + encoding ────────────────────────────────────────
    @staticmethod
    def _got3(got) -> tuple[Any, Optional[float], Optional[float]]:
        """Normalize a provider result to (value, ts, bound) — providers may
        return 2-tuples (legacy) or 3-tuples (bound = source device's own
        staleness threshold)."""
        if not got:
            return None, None, None
        if len(got) >= 3:
            return got[0], got[1], got[2]
        return got[0], got[1], None

    def _span(self, reg: RegisterDef) -> int:
        return max(1, reg.length if reg.type == 'string'
                   else RegisterEncoder.REGISTER_COUNTS.get(reg.type.lower(), 2))

    def _resolve(self, reg: RegisterDef) -> tuple[Optional[list[int]], Optional[float], Optional[float]]:
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
        # live
        value, ts, bound = self._got3(self.provider(reg.source) if reg.source else None)
        if value is None:
            return None, None, None
        return enc.encode(value, reg.type, reg.scale), ts, bound

    def _rebuild_block(self) -> float:
        """Recompute (addr, words) for every register. Returns newest live ts.

        LEGACY mode (default; byte-identical to the pre-composite engine): a
        register with no value leaves a gap — the block keeps its last words —
        and freshness is judged ONCE per instance by the supervisor.

        Policy modes (fail/sentinel/hold) judge freshness PER REGISTER against
        (row bound → source-device bound → instance bound) and apply the
        configured treatment; the returned ts is the newest FRESH one, so the
        supervisor's server-up rule becomes "at least one live source fresh".
        """
        if self.on_stale == "legacy":
            out: list[tuple[int, list[int]]] = []
            newest = 0.0
            for reg in self.t.registers:
                regs, ts, _b = self._resolve(reg)
                if regs is None:
                    continue                          # leave gap (keep last value)
                out.append((reg.addr, [w & 0xffff for w in regs]))
                if ts:
                    newest = max(newest, ts)
            if self.quality_block:
                out.append((QUALITY_BASE, self._quality_words(newest)))
            with self._lock:
                self._regs_out = out
            return newest

        now = time.time()
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
            bound = (reg.stale_after_s if reg.stale_after_s is not None
                     else src_bound if src_bound is not None
                     else self.stale_after_s)
            if words is not None and ts and (now - ts) <= bound:
                out.append((reg.addr, [w & 0xffff for w in words]))
                self._last_good[reg.addr] = ([w & 0xffff for w in words], ts)
                quality["fresh"] += 1
                newest_fresh = max(newest_fresh, ts)
                continue
            quality["stale" if (words is not None or reg.addr in self._last_good) else "missing"] += 1
            if self.on_stale == "hold":
                held = self._last_good.get(reg.addr)
                if held and (now - held[1]) <= self.max_hold_s:
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
        age = (int(min(time.time() - newest_fresh_ts, 0xFFFFFFFE))
               if newest_fresh_ts else 0xFFFFFFFF)
        return [1, state, q["fresh"] & 0xffff, q["stale"] & 0xffff,
                q["missing"] & 0xffff, total & 0xffff,
                (age >> 16) & 0xffff, age & 0xffff]

    # ── server lifecycle (isolated thread + own loop) ─────────────────────
    def _start_server(self) -> None:
        from pymodbus.datastore import (ModbusServerContext, ModbusSlaveContext,
                                         ModbusSequentialDataBlock)
        from pymodbus.server import ModbusTcpServer

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
        # Contiguous block over [base, base+size) so range reads spanning gaps
        # succeed; reads below base raise illegal-address (real-meter behaviour).
        if self.quality_block:
            # two islands: the meter map + the quality block; the void between
            # them still raises illegal-address, preserving real-meter probing
            from pymodbus.datastore import ModbusSparseDataBlock
            _vals = {a: 0 for a in range(self._block_base,
                                         self._block_base + self._block_size)}
            _vals.update({a: 0 for a in range(QUALITY_BASE,
                                              QUALITY_BASE + QUALITY_SPAN)})
            block = ModbusSparseDataBlock(_vals)
        else:
            block = ModbusSequentialDataBlock(self._block_base, [0] * self._block_size)
        # ── always-on instrumentation: every read the consumer issues is
        #    recorded (addr, count, response sample, latency) into stats; an
        #    illegal-address attempt (validate fails) is recorded as an error.
        #    This is the data that let us reverse-engineer consumers — now a
        #    first-class observability feature. Cost: one in-RAM append per read.
        _stats, _dbg, _tid = self.stats, self.debug_reads, self.t.id

        def _note_refused_write(fc, address, count):
            try:
                _stats.record(int(fc), int(address), int(count), None, 0.0,
                              time.time(), err=True)
                logger.warning("vmeter[%s] REFUSED write fc=%s addr=%s count=%s",
                               _tid, fc, address, count)
            except Exception:  # noqa: BLE001
                pass

        _orig_get, _orig_val = block.getValues, block.validate

        def _instrumented_validate(address, count=1, _o=_orig_val):
            ok = _o(address, count)
            if ok and self._unavail_spans:
                # 'fail' staleness policy: a read touching an unavailable
                # (stale/missing-source) register is REFUSED with a Modbus
                # exception instead of serving frozen/invented words — the
                # consumer's own meter-loss fail-safe takes over. Spans are
                # swapped atomically by the supervisor each tick.
                a, c = int(address), int(count)
                for s, e in self._unavail_spans:
                    if a < e and s < a + c:
                        ok = False
                        break
            if not ok:                                   # outside map / unavailable
                try:
                    _stats.record(3, int(address), int(count), None, 0.0, time.time(), err=True)
                    if _dbg:
                        logger.warning("vmeter[%s] ERR refused-read addr=%s count=%s", _tid, address, count)
                except Exception:  # noqa: BLE001
                    pass
            return ok

        def _instrumented_get(address, count=1, _o=_orig_get):
            t0 = time.perf_counter()
            vals = _o(address, count)
            try:
                _stats.record(3, int(address), int(count), vals,
                              (time.perf_counter() - t0) * 1e6, time.time())
                if _dbg:
                    logger.warning("vmeter[%s] READ addr=%s count=%s", _tid, address, count)
            except Exception:  # noqa: BLE001
                pass
            return vals
        block.validate = _instrumented_validate
        block.getValues = _instrumented_get
        # serve the same data on holding + input registers (consumers vary by FC).
        # READ-ONLY: writes are refused (a real meter has no writable data
        # registers) so a consumer cannot inject values into the block — pushed
        # registers would be overwritten within update_interval_s, but pads/gaps
        # would keep an injected value forever (a poisoned grid reading straight
        # into the ESS loop).
        slave = _read_only_slave_context()(hr=block, ir=block, zero_mode=True,
                                           on_write_refused=_note_refused_write)
        # single=True → respond on ANY unit id (a client may poll unit 240 etc.).
        # With single=True pymodbus wants ONE slave context, not a dict.
        ctx = ModbusServerContext(slaves=slave, single=True)
        self._ctx, self._block = ctx, block
        self._push_to_ctx()                          # seed before first request

        def _run():
            loop = asyncio.new_event_loop()
            self._server_loop = loop
            asyncio.set_event_loop(loop)

            async def _serve():
                # ModbusTcpServer.__init__ needs a RUNNING loop → build it here.
                self._server = ModbusTcpServer(ctx, address=(host, port))
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
        """Write each register's words into the datastore — atomic per value."""
        if not getattr(self, "_block", None):
            return
        with self._lock:
            out = list(self._regs_out)
        for addr, words in out:                      # zero_mode → address == index
            self._block.setValues(addr, words)       # one call per value = no word-tearing

    # ── supervisor ─────────────────────────────────────────────────────────
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
        while not self._stop.is_set():
            try:
                newest = self._rebuild_block()
                # legacy: one instance-level freshness judgement. Policy modes:
                # _rebuild_block already judged per register — newest is the
                # newest FRESH ts, so any fresh row keeps the server up.
                fresh = ((newest > 0) if self.on_stale != "legacy"
                         else (newest > 0) and (time.time() - newest <= self.stale_after_s))
                self._policy_fresh = fresh            # health_state's policy-mode signal
                if fresh:
                    self._last_fresh_ts = newest
                    if not self._alive():             # down OR crashed → (re)start
                        if self._running and not (self._server_thread and self._server_thread.is_alive()):
                            logger.warning("virtual meter %s server thread died — restarting", self.t.id)
                            self.stats.record_event("error", "crash",
                                                    "server thread died — restarting")
                        try:
                            self._start_server()
                            restart_fails = 0
                        except Exception as e:        # noqa: BLE001
                            restart_fails += 1
                            self._running = False
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
                            if self._serving_ok(port):
                                probe_fails = 0
                            else:
                                probe_fails += 1
                                if probe_fails >= 3:
                                    logger.warning("virtual meter %s alive but not serving — force-restarting", self.t.id)
                                    self._stop_server("alive but not serving (~30s) — force-restarting")
                                    probe_fails = 0
                else:
                    if self._alive():
                        stale_for = (time.time() - newest) if newest else None
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
        self._stop_server()

    def json_view(self) -> dict:
        """The meter's map as an HTTP-JSON feed, under the SAME staleness
        convention as the Modbus block (the aggregator contract):
          value: null when unavailable — NEVER 0/false for absence;
          quality: good | stale | missing (+ const for fixed rows);
          age_s per row; a stale row carries last_value/last_ts SEPARATELY.
        ``complete`` is False when any live row is not good.
        """
        now = time.time()
        values: dict[str, dict] = {}
        stale_fields: list[str] = []
        for reg in self.t.registers:
            key = (reg.source if isinstance(reg.source, str) and reg.source_kind == "live"
                   else f"addr_{reg.addr}" if reg.source_kind == "sum"
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
                    total += float(v)
                    if ts:
                        oldest = min(oldest if oldest is not None else ts, ts)
                val, ts, src_bound = (None if missing else total), oldest, None
            else:
                val, ts, src_bound = self._got3(self.provider(reg.source) if reg.source else None)
            bound = (reg.stale_after_s if reg.stale_after_s is not None
                     else src_bound if src_bound is not None
                     else self.stale_after_s)
            age = round(now - ts, 1) if ts else None
            entry: dict = {"addr": reg.addr}
            if val is not None and ts and (now - ts) <= bound:
                entry.update({"value": val, "quality": "good", "age_s": age})
            elif val is not None:
                entry.update({"value": None, "quality": "stale", "age_s": age,
                              "last_value": val,
                              "last_ts": datetime.fromtimestamp(ts).isoformat() if ts else None})
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
        lf = self._last_fresh_ts
        fresh = bool(lf) and (time.time() - lf) <= self.stale_after_s
        if not fresh:
            return "stale"
        return "ok" if self._alive() else "down"

    def status(self) -> dict:
        now = time.time()
        conns = self.connections()
        lf = self._last_fresh_ts
        return {"id": self.t.id, "name": self.t.name, "running": self._running,
                "state": self.health_state(),
                "bind": self.t.transport.get("bind", "0.0.0.0"),
                "port": self.t.transport.get("port"),
                "unit_id": self.t.transport.get("unit_id", 1),
                "registers": len(self.t.registers),
                "last_fresh": datetime.fromtimestamp(lf).isoformat() if lf else None,
                "freshness_age_s": round(now - lf, 1) if lf else None,
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
                # Composite staleness policy + last rebuild's per-register quality
                # (legacy instances report policy='legacy' and all-zero quality).
                "on_stale": self.on_stale,
                "quality": dict(self._quality)}
