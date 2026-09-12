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


def client_is_live(client) -> bool:
    """THE liveness verdict for a southbound client — one definition, used by
    the alert harvester, the MQTT availability/runtime leaves and the plant
    unit census, so the product never contradicts itself.

    A device is alive when its acquisition pipeline is PRODUCING, not when a
    socket happens to be open. ``client.connected`` is a transport flag that
    survives a vanished endpoint (it clears only on an explicit disconnect or
    the wedge backstop's forced reopen), so a datalogger that went dark
    overnight kept reporting "online" to Home Assistant, to MQTT and to the
    alert log while its data froze. ``data_health()`` already owns the
    freshness verdict (``ok`` / ``degraded`` / ``down``) and every client type
    implements it; ``degraded`` still counts as alive — it means slow or
    partially stale, not gone.

    One exception runs the other way: ``data_health()`` also answers ``ok``
    when NOTHING has been polled yet (a cold start, or a client whose pollers
    never started because the endpoint refused the first connection). It cannot
    tell that apart from healthy, so a client that has never produced a reading
    defers to the transport flag — otherwise a plant of unreachable units
    reports three green units underneath an ``offline`` plant.

    Falls back to the transport flag if a client cannot answer, and never
    raises: a health probe must not be able to kill its caller.
    """
    if client is None:
        return False
    try:
        health = client.data_health()
    except Exception:  # noqa: BLE001 — liveness must never break the caller
        return bool(getattr(client, 'connected', False))
    if health.get('status') == 'down':
        return False
    if health.get('last_success_ts') is None:
        return bool(getattr(client, 'connected', False))
    return True


def client_health(client) -> str:
    """``client``'s acquisition-health word (``ok`` / ``degraded`` / ``down``),
    or ``idle`` when there is no client to ask.

    A device that is not connected can never read ``ok``: the status dot must
    not contradict the connection text, and ``data_health()`` answers ``ok`` on
    a cold start. The device list has always applied this rule; sharing it here
    keeps the plant page from disagreeing with it."""
    if client is None:
        return 'idle'
    try:
        status = str(client.data_health().get('status') or 'idle')
    except Exception:  # noqa: BLE001
        return 'ok' if getattr(client, 'connected', False) else 'idle'
    if status == 'ok' and not getattr(client, 'connected', False):
        return 'degraded'
    return status


def purge_deselected(store: dict, registers) -> int:
    """Drop live-store entries whose ADDRESS is no longer in the selected set
    (audit 2026-08-14 M2). A template re-select that moves a register to a new
    address while keeping its canonical name left the OLD entry frozen in the
    store; name-based lookup (vmeter sources) binds the first match in
    insertion order, so rows read the ghost's frozen timestamp and fail-stop
    the whole meter until a process restart. Synthetic calc addresses have
    their own purge in CalcEngine.load() and are left alone here. Returns the
    number of entries dropped."""
    from .calc_engine import CALC_ADDR_BASE
    keep = {r.address for r in (registers or [])}
    stale = [a for a in list(store)
             if isinstance(a, int) and a < CALC_ADDR_BASE and a not in keep]
    for a in stale:
        store.pop(a, None)
    return len(stale)
