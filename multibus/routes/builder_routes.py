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
"""Device Builder — drive an external ESPHome dashboard from the gateway UI.

The gateway is the operator's single pane of glass: these routes proxy the
ESPHome dashboard API (node list, YAML read/write, compile/upload streams,
artifact downloads) so the browser only ever talks to us — one login, one
audit trail, one origin. ESPHome does the actual compiling; see
multibus/esphome_client.py for the verified API surface.

Security model:
  * feature is OFF unless `esphome.enabled` + `esphome.url` are configured;
  * node YAML often embeds Wi-Fi credentials → reading/writing any YAML and
    every command stream is ADMIN-only while auth is enabled (viewers keep
    the node list, which carries no secrets);
  * the write-blocking role middleware still applies on top (viewer/operator
    cannot POST/PUT/DELETE anything here);
  * every state-changing call and every command stream lands in the audit log.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Dict

from fastapi import APIRouter, Body, HTTPException, Request, WebSocket
from fastapi.responses import Response
from starlette.websockets import WebSocketDisconnect

from ..esphome_client import (WS_COMMANDS, EsphomeDashboard, EsphomeError,
                              from_config)

logger = logging.getLogger(__name__)

# node YAML filename: no paths, no dotfiles, .yaml/.yml only (secrets.yaml is
# a legitimate target — the dashboard treats it specially but it lives in the
# same directory).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\.(yaml|yml)$")
_MAX_YAML = 512 * 1024

# one client per (url, username, password, timeout) so the dashboard-login
# cookie is reused instead of re-authenticating every request
_clients: Dict[tuple, EsphomeDashboard] = {}

# /api/builder/status is polled by the Status page every few seconds; the
# answer needs two dashboard round-trips, so it is cached briefly. Keyed by
# URL; cleared on settings save.
_STATUS_TTL_S = 15.0
_status_cache: Dict[str, tuple] = {}      # url -> (monotonic_ts, payload)


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["builder"])
    config, audit_log, auth_state = ctx.config, ctx.audit_log, ctx.auth_state

    # ---- helpers -------------------------------------------------------------

    def _cfg() -> Dict:
        return getattr(config, "esphome", {}) or {}

    def _enabled() -> bool:
        c = _cfg()
        return bool(c.get("enabled")) and bool(c.get("url"))

    def _client() -> EsphomeDashboard:
        c = _cfg()
        if not _enabled():
            raise HTTPException(status_code=503, detail=(
                "ESPHome integration is not configured — set esphome.enabled "
                "and esphome.url in Settings"))
        key = (c.get("url"), c.get("username"), c.get("password"),
               c.get("timeout_s"))
        cli = _clients.get(key)
        if cli is None:
            try:
                cli = from_config(c)
            except EsphomeError as e:
                raise HTTPException(status_code=503, detail=str(e))
            _clients.clear()          # config changed → drop stale sessions
            _clients[key] = cli
        return cli

    def _check_name(name: str) -> str:
        if not _NAME_RE.match(name or "") or ".." in name:
            raise HTTPException(status_code=422, detail=(
                "invalid node filename (expected e.g. my-node.yaml)"))
        return name

    def _require_admin(request: Request):
        """YAML content can embed Wi-Fi/OTA secrets: admin-only under auth."""
        if auth_state.enabled and getattr(request.state, "role", None) != "admin":
            raise HTTPException(status_code=403, detail=(
                "node configuration access requires the admin role"))

    def _audit(request: Request, action: str, target: str, status: str = "ok",
               detail=None):
        audit_log.append(
            user=getattr(request.state, "user", "") or "-",
            ip=request.client.host if request.client else "-",
            action=action, target=target, status=status, detail=detail)

    def _wrap(fn):
        """Map EsphomeError to a 502 with the friendly message."""
        try:
            return fn()
        except EsphomeError as e:
            raise HTTPException(status_code=502, detail=str(e))

    # ---- settings ---------------------------------------------------------------

    @r.get("/api/builder/settings")
    def get_builder_settings():
        """The esphome: block with the password redacted (UI settings form)."""
        c = _cfg()
        return {"enabled": bool(c.get("enabled")),
                "url": str(c.get("url", "") or ""),
                "username": str(c.get("username", "") or ""),
                "password_set": bool(c.get("password")),
                "timeout_s": float(c.get("timeout_s", 10) or 10)}

    @r.post("/api/builder/settings")
    def save_builder_settings(request: Request, payload: Dict = Body(...)):
        """Persist the esphome: block. Empty password = keep the stored one."""
        url = str(payload.get("url", "") or "").strip()
        enabled = bool(payload.get("enabled"))
        if enabled:
            from urllib.parse import urlparse
            u = urlparse(url)
            if u.scheme not in ("http", "https") or not u.netloc:
                raise HTTPException(status_code=422, detail={"errors": [
                    "url must be http(s)://host:port (e.g. http://esphome:6052)"]})
        try:
            timeout_s = float(payload.get("timeout_s", 10))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail={"errors": [
                "timeout_s must be a number"]})
        if not 1 <= timeout_s <= 120:
            raise HTTPException(status_code=422, detail={"errors": [
                "timeout_s must be between 1 and 120"]})
        old = _cfg()
        password = str(payload.get("password", "") or "") or str(
            old.get("password", "") or "")
        config.esphome = {
            "enabled": enabled, "url": url,
            "username": str(payload.get("username", "") or ""),
            "password": password, "timeout_s": timeout_s}
        config.save_yaml_config()
        _clients.clear()               # drop cached sessions on any change
        _status_cache.clear()          # and the cached reachability verdict
        _audit(request, "esphome settings save", url,
               detail={"enabled": enabled})
        return {"status": "ok", "applies": "live"}

    # ---- status / list ---------------------------------------------------------

    @r.get("/api/builder/status")
    def builder_status():
        """Feature switch + dashboard reachability + node count. Cached for
        a few seconds — the Status page polls this alongside /api/status and
        must never pay dashboard round-trips per refresh."""
        if not _enabled():
            return {"enabled": False, "reachable": False, "version": "",
                    "url": "", "node_count": 0}
        cli = _client()
        import time
        hit = _status_cache.get(cli.base)
        if hit and time.monotonic() - hit[0] < _STATUS_TTL_S:
            return hit[1]
        try:
            payload = {"enabled": True, "reachable": True,
                       "version": cli.version(),
                       "node_count": len(cli.devices()["configured"]),
                       "url": cli.base}
        except EsphomeError as e:
            payload = {"enabled": True, "reachable": False, "version": "",
                       "node_count": 0, "url": cli.base, "error": str(e)}
        _status_cache[cli.base] = (time.monotonic(), payload)
        return payload

    @r.get("/api/builder/nodes")
    def list_nodes():
        """Node list (configured + mDNS-importable). No YAML content here,
        so it stays readable for every role."""
        return _wrap(lambda: _client().devices())

    # ---- YAML CRUD --------------------------------------------------------------

    @r.get("/api/builder/nodes/{name}/config")
    def get_node_config(name: str, request: Request):
        _require_admin(request)
        _check_name(name)
        content = _wrap(lambda: _client().get_config(name))
        return Response(content=content, media_type="application/yaml")

    @r.put("/api/builder/nodes/{name}/config")
    def save_node_config(name: str, request: Request, payload: Dict = Body(...)):
        """Save (create or overwrite) one node YAML. `content` is the raw
        YAML text; `overwrite` must be true to replace an existing node."""
        _require_admin(request)
        _check_name(name)
        content = str(payload.get("content", "") or "")
        if not content.strip():
            raise HTTPException(status_code=422, detail="YAML content is empty")
        if len(content.encode("utf-8")) > _MAX_YAML:
            raise HTTPException(status_code=422, detail="YAML too large (max 512 KB)")
        cli = _client()
        if not payload.get("overwrite"):
            try:
                cli.get_config(name)
                raise HTTPException(status_code=409, detail=(
                    f"{name} already exists — pass overwrite:true to replace it"))
            except EsphomeError:
                pass                       # not found → create is fine
        _wrap(lambda: cli.save_config(name, content))
        _status_cache.clear()          # node_count may have changed
        _audit(request, "esphome yaml save", name,
               detail={"bytes": len(content)})
        return {"status": "saved", "name": name}

    @r.delete("/api/builder/nodes/{name}")
    def delete_node(name: str, request: Request):
        """Archive on the dashboard (recoverable there under archive/)."""
        _require_admin(request)
        _check_name(name)
        _wrap(lambda: _client().archive(name))
        _status_cache.clear()          # node_count changed
        _audit(request, "esphome node archive", name)
        return {"status": "archived", "name": name}

    # ---- build artifacts ---------------------------------------------------------

    @r.get("/api/builder/nodes/{name}/downloads")
    def list_downloads(name: str, request: Request):
        _require_admin(request)
        _check_name(name)
        return {"downloads": _wrap(lambda: _client().downloads(name))}

    @r.get("/api/builder/nodes/{name}/download")
    def download_artifact(name: str, request: Request, file: str,
                          download: str = ""):
        _require_admin(request)
        _check_name(name)
        data, fname = _wrap(lambda: _client().download_bin(name, file, download))
        _audit(request, "esphome binary download", f"{name}:{file}",
               detail={"bytes": len(data)})
        return Response(
            content=data, media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    # ---- web flasher manifest (esp-web-tools, vendored locally) ------------------

    _CHIP_FAMILIES = {
        "esp8266": "ESP8266", "esp32": "ESP32", "esp32s2": "ESP32-S2",
        "esp32s3": "ESP32-S3", "esp32c3": "ESP32-C3", "esp32c6": "ESP32-C6",
        "esp32h2": "ESP32-H2", "esp32c2": "ESP32-C2", "esp32p4": "ESP32-P4",
    }

    @r.get("/api/builder/nodes/{name}/manifest")
    def flash_manifest(name: str, request: Request):
        """esp-web-tools manifest for the browser USB flasher. The part path
        is RELATIVE, so it resolves to this node's own download endpoint."""
        _require_admin(request)
        _check_name(name)
        cli = _client()
        entry = next((d for d in _wrap(cli.devices)["configured"]
                      if d.get("configuration") == name), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="node not found")
        plat = re.sub(r"[^a-z0-9]", "",
                      str(entry.get("target_platform", "") or "").lower())
        chip = _CHIP_FAMILIES.get(plat)
        if chip is None:
            raise HTTPException(status_code=422, detail=(
                f"unsupported platform {entry.get('target_platform')!r} — "
                "build the node first, then retry"))
        return {
            "name": entry.get("friendly_name") or entry.get("name") or name,
            "version": str(entry.get("current_version", "") or ""),
            "new_install_prompt_erase": True,
            "builds": [{"chipFamily": chip, "parts": [
                {"path": "download?file=firmware.factory.bin", "offset": 0}]}],
        }

    # ---- hardware profiles (shareable board/pin presets for the wizard) ----------

    _BUILTIN_PROFILES = [
        {"id": "esp32-generic-uart2", "name": "ESP32 generic — RS485 on UART2",
         "builtin": True, "platform": "esp32", "board": "esp32dev",
         "tx_pin": "GPIO17", "rx_pin": "GPIO16", "flow_control_pin": "",
         "baud_rate": 9600, "parity": "NONE", "stop_bits": 1},
        {"id": "esp8266-d1mini", "name": "ESP8266 D1 mini — RS485 on UART0",
         "builtin": True, "platform": "esp8266", "board": "d1_mini",
         "tx_pin": "GPIO1", "rx_pin": "GPIO3", "flow_control_pin": "",
         "baud_rate": 9600, "parity": "NONE", "stop_bits": 1},
    ]

    def _profiles_path():
        return config.config_path.parent / "builder_profiles.json"

    def _load_profiles() -> list:
        import json
        p = _profiles_path()
        if not p.exists():
            return []
        try:
            return list(json.loads(p.read_text(encoding="utf-8")) or [])
        except (OSError, ValueError):
            logger.warning("builder_profiles.json unreadable — starting empty")
            return []

    def _save_profiles(items: list) -> None:
        import json
        import os
        p = _profiles_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, p)

    @r.get("/api/builder/profiles")
    def list_profiles():
        return {"profiles": _BUILTIN_PROFILES + _load_profiles()}

    @r.post("/api/builder/profiles")
    def save_profile(request: Request, payload: Dict = Body(...)):
        pid = re.sub(r"[^a-z0-9-]", "-", str(payload.get("id", "") or
                                             payload.get("name", "")).lower()).strip("-")
        if not pid or any(b["id"] == pid for b in _BUILTIN_PROFILES):
            raise HTTPException(status_code=422, detail={"errors": [
                "profile id/name missing or shadows a built-in"]})
        prof = {"id": pid, "builtin": False}
        for k, dflt in (("name", pid), ("platform", "esp32"), ("board", ""),
                        ("tx_pin", ""), ("rx_pin", ""), ("flow_control_pin", ""),
                        ("baud_rate", 9600), ("parity", "NONE"), ("stop_bits", 1)):
            prof[k] = payload.get(k, dflt)
        items = [x for x in _load_profiles() if x.get("id") != pid] + [prof]
        _save_profiles(items)
        _audit(request, "builder profile save", pid)
        return {"status": "saved", "profile": prof}

    @r.delete("/api/builder/profiles/{pid}")
    def delete_profile(pid: str, request: Request):
        items = _load_profiles()
        keep = [x for x in items if x.get("id") != pid]
        if len(keep) == len(items):
            raise HTTPException(status_code=404, detail="profile not found "
                                "(built-ins cannot be deleted)")
        _save_profiles(keep)
        _audit(request, "builder profile delete", pid)
        return {"status": "deleted"}

    # ---- generator (template → firmware YAML + paired device) --------------------

    @r.post("/api/builder/generate")
    def generate_node_yaml(request: Request, payload: Dict = Body(...)):
        """Transpile a gateway device template (register subset) into ESPHome
        node YAML + the PAIRED gateway artifacts (user template & device
        payload with byte-identical topics). Pure preview — saves nothing;
        the UI chains the existing save endpoints on 'Adopt'."""
        _require_admin(request)
        from ..esphome_generator import generate_node
        tpl_id = str(payload.get("template_id", "") or "")
        tpl = ctx.template_registry.get(tpl_id)
        if tpl is None:
            raise HTTPException(status_code=404, detail="template not found")
        from ..device_template import template_transport
        if template_transport(tpl) != "modbus":
            raise HTTPException(status_code=422, detail={"errors": [
                "only Modbus device templates can be transpiled to an RS485 "
                "node (MQTT/HTTP templates already reach the gateway directly)"]})
        mqtt_defaults = {
            "broker": config.mqtt.broker if config.mqtt.enabled else "",
            "port": config.mqtt.port, "username": config.mqtt.username,
            "password": config.mqtt.password,
        }
        try:
            out = generate_node(payload, tpl, config.poll_groups, mqtt_defaults)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        return out

    @r.post("/api/builder/secrets/ensure")
    def ensure_secrets(request: Request, payload: Dict = Body(...)):
        """Append MISSING keys to the dashboard's secrets.yaml (existing lines
        are never rewritten). Values may be null → 'CHANGE_ME' placeholder."""
        _require_admin(request)
        from ..esphome_generator import merge_secrets
        needed = payload.get("keys") or {}
        if not isinstance(needed, dict) or not needed:
            raise HTTPException(status_code=422, detail={"errors": ["keys{} required"]})
        if any(not re.match(r"^[A-Za-z0-9_]{1,64}$", k) for k in needed):
            raise HTTPException(status_code=422, detail={"errors": ["invalid key name"]})
        cli = _client()
        try:
            existing = cli.get_config("secrets.yaml")
        except EsphomeError:
            existing = ""
        merged, added = merge_secrets(existing, {
            k: (str(v) if v is not None else None) for k, v in needed.items()})
        if added:
            _wrap(lambda: cli.save_config("secrets.yaml", merged))
        _audit(request, "esphome secrets ensure", "secrets.yaml",
               detail={"added": added})       # key NAMES only — never values
        return {"status": "ok", "added": added}

    # ---- command stream (compile / validate / upload / run / logs / clean) -------

    @r.websocket("/api/builder/stream/{command}")
    async def builder_stream(websocket: WebSocket, command: str):
        """Proxy one dashboard command; relays {"event":"line"/"exit"} frames.

        Query params: configuration=<name.yaml>, port=<OTA|/dev/...> (upload/
        run/logs only). The HTTP middlewares don't cover WS scopes, so the IP
        allowlist + admin gate are enforced here, like /ws does.
        """
        peer = websocket.client.host if websocket.client else ""
        ip_allowed = getattr(ctx, "ip_allowed", None)
        if ip_allowed is not None and not ip_allowed(peer):
            await websocket.close(code=1008)
            return
        user = "-"
        if auth_state.enabled:
            from .. import auth as _auth
            token = websocket.cookies.get(_auth.COOKIE_NAME, "")
            if auth_state.role_for(token) != "admin":
                await websocket.close(code=1008)
                return
            ident = auth_state.identity_for(token)
            user = ident[1] if ident else "admin"
        origin = websocket.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            host = websocket.headers.get("host", "")
            if urlparse(origin).netloc != host:
                await websocket.close(code=1008)
                return

        name = websocket.query_params.get("configuration", "")
        port = websocket.query_params.get("port", "OTA")
        if command == "update-all":
            name = ""                       # fleet-wide: no configuration
        elif not _NAME_RE.match(name) or ".." in name:
            await websocket.close(code=1008)
            return
        if command not in WS_COMMANDS:
            await websocket.close(code=1008)
            return
        if not _enabled():
            await websocket.close(code=1013)   # try again later (not configured)
            return
        try:
            cli = _client()
        except HTTPException:
            await websocket.close(code=1013)
            return

        await websocket.accept()
        audit_log.append(user=user, ip=peer, action=f"esphome {command}",
                         target=name, status="start")
        exit_code = None

        async def _pump():
            nonlocal exit_code
            async for msg in cli.stream_command(command, name, port=port):
                await websocket.send_json(msg)
                if msg.get("event") == "exit":
                    exit_code = msg.get("code")

        async def _watch_client():
            # returns when the browser goes away (raises WebSocketDisconnect)
            while True:
                await websocket.receive_text()

        pump = asyncio.create_task(_pump())
        watch = asyncio.create_task(_watch_client())
        try:
            done, pending = await asyncio.wait(
                {pump, watch}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, WebSocketDisconnect):
                    raise exc
        except EsphomeError as e:
            try:
                await websocket.send_json({"event": "error", "data": str(e)})
            except Exception:  # noqa: BLE001 — browser already gone
                pass
        except Exception as e:  # noqa: BLE001
            logger.error(f"builder stream {command} {name}: {e}")
        finally:
            audit_log.append(
                user=user, ip=peer, action=f"esphome {command}", target=name,
                status="ok" if exit_code == 0 else f"exit={exit_code}")
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    return r
