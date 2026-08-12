# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Configuration read + persist-only writes.

The config sections that only READ or PERSIST settings (Modbus/MQTT/InfluxDB
connection params, UI/login security, IP allowlist) — `save_yaml_config()` and
return; the live services reconnect on a SEPARATE `/api/config/apply`. The
data-path-coupled endpoints (`apply`, `reload-registers`, `import`, snapshots)
stay in ``create_api`` because they rebind the publishers / restart clients.

``config``/``auth_state``/``audit_log``/``app`` are stable singletons (never
rebound), so binding them once here is safe — unlike the publishers.
"""
from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Body, HTTPException, Request

from ._models import InfluxDBConfigUpdate, ModbusConfigUpdate, MQTTConfigUpdate


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["config"])
    config = ctx.config
    auth_state = ctx.auth_state
    audit_log = ctx.audit_log
    app = ctx.app

    @r.get("/api/config")
    async def get_config(request: Request):
        """Get current configuration. URLs are redacted for viewer AND operator
        — only admin sees raw URL-embedded secrets (same convention as
        /api/devices)."""
        data = config.to_dict()
        if getattr(request.state, "role", None) in ("viewer", "operator"):
            from ..redact import redact_url
            _inf = data.get("influxdb")
            if isinstance(_inf, dict) and _inf.get("url"):
                _inf["url"] = redact_url(str(_inf["url"]))
            for _dev in (data.get("devices") or []):
                if isinstance(_dev, dict) and _dev.get("http_url"):
                    _dev["http_url"] = redact_url(str(_dev["http_url"]))
        return data

    @r.get("/api/config/env-overrides")
    async def get_env_overrides():
        """Get environment variable overrides currently in effect."""
        return config.get_env_overrides()

    @r.get("/api/config/modbus")
    async def get_modbus_config():
        """Get Modbus configuration."""
        return {
            "host": config.modbus.host,
            "port": config.modbus.port,
            "unit_id": config.modbus.unit_id,
            "timeout": config.modbus.timeout,
            "retry_attempts": config.modbus.retry_attempts,
            "retry_delay": config.modbus.retry_delay,
        }

    @r.post("/api/config/modbus")
    async def update_modbus_config(update: ModbusConfigUpdate):
        """Update Modbus configuration."""
        try:
            config.update_modbus(**update.model_dump())
            config.save_yaml_config()
            return {"status": "ok", "message": "Modbus config updated. Apply to reconnect."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @r.get("/api/config/mqtt")
    async def get_mqtt_config():
        """Get MQTT configuration."""
        return {
            "enabled": config.mqtt.enabled,
            "broker": config.mqtt.broker,
            "port": config.mqtt.port,
            "username": config.mqtt.username,
            "topic_prefix": config.mqtt.topic_prefix,
            "retain": config.mqtt.retain,
            "qos": config.mqtt.qos,
            "publish_mode": config.mqtt.publish_mode,
            "heartbeat_interval": config.mqtt.heartbeat_interval,
            "allow_write_entities": config.mqtt.allow_write_entities,
            "ha_discovery_enabled": config.mqtt.ha_discovery_enabled,
            "ha_discovery_prefix": config.mqtt.ha_discovery_prefix,
            "ha_device_name": config.mqtt.ha_device_name,
            "tls_enabled": config.mqtt.tls_enabled,
            "tls_ca_cert": config.mqtt.tls_ca_cert,
            "tls_client_cert": config.mqtt.tls_client_cert,
            "tls_client_key": config.mqtt.tls_client_key,
            "tls_insecure": config.mqtt.tls_insecure,
            "default_topic_pattern": config.mqtt.default_topic_pattern,
        }

    @r.post("/api/config/mqtt")
    async def update_mqtt_config(update: MQTTConfigUpdate):
        """Update MQTT configuration."""
        try:
            config.update_mqtt(**update.model_dump())
            config.save_yaml_config()
            return {"status": "ok", "message": "MQTT config updated. Apply to reconnect."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @r.get("/api/config/ui-security")
    async def get_ui_security():
        """HTTPS + login config for the Security card (no passwords returned)."""
        return {
            "tls_enabled": config.ui.tls_enabled,
            "tls_cert": config.ui.tls_cert,
            "tls_key": config.ui.tls_key,
            "auth_enabled": config.ui.auth_enabled,
            "auth_username": config.ui.auth_username,
            "viewer_username": config.ui.viewer_username,
            "operator_username": config.ui.operator_username,
            "canonical_url": config.ui.canonical_url,
            "lockout_threshold": config.ui.lockout_threshold,
            "lockout_minutes": config.ui.lockout_minutes,
            "has_viewer": bool(config.ui.viewer_username),
            "has_operator": bool(config.ui.operator_username),
        }

    @r.post("/api/config/ui-security")
    async def update_ui_security(request: Request, payload: Dict = Body(...)):
        """Update HTTPS + login config. Passwords are hashed on write; a blank
        password keeps the current one. HTTPS changes need a restart."""
        from .. import auth as _auth
        u = config.ui
        # VALIDATE-THEN-COMMIT: build the change set + run every check BEFORE
        # touching the live config, so a rejected request leaves config.ui (and
        # therefore auth_state) exactly as it was — no half-applied auth state
        # persisted by a later unrelated save.
        changes: Dict = {}
        restart_needed = False
        try:
            if "tls_enabled" in payload:
                changes["tls_enabled"] = bool(payload["tls_enabled"])
                restart_needed = restart_needed or (changes["tls_enabled"] != u.tls_enabled)
            if "tls_cert" in payload:
                changes["tls_cert"] = str(payload["tls_cert"]).strip()
            if "tls_key" in payload:
                changes["tls_key"] = str(payload["tls_key"]).strip()
            if "auth_enabled" in payload:
                changes["auth_enabled"] = bool(payload["auth_enabled"])
            if payload.get("auth_username"):
                changes["auth_username"] = str(payload["auth_username"]).strip()
            if payload.get("auth_password"):
                changes["auth_password"] = _auth.hash_password(str(payload["auth_password"]))
            if "viewer_username" in payload:
                changes["viewer_username"] = str(payload["viewer_username"]).strip()
            if payload.get("viewer_password"):
                changes["viewer_password"] = _auth.hash_password(str(payload["viewer_password"]))
            if "operator_username" in payload:
                changes["operator_username"] = str(payload["operator_username"]).strip()
            if "canonical_url" in payload:
                _cu = str(payload["canonical_url"]).strip()
                if _cu and not (_cu.startswith("http://") or _cu.startswith("https://")):
                    raise ValueError("canonical_url must start with http:// or https://")
                changes["canonical_url"] = _cu
            if payload.get("operator_password"):
                changes["operator_password"] = _auth.hash_password(str(payload["operator_password"]))
            if payload.get("lockout_threshold"):
                changes["lockout_threshold"] = int(payload["lockout_threshold"])
            if payload.get("lockout_minutes"):
                changes["lockout_minutes"] = int(payload["lockout_minutes"])
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=422, detail={"errors": [f"invalid value: {e}"]})
        if changes.get("tls_cert") is not None:
            restart_needed = restart_needed or changes.get("tls_enabled", u.tls_enabled)
        # guard: enabling auth requires a real hashed admin password (the default
        # plaintext "admin" is accepted by verify_password, so enabling login
        # without a NEW password would leave admin/admin usable). Evaluate against
        # the RESULTING state, not the live object.
        _res_enabled = changes.get("auth_enabled", u.auth_enabled)
        _res_pw = changes.get("auth_password", u.auth_password)
        if _res_enabled and not _auth.is_hashed(_res_pw):
            raise HTTPException(status_code=422, detail={"errors": [
                "set a new admin password before enabling login "
                "(the default password cannot be used)"]})
        # Enabling login for the first time: surface any passkeys enrolled while
        # auth was off (the implicit-admin window) so the operator reviews them
        # before they become live admin credentials — an unexpected one is an
        # attacker who enrolled on the open LAN.
        enabling = changes.get("auth_enabled") and not u.auth_enabled
        # all valid → commit atomically
        _pw_rotated = any(k in changes for k in
                          ("auth_password", "viewer_password", "operator_password"))
        for k, v in changes.items():
            setattr(u, k, v)
        config.save_yaml_config()
        if auth_state is not None:
            auth_state.reload(config.ui)
            # a password change invalidates every existing session, so an old
            # cookie can't outlive the rotation (the caller re-logs in)
            if _pw_rotated:
                auth_state.revoke_all_sessions()
        resp = {"status": "ok", "restart_needed": restart_needed}
        if enabling:
            _pk = getattr(getattr(app.state, "ctx", None), "passkey_store", None)
            _enrolled = _pk.list() if _pk else []
            if _enrolled:
                resp["passkeys_to_review"] = _enrolled
                audit_log.append(user=getattr(request.state, "user", "") or "-",
                                 ip=request.client.host if request.client else "-",
                                 action="login enabled",
                                 status="ok", detail={"passkeys_present": len(_enrolled)})
        return resp

    @r.get("/api/config/security")
    async def get_security_config(request: Request):
        """Get security config (IP allowlist) + the caller's own IP so the UI
        can warn before you lock yourself out."""
        return {
            "allowlist": config.security.allowlist,
            "allow_writes": config.security.allow_writes,
            "allow_nonlan_http_devices": config.security.allow_nonlan_http_devices,
            "your_ip": request.client.host if request.client else "",
        }

    @r.post("/api/config/security")
    async def update_security_config(payload: Dict = Body(...)):
        """Update the IP allowlist + the Modbus-write / non-LAN-HTTP gates."""
        import ipaddress as _ip
        if "allowlist" in payload:
            raw = payload.get("allowlist", [])
            if not isinstance(raw, list):
                raise HTTPException(status_code=422, detail={"errors": ["allowlist must be a list"]})
            cleaned, errors = [], []
            for entry in raw:
                entry = str(entry).strip()
                if not entry:
                    continue
                try:
                    _ip.ip_network(entry, strict=False)
                    cleaned.append(entry)
                except ValueError:
                    errors.append(f"invalid IP/CIDR: {entry}")
            if errors:
                raise HTTPException(status_code=422, detail={"errors": errors})
            config.security.allowlist = cleaned
        if "allow_writes" in payload:
            config.security.allow_writes = bool(payload["allow_writes"])
        if "allow_nonlan_http_devices" in payload:
            config.security.allow_nonlan_http_devices = bool(payload["allow_nonlan_http_devices"])
        config.save_yaml_config()
        return {"status": "ok", "allowlist": config.security.allowlist,
                "active": bool(config.security.allowlist),
                "allow_writes": config.security.allow_writes,
                "allow_nonlan_http_devices": config.security.allow_nonlan_http_devices}

    @r.get("/api/config/influxdb")
    async def get_influxdb_config(request: Request):
        """Get InfluxDB configuration (URL redacted for viewer + operator)."""
        _url = config.influxdb.url
        if getattr(request.state, "role", None) in ("viewer", "operator") and _url:
            from ..redact import redact_url
            _url = redact_url(str(_url))
        return {
            "enabled": config.influxdb.enabled,
            "url": _url,
            "org": config.influxdb.org,
            "bucket": config.influxdb.bucket,
            "write_interval": config.influxdb.write_interval,
            "publish_mode": config.influxdb.publish_mode,
            "default_bucket_pattern": config.influxdb.default_bucket_pattern,
        }

    @r.post("/api/config/influxdb")
    async def update_influxdb_config(update: InfluxDBConfigUpdate):
        """Update InfluxDB configuration."""
        try:
            config.update_influxdb(**update.model_dump())
            config.save_yaml_config()
            return {"status": "ok", "message": "InfluxDB config updated. Apply to reconnect."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    return r
