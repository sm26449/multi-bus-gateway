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
    # a setting the device forgets by itself (a Samsung FR limit lasts one
    # hour): the gateway writes the last parameters again every refresh_s
    # until the command is run with its `safe` parameters (or its lease ends)
    refresh_s: float = 0.0
    # what the command controls, when it is more than its registers (a mode
    # that changes what a set point means): commands on one channel are
    # arbitrated together (rules phase 3). Empty: the registers it writes.
    channel: str = ''
    # when this command turns its channel ON, as a condition on its
    # parameters ("${value} > 0"); empty: it neither starts nor stops it
    # (a set point). The channel's motor timers count from these.
    on: str = ''

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
            **({'refresh_s': self.refresh_s} if self.refresh_s else {}),
            **({'channel': self.channel} if self.channel else {}),
            **({'on': self.on} if self.on else {}),
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
                      alias=raw.get('alias'), confirm=bool(raw.get('confirm', True)),
                      refresh_s=float(raw.get('refresh_s', 0) or 0),
                      channel=str(raw.get('channel', '') or ''), on=str(raw.get('on', '') or ''))


def turns_on(cmd: CommandDef, params: Dict[str, Any]) -> Optional[bool]:
    """Does running ``cmd`` with ``params`` leave its channel on? None when
    the command does not say (a set point neither starts nor stops)."""
    if not cmd.on:
        return None
    try:
        return bool(evaluate({'if': cmd.on, 'then': 1, 'else': 0}, params))
    except (ExprError, TypeError, KeyError):
        return None


def assign_channels(defs: Dict[str, CommandDef]) -> Dict[str, str]:
    """command name → channel id. Commands that write a common register, or
    name the same `channel`, are one channel; an alias is on its target's.
    The id is the declared name, else the commands' names joined with "+"."""
    def real(name, seen=()):
        d = defs.get(name)
        if d is not None and d.alias and d.alias.get('command') in defs and name not in seen:
            return real(d.alias['command'], seen + (name,))
        return name
    parent = {n: n for n in defs}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    by_key: Dict[str, str] = {}
    for n, d in defs.items():
        if d.alias:
            continue
        keys = [f"reg:{w.get('register')}" for w in d.writes if w.get('register')]
        if d.channel:
            keys.append(f"ch:{d.channel}")
        for k in keys:
            if k in by_key:
                union(by_key[k], n)
            else:
                by_key[k] = n
    groups: Dict[str, List[str]] = {}
    for n, d in defs.items():
        if not d.alias:
            groups.setdefault(find(n), []).append(n)
    ids = {}
    for members in groups.values():
        declared = sorted({defs[m].channel for m in members if defs[m].channel})
        cid = declared[0] if declared else '+'.join(sorted(members))
        for m in members:
            ids[m] = cid
    for n in defs:
        if n not in ids:
            ids[n] = ids.get(real(n), n)
    return ids


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


def _check_envelope(reg, val) -> None:
    """The register's own envelope applies to a command as it does to a raw
    write — in engineering units, before scaling (F-12, 3.83.0)."""
    _num = isinstance(val, (int, float)) and not isinstance(val, bool)
    wmin, wmax, wallowed = (getattr(reg, 'write_min', None), getattr(reg, 'write_max', None),
                            getattr(reg, 'write_allowed', None))
    if _num and wmin is not None and float(val) < float(wmin):
        raise ValueError(f"{reg.name}: {val} is below the register minimum {wmin}")
    if _num and wmax is not None and float(val) > float(wmax):
        raise ValueError(f"{reg.name}: {val} is above the register maximum {wmax}")
    if _num and wallowed and float(val) not in [float(x) for x in wallowed]:
        raise ValueError(f"{reg.name}: {val} is not in the register's allowed set {wallowed}")


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
        rtype = (getattr(reg, 'register_type', 'holding') or 'holding').lower()
        if rtype not in ('holding', 'coil'):
            # input / discrete registers cannot be written; before 3.83.0 they
            # went out as FC16 to whatever sat at that address (F-12)
            raise ValueError(f"register '{reg.name}' is {rtype} — not writable")
        val = evaluate(w.get('value'), params)
        _check_envelope(reg, val)
        if rtype == 'coil':
            words = [1 if val else 0]
        else:
            raw = _raw_for(reg, float(val), sf)
            dtype = (reg.data_type or 'uint16').lower()
            if not dtype.startswith('float'):
                raw = int(round(raw))
                rng = RegisterEncoder.int_range(dtype)
                if rng and not (rng[0] <= raw <= rng[1]):
                    raise ValueError(f"{reg.name}: {val} encodes to {raw}, outside {dtype} [{rng[0]}, {rng[1]}]")
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

def _blocks_for(regs: Dict[str, Any], names: List[str], max_read: int = 0) -> List[Tuple[int, int]]:
    """The reads that cover ``names``: registers close together share one
    block; a block never spans more than ``max_read`` words (the template's
    protocol.max_registers_per_read; 0 = 125). A device that reads one word
    per request (a Delta VFD-EL parameter) gets one read per register, and
    registers far apart (a parameter at 0200H, a status word at 2102H) get
    a read each instead of one impossible block."""
    limit = max_read if 0 < max_read <= 125 else 125
    spans = []
    for n in names:
        r = regs.get(n)
        if r is None:
            raise ValueError(f"unknown register '{n}'")
        count = RegisterParser.REGISTER_COUNTS.get((r.data_type or 'uint16').lower(), 1)
        if count > limit:
            raise ValueError(f"register '{n}' is {count} words, more than one read of {limit}")
        spans.append((int(r.address), int(r.address) + count))
    blocks: List[List[int]] = []
    for lo, hi in sorted(set(spans)):
        if blocks and hi - blocks[-1][0] <= limit:
            blocks[-1][1] = max(blocks[-1][1], hi)
        else:
            blocks.append([lo, hi])
    return [(lo, hi - lo) for lo, hi in blocks]


def _read_named(conn, regs, names: List[str], parser: RegisterParser, sf_names: List[str],
                max_read: int = 0):
    """Read every named register (plus the scale factors they need), in as
    few blocks per register type as the device allows, and decode each.
    Returns ({name: value}, {sf_name: raw}) or None. Holding registers go out
    as FC3, input registers as FC4 — a guard on an input register used to be
    read with FC3 and could never hold (F-14, 3.83.0)."""
    need = list(dict.fromkeys(list(names) + list(sf_names)))
    if not need:
        return {}, {}
    by_type: Dict[str, List[str]] = {}
    for n in need:
        r = regs.get(n)
        if r is None:
            raise ValueError(f"unknown register '{n}'")
        by_type.setdefault((getattr(r, 'register_type', 'holding') or 'holding').lower(), []).append(n)
    words_at: Dict[Tuple[str, int], int] = {}
    for rtype, group in by_type.items():
        for start, count in _blocks_for(regs, group, max_read):
            words = conn.read_registers(start, count, register_type=rtype)
            if not words or len(words) < count:
                return None
            for i, w in enumerate(words[:count]):
                words_at[(rtype, start + i)] = w

    def _words(n):
        r = regs[n]
        rtype = (getattr(r, 'register_type', 'holding') or 'holding').lower()
        c = RegisterParser.REGISTER_COUNTS.get((r.data_type or 'uint16').lower(), 1)
        return [words_at[(rtype, int(r.address) + i)] for i in range(c)]
    sf: Dict[str, Any] = {}
    for n in sf_names:
        sf[n] = parser.parse_value(_words(n), regs[n].data_type or 'int16', nan=None)
    out: Dict[str, Any] = {}
    for n in names:
        out[n] = _decode(regs[n], _words(n), parser, sf)
    return out, sf


def _check(rule: Dict, got: Any, params: Dict[str, Any], reg: Any = None) -> Optional[str]:
    """None when the rule holds, else the reason. A register with states
    reads back as its text ("On"); a rule written with the code (1) is
    compared through the same register's states."""
    name = rule.get('read')
    if got is None:
        return f"{name} did not decode (missing or a sentinel)"
    if 'expect' in rule:
        want = evaluate(rule['expect'], params)
        if isinstance(want, (int, float)) and not isinstance(want, bool) and isinstance(got, str) and reg is not None:
            # the code is the state's own number — the field a mask/shift
            # already extracted — so it maps straight through the states,
            # never through the mask again
            from .value_decode import decode_enum
            states = getattr(reg, 'enum', None) if not isinstance(reg, dict) else reg.get('enum')
            label = decode_enum(int(want), states) if (states and float(want).is_integer()) else None
            if isinstance(label, str) and not label.startswith('unknown ('):
                want = label
        tol = float(rule.get('tolerance', 0) or 0)
        if isinstance(want, (int, float)) and isinstance(got, (int, float)):
            if abs(float(got) - float(want)) > tol:
                return f"{name} is {got:g}, expected {want:g}" + (f" ± {tol:g}" if tol else '')
        elif str(got) != str(want):
            return f"{name} is {got!r}, expected {want!r}"
    # a NON-NUMERIC read-back (an enum decoded to text, a string register)
    # against a numeric in/min/max rule is a VERDICT, not a crash: float(got)
    # used to raise out of run_command and answer 500 at the face instead of
    # 'rejected' (audit 2026-10-01)
    if 'in' in rule:
        ok = got in rule['in']
        if not ok and isinstance(got, str) and reg is not None:
            # a register with states reads back as text; a rule listing the
            # codes ([1, 2]) is compared through the same states, as expect is
            from .value_decode import decode_enum
            states = getattr(reg, 'enum', None) if not isinstance(reg, dict) else reg.get('enum')
            if states:
                labels = [decode_enum(int(x), states) for x in rule['in']
                          if isinstance(x, (int, float)) and not isinstance(x, bool) and float(x).is_integer()]
                ok = got in labels
        if not ok:
            try:
                ok = float(got) in [float(x) for x in rule['in']]
            except (TypeError, ValueError):
                ok = False
        if not ok:
            return f"{name} is {got!r}, expected one of {rule['in']}"
    return _check_minmax(rule, got, name)


def _check_minmax(rule: Dict, got: Any, name) -> Optional[str]:
    if 'min' in rule or 'max' in rule:
        try:
            g = float(got)
        except (TypeError, ValueError):
            return f"{name} is {got!r}, not numeric (rule expects a number)"
        if 'min' in rule and g < float(rule['min']):
            return f"{name} is {g:g}, below {rule['min']}"
        if 'max' in rule and g > float(rule['max']):
            return f"{name} is {g:g}, above {rule['max']}"
    return None


def run_command(cmd: CommandDef, params: Dict[str, Any], conn, regs: Dict[str, Any], *,
                byte_order: str = 'big', dry_run: bool = False, max_read: int = 0,
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
    read1 = _read_named(conn, regs, list(dict.fromkeys(guard_names + verify_names)), parser, sf_names, max_read)
    if read1 is None:
        read1 = _read_named(conn, regs, list(dict.fromkeys(guard_names + verify_names)), parser, sf_names, max_read)
    if read1 is None:
        out.update(status='error', reason='the device did not answer the pre-write read — nothing written',
                   ms=round((time.monotonic() - t0) * 1000, 1))
        return out
    before, sf = read1
    failed = [_check(g, before.get(g['read']), params, regs.get(g['read'])) for g in cmd.guard]
    if any(failed):
        # a datalogger hands back buffer garbage now and then: one more read
        # before believing a guard failure
        again = _read_named(conn, regs, list(dict.fromkeys(guard_names + verify_names)), parser, sf_names, max_read)
        if again is not None:
            before, sf = again
            failed = [_check(g, before.get(g['read']), params, regs.get(g['read'])) for g in cmd.guard]
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
    read2 = _read_named(conn, regs, verify_names, parser, sf_names, max_read)
    if read2 is None:
        out.update(status='unverified', reason='written, but the read-back did not answer',
                   ms=round((time.monotonic() - t0) * 1000, 1))
        return out
    after, _sf2 = read2
    out['after'] = {n: after.get(n) for n in verify_names}
    for v in cmd.verify:
        why = _check(v, after.get(v['read']), params, regs.get(v['read']))
        if why:
            out.update(status='mismatch', reason=f"read back: {why}",
                       ms=round((time.monotonic() - t0) * 1000, 1))
            return out
    out.update(status='success', ms=round((time.monotonic() - t0) * 1000, 1))
    return out


# ── the same run through an MQTT source ─────────────────────────────────────
#
# A device that takes its commands over MQTT (a Victron GX, Tasmota,
# zigbee2mqtt) is read through the MQTT input source, so that source both
# publishes the write and hears its read-back. The steps and verdicts are the
# Modbus run's; only the reading and writing differ.

def _payload_text(v: Any) -> str:
    """A value as it goes into a payload: an integral number without decimals,
    text JSON-escaped (it may sit inside quotes in a JSON payload)."""
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f.is_integer() else repr(f)
    import json as _json
    return _json.dumps(str(v))[1:-1]


def mqtt_payload(reg, value: Any, sf: Dict[str, Any]) -> str:
    """The text published for one write: the register's write_payload with
    ${value} (the raw number, or an enum's code) and ${label} (an enum's
    text). A payload without either is a constant."""
    tmpl = getattr(reg, 'write_payload', '') or '${value}'
    states = getattr(reg, 'enum', None)
    code, label = value, value
    if states and isinstance(value, str):
        rev = {str(lbl): c for c, lbl in states.items()}
        if value not in rev:
            raise ValueError(f"{reg.name}: {value!r} is not one of its states {sorted(rev)}")
        code = rev[value]
    if isinstance(code, (int, float)) and not isinstance(code, bool):
        code = _raw_for(reg, float(code), sf)
        if states:
            from .value_decode import decode_enum
            label = decode_enum(int(code), states) if float(code).is_integer() else value
    elif isinstance(code, str) and not states:
        try:
            code = _raw_for(reg, float(code), sf)
        except ValueError:
            pass                              # text for a text register
    return tmpl.replace('${value}', _payload_text(code)).replace('${label}', _payload_text(label))


def run_mqtt_command(cmd: CommandDef, params: Dict[str, Any], src, regs: Dict[str, Any], *,
                     dry_run: bool = False, forbidden_topics: Optional[List[str]] = None) -> Dict:
    """Guard → (already set?) → publish → wait for the read-back, with the
    Modbus run's verdicts. ``src`` is the MQTT input source (publish, last,
    wait_for, alive, resolve_topic); ``regs`` the template's registers by
    name. ``forbidden_topics``: the gateway's own command topics — a write
    there would be taken as a new command. Never raises for a device's
    misbehaviour; a bad definition raises ValueError."""
    from .mqtt_input import topic_matches
    t0 = time.monotonic()
    out: Dict[str, Any] = {'command': cmd.name, 'params': dict(params), 'status': 'rejected'}

    def done(status: str, reason: str = '', **kw) -> Dict:
        out.update(status=status, ms=round((time.monotonic() - t0) * 1000, 1), **kw)
        if reason:
            out['reason'] = reason
        return out

    def reg_of(name: str):
        r = regs.get(name)
        if r is None:
            raise ValueError(f"unknown register '{name}' in this command")
        return r

    read_addrs = {int(getattr(r, 'address', -1)) for r in (getattr(src, 'registers', None) or [])}
    alive = src.alive()

    def current(name: str):
        """The register's value now, if the source is alive and has one."""
        e = src.last(int(reg_of(name).address))
        return (e[0] if e is not None else None) if alive else None

    guard_names = [g['read'] for g in cmd.guard]
    verify_names = [v['read'] for v in cmd.verify]
    write_names = [w.get('register') for w in cmd.writes]
    needed = list(dict.fromkeys(guard_names + verify_names + write_names))
    unread = [n for n in needed if int(reg_of(n).address) not in read_addrs]
    if unread:
        return done('rejected', f"{', '.join(unread)} not read by this device — select "
                                f"{'it' if len(unread) == 1 else 'them'} — nothing written")
    if not alive:
        return done('rejected', 'the source has sent nothing lately (or says it is offline) '
                                '— nothing written')
    before = {n: current(n) for n in verify_names}
    out['before'] = before
    out['guard'] = {n: current(n) for n in guard_names}
    for g in cmd.guard:
        got = current(g['read'])
        why = (f"{g['read']} has no current value" if got is None
               else _check(g, got, params, reg_of(g['read'])))
        if why:
            return done('rejected', f"guard: {why} — nothing written")
    sf = dict(getattr(src, '_sf_last_good', {}) or {})
    frames: List[Dict] = []
    subs = list(src.subscriptions())
    for w in cmd.writes:
        reg = reg_of(w.get('register'))
        topic = src.resolve_topic(getattr(reg, 'write_topic', '') or '')
        if not topic:
            raise ValueError(f"register '{reg.name}' has no write_topic")
        if any(topic_matches(p, topic) for p in subs):
            return done('rejected', f"{reg.name}: its write topic {topic} is one this device reads — "
                                    "the gateway would read its own command as the answer")
        if any(topic_matches(p, topic) for p in (forbidden_topics or [])):
            return done('rejected', f"{reg.name}: its write topic {topic} is a command topic of "
                                    "this gateway — nothing written")
        val = evaluate(w.get('value'), params)
        try:
            _check_envelope(reg, val)
            payload = mqtt_payload(reg, val, sf)
        except ValueError as e:
            return done('rejected', str(e))
        frames.append({'topic': topic, 'payload': payload, 'registers': [reg.name],
                       'values': {reg.name: val}})
    out['frames'] = frames
    # already set: the device holds every target and every check reads one of
    # the written registers — nothing to publish (a setting kept in flash is
    # spared a write; the run is spared the wait)
    if cmd.verify and frames and set(verify_names) <= set(write_names):
        same = all(current(f['registers'][0]) is not None and not _check(
                       {'read': f['registers'][0], 'expect': f['values'][f['registers'][0]],
                        'tolerance': next((v.get('tolerance', 0) for v in cmd.verify
                                           if v['read'] == f['registers'][0]), 0)},
                       current(f['registers'][0]), params, reg_of(f['registers'][0]))
                   for f in frames)
        if same and not any(_check(v, before.get(v['read']), params, reg_of(v['read'])) for v in cmd.verify):
            out['after'] = dict(before)
            return done('dry_run' if dry_run else 'success', already_set=True, frames_sent=0)
    if dry_run:
        return done('dry_run')
    mark = getattr(src, '_seq', 0)
    sent = 0
    for fr in frames:
        ok, err = src.publish(fr['topic'], fr['payload'])
        if not ok:
            return done('error', f"publish of {fr['registers']} to {fr['topic']} failed: {err}"
                                 + (f" — {sent} went out before it" if sent else ' — nothing went out'),
                        frames_sent=sent)
        sent += 1
    out['frames_sent'] = sent
    if not cmd.verify:
        return done('success')

    def fresh(name):
        e = src.last(int(reg_of(name).address))
        return e if (e is not None and e[2] > mark) else None

    def passes() -> bool:
        return all(fresh(v['read']) is not None
                   and not _check(v, fresh(v['read'])[0], params, reg_of(v['read'])) for v in cmd.verify)

    # wait for read-backs that came after the publish; an old value that is
    # republished meanwhile is not an answer — the verdict waits for the end
    if src.wait_for(passes, max(0.5, float(cmd.settle_s or 0))):
        out['after'] = {n: fresh(n)[0] for n in verify_names}
        return done('success')
    after = {n: (fresh(n)[0] if fresh(n) else None) for n in verify_names}
    out['after'] = after
    if not any(fresh(n) for n in verify_names):
        return done('unverified', 'published, but no read-back arrived')
    for v in cmd.verify:
        got = after.get(v['read'])
        why = (f"{v['read']} did not answer" if got is None
               else _check(v, got, params, reg_of(v['read'])))
        if why:
            return done('mismatch', f"read back: {why}")
    return done('success')
