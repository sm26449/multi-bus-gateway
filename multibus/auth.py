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
"""Optional login/auth for the UI/API — sessions, password hashing, lockout.

Off by default (this appliance targets a trusted LAN). When ``ui.auth.enabled``
is set, the API middleware requires a valid session cookie for everything
except the login page, the login endpoint and static assets; a read-only
``viewer`` role is limited to GET requests. Passwords are stored as PBKDF2
hashes; login failures are rate-limited per client IP.

Sessions PERSIST across restarts (config/sessions.json, 0600): a container
restart/upgrade must not log everyone out — the cookie is valid for 7 days,
so the server-side record has to live at least as long. Only SHA-256 hashes
of the tokens touch the disk; the raw token exists nowhere but the client's
cookie. Lockout counters stay in-memory (worst case a restart forgives a
brute-force window — acceptable for a LAN appliance).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 600_000              # OWASP 2023 floor for PBKDF2-HMAC-SHA256
SESSION_TTL_S = 7 * 24 * 3600      # 7-day sliding session (slides on each request)
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
    # encode to bytes: compare_digest on str raises TypeError for non-ASCII,
    # which would leak (via the exception) that the stored value is plaintext
    ok = hmac.compare_digest(password.encode("utf-8"), stored.encode("utf-8"))
    if ok:
        logger.warning("SECURITY: authenticated against a PLAINTEXT stored password "
                       "— re-save the credential in the UI so it is hashed (PBKDF2).")
    return ok


# A throwaway PBKDF2 hash of a random secret. authenticate() verifies against it
# when the submitted username matches no account, so a non-existent user costs
# the same one PBKDF2 as a real user with a wrong password (anti-enumeration).
_DECOY_HASH = hash_password(secrets.token_urlsafe(16))


# A slid expiry is persisted at most this often — sliding happens on EVERY
# request, and rewriting the file each time would hammer the disk for a value
# whose precision is irrelevant against a 7-day TTL.
_SLIDE_FLUSH_S = 300


def _token_key(token: str) -> str:
    """Disk/lookup key for a session token — the raw token never leaves the
    client's cookie, so a read of the persisted file yields nothing usable."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthState:
    """Sessions + lockout for one running instance. Thread-safe.

    ``store_path`` enables persistence (the API passes the config-dir file);
    None keeps the pure in-memory behavior (unit tests, ad-hoc embedding).
    """

    def __init__(self, ui_config, store_path: Optional[str] = None):
        self._lock = threading.Lock()
        # token_sha256 -> (role, expiry, username)
        self._sessions: Dict[str, Tuple[str, float, str]] = {}
        self._fails: Dict[str, list] = {}                   # ip -> [failure epochs]
        self._locked_until: Dict[str, float] = {}           # ip -> epoch
        self._store_path = store_path
        self._last_flush = 0.0
        self.reload(ui_config)
        self._load_sessions()

    # ── persistence ────────────────────────────────────────────────────────
    def _load_sessions(self) -> None:
        if not self._store_path or not os.path.exists(self._store_path):
            return
        try:
            with open(self._store_path) as f:
                data = json.load(f)
            now = time.time()
            with self._lock:
                for key, entry in (data.get("sessions") or {}).items():
                    role, expiry, username = entry
                    if float(expiry) > now:               # prune expired at load
                        self._sessions[str(key)] = (str(role), float(expiry), str(username))
                n = len(self._sessions)
            logger.info("restored %d session(s) from %s", n, self._store_path)
        except Exception as e:  # noqa: BLE001
            # a corrupt store must never block boot — everyone just re-logs-in
            logger.warning("session store unreadable (%s) — starting empty", e)

    def _save_sessions_locked(self) -> None:
        """Persist under self._lock. Atomic + 0600 like the other secret-bearing
        writers (passkeys/config); losing a write worst-cases as a re-login."""
        if not self._store_path:
            return
        try:
            tmp = self._store_path + ".tmp"
            payload = json.dumps({"version": 1, "sessions": {
                k: [r, e, u] for k, (r, e, u) in self._sessions.items()}})
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._store_path)
            self._last_flush = time.time()
        except Exception as e:  # noqa: BLE001
            logger.warning("session store write failed: %s", e)

    def reload(self, ui_config) -> None:
        """Pick up config changes (enable flag, credentials, lockout params).

        The default-credential guard lives HERE, not only in the UI route
        (external audit): hand-editing ui.auth.enabled, config import and
        snapshot restore all reach reload() directly — enabling auth with an
        empty or literal-default password produced a gateway that LOOKED
        locked and accepted admin/admin from the whole LAN."""
        with self._lock:
            self.enabled = bool(ui_config.auth_enabled)
            _pw = ui_config.auth_password or ""
            if self.enabled and (_pw == "" or _pw == "admin"):
                self.enabled = False
                logger.error(
                    "SECURITY: refusing to enable login with an empty/default "
                    "admin password — set a real password (Settings → Security "
                    "hashes it) and re-enable. Auth stays OFF.")
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
            if user and hmac.compare_digest(username.encode("utf-8"), user.encode("utf-8")):
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
            self._sessions[_token_key(token)] = (role, time.time() + SESSION_TTL_S, username)
            self._save_sessions_locked()
        return token, role

    def role_for(self, token: str) -> Optional[str]:
        """Validate a session token; slides the expiry. Returns role or None."""
        if not token:
            return None
        now = time.time()
        key = _token_key(token)
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            role, expiry, username = entry
            if expiry < now:
                self._sessions.pop(key, None)
                self._save_sessions_locked()
                return None
            self._sessions[key] = (role, now + SESSION_TTL_S, username)  # sliding
            # slides happen per request — persist at most every _SLIDE_FLUSH_S
            # (losing a slide worst-cases as an expiry a few minutes short of
            # 7 days after an unclean stop; irrelevant, and logins/logouts
            # flush immediately anyway)
            if now - self._last_flush > _SLIDE_FLUSH_S:
                self._save_sessions_locked()
            return role

    def mint_session(self, role: str, username: str) -> str:
        """Create a session without a password check — the passkey login path
        (the WebAuthn assertion IS the authentication)."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[_token_key(token)] = (role, time.time() + SESSION_TTL_S, username)
            self._save_sessions_locked()
        return token

    def identity_for(self, token: str) -> Optional[Tuple[str, str]]:
        """(role, username) for a live session — the audit's 'who'. Does not
        slide the expiry (role_for on the same request already did)."""
        if not token:
            return None
        with self._lock:
            entry = self._sessions.get(_token_key(token))
            if not entry or entry[1] < time.time():
                return None
            return entry[0], entry[2]

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(_token_key(token), None)
            self._save_sessions_locked()

    def revoke_all_sessions(self) -> int:
        """Invalidate every live session (e.g. after a credential rotation, so a
        stolen/old cookie stops working the moment the password changes).
        Returns the count revoked."""
        with self._lock:
            n = len(self._sessions)
            self._sessions.clear()
            self._save_sessions_locked()
            return n
