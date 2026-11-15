# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
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
"""Config snapshots — list / create / download / delete / diff / restore.

Six endpoints under ``/api/config/snapshots`` moved VERBATIM out of create_api
(P0.8b Lot G) into a ``build(ctx) -> APIRouter`` module. The route bodies are
byte-identical; the create_api collaborators they close over are aliased from
``ctx`` at the top of ``build``. ``snapshot_store`` and the four reload/restore
helpers (``_reload_from_disk``, ``_reload_state``, ``_dangling_after_restore``,
``_reissued_response``, all still defined in create_api because the config
routes share them) are carried on ``ctx``; ``write_bundle_files`` is imported
directly. The logger keeps the name ``multibus.api`` so log lines are unchanged.
"""
from __future__ import annotations

import hmac
import io
import logging
import zipfile
from pathlib import Path as _Path
from typing import Dict, List   # noqa: F401 — used by verbatim route bodies/signatures

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response


logger = logging.getLogger("multibus.api")


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["config"])

    config = ctx.config
    auth_state = ctx.auth_state
    audit_log = ctx.audit_log
    event_log = ctx.event_log
    template_registry = ctx.template_registry
    _api_key = ctx.api_key
    snapshot_store = ctx.snapshot_store
    _reload_from_disk = ctx.reload_from_disk
    _reload_state = ctx.reload_state
    _dangling_after_restore = ctx.dangling_after_restore
    _reissued_response = ctx.reissued_response

    @r.get("/api/config/snapshots")
    def list_snapshots():
        """Automatic + manual snapshots, newest first (LKG on top when present)."""
        return {"snapshots": snapshot_store.list(), "keep": snapshot_store.keep}

    @r.post("/api/config/snapshots")
    def create_snapshot(request: Request, payload: Dict = Body(default={})):
        """Take a manual snapshot of the current config bundle."""
        meta = snapshot_store.create("manual",
                                     user=getattr(request.state, "user", "") or "",
                                     note=str(payload.get("note", "") or ""))
        return {"status": "ok", "snapshot": meta}

    @r.get("/api/config/snapshots/{sid}/download")
    def download_snapshot(request: Request, sid: str):
        """Download a snapshot ZIP. Snapshots are FULL-FIDELITY (secrets and
        identity included — they are local restore points), so downloading one
        is gated exactly like a with-secrets export."""
        # UNCONDITIONAL gate, exactly like the with-secrets export: a snapshot
        # is full-fidelity (MQTT password, InfluxDB token, password hashes), so
        # proving admin (or a valid API key) is required REGARDLESS of whether
        # login is enabled. The old `auth_state.enabled and …` made the whole
        # check vanish on an auth-off box — any LAN peer could GET the secrets.
        _is_admin = auth_state.enabled and getattr(request.state, "role", None) == "admin"
        _key_ok = (not auth_state.enabled) and bool(_api_key) and hmac.compare_digest(
            request.headers.get("X-API-Key", ""), _api_key)
        if not (_is_admin or _key_ok):
            raise HTTPException(status_code=403, detail={"errors": [
                "downloading a snapshot requires the admin role"
                + ("" if auth_state.enabled else " or a valid API key")]})
        p = snapshot_store.get_path(sid)
        if p is None:
            raise HTTPException(status_code=404, detail=f"unknown snapshot {sid!r}")
        audit_log.append(user=getattr(request.state, "user", "") or "-",
                         ip=request.client.host if request.client else "-",
                         action="snapshot download", target=sid, status="ok")
        return Response(content=p.read_bytes(), media_type="application/zip",
                        headers={"Content-Disposition":
                                 f'attachment; filename="config-snapshot-{sid}.zip"'})

    @r.delete("/api/config/snapshots/{sid}")
    def delete_snapshot(sid: str):
        if sid == "lkg":
            raise HTTPException(status_code=400,
                                detail="the last-known-good snapshot cannot be deleted")
        if not snapshot_store.delete(sid):
            raise HTTPException(status_code=404, detail=f"unknown snapshot {sid!r}")
        return {"status": "deleted"}

    @r.get("/api/config/snapshots/{sid}/diff")
    def diff_snapshot(sid: str, against: str = Query(default="live")):
        """Semantic, secrets-masked diff: what changed SINCE this snapshot
        (against=live, default) or between two snapshots (against=<sid>).
        A rollback to the snapshot would undo exactly these changes."""
        from multibus.lifecycle.snapshots import diff_bundles
        p = snapshot_store.get_path(sid)
        if p is None:
            raise HTTPException(status_code=404, detail=f"unknown snapshot {sid!r}")
        if against == "live":
            newer = snapshot_store.build_bundle_bytes()
        else:
            p2 = snapshot_store.get_path(against)
            if p2 is None:
                raise HTTPException(status_code=404, detail=f"unknown snapshot {against!r}")
            newer = p2.read_bytes()
        return {"base": sid, "against": against,
                **diff_bundles(p.read_bytes(), newer)}

    @r.post("/api/config/snapshots/{sid}/restore")
    def restore_snapshot(request: Request, sid: str, apply: bool = Query(default=True),
                         restore_identity: bool = Query(default=False),
                         lock_writes: bool = Query(default=True),
                         dry_run: bool = Query(default=False)):
        """Roll the config back to a snapshot. The current state is snapshotted
        first ('pre-restore'), so a rollback is itself reversible. Snapshots are
        verbatim, so config.yaml is REPLACED, not merged.

        F20 (ADR-0041 L2): ``dry_run=true`` returns what the snapshot would unlock/arm
        (devices/components/units with the lock off, enabled command bindings, armed
        rules) WITHOUT applying. On a real restore ``lock_writes`` (default true)
        forces every write path locked the instant the reload lands — binding the
        running devices at once, so an armed rule cannot fire until the operator
        unlocks (locking also suspends the safety/OV rules; the Ekrano failsafe
        backstops). Send ``lock_writes=false`` to keep the snapshot's own lock states."""
        p = snapshot_store.get_path(sid)
        if p is None:
            raise HTTPException(status_code=404, detail=f"unknown snapshot {sid!r}")
        from multibus.lifecycle.snapshots import (apply_bundle as _apply_bundle,
                                                   bundle_write_disclosure as _disclosure,
                                                   BundleInvalid as _BundleInvalid,
                                                   BundleAppliedReloadFailed as _ReloadFailed,
                                                   PreopSnapshotFailed as _PreopFailed)
        from multibus.catalog.device_template import USER_DIR as _UDIR
        _bytes = p.read_bytes()
        # F20: what the snapshot would unlock/arm — shown on dry-run AND the real apply.
        try:
            unlock = _disclosure(_bytes, is_restore=True)
        except Exception:  # noqa: BLE001 — a malformed bundle fails validation below anyway
            unlock = None
        if dry_run:
            return {"status": "dry-run", "snapshot": sid, "would_unlock": unlock,
                    "lock_writes_default": True}
        # runs inside the reload, before its registry resync binds the locked config
        _lock_fn = config.force_lock_all_writes
        try:
            with zipfile.ZipFile(io.BytesIO(_bytes)) as zf:
                # the shared bundle-apply (ADR-0041): validate → pre-op snapshot →
                # write → reload → (F20) force-lock. Verbatim restore (replace_config=True).
                summary, _rotated = _apply_bundle(
                    zf, cfg_dir=config.config_path.parent,
                    user_tpl_dir=_Path(getattr(template_registry, "user_dir", _UDIR)),
                    registers_path_for=config.device_registers_path, replace_config=True,
                    allow_nonlan=config.security.allow_nonlan_http_devices,
                    snapshot_store=snapshot_store, pre_label="pre-restore",
                    user=getattr(request.state, "user", "") or "",
                    reload_from_disk=_reload_from_disk, apply=apply,
                    restore_identity=restore_identity, logger=logger,
                    lock_writes=lock_writes, lock_writes_fn=_lock_fn)
        except _BundleInvalid as e:
            raise HTTPException(status_code=422, detail={"errors": e.errors})
        except _PreopFailed as e:                    # couldn't snapshot (C3): abort, nothing written
            raise HTTPException(status_code=500, detail={"errors": [str(e)],
                                                         "snapshot": e.pre_label, "applied": False})
        except _ReloadFailed as e:                   # files written, reload failed (H1): 500, not 422
            raise HTTPException(status_code=500, detail={"errors": [str(e)],
                                                         "snapshot": e.pre_label, "written": True})
        except (ValueError, zipfile.BadZipFile) as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        _locknote = "writes LOCKED (safe default)" if lock_writes else "writes kept at the snapshot's states"
        event_log.add("warn", "snapshots", f"config rolled back to snapshot {sid} — {_locknote}")
        logger.warning(f"config restored from snapshot {sid} — {_locknote}: {summary}")
        # _reload_from_disk hot-applies the PRIMARY only; non-primary devices are
        # reconstructed on the next restart, so tell the operator when one is
        # needed (more than one device present) rather than leave it implicit.
        # non-primary devices materialize only on restart (REFAC-RESTART-1): a
        # lone non-primary device needs one too, which `len > 1` missed.
        restart_required = any(not d.primary for d in config.devices) or _reload_state['primary_changed']
        resp = {"status": "ok", "restored": sid, "restart_required": restart_required,
                "write_locked_forced": bool(lock_writes), "would_unlock": unlock,
                **summary, "dangling": _dangling_after_restore(f"snapshot {sid} restore")}
        if _rotated:
            return _reissued_response(request, resp)
        return resp

    return r
