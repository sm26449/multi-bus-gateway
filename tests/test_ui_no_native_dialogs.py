# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The page asks through its own dialog (ui/js/app-dialogs.js), never the
browser's confirm()/prompt()/alert(): those put "Yes:/No:" in the text
because their buttons say OK/Cancel, sent a typed "abc" on as null, and
cannot be styled, translated or answered by a test (audit 2026-10-21, G13 /
R-M1)."""
import glob
import re

_NATIVE = re.compile(r"(^|[^.\w$])(confirm|prompt|alert)\s*\(|\bwindow\.(confirm|prompt|alert)\s*\(")


def _code(line):
    """The line without a trailing // comment (good enough for this check:
    no URL with // sits on a line that also calls a dialog)."""
    s = line.strip()
    if s.startswith('//') or s.startswith('*'):
        return ''
    return line.split(' // ', 1)[0]


def test_no_native_dialog_in_the_ui():
    hits = []
    for path in sorted(glob.glob('ui/js/*.js')):
        for n, line in enumerate(open(path, encoding='utf-8'), 1):
            if _NATIVE.search(_code(line)):
                hits.append(f"{path}:{n}: {line.strip()[:100]}")
    assert not hits, "native dialogs left (use confirmAction / askValue(s)):\n" + "\n".join(hits)


def test_the_check_sees_every_spelling():
    for s in ("if (!confirm('x')) return;", "const v = prompt('a', '');",
              "catch (e) { alert(e.message); }", "const k = window.prompt('key');"):
        assert _NATIVE.search(_code(s)), s
    for s in ("this.confirmAction({})", "await this.askValue({})", "// confirm() is gone",
              "x.prompt('a')", "_confirmCommand(cmd)"):
        assert not _NATIVE.search(_code(s)), s


def test_the_dialog_is_loaded_and_its_buttons_are_never_role_hidden():
    html = open('ui/templates/index.html', encoding='utf-8').read()
    at = lambda f: html.index(f'<script src="/static/js/{f}')
    assert at('app-core.js') < at('app-dialogs.js') < at('app-auth.js')
    for el in ('appDialogOk', 'appDialogCancel', 'appDialogClose', 'appKeyOk', 'appKeyCancel'):
        tag = re.search(r'<[^>]*id="%s"[^>]*>' % el, html).group(0)
        assert 'data-admin' not in tag and 'data-live' not in tag and 'data-action' not in tag, tag
