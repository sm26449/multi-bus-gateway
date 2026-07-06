"""Passkeys (WebAuthn) — password-less login for the UI.

A passkey is registered by a logged-in user (or by the implicit admin while
auth is still off — enroll first, enable login after) and is bound to the
RP ID it was created under. Browsers only run WebAuthn in a SECURE CONTEXT
and refuse raw-IP RP IDs, so on the LAN this means ``localhost`` on the box
itself, or a real hostname (e.g. ``gateway.lan``) served over HTTPS.

Credentials live in ``config/passkeys.json``; challenges are one-shot,
server-side and short-lived. Verification (signature, origin, RP ID hash,
sign count) is py_webauthn's.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_CHALLENGE_TTL_S = 120


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _from_b64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def rp_id_for_host(host: str) -> Optional[str]:
    """The WebAuthn RP ID for a request host — or None when passkeys cannot
    work there (raw IPs are rejected by browsers; only hostnames qualify)."""
    host = (host or "").strip().lower()
    if not host:
        return None
    if host == "localhost":
        return "localhost"
    try:
        ipaddress.ip_address(host)
        return None                      # an IP address is not a valid RP ID
    except ValueError:
        return host


class ChallengeCache:
    """One-shot, expiring challenges keyed by an opaque state token."""

    def __init__(self, ttl_s: int = _CHALLENGE_TTL_S):
        self.ttl = ttl_s
        self._lock = threading.Lock()
        self._pending: Dict[str, Dict] = {}

    def put(self, **meta) -> str:
        state = secrets.token_urlsafe(24)
        with self._lock:
            now = time.time()
            self._pending = {k: v for k, v in self._pending.items()
                             if v["ts"] + self.ttl > now}       # prune expired
            if len(self._pending) > 64:                          # flood guard
                self._pending.clear()
            self._pending[state] = {**meta, "ts": now}
        return state

    def take(self, state: str) -> Optional[Dict]:
        """Consume a pending challenge (single use)."""
        with self._lock:
            meta = self._pending.pop(state or "", None)
        if meta and meta["ts"] + self.ttl > time.time():
            return meta
        return None


class PasskeyStore:
    """Registered credentials, persisted as JSON next to config.yaml."""

    def __init__(self, path: str = "config/passkeys.json"):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._creds: List[Dict] = []
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._creds = json.loads(self.path.read_text()).get("credentials", [])
        except Exception:  # noqa: BLE001 — a corrupt store must not block boot
            logger.exception("passkey store unreadable — starting empty")
            self._creds = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"credentials": self._creds}, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def add(self, *, cred_id: bytes, public_key: bytes, sign_count: int,
            user: str, role: str, rp_id: str, label: str = "") -> Dict:
        entry = {"id": _b64u(cred_id), "public_key": _b64u(public_key),
                 "sign_count": int(sign_count), "user": user, "role": role,
                 "rp_id": rp_id, "label": label[:60] or "passkey",
                 "created": round(time.time(), 3)}
        with self._lock:
            self._creds = [c for c in self._creds if c["id"] != entry["id"]]
            self._creds.append(entry)
            self._save()
        return entry

    def list(self, *, user: Optional[str] = None) -> List[Dict]:
        with self._lock:
            out = [dict(c) for c in self._creds
                   if user is None or c["user"] == user]
        for c in out:
            c.pop("public_key", None)    # UI never needs the key material
        return out

    def find(self, cred_id_b64u: str) -> Optional[Dict]:
        with self._lock:
            return next((dict(c) for c in self._creds
                         if c["id"] == cred_id_b64u), None)

    def for_rp(self, rp_id: str) -> List[Dict]:
        with self._lock:
            return [dict(c) for c in self._creds if c["rp_id"] == rp_id]

    def update_sign_count(self, cred_id_b64u: str, new_count: int) -> None:
        with self._lock:
            for c in self._creds:
                if c["id"] == cred_id_b64u:
                    c["sign_count"] = int(new_count)
                    self._save()
                    return

    def delete(self, cred_id_b64u: str, *, user: Optional[str] = None) -> bool:
        """Remove a credential; when ``user`` is given, only that user's own."""
        with self._lock:
            keep = [c for c in self._creds
                    if not (c["id"] == cred_id_b64u
                            and (user is None or c["user"] == user))]
            if len(keep) == len(self._creds):
                return False
            self._creds = keep
            self._save()
            return True

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._creds)
