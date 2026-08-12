# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Device soft-delete lifecycle (tombstones).

When a non-primary device is removed, its full definition is snapshotted to
``devices/<id>/device.json`` so the UI can restore the exact device later; the
selected-registers file is kept beside it. This collaborator owns that on-disk
lifecycle — write / path / load / list / forget — so ``Config`` doesn't. It is
pure file logic: "which ids are active" is policy the caller supplies (Config
knows the live device set), so ``list`` takes the active-id set and the caller
guards ``forget`` against active/primary ids before calling.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class TombstoneStore:
    """Read/write restorable-device tombstones under ``base_dir`` (``…/devices``)."""

    def __init__(self, base_dir: Path, safe_id: Callable[[str], str]):
        self._base = base_dir
        self._safe_id = safe_id

    def path(self, device_id: str) -> Path:
        return self._base / self._safe_id(device_id) / 'device.json'

    def write(self, device_id: str, raw: Dict) -> None:
        """Persist a deleted device's full definition (secrets included, like
        config.yaml itself — the file is created 0600). Non-fatal on error: a
        failed tombstone must not block the delete."""
        try:
            path = self.path(device_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"device": raw, "deleted_ts": round(time.time(), 3)}
            tmp = path.with_suffix('.json.tmp')
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"device {device_id}: could not write restore tombstone: {e}")

    def load(self, device_id: str) -> Optional[Dict]:
        """The raw devices[] dict from a tombstone (None if absent/unreadable)."""
        tomb = self.path(device_id)
        if not tomb.is_file():
            return None
        try:
            return (json.loads(tomb.read_text(encoding='utf-8')).get('device')
                    or None)
        except (OSError, ValueError):
            return None

    def list(self, active_ids: Set[str]) -> List[Dict]:
        """Restorable devices: a devices/<id>/device.json tombstone whose id is
        NOT currently active. Returns compact summaries for the UI."""
        out: List[Dict] = []
        if not self._base.is_dir():
            return out
        for child in sorted(self._base.iterdir()):
            if not child.is_dir() or child.name in active_ids:
                continue
            tomb = child / 'device.json'
            if not tomb.is_file():
                continue
            try:
                data = json.loads(tomb.read_text(encoding='utf-8'))
                dev = data.get('device', {}) or {}
            except (OSError, ValueError):
                continue
            regs_p = child / 'selected_registers.json'
            n_regs = 0
            if regs_p.is_file():
                try:
                    n_regs = len(json.loads(regs_p.read_text()).get('registers', []))
                except (OSError, ValueError):
                    pass
            out.append({
                "id": child.name,
                "name": dev.get('name') or child.name,
                "template": dev.get('template', ''),
                "protocol": (dev.get('connection') or {}).get('protocol', ''),
                "registers": n_regs,
                "deleted_ts": data.get('deleted_ts'),
            })
        return out

    def forget(self, device_id: str) -> bool:
        """Permanently remove a device's kept dir (tombstone + registers). The
        caller must have already refused active/primary ids."""
        d = self._base / self._safe_id(device_id)
        if not d.is_dir():
            return False
        shutil.rmtree(d, ignore_errors=True)
        return True
