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
"""Dependents — what uses a device or installation, and the counts in one pass.

Three endpoints (GET /api/devices/{id}/dependents, GET /api/endpoints/{id}/
dependents, GET /api/dependents) moved VERBATIM out of create_api (P0.8b Lot G)
into a ``build(ctx) -> APIRouter`` module. Bodies are byte-identical; the
dependents resolver (``_dependents``) and the source snapshot (``_dep_sources``)
come from ctx.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["dependents"])

    config = ctx.config
    _dependents = ctx.dependents_of
    _dep_sources = ctx.dep_sources

    @r.get("/api/devices/{device_id}/dependents")
    def device_dependents(device_id: str):
        """What uses this device (blocking) and what goes with it (follows)."""
        if config.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="device not found")
        return _dependents('device', device_id)

    @r.get("/api/endpoints/{endpoint_id}/dependents")
    def endpoint_dependents(endpoint_id: str):
        """What uses this installation or one of its units."""
        if config.get_raw_endpoint(endpoint_id) is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        return _dependents('installation', endpoint_id)

    @r.get("/api/dependents")
    def dependents_counts():
        """How many things use each device and installation, in one pass: a
        list shows "used by N" without asking each one."""
        from multibus.lifecycle.dependents import Unreadable, dependents_index
        try:
            return {"counts": dependents_index(_dep_sources())}
        except Unreadable as e:
            raise HTTPException(status_code=503, detail={"errors": [str(e)]})

    return r
