# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
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
"""Append-only audit trail — who changed what, when, from where.

Separate from the event log on purpose: events are a small live-diagnostics
ring (300 entries), while an audit answers "who touched the thresholds three
weeks ago" — so it lives in its own JSONL files with size-based rotation and
never evicts on volume spikes elsewhere.

Request payloads are recorded REDACTED (any key that smells like a secret is
masked before it touches disk) and truncated — the audit must never become
the place where the MQTT password leaks.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from .redact import _is_secret_key, redact_url

logger = logging.getLogger(__name__)

_MAX_DETAIL = 2000          # chars of redacted payload per entry
_ROTATE_BYTES = 2 << 20     # 2 MB per file
_KEEP_FILES = 5             # audit.jsonl + audit.jsonl.1..5 (≈ 12 MB in all)
# AUD-RET-1: an unchanged fact repeated (an EMS re-sending a setpoint the
# device already holds) is written once when the streak starts, then as one
# summary per hour / when it ends — never as a line per repeat
_REPEAT_SUMMARY_S = 3600.0
_LOGIN_FAIL_SUMMARY_S = 600.0


def _is_url_key(k: str) -> bool:
    kl = k.lower()
    return any(s in kl for s in ("url", "uri", "endpoint"))


def redact_obj(obj: Any, depth: int = 0) -> Any:
    """Deep-copy ``obj`` with secret-keyed values masked and URL-keyed string
    VALUES passed through redact_url (a url can embed userinfo/tokens that a
    key-name check alone would keep). Never raises."""
    try:
        if depth > 8:
            return "…"
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                ks = str(k)
                if _is_secret_key(ks):
                    out[k] = "***"
                elif ks in ('latitude_deg', 'longitude_deg'):
                    # coordinates are identity: coarse wherever a log or a
                    # diff shows them (attribute_catalog, privacy)
                    out[k] = round(float(v), 2) if isinstance(v, (int, float)) and not isinstance(v, bool) else "***"
                elif ks == 'altitude_m':
                    out[k] = "***"
                elif _is_url_key(ks) and isinstance(v, str):
                    out[k] = redact_url(v)
                else:
                    out[k] = redact_obj(v, depth + 1)
            return out
        if isinstance(obj, list):
            return [redact_obj(v, depth + 1) for v in obj[:50]]
        return obj
    except Exception:  # noqa: BLE001
        return "<unredactable>"


class AuditLog:
    """Thread-safe JSONL audit file with size-based rotation."""

    def __init__(self, path: str = "config/audit.jsonl",
                 rotate_bytes: int = _ROTATE_BYTES, keep: int = _KEEP_FILES):
        self.path = Path(path)
        self.rotate_bytes = rotate_bytes
        self.keep = keep
        self._lock = threading.Lock()
        # repeated no-op commands (AUD-RET-1) and refused logins per client IP
        # (AUD-LOGIN-1): one entry per streak + counted summaries
        self.repeats = AuditCoalescer(self)
        self.login_failures = AuditCoalescer(self, every_s=_LOGIN_FAIL_SUMMARY_S)
        # tighten an already-existing file (and its rotated archives) that a
        # pre-0600 build left world-readable — new files are created 0600
        for _p in (self.path, *(self.path.with_name(f"{self.path.name}.{i}")
                                for i in range(1, keep + 1))):
            try:
                if _p.exists():
                    os.chmod(_p, 0o600)
            except OSError:
                pass

    def append(self, *, user: str, ip: str, action: str, target: str = "",
               status: str = "ok", detail: Any = None) -> None:
        """Record one action. Never raises — an audit failure must not break
        the request it describes (it is logged loudly instead)."""
        try:
            # a target that IS a URL (device/builder actions) may carry
            # credentials — store its redacted form, like detail values
            if "://" in target:
                target = redact_url(target)
            entry = {"ts": round(time.time(), 3), "user": user or "-",
                     "ip": ip or "-", "action": action[:200],
                     "target": target[:200], "status": status[:40]}
            if detail is not None:
                s = json.dumps(redact_obj(detail), ensure_ascii=False)
                entry["detail"] = s[:_MAX_DETAIL]
            line = json.dumps(entry, ensure_ascii=False)
            with self._lock:
                self._rotate_if_needed(len(line) + 1)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # audit trail carries identities/IPs/actions — keep it 0600
                # (mode applies only when this call creates the file).
                fd = os.open(str(self.path),
                             os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            # a second copy in the process log for the host's own rotation
            # (3.80.0) — at DEBUG since LOG-CMD-1: the audit file is the
            # record, and the INFO echo was a full JSON line per command
            logger.debug("AUDIT %s", line)
        except Exception:  # noqa: BLE001
            logger.exception("audit append failed")

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size if self.path.exists() else 0
            if size + incoming <= self.rotate_bytes:
                return
            oldest = self.path.with_suffix(self.path.suffix + f".{self.keep}")
            oldest.unlink(missing_ok=True)
            for i in range(self.keep - 1, 0, -1):
                src = self.path.with_suffix(self.path.suffix + f".{i}")
                if src.exists():
                    os.replace(src, self.path.with_suffix(self.path.suffix + f".{i + 1}"))
            os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
        except OSError:
            logger.exception("audit rotation failed")

    # ── read side ────────────────────────────────────────────────────────────

    def _files_newest_first(self) -> List[Path]:
        out = [self.path] if self.path.exists() else []
        for i in range(1, self.keep + 1):
            p = self.path.with_suffix(self.path.suffix + f".{i}")
            if p.exists():
                out.append(p)
        return out

    def recent(self, limit: int = 100, *, q: str = "",
               user: str = "") -> List[Dict]:
        """Newest entries first, optionally filtered by substring / user."""
        ql = q.lower()
        out: List[Dict] = []
        for path in self._files_newest_first():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if user and e.get("user") != user:
                    continue
                if ql and ql not in json.dumps(e, ensure_ascii=False).lower():
                    continue
                out.append(e)
                if len(out) >= limit:
                    return out
        return out


class AuditCoalescer:
    """Collapse a repeated, unchanged fact into a few audit entries (AUD-RET-1).

    A *group* names what repeats (e.g. ``(user, target)``); its *variant* is
    the value that must stay the same (e.g. the command's parameters). The
    first :meth:`repeat` of a streak is written as a normal entry with
    ``detail.coalesce = "start"``; later repeats are only counted. A summary
    entry (``detail.coalesce = "summary"``, ``count`` = repeats since the
    last written entry, ``first_ts``/``last_ts``, ``reason``) is written when
    the variant changes, when :meth:`end` closes the group, every ``every_s``
    while the streak lasts, and once the streak has been idle ``every_s``.

    Summaries are due-checked on every call (no thread of its own): a streak
    whose caller stops entirely is summarised by the next call of any group —
    the API's shutdown flushes every open streak; a crash loses at most
    ``every_s`` of a count, never a change (changes are never coalesced).
    Thread-safe, never raises.
    """

    def __init__(self, audit: "AuditLog", every_s: float = _REPEAT_SUMMARY_S,
                 clock=time.time, max_groups: int = 512, max_notes: int = 10):
        self._audit = audit
        self.every_s = float(every_s)
        self._clock = clock
        self._max = max_groups
        self._max_notes = max_notes
        self._lock = threading.Lock()
        self._streaks: Dict[Any, Dict[str, Any]] = {}

    def repeat(self, group: Any, variant: Any, *, user: str, ip: str,
               action: str, target: str = "", status: str = "ok",
               detail: Any = None, note: str = "") -> bool:
        """Count one occurrence; True when it was written (a streak start)."""
        try:
            now = self._clock()
            with self._lock:
                self._due_locked(now)
                st = self._streaks.get(group)
                if st is not None and st["variant"] != variant:
                    self._summary_locked(group, st, "changed")
                    st = None
                if st is not None:
                    st.update(count=st["count"] + 1, last_ts=now, user=user, ip=ip)
                    if note and note not in st["notes"] and len(st["notes"]) < self._max_notes:
                        st["notes"].append(note)
                    return False
                if len(self._streaks) >= self._max:
                    old = min(self._streaks, key=lambda g: self._streaks[g]["last_ts"])
                    self._summary_locked(old, self._streaks[old], "evicted")
                base = dict(detail) if isinstance(detail, dict) else (
                    {} if detail is None else {"detail": detail})
                self._streaks[group] = {
                    "variant": variant, "count": 0, "first_ts": now, "last_ts": now,
                    "written_ts": now, "user": user, "ip": ip, "action": action,
                    "target": target, "status": status, "base": base,
                    "notes": [note] if note else []}
                self._audit.append(user=user, ip=ip, action=action, target=target,
                                   status=status, detail=dict(base, coalesce="start"))
                return True
        except Exception:  # noqa: BLE001
            logger.exception("audit coalesce failed")
            return True

    def end(self, match) -> None:
        """Close every streak whose group ``match(group)`` accepts — the fact
        changed — writing its summary first. Never raises."""
        try:
            with self._lock:
                self._due_locked(self._clock())
                for g in [g for g in self._streaks if match(g)]:
                    self._summary_locked(g, self._streaks[g], "ended")
        except Exception:  # noqa: BLE001
            logger.exception("audit coalesce end failed")

    def flush(self) -> None:
        """Summarise and forget every open streak (shutdown, tests)."""
        self.end(lambda _g: True)

    def _due_locked(self, now: float) -> None:
        for g, st in list(self._streaks.items()):
            if now - st["last_ts"] >= self.every_s:
                self._summary_locked(g, st, "idle")
            elif st["count"] and now - st["written_ts"] >= self.every_s:
                self._write_summary_locked(st, "hourly")
                st.update(count=0, written_ts=now, first_ts=now, notes=[])

    def _summary_locked(self, group: Any, st: Dict[str, Any], reason: str) -> None:
        if st["count"]:
            self._write_summary_locked(st, reason)
        self._streaks.pop(group, None)

    def _write_summary_locked(self, st: Dict[str, Any], reason: str) -> None:
        extra = {"coalesce": "summary", "count": st["count"],
                 "first_ts": round(st["first_ts"], 3), "last_ts": round(st["last_ts"], 3),
                 "reason": reason}
        if st["notes"]:
            extra["notes"] = list(st["notes"])
        self._audit.append(user=st["user"], ip=st["ip"], action=st["action"],
                           target=st["target"], status=st["status"],
                           detail=dict(st["base"], **extra))
