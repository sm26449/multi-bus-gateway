# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The config tree on disk — ``base.yaml`` + ``config.d/`` — written as ONE transaction
(ADR-0047 P3c, design §8). ``assemble.py`` is the pure map between the logical document and
its files; this module puts those files on disk so that a crash at any point leaves either
the whole old tree or the whole new one, never a mix of an old base and new drop-ins.

The transaction is a **generation swap**:

1. the new generation is staged in ``config.d.new/`` — a file whose content did not change is
   hard-linked from ``config.d/`` (no write, no fsync: a save that touches one device costs one
   file, which matters on an SD card), a changed or new one is written and fsynced; an entity
   that is gone is simply not staged, so its file disappears with the swap;
2. ``base.yaml.new`` appears last, **by a rename** of a fully written and fsynced
   ``base.yaml.new.tmp``. **It is the commit record**: it either does not exist or is whole,
   and once it exists the new generation will be reached, by this call or by the next one;
3. ``config.d`` → ``config.d.old``, ``config.d.new`` → ``config.d``, ``base.yaml.new`` →
   ``base.yaml``, then ``config.d.old`` is removed.

``recover()`` runs before every write: a ``base.yaml.new`` rolls the swap FORWARD from wherever
it stopped (each step is idempotent); staging left without one is thrown away. A live file is
never written in place. Anything of the wrong kind in the way (a file where a directory
belongs, a symlink) is removed rather than left to fail every later write.

Writers are serialized in the process (the per-path lock) and across processes (an ``flock``
on ``.config-tree.lock``): a second writer must never clear the first one's staging.

``base.yaml`` carries ``_layout_source`` — the revision of the ``config.yaml`` text the tree
was written from — so a tree that fell behind is told apart from a current one. In P3 the tree
is NOT authoritative: the gateway reads ``config.yaml`` only, and a tree that is stale, torn or
absent is healed by the next save.

From ``config_store`` (ADR-0001) this reuses the per-path lock and the revision; its
``FileStore.mutate`` writes one file in place and cannot make a change that spans files
all-or-nothing, so entity files do not go through it (recorded in ADR-0047).
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import yaml

from multibus.core.config import assemble as _asm
from multibus.core.config_store import FileStore, lock_for

logger = logging.getLogger("multibus.core.config")

SOURCE_KEY = _asm.SOURCE_KEY
_D = _asm.DROPIN_DIR.rstrip("/")


def _dump(doc: Any) -> str:
    return yaml.dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _write_durable(path: Path, text: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _remove(path: Path) -> None:
    """Remove whatever is at ``path`` — a directory tree, a file, a symlink — or nothing."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


_READ_ERRORS = (OSError, yaml.YAMLError, UnicodeDecodeError, AttributeError, TypeError,
                _asm.AssembleError)


class TreeStore:
    """The config tree under ``root`` (the directory that holds ``config.yaml``)."""

    def __init__(self, root, fault: Optional[Callable[[str], None]] = None):
        self.root = Path(root)
        self.base = self.root / _asm.BASE
        self.base_new = self.root / (_asm.BASE + ".new")
        self.base_tmp = self.root / (_asm.BASE + ".new.tmp")
        self.lock_file = self.root / ".config-tree.lock"
        self.d = self.root / _D
        self.d_new = self.root / (_D + ".new")
        self.d_old = self.root / (_D + ".old")
        # crash tests name the point they stop at; in production this is a no-op
        self._fault = fault or (lambda point: None)
        self._lock = lock_for(self.base)
        self._held = False

    @contextlib.contextmanager
    def _exclusive(self):
        """One writer at a time: in this process (re-entrant) and across processes."""
        with self._lock:
            if self._held:                             # write() → recover(): already ours
                yield
                return
            self.root.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.lock_file), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._held = True
                yield
            finally:
                self._held = False
                os.close(fd)                           # closing releases the flock

    # ── write ────────────────────────────────────────────────────────────────
    def write(self, data: Dict[str, Any], source_rev: str = "") -> Dict[str, int]:
        """Put ``data`` (the logical config document) on disk as a tree, all or nothing.
        Returns ``{'files', 'written', 'linked'}``. Raises ``AssembleError`` before touching
        the disk when the document cannot be mapped to files."""
        files = _asm.split(data)                       # refuses before any write
        base = dict(files.pop(_asm.BASE))
        base[SOURCE_KEY] = source_rev
        with self._exclusive():
            self.recover()
            stats = self._stage(files)
            self._fault("staged")
            # the commit record must be whole or absent: written under another name, made
            # durable, and only then given the name recover() looks for
            _write_durable(self.base_tmp, _dump(base))
            self._fault("commit_written")
            os.replace(self.base_tmp, self.base_new)     # ── the commit ──
            _fsync_dir(self.root)
            self._fault("committed")
            self._finish()
        return stats

    def _stage(self, files: Dict[str, Any]) -> Dict[str, int]:
        _remove(self.d_new)
        os.mkdir(self.d_new, 0o700)
        written = linked = 0
        for path, doc in files.items():
            name = path[len(_asm.DROPIN_DIR):]
            text = _dump(doc)
            live, staged = self.d / name, self.d_new / name
            if self._same(live, text):
                try:
                    os.chmod(live, 0o600)              # a hand-loosened mode is not carried on
                    os.link(live, staged)
                    linked += 1
                    continue
                except OSError:                        # no hard links here: write it
                    pass
            _write_durable(staged, text)
            written += 1
            self._fault("staged_file")
        _fsync_dir(self.d_new)
        return {"files": len(files), "written": written, "linked": linked}

    @staticmethod
    def _same(path: Path, text: str) -> bool:
        try:
            return path.read_text(encoding="utf-8") == text
        except (OSError, UnicodeDecodeError):
            return False

    def _finish(self) -> None:
        """The swap, from wherever it stands. Every step checks what is on disk, so it can be
        re-run after a crash at any point (``recover`` does)."""
        if self.d_new.is_dir() and not self.d_new.is_symlink():
            if self.d.exists() or self.d.is_symlink():
                _remove(self.d_old)
                os.rename(self.d, self.d_old)
                self._fault("old_moved")
            os.rename(self.d_new, self.d)
            self._fault("new_moved")
        if self.base.is_dir() and not self.base.is_symlink():
            _remove(self.base)                         # os.replace cannot put a file over it
        os.replace(self.base_new, self.base)
        _fsync_dir(self.root)
        self._fault("base_replaced")
        _remove(self.d_old)

    def recover(self) -> str:
        """Bring the tree back to one whole generation after an interrupted write:
        ``rolled_forward`` (a commit record was found), ``discarded`` (staging without one),
        or ``clean``."""
        with self._exclusive():
            if self.base_new.is_file() and not self.base_new.is_symlink():
                self._finish()
                _remove(self.base_tmp)
                logger.warning("config tree: an interrupted write was completed")
                return "rolled_forward"
            out = "clean"
            # a commit record is a regular file made by a rename; anything else under that
            # name, a record still being written, and staging without a record all go
            for leftover in (self.base_new, self.base_tmp, self.d_new, self.d_old):
                if leftover.exists() or leftover.is_symlink():
                    _remove(leftover)
                    out = "discarded"
            if out == "discarded":
                logger.warning("config tree: staging left by an interrupted write was removed")
            return out

    # ── read (verification and tests; the gateway does not load from the tree in P3) ─────
    def read_files(self) -> Dict[str, Any]:
        """``{path: parsed}`` of the live generation, in the shape ``assemble`` takes."""
        files: Dict[str, Any] = {}
        if self.base.is_file():
            files[_asm.BASE] = yaml.safe_load(self.base.read_text(encoding="utf-8")) or {}
        if self.d.is_dir():
            for f in sorted(self.d.iterdir()):
                # a drop-in is a .yaml file; an editor's swap file beside one is not
                if f.is_file() and f.name.endswith(".yaml"):
                    files[_asm.DROPIN_DIR + f.name] = yaml.safe_load(f.read_text(encoding="utf-8"))
        return files

    def document(self) -> Dict[str, Any]:
        """The logical document the live generation holds."""
        return _asm.assemble(self.read_files())

    def source_rev(self) -> str:
        try:
            base = yaml.safe_load(self.base.read_text(encoding="utf-8")) or {}
        except _READ_ERRORS:
            return ""
        return str(base.get(SOURCE_KEY) or "") if isinstance(base, dict) else ""

    def state(self, source_rev: str) -> str:
        """``absent`` (never written), ``torn`` (an interrupted write, or the index and the
        files disagree), ``stale`` (whole, but written from another config.yaml), ``current``."""
        if any(p.exists() or p.is_symlink()
               for p in (self.base_new, self.base_tmp, self.d_new, self.d_old)):
            return "torn"
        if not self.base.exists():
            return "absent"
        try:
            problems = _asm.check(self.read_files())
        except _READ_ERRORS:                           # unreadable is torn — never an exception
            return "torn"
        if problems["missing"] or problems["unlisted"]:
            return "torn"
        return "current" if self.source_rev() == source_rev else "stale"


def rev_of(text: str) -> str:
    """The revision of a config.yaml text — the same hash every stored document uses."""
    return FileStore.rev_of(text)
