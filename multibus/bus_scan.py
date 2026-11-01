# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Scan one bus: which unit ids answer, and what each one probably is —
and its passive twin, Listen for addresses (ADR-0031 §2 Address, "Many of
the same"; design §3 2f; FINDINGS L3-F1).

A scan is a job of the read preview's own registry (preview.py), with every
rule a preview keeps, shared rather than forked:

- **Reads only.** Every request goes through ``preview.GuardedModbusClient``
  (FC3/FC4, FC43/14 when asked) and ``preview._in_turn`` (the endpoint's
  turn, the transport lock, the job's deadline, pymodbus' silence count put
  back). The scan's per-question timeout is put on the client for that one
  question and put back, still under the turn.
- **One socket.** On a bus a device of this gateway polls, the scan rides
  that device's running connection (its socket or serial line, its lock,
  its line); a connection of its own only when none does, and then never
  beside a client our bridge reports, a tty another process holds, or —
  when nobody can say — without ``confirm_open``.
- **ADR-0025.** Refused (409) on a bus any device listens on, enabled or
  not, whatever name reaches the bus; Listen for addresses is offered there
  instead. One job per bus (a preview included), a global cap.

Listen for addresses joins the bus's tap reader or Seplos V2 listener (by
its canonical key) as a receive-only watcher of every frame, or opens one
for at most ``preview.listen_s`` seconds; it lists the units heard and the
master's questions. It never transmits, and is refused on a bus a device of
this gateway polls (opening the line beside it would steal its answers).

Recognising a slave uses what the device or its template says, never a list
in code: the Modbus device identification (FC43/14), the SunSpec marker, and
each template's own ``identify`` block (registers that must read given
values). A unit nothing recognises is still listed — the operator picks.
"""
from __future__ import annotations

import logging
import re
import struct
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SUNS = (0x5375, 0x6E53)


def _regs(client, uid: int, address: int, count: int, rtype: str = "holding") -> Optional[List[int]]:
    try:
        fn = client.read_input_registers if rtype == "input" else client.read_holding_registers
        r = fn(address=address, count=count, device_id=uid)
    except Exception:  # noqa: BLE001
        return None
    if r is None or r.isError():
        return None
    return list(r.registers)


def _decode(words: List[int], data_type: str, byte_order: str) -> Optional[float]:
    dt = (data_type or "uint16").lower()
    if dt in ("uint16", "int16", "short"):
        v = words[0]
        return v - 0x10000 if dt != "uint16" and v >= 0x8000 else v
    if len(words) < 2:
        return None
    w = words[:2] if byte_order in ("big", "badc") else words[:2][::-1]
    raw = struct.pack(">HH", *w)
    if byte_order in ("badc", "dcba"):
        raw = bytes([raw[1], raw[0], raw[3], raw[2]])
    fmt = {"uint32": ">I", "int32": ">i", "float": ">f", "float32": ">f"}.get(dt)
    return struct.unpack(fmt, raw)[0] if fmt else None


def _check(value: Optional[float], rule: Dict[str, Any]) -> bool:
    if value is None:
        return False
    if "equals" in rule:
        return value == rule["equals"]
    if "in" in rule:
        return value in rule["in"]
    lo, hi = rule.get("min"), rule.get("max")
    return (lo is None or value >= lo) and (hi is None or value <= hi)


def _fc43(client, uid: int) -> Dict[str, str]:
    try:
        r = client.read_device_information(device_id=uid)
    except Exception:  # noqa: BLE001
        return {}
    info = getattr(r, "information", None) or {}
    dec = lambda b: b.decode(errors="replace").strip() if isinstance(b, (bytes, bytearray)) else str(b)  # noqa: E731
    out = {k: dec(info[i]) for i, k in ((0, "vendor"), (1, "product"), (2, "version")) if i in info}
    return out


def identify(client, uid: int, templates, fc43: bool = True) -> Dict[str, Any]:
    """What this slave says it is, and which templates' fingerprints match.
    ``fc43`` False: the device identification (FC43/14) is not asked."""
    out: Dict[str, Any] = {"unit_id": uid, "fc43": _fc43(client, uid) if fc43 else {}}
    words = _regs(client, uid, 40000, 2)
    out["sunspec"] = bool(words and tuple(words) == SUNS)
    matches = []
    for t in templates:
        idf = getattr(t, "identify", None) or {}
        if not idf:
            continue
        bo = (getattr(t, "protocol", None) or {}).get("byte_order", "big")
        ok = True
        for rule in idf.get("registers") or []:
            dt = rule.get("data_type", "uint16")
            n = 1 if dt in ("uint16", "int16", "short") else 2
            w = _regs(client, uid, rule["address"], n, rule.get("register_type", "holding"))
            if not w or not _check(_decode(w, dt, bo), rule):
                ok = False
                break
        fc = idf.get("fc43") or {}
        if ok and fc:
            info = out["fc43"]
            ok = bool(info) and all(re.search(str(fc[k]), info.get(k, ""), re.I) for k in ("vendor", "product") if fc.get(k))
        if ok and (idf.get("registers") or fc):
            matches.append({"id": t.id, "name": t.name})
    out["matches"] = matches
    out["suggested"] = matches[0]["id"] if len(matches) == 1 else None
    return out


# ── the generic scan job (on the preview's registry) ────────────────────────

from . import preview as _pv  # noqa: E402 — the registry and the guards a scan shares

MAX_UNIT = 247
DEFAULT_RANGE = (1, 32)
TIMEOUT_S = (0.2, 3.0)              # one question's wait; 0.5 s when not said
DEFAULT_TIMEOUT_S = 0.5
PER_UNIT_MAX = 8                    # requests one answering unit may cost (alive, FC43, SunSpec, fingerprints)
SCAN_CAP_S = 900.0                  # no scan asks longer than this
_AUDIT_FOUND = 64


def params(body: Dict[str, Any]) -> Tuple[int, int, float, bool]:
    """(from, to, timeout, fc43) of a request, judged (422)."""
    def num(key, default, kind):
        v = body.get(key, default)
        if v is None or v == '':
            v = default
        if isinstance(v, bool):
            raise _pv.refuse(422, f"{key}: a number")
        try:
            return kind(v)
        except (TypeError, ValueError):
            raise _pv.refuse(422, f"{key}: a number")
    u0 = num('from', DEFAULT_RANGE[0], int)
    u1 = num('to', DEFAULT_RANGE[1], int)
    if not 1 <= u0 <= MAX_UNIT or not 1 <= u1 <= MAX_UNIT:
        raise _pv.refuse(422, f"from/to: unit addresses 1..{MAX_UNIT} (0 is the broadcast address)")
    if u1 < u0:
        raise _pv.refuse(422, "from must not exceed to")
    tmo = num('timeout', DEFAULT_TIMEOUT_S, float)
    if not TIMEOUT_S[0] <= tmo <= TIMEOUT_S[1]:
        raise _pv.refuse(422, f"timeout: {TIMEOUT_S[0]:g}..{TIMEOUT_S[1]:g} s (one question's wait)")
    fc43 = body.get('fc43', True)
    if not isinstance(fc43, bool):
        raise _pv.refuse(422, "fc43: true or false (ask the device identification, FC43/14)")
    return u0, u1, tmo, fc43


def bus_connection(env, bus_id: str) -> Dict[str, Any]:
    """The connection that reaches a bus of ``GET /api/buses`` (404 when it
    is not one): its port and line, a bridge's port, an address."""
    entry = _pv._bus_entry(env, str(bus_id or ''))
    if entry is None:
        raise _pv.Refused(404, {"errors": [f"bus '{bus_id}' is not known to the gateway (GET /api/buses)"]})
    protos = set(entry.get('protocols') or [])
    kind = entry.get('kind')
    if kind == 'can':
        raise _pv.refuse(422, "a CAN bus is heard frame by frame, not scanned: its devices have no unit address")
    if kind == 'serial':
        line = entry.get('line') or {}
        conn: Dict[str, Any] = {'serial_port': entry.get('path') or entry.get('where')}
        if line:
            conn.update({'baudrate': line.get('baud'), 'parity': line.get('parity'),
                         'bytesize': line.get('bytesize'), 'stopbits': line.get('stopbits')})
        conn['protocol'] = 'seplos_v2' if 'seplos_v2' in protos else ('ascii' if protos == {'ascii'} else 'rtu')
        return conn
    if kind == 'bridge' and entry.get('bridge'):
        return {'bridge': entry['bridge']['id'], 'bridge_port': int(entry.get('port') or 0)}
    framing = entry.get('framing')
    proto = 'tcp' if framing == 'mbap' else 'ascii-tcp' if framing == 'ascii' else 'rtu-tcp'
    if 'seplos_v2-tcp' in protos:
        proto = 'seplos_v2-tcp'
    return {'protocol': proto, 'host': entry.get('host'), 'port': int(entry.get('port') or 0)}


def _entry(env, bus: str, strict: bool) -> Dict[str, Any]:
    """The bus's /api/buses entry ({} when nobody uses it). ``strict`` (a
    scan): a list that cannot be read refuses (503) — never a scan with
    nothing known (it would ask the addresses devices have)."""
    try:
        return _pv._bus_entry(env, bus, strict=strict) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("bus scan: the bus list could not be read — %s", e)
        raise _pv.Refused(503, {"errors": [f"cannot read which addresses are in use on this bus ({e}) — "
                                           f"nothing was sent; try again"], "bus": bus})


def _known(env, bus: str, entry: Optional[Dict[str, Any]] = None) -> Dict[int, str]:
    """unit → the device of this gateway that has that address on the bus."""
    entry = entry if entry is not None else (_pv._bus_entry(env, bus) or {})
    out: Dict[int, str] = {}
    for u in entry.get('units_in_use') or []:
        try:
            out.setdefault(int(u.get('unit')), str(u.get('device') or ''))
        except (TypeError, ValueError):
            continue
    return out


def _built(env, conn: Dict[str, Any]):
    raw = {'id': 'bus-scan', 'name': 'bus-scan', 'template': '', 'enabled': True, 'connection': conn}
    built = env.config.preview_devices(device=raw)
    if not built or not (built[0].sources or []):
        raise _pv.refuse(422, "connection: it does not reach a bus")
    return built[0], built[0].sources[0]


def prepare(env, conn: Dict[str, Any], *, mode: str = 'poll', body: Optional[Dict[str, Any]] = None,
            confirm_open: bool = False, edited=None) -> '_pv.Plan':
    """Judge a scan (``mode`` poll) or a Listen for addresses (``mode``
    listen) before anything is sent; raises preview.Refused."""
    from .bus_claims import bus_of, pollers, refusal_detail, where_of
    from .config import CAN_PROTOCOLS, MODBUS_PROTOCOLS, VIRTUAL_MAP_PROTOCOLS
    body = body or {}
    if mode not in ('poll', 'listen'):
        raise _pv.refuse(422, "mode: poll (Scan this bus) or listen (Listen for addresses)")
    if not isinstance(conn, dict) or not conn:
        raise _pv.refuse(422, "connection or bus: required (the bus to scan)")
    cfg = env.config
    conn = {k: v for k, v in conn.items() if k not in ('unit_id', 'template')}
    said = dict(conn)
    if conn.get('bridge'):
        from .bridges import resolve_connection
        conn = resolve_connection(conn, cfg.bridges)
    proto = str(conn.get('protocol', 'tcp') or 'tcp').lower()
    edited = {k for k in (edited if edited is not None else said) if k != 'timeout'}
    if proto in CAN_PROTOCOLS or proto in ('mqtt', 'http', 'none'):
        raise _pv.refuse(422, f"connection.protocol: '{proto}' is not a bus with unit addresses to scan")
    seplos = proto in VIRTUAL_MAP_PROTOCOLS
    if mode == 'poll':
        if seplos:
            raise _pv.refuse(422, "a Seplos V2 bus is not scanned with Modbus questions: use Listen for "
                                  "addresses, or the pack's DIP-switch address")
        if proto == 'rtu_tap':
            raise _pv.refuse(422, "connection.protocol: rtu_tap only listens — use Listen for addresses "
                                  "(mode: listen)")
        if proto not in MODBUS_PROTOCOLS:
            raise _pv.refuse(422, f"connection.protocol: '{proto}' cannot be scanned")
    else:
        if proto in ('tcp', 'udp'):
            raise _pv.refuse(422, "a Modbus TCP device is asked, never heard: use Scan (mode: poll)")
        if proto in ('ascii', 'ascii-tcp'):
            raise _pv.refuse(422, "listening decodes RTU frames only")
        if not seplos and proto not in MODBUS_PROTOCOLS:
            raise _pv.refuse(422, f"connection.protocol: '{proto}' cannot be listened to")
    bus = bus_of(proto, conn)
    if not bus:
        raise _pv.refuse(422, "connection: a serial port, or a host and a port (the bus to scan)")
    where = where_of(proto, conn)
    if mode == 'poll':
        ids = _pv._any_listener(env, bus)
        if ids:
            raise _pv.Refused(409, {**refusal_detail("A scan", bus, ids, where),
                                    "hint": "Listen for addresses (mode: listen) is the discovery on a listened bus"})
    else:
        ids = pollers(cfg, bus, env.templates)
        if ids:
            raise _pv.refuse(409, f"{where} is polled by {', '.join(ids)}: listening there would open the line "
                                  f"beside {'its' if len(ids) == 1 else 'their'} connection. Nothing was opened — "
                                  f"use Scan this bus (it asks through {ids[0]}'s connection)", bus=bus, pollers=ids)
    u0, u1, tmo, fc43 = params(body) if mode == 'poll' else (1, 1, 0.0, False)
    if mode == 'listen':
        extra = {'protocol': 'rtu_tap'} if not seplos else {'mode': 'listen'}
        conn, said = {**conn, **extra}, {**said, **extra}
    entry = _entry(env, bus, strict=(mode == 'poll'))
    known = _known(env, bus, entry)
    if env.validate is not None:
        # the connection as a save judges it (422): as sent, a bridge named by
        # its id, at an address nobody has (the scan asks them all: a taken
        # one is not a refusal here)
        free = next((u for u in range(1, MAX_UNIT + 1) if u not in known), u0)
        env.validate({**{k: v for k, v in said.items() if k != 'timeout'}, 'unit_id': free}, '')
    conn = {**conn, 'unit_id': u0}
    if mode == 'poll':
        conn['timeout'] = tmo
    dev, src = _built(env, conn)
    budget = cfg.preview_budget()
    n = (u1 - u0 + 1) if mode == 'poll' else 0
    plan = _pv.Plan(kind='scan' if mode == 'poll' else 'scan_listen', mode=mode, transport='own', key=bus,
                    where=where, template=None, rows=[], intervals={}, conn=conn, dev=dev, src=src,
                    seconds=0.0, max_requests=n * PER_UNIT_MAX, listen_s=int(budget['listen_s']))
    plan.line = {'baud': int(conn.get('baudrate') or 9600), 'serial': proto not in ('tcp', 'udp')}
    plan.extra = {'bus': bus, 'from': u0, 'to': u1, 'timeout': tmo, 'fc43': fc43, 'known': known,
                  'reader': 'seplos' if seplos else 'tap'}
    confirm = confirm_open is True
    if mode == 'poll':
        _pv._prepare_modbus(env, plan, proto, bus, confirm, edited)
        gap = float(getattr(getattr(plan.holder_mc, 'config', None), 'endpoint_min_gap_s', 0.0) or 0.0)
        wait = tmo
        if plan.transport == 'shared':
            # the holder's own wait is never shortened: a late answer lands in
            # the scan's question, not in the device's next one
            try:
                wait = max(tmo, float((plan.shared_line or {}).get('timeout') or 0))
            except (TypeError, ValueError):
                wait = tmo
            plan.extra['wait'] = wait
        # each question: its wait, the grace for a late answer, the hand-back
        deadline = min(SCAN_CAP_S, n * (wait + DRAIN_GRACE_S + gap + _pv.YIELD_S) * 3 + _pv.POLL_SLACK_S)
        if plan.transport == 'shared':
            _slow(env, plan, entry, wait, body.get('confirm_slow') is True)
        plan.extra['deadline_s'] = deadline
        plan.wall = deadline + _pv.WALL_MARGIN_S
        return plan
    plan.seconds = _pv._seconds(body.get('seconds'), plan.listen_s)
    kind = 'seplos_listen' if seplos else 'tap'
    if conn.get('host') and not conn.get('serial_port'):
        _pv._lan(str(conn.get('host')), env, 'connection.host')
        if not _pv._reader_running(kind, src.connection):
            # a reader of its own over the network is a connection to the bridge too
            _pv._check_open(env, plan, str(conn.get('host')), int(conn.get('port') or 0), bus, confirm)
    elif conn.get('serial_port') and not _pv._reader_running(kind, src.connection):
        _pv._check_tty(env, plan, str(conn.get('serial_port')), bus, confirm)
    plan.transport = 'tap'
    plan.share_now = _pv._share(_pv._bus_entry(env, bus))
    return plan


def _reads_per_cycle(env, dev_id: str) -> int:
    """How many reads one poll cycle of a device makes (its selection, in
    windows as a poll groups them); 1 when it cannot be said."""
    try:
        dev = env.config.get_device(dev_id)
        rows, groups = env.config.load_device_registers(dev)
        tpl = env.templates.get(dev.template) if dev.template else None
        iv = _pv.group_intervals(rows or [], getattr(env.config, 'poll_groups', None),
                                 getattr(tpl, 'poll_groups', None), groups)
        windows, _skipped = _pv.plan_windows(rows or [], iv, _pv.MODBUS_MAX_READ,
                                             int(getattr(dev.connection, 'max_gap', 10) or 0))
        return max(1, len(windows))
    except Exception:  # noqa: BLE001 — an estimate: one read at least
        return 1


def _slow(env, plan, entry: Dict[str, Any], wait: float, confirmed: bool) -> None:
    """A scan rides the devices' own line: each of their reads may wait for
    one of the scan's questions (its wait + the late-answer grace). Said as a
    warning on every shared scan; refused (409) without ``confirm_slow``
    when an armed rule reads or writes a device on this bus."""
    devs = [d['id'] for d in (entry.get('devices') or []) if d.get('enabled') and d.get('role') == 'master']
    per_read = round(wait + DRAIN_GRACE_S, 2)
    k = sum(_reads_per_cycle(env, d) for d in devs) or 1
    worst = round(k * per_read, 1)
    slow = {'devices': devs, 'reads_per_cycle': k, 'per_read_s': per_read, 'worst_per_cycle_s': worst}
    text = (f"the scan shares the line with {', '.join(devs) or 'its devices'}: each of their reads may wait up "
            f"to {per_read:g} s for a question of the scan — a poll cycle of {k} read(s) up to {worst:g} s slower")
    try:
        armed = env.armed() if env.armed is not None else {}
        rules = sorted(rid for rid, ds in (armed or {}).items() if set(ds) & set(devs))
    except Exception:  # noqa: BLE001 — not knowing is not "no rule": refuse
        rules = ['(the rules could not be read)']
    slow['rules'] = rules
    plan.extra['slow'] = slow
    if rules and not confirmed:
        raise _pv.refuse(409, f"{text}. Armed rule(s) {', '.join(rules)} read or drive a device on this bus — a "
                              f"slower poll delays them. Nothing was sent; send confirm_slow: true to scan anyway",
                         bus=plan.key, slow=slow, rules=rules)
    plan.warnings.append(text + (f" (armed rule(s) {', '.join(rules)}: confirmed)" if rules else ''))
    plan.extra['confirm_slow'] = bool(rules and confirmed)


class ScanJob(_pv.Job):
    """A scan's progress and answer, as ``GET /api/bus-scan/{job}`` gives it
    (a superset of the bridge scan's earlier shape: state, done, total,
    found[{unit_id, fc43, sunspec, matches, suggested, device}])."""

    def __init__(self, plan, env=None):
        super().__init__(plan, env)
        x = plan.extra
        self.total = (x['to'] - x['from'] + 1) if plan.mode == 'poll' else 0
        self.done = 0
        self.found: List[Dict[str, Any]] = []
        self.heard: Dict[str, Any] = {}
        self.budget = _pv.Budget(plan.max_requests)

    def view(self) -> Dict[str, Any]:
        p = self.plan
        state = self.state
        if state == 'running' and self.cancel.is_set():
            state = 'cancelling'
        out = {'job': self.id, 'id': self.id, 'state': state, 'mode': p.mode, 'transport': p.transport,
               'where': p.where, 'bus': p.extra.get('bus', p.key), 'shared_with': p.shared_with,
               **({'shared_line': dict(p.shared_line)} if p.shared_line else {}),
               **({'confirm_open': True} if p.confirm_open else {}),
               'started': round(self.started, 3), 'finished': round(self.finished, 3) if self.finished else None,
               'done': self.done, 'total': self.total, 'found': [dict(f) for f in self.found],
               **({'slow': dict(p.extra['slow'])} if p.extra.get('slow') else {}),
               'budget': self.budget.view(),
               'warnings': list(p.warnings) + list((self.answer or {}).get('warnings') or []),
               'error': self.error}
        if p.mode == 'poll':
            out.update({'from': p.extra['from'], 'to': p.extra['to'], 'timeout': p.extra.get('wait') or p.extra['timeout'],
                        'fc43': p.extra['fc43']})
        else:
            out.update({'seconds': p.seconds, 'heard': dict(self.heard)})
        return out


class _Wait:
    """On a bus a running device holds, the wait its client is given for a
    scan's question: never shorter than the device's own (a late answer must
    land inside the wait it was asked with, never in the device's next turn),
    put back after the question, still under the turn."""

    def __init__(self, client, seconds: Optional[float]):
        self.client, self.seconds, self.saved = client, seconds, []

    def __enter__(self):
        if self.seconds is None:
            return self
        for cp in (getattr(self.client, 'comm_params', None),
                   getattr(getattr(self.client, 'transaction', None), 'comm_params', None)):
            t = getattr(cp, 'timeout_connect', None)
            if isinstance(t, (int, float)) and not isinstance(t, bool) and t < self.seconds:
                self.saved.append((cp, t))
                cp.timeout_connect = self.seconds
        return self

    def __exit__(self, *exc):
        for cp, t in self.saved:
            cp.timeout_connect = t
        return False


DRAIN_GRACE_S = 0.3                 # after a silent question: how long a late answer may still come


def drain(client, grace: float = DRAIN_GRACE_S) -> int:
    """After a question went unanswered, still under the bus lock: wait for an
    answer that comes late, then empty the receive side — so it never lands in
    the next question (ours or a device's). Returns the bytes thrown away."""
    sock = getattr(client, 'socket', None)
    if sock is None:
        return 0
    time.sleep(max(0.0, float(grace)))
    n = 0
    try:
        if hasattr(sock, 'reset_input_buffer'):           # a serial line (pyserial)
            n = int(getattr(sock, 'in_waiting', 0) or 0)
            sock.reset_input_buffer()
            return n
        prev = sock.gettimeout()
        sock.setblocking(False)
        try:
            while True:
                try:
                    chunk = sock.recv(4096)
                except (BlockingIOError, InterruptedError):
                    break
                if not chunk:
                    break
                n += len(chunk)
        finally:
            sock.settimeout(prev)
    except OSError:
        pass
    if n:
        logger.info("bus scan: %d late byte(s) thrown away before the turn was handed back", n)
    return n


class _TurnClient:
    """What ``identify`` asks with: each request one turn on the bus, through
    the guard, within the unit's share of the budget; the same read asked
    twice (two templates' fingerprints) is answered from the first. After
    every request the line goes back to the devices (``hand_back``), and a
    cancel or a standby stops the scan before its next request."""

    def __init__(self, job, mc, guard, deadline: float, wait: Optional[float], cap: int, hand_back: float):
        self.job, self.mc, self.guard, self.deadline = job, mc, guard, deadline
        self.wait, self.cap, self.hand_back = wait, cap, hand_back
        self.n = 0
        self.cache: Dict[Tuple, Any] = {}
        self.refused: Optional[Exception] = None
        self.stop = ''

    def _halt(self) -> bool:
        if self.job.cancel.is_set() or self.job.abandoned:
            self.stop = self.stop or 'cancelled'
        elif not _pv._wire_allowed():
            self.stop = self.stop or 'standby'
        return bool(self.stop)

    def ask(self, key, send):
        from pymodbus.pdu import ExceptionResponse
        if key in self.cache:
            return self.cache[key]
        if self.n >= self.cap or self._halt():
            raise RuntimeError("this unit's share of the scan is spent")
        self.n += 1

        def turn(g):
            with _Wait(g.inner, self.wait):
                try:
                    r = send(g)
                except _pv.GuardRefusal:
                    raise
                except Exception:
                    drain(g.inner)                # a silence: what it says late is thrown away
                    raise
            if r is None or (r.isError() and not isinstance(r, ExceptionResponse)):
                drain(g.inner)
            return r
        try:
            r, why = _pv._in_turn(self.mc, self.guard, turn, self.deadline)
        except _pv.GuardRefusal as e:
            self.refused = e                      # said by the runner: never swallowed by identify
            raise
        finally:
            if self.hand_back > 0:
                _pv._sleep(self.hand_back, self.job.cancel)   # the line back to the devices
        if why:
            if why in ('deadline', 'no_connection', 'bus_unwell'):
                self.stop = why
            raise RuntimeError(why)
        self._halt()
        self.cache[key] = r
        return r

    def read_holding_registers(self, address, count=1, device_id=0):
        return self.ask(('h', address, count, device_id),
                        lambda g: g.read_holding_registers(address, count=count, device_id=device_id))

    def read_input_registers(self, address, count=1, device_id=0):
        return self.ask(('i', address, count, device_id),
                        lambda g: g.read_input_registers(address, count=count, device_id=device_id))

    def read_device_information(self, device_id=0):
        return self.ask(('43', device_id), lambda g: g.read_device_information(device_id=device_id))


def _identifiable(job) -> List[Any]:
    reg = getattr(job, 'templates', None)
    return [t for t in (reg.list() if reg is not None else []) if getattr(t, 'identify', None)]


def _known_row(uid: int, device: str) -> Dict[str, Any]:
    """An address a device of this gateway already has: listed, never asked."""
    return {'unit_id': uid, 'answer': 'known', 'asked': False, 'fc43': {}, 'sunspec': False, 'matches': [],
            'suggested': None, 'suggested_name': '', 'device': device, 'requests': 0}


def _run_scan(job: ScanJob) -> None:
    from pymodbus.pdu import ExceptionResponse

    from .modbus_client import ModbusConnection
    plan = job.plan
    x = plan.extra
    guard = _pv.GuardedModbusClient(None, job.budget, _pv.MODBUS_MAX_READ, allow_fc43=bool(x['fc43']))
    own = plan.holder_mc is None
    mc = ModbusConnection(_pv._modbus_config(plan), trace_label=f"bus-scan:{plan.where}") if own else plan.holder_mc
    gap = float(getattr(mc.config, 'endpoint_min_gap_s', 0.0) or 0.0)
    hand_back = max(gap, _pv.YIELD_S if not own else 0.0)
    deadline = time.monotonic() + float(x['deadline_s'])
    templates = _identifiable(job)
    why = ''
    try:
        for uid in range(x['from'], x['to'] + 1):
            if job.cancel.is_set() or job.abandoned:
                break
            if not _pv._wire_allowed():
                why = "the gateway became standby — the scan stopped (its partner is on the wire)"
                break
            if time.monotonic() > deadline:
                why = "the scan ran out of time — it stopped"
                break
            if uid in x['known']:
                if not job.abandoned:
                    job.found.append(_known_row(uid, x['known'][uid]))
                    job.done += 1
                continue
            tc = _TurnClient(job, mc, guard, deadline, x.get('wait'), PER_UNIT_MAX, hand_back)
            try:
                r = tc.read_holding_registers(0, count=1, device_id=uid)
            except _pv.GuardRefusal:
                raise
            except Exception:  # noqa: BLE001 — silence, or the bus not ours now
                r = None
            if tc.stop == 'no_connection':
                why = f"no connection to {plan.where} (or no turn on the shared bus) — the scan stopped at unit {uid}"
                break
            if tc.stop == 'bus_unwell':
                who = plan.shared_with or 'a device'
                why = f"{who} on this bus is missing answers — scan stopped at unit {uid}; retry when it reads again"
                break
            if tc.stop == 'deadline':
                why = "the scan ran out of time — it stopped"
                break
            if tc.stop == 'standby':
                why = "the gateway became standby — the scan stopped (its partner is on the wire)"
                break
            if tc.stop == 'cancelled':
                break
            alive = r is not None and (isinstance(r, ExceptionResponse) or not r.isError())
            if alive:
                info = identify(tc, uid, templates, fc43=bool(x['fc43']))
                if tc.refused is not None:
                    raise tc.refused
                info['answer'] = 'exception' if isinstance(r, ExceptionResponse) else 'data'
                info['asked'] = True
                sid = info.get('suggested')
                st = next((t for t in templates if t.id == sid), None) if sid else None
                info['suggested_name'] = getattr(st, 'name', '') if st else ''
                info['device'] = ''
                info['requests'] = tc.n
                if not job.abandoned:
                    job.found.append(info)
            if tc.stop == 'standby':
                why = "the gateway became standby — the scan stopped (its partner is on the wire)"
            if not job.abandoned:
                job.done += 1
            if tc.stop in ('standby', 'cancelled'):
                break
    finally:
        if own:
            mc.disconnect()                           # the holder's connection is never ours to close
    if why:
        job.answer = {'warnings': [why]}


class _Watcher:
    """Every frame a reader hears, kept per unit (Listen for addresses)."""

    def __init__(self, seplos: bool):
        self.seplos = seplos
        self.lock = threading.Lock()
        self.units: Dict[int, Dict[str, Any]] = {}
        self.frames = 0
        self.writes = 0

    def _heard(self, unit, fc, kind, detail, now) -> None:
        with self.lock:
            self.frames += 1
            if unit is None:
                return
            u = self.units.setdefault(int(unit), {'asked': 0, 'answered': 0, 'exceptions': 0, 'writes': 0,
                                                  'fcs': set(), 'pattern': {}, 'times': []})
            what = ''
            if fc is not None:
                u['fcs'].add(int(fc))
                what = f"0x{int(fc):02X}" if self.seplos else f"FC{fc} {detail}".strip()
            if kind in ('request', 'command'):
                u['asked'] += 1
                u['times'].append(now)
                if what:
                    u['pattern'][what] = u['pattern'].get(what, 0) + 1
            elif kind in ('response', 'inferred'):
                u['answered'] += 1
                if what and (self.seplos or kind == 'inferred'):
                    u['pattern'].setdefault(what, 0)      # what it answered (a Seplos command, a master's block)
            elif kind == 'exception':
                u['exceptions'] += 1
            elif kind == 'write':
                u['writes'] += 1
                self.writes += 1

    def rows(self, known: Dict[int, str]) -> List[Dict[str, Any]]:
        out = []
        with self.lock:
            for unit in sorted(self.units):
                u = self.units[unit]
                t = u['times']
                gaps = sorted(b - a for a, b in zip(t, t[1:]) if b > a)
                every = round(gaps[len(gaps) // 2], 2) if gaps else None
                pat = sorted(u['pattern'], key=lambda k: (-u['pattern'][k], k))[:6]
                out.append({'unit_id': unit, 'asked': u['asked'], 'answered': u['answered'],
                            'exceptions': u['exceptions'], 'writes': u['writes'], 'fcs': sorted(u['fcs']),
                            'pattern': pat, 'every_s': every, 'device': known.get(unit, ''),
                            'fc43': {}, 'sunspec': False, 'matches': [], 'suggested': None})
        return out


def _run_listen(job: ScanJob) -> None:
    plan = job.plan
    seplos = plan.extra['reader'] == 'seplos'
    w = _Watcher(seplos)
    if seplos:
        from . import seplos_v2 as sv
        reader = sv.listener_for(plan.src.connection)
        every_frame, name = sv.ALL_UNITS, reader.name
    else:
        from . import rtu_tap
        reader = rtu_tap.reader_for(plan.src.connection)
        every_frame, name = rtu_tap.ALL_UNITS, reader.port
    reader.observe(every_frame, w)
    try:
        end = time.monotonic() + plan.seconds
        while not job.cancel.is_set():
            left = end - time.monotonic()
            if left <= 0:
                break
            if not job.abandoned:
                job.found = w.rows(plan.extra['known'])
            _pv._sleep(min(0.25, left), job.cancel)
    finally:
        reader.unobserve(every_frame, w)
    job.found = w.rows(plan.extra['known'])
    warnings = []
    if reader.open_error:
        warnings.append(f"{name}: {reader.open_error}")
    elif not w.frames:
        warnings.append(f"{name} was silent for {plan.seconds:g} s — master off? A/B swapped? the line?")
    elif not job.found:
        warnings.append(f"{name} is live, but no frame named a unit address")
    # the master's rhythm: the shortest repeat of any unit's questions
    every = [f['every_s'] for f in job.found if f.get('every_s')]
    job.heard = {'frames': w.frames, 'units': len(job.found), 'writes_seen': w.writes,
                 'cycle_s': min(every) if every else None}
    job.answer = {'warnings': warnings}


_pv._RUNNERS['scan'] = _run_scan
_pv._RUNNERS['scan_listen'] = _run_listen


def start(env, plan, on_done=None) -> ScanJob:
    """Run a prepared scan in the preview's registry (409 while a preview or
    a scan of that bus runs; 429 past the global cap)."""
    return _pv.start(env, plan, on_done=on_done, job_cls=ScanJob)


def get(job_id: str) -> Optional[Dict[str, Any]]:
    return _pv.get(job_id, scans=True)


def cancel(job_id: str) -> Optional[bool]:
    return _pv.cancel(job_id, scans=True)


def audit_detail(job: ScanJob) -> Dict[str, Any]:
    """What a scan's audit line keeps: how it ran and what answered."""
    p = job.plan
    b = job.budget.view()
    return {"mode": p.mode, "transport": p.transport, "shared_with": p.shared_with,
            "confirm_open": p.confirm_open, "confirm_slow": bool(p.extra.get('confirm_slow')),
            **({"slow": p.extra['slow']} if p.extra.get('slow') else {}),
            **({"range": [p.extra.get('from'), p.extra.get('to')]} if p.mode == 'poll' else {"seconds": p.seconds}),
            "found": [f['unit_id'] for f in job.found][:_AUDIT_FOUND],
            "requests": b.get('requests', 0), **({"error": job.error} if job.error else {})}
