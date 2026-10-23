# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Sequences — several commands in order, with waits and checks, possibly
across devices (rules phase 3 §4):

    open the valve → wait until it is open (30 s at most) → start the pump
    → after 10 s, check the motor draws current — else undo, in reverse

Every command goes through the one write path (``run_command``): its gates,
bounds, check after write and audit, the channel's referee and protection.
A sequence speaks as an outside writer: it pauses the channel's lower rules
for its run, and a safety rule holding a channel stops it.

Definitions live in ``config/sequences.yaml``. A run is in memory: one per
sequence at a time, cancellable; a dry run plans every frame and writes
nothing.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from . import expressions
from . import role as gateway_role
from .commands import CommandDef, ExprError, ParamDef, evaluate, normalize_params

logger = logging.getLogger(__name__)

MAX_STEPS = 50
MAX_DURATION_S = 3600.0
MAX_WAIT_S = 3600.0
_ID = __import__('re').compile(r'^[a-z][a-z0-9_-]{0,47}$')
STEP_KINDS = ('command', 'wait_s', 'wait_until', 'check')


# ── definitions ──────────────────────────────────────────────────────────────

@dataclass
class SequenceDef:
    id: str
    label: str = ''
    params: Dict[str, ParamDef] = field(default_factory=dict)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    on_abort: List[Dict[str, Any]] = field(default_factory=list)
    max_duration_s: float = 600.0

    def shell(self) -> CommandDef:
        """The parameters as a command's, to check them the same way."""
        return CommandDef(name=self.id, params=self.params)

    def devices(self) -> List[str]:
        return sorted({s['command']['device'] for s in self.steps + self.on_abort if 'command' in s})


def parse_sequence(raw: Dict[str, Any]) -> SequenceDef:
    params = {}
    for pn, pd in (raw.get('params') or {}).items():
        pd = pd or {}
        params[pn] = ParamDef(name=pn, label=str(pd.get('label', '') or ''), unit=str(pd.get('unit', '') or ''),
                              min=pd.get('min'), max=pd.get('max'), default=pd.get('default'),
                              required=bool(pd.get('required', False)), allowed=pd.get('allowed'))
    return SequenceDef(id=str(raw.get('id', '')), label=str(raw.get('label', '') or ''), params=params,
                       steps=[dict(s) for s in raw.get('steps') or [] if isinstance(s, dict)],
                       on_abort=[dict(s) for s in raw.get('on_abort') or [] if isinstance(s, dict)],
                       max_duration_s=float(raw.get('max_duration_s', 600) or 600))


def _num(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def validate_sequence(raw: Any, *, devices: Callable[[str], Optional[List[str]]] = lambda d: None,
                      validate_expr: Callable[[str], Optional[str]] = lambda e: None) -> List[str]:
    """Errors in words. ``devices(id)`` → the commands that device offers
    (None: no such device); ``validate_expr`` checks an expression."""
    if not isinstance(raw, dict):
        return ["a sequence is an object {id, steps}"]
    errs: List[str] = []
    sid = str(raw.get('id', '') or '')
    if not _ID.match(sid):
        errs.append("id: a-z 0-9 - _, starts with a letter, at most 48")
    md = raw.get('max_duration_s', 600)
    if _num(md) is None or not (1 <= _num(md) <= MAX_DURATION_S):
        errs.append(f"max_duration_s: 1..{int(MAX_DURATION_S)} seconds")
    params = raw.get('params') or {}
    if not isinstance(params, dict):
        errs.append("params: an object {name: {min, max, default}}")
        params = {}
    sample = {}
    for pn, pd in params.items():
        if not isinstance(pd, dict):
            errs.append(f"params.{pn}: an object")
            continue
        sample[pn] = pd.get('default', pd.get('min', 0)) if _num(pd.get('default', pd.get('min', 0))) is not None else 0

    def one(where: str, s: Any, only_commands: bool = False) -> None:
        if not isinstance(s, dict):
            errs.append(f"{where}: an object")
            return
        kinds = [k for k in STEP_KINDS if k in s]
        if len(kinds) != 1:
            errs.append(f"{where}: exactly one of {', '.join(STEP_KINDS)}")
            return
        k = kinds[0]
        if only_commands and k != 'command':
            errs.append(f"{where}: on_abort holds commands only")
            return
        if k == 'command':
            c = s['command']
            if not isinstance(c, dict) or not c.get('device') or not c.get('name'):
                errs.append(f"{where}.command: {{device, name, params}}")
                return
            offered = devices(str(c['device']))
            if offered is None:
                errs.append(f"{where}.command: no device '{c['device']}'")
            elif c['name'] not in offered:
                errs.append(f"{where}.command: '{c['device']}' offers no enabled command '{c['name']}'")
            for pk, pv in (c.get('params') or {}).items():
                try:
                    evaluate(pv, sample)
                except (ExprError, TypeError):
                    errs.append(f"{where}.command.params.{pk}: {pv!r} — a number or ${{param}} of this sequence")
        elif k == 'wait_s':
            if _num(s['wait_s']) is None or not (0 < _num(s['wait_s']) <= MAX_WAIT_S):
                errs.append(f"{where}.wait_s: 0..{int(MAX_WAIT_S)} seconds")
        else:
            e = validate_expr(str(s[k] or ''))
            if not str(s[k] or '').strip() or e:
                errs.append(f"{where}.{k}: {e or 'an expression'}")
            if k == 'wait_until' and (_num(s.get('timeout_s')) is None or not (0 < _num(s.get('timeout_s')) <= MAX_WAIT_S)):
                errs.append(f"{where}.timeout_s: 0..{int(MAX_WAIT_S)} seconds (how long to wait)")
            if s.get('else', 'abort') not in ('abort', 'continue'):
                errs.append(f"{where}.else: abort or continue")
    steps = raw.get('steps')
    if not isinstance(steps, list) or not steps:
        errs.append("steps: at least one step")
    else:
        if len(steps) > MAX_STEPS:
            errs.append(f"steps: at most {MAX_STEPS}")
        for i, s in enumerate(steps):
            one(f"steps[{i}]", s)
    for i, s in enumerate(raw.get('on_abort') or []):
        one(f"on_abort[{i}]", s, only_commands=True)
    return errs


class SequenceStore:
    """``config/sequences.yaml``: a list under ``sequences:``."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.load_error: Optional[str] = None

    def load(self) -> List[Dict]:
        self.load_error = None
        try:
            data = yaml.safe_load(self.path.read_text(encoding='utf-8')) or {}
        except FileNotFoundError:
            return []
        except Exception as e:  # noqa: BLE001
            self.load_error = str(e)
            logger.error("sequences.yaml unreadable: %s", e)
            return []
        items = data.get('sequences') if isinstance(data, dict) else None
        return [dict(x) for x in items or [] if isinstance(x, dict)]

    def save(self, items: List[Dict]) -> None:
        tmp = self.path.with_suffix('.yaml.tmp')
        tmp.write_text(yaml.safe_dump({'sequences': items}, sort_keys=False, allow_unicode=True), encoding='utf-8')
        tmp.replace(self.path)


# ── running ──────────────────────────────────────────────────────────────────

class Cancelled(Exception):
    pass


class SequenceRunner:
    """Runs sequences, one run per sequence at a time and one sequence per
    device at a time. ``run_command(device, name, params, *, who, via,
    dry_run, override_s) -> (http_status, result)`` is the gateway's write
    path; ``resolve()`` a fresh value resolver (the rules' one)."""

    def __init__(self, *, run_command: Callable, resolver_factory: Callable, audit=None, mqtt=None,
                 sleep: Callable[[float], None] = time.sleep, mono: Callable[[], float] = time.monotonic,
                 clock: Callable[[], float] = time.time):
        self._run_command = run_command
        self._resolver_factory = resolver_factory
        self.audit, self.mqtt = audit, mqtt
        self._sleep, self._mono, self._clock = sleep, mono, clock
        self._lock = threading.RLock()
        self.running: Dict[str, Dict[str, Any]] = {}          # id → the live run
        self.runs: Dict[str, deque] = {}                       # id → the last runs, newest first

    # ── the public side ──
    def busy_with(self, seq: SequenceDef) -> Optional[str]:
        with self._lock:
            if seq.id in self.running:
                return f"'{seq.id}' is already running"
            mine = set(seq.devices())
            for oid, run in self.running.items():
                shared = mine & set(run['devices'])
                if shared:
                    return f"device {sorted(shared)[0]} is in sequence '{oid}' now"
        return None

    def start(self, seq: SequenceDef, params: Dict[str, Any], *, who: str, via: str,
              dry_run: bool = False, wait: bool = False) -> Tuple[int, Dict[str, Any]]:
        try:
            norm = normalize_params(seq.shell(), dict(params or {}))
        except ValueError as e:
            return 422, {'status': 'rejected', 'reason': str(e)}
        if not dry_run:
            if not gateway_role.wire_allowed():
                return 503, {'status': 'rejected', 'reason': 'standby gateway: sequences run on the active one'}
            why = self.busy_with(seq)
            if why:
                return 409, {'status': 'rejected', 'reason': why}
        run = {'sequence': seq.id, 'params': norm, 'by': who, 'via': via, 'dry_run': dry_run,
               'status': 'running', 'started': round(self._clock(), 3), 'step': 0, 'steps': [],
               'devices': seq.devices(), '_cancel': threading.Event(), '_deadline': self._mono() + seq.max_duration_s}
        if dry_run:
            self._execute(seq, run)
            return 200, self._public(run)
        with self._lock:
            self.running[seq.id] = run
        self._publish(seq.id, run)
        if wait:
            self._execute(seq, run)
        else:
            threading.Thread(target=self._execute, args=(seq, run), name=f"seq-{seq.id}", daemon=True).start()
        return 202, self._public(run)

    def cancel(self, sid: str, *, who: str) -> bool:
        with self._lock:
            run = self.running.get(sid)
        if run is None:
            return False
        run['cancelled_by'] = who
        run['_cancel'].set()
        return True

    def view(self, sid: str) -> Dict[str, Any]:
        with self._lock:
            live = self.running.get(sid)
            last = list(self.runs.get(sid, []))
        return {'running': self._public(live) if live else None, 'last': [self._public(r) for r in last[:10]]}

    # ── the run ──
    @staticmethod
    def _public(run: Optional[Dict]) -> Optional[Dict]:
        return None if run is None else {k: v for k, v in run.items() if not k.startswith('_')}

    def _note(self, run: Dict, what: str, ok: bool, detail: str = '', **extra) -> None:
        run['steps'].append({'ts': round(self._clock(), 3), 'what': what, 'ok': ok, 'detail': detail, **extra})

    def _left(self, run: Dict) -> float:
        return run['_deadline'] - self._mono()

    def _wait(self, run: Dict, seconds: float) -> None:
        end = self._mono() + seconds
        while True:
            if run['_cancel'].is_set():
                raise Cancelled()
            left = end - self._mono()
            if left <= 0:
                return
            if self._left(run) <= 0:
                raise TimeoutError('the sequence ran past max_duration_s')
            self._sleep(min(0.5, left))

    def _truth(self, expr: str) -> Optional[bool]:
        resolve = self._resolver_factory({})
        try:
            return bool(expressions.evaluate(expr, lambda n: resolve(expressions.unprepare_ref(n))))
        except expressions.MissingValue:
            return None

    def _command(self, seq: SequenceDef, run: Dict, step: Dict) -> Tuple[bool, str]:
        c = step['command']
        try:
            params = {k: evaluate(v, run['params']) for k, v in (c.get('params') or {}).items()}
        except (ExprError, TypeError) as e:
            return False, f"params: {e}"
        override = max(1, int(min(self._left(run), MAX_DURATION_S))) if not run['dry_run'] else 0
        if override:
            params['override_s'] = override
        code, res = self._run_command(str(c['device']), str(c['name']), params,
                                      who=f"sequence {seq.id} ({run['by']})", via=f"sequence:{seq.id}",
                                      dry_run=run['dry_run'])
        status = (res or {}).get('status')
        ok = status in ('success', 'unverified', 'dry_run')
        detail = f"{c['device']} {c['name']} {params if not override else {k: v for k, v in params.items() if k != 'override_s'}}"
        return ok, detail + f" · {status}" + (f" · {res.get('reason')}" if res.get('reason') else '')

    def _execute(self, seq: SequenceDef, run: Dict) -> None:
        reason = ''
        try:
            for i, step in enumerate(seq.steps):
                run['step'] = i
                if run['_cancel'].is_set():
                    raise Cancelled()
                if self._left(run) <= 0:
                    raise TimeoutError('the sequence ran past max_duration_s')
                if 'command' in step:
                    ok, detail = self._command(seq, run, step)
                    self._note(run, 'command', ok, detail)
                    if not ok:
                        raise RuntimeError(f"step {i + 1}: {detail}")
                elif 'wait_s' in step:
                    if not run['dry_run']:
                        self._wait(run, float(step['wait_s']))
                    self._note(run, 'wait', True, f"{step['wait_s']} s")
                elif 'wait_until' in step:
                    if run['dry_run']:
                        self._note(run, 'wait_until', True, f"{step['wait_until']} (now: {self._truth(step['wait_until'])})")
                        continue
                    end = self._mono() + float(step['timeout_s'])
                    while self._truth(step['wait_until']) is not True:
                        if self._mono() >= end:
                            if step.get('else', 'abort') == 'continue':
                                break
                            raise RuntimeError(f"step {i + 1}: {step['wait_until']} not true after {step['timeout_s']} s")
                        self._wait(run, min(1.0, max(0.0, end - self._mono())))
                    self._note(run, 'wait_until', True, step['wait_until'])
                else:
                    truth = self._truth(step['check'])
                    self._note(run, 'check', bool(truth), f"{step['check']} → {truth}")
                    if truth is not True and step.get('else', 'abort') == 'abort' and not run['dry_run']:
                        raise RuntimeError(f"step {i + 1}: {step['check']} is not true")
            run['status'] = 'dry_run' if run['dry_run'] else 'done'
        except Cancelled:
            run['status'], reason = 'cancelled', f"cancelled by {run.get('cancelled_by', '?')}"
        except (RuntimeError, TimeoutError) as e:
            run['status'], reason = 'aborted', str(e)
        except Exception as e:  # noqa: BLE001 — a run must end in a status, never hang
            logger.exception("sequence %s: %s", seq.id, e)
            run['status'], reason = 'aborted', f"{type(e).__name__}: {e}"
        if run['status'] in ('cancelled', 'aborted') and seq.on_abort and not run['dry_run']:
            final, run['status'], run['reason'] = run['status'], 'undoing', reason   # still running, undoing
            self._publish(seq.id, run)
            for step in seq.on_abort:                         # undo, best effort, every step tried
                ok, detail = self._command(seq, {**run, '_deadline': self._mono() + 120}, step)
                self._note(run, 'on_abort', ok, detail)
            run['status'] = final
        run['reason'] = reason
        run['ended'] = round(self._clock(), 3)
        with self._lock:
            self.running.pop(seq.id, None) if not run['dry_run'] else None
            if not run['dry_run']:
                self.runs.setdefault(seq.id, deque(maxlen=20)).appendleft(run)
        if not run['dry_run']:
            self._publish(seq.id, run)
            if self.audit is not None:
                try:
                    self.audit.append(user=run['by'], ip='-', action='sequence', target=seq.id, status=run['status'],
                                      detail={'params': run['params'], 'via': run['via'], 'reason': reason,
                                              'steps': run['steps']})
                except Exception:  # noqa: BLE001
                    pass

    def _publish(self, sid: str, run: Dict) -> None:
        m = self.mqtt
        if not m or not getattr(m, 'connected', False):
            return
        try:
            import json
            body = {k: run.get(k) for k in ('status', 'step', 'started', 'ended', 'reason', 'by', 'via')}
            m.client.publish(f"mbg/sequences/{sid}/state", json.dumps(body, default=str), qos=1, retain=True)
        except Exception:  # noqa: BLE001
            pass
