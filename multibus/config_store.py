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
"""A configuration document kept apart from config.yaml.

One YAML document per store, with a revision. The rest of the gateway reads
and changes it only through ``read`` and ``mutate``, so where it lives (a
file mirrored to a standby today, a replicated log later) is this module's
business alone.

- **Read:** the parsed document and its revision. The file is small and is
  read on every call, so a change made under the store (a restore, an
  import, the HA mirror) is never missed. Timestamps would miss a write of
  the same size within one clock step.
- **Change:** ``mutate(fn, expected_rev)`` runs ``fn`` on a copy under the
  file's lock and saves the result atomically (temp file, fsync, rename,
  0600). A stale ``expected_rev`` is a conflict, and nothing is written.
- **The file's lock** is shared by path (``lock_for``). A restore or an import
  that writes the file takes the same lock, so it never interleaves with a
  change.
- **A file that does not parse** (a bad hand edit) does not stop the
  gateway. Reads answer the last good document, or the default, with
  ``broken`` set. Writes are refused (``Broken``) instead of overwriting what
  someone may still want to fix.
- **The revision** is a short hash of the saved text.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

_LOCKS: Dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def lock_for(path) -> threading.RLock:
    """The one lock of a stored file, whoever writes it."""
    key = str(Path(path).resolve())
    with _LOCKS_GUARD:
        lk = _LOCKS.get(key)
        if lk is None:
            lk = _LOCKS[key] = threading.RLock()
        return lk


class Conflict(Exception):
    """The document changed since the caller read it (its revision moved)."""

    def __init__(self, current_rev: str):
        super().__init__(f"changed meanwhile (now {current_rev})")
        self.current_rev = current_rev


class Broken(Exception):
    """The file does not parse: writes are refused until it is fixed."""


class FileStore:
    """A YAML document in one file, read on demand and written atomically."""

    def __init__(self, path, default: Dict[str, Any],
                 normalize: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None):
        self.path = Path(path)
        self._default = copy.deepcopy(default)
        self._normalize = normalize or (lambda d: d)
        self._lock = lock_for(self.path)
        self._doc: Dict[str, Any] = self._normalize(copy.deepcopy(default))
        self._rev = ''
        self.broken: Optional[str] = None

    @staticmethod
    def rev_of(text: str) -> str:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]

    def _load(self) -> None:
        try:
            text = self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            self._doc, self._rev, self.broken = self._normalize(copy.deepcopy(self._default)), '', None
            return
        rev = self.rev_of(text)
        if rev == self._rev and self.broken is None:
            return
        try:
            data = yaml.safe_load(text)
            if data is None:
                data = copy.deepcopy(self._default)
            if not isinstance(data, dict):
                raise ValueError("not a mapping")
        except Exception as e:  # noqa: BLE001 — a bad file must not stop the gateway
            if self.broken is None:
                logger.error("%s does not parse (%s) — keeping the last good document; "
                             "changes are refused until the file is fixed", self.path.name, e)
            self.broken = f"{self.path.name}: {e}"
            return
        self._doc, self._rev, self.broken = self._normalize(data), rev, None

    def read(self) -> Tuple[Dict[str, Any], str]:
        """(a copy of the document, its revision; '' while never saved)."""
        with self._lock:
            self._load()
            return copy.deepcopy(self._doc), self._rev

    def mutate(self, fn: Callable[[Dict[str, Any]], Any],
               expected_rev: Optional[str] = None) -> Tuple[Dict[str, Any], str, Any]:
        """Apply ``fn`` to a copy of the document and save it. ``fn`` may
        raise to abort; its return value is handed back. Returns (the new
        document, its revision, fn's result)."""
        with self._lock:
            self._load()
            if self.broken:
                raise Broken(self.broken)
            if expected_rev is not None and expected_rev != self._rev:
                raise Conflict(self._rev)
            doc = copy.deepcopy(self._doc)
            result = fn(doc)
            self._save(doc)
            return copy.deepcopy(self._doc), self._rev, result

    def ensure(self) -> None:
        """Write the default document when there is no file yet: a bundle
        always carries one, so a restore or a mirror never keeps a stale file
        just because the source had none."""
        with self._lock:
            if not self.path.exists():
                self._save(self._normalize(copy.deepcopy(self._default)))

    def _save(self, doc: Dict[str, Any]) -> None:
        text = yaml.safe_dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + '.tmp')
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._doc, self._rev, self.broken = doc, self.rev_of(text), None
