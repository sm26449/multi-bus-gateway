# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""There is no global `t()`: a method that translates calls `this.t(...)` or
binds a local `const t = (k, d) => this.t(k, d)` first. A bare `t(` in a
method without that binding throws ReferenceError the first time the line
runs. It happened on the Rules page (QA-R2-02): a rule whose last decision
had a result ("error", "success" …) made `_ruleResult` throw, the whole card
list stopped redrawing, and the page kept showing a rule as SHADOW after it
was armed."""
import pathlib
import re

UI = pathlib.Path(__file__).resolve().parent.parent / "ui" / "js"

# a method of a prototype mixin starts at four spaces: `name(` or `async name(`;
# its parameter list may run over several lines, up to `) {`
_DEF = re.compile(r'^    (?:async )?([A-Za-z_$][\w$]*)\s*\(')
_NOT_METHODS = ('if', 'for', 'while', 'switch', 'catch', 'function', 'return')
_BARE_T = re.compile(r'(?<![\w$.])t\(')
# a local binding of t: a declaration (`const t =`, `let a = 1, t =`) …
_DECL_T = re.compile(r'\b(?:const|let|var)\b[^;]*?(?<![\w$.])t\s*=(?![=>])')
# … or a parameter named t: `(t)`, `(a, t = …)`, `function (t, b)`, `t => …`
_PARAMS = re.compile(r'(?:\bfunction\b\s*[\w$]*\s*)?\(([^()]*)\)\s*(?:=>|\{)')
_ARROW_T = re.compile(r'(?<![\w$.])t\s*=>')
# one parameter: a name, maybe `...rest`, maybe a default (`t = x`, never `t === x`)
_PARAM = re.compile(r'\s*(?:\.\.\.)?([A-Za-z_$][\w$]*)\s*(?:=(?![=>]).*)?', re.S)
_CONTROL = re.compile(r'\b(?:if|while|for|switch|catch|with)\s*$')


def _binds_t(text: str) -> bool:
    if _DECL_T.search(text) or _ARROW_T.search(text):
        return True
    for m in _PARAMS.finditer(text):
        if _CONTROL.search(text[:m.start()]):
            continue                               # `if (t) {` is a condition, not a parameter
        names = [_PARAM.fullmatch(p) for p in m.group(1).split(',')]
        if any(n and n.group(1) == 't' for n in names):
            return True
    return False


def _methods(path):
    """(name, first line number, text) for every top-level method of a
    prototype mixin (four-space indent, as every ui/js/app-*.js writes them),
    a parameter list over several lines included."""
    name, start, body = None, 0, []
    lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
    for n, line in enumerate(lines, 1):
        m = _DEF.match(line)
        if m and m.group(1) not in _NOT_METHODS and not line.rstrip().endswith(';'):
            if name:
                yield name, start, ''.join(body)
            name, start, body = m.group(1), n, [line]
        elif name:
            body.append(line)
    if name:
        yield name, start, ''.join(body)


def _code(text):
    # a comment line is not a call
    return ''.join('' if x.lstrip().startswith(('//', '*', '/*')) else x for x in text.splitlines(keepends=True))


def test_the_binding_rule_is_narrow():
    assert _binds_t("const t = (k, d) => this.t(k, d);")
    assert _binds_t("let a = 1, t = this.t.bind(this);")
    assert _binds_t("(t) => t('x')") and _binds_t("rows.map((r, t) => t)") and _binds_t("t => t('x')")
    assert _binds_t("_x(\n        a,\n        t,\n    ) {")                     # a parameter list over lines
    assert not _binds_t("this.t = 1; obj.t = 2; if (t === 1) {}")
    assert not _binds_t("if (t) { x(); } while (t) {}")
    assert not _binds_t("const tt = 1; x.t(k); (tx) => 1")


def test_a_method_with_its_parameters_over_several_lines_is_one_method(tmp_path):
    p = tmp_path / "app-x.js"
    p.write_text("Object.assign(App.prototype, {\n    _a(\n        b,\n        t,\n    ) {\n        return t('k');\n    },\n"
                 "    _b() {\n        return 1;\n    },\n});\n", encoding="utf-8")
    got = {name: text for name, _s, text in _methods(p)}
    assert set(got) == {'_a', '_b'} and _binds_t(_code(got['_a']))


def test_no_bare_t_without_a_local_binding():
    paths = sorted(UI.glob('app-*.js'))
    assert paths, UI
    bad, translating = [], 0
    for path in paths:
        for name, start, text in _methods(path):
            text = _code(text)
            if name != 't' and _BARE_T.search(text):
                translating += 1
                if not _binds_t(text):
                    bad.append(f"{path.name}:{start} {name}")
    assert translating > 50, translating                        # the scan sees the methods that bind t
    assert not bad, f"bare t(...) with no `const t = …` in the method (use this.t): {bad}"
