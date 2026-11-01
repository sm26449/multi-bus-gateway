# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""One write path for a device (ADR-0030): a write to `/api/devices/…` (or
to the device's entry by another route, the PQ recorder) goes through
`DeviceStore.devWrite`, which sends `If-Match` and keeps the tag the answer
hands back. A raw `fetch` with a write method would skip both: the page's
next Save would answer a false 412, or a change made elsewhere would be
overwritten without a word (QA-D-08).

Outside ui/js/device-store.js:
- `fetch('/api/devices/…', {method: 'POST'|…})` is refused;
- a raw write whose URL is not written out where it is (a helper's
  parameter, a variable, `${base}…`) is refused unless it is listed as
  never naming a device.
"""
import pathlib
import re

UI = pathlib.Path(__file__).resolve().parent.parent / "ui" / "js"
STORE = "device-store.js"

# a device write: under /api/devices/<something>, or the entry by query
_DEVICE_URL = re.compile(r"""['"`]/api/(?:devices/|pq/config)""")
# not one device: the connection Test of a form, the old dry-run probe, a
# deleted device's kept settings (no entry, no tag)
_NOT_ONE_DEVICE = re.compile(r"""['"`]/api/devices/(?:test['"`?]|x-dry-run|restorable/)""")
_WRITE = re.compile(r"""method\s*:\s*[^,}]*?['"](?:POST|PUT|PATCH|DELETE)['"]|method\s*:\s*method\b|\bmethod\s*[,}]""")
_DEF = re.compile(r'^    (?:async )?([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{', re.M)


def _call_args(text, start):
    """The text inside the parentheses that open at text[start]."""
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    return text[start + 1:]


def _first_arg(args):
    depth = 0
    for i, c in enumerate(args):
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        elif c == ',' and depth == 0:
            return args[:i]
    return args


def _files():
    return sorted(p for p in UI.glob("*.js") if p.name != STORE)


def _device_write(url_text):
    return bool(_DEVICE_URL.search(url_text)) and not _NOT_ONE_DEVICE.search(url_text)


def _methods(text):
    """(name, params, body) of each top-level method of a prototype mixin."""
    starts = list(_DEF.finditer(text))
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        yield m.group(1), [p.strip().split('=')[0].strip() for p in m.group(2).split(',') if p.strip()], \
            text[m.start():end]


def _raw_writes(text):
    """(first argument, line) of each fetch( that writes."""
    for m in re.finditer(r'(?<![\w$.])fetch\s*\(', text):
        args = _call_args(text, m.end() - 1)
        if _WRITE.search(args):
            yield _first_arg(args).strip(), text.count('\n', 0, m.start()) + 1


def test_no_raw_write_to_a_device_url():
    bad = []
    for p in _files():
        text = p.read_text(encoding='utf-8')
        for url, line in _raw_writes(text):
            if _device_write(url):
                bad.append(f"{p.name}:{line}: {url[:80]}")
    assert not bad, "a raw write to a device (use DeviceStore.devWrite):\n" + "\n".join(bad)


# Raw writes whose URL is not written out where they are. Each is allowed
# for the URLs it can build, proven here: the URL expression is followed to
# where it is built (a local variable, a helper's return, the helper's call
# sites), and every /api/ string found there must match the pattern — and
# none may name a device. A new one is added here, or goes through devWrite.
_NOT_A_DEVICE = {
    ('app-settings.js', 'saveAndApply'): r'/api/config/',
    ('app-rules.js', '_ruleOp'): r'/api/(rules|sequences)',
    ('app-endpoints.js', 'saveEndpoint'): r'/api/endpoints',
    ('app-attributes.js', 'attrSave'): r'/api/attributes/',
    ('app-attributes.js', 'attrDelete'): r'/api/attributes/',
    ('app-usedby.js', '_ubRawDelete'): r'/api/(endpoints|device-templates)/',
}

# the URLs with their own revision (ADR-0030): a write sends If-Match
_OWN_REV = re.compile(r"""['"`]/api/(?:registers/selected|energy/fields)""")


def _literal_url(arg):
    """A URL written out where it is used: a string that starts with /api/,
    or `cond ? <that> : <that>` (each branch checked by the device rule)."""
    parts = [arg]
    m = re.match(r"""^[^'"`?]+\?\s*(.+?)\s*:\s*(['"`].+)$""", arg, re.S)
    if m:
        parts = [m.group(1), m.group(2)]
    return all(re.match(r"""^['"`]/api/""", p.strip()) for p in parts)


def _all_methods():
    out = {}
    for p in _files():
        for name, params, body in _methods(p.read_text(encoding='utf-8')):
            out.setdefault(name, []).append((p.name, params, body))
    return out


def _api_strings(text):
    """The /api/… strings written in a piece of code (up to a ${ … })."""
    return re.findall(r"""['"`](/api/[^'"`$]*)""", text)


def _split_args(args):
    out, depth, cur = [], 0, ''
    for c in args:
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        if c == ',' and depth == 0:
            out.append(cur.strip())
            cur = ''
        else:
            cur += c
    if cur.strip():
        out.append(cur.strip())
    return out


def _resolve(expr, params, body, methods, owner, depth=0):
    """The code a URL expression is built from: itself, its local variable's
    assignment, a helper's body, or — for a parameter — every call site's
    argument (one level up)."""
    expr = expr.strip()
    out = [expr]
    if depth > 2:
        return out
    m = re.match(r'^(\w+)$', expr)
    if m:
        name = m.group(1)
        # const url = …;  or  const [url, body] = this._helper(…);
        for a in re.finditer(r'\b(?:const|let|var)\s+(?:' + name + r'|\[[^\]]*\b' + name + r'\b[^\]]*\])\s*=\s*([^;]+);', body):
            out += _resolve(a.group(1), params, body, methods, owner, depth + 1)
        if name in params:
            for meth in methods.values():
                for _f, cparams, cbody in meth:
                    for c in re.finditer(r'\bthis\.' + re.escape(owner) + r'\s*\(', cbody):
                        args = _split_args(_call_args(cbody, c.end() - 1))
                        i = params.index(name)
                        if i >= len(args):
                            continue
                        arg = args[i]
                        cname = next((n for n, ms in methods.items() if any(b is cbody for _x, _y, b in ms)), owner)
                        out += _resolve(arg, cparams, cbody, methods, cname, depth + 1)
    # a template: each ${name} in it, followed the same way
    for v in re.finditer(r'\$\{(\w+)\}', expr):
        out += _resolve(v.group(1), params, body, methods, owner, depth + 1)
    for h in re.finditer(r'\bthis\.(\w+)\s*\(', expr):
        for _f, hparams, hbody in methods.get(h.group(1), []):
            out.append(hbody)
    return out


def test_every_other_raw_write_is_proven_not_to_name_a_device():
    methods = _all_methods()
    bad, seen = [], set()
    for p in _files():
        for name, params, body in _methods(p.read_text(encoding='utf-8')):
            for url, _line in _raw_writes(body):
                if _literal_url(url):
                    continue
                key = (p.name, name)
                seen.add(key)
                pat = _NOT_A_DEVICE.get(key)
                if pat is None:
                    bad.append(f"{p.name} {name}(): fetch({url[:60]}, …) — not listed")
                    continue
                code = _resolve(url, params, body, methods, name)
                found = [u for c in code for u in _api_strings(c)]
                wrong = [u for u in found if not re.match(pat, u) or _device_write("'" + u)]
                if not found or wrong:
                    bad.append(f"{p.name} {name}(): fetch({url[:40]}) builds {wrong or 'no /api/ URL it can prove'}")
    assert not bad, "a raw write whose URL may name a device (use DeviceStore.devWrite):\n" + "\n".join(bad)
    assert set(_NOT_A_DEVICE) <= seen, f"listed but gone: {set(_NOT_A_DEVICE) - seen}"


def test_a_fetch_says_its_method_where_it_is():
    """`fetch(url, opts)`: what it does cannot be read here — the options
    are an object literal at the call."""
    bad = []
    for p in _files():
        text = p.read_text(encoding='utf-8')
        for m in re.finditer(r'(?<![\w$.])fetch\s*\(', text):
            args = _call_args(text, m.end() - 1)
            first = _first_arg(args)
            rest = args[len(first):].lstrip()
            if rest.startswith(','):
                second = rest[1:].strip()
                if second and not second.startswith('{'):
                    bad.append(f"{p.name}:{text.count(chr(10), 0, m.start()) + 1}: fetch({first[:40]}, {second[:30]})")
    assert not bad, "a fetch whose options are not written at the call:\n" + "\n".join(bad)


def test_a_write_to_a_revised_store_sends_its_revision():
    """The selection and the Energy fields have their own revision: a write
    to them sends If-Match (a view holds the tag of the rows it shows)."""
    bad = []
    for p in _files():
        for name, _params, body in _methods(p.read_text(encoding='utf-8')):
            for url, line in _raw_writes(body):
                if _OWN_REV.search(url) and 'If-Match' not in body:
                    bad.append(f"{p.name} {name}(): fetch({url[:50]}) without If-Match")
    assert not bad, "\n".join(bad)


def test_the_store_is_loaded_before_the_pages_that_write():
    html = (UI.parent / "templates" / "index.html").read_text(encoding='utf-8')
    srcs = re.findall(r'<script src="/static/js/([\w.-]+)\.js', html)
    assert 'device-store' in srcs, "ui/templates/index.html does not load device-store.js"
    first_user = min(srcs.index(s) for s in srcs if s.startswith('app-') and s != 'app-core')
    assert srcs.index('device-store') < first_user


def test_an_uploaded_map_is_assigned_by_the_model_section_alone():
    """L3-F11: "Upload map" assigned the imported template by a PUT of the
    whole device built from the cached list row — a connection edit saved
    meanwhile was undone. It is the Edit modal's Model section now: PATCH
    {sections: {model: {template}}} with the device's own tag."""
    text = (UI / "app-registers.js").read_text(encoding='utf-8')
    body = text[text.index("async _uploadMapAndAssign("):]
    body = body[:body.index("\n    },\n")]
    assert "'PATCH'" in body and "sections: { model: { template: tplId } }" in body
    assert "'PUT'" not in body and "connection:" not in body and "_connPayloadOf" not in body
    assert "ifMatch" in body


def test_the_checker_sees_what_it_should():
    """The patterns themselves: a raw device write is caught, a GET and the
    allowed paths are not."""
    raw = "await fetch(`/api/devices/${encodeURIComponent(id)}/write-lock`, { method: 'POST', body: x });"
    assert [_device_write(u) for u, _ in _raw_writes(raw)] == [True]
    assert not list(_raw_writes("fetch(`/api/devices/${id}/commands`)"))
    assert [_device_write(u) for u, _ in _raw_writes("fetch('/api/devices/test', { method: 'POST' })")] == [False]
    assert [_device_write(u) for u, _ in _raw_writes("fetch('/api/pq/config?device=' + id, { method: 'POST' })")] == [True]
    assert [_device_write(u) for u, _ in _raw_writes("fetch(url, { method, headers: {} })")] == [False]
    # a method chosen at run time is a write; a URL in a variable is not "literal"
    assert [u for u, _ in _raw_writes("fetch(url, { method: editing ? 'PUT' : 'POST' })")] == ['url']
    assert not _literal_url('url') and _literal_url("e ? `/api/bridges/${x}` : '/api/bridges'")
