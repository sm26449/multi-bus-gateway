"""Write-lease manager — a dead-man switch for Modbus writes.

A *leased* write carries a TTL. If it is not renewed before the TTL expires, the
register is automatically reverted to a declared safe value. This protects the
plant from a controller that crashes or hangs while holding a device at a
dangerous setpoint (e.g. an export limit stuck high while the battery fills).

The manager is transport-agnostic: it stores a zero-arg ``revert`` callable per
lease and invokes it once on expiry. The API layer builds that callable so it
resolves the *current* device client at revert time (surviving device restarts).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_Key = Tuple[str, str, int]   # (device_id, register_type, address)


class WriteLeaseManager:
    def __init__(self, tick_s: float = 1.0, persist_path=None):
        self._leases: Dict[_Key, dict] = {}
        self._lock = threading.Lock()
        self._tick = tick_s
        self._gen = 0                 # monotonic generation; bumps on every arm()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Declarative leases are mirrored here so a process crash doesn't strand a
        # dangerous setpoint with no revert. Only the *set* of active leases is
        # persisted (device/addr/safe-value), never the callable — on boot the API
        # rebuilds the revert and fires the dead-man immediately.
        self._persist_path: Optional[Path] = Path(persist_path) if persist_path else None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="write-lease", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def arm(self, device_id: str, register_type: str, address: int,
            lease_ms: int, revert: Callable[[Callable[[], bool]], None],
            meta: Optional[dict] = None, fire_now: bool = False) -> None:
        """Set or renew a lease.

        ``revert`` is invoked once if the lease expires. It receives an
        ``is_current()`` predicate and MUST call it immediately before issuing
        its (blocking) safe-write; if it returns False the lease was renewed or
        cleared and the write must be skipped. ``revert`` MUST raise on a failed
        safe-write so the dead-man can retry.

        ``meta`` is the declarative record (device/type/addr/data_type/scale/
        safe_value/lease_ms) mirrored to disk so the lease survives a crash.
        ``fire_now`` (boot recovery) marks the lease already-expired so the next
        sweep reverts to safe immediately — a crash means the renewer is gone.
        """
        key: _Key = (device_id, register_type, int(address))
        with self._lock:
            self._gen += 1
            existed = key in self._leases
            self._leases[key] = {
                'expiry': time.monotonic() + (0.0 if fire_now else lease_ms / 1000.0),
                'revert': revert,
                'gen': self._gen,
                'lease_ms': int(lease_ms),
                'armed_ts': time.time(),
                'meta': meta,
            }
            if not existed:                        # a renewal doesn't change the set
                self._persist_locked()

    def clear(self, device_id: str, register_type: str, address: int) -> None:
        with self._lock:
            if self._leases.pop((device_id, register_type, int(address)), None) is not None:
                self._persist_locked()

    def clear_device(self, device_id: str) -> None:
        """Drop all leases for a device (e.g. on delete) without reverting."""
        with self._lock:
            removed = [k for k in self._leases if k[0] == device_id]
            for k in removed:
                del self._leases[k]
            if removed:
                self._persist_locked()

    def snapshot(self) -> List[dict]:
        now = time.monotonic()
        with self._lock:
            return [{'device': k[0], 'register_type': k[1], 'address': k[2],
                     'remaining_s': round(max(0.0, v['expiry'] - now), 1),
                     'lease_ms': v['lease_ms'], 'armed_ts': round(v['armed_ts'], 3)}
                    for k, v in self._leases.items()]

    def load_persisted(self) -> List[dict]:
        """Declarative leases left on disk by a previous (possibly crashed) run.
        The API rebuilds a revert for each and re-arms with ``fire_now=True``."""
        if not self._persist_path or not self._persist_path.exists():
            return []
        try:
            data = json.loads(self._persist_path.read_text())
            return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []
        except Exception as e:  # noqa: BLE001
            logger.error("write-lease: could not read persisted leases %s: %s",
                         self._persist_path, e)
            return []

    # ── internals ─────────────────────────────────────────────────────────
    def _persist_locked(self) -> None:
        """Atomically mirror the current lease set to disk. Caller holds _lock."""
        if not self._persist_path:
            return
        records = [v['meta'] for v in self._leases.values() if v.get('meta')]
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_name(self._persist_path.name + ".tmp")
            with open(tmp, 'w') as f:
                json.dump(records, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._persist_path)
        except Exception as e:  # noqa: BLE001
            logger.error("write-lease: could not persist leases to %s: %s",
                         self._persist_path, e)

    def _run(self) -> None:
        while not self._stop.wait(self._tick):
            self._sweep()

    def _is_current(self, key: _Key, gen: int) -> bool:
        """True iff ``key`` still holds the same expired lease we picked up.

        A renewal bumps ``gen`` and pushes ``expiry`` into the future, so a
        stale revert that checks this right before writing will abort instead of
        clobbering a freshly-renewed setpoint.
        """
        with self._lock:
            cur = self._leases.get(key)
            return cur is not None and cur['gen'] == gen and cur['expiry'] <= time.monotonic()

    def _sweep(self) -> None:
        now = time.monotonic()
        with self._lock:
            due = [(k, v['gen'], v['revert']) for k, v in self._leases.items()
                   if v['expiry'] <= now]
        if not due:
            return
        # Fire each due revert in its OWN thread and join. A revert does blocking
        # Modbus I/O (a connect() to an offline device can burn the full timeout),
        # so running them concurrently stops one stuck device from delaying another
        # device's safety revert (head-of-line blocking). Joining keeps _sweep
        # serial with itself, so the next tick can't double-fire the same lease.
        threads = [threading.Thread(target=self._do_revert, args=(k, gen, revert),
                                    name=f"lease-revert-{k[0]}", daemon=True)
                   for k, gen, revert in due]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _do_revert(self, key: _Key, gen: int, revert: Callable) -> None:
        if not self._is_current(key, gen):
            return                              # renewed/cleared before we got to it
        ok = False
        try:
            # revert re-checks is_current() right before its safe-write and raises
            # on a failed write; a no-op (renewed) return also lands here as ok=True
            revert(lambda _k=key, _g=gen: self._is_current(_k, _g))
            ok = True
            logger.warning("WRITE-LEASE expired → reverted device=%s %s addr=%s",
                           key[0], key[1], key[2])
        except Exception as e:  # noqa: BLE001
            logger.error("WRITE-LEASE revert FAILED (will retry) device=%s addr=%s: %s",
                         key[0], key[2], e)
        # Recompute the clock AFTER the (possibly multi-second, blocking) revert so
        # the retry is scheduled relative to now, not to sweep-start — otherwise the
        # backoff lands in the past and a permanently-offline device is retried every
        # tick and log-spams instead of backing off.
        now = time.monotonic()
        with self._lock:
            cur = self._leases.get(key)
            if cur is None or cur['gen'] != gen or cur['expiry'] > now:
                return                          # renewed or cleared during the revert — leave it
            if ok:
                del self._leases[key]           # safe value written (or renewal took over) — done
                self._persist_locked()
            else:
                # keep the dead-man alive: retry until the register reaches its safe
                # value (a stuck setpoint is exactly what we guard against)
                cur['expiry'] = now + max(1.0, self._tick * 3)
