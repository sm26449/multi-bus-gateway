# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Commands — the universal write path (docs/commands-design.md).

A command is *what* a controller wants ("power_limit = 60 %"); its recipe is
*how* that is said to one device, declared in the device's template — which
registers, in what order, verified how. The engine below is vendor-blind: it
evaluates a tiny expression grammar, encodes through the template (type,
scale, SunSpec scale factor, byte order), groups consecutive registers into
one frame, and answers with one of five verdicts. It never decides *when*.

Pure over a connection object with ``read_registers(addr, count)`` and
``write(addr, register_type=…, values=[…])`` so it is testable with a fake;
gates, audit, MQTT and the lease live in the API layer.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .encoder import RegisterEncoder
from .register_parser import RegisterParser
from .value_decode import apply_corrections

VERDICTS = ('success', 'mismatch', 'unverified', 'rejected', 'error')


# ── expressions: literal · ${param} · one if · one arithmetic op ────────────

_PARAM = re.compile(r'^\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}$')
_COND = re.compile(r'^\s*(\S+)\s*(<=|>=|==|!=|<|>)\s*(\S+)\s*$')
_OPS = {'*': lambda a, b: a * b, '/': lambda a, b: a / b,
        '+': lambda a, b: a + b, '-': lambda a, b: a - b}


class ExprError(ValueError):
    pass


def _atom(token: str, params: Dict[str, Any]) -> Any:
    m = _PARAM.match(token)
    if m:
        if m.group(1) not in params:
            raise ExprError(f"unknown parameter '{m.group(1)}'")
        return params[m.group(1)]
    try:
        return float(token) if ('.' in token or 'e' in token.lower()) else int(token)
    except ValueError:
        return token.strip('"\'')


def evaluate(expr: Any, params: Dict[str, Any]) -> Any:
    """Evaluate a command expression. Numbers and strings pass through;
    ``"${name}"`` reads a parameter; ``{"if": "a < b", "then": x, "else": y}``
    picks; ``{"*": [a, b]}`` (or ``/``, ``+``, ``-``) computes. Nothing else."""
    if isinstance(expr, bool) or expr is None:
        return expr
    if isinstance(expr, (int, float)):
        return expr
    if isinstance(expr, str):
        return _atom(expr, params)
    if isinstance(expr, dict):
        if 'if' in expr:
            m = _COND.match(str(expr['if']))
            if not m:
                raise ExprError(f"bad condition {expr['if']!r}")
            a, op, b = _atom(m.group(1), params), m.group(2), _atom(m.group(3), params)
            ok = {'<': a < b, '<=': a <= b, '==': a == b, '!=': a != b,
                  '>=': a >= b, '>': a > b}[op]
            return evaluate(expr.get('then') if ok else expr.get('else'), params)
        for op, fn in _OPS.items():
            if op in expr:
                args = expr[op]
                if not isinstance(args, (list, tuple)) or len(args) != 2:
                    raise ExprError(f"'{op}' takes two operands")
                a, b = evaluate(args[0], params), evaluate(args[1], params)
                return fn(float(a), float(b))
    raise ExprError(f"unsupported expression {expr!r}")


# ── the definition ───────────────────────────────────────────────────────────

@dataclass
class ParamDef:
    name: str
    label: str = ''                                     # for a form; the name when empty
    unit: str = ''
    min: Optional[float] = None
    max: Optional[float] = None
    default: Any = None
    required: bool = False
    allowed: Optional[list] = None
    aliases: List[str] = field(default_factory=list)   # legacy names still accepted


@dataclass
class CommandDef:
    name: str
    label: str = ''
    params: Dict[str, ParamDef] = field(default_factory=dict)
    guard: List[Dict] = field(default_factory=list)      # [{read, expect|in|min|max}]
    writes: List[Dict] = field(default_factory=list)     # [{register, value}]
    settle_s: float = 1.0
    verify: List[Dict] = field(default_factory=list)     # [{read, expect, tolerance}]
    safe: Dict[str, Any] = field(default_factory=dict)   # params a lease restores with
    readback_group: str = ''
    alias: Optional[Dict] = None                         # {command, params}
    confirm: bool = True

    @property
    def value_param(self) -> Optional[ParamDef]:
        return self.params.get('value')

    def to_dict(self) -> Dict:
        return {
            'name': self.name, 'label': self.label or self.name,
            'params': {n: {k: v for k, v in vars(p).items() if k != 'name' and v not in (None, [], '')}
                       for n, p in self.params.items()},
            'guard': self.guard, 'writes': self.writes, 'settle_s': self.settle_s,
            'verify': self.verify, 'safe': self.safe, 'readback_group': self.readback_group,
            **({'alias': self.alias} if self.alias else {}), 'confirm': self.confirm,
        }


def parse_command_def(name: str, raw: Dict) -> CommandDef:
    raw = raw or {}
    params = {}
    for pn, pd in (raw.get('params') or {}).items():
        pd = pd or {}
        params[pn] = ParamDef(name=pn, label=str(pd.get('label', '') or ''), unit=str(pd.get('unit', '') or ''),
                              min=pd.get('min'), max=pd.get('max'), default=pd.get('default'),
                              required=bool(pd.get('required', False)), allowed=pd.get('allowed'),
                              aliases=list(pd.get('aliases') or []))
    return CommandDef(name=name, label=str(raw.get('label', '') or ''), params=params,
                      guard=list(raw.get('guard') or []), writes=list(raw.get('writes') or []),
                      settle_s=float(raw.get('settle_s', 1.0) or 0), verify=list(raw.get('verify') or []),
                      safe=dict(raw.get('safe') or {}), readback_group=str(raw.get('readback_group', '') or ''),
                      alias=raw.get('alias'), confirm=bool(raw.get('confirm', True)))


def normalize_params(cmd: CommandDef, given: Dict[str, Any]) -> Dict[str, Any]:
    """Defaults, aliases, bounds. Raises ValueError with the reason."""
    given = dict(given or {})
    known = set(cmd.params) | {a for p in cmd.params.values() for a in p.aliases}
    unknown = sorted(k for k in given if k not in known)
    if unknown:
        # a typo silently ignored is a controller believing it asked for
        # something it did not; the caller learns the names it may use
        raise ValueError(f"unknown parameter(s) {', '.join(unknown)}; expected {', '.join(sorted(known))}")
    out: Dict[str, Any] = {}
    for name, p in cmd.params.items():
        val = given.get(name)
        if val is None:
            for a in p.aliases:
                if given.get(a) is not None:
                    val = given[a]
                    break
        if val is None:
            if p.required and p.default is None:
                raise ValueError(f"{name} is required")
            val = p.default
        if val is not None:
            try:
                val = float(val)
            except (TypeError, ValueError):
                raise ValueError(f"{name} must be a number")
            if val != val or val in (float('inf'), float('-inf')):
                raise ValueError(f"{name} must be finite")
            if p.min is not None and val < p.min:
                raise ValueError(f"{name} {val:g} is below the minimum {p.min:g}")
            if p.max is not None and val > p.max:
                raise ValueError(f"{name} {val:g} is above the maximum {p.max:g}")
            if p.allowed and val not in [float(x) for x in p.allowed]:
                raise ValueError(f"{name} {val:g} is not one of {p.allowed}")
            if float(val).is_integer():
                val = int(val)
        out[name] = val
    return out


def parse_payload(cmd: CommandDef, payload: Any) -> Dict[str, Any]:
    """A command as it arrives over a wire: a bare number (the ``value``
    param), or a JSON object of params. ``source`` rides along untouched."""
    import json
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode('utf-8', 'replace')
    if isinstance(payload, str):
        s = payload.strip()
        if not s:
            return {}
        try:
            data = json.loads(s)
        except ValueError:
            data = s
    else:
        data = payload
    if isinstance(data, bool):
        raise ValueError('a command is a number or an object')
    if isinstance(data, (int, float)):
        return {'value': data}
    if isinstance(data, str):
        try:
            return {'value': float(data)}
        except ValueError:
            raise ValueError(f'not a number or a command object: {data[:40]!r}')
    if not isinstance(data, dict):
        raise ValueError('a command is a number or an object')
    return data


# ── encoding through the template ───────────────────────────────────────────

def _decode(reg, words: List[int], parser: RegisterParser, sf: Dict[str, Any]):
    """Raw words → engineering value, exactly as the poll path does it."""
    raw = parser.parse_value(words, reg.data_type or 'uint16', nan=None)
    return apply_corrections(raw, reg, siblings=sf)


def _raw_for(reg, value: float, sf: Dict[str, Any]) -> float:
    """Engineering value → the RAW number the encoder will pack: the exact
    inverse of apply_corrections (raw × 10^SF / scale + offset)."""
    scale = float(getattr(reg, 'scale', 1.0) or 1.0)
    offset = float(getattr(reg, 'offset', 0.0) or 0.0)
    sf_ref = getattr(reg, 'scale_from', '') or ''
    v = float(value) - offset
    if sf_ref:
        e = sf.get(sf_ref)
        if not isinstance(e, (int, float)) or abs(e) > 10:
            raise ValueError(f"no valid scale factor '{sf_ref}' for {reg.name}")
        return v * scale / (10.0 ** int(e))
    return v * scale


def plan_frames(cmd: CommandDef, params: Dict[str, Any], regs: Dict[str, Any],
                encoder: RegisterEncoder, sf: Dict[str, Any]) -> List[Dict]:
    """The frames a run would write: consecutive holding registers become one
    FC16; anything else goes in declaration order. Each frame carries the
    engineering values it encodes so a result can say what was written."""
    items = []
    for w in cmd.writes:
        reg = regs.get(w.get('register'))
        if reg is None:
            raise ValueError(f"unknown register '{w.get('register')}' in writes")
        rtype = getattr(reg, 'register_type', 'holding') or 'holding'
        val = evaluate(w.get('value'), params)
        if rtype == 'coil':
            words = [1 if val else 0]
        else:
            raw = _raw_for(reg, float(val), sf)
            dtype = (reg.data_type or 'uint16').lower()
            if not dtype.startswith('float'):
                raw = int(round(raw))
            words = encoder.encode(raw, dtype, 1.0, offset=0.0)
        items.append({'register': reg.name, 'address': int(reg.address), 'type': rtype,
                      'value': val, 'words': words})
    frames: List[Dict] = []
    for it in items:
        last = frames[-1] if frames else None
        if last and last['type'] == it['type'] == 'holding' \
                and last['address'] + len(last['words']) == it['address']:
            last['words'] = last['words'] + it['words']
            last['registers'].append(it['register'])
            last['values'][it['register']] = it['value']
        else:
            frames.append({'address': it['address'], 'type': it['type'], 'words': list(it['words']),
                           'registers': [it['register']], 'values': {it['register']: it['value']}})
    return frames


# ── the run ──────────────────────────────────────────────────────────────────

def _block_for(regs: Dict[str, Any], names: List[str]) -> Optional[Tuple[int, int]]:
    addrs = []
    for n in names:
        r = regs.get(n)
        if r is None:
            raise ValueError(f"unknown register '{n}'")
        count = RegisterParser.REGISTER_COUNTS.get((r.data_type or 'uint16').lower(), 1)
        addrs.append((int(r.address), int(r.address) + count))
    if not addrs:
        return None
    lo, hi = min(a for a, _ in addrs), max(b for _, b in addrs)
    if hi - lo > 125:
        raise ValueError('guard/verify registers span more than one Modbus read')
    return lo, hi - lo


def _read_named(conn, regs, names: List[str], parser: RegisterParser, sf_names: List[str]):
    """Read every named register (plus the scale factors they need) in ONE
    block and decode each. Returns ({name: value}, {sf_name: raw}) or None."""
    need = list(dict.fromkeys(list(names) + list(sf_names)))
    blk = _block_for(regs, need)
    if blk is None:
        return {}, {}
    words = conn.read_registers(blk[0], blk[1])
    if not words or len(words) < blk[1]:
        return None
    sf: Dict[str, Any] = {}
    for n in sf_names:
        r = regs[n]
        c = RegisterParser.REGISTER_COUNTS.get((r.data_type or 'uint16').lower(), 1)
        off = int(r.address) - blk[0]
        sf[n] = parser.parse_value(words[off:off + c], r.data_type or 'int16', nan=None)
    out: Dict[str, Any] = {}
    for n in names:
        r = regs[n]
        c = RegisterParser.REGISTER_COUNTS.get((r.data_type or 'uint16').lower(), 1)
        off = int(r.address) - blk[0]
        out[n] = _decode(r, words[off:off + c], parser, sf)
    return out, sf


def _check(rule: Dict, got: Any, params: Dict[str, Any]) -> Optional[str]:
    """None when the rule holds, else the reason."""
    name = rule.get('read')
    if got is None:
        return f"{name} did not decode (missing or a sentinel)"
    if 'expect' in rule:
        want = evaluate(rule['expect'], params)
        tol = float(rule.get('tolerance', 0) or 0)
        if isinstance(want, (int, float)) and isinstance(got, (int, float)):
            if abs(float(got) - float(want)) > tol:
                return f"{name} is {got:g}, expected {want:g}" + (f" ± {tol:g}" if tol else '')
        elif str(got) != str(want):
            return f"{name} is {got!r}, expected {want!r}"
    if 'in' in rule and got not in rule['in'] and float(got) not in [float(x) for x in rule['in']]:
        return f"{name} is {got!r}, expected one of {rule['in']}"
    if 'min' in rule and float(got) < float(rule['min']):
        return f"{name} is {got:g}, below {rule['min']}"
    if 'max' in rule and float(got) > float(rule['max']):
        return f"{name} is {got:g}, above {rule['max']}"
    return None


def run_command(cmd: CommandDef, params: Dict[str, Any], conn, regs: Dict[str, Any], *,
                byte_order: str = 'big', dry_run: bool = False,
                sleep: Callable[[float], None] = time.sleep) -> Dict:
    """Guard → writes → settle → verify, with a verdict. ``regs`` is the
    device's template map by NAME. Never raises for a device's misbehaviour;
    a bad definition raises ValueError (that is the operator's to fix)."""
    t0 = time.monotonic()
    parser, encoder = RegisterParser(byte_order), RegisterEncoder(byte_order)
    out: Dict[str, Any] = {'command': cmd.name, 'params': dict(params), 'status': 'rejected'}
    sf_names = sorted({getattr(regs[w['register']], 'scale_from', '') for w in cmd.writes
                       if w.get('register') in regs and getattr(regs[w['register']], 'scale_from', '')})
    for n in sf_names:
        if n not in regs:
            raise ValueError(f"scale factor register '{n}' is not in the template")
    guard_names = [g['read'] for g in cmd.guard]
    verify_names = [v['read'] for v in cmd.verify]
    read1 = _read_named(conn, regs, list(dict.fromkeys(guard_names + verify_names)), parser, sf_names)
    if read1 is None:
        read1 = _read_named(conn, regs, list(dict.fromkeys(guard_names + verify_names)), parser, sf_names)
    if read1 is None:
        out.update(status='error', reason='the device did not answer the pre-write read — nothing written',
                   ms=round((time.monotonic() - t0) * 1000, 1))
        return out
    before, sf = read1
    failed = [_check(g, before.get(g['read']), params) for g in cmd.guard]
    if any(failed):
        # a datalogger hands back buffer garbage now and then: one more read
        # before believing a guard failure
        again = _read_named(conn, regs, list(dict.fromkeys(guard_names + verify_names)), parser, sf_names)
        if again is not None:
            before, sf = again
            failed = [_check(g, before.get(g['read']), params) for g in cmd.guard]
    out['before'] = {n: before.get(n) for n in verify_names}
    out['guard'] = {n: before.get(n) for n in guard_names}
    if sf:
        out['scale_factors'] = sf
    for why in failed:
        if why:
            out.update(status='rejected', reason=f"guard: {why} — nothing written",
                       ms=round((time.monotonic() - t0) * 1000, 1))
            return out
    try:
        frames = plan_frames(cmd, params, regs, encoder, sf)
    except ValueError as e:
        out.update(status='rejected', reason=str(e), ms=round((time.monotonic() - t0) * 1000, 1))
        return out
    out['frames'] = frames
    if dry_run:
        out.update(status='dry_run', ms=round((time.monotonic() - t0) * 1000, 1))
        return out
    sent = 0
    for fr in frames:
        if fr['type'] == 'coil':
            ok, err = conn.write(fr['address'], register_type='coil', coils=[bool(w) for w in fr['words']])
        else:
            ok, err = conn.write(fr['address'], register_type='holding', values=fr['words'])
        if not ok:
            out.update(status='error', frames_sent=sent,
                       reason=f"write of {fr['registers']} at {fr['address']} failed: {err}"
                              + (f" — {sent} frame(s) went out before it" if sent else ' — nothing went out'),
                       ms=round((time.monotonic() - t0) * 1000, 1))
            return out
        sent += 1
    out['frames_sent'] = sent
    if not cmd.verify:
        out.update(status='success', ms=round((time.monotonic() - t0) * 1000, 1))
        return out
    if cmd.settle_s > 0:
        sleep(cmd.settle_s)
    read2 = _read_named(conn, regs, verify_names, parser, sf_names)
    if read2 is None:
        out.update(status='unverified', reason='written, but the read-back did not answer',
                   ms=round((time.monotonic() - t0) * 1000, 1))
        return out
    after, _sf2 = read2
    out['after'] = {n: after.get(n) for n in verify_names}
    for v in cmd.verify:
        why = _check(v, after.get(v['read']), params)
        if why:
            out.update(status='mismatch', reason=f"read back: {why}",
                       ms=round((time.monotonic() - t0) * 1000, 1))
            return out
    out.update(status='success', ms=round((time.monotonic() - t0) * 1000, 1))
    return out
