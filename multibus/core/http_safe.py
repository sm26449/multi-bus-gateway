# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""A urllib opener that refuses HTTP redirects.

An outbound POST to an operator-configured URL (the alerts webhook, the alertd
producer) carries that endpoint's credentials (``X-API-Key`` …). A 3xx to an
attacker-controlled host would replay them off-site — the classic SSRF credential
leak. A notifier endpoint has no legitimate reason to redirect, so we hard-fail
instead of following. Shared so there is one guard, not a copy per caller.
"""
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, f"redirect blocked → {newurl}", headers, fp)


# One shared opener. Callers patch its ``.open`` in tests; monkeypatching the
# single object is seen by every importer (it is the same object everywhere).
NO_REDIRECT_OPENER = urllib.request.build_opener(NoRedirect())
