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
"""MQTT discovery — devices announcing themselves on the gateway broker.

Four endpoints under ``/api/mqtt-discovery`` (list / adopt / ignore / enabled)
moved VERBATIM out of create_api (P0.8b Lot G) into a ``build(ctx) -> APIRouter``
module. Bodies are byte-identical; create_api collaborators are aliased from
``ctx``. ``discovery_state`` (the MqttDiscovery instance, still built in
create_api and also on app.state.mqtt_discovery) and ``create_device`` (the
device-create path mqtt_discovery_adopt reuses) are carried on ``ctx``.
"""
from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Body, HTTPException, Request

from multibus.payload import flag


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["mqtt-discovery"])

    config = ctx.config
    registry = ctx.registry
    template_registry = ctx.template_registry
    audit_log = ctx.audit_log
    discovery_state = ctx.discovery_state
    # create_device lives in routes/devices.py (P0.8b Lot G); it is exposed on
    # ctx at that module's build() time, which may run after this one, so resolve
    # it at request time rather than binding it here.
    def create_device(*a, **k):
        return ctx.create_device(*a, **k)

    @r.get("/api/mqtt-discovery")
    def mqtt_discovery_list():
        """Devices found announcing themselves on the gateway's broker, each
        new / in_mbg / template_missing / ignored. Nothing is made by itself."""
        items = discovery_state.listing([d for d, _c in registry],
                                        lambda tid: template_registry.get(tid) is not None)
        return {"enabled": discovery_state.enabled, "connected": discovery_state.connected,
                "error": discovery_state.error, "items": items,
                "rules": [{"id": r["id"], "label": r.get("label") or r["id"], "topic": r["topic"]}
                          for r in discovery_state.rules()],
                "rule_problems": discovery_state.rule_problems, "nodes_url": config.ui.nodes_url}

    @r.post("/api/mqtt-discovery/{key}/adopt")
    def mqtt_discovery_adopt(key: str, request: Request, payload: Dict = Body(default={})):
        """Make a found device a device of the gateway's, OFF: its template and
        topics from its announcement; an id and a name may be given."""
        item = discovery_state.get(key)
        if item is None:
            raise HTTPException(status_code=404, detail="no such found device (it may have gone: reload)")
        if template_registry.get(item["template"]) is None:
            raise HTTPException(status_code=409, detail=f"template {item['template']} is not here: "
                                                        + item.get("missing_template", "add it first"))
        ids = {d.id for d, _c in registry}
        did = str(payload.get("id") or item["device_id"]).strip().lower()
        base, n = did, 2
        while did in ids:
            did, n = f"{base}-{n}", n + 1
        out = create_device({"id": did, "name": str(payload.get("name") or item["name"])[:80], "enabled": False,
                             "template": item["template"], "connection": dict(item["connection"])})
        audit_log.append(user=getattr(request.state, "user", "") or "-",
                         ip=request.client.host if request.client else "-",
                         action="mqtt device adopted", target=did, status="ok",
                         detail={"found": key, "template": item["template"]})
        return {"status": "adopted", "device_id": did, "device": out.get("device")}

    @r.post("/api/mqtt-discovery/{key}/ignore")
    def mqtt_discovery_ignore(key: str, payload: Dict = Body(default={})):
        """Hide a found device from the list (`ignore: false` shows it again)."""
        ignore = flag(payload, "ignore", True)
        discovery_state.ignore(key, ignore)
        return {"key": key, "ignored": ignore}

    @r.post("/api/mqtt-discovery/enabled")
    def mqtt_discovery_enabled(payload: Dict = Body(...)):
        """Listen for announcements or not (from the next start of the gateway)."""
        discovery_state.set_enabled(flag(payload, "enabled"))
        return {"enabled": discovery_state.enabled}

    return r
