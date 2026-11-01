# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The buses and the CAN interfaces (ADR-0031 §5), and a bus's line (§4).

- ``GET /api/buses``: every known bus with its pollers, listeners, units in
  use, line, measured load and, for a bridge, its framing and the client it
  reports (``buses.py``).
- ``GET /api/can-interfaces``: the CAN interfaces of this host with up/down,
  bitrate, listen-only and state, and the devices reading each
  (``can_interfaces.py``).

- ``PATCH /api/buses/{bus}/line``: Edit bus line… — one line for every
  device on a serial bus, dry run first (``bus_line.py``). Admin only.

The GETs are open to every role; nothing is opened on a bus.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException

logger = logging.getLogger(__name__)


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["buses"])
    config, registry = ctx.config, ctx.registry

    def _share(dev_id: str) -> Optional[float]:
        """A device's own wire share over the last minute, as measured."""
        for d, c in registry:
            if d.id != dev_id or c is None or not d.enabled:
                continue
            try:
                return (c.get_stats() or {}).get("bus_occupancy")
            except Exception:  # noqa: BLE001
                return None
        return None

    @r.get("/api/buses")
    def list_buses():
        """Every bus the gateway knows (``buses.buses``)."""
        from .. import buses
        app = getattr(ctx, "app", None)
        diag = getattr(getattr(app, "state", None), "bridge_diag", None)
        return {"buses": buses.buses(config, getattr(ctx, "template_registry", None),
                                     share_of=_share, diag=diag)}

    @r.get("/api/can-interfaces")
    def list_can_interfaces():
        """The CAN interfaces of this host (``can_interfaces.interfaces``).
        Never raises: an unreadable sysfs is an empty list and a logged line."""
        from .. import can_interfaces
        try:
            return {"interfaces": can_interfaces.interfaces(
                devices=can_interfaces.devices_by_channel(config))}
        except Exception:  # noqa: BLE001 — an odd sysfs never breaks the form
            logger.warning("CAN interfaces could not be listed", exc_info=True)
            return {"interfaces": [], "error": "the CAN interfaces could not be listed (see the gateway log)"}

    @r.get("/api/buses/{bus:path}/line")
    def bus_line_now(bus: str):
        """The line a bus has now (the Edit bus line dialog starts from it):
        ``{bus, kind, line, source: devices|bridge|unknown}``."""
        from .. import bus_line
        try:
            return bus_line.current(config, getattr(ctx, "template_registry", None), bus)
        except bus_line.Refused as e:
            raise HTTPException(status_code=e.status, detail=e.detail)

    @r.patch("/api/buses/{bus:path}/line")
    def edit_bus_line(bus: str, payload: Dict = Body(...), dry_run: bool = False):
        """Edit bus line… (ADR-0031 §4): set ``{baudrate, parity, bytesize,
        stopbits}`` for every device on a serial bus (its id as GET
        /api/buses gives it) and restart them. ``?dry_run=true`` lists the
        devices that change and restart, and stores nothing. On a port of our
        serial bridge the bridge port's line is set through the bridge's API
        in the same operation (the bridge is asked first, never under a
        lock; a port already at that line is unchanged); a converter or
        another bridge is refused (set on the box). Refused (409) while a
        write to one of its devices is in flight. One config write, one audit
        record. A restart that fails after the write is named in
        ``restart_failed`` (the line is stored). Admin only."""
        from .. import bus_line
        templates = getattr(ctx, "template_registry", None)
        try:
            line = bus_line.clean(payload)
            plan = bus_line.plan(config, templates, bus, line)
        except bus_line.Refused as e:
            raise HTTPException(status_code=e.status, detail=e.detail)
        bstate = None
        if plan['bridge'] is not None:
            bstate = bus_line.bridge_state(config, plan['bridge'])
            plan = bus_line.with_bridge_line(plan, bstate['line'], line)
        changes = bool(plan['raws']) or (plan['bridge'] is not None and not plan.get('unchanged'))
        if dry_run or not changes:
            res = bus_line.view(plan, ('valid' if changes else 'unchanged') if dry_run else 'unchanged')
            return res if dry_run else {**res, 'restarted': []}
        if plan['bridge'] is not None and not bstate['key']:
            raise HTTPException(status_code=502, detail={"errors": [
                f"the bridge '{plan['bridge']['id']}' did not say which adapter serves :{plan['bridge']['port']}"
                f"{' (' + bstate['error'] + ')' if bstate['error'] else ''} — nothing was changed"]})
        # every device's entry lock, never waited for: a write in flight to
        # one of them (it holds its lock) refuses the whole bus. Units of one
        # installation share one lock: each lock is taken once.
        held: List[Any] = []
        checked = set()
        try:
            for d in sorted({x['id'] for x in plan['devices']}):
                checked.add(d)
                lk = ctx.entry_lock(d)
                if lk is None or any(lk is h for h in held):
                    continue
                if not lk.acquire(blocking=False):
                    raise HTTPException(status_code=409, detail={"errors": [
                        f"{d}: a write to it is in flight — nothing was changed; try again in a moment"],
                        "busy": d})
                held.append(lk)
            with ctx.mutation_lock:
                return _apply_line(bus, line, templates, checked, bstate)
        finally:
            for lk in held:
                lk.release()

    def _apply_line(bus: str, line: Dict, templates, checked, bstate) -> Dict:
        from .. import bridges as bridge_api
        from .. import bus_line
        try:
            plan = bus_line.plan(config, templates, bus, line)        # again, under the locks
        except bus_line.Refused as e:
            raise HTTPException(status_code=e.status, detail=e.detail)
        if bstate is not None and plan['bridge'] is not None:
            plan = bus_line.with_bridge_line(plan, bstate['line'], line)
        # a device that joined the bus meanwhile: its lock is not held
        late = sorted({d['id'] for d in plan['devices']} - checked)
        if late:
            raise HTTPException(status_code=409, detail={"errors": [
                f"{', '.join(late)} joined this bus meanwhile — nothing was changed; check again"], "busy": late[0]})
        ids = [d['id'] for d in plan['devices'] if d['restart']]
        if plan['bridge'] is not None:
            b = config.get_raw_bridge(str(plan['bridge']['id'])) or {}
            try:
                bridge_api.api_post(b, bstate['key'], "serial", {"baud": line['baudrate'], "parity": line['parity'],
                                                                 "databits": line['bytesize'],
                                                                 "stopbits": line['stopbits']})
            except bridge_api.BridgeApiError as e:
                raise HTTPException(status_code=e.status, detail={"errors": [
                    f"the bridge '{b.get('id')}' did not take the line ({e.text}) — nothing was changed"]})
        elif plan['raws']:
            for raw in plan['raws']:
                if not config.preview_devices(device=raw):
                    raise HTTPException(status_code=422, detail={"errors": [
                        f"device {raw.get('id')!r} could not be built with this line — nothing was changed"]})
            try:
                config.upsert_raw_devices(plan['raws'])               # the one config write
            except ValueError as e:
                raise HTTPException(status_code=422, detail={"errors": [str(e)]})
            # what is stored is what the device list says, a restart or not
            for raw in plan['raws']:
                fresh = config.get_device(str(raw.get('id')))
                if fresh is not None:
                    try:
                        registry.replace(fresh.id, fresh)
                    except Exception:  # noqa: BLE001 — the restart below rebuilds it
                        logger.debug("registry entry %s not replaced", fresh.id, exc_info=True)
        if not ids:
            return {**bus_line.view(plan, 'unchanged'), 'restarted': []}
        # the line is stored now: a restart that fails is said, never a 500
        try:
            restarted = list(ctx.restart_devices(ids) or [])
        except Exception:  # noqa: BLE001
            logger.exception("bus %s: restart after the line change failed", plan['where'])
            restarted = []
        failed = [d for d in ids if d not in restarted]
        logger.info("bus %s: line set to %s — %s restarted%s", plan['where'], bus_line.text(line),
                    ', '.join(restarted) or 'none', f"; restart failed: {', '.join(failed)}" if failed else '')
        return {**bus_line.view(plan, 'updated'), 'restarted': restarted, 'restart_failed': failed}

    return r
