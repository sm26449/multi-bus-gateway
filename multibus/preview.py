# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The read preview: what a device would read, before it is saved
(ADR-0031 §2 Check, §5).

A preview is a background job that reads a connection once with the
template's map and answers the decoded values, their plausibility, the
identity the device reports and the load it would add. It is a READ, and
only a read:

- **A guard transport.** Every Modbus request goes through
  :class:`GuardedModbusClient`, which refuses any function code but 3 and 4
  (FC43/14, the read device identification, only when the template's
  ``identify`` asks for it), any request without an answer, any read longer
  than the template allows, and any request past the budget. No setup
  write, no keepalive write: nothing else is ever built. Seplos V2 asks only
  through ``seplos_v2.request`` (its read commands, ADR-0014). A tap, a
  Seplos listener and CAN only receive.
- **The bus claim (ADR-0025).** A poll on a bus a device of this gateway
  only listens to is refused, whatever name the bus is reached by. A
  listen preview joins the bus's existing reader (or opens a receive-only
  one, reference counted) and transmits nothing.
- **One socket.** When a device of this gateway polls the bus, the preview
  rides its transport — the same socket, lock and turn (the endpoint's
  ``endpoint_min_gap_s`` kept). When none does and our bridge reports a
  client on that port, the preview is refused and the client named: a
  single-client bridge drops the old client for a new one.
- **A visible budget** (``preview: {max_requests, listen_s}``, ADR-0031
  §5): at most ``max_requests`` requests, each within the template's
  ``max_registers_per_read``; listening at most ``listen_s`` (and 15 s).
  One preview per bus at a time.

MQTT is a subscription; the template's broker keepalive is sent once, as
the running device would send it, and the answer says so. The component
discover topics are read in the same window (component_discover.py).
"""
from __future__ import annotations

import copy
import logging
import math
import re
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from pymodbus.client.mixin import ModbusClientMixin

from .register_parser import RegisterParser
from .value_decode import apply_corrections, is_sentinel_value, is_textual

logger = logging.getLogger(__name__)

MAX_SECONDS = 15                 # the API's bound on one listening window
JOB_TTL_S = 600                  # a finished answer is kept this long
MAX_JOBS = 4                     # previews running at once, over every bus (429 beyond)
RETRY_AFTER_S = 5
YIELD_S = 0.05                   # the line handed back between two turns on a shared bus
DEADLINE_CAP_S = 120.0           # no preview job runs longer than this
POLL_SLACK_S = 5.0               # a poll's deadline: its requests' time, plus this
WALL_MARGIN_S = 15.0             # the job's wall time: its deadline, plus this
PREVIEW_TRACE = "preview"        # what the bus monitor calls the preview's traffic


def poll_deadline_s(max_requests: int, timeout: float, gap: float = 0.0) -> float:
    """How long a polling preview may ask: each request's timeout, gap and
    hand-back, plus a little; never past DEADLINE_CAP_S."""
    return min(DEADLINE_CAP_S, max(1, int(max_requests)) * (float(timeout) + float(gap) + YIELD_S) + POLL_SLACK_S)
MODBUS_MAX_READ = 125            # FC3/FC4's own limit
QUALITIES = ('ok', 'no_answer', 'illegal_address', 'out_of_range', 'byte_swap_suspect', 'sentinel')
# where a unit alone tells a reading from nonsense (a declared min/max wins)
UNIT_BOUNDS = {'°C': (-60.0, 250.0), '°F': (-76.0, 482.0), 'K': (0.0, 600.0), 'Hz': (0.0, 1000.0)}
ABSURD = 1e9                     # |v| past this: a wrong type, scale or byte order
BITS_PER_BYTE = 11               # start + 8 data + parity/stop + stop (the worst line)
SEPLOS_FRAME_BYTES = 18 + 180    # a request + a typical answer, in ASCII characters


class Refused(Exception):
    """The preview is not started: ``status`` (409/422) and the API's detail."""

    def __init__(self, status: int, detail: Any, headers: Optional[Dict[str, str]] = None):
        super().__init__(str(detail))
        self.status, self.detail, self.headers = status, detail, headers


def refuse(status: int, *errors: str, **extra) -> Refused:
    return Refused(status, {"errors": list(errors), **extra})


class GuardRefusal(Exception):
    """A request the preview may not send (never reaches the wire)."""


class BudgetSpent(GuardRefusal):
    """The budget's last request was already sent."""


# ── the budget and the guard transport ──────────────────────────────────────

class Budget:
    """Requests and registers sent, against ``max_requests``."""

    def __init__(self, max_requests: int):
        self.max_requests = max(0, int(max_requests))
        self.requests = 0
        self.registers = 0
        self._t0 = time.monotonic()

    def left(self) -> int:
        return self.max_requests - self.requests

    def take(self, registers: int = 0) -> None:
        if self.requests >= self.max_requests:
            raise BudgetSpent(f"the preview's budget of {self.max_requests} request(s) is spent")
        self.requests += 1
        self.registers += int(registers or 0)

    def view(self) -> Dict[str, Any]:
        return {'requests': self.requests, 'registers': self.registers, 'max_requests': self.max_requests,
                'ms': int(round((time.monotonic() - self._t0) * 1000))}


class GuardedModbusClient(ModbusClientMixin):
    """A pymodbus client that can only read. Every request pymodbus builds
    comes through :meth:`execute`; one with a function code other than 3/4
    (FC43/14 when ``allow_fc43``), one that expects no answer, one longer
    than ``max_per_read`` registers or one past the budget is refused before
    the wire. ``inner`` is the connected client of the turn being taken."""

    READS = frozenset({3, 4})

    def __init__(self, inner, budget: Budget, max_per_read: int = MODBUS_MAX_READ,
                 allow_fc43: bool = False):
        ModbusClientMixin.__init__(self)
        self.inner = inner
        self.budget = budget
        self.max_per_read = max(1, min(MODBUS_MAX_READ, int(max_per_read or MODBUS_MAX_READ)))
        self.allow_fc43 = bool(allow_fc43)
        self.sent: List[Tuple[int, Optional[int], int]] = []

    def execute(self, no_response_expected, request):  # noqa: D401 — pymodbus' own entry point
        fc = int(getattr(request, 'function_code', -1) or -1)
        sub = getattr(request, 'sub_function_code', None)
        if not (fc in self.READS or (fc == 43 and sub == 14 and self.allow_fc43)):
            raise GuardRefusal(f"function code {fc} refused: the preview only reads (FC3/FC4)")
        if no_response_expected:
            raise GuardRefusal("a request that expects no answer is not a read")
        count = int(getattr(request, 'count', 0) or 0)
        if fc in self.READS and not 1 <= count <= self.max_per_read:
            raise GuardRefusal(f"a read of {count} registers: 1..{self.max_per_read} per request")
        if self.inner is None:
            raise GuardRefusal("no turn on the bus")
        self.budget.take(count)
        self.sent.append((fc, getattr(request, 'address', None), count))
        return self.inner.execute(False, request)


# ── decoding and plausibility ───────────────────────────────────────────────

def _bounds(r) -> Tuple[Optional[float], Optional[float]]:
    lo, hi = getattr(r, 'min', None), getattr(r, 'max', None)
    if lo is None and hi is None:
        return UNIT_BOUNDS.get(str(getattr(r, 'unit', '') or '').strip(), (None, None))
    return lo, hi


def _absurd(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and (
        (isinstance(v, float) and not math.isfinite(v)) or abs(v) > ABSURD
        or (isinstance(v, float) and v != 0 and abs(v) < 1e-9))


def _swapped_plausible(words: List[int], data_type: str, byte_order: str) -> bool:
    """The same words in the other word order decode to a sane number."""
    if len(words) < 2:
        return False
    other = 'little' if str(byte_order or 'big').lower() in ('big', 'badc') else 'big'
    try:
        alt = RegisterParser(other).parse_value(list(words), data_type)
    except Exception:  # noqa: BLE001
        return False
    return isinstance(alt, (int, float)) and not _absurd(alt) and alt != 0


def quality_of(r, value, *, raw=None, words=None, byte_order: str = 'big') -> str:
    """The plausibility of a decoded value (after scale, offset, enum)."""
    if isinstance(value, str) or is_textual(r):
        return 'ok'
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 'ok'
    if isinstance(value, float) and not math.isfinite(value):
        return 'byte_swap_suspect'
    dt = str(getattr(r, 'data_type', '') or '').lower()
    if words and len(words) >= 2 and dt in ('float', 'float32', 'int32', 'uint32', 'sm32') \
            and _absurd(raw if raw is not None else value) and _swapped_plausible(words, dt, byte_order):
        return 'byte_swap_suspect'
    if abs(value) > ABSURD:
        return 'out_of_range'
    lo, hi = _bounds(r)
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        return 'out_of_range'
    return 'ok'


def judge(r, raw, *, siblings=None, words=None, byte_order: str = 'big') -> Tuple[Any, str]:
    """A raw value (before corrections) → (value, quality)."""
    if raw is None:
        return None, 'no_answer'
    if isinstance(raw, float) and not math.isfinite(raw):
        return None, 'byte_swap_suspect'
    nan = getattr(r, 'nan', None)
    if nan is not None and is_sentinel_value(raw, getattr(r, 'data_type', ''), nan):
        return None, 'sentinel'
    try:
        val = apply_corrections(raw, r, siblings=siblings)
    except Exception:  # noqa: BLE001 — a decode failure is no value, never a crash
        val = None
    if val is None:
        return None, 'no_answer'
    return val, quality_of(r, val, raw=raw, words=words, byte_order=byte_order)


def decode_row(r, words: Optional[List[Optional[int]]], byte_order: str = 'big', *,
               error: str = '', siblings=None) -> Tuple[Any, str]:
    """A register row from its words → (value, quality). ``error`` is what the
    window's read answered when it gave no words."""
    if error == 'illegal_address':
        return None, 'illegal_address'
    if not words or any(w is None for w in words):
        return None, 'no_answer'
    p = RegisterParser(byte_order)
    dt = getattr(r, 'data_type', '') or 'uint16'
    try:
        raw = p.parse_value(list(words), dt)
    except Exception:  # noqa: BLE001
        raw = None
    if raw is None:
        # the parser drops a NaN / ±Inf float: a value the words cannot be in
        # this order — the design's "check the model or the byte order"
        if str(dt).lower() in ('float', 'float32', 'double') and getattr(r, 'nan', None) is None:
            return None, 'byte_swap_suspect'
        return None, 'no_answer'
    nan = getattr(r, 'nan', None)
    if nan is not None:
        try:
            if p.parse_value(list(words), dt, nan=nan) is None:
                return None, 'sentinel'
        except Exception:  # noqa: BLE001
            pass
    return judge(r, raw, siblings=siblings, words=list(words), byte_order=byte_order)


def _table(r) -> str:
    from .config import normalize_register_type
    return normalize_register_type(getattr(r, 'register_type', '') or 'holding')


def decode_cache(rows, cache: Dict[Tuple[str, int], int], errors: Dict[Tuple[str, int], str],
                 byte_order: str) -> Dict[str, Tuple[Any, str]]:
    """Every row of a register map against the words heard or read:
    {name: (value, quality)}. One-word rows are the siblings a scale_from
    reads (SunSpec)."""
    parser = RegisterParser(byte_order)
    siblings: Dict[str, Any] = {}
    spans = []
    for r in rows:
        t = _table(r)
        n = parser.get_register_count(getattr(r, 'data_type', '') or 'uint16')
        ws = [cache.get((t, a)) for a in range(r.address, r.address + n)]
        spans.append((r, t, n, ws))
        if n == 1 and ws[0] is not None:
            siblings[r.name] = ws[0]
    out = {}
    for r, t, n, ws in spans:
        err = next((errors[(t, a)] for a in range(r.address, r.address + n) if (t, a) in errors), '')
        out[r.name] = decode_row(r, None if any(w is None for w in ws) else ws, byte_order,
                                 error=err, siblings=siblings)
    return out


# ── the map: windows, groups, identity ──────────────────────────────────────

@dataclass
class Window:
    table: str
    start: int
    end: int
    group: str
    interval: float
    rows: List[Any] = field(default_factory=list)

    @property
    def count(self) -> int:
        return self.end - self.start


def group_intervals(rows, *groups) -> Dict[str, float]:
    """{group: interval s} from the groups given (later wins), for the rows' groups."""
    out: Dict[str, float] = {}
    for g in groups:
        for name, v in (g or {}).items():
            iv = v.get('interval') if isinstance(v, dict) else getattr(v, 'interval', v)
            try:
                out[name] = max(0.05, float(iv))
            except (TypeError, ValueError):
                continue
    for r in rows:
        out.setdefault(getattr(r, 'poll_group', '') or 'normal', out.get('normal', 5.0))
    return out


def plan_windows(rows, intervals: Dict[str, float], max_read: int, max_gap: int = 10,
                 setup_ranges=()) -> Tuple[List[Window], List[Tuple[Any, str]]]:
    """The reads a poll would make, per poll group (fastest first), each
    within ``max_read`` registers — and the rows a preview cannot read
    (coils, a register a setup write provides, one longer than a read)."""
    parser = RegisterParser('big')
    skipped: List[Tuple[Any, str]] = []
    by_group: Dict[str, List[Any]] = {}
    for r in rows:
        g = getattr(r, 'poll_group', '') or 'normal'
        by_group.setdefault(g if g in intervals else 'normal', []).append(r)
    out: List[Window] = []
    for g in sorted(by_group, key=lambda x: (intervals.get(x, 5.0), x)):
        cur: Optional[Window] = None
        for r in sorted(by_group[g], key=lambda x: (_table(x), x.address)):
            t = _table(r)
            n = parser.get_register_count(getattr(r, 'data_type', '') or 'uint16')
            if t not in ('holding', 'input'):
                skipped.append((r, "a coil or discrete input: the preview reads registers only (FC3/FC4)"))
                continue
            if any(lo <= r.address + n - 1 and r.address <= hi for lo, hi in setup_ranges):
                skipped.append((r, "exists only after the template's setup write, which a preview never sends"))
                continue
            if n > max_read:
                skipped.append((r, f"{n} registers: longer than one read ({max_read})"))
                continue
            if (cur is not None and cur.table == t and r.address <= cur.end + max(0, max_gap)
                    and max(cur.end, r.address + n) - cur.start <= max_read):
                cur.end = max(cur.end, r.address + n)
                cur.rows.append(r)
                continue
            if cur is not None:
                out.append(cur)
            cur = Window(t, r.address, r.address + n, g, intervals.get(g, 5.0), [r])
        if cur is not None:
            out.append(cur)
    return out, skipped


SUNS = (0x5375, 0x6E53)
SUNSPEC_BASE, SUNSPEC_SPAN = 40000, 70


def _text(words: List[Optional[int]]) -> str:
    b = b''.join(int(w).to_bytes(2, 'big') for w in words if w is not None)
    return b.split(b'\0', 1)[0].decode('ascii', 'replace').strip()


def sunspec_identity(cache: Dict[Tuple[str, int], int], base: int = SUNSPEC_BASE) -> Optional[Dict[str, str]]:
    """The SunSpec common model (1) after the 'SunS' marker: manufacturer,
    model, version, serial — or None when there is no marker."""
    g = lambda a: cache.get(('holding', base + a))   # noqa: E731
    if (g(0), g(1)) != SUNS:
        return None

    def s(off, n):
        return _text([g(off + i) for i in range(n)])
    return {'manufacturer': s(4, 16), 'model': s(20, 16), 'version': s(44, 8), 'serial': s(52, 16)}


def fingerprint(tpl, cache, errors) -> str:
    """The template's own ``identify`` registers against what was read:
    match | mismatch | unknown (no answer) | none (nothing to check)."""
    from .bus_scan import _check, _decode
    rules = list(((getattr(tpl, 'identify', None) or {}).get('registers')) or [])
    if not rules:
        return 'none'
    bo = (getattr(tpl, 'protocol', None) or {}).get('byte_order', 'big')
    unknown = False
    for rule in rules:
        dt = rule.get('data_type', 'uint16')
        n = 1 if dt in ('uint16', 'int16', 'short') else 2
        t = rule.get('register_type', 'holding')
        addrs = range(int(rule['address']), int(rule['address']) + n)
        if any(errors.get((t, a)) == 'illegal_address' for a in addrs):
            return 'mismatch'                     # the device says it has no such register
        words = [cache.get((t, a)) for a in addrs]
        if any(w is None for w in words):
            unknown = True
            continue
        if not _check(_decode(words, dt, bo), rule):
            return 'mismatch'
    return 'unknown' if unknown else 'match'


def identify_windows(tpl, max_read: int) -> List[Window]:
    """The reads a template's fingerprint needs, merged where they fit."""
    out: List[Window] = []
    for rule in ((getattr(tpl, 'identify', None) or {}).get('registers')) or []:
        dt = rule.get('data_type', 'uint16')
        n = 1 if dt in ('uint16', 'int16', 'short') else 2
        t = rule.get('register_type', 'holding')
        a = int(rule['address'])
        w = next((w for w in out if w.table == t and max(w.end, a + n) - min(w.start, a) <= max_read), None)
        if w is None:
            out.append(Window(t, a, a + n, 'identify', 0.0))
        else:
            w.start, w.end = min(w.start, a), max(w.end, a + n)
    return out


# ── the plan ────────────────────────────────────────────────────────────────

@dataclass
class Plan:
    kind: str                          # modbus | seplos | tap | seplos_listen | can | http | mqtt
    mode: str                          # poll | listen
    transport: str                     # shared | own | tap
    key: str                           # the bus: one preview at a time on it
    where: str
    template: Any
    rows: List[Any]
    intervals: Dict[str, float]
    conn: Dict[str, Any]
    dev: Any
    src: Any
    seconds: float
    max_requests: int
    listen_s: int
    shared_with: str = ''
    holder_mc: Any = None              # the holder's own running ModbusConnection (M2)
    shared_line: Dict[str, Any] = field(default_factory=dict)
    confirm_open: bool = False
    link: Any = None
    warnings: List[str] = field(default_factory=list)
    share_now: Optional[float] = None
    device_id: str = ''
    unit: Any = None
    byte_order: str = 'big'
    max_read: int = MODBUS_MAX_READ
    line: Dict[str, Any] = field(default_factory=dict)
    wall: Optional[float] = None       # the job's wall time when its kind sets it (a bus scan)
    extra: Dict[str, Any] = field(default_factory=dict)   # what a kind of its own carries (bus_scan.py)

    def head(self) -> Dict[str, Any]:
        return {'mode': self.mode, 'transport': self.transport, 'where': self.where,
                'template': getattr(self.template, 'id', ''), 'unit': self.unit,
                'shared_with': self.shared_with, 'seconds': self.seconds,
                **({'shared_line': dict(self.shared_line)} if self.shared_line else {}),
                **({'confirm_open': True} if self.confirm_open else {}),
                **({'device': self.device_id} if self.device_id else {})}


@dataclass
class Env:
    """What the routes hand the preview: the configuration and the running
    gateway's state, read at each call."""
    config: Any
    templates: Any
    diag: Any = None
    validate: Optional[Callable[[Dict[str, Any], str], None]] = None
    share_of: Optional[Callable[[str], Optional[float]]] = None
    running: Optional[Callable[[str], Any]] = None     # device id → its running client
    armed: Optional[Callable[[], Dict[str, Any]]] = None   # {rule id: device ids} of the armed rules


def _seconds(raw, listen_s: int) -> float:
    cap = float(min(int(listen_s), MAX_SECONDS))
    if raw is None or raw == '':
        return cap
    try:
        s = float(raw)
    except (TypeError, ValueError):
        s = float('nan')
    if isinstance(raw, bool) or not math.isfinite(s) or not 1 <= s <= MAX_SECONDS:
        raise refuse(422, f"seconds: 1..{MAX_SECONDS} (how long a preview may listen)")
    return min(s, cap)


def template_rows(config, tpl):
    """The rows a new device of this template would start with, as its
    selection stores them (device_seed.seeded_rows / seed_row)."""
    from .config import Config
    from .device_seed import seed_row, seeded_rows
    return Config._parse_selected_payload({'registers': [seed_row(r) for r in seeded_rows(tpl)]})


def _lan(host: str, env: Env, field_: str) -> None:
    from .discovery import lan_host_error
    e = lan_host_error(host, bool(getattr(env.config.security, 'allow_nonlan_http_devices', False)))
    if e:
        raise refuse(422, f"{field_}: {e}")


def _device_sources(dev) -> List[Tuple[str, Any]]:
    from .bus_claims import _sources
    return [(str(p or '').lower(), c) for _i, p, c, _t in _sources(dev)]


def _holder(env: Env, protocol: str, bus: str, *, mode_of=None) -> Tuple[Optional[Any], Optional[Any], List[str]]:
    """(device, its connection) of an enabled device of this gateway that
    POLLS ``bus`` with ``protocol`` — the transport a preview rides — and the
    ids of the devices reading that bus in another protocol."""
    from .bus_claims import bus_of, source_claims
    holder = holder_conn = None
    other: List[str] = []
    for d in env.config.devices:
        if not getattr(d, 'enabled', False):
            continue
        claims = {b: lis for _i, b, lis, _w in source_claims(d, env.templates, standby=False)}
        for p, c in _device_sources(d):
            if bus_of(p, c) != bus:
                continue
            if p != protocol:
                if p != 'rtu_tap' and d.id not in other:
                    other.append(d.id)
                continue
            if claims.get(bus):
                continue                           # it listens: refused before
            if mode_of is not None and mode_of(c) != 'poll':
                continue
            if holder is None:
                holder, holder_conn = d, c
    return holder, holder_conn, other


def _bus_entry(env: Env, bus: str, strict: bool = False) -> Optional[Dict[str, Any]]:
    """The bus's entry of /api/buses, or None (not a bus anyone uses).
    ``strict``: a list that cannot be read raises instead of reading as
    "nobody here" (a scan fails closed on it)."""
    from . import buses
    try:
        for b in buses.buses(env.config, env.templates, share_of=env.share_of, diag=env.diag):
            if b.get('id') == bus:
                return b
    except Exception:  # noqa: BLE001 — the load and the bridge's word only add to an answer
        if strict:
            raise
        logger.debug("preview: bus list not read", exc_info=True)
    return None


def _share(entry: Optional[Dict[str, Any]]) -> Optional[float]:
    load = (entry or {}).get('load') or {}
    return None if load.get('pct') is None else round(load['pct'] / 100.0, 3)


def _any_listener(env: Env, bus: str) -> List[str]:
    """Every device that listens on ``bus`` — enabled or not (the operator's
    rule for a preview, L3.8-F5): a tap switched off still says another
    master owns that bus. On a standby the enabled polled RTU sources count
    too (bus_claims.listeners)."""
    from .bus_claims import listeners, source_claims
    out = list(listeners(env.config, bus, env.templates))
    for d in env.config.devices:
        if d.id in out:
            continue
        if any(b == bus and lis for _i, b, lis, _w in source_claims(d, env.templates, standby=False)):
            out.append(d.id)
    return out


def _running_connection(client, bus: str):
    """The holder's own running ModbusConnection on ``bus`` (inside its
    client: a source's driver, or the client itself), or None."""
    from .bus_claims import bus_of
    from .modbus_client import ModbusConnection
    cands = [drv for _s, drv in (getattr(client, 'parts', None) or [])] + [client]
    for d in cands:
        mc = getattr(d, 'connection', None)
        if isinstance(mc, ModbusConnection) and not getattr(mc, '_retired', False) \
                and bus_of(getattr(mc.config, 'protocol', 'tcp'), mc.config) == bus:
            return mc
    return None


STALE_DIAG_S = 3 * 30.0             # three of the bridge poller's rounds (bridge_diag.POLL_S)


def _declared_bridge(env: Env, host: str, port: int) -> Optional[Dict[str, Any]]:
    """The bridge record that declares ``host:port`` (host in any case)."""
    h, p = str(host or '').strip().lower(), int(port or 0)
    return next((x for x in (getattr(env.config, 'bridges', None) or [])
                 if str(x.get('host', '') or '').strip().lower() == h
                 and any(int((q or {}).get('port') or 0) == p for q in (x.get('ports') or []))), None)


def foreign_client(env: Env, host: str, port: int) -> Tuple[str, str]:
    """Is another program connected to ``host:port``? ('present', its
    address) | ('absent', '') | ('unknown', why). Only our own bridge says
    which client it serves, and only a fresh answer counts."""
    from .bridges import TYPES
    p = int(port or 0)
    b = _declared_bridge(env, host, p)
    if b is None:
        return 'unknown', f"{host}:{port} is not a bridge this gateway knows, so whether another program is " \
                          f"connected to it cannot be asked"
    name = b.get('name') or b.get('id')
    if (TYPES.get(str(b.get('type') or ''), {}) or {}).get('discovery') != 'api':
        return 'unknown', f"bridge '{name}' ({b.get('type')}) cannot say which client it serves"
    if env.diag is None:
        return 'unknown', f"bridge '{name}' was not asked: its diagnostics are not running"
    try:
        v = env.diag.port_view(b.get('id'), p)
    except Exception:  # noqa: BLE001
        return 'unknown', f"bridge '{name}': its diagnostics could not be read"
    st = (v or {}).get('stats') if isinstance(v, dict) else None
    if not isinstance(v, dict) or not isinstance(st, dict):
        return 'unknown', f"bridge '{name}' was not asked yet about port {p}"
    age = time.time() - float(v.get('sampled_ts') or 0)
    if age > STALE_DIAG_S:
        return 'unknown', f"bridge '{name}''s last answer about port {p} is {int(age)} s old"
    c = st.get('client')
    addr = (c or {}).get('address') if isinstance(c, dict) else (c if isinstance(c, str) else None)
    if addr:
        return 'present', str(addr)
    return 'absent', ''


def second_connection(env: Env, host: str, port: int, confirm: bool = False) -> Optional[Dict[str, Any]]:
    """Why a probe of its own (a SunSpec walk, a LAN sweep) must not open a
    connection to ``host:port``, or None: a device of this gateway polls it
    (its socket is the only one we keep there — read through it instead), or
    it is a bridge port of ours with a client that is not us / that nobody
    can name (unless ``confirm``)."""
    from .bus_claims import bus_of, pollers
    bus = bus_of('tcp', {'host': host, 'port': port})
    ids = pollers(env.config, bus, env.templates)
    if ids:
        return {'errors': [f"{host}:{port} is read by {', '.join(ids)} — a connection of a probe's own beside it "
                           f"could take its place on a single-client bridge or mix its answers. Nothing was sent; "
                           f"use Scan this bus (it asks through {ids[0]}'s connection)"], 'pollers': ids, 'bus': bus}
    if _declared_bridge(env, host, port) is None:
        return None
    state, said = foreign_client(env, host, port)
    if state == 'present':
        return {'errors': [f"{host}:{port} is held by another program ({said}): the bridge serves one client. "
                           f"Nothing was sent"], 'client': said, 'bus': bus}
    if state == 'unknown' and not confirm:
        return {'errors': [f"{host}:{port}: {said} — opening a connection there may disconnect another master. "
                           f"Nothing was sent; send confirm_open: true to open it anyway"],
                'client': 'unknown', 'unknown': said, 'bus': bus}
    return None


def _check_open(env: Env, plan: Plan, host: str, port: int, bus: str, confirm: bool) -> None:
    """Before a connection of the preview's own to ``host:port`` (M3): refused
    when another program holds it; when nobody can say, refused unless the
    request confirms it (``confirm_open: true``, audited)."""
    state, said = foreign_client(env, host, port)
    if state == 'present':
        raise refuse(409, f"{plan.where} is held by another program ({said}): the bridge serves one client, "
                          f"and a second connection would drop it. Nothing was sent — disconnect that client, "
                          f"or add the device and read through it", bus=bus, client=said)
    if state == 'unknown':
        if not confirm:
            raise refuse(409, f"{plan.where}: {said} — opening a connection there may disconnect another master "
                              f"(a single-client bridge drops its old client for a new one). Nothing was sent; "
                              f"send confirm_open: true to open it anyway", bus=bus, client='unknown',
                         unknown=said)
        plan.confirm_open = True
        plan.warnings.append(f"opened {plan.where} although {said} (confirm_open)")


def prepare(env: Env, conn: Dict[str, Any], template_id: str, *, seconds=None, unit=None,
            rows=None, groups=None, device_id: str = '', confirm_open: bool = False,
            edited=None) -> Plan:
    """Judge a preview before anything is sent: the template, the mode, the
    bus claim (409), the connection as a save judges it (422), the transport
    (a running device's, our bridge's client), CAN's listen-only gate.
    Raises :class:`Refused`; returns the plan a job runs."""
    from .bus_claims import bus_of, refusal_detail, where_of
    from .config import (BUS_OVER_TCP_PROTOCOLS, CAN_PROTOCOLS, MODBUS_PROTOCOLS, SERIAL_PROTOCOLS,
                         VIRTUAL_MAP_PROTOCOLS)
    from .device_template import template_listens, template_listens_only
    cfg = env.config
    budget = cfg.preview_budget()
    tid = str(template_id or '').strip()
    tpl = env.templates.get(tid) if tid else None
    if tpl is None:
        raise refuse(422, f"template: '{tid}' not found" if tid else "template: required (the map a preview decodes with)")
    if not isinstance(conn, dict) or not conn:
        raise refuse(422, "connection: required")
    conn = dict(conn)
    if unit is not None:
        try:
            conn['unit_id'] = int(unit)
        except (TypeError, ValueError):
            raise refuse(422, "unit: a whole number (the slave / pack address)")
    said = dict(conn)                                  # as sent: what a save would judge
    if conn.get('bridge'):
        from .bridges import resolve_connection
        conn = resolve_connection(conn, cfg.bridges)
    proto = str(conn.get('protocol', 'tcp') or 'tcp').lower()
    if proto == 'none':
        raise refuse(422, "connection.protocol: a device measured by its meters reads nothing to preview")
    secs = _seconds(seconds, budget['listen_s'])
    tproto = getattr(tpl, 'protocol', None) or {}

    # what the preview does on that connection
    if proto in CAN_PROTOCOLS or proto == 'mqtt':
        kind, mode = ('can' if proto in CAN_PROTOCOLS else 'mqtt'), 'listen'
    elif proto == 'http':
        kind, mode = 'http', 'poll'
    elif proto in VIRTUAL_MAP_PROTOCOLS:
        vmode = str(conn.get('mode') or '').lower()
        if template_listens_only(tpl):
            vmode = 'listen'
        if vmode not in ('poll', 'listen'):
            raise refuse(422, "connection.mode: choose poll or listen before a preview")
        conn['mode'] = vmode
        kind, mode = ('seplos_listen', 'listen') if vmode == 'listen' else ('seplos', 'poll')
    elif proto == 'rtu_tap' or (proto in MODBUS_PROTOCOLS and template_listens(tpl)):
        kind, mode = 'tap', 'listen'
        proto = conn['protocol'] = 'rtu_tap'
    elif proto in MODBUS_PROTOCOLS:
        kind, mode = 'modbus', 'poll'
    else:
        raise refuse(422, f"connection.protocol: '{proto}' cannot be previewed")

    # ADR-0025: nothing is transmitted on a bus another master owns — asked
    # before anything else is judged, whatever name the bus is reached by
    bus = bus_of(proto, conn)
    if mode == 'poll' and bus:
        ids = _any_listener(env, bus)
        if ids:
            raise Refused(409, refusal_detail("A preview", bus, ids, where_of(proto, conn)))

    if env.validate is not None:
        # the connection as a save judges it (422): as sent, a bridge named by its id
        env.validate({**said, **({'mode': conn['mode']} if 'mode' in conn else {}),
                      **({'protocol': 'rtu_tap'} if kind == 'tap' else {})}, tid)

    raw = {'id': 'preview', 'name': 'preview', 'template': tid, 'enabled': True, 'connection': conn}
    built = cfg.preview_devices(device=raw)
    if not built or not (built[0].sources or []):
        raise refuse(422, "connection: it does not make a device that reads anything")
    dev, src = built[0], built[0].sources[0]
    if rows is None:
        rows = template_rows(cfg, tpl)
    intervals = group_intervals(rows, getattr(cfg, 'poll_groups', None), getattr(tpl, 'poll_groups', None),
                                groups)
    max_read = MODBUS_MAX_READ
    try:
        if int(tproto.get('max_registers_per_read') or 0) > 0:
            max_read = min(MODBUS_MAX_READ, int(tproto['max_registers_per_read']))
    except (TypeError, ValueError):
        pass
    plan = Plan(kind=kind, mode=mode, transport='own', key=bus or '', where=where_of(proto, conn) if bus else '',
                template=tpl, rows=list(rows or []), intervals=intervals, conn=conn, dev=dev, src=src,
                seconds=secs, max_requests=int(budget['max_requests']), listen_s=int(budget['listen_s']),
                device_id=device_id, unit=conn.get('unit_id'), confirm_open=False,
                byte_order=env.templates.byte_order_for(tid) if hasattr(env.templates, 'byte_order_for')
                else tproto.get('byte_order', 'big'), max_read=max_read)
    plan.line = {'baud': int(conn.get('baudrate') or 9600),
                 'serial': proto in SERIAL_PROTOCOLS + BUS_OVER_TCP_PROTOCOLS + VIRTUAL_MAP_PROTOCOLS + ('rtu_tap',)}
    if not plan.rows:
        plan.warnings.append(f"template '{tid}' has no rows a new device would read (pick registers after saving)")

    confirm = confirm_open is True
    if kind == 'modbus':
        _prepare_modbus(env, plan, proto, bus, confirm, set(edited if edited is not None else conn))
    elif kind == 'seplos':
        _prepare_seplos_poll(env, plan, proto, bus, confirm, set(edited if edited is not None else conn))
    elif kind in ('tap', 'seplos_listen'):
        if conn.get('host') and not conn.get('serial_port'):
            _lan(str(conn.get('host')), env, 'connection.host')
            if not _reader_running(kind, src.connection):
                # a reader of its own over the network is a connection to the bridge too
                _check_open(env, plan, str(conn.get('host')), int(conn.get('port') or 0), bus, confirm)
        elif conn.get('serial_port') and not _reader_running(kind, src.connection):
            _check_tty(env, plan, str(conn.get('serial_port')), bus, confirm)
        plan.transport = 'tap'
        plan.share_now = _share(_bus_entry(env, bus))
    elif kind == 'can':
        _prepare_can(env, plan)
    elif kind == 'http':
        url = str((src.http or {}).get('url') or conn.get('url') or '').strip()
        if not (url.startswith('http://') or url.startswith('https://')):
            raise refuse(422, "connection.url required (http:// or https://)")
        if not getattr(cfg.security, 'allow_nonlan_http_devices', False):
            from .http_client import lan_url_error
            e = lan_url_error(url)
            if e:
                raise refuse(422, f"connection.url: {e}")
        from urllib.parse import urlparse
        plan.key, plan.where = f"http:{urlparse(url).netloc.lower()}", urlparse(url).netloc
    elif kind == 'mqtt':
        _prepare_mqtt(env, plan)
    return plan


def _reader_running(kind: str, conn_cfg) -> bool:
    """A reader of that bus already runs (a device listens): the preview
    joins it and opens nothing."""
    if kind == 'tap':
        from . import rtu_tap
        with rtu_tap._READERS_LOCK:
            r = rtu_tap._READERS.get(rtu_tap._reader_key(conn_cfg))
        return r is not None and r._thread is not None
    from . import seplos_v2
    with seplos_v2._REG_LOCK:
        r = seplos_v2._LISTENERS.get(seplos_v2.bus_key(conn_cfg))
    return r is not None and r._thread is not None


LINE_KEYS = ('baudrate', 'parity', 'stopbits', 'bytesize', 'timeout')


def _starting(plan: Plan, holder_id: str, bus: str) -> Refused:
    """N1: the device that holds the bus has no live connection now (it is
    starting, reconnecting, or failed to start): the preview never opens one
    of its own there — a starting device could inherit it, with the
    preview's line."""
    return refuse(409, f"{plan.where}: the bus is busy starting '{holder_id}' — retry in a moment "
                       f"(nothing was sent)", bus=bus, holder=holder_id)


def _same(a, b) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).strip().upper() == str(b).strip().upper()


def _keep_line(plan: Plan, holder_id: str, line: Dict[str, Any], edited: set) -> None:
    """m9 / N3: through a holder's connection its line and timeout are used —
    said in the answer; a request that names another is refused."""
    conn = plan.conn
    said = [k for k in LINE_KEYS if k in edited and conn.get(k) is not None and line.get(k) is not None
            and not _same(conn.get(k), line.get(k))]
    if said:
        # `line`: the holder's values, so a form can take them and ask again
        # (Add's Check, L3-UX-12) instead of showing this line raw
        raise refuse(422, *[f"connection.{k}: {plan.where} is read through {holder_id}'s connection, whose "
                            f"{k} is {line.get(k)} — a preview cannot change it; save the device to change the "
                            f"line" for k in said], holder=holder_id, line={k: line.get(k) for k in said})
    plan.shared_line = dict(line)
    plan.warnings.append(
        f"read through {holder_id}'s connection: its line ({line['baudrate']} Bd, {line['bytesize']}"
        f"{line['parity']}{line['stopbits']}) and timeout ({line['timeout']} s) were used")


def _check_tty(env: Env, plan: Plan, serial_port: str, bus: str, confirm: bool) -> None:
    """A local serial line another process holds (serial_inventory: busy):
    opening it beside that program mixes two masters' bytes — refused unless
    confirmed, like an unknown bridge client."""
    from . import serial_inventory
    from .bus_claims import canonical_serial
    want = canonical_serial(serial_port)
    if not want:
        return
    try:
        ports = serial_inventory.inventory(env.config, env.templates, show_all=True).get('ports') or []
    except Exception:  # noqa: BLE001 — an unreadable /proc: said as unknown below only when it is busy
        logger.debug("preview: serial inventory not read", exc_info=True)
        return
    for p in ports:
        names = [p.get('path'), *(p.get('by_id') or []), *(p.get('aliases') or [])]
        if want not in {canonical_serial(n) for n in names if n}:
            continue
        if p.get('base_state') == 'busy':
            said = f"{serial_port} is open in another process ({', '.join(p.get('processes') or []) or '?'})"
            if not confirm:
                raise refuse(409, f"{said} — opening it beside that program would mix two masters on the line. "
                                  f"Nothing was sent; send confirm_open: true to open it anyway", bus=bus,
                             client='unknown', unknown=said)
            plan.confirm_open = True
            plan.warnings.append(f"opened {serial_port} although {said} (confirm_open)")
        return


def _prepare_modbus(env: Env, plan: Plan, proto: str, bus: str, confirm: bool, edited: set) -> None:
    from .config import BUS_OVER_TCP_PROTOCOLS, NETWORK_PROTOCOLS, SERIAL_PROTOCOLS
    conn = plan.conn
    if proto in SERIAL_PROTOCOLS:
        if not str(conn.get('serial_port', '') or '').strip():
            raise refuse(422, "connection.serial_port required")
    elif proto in NETWORK_PROTOCOLS:
        _lan(str(conn.get('host', '') or ''), env, 'connection.host')
    if proto in SERIAL_PROTOCOLS + BUS_OVER_TCP_PROTOCOLS and int(conn.get('unit_id', 1) or 0) == 0:
        raise refuse(422, "connection.unit_id: 0 is the broadcast address on a serial bus — every slave would "
                          "act on it and none answers; give the slave's own address")
    holder, hconn, other = _holder(env, proto, bus)
    if other:
        raise refuse(409, f"{plan.where} is read by {', '.join(other)} in another protocol — one port speaks "
                          f"one framing; nothing was sent", bus=bus)
    plan.share_now = _share(_bus_entry(env, bus))
    if holder is not None:
        if not getattr(hconn, 'share_transport', True):
            raise refuse(409, f"{plan.where} is polled by '{holder.id}' with share_transport: false — its "
                              f"connection is its own and takes no second user, and a second connection would "
                              f"contend with it. Nothing was sent", bus=bus, holder=holder.id)
        client = env.running(holder.id) if env.running is not None else None
        mc = _running_connection(client, bus) if client is not None else None
        if mc is None:
            raise _starting(plan, holder.id, bus)
        # the holder's own connection: its socket, lock, lane and turn (M2, m6)
        _keep_line(plan, holder.id, {k: getattr(mc.config, k, None) for k in LINE_KEYS}, edited)
        plan.transport, plan.shared_with, plan.holder_mc = 'shared', holder.id, mc
        return
    host = str(conn.get('host', '') or '')
    port = int(conn.get('port') or 0)
    # a serial bus behind a converter, one of our declared bridge ports, or
    # plain tcp off 502 (a raw serial server serves one client): a connection
    # of our own may take another master's place there
    if host and (proto in BUS_OVER_TCP_PROTOCOLS or _declared_bridge(env, host, port)
                 or (proto == 'tcp' and port != 502)):
        _check_open(env, plan, host, port, bus, confirm)
    elif proto in SERIAL_PROTOCOLS:
        _check_tty(env, plan, str(conn.get('serial_port') or ''), bus, confirm)
def _prepare_seplos_poll(env: Env, plan: Plan, proto: str, bus: str, confirm: bool, edited: set) -> None:
    from . import seplos_v2
    from .config import VIRTUAL_MAP_OVER_TCP
    conn = plan.conn
    if proto in VIRTUAL_MAP_OVER_TCP:
        if not str(conn.get('host', '') or '').strip():
            raise refuse(422, "connection.host required")
        _lan(str(conn.get('host')), env, 'connection.host')
    elif not str(conn.get('serial_port', '') or '').strip():
        raise refuse(422, "connection.serial_port required")
    holder, _hc, other = _holder(env, proto, bus,
                                 mode_of=lambda c: str(getattr(c, 'mode', '') or 'poll').lower())
    if other:
        raise refuse(409, f"{plan.where} is read by {', '.join(other)} in another protocol — a Seplos question "
                          f"there would land on another protocol's bus; nothing was sent", bus=bus)
    with seplos_v2._REG_LOCK:
        heard = seplos_v2._LISTENERS.get(seplos_v2.bus_key(plan.src.connection))
        busy = bool(heard is not None and (heard._clients or heard._observers))
        held = seplos_v2._BUSES.get(seplos_v2.bus_key(plan.src.connection))
        held_open = bool(held is not None and held.users > 0)
    if busy:
        raise refuse(409, f"{plan.where} is listened to by the gateway — another master polls it, so a question "
                          f"from here would collide; nothing was sent", bus=bus)
    plan.share_now = _share(_bus_entry(env, bus))
    if holder is not None:
        if not held_open:
            raise _starting(plan, holder.id, bus)
        # its _Bus: one port, one lock, its line and its timeout (N3)
        baud, parity, stopbits, bytesize = held.line
        line = {'baudrate': baud, 'parity': parity, 'stopbits': stopbits, 'bytesize': bytesize,
                'timeout': getattr(held.conn, 'timeout', None)}
        _keep_line(plan, holder.id, line, edited)
        plan.transport, plan.shared_with = 'shared', holder.id
        plan.conn['timeout'] = line['timeout']
        return
    if proto in VIRTUAL_MAP_OVER_TCP and not held_open:
        _check_open(env, plan, str(conn.get('host')), int(conn.get('port') or 0), bus, confirm)
    elif not held_open:
        _check_tty(env, plan, str(conn.get('serial_port') or ''), bus, confirm)


def _prepare_can(env: Env, plan: Plan) -> None:
    from . import can_client
    can = dict(getattr(plan.src, 'can', None) or {})
    errs = can_client.connection_errors({**plan.conn, **can})
    if errs:
        raise refuse(422, *errs)
    ch = str(can.get('channel') or plan.conn.get('channel') or 'can0')
    lo = can_client.listen_only(ch)
    if lo is not True:
        raise refuse(409, f"{ch} is not in listen-only mode{' (or it could not be read)' if lo is None else ''}: "
                          f"the controller would acknowledge frames on the bus. Set it on the host "
                          f"(ip link set {ch} type can bitrate {can.get('bitrate') or 500000} listen-only on); "
                          f"nothing was opened", channel=ch)
    plan.transport, plan.key, plan.where = 'tap', f"can:{ch}", ch


def _command_topics(tpl) -> set:
    """Every topic the template writes to: its rows' write_topic and every
    topic its commands name (at any depth)."""
    out = {str(getattr(r, 'write_topic', '') or '') for r in (getattr(tpl, 'registers', None) or [])}

    def walk(v, key=''):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, str(k))
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, str) and 'topic' in key.lower():
            out.add(v)
    walk(getattr(tpl, 'commands', None) or {})
    return {t for t in out if t}


def _keepalive_reads_only(plan: Plan, m: Dict[str, Any]) -> None:
    """The one publish a preview sends is the keepalive; it must not be a
    write (a Victron ``W/`` topic, or a topic a command writes to)."""
    from .mqtt_input import parse_keepalive
    ka = m.get('keepalive')
    if ka is None:
        ka = (getattr(plan.template, 'protocol', None) or {}).get('keepalive')
    ka = parse_keepalive(ka) if ka else None
    if not ka:
        return
    tvars = {str(k): str(v) for k, v in (m.get('vars') or {}).items()} if isinstance(m.get('vars'), dict) else {}

    def fill(t: str) -> str:
        return re.sub(r'\{([A-Za-z0-9_]+)\}', lambda x: tvars.get(x.group(1), x.group(0)), str(t or ''))
    topic = fill(ka['topic'])
    if topic.startswith('W/') or topic in {fill(t) for t in _command_topics(plan.template)}:
        raise refuse(422, f"connection.keepalive: '{topic}' is a topic a command writes to — the preview sends "
                          f"only a read request to the broker; nothing was sent")


def _prepare_mqtt(env: Env, plan: Plan) -> None:
    from . import mqtt_link
    from .device_runtime import GATEWAY_BROKER, gateway_broker
    m = gateway_broker(env.config, dict(plan.src.mqtt_in or {}))
    broker = str(m.get('broker', '') or '').strip()
    if not broker or not str(m.get('topic', '') or plan.conn.get('topic', '') or '').strip():
        raise refuse(422, "connection.broker and connection.topic required")
    if str(plan.conn.get('broker', '')).strip() != GATEWAY_BROKER:
        _lan(broker, env, 'connection.broker')
    port = int(m.get('port', 1883) or 1883)
    _keepalive_reads_only(plan, m)
    plan.key, plan.where = f"mqtt:{broker.lower()}:{port}", f"{broker}:{port}"
    user = str(m.get('username', '') or '')
    link = next((ln for ln in mqtt_link.links()
                 if ln.connected and str(ln.broker).lower() == broker.lower() and int(ln.port) == port
                 and str(getattr(ln._cfg, 'username', '') or '') == user), None)
    if link is not None:
        plan.transport, plan.link, plan.shared_with = 'shared', link, link.name
    plan.conn = {**plan.conn, **{k: m.get(k) for k in ('broker', 'port', 'username', 'password', 'tls',
                                                       'tls_ca', 'tls_insecure') if k in m}}


# ── the jobs ────────────────────────────────────────────────────────────────

class Job:
    def __init__(self, plan: Plan, env: Optional[Env] = None):
        self.id = uuid.uuid4().hex[:12]
        self.plan = plan
        # what a runner reads of the gateway (the template library, the
        # gateway's broker, the LAN policy)
        self.config = getattr(env, 'config', None)
        self.templates = getattr(env, 'templates', None)
        self.allow_nonlan = bool(getattr(getattr(self.config, 'security', None),
                                         'allow_nonlan_http_devices', False))
        self.state = 'running'
        self.cancel = threading.Event()
        self.started = time.time()
        self.finished: Optional[float] = None
        self.abandoned = False              # past its wall time: what its worker writes is ignored
        self._answer: Dict[str, Any] = {}
        self.error = ''
        self.final: Optional[str] = None

    @property
    def answer(self) -> Dict[str, Any]:
        return self._answer

    @answer.setter
    def answer(self, value: Dict[str, Any]) -> None:
        if not self.abandoned:
            self._answer = value

    def view(self) -> Dict[str, Any]:
        out = {'job': self.id, 'state': self.state, **self.plan.head(),
               'started': round(self.started, 3), 'finished': round(self.finished, 3) if self.finished else None}
        out.update({'values': [], 'groups': {'total': 0, 'asked': 0, 'answered': 0, 'heard': 0},
                    'identity': None, 'components': [], 'budget': {}, 'warnings': list(self.plan.warnings),
                    'load': {'share_now': self.plan.share_now, 'share_added': None}})
        out.update(self.answer)
        if self.error:
            out['error'] = self.error
        return out


_JOBS: Dict[str, Job] = {}
_BUSY: Dict[str, str] = {}
_LOCK = threading.Lock()


def _sleep(seconds: float, cancel: threading.Event) -> None:
    """Every listening window of a preview: ends early on a cancel."""
    cancel.wait(max(0.0, seconds))


def _word(plan: Plan) -> str:
    return 'scan' if plan.kind in SCAN_KINDS else 'preview'


def start(env: Env, plan: Plan, on_done: Optional[Callable[[Job], None]] = None, job_cls=None) -> Job:
    """Run ``plan`` in the background; 409 while a preview or a bus scan of
    that bus runs (one job per bus, whatever its kind); 429 past MAX_JOBS
    over every bus. ``job_cls``: a Job of a kind of its own (bus_scan.py)."""
    now = time.time()
    with _LOCK:
        for k in [k for k, j in _JOBS.items() if j.finished and now - j.finished > JOB_TTL_S]:
            _JOBS.pop(k, None)
        if plan.key and plan.key in _BUSY:
            other = _JOBS.get(_BUSY[plan.key])
            what = _word(other.plan) if other is not None else 'preview'
            raise refuse(409, f"a {what} of {plan.where or plan.key} is already running — wait for its answer "
                              f"(one preview or scan per bus at a time)", job=_BUSY[plan.key])
        if plan.kind == 'scan':
            # one scan that asks at a time, over every bus (a listen and the
            # previews are not counted here: the cap below holds them)
            other = next((j for j in _JOBS.values() if j.finished is None and j.plan.kind == 'scan'), None)
            if other is not None:
                raise refuse(409, f"a scan of {other.plan.where or other.plan.key} is already running — one bus is "
                                  f"scanned at a time; wait for it or stop it", job=other.id)
        running = sum(1 for j in _JOBS.values() if j.finished is None)
        if running >= MAX_JOBS:
            raise Refused(429, {"errors": [f"busy: {running} previews or scans are running (at most {MAX_JOBS} "
                                           f"at once) — try again in a few seconds"]},
                          headers={'Retry-After': str(RETRY_AFTER_S)})
        job = (job_cls or Job)(plan, env)
        _JOBS[job.id] = job
        if plan.key:
            _BUSY[plan.key] = job.id
    threading.Thread(target=_run, args=(job, on_done), daemon=True, name=f"{_word(plan)}-{job.id}").start()
    return job


SCAN_KINDS = ('scan', 'scan_listen')          # bus_scan.py's jobs, in the same registry


def _job(job_id: str, scans: bool) -> Optional[Job]:
    with _LOCK:
        j = _JOBS.get(job_id)
    if j is None or (j.plan.kind in SCAN_KINDS) != scans:
        return None
    return j


def get(job_id: str, scans: bool = False) -> Optional[Dict[str, Any]]:
    """A preview's answer (``scans``: a bus scan's); None when there is none."""
    j = _job(job_id, scans)
    return j.view() if j is not None else None


def cancel(job_id: str, scans: bool = False) -> Optional[bool]:
    """True: asked to stop; False: it had finished; None: no such job."""
    j = _job(job_id, scans)
    if j is None:
        return None
    if j.finished is not None:
        return False
    j.cancel.set()
    return True


_RUNNERS: Dict[str, Callable] = {}


def wall_s(plan: Plan) -> float:
    """The job's wall time (N4): past it the job ends whatever its worker is
    doing — a request stuck behind a wedged lock is abandoned, never broken."""
    if plan.wall:
        return float(plan.wall)
    if plan.mode == 'listen':
        return plan.seconds + 5.0 + WALL_MARGIN_S          # + a broker connect (component_discover.CONNECT_S)
    if plan.kind == 'http':
        t = float(plan.conn.get('timeout') or 8)
        return min(DEADLINE_CAP_S, plan.max_requests * (t + 1.0)) + WALL_MARGIN_S
    mc = plan.holder_mc
    t = float(getattr(getattr(mc, 'config', None), 'timeout', None) or plan.conn.get('timeout')
              or getattr(plan.src.connection, 'timeout', 3) or 3)
    gap = float(getattr(getattr(mc, 'config', None), 'endpoint_min_gap_s', 0.0) or 0.0)
    return poll_deadline_s(plan.max_requests, t, gap) + WALL_MARGIN_S


def _run(job: Job, on_done) -> None:
    box: Dict[str, Any] = {}
    finished = threading.Event()

    def work() -> None:
        try:
            _RUNNERS[job.plan.kind](job)
            box['final'] = 'cancelled' if job.cancel.is_set() else 'done'
        except GuardRefusal as e:                  # a bug, never the wire: said and stopped
            logger.error("preview %s: refused by its own guard — %s", job.id, e)
            box['error'] = f"refused by the preview's guard: {e}"
        except Exception as e:  # noqa: BLE001 — a preview never takes the gateway down
            logger.warning("preview %s failed: %s", job.id, e, exc_info=True)
            box['error'] = str(e)
        finally:
            finished.set()

    wall = wall_s(job.plan)
    threading.Thread(target=work, daemon=True, name=f"preview-{job.id}-io").start()
    if finished.wait(wall):
        final = box.get('final', 'failed')
        if box.get('error'):
            job.error = box['error']
    else:
        # the worker is left where it is stuck (a production lock is never
        # touched); whatever it does later is ignored, and it sends nothing
        # more: its deadline has passed and the cancel is set
        job.cancel.set()
        job.abandoned = True
        final = 'failed'
        job.error = (f"the preview did not finish within {wall:g} s — stopped (a request is stuck on "
                     f"{job.plan.where or job.plan.key}); the bus was left to its devices")
        logger.warning("preview %s: %s", job.id, job.error)
    job.final = final
    with _LOCK:
        if _BUSY.get(job.plan.key) == job.id:
            _BUSY.pop(job.plan.key, None)
    if on_done is not None:
        try:
            on_done(job)                           # the audit line, before the answer says done
        except Exception:  # noqa: BLE001
            logger.exception("preview %s: on_done failed", job.id)
    job.finished = time.time()
    job.state = final
def _value(r, value, quality, **where) -> Dict[str, Any]:
    if isinstance(value, float):
        value = round(value, 6)
    return {'name': r.name, 'label': getattr(r, 'label', '') or r.name, 'value': value,
            'unit': getattr(r, 'unit', '') or '', 'quality': quality,
            'group': getattr(r, 'poll_group', '') or 'normal', **where}


def _common_warnings(values: List[Dict[str, Any]]) -> List[str]:
    nums = [v['value'] for v in values if v['quality'] == 'ok' and isinstance(v['value'], (int, float))
            and not isinstance(v['value'], bool)]
    out = []
    if len(nums) >= 3 and all(n == 0 for n in nums):
        out.append("every value read is 0 — check the model, the unit address or the byte order")
    if any(v['quality'] in ('byte_swap_suspect', 'out_of_range') for v in values):
        out.append("some values look wrong: byte order or scale? Compare them with the device's own display")
    return out


# ── Modbus, polled ──────────────────────────────────────────────────────────

def _modbus_config(plan: Plan):
    """A connection of the preview's own (nobody of ours holds the bus)."""
    base = plan.src.connection
    mc = copy.copy(base)
    mc.unit_id = int(plan.conn.get('unit_id', getattr(base, 'unit_id', 1)) or 0)
    try:
        mc.timeout = max(0.3, min(5.0, float(plan.conn.get('timeout') or getattr(base, 'timeout', 3) or 3)))
    except (TypeError, ValueError):
        mc.timeout = 3
    mc.retry_attempts, mc.retry_delay = 1, 0
    return mc


def _in_turn(mc, guard: GuardedModbusClient, send: Callable[[GuardedModbusClient], Any],
             deadline: Optional[float] = None):
    """One request in the connection's turn: (response, '') or (None, why).
    Every request a preview sends comes through here (a register read, FC43):
    nothing past the job's deadline; nothing on a client already counting
    silences; a preview's timeout never counts towards pymodbus' own
    "close after N silent requests" on that client (the count is put back,
    still under the turn)."""
    if deadline is not None and time.monotonic() > deadline:
        return None, 'deadline'
    with mc.transaction(label=PREVIEW_TRACE) as client:
        if client is None:
            return None, 'no_connection'
        if deadline is not None and time.monotonic() > deadline:
            return None, 'deadline'              # the turn came too late (an abandoned job): nothing sent
        tm = getattr(client, 'transaction', None)
        before = getattr(tm, 'count_until_disconnect', None)
        if isinstance(before, int) and isinstance(getattr(tm, 'max_until_disconnect', None), int) \
                and before < tm.max_until_disconnect:
            return None, 'bus_unwell'
        guard.inner = client
        try:
            return send(guard), ''
        except GuardRefusal:
            raise
        except Exception as e:  # noqa: BLE001 — a timeout, a dropped socket: no answer
            logger.debug("preview request: %s", e)
            return None, 'no_answer'
        finally:
            guard.inner = None
            if isinstance(before, int) and isinstance(getattr(tm, 'count_until_disconnect', None), int) \
                    and tm.count_until_disconnect < before:
                tm.count_until_disconnect = before


def _read_window(mc, guard: GuardedModbusClient, table: str, start: int, count: int, unit: int,
                 deadline: Optional[float] = None):
    """One register read: (words, '') or (None, why)."""
    from pymodbus.pdu import ExceptionResponse

    def send(g):
        fn = g.read_input_registers if table == 'input' else g.read_holding_registers
        return fn(start, count=count, device_id=unit)
    r, why = _in_turn(mc, guard, send, deadline)
    if why:
        return None, why
    if isinstance(r, ExceptionResponse) or r is None or r.isError():
        return None, 'illegal_address' if getattr(r, 'exception_code', None) == 2 else 'no_answer'
    regs = list(getattr(r, 'registers', None) or [])
    if len(regs) < count:
        return None, 'no_answer'
    return regs[:count], ''


def _fc43(mc, guard: GuardedModbusClient, unit: int, deadline: Optional[float] = None) -> Optional[Dict[str, str]]:
    """FC43/14, with every guard a register read has (N2)."""
    r, why = _in_turn(mc, guard, lambda g: g.read_device_information(device_id=unit), deadline)
    if why or r is None:
        return None
    info = getattr(r, 'information', None) or {}
    dec = lambda b: b.decode(errors='replace').strip() if isinstance(b, (bytes, bytearray)) else str(b)  # noqa: E731
    return {k: dec(info[i]) for i, k in ((0, 'vendor'), (1, 'product'), (2, 'version')) if i in info} or None
def _wire_allowed() -> bool:
    """Asked before every request: a gateway gone standby stops its previews."""
    from . import role
    return role.is_active()


def _run_modbus(job: Job) -> None:
    from .device_setup import provides
    from .modbus_client import ModbusConnection
    plan = job.plan
    tpl = plan.template
    budget = Budget(plan.max_requests)
    idf = getattr(tpl, 'identify', None) or {}
    guard = GuardedModbusClient(None, budget, plan.max_read, allow_fc43=bool(idf.get('fc43')))
    setup = [p for p in (provides(s) for s in (getattr(tpl, 'setup', None) or [])) if p]
    windows, skipped = plan_windows(plan.rows, plan.intervals, plan.max_read,
                                    int(getattr(plan.src.connection, 'max_gap', 10) or 0), setup)
    cache: Dict[Tuple[str, int], int] = {}
    errors: Dict[Tuple[str, int], str] = {}
    warnings = list(plan.warnings)
    asked = answered = 0
    stop = {'why': ''}
    unit = int(plan.conn.get('unit_id', 1) or 0)
    own = plan.holder_mc is None
    mc = ModbusConnection(_modbus_config(plan), trace_label=f"preview:{plan.where}") if own else plan.holder_mc
    gap = float(getattr(mc.config, 'endpoint_min_gap_s', 0.0) or 0.0)
    hand_back = max(gap, YIELD_S if not own else 0.0)
    deadline = time.monotonic() + poll_deadline_s(plan.max_requests, float(getattr(mc.config, 'timeout', 3) or 3),
                                                  gap)

    def read(w: Window) -> bool:
        if stop['why'] or job.cancel.is_set():
            return False
        if not _wire_allowed():
            stop['why'] = "the gateway became standby — the preview stopped (its partner is on the wire)"
            return False
        if time.monotonic() > deadline:
            stop['why'] = "the preview ran out of time — it stopped"
            return False
        words, err = _read_window(mc, guard, w.table, w.start, w.count, unit, deadline=deadline)
        if hand_back > 0:
            # the line back to the devices between two of the preview's turns
            _sleep(hand_back, job.cancel)
        if words is not None:
            for i, x in enumerate(words):
                cache[(w.table, w.start + i)] = x
            return True
        for a in range(w.start, w.end):
            errors[(w.table, a)] = err
        if err == 'no_connection':
            stop['why'] = (f"no connection to {plan.where} (or no turn on the shared bus within "
                           f"{getattr(mc.config, 'endpoint_wait_s', 10):g} s) — nothing more was asked")
        elif err == 'no_answer':
            stop['why'] = (f"no answer from unit {unit} on {plan.where} — check the address; the preview "
                           f"stopped after one silent request")
        elif err == 'deadline':
            stop['why'] = "the preview ran out of time — it stopped"
        elif err == 'bus_unwell':
            stop['why'] = (f"{plan.where} is already missing answers — the preview does not add to it; "
                           f"nothing was sent")
        return False

    identity: Dict[str, Any] = {'template': tpl.id, 'fingerprint': 'none', 'suggested': None,
                                'fc43': None, 'sunspec': None}
    try:
        # the identity first: a wrong template shows before its values do
        id_windows = identify_windows(tpl, plan.max_read)
        sunspec = bool((getattr(tpl, 'protocol', None) or {}).get('sunspec'))
        if sunspec:
            id_windows.append(Window('holding', SUNSPEC_BASE, SUNSPEC_BASE + min(SUNSPEC_SPAN, plan.max_read),
                                     'identify', 0.0))
        reserve = max(0, plan.max_requests - 1) if windows else plan.max_requests
        for w in id_windows:
            if budget.requests >= reserve:
                break
            read(w)
        if idf.get('fc43') and budget.requests < reserve and not job.cancel.is_set() and not stop['why'] \
                and _wire_allowed():
            identity['fc43'] = _fc43(mc, guard, unit, deadline=deadline)
        identity['fingerprint'] = fingerprint(tpl, cache, errors)
        if identity['fc43'] is not None and idf.get('fc43'):
            fc = idf['fc43']
            ok = all(re.search(str(fc[k]), identity['fc43'].get(k, ''), re.I) for k in ('vendor', 'product')
                     if fc.get(k))
            identity['fingerprint'] = 'mismatch' if not ok else (
                'match' if identity['fingerprint'] in ('none', 'match') else identity['fingerprint'])
        if sunspec:
            ss = sunspec_identity(cache)
            identity['sunspec'] = ss
            answered_marker = (('holding', SUNSPEC_BASE) in cache) or (('holding', SUNSPEC_BASE) in errors
                                                                       and errors[('holding', SUNSPEC_BASE)]
                                                                       == 'illegal_address')
            if ss is None and answered_marker:
                identity['fingerprint'] = 'mismatch'
            elif ss is not None and identity['fingerprint'] in ('none', 'unknown'):
                identity['fingerprint'] = 'match'
        if identity['fingerprint'] == 'mismatch' and not stop['why']:
            identity['suggested'] = _suggest(job, tpl, cache, errors, budget, read, plan.max_read)
        if identity['suggested']:
            st = next((t for t in _identifiable(job) if t.id == identity['suggested']), None)
            identity['suggested_name'] = getattr(st, 'name', '') if st else ''
        # the values, fastest group first, within what is left
        for w in windows:
            if job.cancel.is_set() or stop['why'] or budget.left() <= 0:
                break
            asked += 1
            if read(w):
                answered += 1
    finally:
        if own:
            mc.disconnect()                       # the holder's connection is never ours to close
    if stop['why']:
        warnings.append(stop['why'])
    if len(windows) > asked and not job.cancel.is_set() and not stop['why']:
        warnings.append(f"{asked} of {len(windows)} reads were made within the budget of "
                        f"{plan.max_requests} request(s) (Settings → preview.max_requests): the rest is "
                        f"shown as not answered")
    decoded = decode_cache([r for w in windows for r in w.rows], cache, errors, plan.byte_order)
    values = []
    for w in windows:
        for r in w.rows:
            v, q = decoded[r.name]
            values.append(_value(r, v, q, address=r.address, register_type=w.table))
    for r, why in skipped:
        values.append(_value(r, None, 'no_answer', address=r.address, register_type=_table(r), note=why))
    if skipped:
        warnings.append(f"{len(skipped)} row(s) not read by a preview: "
                        + "; ".join(sorted({why for _r, why in skipped})))
    warnings += _common_warnings(values)
    line = dict(plan.line)
    if not line.get('serial'):
        from .modbus_client import endpoint_bus_stats
        try:
            line['tx_s'] = endpoint_bus_stats(mc.config.host, int(mc.config.port)).get('tx_p50_s')
        except Exception:  # noqa: BLE001
            line['tx_s'] = None
    job.answer = {
        'values': values,
        'groups': {'total': len(windows), 'asked': asked, 'answered': answered, 'heard': answered},
        'identity': identity, 'components': [], 'budget': budget.view(), 'warnings': warnings,
        'load': {'share_now': plan.share_now,
                 'share_added': estimate_added([{'count': w.count, 'interval': w.interval} for w in windows], line),
                 'basis': _basis(line)},
    }


def _identifiable(job: Job) -> List[Any]:
    reg = getattr(job, 'templates', None)
    return [t for t in (reg.all() if reg is not None else [])
            if ((getattr(t, 'identify', None) or {}).get('registers'))]


def _suggest(job: Job, tpl, cache, errors, budget: Budget, read: Callable[[Window], bool],
             max_read: int) -> Optional[str]:
    """The template whose fingerprint the device matches: first from what was
    already read, then — while one request stays for the values — reading
    each candidate's fingerprint when it fits in one read."""
    for t in sorted(_identifiable(job), key=lambda x: x.id):
        if t.id == tpl.id:
            continue
        verdict = fingerprint(t, cache, errors)
        if verdict == 'unknown':
            ws = identify_windows(t, max_read)
            if len(ws) != 1 or budget.left() <= 1 or job.cancel.is_set():
                continue
            read(ws[0])
            verdict = fingerprint(t, cache, errors)
        if verdict == 'match':
            return t.id
    return None


# ── the load ────────────────────────────────────────────────────────────────

def estimate_added(windows: List[Dict[str, Any]], line: Dict[str, Any]) -> float:
    """The share of the bus these reads would take, polled at their groups'
    intervals: on a serial line (11 bits per byte, 3.5 characters of silence
    each way and a few ms of the slave's own time), else the measured time
    per transaction on that endpoint (30 ms when nothing was measured)."""
    total = 0.0
    baud = max(300, int(line.get('baud') or 9600))
    for w in windows:
        iv = max(0.05, float(w.get('interval') or 5.0))
        if line.get('serial'):
            nbytes = w.get('bytes') or (8 + 5 + 2 * int(w.get('count') or 0))
            t = nbytes * BITS_PER_BYTE / baud + 2 * 3.5 * BITS_PER_BYTE / baud + 0.005
        else:
            t = float(line.get('tx_s') or 0.03)
        total += t / iv
    return round(total, 4)


def _basis(line: Dict[str, Any]) -> str:
    if line.get('serial'):
        return f"{line.get('baud') or 9600} Bd, {BITS_PER_BYTE} bits per byte, the groups' intervals"
    return ("the measured time per transaction on this endpoint" if line.get('tx_s')
            else "30 ms per transaction (nothing measured on this endpoint yet)") + ", the groups' intervals"


# ── Seplos V2 ───────────────────────────────────────────────────────────────

def _seplos_values(plan: Plan, client, asked_cmds: set, answered_cmds: set) -> List[Dict[str, Any]]:
    from .seplos_v2 import AREA_DIAG, area_of
    with client._lock:
        words = dict(client.words)
    cache = {('input', a): w for a, w in words.items()}
    rows = [r for r in plan.rows if area_of(r.address) != AREA_DIAG]
    decoded = decode_cache(rows, cache, {}, plan.byte_order)
    return [_value(r, *decoded[r.name], address=r.address, command=f"0x{area_of(r.address):02X}"
                   if isinstance(area_of(r.address), int) else str(area_of(r.address)))
            for r in rows]


def _seplos_conn(plan: Plan, mode: str):
    c = copy.copy(plan.src.connection)
    c.mode = mode
    c.retry_attempts, c.retry_delay = 1, 0
    try:
        c.timeout = max(0.3, min(5.0, float(plan.conn.get('timeout') or getattr(c, 'timeout', 3) or 3)))
    except (TypeError, ValueError):
        c.timeout = 3
    return c


def _run_seplos(job: Job) -> None:
    from . import seplos_v2 as sv
    plan = job.plan
    budget = Budget(plan.max_requests)
    client = sv.SeplosV2Client(_seplos_conn(plan, 'poll'), plan.rows, {}, byte_order=plan.byte_order,
                               device_id='preview')
    if client.config_error:
        raise RuntimeError(client.config_error)
    plan_groups = client._plan()
    cmds: List[Tuple[int, float]] = []
    for _g, iv, _rs, cs in sorted(plan_groups, key=lambda p: p[1]):
        for c in cs:
            if c not in [x for x, _i in cmds]:
                cmds.append((c, iv))
    asked, answered = set(), set()
    bus = client._bus
    why = ''
    try:
        for cid2, _iv in cmds:
            if job.cancel.is_set() or budget.left() <= 0:
                break
            if not _wire_allowed():
                why = "the gateway became standby — the preview stopped (its partner is on the wire)"
                break
            if cid2 not in sv.READ_COMMANDS:          # the guard: read commands only (ADR-0014)
                raise GuardRefusal(f"Seplos command 0x{cid2:02X} is not a read")
            budget.take(0)
            asked.add(cid2)
            silent = client.stats.timeouts
            if client.poll_once(cid2):
                answered.add(cid2)
            elif client.stats.timeouts > silent:
                why = (f"no answer from pack {client.unit_id} on {plan.where} — check the address; the preview "
                       f"stopped after one silent request")
                break
            _sleep(YIELD_S, job.cancel)                # the line back to the devices between two turns
    finally:
        if bus is not None:
            # judged and closed under the bus's lock: a device that attaches
            # meanwhile counts itself first, or opens the port again after us
            with bus.lock:
                with sv._REG_LOCK:
                    idle = bus.users == 0
                if idle:
                    bus.close()
    warnings = list(plan.warnings) + ([why] if why else [])
    if len(cmds) > len(asked) and not job.cancel.is_set():
        warnings.append(f"{len(asked)} of {len(cmds)} commands were asked within the budget of "
                        f"{plan.max_requests} request(s)")
    values = _seplos_values(plan, client, asked, answered)
    warnings += _common_warnings(values)
    line = dict(plan.line)
    job.answer = {
        'values': values,
        'groups': {'total': len(cmds), 'asked': len(asked), 'answered': len(answered), 'heard': len(answered)},
        'identity': {'template': plan.template.id, 'fingerprint': 'none', 'suggested': None},
        'components': [], 'budget': budget.view(), 'warnings': warnings,
        'load': {'share_now': plan.share_now,
                 'share_added': estimate_added([{'bytes': SEPLOS_FRAME_BYTES, 'interval': iv} for _c, iv in cmds],
                                               line),
                 'basis': _basis(line)},
    }


def _listen_until(job: Job, seconds: float, done: Callable[[], bool]) -> None:
    """Listen up to ``seconds``; stop early once everything was heard."""
    end = time.monotonic() + seconds
    while not job.cancel.is_set():
        left = end - time.monotonic()
        if left <= 0 or done():
            return
        _sleep(min(0.1, left), job.cancel)


def _run_seplos_listen(job: Job) -> None:
    from . import seplos_v2 as sv
    plan = job.plan
    client = sv.SeplosV2Client(_seplos_conn(plan, 'listen'), plan.rows, {}, byte_order=plan.byte_order,
                               device_id='preview')
    if client.config_error:
        raise RuntimeError(client.config_error)
    lst = client._listener
    wanted = {sv.area_of(r.address) for r in plan.rows} & set(sv._DECODERS)
    adr = client.unit_id
    budget = Budget(0)
    lst.observe(adr, client)
    try:
        _listen_until(job, plan.seconds, lambda: bool(wanted) and wanted <= set(client.last_rx_by_cmd))
    finally:
        lst.unobserve(adr, client)
    heard = set(client.last_rx_by_cmd) & wanted
    warnings = list(plan.warnings)
    if lst.open_error:
        warnings.append(f"{lst.name}: {lst.open_error}")
    elif not lst.frames:
        warnings.append(f"{lst.name} was silent for {plan.seconds:g} s — master pack off? A/B swapped? the line?")
    values = _seplos_values(plan, client, set(), heard)
    warnings += _common_warnings(values)
    job.answer = {
        'values': values,
        'groups': {'total': len(wanted), 'asked': 0, 'answered': 0, 'heard': len(heard)},
        'identity': {'template': plan.template.id, 'fingerprint': 'none', 'suggested': None},
        'components': [], 'budget': budget.view(), 'warnings': warnings,
        'load': {'share_now': plan.share_now, 'share_added': 0.0, 'basis': "listening transmits nothing"},
    }


# ── a Modbus tap ────────────────────────────────────────────────────────────

class _Heard:
    """What a tap reader dispatches for one unit, kept as words (the reader
    calls ``_ingest_window``; nothing is published)."""

    def __init__(self):
        self.cache: Dict[Tuple[str, int], int] = {}
        self.windows: List[Tuple[str, int, int]] = []
        self.lock = threading.Lock()

    def _ingest_window(self, addr: int, words: List[int], now: float, table: str = 'holding') -> None:
        with self.lock:
            for i, w in enumerate(words):
                self.cache[(table, addr + i)] = w
            self.windows.append((table, addr, len(words)))

    def _ingest_coils(self, *a, **k) -> None:
        """Bits are not previewed."""


def _run_tap(job: Job) -> None:
    from . import rtu_tap
    plan = job.plan
    windows, skipped = plan_windows(plan.rows, plan.intervals, MODBUS_MAX_READ, 0)
    heard = _Heard()
    reader = rtu_tap.reader_for(plan.src.connection)
    unit = int(plan.conn.get('unit_id', 1) or 0)

    def covered(w: Window) -> bool:
        with heard.lock:
            return any((w.table, r.address) in heard.cache for r in w.rows)
    reader.observe(unit, heard)
    try:
        _listen_until(job, plan.seconds, lambda: bool(windows) and all(covered(w) for w in windows))
    finally:
        reader.unobserve(unit, heard)
    with heard.lock:
        cache = dict(heard.cache)
    n_heard = sum(1 for w in windows if any((w.table, r.address) in cache for r in w.rows))
    decoded = decode_cache([r for w in windows for r in w.rows], cache, {}, plan.byte_order)
    values = [_value(r, *decoded[r.name], address=r.address, register_type=w.table) for w in windows for r in w.rows]
    values += [_value(r, None, 'no_answer', address=r.address, note=why) for r, why in skipped]
    warnings = list(plan.warnings)
    if reader.open_error:
        warnings.append(f"{reader.port}: {reader.open_error}")
    elif not reader.frames:
        warnings.append(f"{reader.port} was silent for {plan.seconds:g} s — master off? A/B swapped? the line?")
    elif not n_heard:
        warnings.append(f"the bus is live but the master did not ask unit {unit} within {plan.seconds:g} s")
    warnings += _common_warnings(values)
    job.answer = {
        'values': values,
        'groups': {'total': len(windows), 'asked': 0, 'answered': 0, 'heard': n_heard},
        'identity': {'template': plan.template.id, 'fingerprint': 'none', 'suggested': None},
        'components': [], 'budget': Budget(0).view(), 'warnings': warnings,
        'load': {'share_now': plan.share_now, 'share_added': 0.0, 'basis': "listening transmits nothing"},
    }


# ── CAN ─────────────────────────────────────────────────────────────────────

def _run_can(job: Job) -> None:
    from . import can_client
    from .can_signal import decode_signal, frame_key, signal_bits
    plan = job.plan
    can = dict(getattr(plan.src, 'can', None) or {})
    rows = [r for r in plan.rows if frame_key(r) is not None and getattr(r, 'start_bit', None) is not None]
    wanted = {frame_key(r) for r in rows}
    last: Dict[Tuple[int, bool], bytes] = {}
    frames = errors = 0
    car = can_client.carrier_for(can)
    car.open()
    ch = str(can.get('channel') or plan.where or 'can0')
    if can_client.listen_only(ch) is not True:
        # asked again once the socket is open: the interface may have changed
        car.close()
        raise RuntimeError(f"{ch} is no longer in listen-only mode — closed before anything was read")
    try:
        end = time.monotonic() + plan.seconds
        while time.monotonic() < end and not job.cancel.is_set():
            f = car.recv()
            if f is None:
                continue
            cid, ext, remote, err, data = f
            if err or remote:
                errors += 1
                continue
            frames += 1
            last[(cid, ext)] = bytes(data)
            if wanted and wanted <= set(last):
                break
    finally:
        car.close()
    values = []
    for r in rows:
        k = frame_key(r)
        data = last.get(k)
        where = {'can_id': f"0x{k[0]:X}"}
        if data is None:
            values.append(_value(r, None, 'no_answer', **where))
            continue
        first = min(signal_bits(int(r.start_bit), int(r.bit_length),
                                str(getattr(r, 'byte_order', '') or 'little').lower())) // 8
        if first >= len(data):
            values.append(_value(r, None, 'no_answer', **where))
            continue
        try:
            raw = decode_signal(data, r)
        except (ValueError, TypeError, OverflowError, struct.error):
            values.append(_value(r, None, 'no_answer', **where))
            continue
        if isinstance(raw, str):
            values.append(_value(r, raw, 'ok', **where))
            continue
        values.append(_value(r, *judge(r, raw), **where))
    warnings = list(plan.warnings)
    if not frames:
        warnings.append(f"{plan.where}: no frame in {plan.seconds:g} s — the bus idle, the bitrate, the wiring?")
    if errors:
        warnings.append(f"{errors} error or remote frame(s) on {plan.where}")
    warnings += _common_warnings(values)
    job.answer = {
        'values': values,
        'groups': {'total': len(wanted), 'asked': 0, 'answered': 0, 'heard': len(wanted & set(last))},
        'identity': {'template': plan.template.id, 'fingerprint': 'none', 'suggested': None},
        'components': [], 'budget': Budget(0).view(), 'warnings': warnings,
        'load': {'share_now': None, 'share_added': 0.0, 'basis': "a receive-only socket transmits nothing"},
    }


# ── HTTP ────────────────────────────────────────────────────────────────────

def _run_http(job: Job) -> None:
    from .http_client import HttpClient
    plan = job.plan
    allow = bool(getattr(job, 'allow_nonlan', False))
    client = HttpClient(dict(plan.src.http or {}), plan.rows, {}, allow_nonlan=allow)
    budget = Budget(plan.max_requests)
    fetch = client._fetch

    def counted():
        budget.take(0)                         # each GET, test_read's retry included
        return fetch()
    client._fetch = counted
    res = client.test_read()
    by = {r.name: r for r in plan.rows}
    values = []
    for v in res.get('values') or []:
        r = by.get(v['name'])
        if r is None:
            continue
        raw = v.get('raw')
        num = raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
        if num is None and isinstance(raw, str):
            try:
                num = float(raw)
            except ValueError:
                num = None
        if raw is None or num is None:
            values.append(_value(r, None, 'no_answer', path=v.get('path', '')))
        else:
            values.append(_value(r, *judge(r, num), path=v.get('path', '')))
    warnings = list(plan.warnings)
    if not res.get('ok'):
        warnings.append(f"{plan.where}: {res.get('message', 'no answer')}")
    warnings += _common_warnings(values)
    got = sum(1 for v in values if v['quality'] != 'no_answer')
    job.answer = {
        'values': values,
        'groups': {'total': 1, 'asked': 1, 'answered': 1 if res.get('ok') else 0, 'heard': 1 if got else 0},
        'identity': {'template': plan.template.id, 'fingerprint': 'none', 'suggested': None},
        'components': [], 'budget': budget.view(), 'warnings': warnings,
        'load': {'share_now': None, 'share_added': None, 'basis': "one HTTP document per interval"},
        'message': res.get('message', ''),
    }


# ── MQTT ────────────────────────────────────────────────────────────────────

def _run_mqtt(job: Job) -> None:
    from . import component_discover as cd
    from .device_runtime import driver_for
    plan = job.plan
    cfg = job.config
    inp = driver_for(cfg, job.templates, plan.dev, plan.src, plan.rows, {}, False)
    got: Dict[str, Any] = {}
    lock = threading.Lock()

    def take(_group, data):
        with lock:
            for row in data.values():
                got[row['register'].name] = row['value']
    inp.publish_callback = take
    link, own = plan.link, plan.link is None
    if own:
        link = cd.open_link(plan.conn)
        if link is None:
            raise RuntimeError(f"{plan.where} could not be reached within {cd.CONNECT_S:g} s")
    taps = []

    def feed(msg):
        inp._on_message(None, None, msg)
    warnings = list(plan.warnings)
    components: List[Dict[str, str]] = []
    try:
        for s in inp.subscriptions():
            link.tap(s, feed)
            taps.append(s)

        def keepalive():
            ka = inp.keepalive
            if not ka:
                return
            topic = inp.resolve_topic(ka['topic'])
            ok, err = link.publish(topic, ka.get('first_payload', ''))
            warnings.append(f"sent the template's keepalive to the broker ({topic}), as the running device "
                            f"would — it asks the device to publish, it changes nothing"
                            if ok else f"the template's keepalive could not be sent: {err}")
        types = cd.discoverable(dict(getattr(plan.template, 'components', None) or {}))
        if types:
            components = cd.listen(link, types, dict(inp.vars or {}), cfg.COMPONENT_INSTANCE_RE, plan.seconds,
                                   before=keepalive, wait=lambda s: _sleep(s, job.cancel))
        else:
            keepalive()
            _sleep(plan.seconds, job.cancel)
    finally:
        for s in taps:
            link.untap(s, feed)
        if own:
            cd.close(link)
    values = []
    with lock:
        heard = dict(got)
    topics = set()
    for r in plan.rows:
        t = inp._reg_topic(r)
        topics.add(t)
        if r.name in heard:
            v = heard[r.name]
            values.append(_value(r, v, quality_of(r, v), path=t))
        else:
            values.append(_value(r, None, 'no_answer', path=t))
    heard_topics = {inp._reg_topic(r) for r in plan.rows if r.name in heard}
    if not heard:
        warnings.append(f"no value on the template's topics within {plan.seconds:g} s — the topic, the "
                        f"variables, retained-only messages (accept_retained is off)?")
    warnings += _common_warnings(values)
    job.answer = {
        'values': values,
        'groups': {'total': len(topics), 'asked': 0, 'answered': 0, 'heard': len(heard_topics)},
        'identity': {'template': plan.template.id, 'fingerprint': 'none', 'suggested': None},
        'components': components, 'budget': Budget(0).view(), 'warnings': warnings,
        'load': {'share_now': None, 'share_added': 0.0, 'basis': "a subscription adds nothing to a bus"},
    }


_RUNNERS.update({'modbus': _run_modbus, 'seplos': _run_seplos, 'seplos_listen': _run_seplos_listen,
                 'tap': _run_tap, 'can': _run_can, 'http': _run_http, 'mqtt': _run_mqtt})
