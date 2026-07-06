"""Device templates — the library (built-ins + user uploads) behind the wizard.

Moved verbatim from create_api().
"""
from __future__ import annotations

import logging
from typing import Dict, List

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["device-templates"])
    template_registry, registry = ctx.template_registry, ctx.registry

    def _templates_in_use() -> Dict[str, List[str]]:
        """template id -> device ids using it (delete guard + UI badge)."""
        used: Dict[str, List[str]] = {}
        for dev_cfg, _c in registry:
            if dev_cfg.template:
                used.setdefault(dev_cfg.template, []).append(dev_cfg.id)
        return used

    @r.get("/api/device-templates")
    def list_device_templates():
        """Template library (built-ins + user uploads) for the wizard picker."""
        used = _templates_in_use()
        out = []
        for t in template_registry.list():
            s = t.summary()
            s['used_by'] = used.get(t.id, [])
            out.append(s)
        return {"templates": out, "load_errors": template_registry.load_errors}

    @r.get("/api/device-templates/{template_id}")
    def get_device_template(template_id: str):
        """Full template (preview / registers catalog for non-primary devices)."""
        t = template_registry.get(template_id)
        if t is None:
            raise HTTPException(status_code=404, detail="template not found")
        d = t.to_dict()
        d['device_template']['builtin'] = t.builtin
        return d

    @r.post("/api/device-templates")
    def save_device_template(payload: Dict = Body(...)):
        """Create or update a USER template (built-in ids are shielded).
        Validation errors come back as a per-row list (422) so the editor can
        mark the exact offending rows."""
        from ..device_template import validate_template
        errors = validate_template(payload)
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        try:
            t = template_registry.save_user(payload)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        logger.info(f"device template {t.id}: saved ({len(t.registers)} registers)")
        return {"status": "saved", "template": t.summary()}

    @r.delete("/api/device-templates/{template_id}")
    def delete_device_template(template_id: str):
        """Delete a USER template. Blocked while any device uses it."""
        used = _templates_in_use().get(template_id)
        if used:
            raise HTTPException(status_code=422, detail={"errors": [
                f"template is in use by device(s): {', '.join(used)} — "
                f"reassign or delete those devices first"]})
        try:
            template_registry.delete_user(template_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="template not found")
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        return {"status": "deleted"}

    @r.get("/api/device-templates/{template_id}/export")
    def export_device_template(template_id: str):
        """Download the template as a JSON file (round-trips through upload)."""
        t = template_registry.get(template_id)
        if t is None:
            raise HTTPException(status_code=404, detail="template not found")
        return JSONResponse(
            content=t.to_dict(),
            headers={"Content-Disposition":
                     f'attachment; filename="{template_id}.json"'})

    @r.post("/api/device-templates/upload")
    def upload_device_template(payload: Dict = Body(...)):
        """Upload = the same validated save, but NEVER overwrites an existing
        id silently: pass ?overwrite=true semantics via payload flag."""
        data = payload.get('template') or payload
        overwrite = bool(payload.get('overwrite', False))
        from ..device_template import validate_template
        errors = validate_template(data)
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        tid = data['device_template']['id']
        existing = template_registry.get(tid)
        if existing and not overwrite:
            kind = "built-in" if existing.builtin else "existing"
            raise HTTPException(status_code=409, detail={
                "errors": [f"a template with id '{tid}' already exists ({kind})"],
                "conflict": tid, "builtin": existing.builtin})
        try:
            t = template_registry.save_user(data)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        return {"status": "saved", "template": t.summary()}

    @r.post("/api/device-templates/import-csv")
    def import_csv_template(payload: Dict = Body(...)):
        """Convert a CSV register map into a device-template PREVIEW (not saved).
        The UI reviews register_count/warnings/validation_errors, then POSTs the
        returned device_template to /api/device-templates/upload to save it."""
        from ..csv_import import parse_csv
        from ..device_template import validate_template
        csv_text = str(payload.get('csv', '') or '')
        if not csv_text.strip():
            raise HTTPException(status_code=422, detail={"errors": ["csv is empty"]})
        parsed = parse_csv(
            csv_text,
            default_data_type=str(payload.get('default_data_type', 'float')),
            default_poll_group=str(payload.get('default_poll_group', '')))
        if parsed['errors']:
            raise HTTPException(status_code=422, detail={"errors": parsed['errors']})
        tpl = {"device_template": {
            "id": str(payload.get('id', '') or 'imported_device').strip(),
            "name": str(payload.get('name', '') or 'Imported device').strip(),
            "vendor": str(payload.get('vendor', '') or ''),
            "model": str(payload.get('model', '') or ''),
            "source_document": "CSV import",
            "registers": parsed['registers'],
        }}
        return {
            "device_template": tpl,
            "register_count": len(parsed['registers']),
            "warnings": parsed['warnings'],
            "columns": parsed['columns'],
            "validation_errors": validate_template(tpl),
        }

    return r
