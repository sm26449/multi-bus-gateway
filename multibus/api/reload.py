# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Backup-import / snapshot-restore reload tail, extracted whole from create_api (P0.8b squeeze).

ReloadService holds the restart-lite reload closures: dangling_after_restore (reference
report), identity_fields (login-identity tuple), reload_from_disk (reload config +
registries and hot-apply) and reissued_response (fresh admin cookie after an identity
rotation). Kept as closures built in __init__ so the bodies are verbatim; collaborators
are an explicit keyword constructor. Layer-clean intermediate in the api layer.
"""
from __future__ import annotations

import logging
from typing import Dict, List

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from multibus import auth as _auth

logger = logging.getLogger("multibus.api")  # preserve caplog logger name


class ReloadService:
    """The config reload / restore tail (ADR-0038 service, api-layer for now)."""

    def __init__(self, *, config, app, attributes, audit_log, auth_state, event_log, modbus_client, _pub, template_registry, _publish_attribute_meta, _reload_state):
        def _dangling_after_restore(what: str) -> List[Dict]:
            """An import or a restore replaces the configuration as one set, so it
            is not refused for what it leaves out — but the references it makes
            to devices it does not have are said (in the answer and the event
            log), read from the files it just wrote."""
            from multibus.lifecycle.dependents import Sources, dangling
            from multibus.control.rules_runtime import RuleStore
            try:
                rules = {str(r.get('id') or ''): r for r in RuleStore(config.config_path.parent / 'rules.yaml').load()}
                store = getattr(app.state, 'sequence_store', None)
                out = dangling(Sources(config=config, rules=lambda: rules,
                                       sequences=(store.load if store is not None else list)))
            except Exception:  # noqa: BLE001 — a report, never a failed import
                logger.warning("%s: dangling references not judged", what, exc_info=True)
                return []
            if out:
                names = sorted({f"{d['id']} → {d['missing']}" for d in out})
                event_log.add("warn", "config", f"{what}: {len(names)} reference(s) to devices that are not here: "
                                                + ', '.join(names[:10]) + (' …' if len(names) > 10 else ''))
            return out

        def _identity_fields() -> tuple:
            """The ui.auth fields that make up login identity — compared around a
            disk reload to detect that an import/restore rotated credentials."""
            u = config.ui
            return (u.auth_enabled, u.auth_username, u.auth_password,
                    getattr(u, "viewer_username", "") or "",
                    getattr(u, "viewer_password", "") or "",
                    getattr(u, "operator_username", "") or "",
                    getattr(u, "operator_password", "") or "")

        def _reload_from_disk(apply: bool = True, restore_identity: bool = False) -> bool:
            """Reload config + registries from disk and hot-apply — the shared tail
            of backup import and snapshot restore (restart-lite). Returns True when
            the reload rotated identity: all sessions were revoked, so the caller
            should re-issue its own cookie (like update_ui_security does).

            The F20 forced write-lock is not applied here: ``Config.load()`` applies it to
            its staging copy before it publishes anything (R2D-F20-1), so by the time this
            function binds the registry or starts a device the config is already locked."""
            _before = _identity_fields()
            _primary_before = config.primary_enabled
            _gw_before = (config.identity_scope, config.gateway_id)
            _old_devices = list(config.devices)          # for the device reconcile below (ADR-0047 P2)
            # An import/restore replaces what a Virtual BMS aggregates and is allowed to send:
            # every armed instance is disarmed BEFORE the new config is loaded and stays so
            # until an explicit re-arm — the CAN side of the F20 safe restore (WK7-LIFE-1).
            _vbm = getattr(app.state, "vbms_manager", None)
            if _vbm is not None:
                try:
                    # freeze = revoke + refuse every arm until thaw(): an arm request that
                    # arrives WHILE the reload runs must not slip in behind the revoke and
                    # survive it (R2D-LIFE-1)
                    _vbm.freeze("the configuration is being imported/restored")
                except Exception:  # noqa: BLE001 — the reload must go on; the loop's gate still holds
                    logger.exception("virtual BMS freeze before reload")
            try:
                return _reload_locked(apply, restore_identity, _before, _primary_before,
                                      _gw_before, _old_devices)
            finally:
                if _vbm is not None:
                    # ALWAYS, also when the reload raised half-way: the instance set follows
                    # whatever config is live now (removed → dropped, changed or new → rebuilt
                    # disarmed), its EMS command topics too, and only then may it be armed again
                    try:
                        _vbm.reconcile(config)
                        _bridge = getattr(app.state, "vbms_command_bridge", None)
                        if _bridge is not None:
                            _bridge.sync()
                    except Exception:  # noqa: BLE001
                        logger.exception("virtual BMS reconcile after reload")
                    finally:
                        _vbm.thaw()

        def _reload_locked(apply, restore_identity, _before, _primary_before, _gw_before,
                           _old_devices) -> bool:
            """The body of ``_reload_from_disk``, run while the Virtual BMS manager is frozen."""
            if not config.load():
                # The file on disk could not be read (parse error / implausible shape). load()
                # refused it and kept the running configuration (ADR-0047 §1), so there is
                # nothing new to bind the runtime to — and binding it to anything else would
                # read every device missing from it as "removed" (RECON-TRUNC-1). Touch nothing
                # that runs and say so; the caller names the pre-op snapshot to go back to.
                raise RuntimeError("config.yaml could not be loaded after the write — the running "
                                   "configuration and devices were left as they were")
            if not restore_identity and (config.identity_scope, config.gateway_id) != _gw_before:
                # how this gateway names new devices is its own, not the bundle's
                # (another gateway's backup, last month's snapshot): kept unless
                # the restore asks for it (ADR-0026 §3)
                config.set_identity(scope=_gw_before[0], gateway_id=_gw_before[1])
                try:
                    config.save_yaml_config()
                except Exception:  # noqa: BLE001 — kept in memory; the next save writes it
                    logger.exception("restore: the live gateway identity could not be written back")
            # device #1 appearing or going (ADR-0024) changes the pollers, the
            # publishers' HA identity and the live store: only a restart builds them
            _reload_state['primary_changed'] = config.primary_enabled != _primary_before
            attributes._invalidate()          # a restored or imported attributes.yaml
            # hot-apply imported login settings so a backup that enables/disables auth
            # or rotates credentials takes effect immediately (the middleware reads
            # auth_state live) — matches update_ui_security(), not a restart-only change
            auth_state.reload(config.ui)
            # Identity rotated by the bundle: pre-existing cookies must not outlive
            # it. update_ui_security() revokes on rotation, but import/restore reach
            # reload() directly and used to skip the revocation — "restored last
            # week's snapshot to kick the attacker" left the attacker's session
            # valid for up to 30 days.
            revoked = False
            if _identity_fields() != _before:
                n = auth_state.revoke_all_sessions()
                revoked = True
                logger.warning("import/restore rotated identity — "
                               f"{n} session(s) revoked")
            # The bundle may carry its own passkeys.json (full-fidelity snapshots
            # do): re-read the live store, else assertions keep validating against
            # the pre-restore credentials and the next sign-count update would
            # overwrite the restored file with the stale list.
            _pk = getattr(getattr(app.state, "ctx", None), "passkey_store", None)
            if _pk is not None:
                _pk.reload()
                # prune credentials whose account no longer exists — mirrors the
                # ui-security rename/remove pruning (F-16); login_finish refuses
                # them too, so this is defense-in-depth plus registry hygiene
                _live = {u for u in (auth_state.admin_user, auth_state.operator_user,
                                     auth_state.viewer_user) if u}
                for _c in _pk.list():
                    if _c.get("user") not in _live:
                        _pk.delete(_c["id"])
                        audit_log.append(user="-", ip="-", action="passkey pruned",
                                         target=str(_c.get("user", "")), status="ok",
                                         detail={"reason": "account absent after import/restore",
                                                 "label": _c.get("label", "")})
            template_registry.reload()
            # Bind the reloaded config onto the RUNNING registry so a write-lock (and
            # every other derived DeviceConfig field) a restore/import brought back
            # takes effect immediately — a lock is safety-critical and must not wait
            # for a restart or for apply=True (REFAC-LOCK-1). `resync` only re-pairs
            # the fresh configs with the existing clients (no reconnect, no I/O), so
            # it is safe on apply=false too; without it a queued command still saw the
            # stale, unlocked registry device and wrote.
            _registry = getattr(app.state, "registry", None)
            # the running clients BEFORE resync: resync drops removed devices from the pairs but
            # leaves their client running, so the reconcile below needs these to stop the orphans
            _old_clients = {c.id: cl for c, cl in _registry.pairs()} if _registry is not None else {}
            if _registry is not None:
                _registry.resync(config.devices, modbus_client)
            # Runs BEFORE the primary reconnect and the publisher updates below: those can raise,
            # and a removed device's client must already be stopped by then — once resync has
            # dropped it, nothing can find it again (R2D-REMOVE-1).
            # Non-primary device membership (ADR-0047 P2): stop REMOVED devices on EVERY reload —
            # the resync above already dropped them from the registry, so without this their
            # client runs on, unreachable (RECON-REMOVE-2) — and start ADDED ones only on an
            # apply. A RETARGETED device keeps its client and refuses writes (ADR-0042).
            _recon = getattr(app.state, "reconcile_devices", None)
            if _recon is not None:
                try:
                    _recon(_old_devices, config.devices, _old_clients, start_added=apply)
                except Exception:  # noqa: BLE001 — a reconcile error must not fail the reload
                    logger.exception("device reconcile after reload")
            if apply:
                # rebuild device runtime like a restart-lite: reconnect primary +
                # reload its registers; other devices are picked up on next restart
                if modbus_client:
                    modbus_client.update_config(config.modbus)
                    modbus_client.update_registers(config.selected_registers, config.poll_groups)
                    modbus_client.reconnect()
                    # the primary's client was just rebuilt onto the new target, so
                    # its wire is now the applied one again (ADR-0042) — clear any
                    # retarget guard for it. Non-primary clients are NOT rebuilt here,
                    # so they stay guarded until a restart.
                    _pd = config.get_device(config.primary_id) if config.primary_id else None
                    if _registry is not None and _pd is not None:
                        _registry.client_rebuilt(_pd)
                if _pub.mqtt:
                    _pub.mqtt.update_config(config.mqtt)
                    _pub.mqtt.update_registers(config.selected_registers)
                if _pub.influx:
                    _pub.influx.update_config(config.influxdb)
                    _pub.influx.update_registers(config.selected_registers)
            try:
                _publish_attribute_meta()     # what the restore or import changed reaches the broker
            except Exception:  # noqa: BLE001
                logger.exception("attributes meta after reload")
            return revoked

        def _reissued_response(request: Request, resp: Dict) -> Response:
            """JSON response carrying a fresh admin cookie — after an identity
            rotation revoked every session, the admin DOING the import/restore
            must not be logged out by their own action (same pattern as
            update_ui_security). Only for a caller who WAS a logged-in admin:
            an anonymous caller importing a bundle that turns auth ON must log
            in with the imported credentials, not be handed a session."""
            out = JSONResponse(resp)
            if auth_state.enabled and getattr(request.state, "role", None) == "admin":
                token = auth_state.mint_session("admin", auth_state.admin_user)
                secure = bool(config.ui.tls_enabled) or request.url.scheme == "https"
                out.set_cookie(_auth.COOKIE_NAME, token, httponly=True,
                               samesite="lax", secure=secure,
                               max_age=_auth.SESSION_TTL_S, path="/")
            return out

        # --- public surface -------------------------------------------------
        self.dangling_after_restore = _dangling_after_restore
        self.reload_from_disk = _reload_from_disk
        self.reissued_response = _reissued_response
