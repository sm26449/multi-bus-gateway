"""Redact secrets from URLs before they reach the logs.

A webhook / REST-push / device URL can carry credentials in two places:
  * userinfo  — ``https://user:token@host/path``
  * query     — ``https://host/ingest?api_key=SECRET&token=…``
Logging the raw URL (on a failed POST, say) leaks those into the log file. This
turns such a URL into a safe-to-log form: userinfo dropped, sensitive query
values replaced with ``***``. Everything else (host, port, path, benign params)
is kept so the log stays useful for debugging.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query keys whose *value* is a secret (matched case-insensitively, substring).
_SECRET_KEYS = ("token", "key", "secret", "password", "passwd", "pwd", "sig",
                "signature", "auth", "access", "credential", "sas")


def _is_secret_key(k: str) -> bool:
    kl = k.lower()
    return any(s in kl for s in _SECRET_KEYS)


def redact_url(url: str) -> str:
    """Return ``url`` with userinfo dropped and secret query values masked.

    Never raises — an unparseable input yields a coarse ``<scheme>://<host>``
    (or ``<redacted-url>`` if even that is unavailable), never the raw string.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
        # netloc without userinfo: keep host[:port] only
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        query = urlencode([(k, "***" if _is_secret_key(k) else v)
                           for k, v in parse_qsl(parts.query, keep_blank_values=True)])
        return urlunsplit((parts.scheme, host, parts.path, query, parts.fragment))
    except Exception:  # noqa: BLE001
        try:
            p = urlsplit(url)
            return f"{p.scheme}://{p.hostname or ''}" if p.scheme else "<redacted-url>"
        except Exception:  # noqa: BLE001
            return "<redacted-url>"
