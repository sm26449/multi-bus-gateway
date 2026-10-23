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
"""Safe arithmetic expression evaluator for CALCULATED REGISTERS.

We adopt the familiar Python-arithmetic syntax (no new language to learn) but
evaluate via a strict AST walk — NEVER ``eval()`` — allowing only a whitelist of
node kinds, operators and functions. References resolve to live register values:

    bare name      -> a register on the SAME device     (e.g. ``_P_SUM3``)
    dotted name    -> a register on ANOTHER device       (e.g. ``fronius._P``)

A calculated register is just ``{name, label, unit, expr, poll_group, decimals}``;
the result is injected into the value store like any other measurement so it flows
to every sink (MQTT / InfluxDB / HTTP-JSON output).
"""
import ast
import re
import math
import time
import operator

from . import clock

# Long enough for a 30-term alarm roll-up (a BMS alarm_count sums ~27
# individually named bits); the AST walk is depth- and node-bounded anyway.
MAX_EXPR_LEN = 2000

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_CMPOPS = {
    ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Eq: operator.eq, ast.NotEq: operator.ne,
}
# Pure, numeric, side-effect-free functions only.
_FUNCS = {
    'min': min, 'max': max, 'abs': abs, 'round': round,
    'sqrt': math.sqrt, 'pow': math.pow, 'floor': math.floor, 'ceil': math.ceil,
    'avg': lambda *a: (sum(a) / len(a)) if a else 0.0,
    'clamp': lambda x, lo, hi: max(lo, min(hi, x)),
    # set bits in an integer bitmask (optionally AND-masked first) — the
    # BMS staple: balancing_count = popcount(balancing_bits)
    'popcount': lambda x, mask=-1: bin(int(round(abs(x))) & (int(mask) if mask != -1 else -1) & 0xFFFFFFFFFFFFFFFF).count('1'),
}
# The installation's local time (clock.py, ui.timezone): pure within one
# evaluation, so a rule can say "between 10:00 and 16:00" or "on weekdays".
_FUNCS.update({
    'hour': lambda: clock.local().hour,
    'minute': lambda: clock.local().minute,
    'weekday': lambda: clock.local().isoweekday(),        # 1 Monday … 7 Sunday
    'day': lambda: clock.local().day,
    'month': lambda: clock.local().month,
})
# Functions that take a short text argument, checked against a pattern at
# save time (no other string ever reaches the engine): between("10:00",
# "16:00") — the hours may also be numbers, 10.5 — and a rule's counters
# by its id, run_today_h("pool-pump").
_TIME_ARG = re.compile(r'^\d{1,2}:\d{2}$')
_RULE_ARG = re.compile(r'^[a-z][a-z0-9_-]{0,47}$')
RULE_STATS = ('run_today_h', 'run_total_h', 'starts_today', 'starts_last_h')
# a place (the plant: "site", an installation or a device id) and an attribute
# name (docs/attributes.md): checked on save, read through providers at run time
_PLACE_ARG = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
_ATTR_NAME_ARG = re.compile(r'^[a-z][a-z_]{0,63}$')
SUN_FUNCS = ('sun_elevation', 'sun_azimuth', 'is_day')
_TEXT_FUNCS = {'between': _TIME_ARG, **{f: _RULE_ARG for f in RULE_STATS},
               **{f: _PLACE_ARG for f in SUN_FUNCS}, 'attr': _PLACE_ARG}
_rule_stats = None          # (rule_id, stat) -> number or None; set by the rules runtime
_place_location = None      # place id ('site', installation, device) -> (lat, lon) or None
_place_ids = None           # () -> the place ids that exist (save-time check)
_attr_value = None          # (target, category, field) -> number or None


def set_place_provider(location, ids=None) -> None:
    """Where sun_elevation() & co. learn a place's coordinates (the asset
    attributes), and which places exist (checked when a formula is saved)."""
    global _place_location, _place_ids
    _place_location, _place_ids = location, ids


def set_attr_provider(fn) -> None:
    """Where attr("device", "category", "field") reads an attribute."""
    global _attr_value
    _attr_value = fn


_SUN_CACHE: dict = {}


def _sun(place: str):
    """(true elevation, apparent elevation, azimuth) of the sun at a place
    now, cached per place and second (the place's location is looked up once
    a second, not on every evaluation)."""
    t = clock.now()
    key = (place, int(t))
    hit = _SUN_CACHE.get(key)
    if hit is None:
        loc = _place_location(place) if _place_location else None
        if not loc:
            raise MissingValue(f'the location of "{place}"')
        from . import sun
        from datetime import datetime, timezone
        hit = sun.position3(loc[0], loc[1], datetime.fromtimestamp(t, timezone.utc))
        if len(_SUN_CACHE) > 256:
            _SUN_CACHE.clear()
        _SUN_CACHE[key] = hit
    return hit


def set_rule_stats_provider(fn) -> None:
    """Where run_today_h() & co. read a rule's counters (the rules runtime)."""
    global _rule_stats
    _rule_stats = fn


_CONSTS = {'pi': math.pi, 'e': math.e, 'true': True, 'false': False}
# Argument count per function: (min, max|None). Wrong arity is rejected at save
# time so a formula like round(x,1,2) or clamp(x,0) can never raise at eval time.
_FUNC_ARITY = {
    'min': (2, None), 'max': (2, None), 'avg': (1, None),
    'abs': (1, 1), 'round': (1, 2), 'sqrt': (1, 1), 'pow': (2, 2),
    'floor': (1, 1), 'ceil': (1, 1), 'clamp': (3, 3),
    'popcount': (1, 2),
    'hour': (0, 0), 'minute': (0, 0), 'weekday': (0, 0), 'day': (0, 0), 'month': (0, 0),
    'between': (2, 2), **{f: (1, 1) for f in RULE_STATS},
    'sun_elevation': (0, 1), 'sun_azimuth': (0, 1), 'is_day': (0, 2), 'attr': (3, 3),
}
MAX_POW_EXP = 64          # cap exponents so base**exp can't blow up CPU/RAM
MAX_ROUND_DIGITS = 15     # round(x, n): |n| beyond this is meaningless for a float
                          # and a huge negative n makes CPython compute 10**|n|
MAX_RESULT_BITS = 63      # an int result wider than this cannot leave the engine
# Stateful helpers (v2): ``dt`` = seconds since this register was last computed;
# ``prev(<ref>)`` = the previous value of a referenced measurement. Enables rates,
# e.g. average power from an energy counter: (E - prev(E)) / dt * 3600.
_SPECIAL_NAMES = {'dt'}
_SPECIAL_FUNCS = {'prev'}

# Container / structural nodes that are always safe on their own.
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.IfExp, ast.Call, ast.Name, ast.Attribute, ast.Load, ast.And, ast.Or,
    ast.Constant,
)
_OP_NODES = tuple(_BINOPS) + tuple(_UNARYOPS) + tuple(_CMPOPS)


class ExpressionError(Exception):
    """Bad expression or a runtime math error (e.g. division by zero)."""


class MissingValue(Exception):
    """A referenced register has no current value yet — the whole expression is
    skipped this round rather than publishing a partial/garbage result."""


def _const_int(node):
    """Return a non-negative int subscript, or None. Handles the py<=3.8 ``Index``
    slice wrapper as well as the py3.9+ bare-node slice."""
    if node.__class__.__name__ == 'Index':      # py<=3.8 wraps the slice value
        node = node.value
    if isinstance(node, ast.Constant) and isinstance(node.value, int) \
            and not isinstance(node.value, bool):
        return node.value
    return None


def _ref_name(node):
    """Reconstruct a register reference string from a Name / Attribute / subscript
    node, or None if the node is not a reference. Register names use array-style
    brackets (e.g. ``_G_ULN[1]``) which Python parses as a Subscript — we treat
    ``<name>[<int>]`` and ``<dev>.<name>[<int>]`` as a single reference token."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    if isinstance(node, ast.Subscript):
        base = _ref_name(node.value)
        idx = _const_int(node.slice)
        if base is not None and idx is not None:
            return f"{base}[{idx}]"
    return None


def _collect_refs(node, out):
    """Collect referenced register names: bare ``x``, dotted ``dev.reg`` and
    subscripted ``x[0]`` / ``dev.reg[0]``. Function and constant names are not refs."""
    if isinstance(node, ast.Call):
        for a in node.args:                      # skip the function name itself
            _collect_refs(a, out)
        return
    ref = _ref_name(node)
    if ref is not None:
        if isinstance(node, ast.Name) and (node.id in _FUNCS or node.id in _CONSTS
                                           or node.id in _SPECIAL_NAMES):
            return
        out.add(ref)
        return
    for child in ast.iter_child_nodes(node):
        _collect_refs(child, out)


# A device id may carry hyphens (`samsung-dvm.water_in`), which Python would
# read as a minus. A token with a hyphen followed by `.field` is a device
# reference: it is rewritten with `__` before parsing and back when resolved.
# Rules have always done this; calculated fields share it now.
_DEV_REF = re.compile(r'(?<![\w.])([A-Za-z_][\w-]*-[\w-]*)(?=\.[A-Za-z_])')
# with the gateway's device ids known: any `<id>.` token that names one
_ID_TOKEN = re.compile(r'(?<![\w.])([A-Za-z0-9_][\w-]*)(?=\.[A-Za-z_])')
DEV_DASH = '__'
_device_ids = None          # () -> set of device ids; set by the API at start-up


def set_device_ids_provider(fn) -> None:
    """Tell the expression engine which device ids exist. With it, only a
    token that IS a device id is read as one: `grid_power-meter2.p` stays a
    subtraction unless `grid_power-meter2` is a device, and an id that starts
    with a digit (`3150-iem.p`) works. Without it (tests, a template check),
    any hyphenated `<a-b>.` token is taken for a device, as rules always did."""
    global _device_ids
    _device_ids = fn


def _known() -> set:
    try:
        return set(_device_ids()) if _device_ids else set()
    except Exception:  # noqa: BLE001
        return set()


def known_ids() -> set:
    """The device and installation ids a formula may read (empty when the
    engine was not told: tests, an offline template check)."""
    return _known()


def dashed_unknown(expr: str) -> list:
    """The hyphenated `<a-b>.field` tokens that name nothing this gateway has
    and do not read as a subtraction either: `battery-bank.x` with no
    `battery-bank` (and no `bank`) is a reference to a device that is not
    here; `grid_power-meter2.p` with a `meter2` is a subtraction and is left
    alone. Only for what is SAID about a formula — evaluation is unchanged.
    Empty when the ids are not known."""
    known = _known()
    if not known:
        return []
    out = []
    for m in _DEV_REF.finditer(expr or ''):
        tok = m.group(1)
        if tok in known or tok.rsplit('-', 1)[-1] in known or tok in out:
            continue
        out.append(tok)
    return out


def dashed_unknown_text(tok: str) -> str:
    """The sentence for one dashed_unknown token."""
    return (f"'{tok}' is not a device or installation of this gateway "
            f"(in a formula a hyphen between two names is a minus)")


def _alias(dev: str) -> str:
    """A device id as a Python name: hyphens doubled into underscores, a
    leading digit prefixed."""
    a = dev.replace('-', DEV_DASH)
    return ('d_' + a) if a[:1].isdigit() else a


def prepare_refs(expr: str, extra=()) -> str:
    """`extra`: ids to read as devices although the gateway has none by that
    name (a template check that names a device the gateway does not have yet)."""
    # a formula pasted over several lines (or with tabs) is one formula
    expr = ' '.join((expr or '').split()) if any(c in (expr or '') for c in '\t\n\r') else expr
    known = _known() | set(extra)
    if not known:
        return _DEV_REF.sub(lambda m: m.group(1).replace('-', DEV_DASH), expr or '')

    def sub(m):
        tok = m.group(1)
        if tok in known and ('-' in tok or tok[:1].isdigit()):
            return _alias(tok)
        return tok
    return _ID_TOKEN.sub(sub, expr or '')


def unprepare_ref(name: str, extra=()) -> str:
    if '.' not in name:
        return name
    dev, reg = name.split('.', 1)
    known = _known() | set(extra)
    if known:
        if dev in known:
            return name                          # a real id, `__` and all
        back = {_alias(d): d for d in known}
        if dev in back:
            return f"{back[dev]}.{reg}"
    return f"{dev.replace(DEV_DASH, '-')}.{reg}"


def validate_expression(expr, check_places: bool = True, assume_ids=()):
    """Validate an expression. Returns ``(ok: bool, error: str|None,
    refs: list[str])`` — refs are the register names it reads. `assume_ids`:
    hyphenated ids to read as devices (see prepare_refs)."""
    if not expr or not expr.strip():
        return False, "empty expression", []
    if len(expr) > MAX_EXPR_LEN:
        return False, f"expression too long (max {MAX_EXPR_LEN} chars)", []
    # what a copy/paste brings and nobody sees: a typographic minus, a
    # no-break space, a look-alike letter — named, with what to type instead
    from .text_check import formula_issue
    issue = formula_issue(expr)
    if issue:
        return False, issue, []
    try:
        tree = ast.parse(prepare_refs(expr, assume_ids).strip(), mode='eval')
    except SyntaxError as e:
        return False, f"syntax error: {e.msg}", []
    texts = set()            # the text arguments a function may take, checked here
    _places_cache: list = []

    def _known_places():
        if not _places_cache:
            try:
                _places_cache.append(set(_place_ids()))
            except Exception:  # noqa: BLE001
                _places_cache.append(None)
        return _places_cache[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _TEXT_FUNCS:
            pat = _TEXT_FUNCS[node.func.id]
            fname = node.func.id
            for ai, a in enumerate(node.args):
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    apat = _ATTR_NAME_ARG if (fname == 'attr' and ai > 0) else pat
                    if not apat.match(a.value):
                        what = ('a time "HH:MM"' if fname == 'between' else
                                'a place ("site", an installation or a device id)' if fname in SUN_FUNCS
                                or (fname == 'attr' and ai == 0) else
                                'an attribute name' if fname == 'attr' else 'a rule id')
                        return False, f"{fname}(): {a.value!r} is not {what}", []
                    if (fname in SUN_FUNCS or (fname == 'attr' and ai == 0)) and _place_ids is not None \
                            and a.value != 'site' and check_places:
                        known_places = _known_places()
                        if known_places is not None and a.value not in known_places:
                            return False, f"{fname}(): there is no installation or device {a.value!r}", []
                    if node.func.id == 'between':
                        try:
                            clock.hours_of(a.value)
                        except ValueError as e:
                            return False, f"between(): {e}", []
                    texts.add(id(a))
                elif node.func.id in RULE_STATS:
                    return False, f'{node.func.id}() takes a rule id in quotes: {node.func.id}("pool-pump")', []
                elif fname == 'attr' or (fname in ('sun_elevation', 'sun_azimuth')):
                    return False, (f'{fname}() takes its place in quotes: {fname}("pv")' if fname != 'attr' else
                                   'attr() takes three names in quotes: attr("umg512", "grid_connection", "max_export_kw")'), []
            if fname == 'is_day':
                nums = [a for a in node.args if not (isinstance(a, ast.Constant) and isinstance(a.value, str))]
                if any((isinstance(a, ast.Constant) and isinstance(a.value, bool))
                       or (isinstance(a, ast.Name) and a.id in ('true', 'false')) for a in nums):
                    return False, 'is_day(): the degrees are a number, not true/false', []
                strs = [a for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                if len(nums) > 1 or len(strs) > 1 or (nums and strs and node.args.index(strs[0]) < node.args.index(nums[0])):
                    return False, 'is_day(degrees, "place"): both optional, the degrees first', []
            if fname == 'attr' and len(node.args) == 3:
                from .attribute_catalog import CATALOG
                cat_, field_ = node.args[1].value, node.args[2].value
                if cat_ not in CATALOG:
                    return False, f"attr(): {cat_!r} is not an attribute category ({', '.join(CATALOG)})", []
                if field_ not in CATALOG[cat_]['fields']:
                    return False, f"attr(): {cat_} has no {field_!r} ({', '.join(CATALOG[cat_]['fields'])})", []
    for node in ast.walk(tree):
        if isinstance(node, _OP_NODES):
            continue
        if isinstance(node, ast.Call):
            if node.keywords:                        # no keyword/starred args
                return False, "keyword arguments are not allowed", []
            fn = getattr(node.func, 'id', None) if isinstance(node.func, ast.Name) else None
            if fn in _SPECIAL_FUNCS:                 # prev(<measurement ref>)
                if len(node.args) != 1 or _ref_name(node.args[0]) is None:
                    return False, "prev() takes a single measurement reference", []
                continue
            if fn == 'first':                        # first(a, b, …): the first one that has a value
                if len(node.args) < 2:
                    return False, "first() takes at least 2 arguments, got " + str(len(node.args)), []
                continue
            if fn in ('fresh', 'age'):               # fresh(x), age(x): about the values x reads
                if len(node.args) != 1:
                    return False, f"{fn}() takes 1 argument, got {len(node.args)}", []
                continue
            if fn == 'count_fresh':                  # count_fresh(a, b, …): how many are fresh
                if not node.args:
                    return False, "count_fresh() takes at least 1 argument", []
                continue
            if fn == 'steps':                        # steps(x, below, t1, v1, t2, v2, …)
                n = len(node.args)
                if n < 4 or n % 2:
                    return False, ("steps(x, below, t1, v1, t2, v2, …): a value, what it is below "
                                   f"the first threshold, then threshold/value pairs — got {n} arguments"), []
                consts = [a.value for a in node.args[2::2]
                          if isinstance(a, ast.Constant) and isinstance(a.value, (int, float))]
                if len(consts) == len(node.args[2::2]) and any(b <= a for a, b in zip(consts, consts[1:])):
                    return False, "steps(): thresholds must rise (t1 < t2 < …)", []
                continue
            if fn == 'absent':                       # absent(): no value this round
                if node.args:
                    return False, "absent() takes no arguments", []
                continue
            if fn == 'ema':                          # ema(x, seconds): a smoothed x
                if len(node.args) != 2:
                    return False, "ema(x, seconds) takes 2 arguments: a value and its time constant", []
                t = node.args[1]
                if isinstance(t, ast.Constant) and isinstance(t.value, (int, float)) \
                        and not isinstance(t.value, bool) and t.value <= 0:
                    return False, "ema(): the time constant is seconds, above 0", []
                continue
            if fn == 'hysteresis':                   # hysteresis(x, low, high): 1 / 0 with a dead band
                if len(node.args) != 3:
                    return False, "hysteresis(x, low, high) takes 3 arguments", []
                lo_, hi_ = node.args[1], node.args[2]
                if all(isinstance(a, ast.Constant) and isinstance(a.value, (int, float))
                       and not isinstance(a.value, bool) for a in (lo_, hi_)) and lo_.value >= hi_.value:
                    return False, "hysteresis(): low must be under high", []
                continue
            if fn is None or (fn not in _FUNCS and fn not in _TEXT_FUNCS):
                return False, f"function not allowed: {fn or '?'}()", []
            lo, hi = _FUNC_ARITY.get(fn, (0, None))
            n = len(node.args)
            if n < lo or (hi is not None and n > hi):
                want = f"{lo}" if lo == hi else (f"at least {lo}" if hi is None else f"{lo}–{hi}")
                return False, f"{fn}() takes {want} arguments, got {n}", []
            if fn == 'round' and n == 2:
                a1 = node.args[1]
                neg = isinstance(a1, ast.UnaryOp) and isinstance(a1.op, ast.USub)
                lit = a1.operand if neg else a1
                if isinstance(lit, ast.Constant) and isinstance(lit.value, (int, float)) \
                        and not isinstance(lit.value, bool) and abs(lit.value) > MAX_ROUND_DIGITS:
                    return False, f"round(): decimals must be within ±{MAX_ROUND_DIGITS}", []
            continue
        if isinstance(node, ast.Subscript):      # register name like _G_ULN[1]
            if _ref_name(node) is None:
                return False, "invalid index — only <name>[<integer>] allowed", []
            continue
        if node.__class__.__name__ == 'Index':   # py<=3.8 slice wrapper
            continue
        if isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name):
                return False, "only <device>.<register> references allowed", []
            continue
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float, bool)) and id(node) not in texts:
                return False, "only numeric constants allowed", []
            continue
        if not isinstance(node, _ALLOWED_NODES):
            return False, f"not allowed: {type(node).__name__}", []
    refs = set()
    _collect_refs(tree.body, refs)
    return True, None, sorted(unprepare_ref(r) for r in refs)


def evaluate(expr, resolve, *, prev_resolve=None, dt=0.0, stale=None, fresh=None, age=None, memo=None):
    """Evaluate a (validated) expression. ``resolve(name)`` returns the numeric
    value of a reference or ``None`` if it has none. ``prev_resolve(ref)`` returns
    the previous value of a reference (for ``prev(...)``) and ``dt`` is the seconds
    since the last evaluation. Raises ``MissingValue`` when a reference (or a
    prev value) is absent, ``ExpressionError`` on a math error."""
    try:
        tree = ast.parse(prepare_refs(expr).strip(), mode='eval')
    except SyntaxError as e:
        raise ExpressionError(str(e))
    return evaluate_tree(tree, resolve, prev_resolve=prev_resolve, dt=dt, stale=stale, fresh=fresh, age=age, memo=memo)


def compile_expression(expr):
    """Parse an expression to an AST once (reuse across evaluations). Returns the
    tree or raises ExpressionError. Lets the hot poll path skip re-parsing every
    calc register every cycle."""
    try:
        return ast.parse(prepare_refs(expr).strip(), mode='eval')
    except SyntaxError as e:
        raise ExpressionError(str(e))


def evaluate_tree(tree, resolve, *, prev_resolve=None, dt=0.0, stale=None, fresh=None, age=None, memo=None):
    """Evaluate a PRE-PARSED expression tree (from compile_expression).
    ``stale(ref)`` (optional) says a reference's value is too old to trust —
    first() then prefers another argument. ``fresh(ref)`` answers the same
    question for fresh() and count_fresh() alone (default: ``stale``), so a
    caller can offer them without changing what first() does. ``age(ref)``
    is the seconds since a reference was measured, for age(). ``memo`` (a
    dict the caller keeps per formula) is what ema() and hysteresis()
    remember between evaluations."""
    ctx = {'resolve': resolve, 'prev': prev_resolve, 'dt': float(dt or 0.0), 'stale': stale,
           'fresh_stale': fresh or stale, 'age': age, 'memo': memo}
    try:
        val = _eval(tree.body, ctx)
    except (ZeroDivisionError, OverflowError, ValueError, TypeError) as e:
        raise ExpressionError(str(e))
    # what leaves the engine must be a number every sink can carry: no
    # inf/nan (they break /api/values, MQTT JSON and Influx line protocol)
    # and no integer wider than 64 bits (3.83.0)
    if isinstance(val, float) and not math.isfinite(val):
        raise ExpressionError("result is not finite")
    if isinstance(val, int) and not isinstance(val, bool) and val.bit_length() > MAX_RESULT_BITS:
        raise ExpressionError("result too large")
    return val


def _is_fresh(arg, ctx) -> bool:
    """Every value ``arg`` reads is present and not stale (without reading a
    stale one); with no staleness answer, the values only have to be there."""
    refs = set()
    _collect_refs(arg, refs)
    stale = ctx.get('fresh_stale')
    if stale is not None:
        return not any(stale(r) for r in refs)
    try:
        _eval(arg, ctx)
        return True
    except (MissingValue, ExpressionError, ZeroDivisionError, ValueError, OverflowError, TypeError):
        return False


def _lookup(resolve, name):
    v = resolve(name)
    if v is None:
        raise MissingValue(name)
    if isinstance(v, bool):          # a boolean flag register — allowed as-is
        return v
    if not isinstance(v, (int, float)):
        raise MissingValue(name)     # non-numeric (string/None) → treat as absent
    return v


def _eval(node, ctx):
    if isinstance(node, ast.Constant):
        # defense-in-depth, mirrors the validate-time rule: a tree that never
        # went through validate_expression (hand-edited registers file, an
        # imported backup) must not evaluate string constants — 'A' * 10**8
        # would allocate the string before any result-size cap could fire
        if not isinstance(node.value, (int, float, bool)):
            raise ExpressionError("only numeric constants allowed")
        return node.value
    if isinstance(node, ast.Name):
        if node.id == 'dt':
            return ctx['dt']
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        return _lookup(ctx['resolve'], node.id)
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        ref = _ref_name(node)
        if ref is None:
            raise ExpressionError("invalid reference")
        return _lookup(ctx['resolve'], ref)
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Pow):             # bound the power (DoS guard)
            base, exp = _eval(node.left, ctx), _eval(node.right, ctx)
            if abs(exp) > MAX_POW_EXP:
                raise ExpressionError(f"exponent too large (max {MAX_POW_EXP})")
            # Bounding only the exponent isn't enough: nesting ((2**64)**64)**…
            # keeps every exponent at 64 while the BASE grows super-exponentially
            # (each ** result feeds the next base). Cap the int result bit-length
            # so the first over-large level is rejected BEFORE it is allocated.
            # (float ** already raises OverflowError, which evaluate() catches.)
            if isinstance(base, int) and base and exp > 0 and base.bit_length() * exp > 2048:
                raise ExpressionError("power result too large")
            return base ** exp
        return _BINOPS[type(node.op)](_eval(node.left, ctx),
                                      _eval(node.right, ctx))
    if isinstance(node, ast.UnaryOp):
        return _UNARYOPS[type(node.op)](_eval(node.operand, ctx))
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, ctx) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, ctx)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, ctx)
            if not _CMPOPS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return (_eval(node.body, ctx) if _eval(node.test, ctx)
                else _eval(node.orelse, ctx))
    if isinstance(node, ast.Call):
        fn = node.func.id
        if fn in _SPECIAL_FUNCS:                 # prev(<measurement ref>)
            ref = _ref_name(node.args[0])
            pv = ctx.get('prev')
            val = pv(ref) if pv else None
            if val is None:
                raise MissingValue(f"prev({ref})")   # first run / no history yet
            return val
        if fn == 'first':
            # the first argument that has a FRESH value: a linked meter when it
            # answers, the estimate when the meter is gone or frozen; a math
            # error (a division by 0) also passes to the next. Only if none is
            # fresh, the first that has any value (its age then marks the result)
            stale = ctx.get('stale')
            for pass_ in ((True, False) if stale else (False,)):
                for a in node.args:
                    if pass_:
                        refs = set()
                        _collect_refs(a, refs)
                        if any(stale(r) for r in refs):
                            continue
                    try:
                        return _eval(a, ctx)
                    except (MissingValue, ExpressionError, ZeroDivisionError, ValueError, OverflowError):
                        continue
            raise MissingValue("first(): none of its arguments has a value")
        if fn in ('fresh', 'count_fresh'):
            # fresh(x): 1 when every value x reads is there and not stale, else
            # 0; count_fresh(a, b, …): how many of its arguments are fresh.
            # A stale value is not read: its age must not mark the result
            # (that is what a formula gated on fresh() avoids)
            n = sum(_is_fresh(a, ctx) for a in node.args)
            return n if fn == 'count_fresh' else (1 if n else 0)
        if fn == 'age':
            # seconds since the OLDEST value x reads was measured (a calculated
            # value: since its oldest input). Unknown → no value
            refs = set()
            _collect_refs(node.args[0], refs)
            age = ctx.get('age')
            ages = [age(r) for r in refs] if age is not None else []
            if not refs or not ages or any(a is None for a in ages):
                raise MissingValue("age(): no measurement time")
            return round(max(ages), 1)
        if fn == 'steps':
            # steps(x, below, t1, v1, t2, v2, …): `below` under t1, v1 from t1,
            # v2 from t2… — the value of the highest threshold x has reached
            x = _eval(node.args[0], ctx)
            out = _eval(node.args[1], ctx)
            last = None
            for i in range(2, len(node.args), 2):
                t = _eval(node.args[i], ctx)
                if last is not None and t <= last:
                    raise ExpressionError("steps(): thresholds must rise")
                last = t
                if x >= t:
                    out = _eval(node.args[i + 1], ctx)
            return out
        if fn == 'absent':
            # the formula says "no honest value now" (an input it needs is
            # missing): nothing is published this round, as for a missing input
            raise MissingValue("absent()")
        if fn in ('ema', 'hysteresis'):
            # remembered between evaluations, one memory per call in the
            # formula (a preview has none: it answers as a first evaluation)
            memo = ctx.get('memo')
            key = f"{fn}@{node.lineno}:{node.col_offset}"
            x = _eval(node.args[0], ctx)
            if isinstance(x, bool) or not isinstance(x, (int, float)):
                raise ExpressionError(f"{fn}(): the value is a number")
            if fn == 'ema':
                # an exponential moving average, by the time between two
                # evaluations: after `tau` seconds a step has gone 63 % of the way
                tau = _eval(node.args[1], ctx)
                if isinstance(tau, bool) or not isinstance(tau, (int, float)) or tau <= 0:
                    raise ExpressionError("ema(): the time constant is seconds, above 0")
                now = time.monotonic()
                last = memo.get(key) if memo is not None else None
                if last is None:
                    y = float(x)
                else:
                    y_prev, t_prev = last
                    step = max(0.0, now - t_prev)
                    y = y_prev + (1.0 - math.exp(-step / float(tau))) * (float(x) - y_prev)
                if memo is not None:
                    memo[key] = (y, now)
                return y
            lo = _eval(node.args[1], ctx)
            hi = _eval(node.args[2], ctx)
            if lo >= hi:
                raise ExpressionError("hysteresis(): low must be under high")
            # 1 from high up, 0 from low down, and between them what it was
            # (0 at the start): a threshold that does not flap
            state = memo.get(key) if memo is not None else None
            out = 1 if x >= hi else 0 if x <= lo else (state if state is not None else 0)
            if memo is not None:
                memo[key] = out
            return out
        if fn == 'between':
            ends = []
            for a in node.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    if not _TIME_ARG.match(a.value):
                        raise ExpressionError(f"between(): {a.value!r} is not a time")
                    ends.append(clock.hours_of(a.value))
                else:
                    ends.append(float(_eval(a, ctx)))
            return clock.between(ends[0], ends[1])
        if fn in ('sun_elevation', 'sun_azimuth'):
            place = node.args[0].value if node.args else 'site'
            _true, app_, az = _sun(place)
            return round(app_ if fn == 'sun_elevation' else az, 2)
        if fn == 'is_day':
            place, deg = 'site', None
            for a in node.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    place = a.value
                else:
                    deg = _eval(a, ctx)
                    if isinstance(deg, bool) or not isinstance(deg, (int, float)):
                        raise ExpressionError("is_day(): the degrees are a number")
            from .sun import SUNRISE_DEG
            true_, app_, _az = _sun(place)
            # no degrees: between sunrise and sunset (true elevation, −0.833°);
            # with degrees: the sun as seen is above them
            return true_ > SUNRISE_DEG if deg is None else app_ > float(deg)
        if fn == 'attr':
            target, cat_, field_ = (a.value for a in node.args)
            val = _attr_value(target, cat_, field_) if _attr_value else None
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise MissingValue(f'attr("{target}", "{cat_}", "{field_}")')
            return val
        if fn in RULE_STATS:
            a = node.args[0]
            if not (isinstance(a, ast.Constant) and isinstance(a.value, str) and _RULE_ARG.match(a.value)):
                raise ExpressionError(f"{fn}() takes a rule id")
            val = _rule_stats(a.value, fn) if _rule_stats else None
            if val is None:
                raise MissingValue(f'{fn}("{a.value}")')
            return val
        args = [_eval(a, ctx) for a in node.args]
        if fn == 'round' and len(args) == 2:
            # round(x, -10**8) makes CPython build 10**(10**8) — a hang of the
            # poller or the single rules thread; the value of n is bounded
            # whether it came from a constant or from a measurement (3.83.0)
            nd = args[1]
            if isinstance(nd, bool) or not isinstance(nd, (int, float)) or abs(nd) > MAX_ROUND_DIGITS:
                raise ExpressionError(f"round(): decimals must be within ±{MAX_ROUND_DIGITS}")
            args[1] = int(nd)
        return _FUNCS[fn](*args)
    raise ExpressionError(f"unsupported node {type(node).__name__}")


# ---- Helper catalogs surfaced to the UI builder --------------------------------

FUNCTIONS = [
    {"name": "min", "sig": "min(a, b, …)", "desc": "smallest value"},
    {"name": "max", "sig": "max(a, b, …)", "desc": "largest value"},
    {"name": "avg", "sig": "avg(a, b, …)", "desc": "average"},
    {"name": "abs", "sig": "abs(x)", "desc": "absolute value"},
    {"name": "round", "sig": "round(x, n)", "desc": "round to n decimals"},
    {"name": "sqrt", "sig": "sqrt(x)", "desc": "square root"},
    {"name": "popcount", "sig": "popcount(x, mask)", "desc": "set bits in a bitmask (mask optional)"},
    {"name": "pow", "sig": "pow(x, y)", "desc": "x to the power y"},
    {"name": "floor", "sig": "floor(x)", "desc": "round down"},
    {"name": "ceil", "sig": "ceil(x)", "desc": "round up"},
    {"name": "clamp", "sig": "clamp(x, lo, hi)", "desc": "constrain x to [lo, hi]"},
    {"name": "prev", "sig": "prev(x)", "desc": "previous value of a measurement (for rates)"},
    {"name": "first", "sig": "first(a, b, …)", "desc": "the first that has a value (a meter, else an estimate)"},
    {"name": "fresh", "sig": "fresh(x)", "desc": "1 when every value x reads is present and fresh, else 0"},
    {"name": "absent", "sig": "absent()", "desc": "no value this round: x if fresh(a) else absent()"},
    {"name": "count_fresh", "sig": "count_fresh(a, b, …)", "desc": "how many of its arguments are present and fresh"},
    {"name": "age", "sig": "age(x)", "desc": "seconds since the oldest value x reads was measured"},
    {"name": "steps", "sig": "steps(x, below, t1, v1, t2, v2, …)", "desc": "a step function: below under t1, v1 from t1, v2 from t2…"},
    {"name": "ema", "sig": "ema(x, 300)", "desc": "x smoothed, with a time constant in seconds (63 % of a step after it)"},
    {"name": "hysteresis", "sig": "hysteresis(x, low, high)", "desc": "1 from high up, 0 from low down, unchanged between them"},
    {"name": "sun_elevation", "sig": 'sun_elevation("pv")', "desc": "the sun's height in degrees at a place (the plant when empty)"},
    {"name": "sun_azimuth", "sig": 'sun_azimuth("pv")', "desc": "where the sun is, compass degrees (180 = south)"},
    {"name": "is_day", "sig": 'is_day(5, "pv")', "desc": "the sun is above the degrees (default: sunrise, -0.833)"},
    {"name": "attr", "sig": 'attr("umg512", "grid_connection", "max_export_kw")', "desc": "an asset attribute (docs/attributes.md)"},
    {"name": "dt", "sig": "dt", "desc": "seconds since this value was last computed"},
    {"name": "hour", "sig": "hour()", "desc": "the hour now, 0-23 (the installation's timezone)"},
    {"name": "minute", "sig": "minute()", "desc": "the minute now, 0-59"},
    {"name": "weekday", "sig": "weekday()", "desc": "the day of the week, 1 Monday … 7 Sunday"},
    {"name": "day", "sig": "day()", "desc": "the day of the month"},
    {"name": "month", "sig": "month()", "desc": "the month, 1-12"},
    {"name": "between", "sig": 'between("10:00", "16:00")', "desc": "true inside the window (it may cross midnight)"},
    {"name": "run_today_h", "sig": 'run_today_h("rule-id")', "desc": "hours a rule kept its target on today"},
    {"name": "run_total_h", "sig": 'run_total_h("rule-id")', "desc": "hours a rule kept its target on, in all"},
    {"name": "starts_today", "sig": 'starts_today("rule-id")', "desc": "how many times a rule started its target today"},
    {"name": "starts_last_h", "sig": 'starts_last_h("rule-id")', "desc": "starts in the last hour"},
]
OPERATORS = ["+", "-", "*", "/", "%", "**", "( )", ">", "<", ">=", "<=",
             "==", "and", "or", "a if cond else b"]

# Built-in presets = PARAMETERIZED formulas. Inputs are bound to the device's own
# measurement fields in the UI (so we never fabricate register names). {key}
# placeholders are substituted with the chosen field names to build the expr.
PRESETS = [
    {"id": "pf_total", "name": "PF_TOTAL", "label": "Power factor (total)",
     "unit": "", "decimals": 3, "template": "{P} / {S}",
     "inputs": [{"key": "P", "label": "Active power P (W)"},
                {"key": "S", "label": "Apparent power S (VA)"}],
     "hint": "Active ÷ apparent power (−1..1)."},
    {"id": "p_sum", "name": "P_TOTAL", "label": "Total active power",
     "unit": "W", "decimals": 1, "template": "{L1} + {L2} + {L3}",
     "inputs": [{"key": "L1", "label": "P phase 1"},
                {"key": "L2", "label": "P phase 2"},
                {"key": "L3", "label": "P phase 3"}],
     "hint": "Sum of the per-phase active power."},
    {"id": "w_to_kw", "name": "P_KW", "label": "Power in kW",
     "unit": "kW", "decimals": 3, "template": "{W} / 1000",
     "inputs": [{"key": "W", "label": "Power (W)"}],
     "hint": "Watts → kilowatts."},
    {"id": "wh_to_kwh", "name": "E_KWH", "label": "Energy in kWh",
     "unit": "kWh", "decimals": 3, "template": "{Wh} / 1000",
     "inputs": [{"key": "Wh", "label": "Energy (Wh)"}],
     "hint": "Watt-hours → kilowatt-hours."},
    {"id": "i_imbalance", "name": "I_IMBALANCE", "label": "Current imbalance",
     "unit": "%", "decimals": 1,
     "template": "(max({L1},{L2},{L3}) - avg({L1},{L2},{L3})) / avg({L1},{L2},{L3}) * 100",
     "inputs": [{"key": "L1", "label": "Current L1"},
                {"key": "L2", "label": "Current L2"},
                {"key": "L3", "label": "Current L3"}],
     "hint": "Max deviation from the 3-phase average current."},
    {"id": "power_from_energy", "name": "P_FROM_E", "label": "Power from energy counter",
     "unit": "W", "decimals": 1, "template": "({E} - prev({E})) / dt * 3600",
     "inputs": [{"key": "E", "label": "Energy counter (Wh)"}],
     "hint": "Average power between polls, from the change in a Wh energy counter "
             "(stateful: uses prev() + dt). Put this on a slow poll group."},
]
