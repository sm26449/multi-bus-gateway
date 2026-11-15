# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The SPA shell: render index.html with each static asset's ?v= cache-bust
token stamped from its mtime, the build hash a loaded page carries, and the
<head> markup that steers the browser to the canonical HTTPS host. Pure string
work over files under ui/; no FastAPI."""
import hashlib
import os
import re
from html import escape as _html_escape

from multibus import __version__

# Static assets referenced as /static/<path>?v=<token>. The token is rewritten
# at serve time from the file's mtime (see _render_index_html) so a changed
# app.js/css busts the browser cache automatically — no hand-edited ?v= tokens.
_STATIC_ASSET_RE = re.compile(r'(/static/([^"\'?\s]+))\?v=[^"\'\s]*')


_INDEX_CACHE = {"key": None, "html": None}


def _canonical_redirect_script(canonical_url: str) -> str:
    """The <head> markup that steers the browser to the canonical HTTPS host
    (so TLS + passkeys are the default) WITHOUT locking out the local IP. The
    logic lives in the static ui/js/canonical.js — the page carries no inline
    script, so the CSP can forbid inline code entirely — and the target URL
    travels in a <meta> attribute, HTML-escaped, where no JS-string or
    </script> trick can turn it into code. It is client-side on purpose: the
    page loads first, so a down hostname is recoverable (`?local` in the URL,
    or the sticky `mbg-stay-local` flag the script sets)."""
    if not canonical_url:
        return ""
    return ('<meta name="mbg-canonical" content="' + _html_escape(canonical_url, quote=True)
            + '"><script src="/static/js/canonical.js?v=0"></script>')


def _render_index_html(path: str = "ui/templates/index.html",
                       canonical_url: str = "") -> str:
    """Return the SPA shell with each static asset's ?v= cache-bust token set to
    the asset's mtime (files are served from ui/ at /static/). The rendered HTML
    is cached and only re-read+re-stamped when the template file changes — the
    old path re-read the 138 KB file and ran the regex on EVERY request (page
    load, SPA nav, unauth redirect). The page carries its build (`mbg-build`):
    a tab left open across a deploy compares it with `/api/status`."""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = None
    key = (mt, canonical_url, path)
    if mt is not None and _INDEX_CACHE["key"] == key and _INDEX_CACHE["html"] is not None:
        return _INDEX_CACHE["html"]
    with open(path, encoding="utf-8") as _f:
        html = _f.read()

    def _stamp(m):
        rel = m.group(2)
        try:
            ver = int(os.path.getmtime(os.path.join("ui", rel)))
        except OSError:
            ver = 0
        return f"{m.group(1)}?v={ver}"

    rendered = _STATIC_ASSET_RE.sub(_stamp, html)
    build = _build_of(rendered)
    # inject the canonical redirect as the FIRST thing in <head> so it runs
    # before the heavy JS loads (no flash of the app on the wrong host)
    head = (_STATIC_ASSET_RE.sub(_stamp, _canonical_redirect_script(canonical_url))
            + f'<meta name="mbg-build" content="{build}">')
    rendered = rendered.replace("<head>", "<head>" + head, 1)
    _INDEX_CACHE.update(key=key, html=rendered, build=build)
    return rendered


def _build_of(stamped_html: str) -> str:
    """The page's build: its shell with every asset's stamp, and the version."""
    return hashlib.sha1((__version__ + stamped_html).encode()).hexdigest()[:12]


def ui_build(path: str = "ui/templates/index.html") -> str:
    """The build a page loaded now would carry ('' when the shell is unreadable)."""
    try:
        k = _INDEX_CACHE["key"]
        if _INDEX_CACHE.get("build") is None or k[0] != os.path.getmtime(path) or k[2] != path:
            _render_index_html(path, k[1] if k else "")
        return _INDEX_CACHE.get("build") or ""
    except (OSError, TypeError):
        return ""
