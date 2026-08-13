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
"""Virtual meters — instances, templates (editor/import/export), observability.

Moved verbatim from create_api(). Everything goes through the manager on
``app.state.vmeter_manager`` (set at boot by main.py), read per-request so the
routes degrade to 503 when virtual meters aren't initialized.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["virtual-meters"])

    def _mgr():
        return getattr(ctx.app.state, "vmeter_manager", None)

    @r.get("/api/virtual-meters")
    async def list_virtual_meters():
        """Configured virtual meters + live running status + served values."""
        mgr = _mgr()
        if mgr is None:
            return {"instances": []}
        return {"instances": mgr.overview(), "port_range": mgr.port_info()}

    @r.get("/api/virtual-meters/templates")
    async def list_vm_templates():
        """Available meter templates (for the 'add instance' dropdown)."""
        mgr = _mgr()
        return {"templates": mgr.list_templates() if mgr else []}

    @r.post("/api/virtual-meters")
    async def add_virtual_meter(payload: dict = Body(...)):
        """Add a new virtual-meter instance from a template."""
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        try:
            res = mgr.add_instance(
                template_id=payload["template"], port=int(payload["port"]),
                unit_id=int(payload.get("unit_id", 1)),
                stale_after_s=float(payload.get("stale_after_s", 15)),
                enabled=bool(payload.get("enabled", False)),
                device=str(payload.get("device", "")),
                device_fallback=str(payload.get("device_fallback", "")),
                on_stale=str(payload.get("on_stale", "legacy")),
                max_hold_s=float(payload.get("max_hold_s", 30)),
                quality_block=bool(payload.get("quality_block", False)))
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"bad payload: {e}")
        if "error" in res:
            raise HTTPException(status_code=400, detail=res["error"])
        return res

    @r.delete("/api/virtual-meters/{template}")
    async def delete_virtual_meter(template: str):
        """Remove a virtual-meter instance."""
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.remove_instance(template)
        if "error" in res:
            raise HTTPException(status_code=404, detail=res["error"])
        return res

    @r.get("/api/virtual-meters/sources")
    async def vm_sources(device: str = Query(default="")):
        """Live registers of a source device (for the editor source picker) +
        valid types. Absent device → the primary device's registers."""
        mgr = _mgr()
        return {"sources": mgr.list_sources(device) if mgr else [],
                "types": mgr.valid_types() if mgr else [],
                "port_range": mgr.port_info() if mgr else None}

    @r.get("/api/virtual-meters/template/{template_id}")
    async def vm_get_template(template_id: str):
        """Full editor view of a template (per-register fields)."""
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.get_template(template_id)
        if "error" in res:
            raise HTTPException(status_code=404 if "unknown" in res["error"] else 400,
                                detail=res["error"])
        return res

    @r.put("/api/virtual-meters/template/{template_id}")
    async def vm_save_template(template_id: str, payload: dict = Body(...)):
        """Create or overwrite a template from the editor."""
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.save_template(template_id, payload)
        if "error" in res:
            raise HTTPException(status_code=400, detail=res["error"])
        return res

    @r.delete("/api/virtual-meters/template/{template_id}")
    async def vm_delete_template(template_id: str):
        """Delete a template file (refused while an instance uses it)."""
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.delete_template(template_id)
        if "error" in res:
            code = (404 if "unknown" in res["error"]
                    else 409 if "in use" in res["error"] else 400)
            raise HTTPException(status_code=code, detail=res["error"])
        return res

    @r.post("/api/virtual-meters/templates/import")
    async def vm_import_template(payload: dict = Body(...)):
        """Import a template from uploaded YAML (validated before save)."""
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.import_template(str(payload.get("yaml", "")), bool(payload.get("overwrite", False)))
        if "error" in res:
            raise HTTPException(status_code=409 if res.get("exists") else 400, detail=res["error"])
        return res

    @r.get("/api/virtual-meters/template/{template_id}/export")
    async def vm_export_template(template_id: str):
        """Export a template's raw YAML (for download / sharing)."""
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.export_template(template_id)
        if "error" in res:
            raise HTTPException(status_code=404, detail=res["error"])
        return res

    @r.get("/api/virtual-meters/{template}/values")
    async def vm_json_view(template: str):
        """The meter's map as JSON, under the aggregator staleness convention:
        value null + quality good/stale/missing + age_s per row; a stale row
        carries last_value SEPARATELY (absence is never served as a number)."""
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.json_view(template)
        if "error" in res:
            raise HTTPException(status_code=404, detail=res["error"])
        return res

    @r.get("/api/virtual-meters/{template}/stats")
    async def vm_stats(template: str, limit: int = Query(200, ge=1, le=1024)):
        """Live observability: query log (last 1024), counters, rate, per-register."""
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        return mgr.get_stats(template, limit)

    @r.get("/api/virtual-meters/{template}/decode")
    async def vm_decode(template: str, addr: int = Query(...), count: int = Query(1, ge=1, le=125)):
        """Decode a register range -> values + the source variable each maps to."""
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.decode_range(template, addr, count)
        if "error" in res:
            raise HTTPException(status_code=404, detail=res["error"])
        return res

    @r.post("/api/virtual-meters/{template}/toggle")
    async def toggle_virtual_meter(template: str, on: bool = Query(True)):
        """Enable/disable a virtual meter (persists + starts/stops live)."""
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.set_enabled(template, on)
        if "error" in res:
            raise HTTPException(status_code=404, detail=res["error"])
        return res

    @r.patch("/api/virtual-meters/{template}")
    async def edit_virtual_meter(template: str, payload: dict = Body(...)):
        """Edit an existing instance (port / unit_id / stale_after_s /
        update_interval_s — partial). Restarts the meter live if running."""
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(status_code=503, detail="virtual meters not initialized")
        res = mgr.update_instance(
            template_id=template,
            port=payload.get("port"), unit_id=payload.get("unit_id"),
            stale_after_s=payload.get("stale_after_s"),
            update_interval_s=payload.get("update_interval_s"),
            device=payload.get("device"),
            device_fallback=payload.get("device_fallback"),
            on_stale=payload.get("on_stale"),
            max_hold_s=payload.get("max_hold_s"),
            quality_block=payload.get("quality_block"))
        if "error" in res:
            code = 404 if "no instance" in res["error"] else 400
            raise HTTPException(status_code=code, detail=res["error"])
        return res

    return r
