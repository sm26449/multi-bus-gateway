"""Optional login/auth for the UI/API — sessions, password hashing, lockout.

Off by default (this appliance targets a trusted LAN). When ``ui.auth.enabled``
is set, the API middleware requires a valid session cookie for everything
except the login page, the login endpoint and static assets; a read-only
``viewer`` role is limited to GET requests. Passwords are stored as PBKDF2
hashes; login failures are rate-limited per client IP.

State is in-memory (sessions + lockout counters reset on restart) — fine for a
single-instance appliance.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 600_000              # OWASP 2023 floor for PBKDF2-HMAC-SHA256
SESSION_TTL_S = 12 * 3600          # 12h sliding session
COOKIE_NAME = "janitza_session"


def hash_password(password: str) -> str:
    """Return a self-describing PBKDF2 hash: algo$iter$salt_hex$hash_hex."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def is_hashed(stored: str) -> bool:
    """True only for a real PBKDF2 hash — not blank and not legacy plaintext.
    Used to refuse enabling login while the default/plaintext password stands."""
    return bool(stored) and stored.startswith(_ALGO + "$")


def verify_password(password: str, stored: str) -> bool:
    """Verify against a stored hash. Falls back to a constant-time plaintext
    compare for hand-edited/legacy configs (value not in hash format)."""
    if not stored:
        return False
    try:
        if stored.startswith(_ALGO + "$"):
            _algo, iter_s, salt_hex, hash_hex = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                     bytes.fromhex(salt_hex), int(iter_s))
            return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:  # noqa: BLE001
        return False
    # legacy plaintext (hand-edited config): still constant-time, but surface it —
    # a successful plaintext auth means the stored credential is not hashed.
    ok = hmac.compare_digest(password, stored)
    if ok:
        logger.warning("SECURITY: authenticated against a PLAINTEXT stored password "
                       "— re-save the credential in the UI so it is hashed (PBKDF2).")
    return ok


# A throwaway PBKDF2 hash of a random secret. authenticate() verifies against it
# when the submitted username matches no account, so a non-existent user costs
# the same one PBKDF2 as a real user with a wrong password (anti-enumeration).
_DECOY_HASH = hash_password(secrets.token_urlsafe(16))


class AuthState:
    """Sessions + lockout for one running instance. Thread-safe."""

    def __init__(self, ui_config):
        self._lock = threading.Lock()
        self._sessions: Dict[str, Tuple[str, float, str]] = {}  # token -> (role, expiry, username)
        self._fails: Dict[str, list] = {}                   # ip -> [failure epochs]
        self._locked_until: Dict[str, float] = {}           # ip -> epoch
        self.reload(ui_config)

    def reload(self, ui_config) -> None:
        """Pick up config changes (enable flag, credentials, lockout params)."""
        with self._lock:
            self.enabled = bool(ui_config.auth_enabled)
            self.admin_user = ui_config.auth_username or "admin"
            self.admin_pw = ui_config.auth_password or ""
            self.viewer_user = ui_config.viewer_username or ""
            self.viewer_pw = ui_config.viewer_password or ""
            self.operator_user = getattr(ui_config, 'operator_username', '') or ""
            self.operator_pw = getattr(ui_config, 'operator_password', '') or ""
            self.lockout_threshold = max(1, int(ui_config.lockout_threshold or 5))
            self.lockout_s = max(1, int(ui_config.lockout_minutes or 5)) * 60

    # ── lockout ────────────────────────────────────────────────────────────
    def is_locked(self, ip: str) -> Optional[int]:
        """Seconds remaining if this IP is locked out, else None."""
        with self._lock:
            until = self._locked_until.get(ip, 0)
        rem = until - time.time()
        return int(rem) if rem > 0 else None

    def _record_failure(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            fails = [t for t in self._fails.get(ip, []) if now - t < self.lockout_s]
            fails.append(now)
            self._fails[ip] = fails
            if len(fails) >= self.lockout_threshold:
                self._locked_until[ip] = now + self.lockout_s
                self._fails[ip] = []

    def _clear_failures(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)
            self._locked_until.pop(ip, None)

    # ── login / sessions ─────────────────────────────────────────────────────
    def authenticate(self, username: str, password: str) -> Optional[str]:
        """Return the role ('admin'|'viewer') on success, else None.

        Constant-work against user enumeration: exactly one ``verify_password``
        runs regardless of whether the username exists — a wrong username costs
        the same PBKDF2 as a wrong password, so response time can't reveal which
        usernames are valid. Username matching is constant-time too."""
        username = username or ""
        matched_role: Optional[str] = None
        matched_stored: str = _DECOY_HASH          # verify against a decoy if no user matches
        for user, stored, role in ((self.admin_user, self.admin_pw, "admin"),
                                   (self.operator_user, self.operator_pw, "operator"),
                                   (self.viewer_user, self.viewer_pw, "viewer")):
            if user and hmac.compare_digest(username, user):
                matched_stored = stored or _DECOY_HASH
                matched_role = role
        ok = verify_password(password, matched_stored)
        return matched_role if (ok and matched_role) else None

    def login(self, ip: str, username: str, password: str) -> Tuple[Optional[str], Optional[str]]:
        """Attempt login. Returns (token, role) on success, (None, None) on
        bad credentials. Raises PermissionError with seconds if locked out."""
        locked = self.is_locked(ip)
        if locked:
            raise PermissionError(locked)
        role = self.authenticate(username, password)
        if role is None:
            self._record_failure(ip)
            return None, None
        self._clear_failures(ip)
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = (role, time.time() + SESSION_TTL_S, username)
        return token, role

    def role_for(self, token: str) -> Optional[str]:
        """Validate a session token; slides the expiry. Returns role or None."""
        if not token:
            return None
        now = time.time()
        with self._lock:
            entry = self._sessions.get(token)
            if not entry:
                return None
            role, expiry, username = entry
            if expiry < now:
                self._sessions.pop(token, None)
                return None
            self._sessions[token] = (role, now + SESSION_TTL_S, username)  # sliding
            return role

    def mint_session(self, role: str, username: str) -> str:
        """Create a session without a password check — the passkey login path
        (the WebAuthn assertion IS the authentication)."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = (role, time.time() + SESSION_TTL_S, username)
        return token

    def identity_for(self, token: str) -> Optional[Tuple[str, str]]:
        """(role, username) for a live session — the audit's 'who'. Does not
        slide the expiry (role_for on the same request already did)."""
        if not token:
            return None
        with self._lock:
            entry = self._sessions.get(token)
            if not entry or entry[1] < time.time():
                return None
            return entry[0], entry[2]

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)
