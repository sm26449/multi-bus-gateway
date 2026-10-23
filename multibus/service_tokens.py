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
"""Service tokens: how another program (a companion service, a script) signs
in to the API without a person's password.

A token is named after who uses it (``mbg-esphome``) and carries one role,
like a login. It is sent as ``Authorization: Bearer <token>``, and the audit
log names the token, not ``admin``. An admin makes one in Settings →
Security, sees it once, and can revoke it alone, without touching anyone's
password.

Only a SHA-256 of each token is kept (``config/service_tokens.json``, 0600):
a token is 32 random bytes, so a fast hash is enough, and the check costs a
request nothing (a password's PBKDF2 would cost every call half a second).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PREFIX = "mbgst_"
# "ha": a high-availability companion service — reads, sets the gateway's
# role and raises alerts, nothing else
ROLES = ("viewer", "operator", "admin", "ha")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
MAX_TOKENS = 32
_LAST_USED_FLUSH_S = 300           # last-used times reach the disk at most this often


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ServiceTokens:
    """The named tokens of one gateway. Thread-safe."""

    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.Lock()
        self._tokens: Dict[str, Dict] = {}         # name -> {hash, role, created, last_used}
        self._last_flush = 0.0
        self._load()

    # ── persistence ────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            for name, t in (data.get("tokens") or {}).items():
                if (NAME_RE.match(str(name)) and t.get("role") in ROLES
                        and re.fullmatch(r"[0-9a-f]{64}", str(t.get("hash", "")))):
                    self._tokens[str(name)] = {"hash": t["hash"], "role": t["role"],
                                               "created": float(t.get("created") or 0),
                                               "last_used": t.get("last_used")}
            logger.info("loaded %d service token(s)", len(self._tokens))
        except Exception as e:  # noqa: BLE001
            # a corrupt store must not block boot; every token stops working
            logger.warning("service token store unreadable (%s) — no tokens", e)

    def _save_locked(self) -> None:
        if not self._path:
            return
        tmp = self._path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "tokens": self._tokens}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)
        self._last_flush = time.time()

    # ── management (an admin, through the API) ─────────────────────────────
    def create(self, name: str, role: str) -> str:
        """Make a token and return it: the only time it exists in clear."""
        name = (name or "").strip().lower()
        if not NAME_RE.match(name):
            raise ValueError("name: 2–40 characters, a–z 0–9 - _, starting with a letter or digit")
        if role not in ROLES:
            raise ValueError(f"role: one of {', '.join(ROLES)}")
        token = PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            if name in self._tokens:
                raise ValueError(f"a token named {name!r} exists — revoke it first")
            if len(self._tokens) >= MAX_TOKENS:
                raise ValueError(f"at most {MAX_TOKENS} tokens")
            self._tokens[name] = {"hash": _digest(token), "role": role,
                                  "created": time.time(), "last_used": None}
            self._save_locked()
        return token

    def revoke(self, name: str) -> bool:
        with self._lock:
            if self._tokens.pop(name, None) is None:
                return False
            self._save_locked()
            return True

    def list(self) -> List[Dict]:
        with self._lock:
            return [{"name": n, "role": t["role"], "created": t["created"],
                     "last_used": t.get("last_used")}
                    for n, t in sorted(self._tokens.items())]

    def __len__(self) -> int:
        return len(self._tokens)

    # ── checking (every request that carries one) ──────────────────────────
    def check(self, token: str) -> Optional[Tuple[str, str]]:
        """``(name, role)`` for a valid token, else None. Compares against
        every token, in constant time, so timing tells nothing."""
        if not token or not token.startswith(PREFIX) or len(token) > 128:
            return None
        h = _digest(token)
        found = None
        with self._lock:
            for name, t in self._tokens.items():
                if hmac.compare_digest(h, t["hash"]):
                    found = (name, t["role"])
            if found:
                now = time.time()
                self._tokens[found[0]]["last_used"] = now
                if now - self._last_flush > _LAST_USED_FLUSH_S:
                    try:
                        self._save_locked()
                    except OSError as e:
                        logger.warning("service token store write failed: %s", e)
        return found


def bearer(headers) -> str:
    """The token of an ``Authorization: Bearer …`` header, or ''."""
    v = headers.get("authorization", "") or ""
    return v[7:].strip() if v[:7].lower() == "bearer " else ""
