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
"""Config backup / restore (ZIP) — export and import the whole configuration.

Two endpoints (GET /api/config/export, POST /api/config/import) plus their
create_api helpers and constants (BACKUP_VERSION, _SECRET_PATHS,
_IDENTITY_PATHS, _strip_device_secrets, _strip_paths — all used only here) moved
VERBATIM out of create_api (P0.8b Lot G) into a ``build(ctx) -> APIRouter``
module. Bodies are byte-identical; the reload/restore tail is shared with
snapshot restore, so _reload_from_disk / _reload_state / _dangling_after_restore
/ _reissued_response stay in create_api and are reached through ``ctx``;
write_bundle_files is imported directly. The logger keeps the name
``multibus.api``.
"""
from __future__ import annotations

import asyncio
import hmac
import io
import json
import logging
import zipfile
from pathlib import Path as _Path

import yaml as _yaml
from fastapi import APIRouter, HTTPException, Query, Request, Response


logger = logging.getLogger("multibus.api")


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["config"])

    config = ctx.config
    auth_state = ctx.auth_state
    audit_log = ctx.audit_log
    template_registry = ctx.template_registry
    snapshot_store = ctx.snapshot_store
    _api_key = ctx.api_key
    _reload_from_disk = ctx.reload_from_disk
    _reload_state = ctx.reload_state
    _dangling_after_restore = ctx.dangling_after_restore
    _reissued_response = ctx.reissued_response

    BACKUP_VERSION = 1
    # secret keys stripped from config.yaml on export unless include_secrets=true
    _SECRET_PATHS = [("mqtt", "password"), ("influxdb", "token"),
                     ("ui", "auth", "password"), ("ui", "auth", "viewer_password"),
                     ("ui", "auth", "operator_password"),
                     ("alerts", "webhook_headers"),   # holds the webhook X-API-Key/bearer
                     ("rest_push", "headers")]        # primary device REST-push auth headers

    def _strip_device_secrets(data: dict):
        """Redact the secrets that live inside the devices[] and endpoints[]
        trees, wherever they sit: a device's connection / http / mqtt_in /
        rest_push blocks, each of its sources, every installation group's
        sources. Secret-named fields are dropped and every URL goes through
        redact_url (userinfo and secret query values). These are NOT top-level
        so _strip_paths misses them. The import side (snapshots.
        _reinject_secret_tree) recognizes the stripped and redacted forms and
        keeps the live originals on a merge-import. (3.83.0: the old version
        only knew connection.* and rest_push.*, so http.headers, mqtt_in.
        password and per-source credentials went out in a "sanitized" export
        any viewer could download.)"""
        from multibus.core.redact import redact_url
        from multibus.lifecycle.snapshots import SECRET_FIELDS

        def _strip(node):
            if isinstance(node, dict):
                for k in list(node.keys()):
                    if k in SECRET_FIELDS:
                        node.pop(k, None)
                    elif k == "url" and isinstance(node[k], str) and node[k]:
                        node[k] = redact_url(node[k])
                    else:
                        _strip(node[k])
            elif isinstance(node, list):
                for item in node:
                    _strip(item)
        for sect in ("devices", "endpoints"):
            _strip(data.get(sect))
    # network identity kept out of a portable backup (clone-to-another-host safe)
    _IDENTITY_PATHS = [("ui", "host"), ("ui", "port")]

    def _strip_paths(d: dict, paths):
        for p in paths:
            node = d
            for k in p[:-1]:
                node = node.get(k) if isinstance(node, dict) else None
                if node is None:
                    break
            if isinstance(node, dict):
                node.pop(p[-1], None)

    @r.get("/api/config/export")
    def export_config(request: Request,
                      include_secrets: bool = Query(default=False),
                      include_identity: bool = Query(default=False)):
        """Download a ZIP backup: config.yaml (secrets/identity stripped by
        default), every device's selected_registers.json, user device
        templates, and virtual_meters.yaml. Restores via /api/config/import."""
        # A backup WITH secrets carries the MQTT/InfluxDB credentials and the
        # admin/viewer password hashes, so it always needs a credential — a
        # read-only viewer must not walk away with the admin hash. Export is a GET,
        # and the API-key middleware only guards state-changing methods, so we
        # must check the key HERE too (else an api-key-protected, auth-off box
        # would still leak secrets over a plain GET).
        if include_secrets or include_identity:
            # admin only while login is on; on an auth-off box the API key is
            # the only gate there is (3.83.0: the key no longer stands in for
            # the role — every operator holds it)
            _is_admin = auth_state.enabled and getattr(request.state, "role", None) == "admin"
            _key_ok = (not auth_state.enabled) and bool(_api_key) and hmac.compare_digest(
                request.headers.get("X-API-Key", ""), _api_key)
            if not (_is_admin or _key_ok):
                raise HTTPException(status_code=403, detail={"errors": [
                    "exporting secrets/identity requires the admin role"
                    + ("" if auth_state.enabled else " or a valid API key")]})
        if include_secrets or include_identity:
            audit_log.append(user=getattr(request.state, "user", "") or "-",
                             ip=request.client.host if request.client else "-",
                             action="config export", status="ok",
                             detail={"include_secrets": include_secrets,
                                     "include_identity": include_identity})
        cfg_dir = config.config_path.parent
        buf = io.BytesIO()
        manifest = {"backup_version": BACKUP_VERSION,
                    "app_version": __import__("multibus").__version__,
                    "include_secrets": include_secrets,
                    "include_identity": include_identity,
                    "devices": [d.id for d in config.devices]}
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            # config.yaml (sanitized copy — never mutate the live file)
            if config.config_path.exists():
                data = _yaml.safe_load(config.config_path.read_text()) or {}
                if not include_secrets:
                    _strip_paths(data, _SECRET_PATHS)
                    _strip_device_secrets(data)
                    # top-level URLs can embed credentials too (userinfo /
                    # ?token=…) — same redaction, same import re-injection
                    from multibus.core.redact import redact_url as _redact_url
                    for _sect in ("rest_push", "influxdb"):
                        _node = data.get(_sect)
                        if isinstance(_node, dict) and _node.get("url"):
                            _node["url"] = _redact_url(_node["url"])
                    _al = data.get("alerts")
                    if isinstance(_al, dict) and _al.get("webhook_url"):
                        from urllib.parse import urlsplit, urlunsplit
                        u = urlsplit(str(_al["webhook_url"]))
                        if u.query or "@" in u.netloc:      # token in query/userinfo
                            host = u.netloc.rsplit("@", 1)[-1]
                            _al["webhook_url"] = urlunsplit((u.scheme, host, u.path, "", ""))
                if not include_identity:
                    _strip_paths(data, _IDENTITY_PATHS)
                z.writestr("config.yaml", _yaml.dump(data, default_flow_style=False,
                                                     allow_unicode=True, sort_keys=False))
            # per-device selected registers
            for d in config.devices:
                p = config.device_registers_path(d.id)
                if p.exists():
                    z.writestr(f"devices/{d.id}/selected_registers.json", p.read_text())
            # user device templates
            from multibus.catalog.device_template import USER_DIR as _UDIR
            udir = _Path(getattr(template_registry, "user_dir", _UDIR))
            if udir.is_dir():
                for f in udir.iterdir():
                    if f.suffix.lower() in (".json", ".yaml", ".yml"):
                        z.writestr(f"device_templates/{f.name}", f.read_text())
            # virtual meters (instances + user templates)
            vm = cfg_dir / "virtual_meters.yaml"
            if vm.exists():
                z.writestr("virtual_meters.yaml", vm.read_text())
            _vtpl = cfg_dir / "templates"
            if _vtpl.is_dir():
                for f in sorted(_vtpl.iterdir()):
                    if f.suffix.lower() in (".yaml", ".yml"):
                        z.writestr(f"templates/{f.name}", f.read_text())
            # calculated presets and the asset attributes travel with every
            # backup; the passkey registry (identity) only when secrets do.
            # (rules.yaml / sequences.yaml are not in this export: an import
            # would replace the live rules, armed modes included — a decision
            # of its own, not a side effect)
            for name in ("calculated_templates.json",):
                p = cfg_dir / name
                if p.exists():
                    z.writestr(name, p.read_text())
            _attrs_p = cfg_dir / "attributes.yaml"
            if _attrs_p.exists():
                from multibus.lifecycle.snapshots import strip_attribute_locations as _strip_locs
                _raw_attrs = _attrs_p.read_bytes()
                # coordinates are identity: only with include_identity
                try:
                    z.writestr("attributes.yaml", _raw_attrs if include_identity else _strip_locs(_raw_attrs))
                except ValueError as e:      # a broken file is left out, never half-stripped
                    logger.error("export: %s — attributes.yaml left out", e)
            if include_secrets:
                p = cfg_dir / "passkeys.json"
                if p.exists():
                    z.writestr("passkeys.json", p.read_text())
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
        buf.seek(0)
        return Response(
            content=buf.getvalue(), media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="multibus-config-backup.zip"'})

    @r.post("/api/config/import")
    async def import_config(request: Request, apply: bool = Query(default=True),
                            restore_identity: bool = Query(default=False),
                            lock_writes: bool = Query(default=True),
                            dry_run: bool = Query(default=False)):
        """Restore a ZIP backup produced by /api/config/export. The raw ZIP is
        sent as the request body (application/zip) — no multipart dependency.
        Writes the files back under the config dir (paths validated — no
        traversal), reloads config and hot-applies. Secrets/identity absent
        from the backup keep their current values (the live config.yaml is
        merged, not blindly overwritten)."""
        # Bound the input: a config backup is tiny, so cap the body and the total
        # uncompressed size to refuse an oversized upload / ZIP bomb (OOM guard).
        _MAX_IMPORT = 25 * 1024 * 1024        # 25 MB compressed
        _cl = request.headers.get('content-length', '')
        if _cl.isdigit() and int(_cl) > _MAX_IMPORT:
            raise HTTPException(status_code=413, detail={"errors": ["backup too large (max 25 MB)"]})
        raw = await request.body()
        if len(raw) > _MAX_IMPORT:
            raise HTTPException(status_code=413, detail={"errors": ["backup too large (max 25 MB)"]})
        cfg_dir = config.config_path.parent
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except Exception:
            raise HTTPException(status_code=422, detail={"errors": ["not a valid ZIP file"]})
        if sum(getattr(zi, 'file_size', 0) for zi in zf.infolist()) > 50 * 1024 * 1024:
            raise HTTPException(status_code=422, detail={"errors": ["archive expands too large (ZIP bomb?)"]})
        from multibus.catalog.device_template import USER_DIR as _UDIR
        from multibus.lifecycle.snapshots import (apply_bundle as _apply_bundle,
                                                   bundle_write_disclosure as _disclosure,
                                                   BundleInvalid as _BundleInvalid,
                                                   BundleAppliedReloadFailed as _ReloadFailed,
                                                   PreopSnapshotFailed as _PreopFailed)
        # F20: what the backup would unlock. is_restore=False — an import bundle carries
        # no rules.yaml, so it can never re-arm a rule (R omitted).
        try:
            unlock = _disclosure(raw, is_restore=False)
        except Exception:  # noqa: BLE001 — a malformed bundle fails validation below anyway
            unlock = None
        if dry_run:
            return {"status": "dry-run", "would_unlock": unlock, "lock_writes_default": True}
        # runs inside the reload, before its registry resync binds the locked config
        _lock_fn = config.force_lock_all_writes

        def _import_on_disk():
            # the shared bundle-apply (ADR-0041): validate → pre-op snapshot →
            # write → reload. Merge import (replace_config=False): sanitized
            # backups merge config.yaml over the live file so stripped
            # secrets/identity survive; per-device register paths map through
            # device_registers_path (the primary keeps its legacy root file).
            # N fsyncs and a full Modbus reconnect: off the event loop (F-38, 3.83.0).
            return _apply_bundle(
                zf, cfg_dir=cfg_dir,
                user_tpl_dir=_Path(getattr(template_registry, "user_dir", _UDIR)),
                registers_path_for=config.device_registers_path, replace_config=False,
                allow_nonlan=config.security.allow_nonlan_http_devices,
                snapshot_store=snapshot_store, pre_label="pre-import",
                user=getattr(request.state, "user", "") or "",
                reload_from_disk=_reload_from_disk, apply=apply,
                restore_identity=restore_identity, logger=logger,
                lock_writes=lock_writes, lock_writes_fn=_lock_fn)
        try:
            summary, _rotated = await asyncio.to_thread(_import_on_disk)
        except _BundleInvalid as e:
            raise HTTPException(status_code=422, detail={"errors": e.errors})
        except _PreopFailed as e:                    # couldn't snapshot (C3): abort, nothing written
            raise HTTPException(status_code=500, detail={"errors": [str(e)],
                                                         "snapshot": e.pre_label, "applied": False})
        except _ReloadFailed as e:                   # files written, reload failed (H1): 500, not 422
            raise HTTPException(status_code=500, detail={"errors": [str(e)],
                                                         "snapshot": e.pre_label, "written": True})
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        # a non-primary device materializes only on (re)start — only the legacy
        # primary is hot-applied — so ANY non-primary device present means the
        # import needs a restart (REFAC-RESTART-1: `len > 1` missed a lone
        # non-primary device, where len == 1 but it is not the primary).
        _restart = any(not d.primary for d in config.devices) or _reload_state['primary_changed']
        summary["note"] = ("imported; a restart is recommended so newly-added "
                           "devices start polling") if _restart else "imported"
        if _reload_state['primary_changed']:
            summary["note"] = ("imported; the legacy primary device (primary_device) "
                               "changed — restart the gateway to apply it")
        summary["restart_required"] = _restart
        summary["write_locked_forced"] = bool(lock_writes)
        summary["would_unlock"] = unlock
        logger.info(f"config import ({'writes LOCKED' if lock_writes else 'writes kept'}): {summary}")
        resp = {"status": "ok", **summary, "dangling": _dangling_after_restore("config import")}
        if _rotated:
            return _reissued_response(request, resp)
        return resp

    return r
