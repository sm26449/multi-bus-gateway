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


# the classic-CAN rates a port may be set to (CANCTL-1: anything else is a 422, not an `ip` error)
CAN_BITRATES = (10000, 20000, 50000, 100000, 125000, 250000, 500000, 800000, 1000000)


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
        from ..devices import buses
        app = getattr(ctx, "app", None)
        diag = getattr(getattr(app, "state", None), "bridge_diag", None)
        return {"buses": buses.buses(config, getattr(ctx, "template_registry", None),
                                     share_of=_share, diag=diag)}

    @r.get("/api/can-interfaces")
    def list_can_interfaces():
        """The CAN interfaces of this host (``can_interfaces.interfaces``).
        Never raises: an unreadable sysfs is an empty list and a logged line."""
        from ..transports.can import can_interfaces
        try:
            return {"interfaces": can_interfaces.interfaces(
                devices=can_interfaces.devices_by_channel(config))}
        except Exception:  # noqa: BLE001 — an odd sysfs never breaks the form
            logger.warning("CAN interfaces could not be listed", exc_info=True)
            return {"interfaces": [], "error": "the CAN interfaces could not be listed (see the gateway log)"}

    @r.post("/api/can-interfaces/{name}")
    def set_can_interface(name: str, payload: Dict = Body(...)):
        """Switch a CAN interface's **mode** (``transmit``/``listen_only``), **bitrate** and
        **up** (ADR-0046 L2). Needs MBG to hold ``CAP_NET_ADMIN``; admin, audited. This is how a
        guided prerequisite takes a port out of listen-only so a Virtual BMS can transmit."""
        from ..transports.can import can_ctl, can_interfaces
        try:
            itfs = {i.get("name"): i for i in can_interfaces.interfaces(
                devices=can_interfaces.devices_by_channel(config))}
        except Exception:  # noqa: BLE001 — an odd sysfs: no interface is known, so none is changed
            itfs = {}
        if name not in itfs:
            raise HTTPException(status_code=404, detail=f"{name!r} is not a CAN interface on this host")
        mode, bitrate, up = payload.get("mode"), payload.get("bitrate"), payload.get("up")
        # every field is checked before the link is touched (CANCTL-1): a bad value used to
        # reach `ip` (or int()) and answer 500 with the link already down
        errors = []
        if mode is not None and mode not in ("transmit", "listen_only"):
            errors.append("mode must be 'transmit' or 'listen_only'")
        if bitrate is not None and (isinstance(bitrate, bool) or not isinstance(bitrate, int)
                                    or bitrate not in CAN_BITRATES):
            errors.append("bitrate must be one of " + ", ".join(str(b) for b in CAN_BITRATES))
        if up is not None and not isinstance(up, bool):
            errors.append("up must be true or false")
        if mode is None and bitrate is None and up is None:
            errors.append("nothing to change — give mode, bitrate and/or up")
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        cur = itfs[name]
        # taking a port OUT of listen-only is what lets the gateway transmit on a bus: it obeys
        # the master write switch like every other hardware write. Into listen-only, or down,
        # is the safe direction and is never refused for it.
        if mode == "transmit" and not config.security.allow_writes:
            raise HTTPException(status_code=409, detail={"errors": [
                "Writes are disabled — set security.allow_writes=true before a CAN port may transmit"]})
        # any change cycles the link. An ARMED Virtual BMS on this port would stop reaching its
        # inverter mid-flight: disarm it first — a deliberate act, not a side effect.
        _app = getattr(ctx, "app", None)
        vbm = getattr(getattr(_app, "state", None), "vbms_manager", None)
        armed = sorted(i.id for i in (vbm.instances.values() if vbm is not None else [])
                       if i.channel == name and (i.armed or i.running))
        if armed:
            raise HTTPException(status_code=409, detail={"errors": [
                f"{name} carries an armed Virtual BMS ({', '.join(armed)}) — disarm it first"]})
        # a different bitrate makes every device reading this bus deaf: only on purpose
        readers = list(cur.get("devices") or [])
        if (bitrate is not None and cur.get("bitrate") and bitrate != cur.get("bitrate")
                and readers and payload.get("force") is not True):
            raise HTTPException(status_code=409, detail={
                "code": "bus_has_readers", "readers": readers, "bitrate": cur.get("bitrate"),
                "errors": [
                    f"{name} is read by {', '.join(str(r) for r in readers)} at {cur.get('bitrate')} bit/s — "
                    f"changing the bitrate would stop them; send force: true to do it anyway"]})
        try:
            can_ctl.apply(name, mode=mode, bitrate=bitrate, up=up)
        except PermissionError:
            raise HTTPException(status_code=409, detail={"errors": [
                "MBG cannot change CAN interfaces — it needs CAP_NET_ADMIN (add --cap-add NET_ADMIN)"]})
        except OSError as e:
            raise HTTPException(status_code=409, detail={"errors": [f"could not set {name}: {e}"]})
        itf = next((i for i in can_interfaces.interfaces() if i.get("name") == name), {})
        return {"status": "applied", "interface": itf}

    @r.get("/api/buses/{bus:path}/line")
    def bus_line_now(bus: str):
        """The line a bus has now (the Edit bus line dialog starts from it):
        ``{bus, kind, line, source: devices|bridge|unknown}``."""
        from ..devices import bus_line
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
        from ..devices import bus_line
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
        from multibus.transports.serial import bridges as bridge_api
        from ..devices import bus_line
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
