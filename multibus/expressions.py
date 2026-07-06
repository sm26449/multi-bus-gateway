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
import math
import operator

MAX_EXPR_LEN = 500

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
}
_CONSTS = {'pi': math.pi, 'e': math.e, 'true': True, 'false': False}
# Argument count per function: (min, max|None). Wrong arity is rejected at save
# time so a formula like round(x,1,2) or clamp(x,0) can never raise at eval time.
_FUNC_ARITY = {
    'min': (2, None), 'max': (2, None), 'avg': (1, None),
    'abs': (1, 1), 'round': (1, 2), 'sqrt': (1, 1), 'pow': (2, 2),
    'floor': (1, 1), 'ceil': (1, 1), 'clamp': (3, 3),
}
MAX_POW_EXP = 64          # cap exponents so base**exp can't blow up CPU/RAM
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


def validate_expression(expr):
    """Validate an expression. Returns ``(ok: bool, error: str|None,
    refs: list[str])`` — refs are the register names it reads."""
    if not expr or not expr.strip():
        return False, "empty expression", []
    if len(expr) > MAX_EXPR_LEN:
        return False, f"expression too long (max {MAX_EXPR_LEN} chars)", []
    try:
        tree = ast.parse(expr.strip(), mode='eval')
    except SyntaxError as e:
        return False, f"syntax error: {e.msg}", []
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
            if fn is None or fn not in _FUNCS:
                return False, f"function not allowed: {fn or '?'}()", []
            lo, hi = _FUNC_ARITY.get(fn, (0, None))
            n = len(node.args)
            if n < lo or (hi is not None and n > hi):
                want = f"{lo}" if lo == hi else (f"at least {lo}" if hi is None else f"{lo}–{hi}")
                return False, f"{fn}() takes {want} arguments, got {n}", []
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
            if not isinstance(node.value, (int, float, bool)):
                return False, "only numeric constants allowed", []
            continue
        if not isinstance(node, _ALLOWED_NODES):
            return False, f"not allowed: {type(node).__name__}", []
    refs = set()
    _collect_refs(tree.body, refs)
    return True, None, sorted(refs)


def evaluate(expr, resolve, *, prev_resolve=None, dt=0.0):
    """Evaluate a (validated) expression. ``resolve(name)`` returns the numeric
    value of a reference or ``None`` if it has none. ``prev_resolve(ref)`` returns
    the previous value of a reference (for ``prev(...)``) and ``dt`` is the seconds
    since the last evaluation. Raises ``MissingValue`` when a reference (or a
    prev value) is absent, ``ExpressionError`` on a math error."""
    try:
        tree = ast.parse(expr.strip(), mode='eval')
    except SyntaxError as e:
        raise ExpressionError(str(e))
    ctx = {'resolve': resolve, 'prev': prev_resolve, 'dt': float(dt or 0.0)}
    try:
        return _eval(tree.body, ctx)
    except (ZeroDivisionError, OverflowError, ValueError, TypeError) as e:
        raise ExpressionError(str(e))


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
        args = [_eval(a, ctx) for a in node.args]
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
    {"name": "pow", "sig": "pow(x, y)", "desc": "x to the power y"},
    {"name": "floor", "sig": "floor(x)", "desc": "round down"},
    {"name": "ceil", "sig": "ceil(x)", "desc": "round up"},
    {"name": "clamp", "sig": "clamp(x, lo, hi)", "desc": "constrain x to [lo, hi]"},
    {"name": "prev", "sig": "prev(x)", "desc": "previous value of a measurement (for rates)"},
    {"name": "dt", "sig": "dt", "desc": "seconds since this value was last computed"},
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
