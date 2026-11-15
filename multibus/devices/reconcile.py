# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Device-set reconciliation diff (ADR-0047 P2): classify an old→new device list into add / remove
/ rebuild / update / unchanged, so a config reload applies MEMBERSHIP changes — not just the
primary. Today `restart_devices` only rebuilds devices already in the registry, so a reload that
added a device never started it and one that removed a device never stopped it; `reload_from_disk`
leaves non-primary devices "to the next restart". This pure diff is the core the executor acts on.

Classification by id:
- **added**   — in new, not in old → start a client + register.
- **removed** — in old, not in new → stop the client + deregister + clean outputs.
- **rebuilt** — in both, but the WIRE identity changed (ADR-0042 retarget): the running client would
  still write the old wire, so rebuild it.
- **updated** — in both, same wire but some other field changed → swap the config, keep the client
  (output toggles etc.).
- **unchanged** — identical.

Components follow their parent: a component reads through the parent's connection, so when a parent
is rebuilt the component is forced into `rebuilt` too; a component of a removed parent is removed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, List, Tuple

from multibus.core.config.models import device_wire_identity

logger = logging.getLogger("multibus.devices.reconcile")


@dataclass
class DeviceDiff:
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    rebuilt: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        """True when nothing needs doing (only unchanged devices)."""
        return not (self.added or self.removed or self.rebuilt or self.updated)


def _parent(dev: Any) -> str:
    return str(getattr(dev, "component_of", "") or "")


def diff_devices(old: List[Any], new: List[Any]) -> DeviceDiff:
    """Classify the transition from ``old`` to ``new`` DeviceConfig lists (see module doc)."""
    old_by = {d.id: d for d in old}
    new_by = {d.id: d for d in new}
    d = DeviceDiff()

    for did, nd in new_by.items():
        od = old_by.get(did)
        if od is None:
            d.added.append(did)
        elif device_wire_identity(od) != device_wire_identity(nd):
            d.rebuilt.append(did)
        elif od != nd:                       # DeviceConfig is a dataclass (field-wise eq)
            d.updated.append(did)
        else:
            d.unchanged.append(did)
    d.removed = [did for did in old_by if did not in new_by]

    _close_over_components(d, new_by, old_by)
    return d


def _close_over_components(d: DeviceDiff, new_by: dict, old_by: dict) -> None:
    """A component reads through its parent's connection (ADR device-components): when the parent is
    rebuilt the component must rebuild too, even if its own config is unchanged; a component whose
    parent is removed is itself removed. Idempotent (buckets stay disjoint)."""
    rebuilt = set(d.rebuilt)
    removed = set(d.removed)

    # components of a rebuilt parent → rebuilt (pull out of updated/unchanged)
    for did, nd in new_by.items():
        p = _parent(nd)
        if p and p in rebuilt and did not in rebuilt:
            for bucket in (d.updated, d.unchanged, d.added):
                if did in bucket:
                    bucket.remove(did)
            d.rebuilt.append(did)
            rebuilt.add(did)

    # a component still present in new but whose parent vanished → remove it
    for did, nd in new_by.items():
        p = _parent(nd)
        if p and p in removed and did not in removed and p not in new_by:
            for bucket in (d.updated, d.unchanged, d.rebuilt, d.added):
                if did in bucket:
                    bucket.remove(did)
            d.removed.append(did)
            removed.add(did)


def apply_diff(diff: DeviceDiff, *, stop: Callable[[str], None], rebuild: Callable[[List[str]], Any],
               update: Callable[[str], None], start: Callable[[str], None]
               ) -> List[Tuple[str, str, str]]:
    """Apply a DeviceDiff via injected per-device operations and return the failures
    ``[(device_id, op, error)]`` — a reconcile that touches N devices must not abort the other N-1
    when one fails (ADR-0047 P2: partial-start handling). Order is safety-first: **stop** removed
    devices first (free the wire/bus), then **rebuild** retargeted ones, **update** config-only
    ones (keep the client), and **start** added ones last. ``rebuild`` takes the whole list
    (``restart_devices`` already guards per device and pulls in components)."""
    failed: List[Tuple[str, str, str]] = []

    def _guard(fn, did, op):
        try:
            fn(did)
        except Exception as e:  # noqa: BLE001 — one device must not stop the reconcile
            logger.error("reconcile %s of %s failed: %s", op, did, e)
            failed.append((did, op, str(e)))

    for did in diff.removed:
        _guard(stop, did, "stop")
    if diff.rebuilt:
        try:
            rebuild(diff.rebuilt)
        except Exception as e:  # noqa: BLE001
            logger.error("reconcile rebuild of %s failed: %s", diff.rebuilt, e)
            failed.append((",".join(diff.rebuilt), "rebuild", str(e)))
    for did in diff.updated:
        _guard(update, did, "update")
    for did in diff.added:
        _guard(start, did, "start")
    return failed
