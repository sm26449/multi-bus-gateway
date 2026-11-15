# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
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

    def write(self, device_id: str, raw: Dict) -> Optional[float]:
        """Persist a deleted device's full definition (secrets included, like
        config.yaml itself — the file is created 0600). Non-fatal on error: a
        failed tombstone must not block the delete. Returns its ``deleted_ts``
        (what an Undo names, L3.11c m1), or None when it was not written."""
        try:
            path = self.path(device_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            ts = round(time.time(), 3)
            payload = {"device": raw, "deleted_ts": ts}
            tmp = path.with_suffix('.json.tmp')
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return ts
        except Exception as e:  # noqa: BLE001
            logger.warning(f"device {device_id}: could not write restore tombstone: {e}")
            return None

    def deleted_ts(self, device_id: str) -> Optional[float]:
        """When the kept device was deleted (its tombstone's ``deleted_ts``)."""
        tomb = self.path(device_id)
        try:
            data = json.loads(tomb.read_text(encoding='utf-8'))
            v = data.get('deleted_ts') if isinstance(data, dict) else None
            return float(v) if isinstance(v, (int, float)) else None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def load(self, device_id: str) -> Optional[Dict]:
        """The raw devices[] entry from a tombstone (None if absent or not
        JSON). A file that is not shaped as a kept device (a list, a device
        that is not an object) raises ValueError in plain words: the caller
        says it, and the row stays listed so it can be forgotten (L3.11c R1)."""
        tomb = self.path(device_id)
        if not tomb.is_file():
            return None
        try:
            data = json.loads(tomb.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            raise ValueError("its kept settings are damaged: the file is not a kept device")
        dev = data.get('device')
        if dev is None:
            return None
        if not isinstance(dev, dict):
            raise ValueError("its kept settings are damaged: the device in it is not a device entry")
        return dev or None

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
            except (OSError, ValueError):
                continue
            # a damaged file is still listed (its restore says why; it can be
            # forgotten), never a failure of the whole list (L3.11c R1)
            data = data if isinstance(data, dict) else {}
            dev = data.get('device') if isinstance(data.get('device'), dict) else {}
            conn = dev.get('connection') if isinstance(dev.get('connection'), dict) else {}
            regs_p = child / 'selected_registers.json'
            n_regs = 0
            if regs_p.is_file():
                try:
                    regs = json.loads(regs_p.read_text())
                    n_regs = len(regs.get('registers', []) or []) if isinstance(regs, dict) else 0
                except (OSError, ValueError, TypeError):
                    pass
            out.append({
                "id": child.name,
                "name": str(dev.get('name') or child.name),
                "template": str(dev.get('template') or ''),
                "protocol": str(conn.get('protocol') or ''),
                "registers": n_regs,
                "deleted_ts": data.get('deleted_ts') if isinstance(data.get('deleted_ts'), (int, float)) else None,
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
