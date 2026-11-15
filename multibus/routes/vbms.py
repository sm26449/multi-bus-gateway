# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Virtual BMS endpoints — status + arm/disarm (ADR-0037) and user-facing CRUD (ADR-0045).

Arming lets an instance TRANSMIT to its inverter — a hardware write — so it is gated on the
``security.allow_writes`` master switch on top of the write-auth credential. Disarming is the SAFE
direction and is never gated. CRUD persists to ``config.virtual_bms`` and reconfigures the live
manager; a port is available only if no **other** Virtual BMS publishes on it (readers share the
bus); edit/delete are refused while an instance is armed. Every change is audited by the
middleware.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request

# the fields a Virtual BMS instance persists (ADR-0037 + ADR-0045); `armed` is runtime, never config
_FIELDS = ("id", "name", "profile", "channel", "packs", "bounds", "floor",
           "manufacturer", "period_s", "field_map", "fields", "inverter",
           "min_packs", "fallback", "command_max_age_s", "recovery", "pack_limits")


logger = logging.getLogger(__name__)

# blocks an edit MERGES key by key instead of replacing: a form that knows two of the six
# bounds must not erase the other four (WK7-UI-1). A key sent as null is removed.
_MERGED_BLOCKS = ("bounds", "floor", "fields", "fallback", "recovery", "pack_limits")


def merge_edit(cur: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """The stored instance with ``payload`` applied: top-level keys replace, the safety
    blocks merge one level deep so a partial edit keeps what it did not mention."""
    out = {**cur, **payload}
    for key in _MERGED_BLOCKS:
        old, new = cur.get(key), payload.get(key)
        if isinstance(old, dict) and isinstance(new, dict):
            block = {**old, **new}
            out[key] = {k: v for k, v in block.items() if v is not None}
    return out


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["vbms"])
    config = ctx.config
    treg = ctx.template_registry

    # ── registry / sources / validation inputs (ADR-0045) ────────────────
    def _publishers() -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for inst in (config.virtual_bms or []):
            out.setdefault(inst.get("channel") or "can0", []).append(inst.get("id"))
        return out

    def _registry(exclude_id: Optional[str] = None) -> List[Dict[str, Any]]:
        from ..outputs.vbms_registry import port_registry
        from ..transports.can import can_interfaces
        try:
            itfs = can_interfaces.interfaces(devices=can_interfaces.devices_by_channel(config))
        except Exception:  # noqa: BLE001 — an odd sysfs never breaks the form
            itfs = []
        return port_registry(itfs, _publishers(), exclude_id=exclude_id)

    def _is_aggregate(tpl: Any) -> bool:
        names = {getattr(rr, "name", "") for rr in (getattr(tpl, "registers", None) or [])}
        return (getattr(tpl, "aggregate", None) == "bank"
                or any(n.startswith("pack_total") or n == "batteries_online" for n in names))

    def _source_devices() -> List[Dict[str, Any]]:
        from ..catalog.device_template import kind_from_names
        out = []
        for dev in (config.devices or []):
            tpl = treg.get(dev.template) if getattr(dev, "template", None) else None
            kind = (getattr(tpl, "kind", "") or kind_from_names(tpl)) if tpl else ""
            out.append({"id": dev.id, "name": getattr(dev, "name", None) or dev.id,
                        "kind": kind, "is_aggregate": _is_aggregate(tpl) if tpl else False})
        return out

    def _device_channel(dev: Any) -> Optional[str]:
        from ..core.config import CAN_PROTOCOLS
        if str(getattr(dev, "protocol", "") or "").lower() not in CAN_PROTOCOLS:
            return None
        src = (getattr(dev, "sources", None) or [None])[0]
        can = dict(getattr(src, "can", None) or {}) if src is not None else {}
        return str(can.get("channel") or "can0")

    def _sources() -> List[Dict[str, Any]]:
        from ..outputs.vbms_registry import eligible_sources
        channel = {dev.id: _device_channel(dev) for dev in (config.devices or [])}
        return [{**s, "channel": channel.get(s["id"])} for s in eligible_sources(_source_devices())]

    def _tpl_frames(tpl: Any, *, tx_only: bool) -> set:
        """The CAN frame IDs a template's map carries — only the ones the emulator transmits when
        ``tx_only`` (rx rows, e.g. the inverter keepalive 0x305, are never sent)."""
        from ..codec.can_signal import can_fields, frame_key, is_can_row
        ids = set()
        for rr in (getattr(tpl, "registers", None) or []):
            if not is_can_row(rr):
                continue
            if tx_only and str((can_fields(rr) or {}).get("direction") or "").lower() == "rx":
                continue
            fk = frame_key(rr)
            if fk:
                ids.add(fk[0])
        return ids

    def _tpl_is_emulator(tpl: Any) -> bool:
        from ..codec.can_signal import is_can_row
        return bool(tpl) and any(is_can_row(rr) for rr in (getattr(tpl, "registers", None) or []))

    def _profile_is_emulator(name: str) -> bool:
        return _tpl_is_emulator(treg.get(name))

    def _profile_facts(name: str) -> Optional[Dict[str, Any]]:
        tpl = treg.get(name) if name else None
        if not tpl:
            return None
        return {"bitrate": (getattr(tpl, "protocol", {}) or {}).get("bitrate"),
                "tx_frame_ids": _tpl_frames(tpl, tx_only=True)}

    def _reader_frames() -> Dict[str, set]:
        """Each CAN reader device → the frame IDs its map decodes (for the collision check)."""
        out: Dict[str, set] = {}
        for dev in (config.devices or []):
            if _device_channel(dev) is None:
                continue
            tpl = treg.get(dev.template) if getattr(dev, "template", None) else None
            if tpl:
                out[dev.id] = _tpl_frames(tpl, tx_only=False)
        return out

    def _emulation_profiles() -> List[Dict[str, str]]:
        return [{"id": t.id, "name": getattr(t, "name", t.id)}
                for t in (treg.all() if hasattr(treg, "all") else []) if _tpl_is_emulator(t)]

    def _validate(body: Dict[str, Any], editing: Optional[str] = None) -> List[str]:
        from ..outputs.vbms_registry import config_device_ids, validate_instance
        return validate_instance(
            body, existing_ids=[i.get("id") for i in (config.virtual_bms or [])],
            registry=_registry(exclude_id=editing), sources=_sources(),
            profile_is_emulator=_profile_is_emulator,
            profile_facts=_profile_facts(str(body.get("profile") or "").strip()),
            reader_frames=_reader_frames(), editing=editing,
            device_ids=config_device_ids(config))

    def _clean(body: Dict[str, Any]) -> Dict[str, Any]:
        """Only the known config fields, nothing a caller tacks on; never `armed`."""
        return {k: body[k] for k in _FIELDS if k in body and body[k] is not None}

    # ── live manager + helpers ───────────────────────────────────────────
    def _mgr(request: Request):
        m = getattr(request.app.state, "vbms_manager", None)
        if m is None:
            raise HTTPException(status_code=503, detail="virtual BMS executor not available")
        return m

    def _inst(request: Request, instance_id: str):
        m = _mgr(request)
        if instance_id not in m.instances:
            raise HTTPException(status_code=404, detail=f"no virtual BMS {instance_id!r}")
        return m

    def _find(iid: str):
        for i, inst in enumerate(config.virtual_bms or []):
            if inst.get("id") == iid:
                return i, inst
        return -1, None

    def _refuse_if_armed(request: Request, iid: str) -> None:
        m = _mgr(request)
        inst = m.instances.get(iid)
        if inst is not None and inst.armed:
            raise HTTPException(status_code=409, detail={"errors": [
                f"{iid} is armed — disarm it before editing or deleting"]})

    def _buildable(request: Request, cfg: Dict[str, Any]) -> None:
        """Construct the instance from ``cfg`` WITHOUT keeping it: whatever the field checks
        missed must fail here — a 422, before memory, disk or the running manager change
        (WK7-CFG-1: a config that crashes the constructor used to be saved first)."""
        from ..outputs.vbms_manager import VbmsInstance
        m = _mgr(request)
        try:
            VbmsInstance(cfg, m.load_profile(cfg["profile"]))
        except Exception as e:  # noqa: BLE001 — anything the constructor refuses is a bad config
            raise HTTPException(status_code=422, detail={"errors": [
                f"this configuration cannot be built: {type(e).__name__}: {e}"]})

    def _commit(request: Request, new_list: List[Dict[str, Any]], *,
                apply: Optional[Dict[str, Any]] = None, remove: Optional[str] = None) -> None:
        """One CRUD change, all or nothing (WK7-CFG-4): the new list is put in memory, saved,
        and only THEN does the running manager change. A save that fails puts the old list
        back — memory, disk and runtime never disagree about which instances exist."""
        before = config.virtual_bms
        config.virtual_bms = new_list
        try:
            config.save_yaml_config()
        except Exception as e:
            config.virtual_bms = before
            logger.error("virtual BMS: the configuration could not be saved (%s) — nothing changed", e)
            raise HTTPException(status_code=503, detail={"errors": [
                f"the configuration could not be saved ({type(e).__name__}) — nothing was changed"]})
        m = _mgr(request)
        if apply is not None:
            m.add_or_replace(apply)
        if remove is not None:
            m.remove(remove)
        bridge = getattr(request.app.state, "vbms_command_bridge", None)
        if bridge is not None:
            bridge.sync()

    # ── read: status, the port picker, the eligible packs, the profiles ──
    @r.get("/api/vbms")
    async def vbms_status(request: Request):
        """Every configured virtual BMS: armed, running, its CAN channel and source packs."""
        return _mgr(request).status()

    @r.get("/api/vbms/ports")
    async def vbms_ports(editing: Optional[str] = None):
        """The CAN ports for the picker (ADR-0045): each with its state and availability. Pass
        ``editing=<id>`` so the instance being edited does not see its own port as reserved."""
        return {"ports": _registry(exclude_id=editing)}

    @r.get("/api/vbms/sources")
    async def vbms_sources():
        """The battery devices eligible as source packs."""
        return {"sources": _sources()}

    @r.get("/api/vbms/profiles")
    async def vbms_profiles():
        """The emulation (CAN) profiles a Virtual BMS may use."""
        return {"profiles": _emulation_profiles()}

    @r.get("/api/vbms/{instance_id}")
    async def vbms_get(instance_id: str):
        """One instance's full config (for the edit form). Declared after the literal
        ports/sources/profiles routes so those win."""
        _, cur = _find(instance_id)
        if cur is None:
            raise HTTPException(status_code=404, detail=f"no virtual BMS {instance_id!r}")
        return cur

    @r.get("/api/vbms/{instance_id}/preview")
    async def vbms_preview(instance_id: str, request: Request):
        """The live aggregation (ADR-0045 L2): each source pack's contributed values + eligibility,
        the aggregated values sent, and the active frames. Transparency — reads only."""
        m = _inst(request, instance_id)
        return m.preview(m.instances[instance_id])

    # ── write: create / edit / delete (ADR-0045) ─────────────────────────
    @r.post("/api/vbms")
    async def vbms_create(request: Request, payload: Dict = Body(...)):
        """Create a Virtual BMS from the UI. Validated against the shared port registry; the new
        instance starts **disarmed** (nothing transmits until an explicit arm)."""
        errs = _validate(payload)
        if errs:
            raise HTTPException(status_code=422, detail={"errors": errs})
        cfg = _clean(payload)
        _buildable(request, cfg)
        _commit(request, list(config.virtual_bms or []) + [cfg], apply=cfg)
        return {"status": "created", "instance": cfg["id"]}

    @r.patch("/api/vbms/{instance_id}")
    async def vbms_edit(instance_id: str, request: Request, payload: Dict = Body(...)):
        """Edit a Virtual BMS. Refused while armed (disarm first). The merged instance is
        re-validated and rebuilt disarmed."""
        i, cur = _find(instance_id)
        if cur is None:
            raise HTTPException(status_code=404, detail=f"no virtual BMS {instance_id!r}")
        _refuse_if_armed(request, instance_id)
        merged = {**merge_edit(cur, payload), "id": instance_id}
        errs = _validate(merged, editing=instance_id)
        if errs:
            raise HTTPException(status_code=422, detail={"errors": errs})
        cfg = _clean(merged)
        _buildable(request, cfg)
        lst = list(config.virtual_bms or [])
        lst[i] = cfg
        _commit(request, lst, apply=cfg)
        return {"status": "updated", "instance": instance_id}

    @r.delete("/api/vbms/{instance_id}")
    async def vbms_delete(instance_id: str, request: Request):
        """Delete a Virtual BMS. Refused while armed (disarm first)."""
        i, cur = _find(instance_id)
        if cur is None:
            raise HTTPException(status_code=404, detail=f"no virtual BMS {instance_id!r}")
        _refuse_if_armed(request, instance_id)
        lst = list(config.virtual_bms or [])
        del lst[i]
        _commit(request, lst, remove=instance_id)
        return {"status": "deleted", "instance": instance_id}

    # ── arm / disarm (ADR-0037) ──────────────────────────────────────────
    @r.post("/api/vbms/{instance_id}/arm")
    async def vbms_arm(instance_id: str, request: Request):
        """Arm one instance — it starts transmitting to its inverter. Needs the
        ``security.allow_writes`` master switch on (arming is a hardware write), and the CAN
        interface out of listen-only on the host (otherwise the transmitter refuses and it stays
        silent)."""
        from ..core import prerequisites as prereq
        from ..transports.can import can_interfaces
        m = _inst(request, instance_id)
        channel = m.instances[instance_id].channel
        try:
            itfs = {i.get("name"): i for i in can_interfaces.interfaces()}
        except Exception:  # noqa: BLE001 — an odd sysfs must not block arming
            itfs = {}
        _, cfg_inst = _find(instance_id)
        expected = (_profile_facts((cfg_inst or {}).get("profile") or "") or {}).get("bitrate")
        missing = prereq.unmet([prereq.allow_writes(ctx.config),   # 409 + guided dialog (ADR-0046)
                                prereq.can_transmit(channel, itfs.get(channel)),
                                prereq.can_bitrate(channel, itfs.get(channel), expected)])
        if missing:
            raise HTTPException(status_code=409, detail=prereq.detail(missing))
        from ..outputs.vbms_manager import VbmsGateClosed
        try:
            m.arm(instance_id)
        except VbmsGateClosed as e:                  # e.g. this gateway is standby
            raise HTTPException(status_code=409, detail={"errors": [f"not armed — {e}"]})
        return {"status": "armed", "instance": instance_id}

    @r.post("/api/vbms/{instance_id}/disarm")
    async def vbms_disarm(instance_id: str, request: Request):
        """Disarm one instance — the loop stops, the transmitter closes, the inverter falls to its
        safe BMS-loss state."""
        _inst(request, instance_id).disarm(instance_id)
        return {"status": "disarmed", "instance": instance_id}

    @r.post("/api/vbms/{instance_id}/packs/{pack}/clear")
    async def vbms_clear_pack(instance_id: str, pack: str, request: Request):
        """Readmit a pack held out for flapping (R2D-VBMS-3, design §3.4). It still has to
        stay eligible for ``return_after_s`` and comes back through the ramp."""
        m = _inst(request, instance_id)
        if pack not in m.instances[instance_id].packs:
            raise HTTPException(status_code=404, detail=f"{pack!r} is not a pack of {instance_id}")
        if not m.clear_pack(instance_id, pack):
            raise HTTPException(status_code=409, detail={"errors": [
                f"{pack} is not held out — nothing to clear"]})
        return {"status": "cleared", "instance": instance_id, "pack": pack}

    return r
