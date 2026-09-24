# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The browser side of the security model: no inline script anywhere in the
UI, so the CSP can forbid it (script-src 'self') and an injected string can
never execute — whatever an escaping slip elsewhere lets through.

Every action in markup is a data-action attribute dispatched by app-core.js;
these tests keep it that way."""
import glob
import re

import pytest

try:
    from fastapi.testclient import TestClient
except Exception:      # pragma: no cover
    TestClient = None

needs_tc = pytest.mark.skipif(TestClient is None, reason="fastapi testclient unavailable")

_INLINE_HANDLER = re.compile(r'\son[a-z]+\s*=\s*["\']', re.I)
_INLINE_SCRIPT = re.compile(r'<script(?![^>]*\ssrc=)[^>]*>', re.I)
_JS_URL = re.compile(r'javascript:', re.I)


def _ui_sources():
    return ["ui/templates/index.html"] + sorted(glob.glob("ui/js/*.js"))


def test_no_inline_event_handlers_in_ui():
    for path in _ui_sources():
        with open(path, encoding="utf-8") as f:
            src = f.read()
        hits = [m.group(0) for m in _INLINE_HANDLER.finditer(src)]
        assert not hits, f"{path}: inline handlers {hits[:3]} — use data-action / _act()"


def test_no_inline_script_blocks_or_js_urls():
    with open("ui/templates/index.html", encoding="utf-8") as f:
        html = f.read()
    assert not _INLINE_SCRIPT.findall(html), "index.html has an inline <script> block"
    for path in _ui_sources():
        with open(path, encoding="utf-8") as f:
            assert not _JS_URL.search(f.read()), f"{path}: javascript: URL"


def test_no_eval_or_function_constructor_in_ui():
    bad = re.compile(r'\beval\s*\(|new\s+Function\s*\(|set(?:Timeout|Interval)\s*\(\s*["\']')
    for path in sorted(glob.glob("ui/js/*.js")):
        with open(path, encoding="utf-8") as f:
            assert not bad.search(f.read()), f"{path}: string-to-code construct"


def test_every_data_action_names_an_app_method():
    """A data-action whose method does not exist is a dead button. Static
    ones in index.html and the string literals passed to _act() must both
    resolve to a method defined on the app object (`name(` or `async name(`)."""
    methods = set()
    for path in glob.glob("ui/js/*.js"):
        with open(path, encoding="utf-8") as f:
            methods |= set(re.findall(r'^\s{4}(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(', f.read(), re.M))
    missing = set()
    for path in _ui_sources():
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for name in re.findall(r'data-action="([A-Za-z_$][\w$]*)"', src):
            if name not in methods:
                missing.add(name)
        for name in re.findall(r"_act\('([A-Za-z_$][\w$]*)'", src):
            if name not in methods:
                missing.add(name)
    assert not missing, f"data-action targets without a method: {sorted(missing)}"


@needs_tc
def test_csp_forbids_inline_script(tmp_path):
    from tests.test_security import make_app
    _cfg, client = make_app(tmp_path)
    for path in ("/", "/api/status"):
        csp = client.get(path).headers.get("content-security-policy", "")
        directives = {d.strip().split(" ")[0]: d.strip() for d in csp.split(";") if d.strip()}
        assert directives["script-src"] == "script-src 'self'", csp
        assert "'unsafe-inline'" not in directives["script-src"]
        assert "'unsafe-eval'" not in csp


def test_template_command_names_are_identifiers():
    from multibus.device_template import validate_template
    base = {"device_template": {"id": "t1", "name": "T", "registers": [
        {"address": 0, "name": "v", "data_type": "uint16"}]}}
    assert validate_template(base) == []

    def with_cmds(cmds):
        d = {"device_template": dict(base["device_template"])}
        d["device_template"]["commands"] = cmds
        return validate_template(d)

    assert with_cmds({"power_limit": {"params": {"value": {"min": 0, "max": 100}}}}) == []
    errs = with_cmds({"x'); alert(1); //": {}})
    assert errs and "command" in errs[0] and "name invalid" in errs[0]
    errs = with_cmds({"ok": {"params": {"<img src=x onerror=alert(1)>": {}}}})
    assert errs and "param" in errs[0]
    assert with_cmds({"ok": "not a mapping"})
    assert with_cmds("not a mapping")
