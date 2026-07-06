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
from typing import Any, Dict, List, Optional

from .redact import _is_secret_key

logger = logging.getLogger(__name__)

_MAX_DETAIL = 2000          # chars of redacted payload per entry
_ROTATE_BYTES = 1 << 20     # 1 MB per file
_KEEP_FILES = 4             # audit.jsonl + audit.jsonl.1..4


def redact_obj(obj: Any, depth: int = 0) -> Any:
    """Deep-copy ``obj`` with secret-keyed values masked. Never raises."""
    try:
        if depth > 8:
            return "…"
        if isinstance(obj, dict):
            return {k: ("***" if _is_secret_key(str(k)) else redact_obj(v, depth + 1))
                    for k, v in obj.items()}
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

    def append(self, *, user: str, ip: str, action: str, target: str = "",
               status: str = "ok", detail: Any = None) -> None:
        """Record one action. Never raises — an audit failure must not break
        the request it describes (it is logged loudly instead)."""
        try:
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
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
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
