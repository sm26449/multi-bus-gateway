"""Device registry — owns the (DeviceConfig, client) pairs + per-device stores.

Extracted from ``create_api()`` where these lived as a bare ``device_list``
list, a ``device_values`` dict and a ``_devices_lock``, mutated inline by the
device CRUD routes. The registry gives that state a name and atomic methods;
behavior is byte-identical (golden tests pin the store aliasing and routing).

Concurrency model (same as before the extraction): mutating methods take the
internal lock — device CRUD runs in FastAPI's threadpool, so two concurrent
admin calls must not interleave a find-index with a pop/replace. Read paths
are lock-free snapshots (list/dict iteration is safe under the GIL; worst case
a snapshot misses an in-flight change, exactly like the old code).

Store semantics (the invisible-migration contract):
* the PRIMARY device's store IS the legacy ``current_values`` dict — the same
  object, registered as an alias, so the UI/vmeters/api keep reading it;
* ``store_for(primary_id)`` answers even when the primary was never registered
  (legacy single-device mode constructs no device pairs).
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Iterator, List, Optional, Tuple

_KEEP = object()          # sentinel: replace() keeps the running client


class DeviceRegistry:
    def __init__(self, primary_id: str, primary_store: Dict[int, Dict]):
        self._primary_id = primary_id
        self._primary_store = primary_store
        self._pairs: List[Tuple[Any, Any]] = []      # (DeviceConfig, client|None)
        self.values: Dict[str, Dict[int, Dict]] = {} # device_id -> live store
        self.lock = threading.Lock()

    # ── construction / seeding ─────────────────────────────────────────────
    def register(self, dev_cfg, client) -> None:
        """Boot-time add (no lock: create_api runs single-threaded)."""
        self._pairs.append((dev_cfg, client))
        self.values[dev_cfg.id] = (self._primary_store if dev_cfg.primary else {})

    # ── reads (lock-free snapshots) ────────────────────────────────────────
    def __iter__(self) -> Iterator[Tuple[Any, Any]]:
        return iter(list(self._pairs))

    def __len__(self) -> int:
        return len(self._pairs)

    def __bool__(self) -> bool:
        return bool(self._pairs)

    def pairs(self) -> List[Tuple[Any, Any]]:
        return list(self._pairs)

    def find(self, device_id: str):
        """(index, cfg, client) for a device id, or (None, None, None)."""
        for i, (cfg_d, client) in enumerate(list(self._pairs)):
            if cfg_d.id == device_id:
                return i, cfg_d, client
        return None, None, None

    def has(self, device_id: str) -> bool:
        return any(cfg_d.id == device_id for cfg_d, _ in list(self._pairs))

    def store_for(self, device_id: str) -> Optional[Dict[int, Dict]]:
        """A device's live value store, or None if unknown. The primary answers
        even in legacy mode (no registered pairs) — its store is the alias."""
        if device_id == self._primary_id:
            return self._primary_store
        return self.values.get(device_id)

    def ensure_store(self, device_id: str) -> Dict[int, Dict]:
        """setdefault semantics — used by the poller-callback factory so a
        device created at runtime gets a store even before/without register()."""
        if device_id == self._primary_id:
            return self._primary_store
        return self.values.setdefault(device_id, {})

    # ── atomic mutations ───────────────────────────────────────────────────
    def add(self, dev_cfg, client) -> None:
        with self.lock:
            self._pairs.append((dev_cfg, client))
            self.values.setdefault(dev_cfg.id,
                                   self._primary_store if dev_cfg.primary else {})

    def replace(self, device_id: str, new_cfg, client=_KEEP,
                add_if_missing: bool = False) -> None:
        """Swap a device's config (and optionally its client) by id.

        ``client`` omitted → the running client is kept (config-only edits:
        http-output / rest-push toggles). Missing id: appended when
        ``add_if_missing`` (device-update race with a concurrent delete),
        silently ignored otherwise — both exactly the pre-registry behavior.
        """
        with self.lock:
            for i, (cfg_d, c) in enumerate(self._pairs):
                if cfg_d.id == device_id:
                    self._pairs[i] = (new_cfg, c if client is _KEEP else client)
                    return
            if add_if_missing:
                self._pairs.append((new_cfg, None if client is _KEEP else client))
                self.values.setdefault(new_cfg.id,
                                       self._primary_store if new_cfg.primary else {})

    def remove(self, device_id: str) -> None:
        """Drop a device's pair AND its value store (one atomic step)."""
        with self.lock:
            for i, (cfg_d, _c) in enumerate(self._pairs):
                if cfg_d.id == device_id:
                    self._pairs.pop(i)
                    break
            self.values.pop(device_id, None)

    def resync(self, device_cfgs, primary_client) -> None:
        """Re-sync every pair against freshly rebuilt DeviceConfigs (a primary
        edit reruns config._build_devices(), which rebuilds EVERY DeviceConfig):
        clients are matched by id so pollers and config.get_device() never
        diverge on derived fields. One atomic step, like the old
        ``device_list[:] = ...`` under the lock. Value stores are untouched —
        ids don't change on a resync."""
        with self.lock:
            clients = {cfg_d.id: c for cfg_d, c in self._pairs}
            self._pairs[:] = [
                (dev, primary_client if dev.primary else clients.get(dev.id))
                for dev in device_cfgs]
