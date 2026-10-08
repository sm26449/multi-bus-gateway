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
        """template id -> device ids using it (delete guard + UI badge) —
        the device's own template and every source's (an installation's unit
        is often read through a source that names the map)."""
        used: Dict[str, List[str]] = {}
        for dev_cfg, _c in registry:
            tids = {getattr(dev_cfg, "template", "") or ""} | {
                getattr(sx, "template", "") or "" for sx in (getattr(dev_cfg, "sources", None) or [])}
            for tid in sorted(t for t in tids if t):
                used.setdefault(tid, []).append(dev_cfg.id)
        return used

    def _pending_updates() -> Dict[str, List[Dict]]:
        """template id -> the installations / standalone devices that have
        an update from it waiting (the Templates page links to them)."""
        from ..template_refresh import plan_devices
        config = ctx.config
        out: Dict[str, List[Dict]] = {}

        def note(kind, oid, name, devs):
            try:
                plan = plan_devices(config, template_registry, devs)
            except Exception:  # noqa: BLE001 — a listing must not fail on this
                return
            if not plan["pending"]:
                return
            for t in plan["templates"]:
                out.setdefault(t["id"], []).append({"kind": kind, "id": oid, "name": name})
        for p in (getattr(config, "endpoints", None) or []):
            if p.get("id"):
                note("endpoint", p["id"], p.get("name") or p["id"], config.endpoint_devices(p["id"]))
        for dev_cfg, _c in registry:
            if not dev_cfg.primary and not getattr(dev_cfg, "endpoint_id", ""):
                note("device", dev_cfg.id, dev_cfg.name or dev_cfg.id, [dev_cfg])
        return out

    @r.get("/api/device-templates")
    def list_device_templates():
        """Template library (built-ins + user uploads) for the wizard picker."""
        used = _templates_in_use()
        pending = _pending_updates()
        out = []
        for t in template_registry.list():
            s = t.summary()
            s['used_by'] = used.get(t.id, [])
            s['pending_updates'] = pending.get(t.id, [])
            out.append(s)
        return {"templates": out, "load_errors": template_registry.load_errors,
                "load_warnings": getattr(template_registry, "load_warnings", {})}

    @r.get("/api/device-templates/{template_id}")
    def get_device_template(template_id: str):
        """Full template (preview / registers catalog for non-primary devices)."""
        t = template_registry.get(template_id)
        if t is None:
            raise HTTPException(status_code=404, detail="template not found")
        d = t.to_dict()
        d['device_template']['builtin'] = t.builtin
        return d

    def _apply_protocol_change(t, old_proto) -> List[str]:
        """The word order and read size are taken when a device's client is
        made — a changed protocol restarts the devices reading with this map."""
        if old_proto is None or dict(getattr(t, "protocol", None) or {}) == old_proto:
            return []
        restart = getattr(ctx, "restart_devices_using_template", None)
        return restart(t.id) if restart else []

    # ── checks for the editor: nothing is saved ───────────────────────────
    @r.post("/api/device-templates/check")
    def check_device_template(payload: Dict = Body(...)):
        """Every problem a save would report, without saving — the Advanced
        tabs and the raw JSON editor ask after each change."""
        from ..device_template import validate_template
        if not isinstance(payload.get("device_template"), dict):
            payload = {"device_template": payload}
        try:
            errors = validate_template(payload)
        except Exception as e:  # noqa: BLE001 — a malformed draft is an answer, not a 500
            errors = [f"cannot read the template: {e}"]
        return {"ok": not errors, "errors": errors}

    @r.post("/api/device-templates/check-expression")
    def check_template_expression(payload: Dict = Body(...)):
        """A calculated field's formula against the map being edited: syntax,
        and whether every name it reads is a register or another calculated
        field of this template. No device, no live value needed."""
        from .. import expressions
        tpl = payload.get("template") or {}
        tpl = tpl.get("device_template", tpl) if isinstance(tpl, dict) else {}
        expr = str(payload.get("expr") or "")
        me = str(payload.get("name") or "")
        regs = [r.get("name") for r in tpl.get("registers") or [] if isinstance(r, dict) and r.get("name")]
        calcs = [c.get("name") for c in tpl.get("calculated") or []
                 if isinstance(c, dict) and c.get("name") and c.get("name") != me]
        ok, err, refs = expressions.validate_expression(expr)
        if not ok:
            return {"ok": False, "error": err, "refs": []}
        known = set(regs) | set(calcs)
        base = lambda n: n.split("[", 1)[0]  # noqa: E731 — name[0] reads an array register
        unknown = [n for n in refs if n not in known and base(n) not in known and "." not in n]
        other_dev = [n for n in refs if "." in n]
        if me and me in refs:
            return {"ok": False, "error": f"'{me}' reads itself", "refs": refs}
        if unknown:
            return {"ok": False, "refs": refs, "unknown": unknown,
                    "error": "not a register or calculated field of this template: " + ", ".join(unknown)}
        return {"ok": True, "refs": refs,
                **({"warning": "reads another device (" + ", ".join(other_dev) + ") — every device made from "
                               "this template would read that same device"} if other_dev else {})}

    @r.post("/api/device-templates/identify-test")
    def identify_test(payload: Dict = Body(...)):
        """Does this draft's ``identify`` block recognise a real device? Asked
        through the device's own (shared) connection, in turn."""
        from ..bus_scan import identify
        from ..device_template import parse_template
        tpl = payload.get("template") or {}
        if not isinstance(tpl.get("device_template"), dict):
            tpl = {"device_template": tpl}
        dev_id = str(payload.get("device") or "")
        client = next((c for d, c in registry if d.id == dev_id), None)
        conn = getattr(client, "connection", None)
        if conn is None or not hasattr(conn, "lock"):
            raise HTTPException(status_code=400, detail="pick a Modbus device that is running")
        try:
            t = parse_template(tpl)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": str(e).split("\n- ")[1:] or [str(e)]})
        if not t.identify:
            raise HTTPException(status_code=422, detail={"errors": ["identify is empty — add a register or an FC43 pattern"]})
        uid = int(getattr(conn.config, "unit_id", 1) or 1)
        with conn.lock:
            if conn.client is None:
                conn.connect()
            info = identify(conn.client, uid, [t])
        matched = bool(info.get("matches"))
        return {"ok": matched, "device": dev_id, "unit_id": uid, "fc43": info.get("fc43"),
                "sunspec": info.get("sunspec"),
                "message": (f"'{dev_id}' (unit {uid}) is recognised as this template" if matched else
                            f"'{dev_id}' (unit {uid}) does not match — check each register's value "
                            f"and table, and the FC43 patterns against what it reports")}

    @r.post("/api/device-templates")
    def save_device_template(payload: Dict = Body(...)):
        """Create or update a USER template (built-in ids are shielded).
        Validation errors come back as a per-row list (422) so the editor can
        mark the exact offending rows."""
        from ..device_template import validate_template
        errors = validate_template(payload)
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        old = template_registry.get(str((payload.get("device_template") or payload).get("id") or ""))
        old_proto = dict(getattr(old, "protocol", None) or {}) if old else None
        try:
            t = template_registry.save_user(payload)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        logger.info(f"device template {t.id}: saved ({len(t.registers)} registers)")
        return {"status": "saved", "template": t.summary(),
                "restarted": _apply_protocol_change(t, old_proto)}

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

    def _own_fields_of(t) -> Dict[str, Dict]:
        """The installation's own canonical fields this template names — they
        travel with it, so it reads the same way on another gateway."""
        from ..canonical_fields import USER_FIELDS
        names = {r.name for r in (t.registers or [])} | {c.name for c in (t.calculated or [])}
        return {n: dict(USER_FIELDS[n]) for n in sorted(names) if n in USER_FIELDS}

    def _adopt_fields(data: Dict) -> Dict[str, List[str]]:
        """Create the canonical fields a template brings that this gateway
        lacks. One that exists with another definition is left as it is and
        named — the local definition holds history already."""
        from .. import canonical_fields as cf
        dt = (data or {}).get("device_template") or data or {}
        brought = dt.get("fields") or {}
        added, kept = [], []
        if not isinstance(brought, dict) or not brought:
            return {"added": added, "kept": kept}
        fields = dict(cf.USER_FIELDS)
        for name, d in brought.items():
            if name in cf.BUILTIN_FIELDS:
                continue
            if name in fields:
                if {k: fields[name].get(k) for k in ("category", "unit", "topic")} != \
                        {k: (d or {}).get(k) for k in ("category", "unit", "topic")}:
                    kept.append(name)
                continue
            if not cf.validate_user_field(name, d or {}):
                fields[name] = d
                added.append(name)
        if added:
            cf.set_user_fields(fields)
            cf.save_user_fields(ctx.config.config_path.parent / "canonical_fields_user.json")
        return {"added": added, "kept": kept}

    @r.get("/api/device-templates/{template_id}/export")
    def export_device_template(template_id: str):
        """Download the template as a JSON file (round-trips through upload).
        The installation's own canonical fields it names ride along."""
        t = template_registry.get(template_id)
        if t is None:
            raise HTTPException(status_code=404, detail="template not found")
        content = t.to_dict()
        own = _own_fields_of(t)
        if own:
            content["device_template"]["fields"] = own
        return JSONResponse(
            content=content,
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
        old_proto = dict(getattr(existing, "protocol", None) or {}) if existing else None
        adopted = _adopt_fields(data)
        try:
            t = template_registry.save_user(data)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        return {"status": "saved", "template": t.summary(),
                "restarted": _apply_protocol_change(t, old_proto), "fields": adopted}

    BYTE_ORDERS = ("big", "little", "badc", "dcba")
    TRANSPORTS = ("tcp", "rtu", "rtu-tcp", "rtu_tap", "http", "mqtt")

    def _protocol_from(payload: Dict, meta: Dict) -> Dict:
        """The map's protocol block from the import form (or the YAML's own
        header): how its 32-bit words are laid out and how it is reached. A map
        imported without it reads as ABCD over Modbus — say so in the form."""
        native = meta.get("protocol") if isinstance(meta.get("protocol"), dict) else {}
        out: Dict = dict(native)          # an exported template keeps its whole block
        bo = str(payload.get("byte_order") or meta.get("byte_order") or meta.get("word_order")
                 or native.get("byte_order") or "").strip().lower()
        alias = {"abcd": "big", "cdab": "little", "word_swap": "little"}   # "le" is ambiguous: refused
        bo = alias.get(bo, bo)
        if bo:
            if bo not in BYTE_ORDERS:
                raise HTTPException(status_code=422, detail={"errors": [
                    f"byte_order {bo!r}: use big (ABCD), little (CDAB), badc or dcba"]})
            out["byte_order"] = bo
        tr = payload.get("transports") or meta.get("transports") or native.get("transports")
        if isinstance(tr, str):
            tr = [tr]
        if tr:
            tr = [str(x).strip().lower() for x in tr if str(x).strip()]
            bad = [x for x in tr if x not in TRANSPORTS]
            if bad:
                raise HTTPException(status_code=422, detail={"errors": [
                    f"transports {bad}: use {', '.join(TRANSPORTS)}"]})
            out["transports"] = tr
        return out

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
        # bound the input — a register map is at most a few thousand short rows;
        # anything past this is a paste error or an abuse attempt, not a real map.
        if len(csv_text) > 1_000_000:
            raise HTTPException(status_code=413, detail={"errors": [
                "csv too large (max 1 MB) — a register map should be a few thousand rows"]})
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
        proto = _protocol_from(payload, {})
        if proto:
            tpl["device_template"]["protocol"] = proto
        return {
            "device_template": tpl,
            "register_count": len(parsed['registers']),
            "warnings": parsed['warnings'],
            "columns": parsed['columns'],
            "validation_errors": validate_template(tpl),
        }

    @r.post("/api/device-templates/import-yaml")
    def import_yaml_template(payload: Dict = Body(...)):
        """Convert an upstream/community YAML register map into a device-template
        PREVIEW (not saved). Richer than CSV — per-register enum/bits/thresholds/
        write envelope pass through. The UI reviews register_count/warnings/
        validation_errors, then POSTs the returned device_template to
        /api/device-templates/upload to save it."""
        from ..yaml_import import parse_yaml
        from ..device_template import validate_template
        yaml_text = str(payload.get('yaml', '') or '')
        if not yaml_text.strip():
            raise HTTPException(status_code=422, detail={"errors": ["yaml is empty"]})
        if len(yaml_text) > 2_000_000:
            raise HTTPException(status_code=413, detail={"errors": [
                "yaml too large (max 2 MB) — a register map should be a few thousand entries"]})
        parsed = parse_yaml(
            yaml_text,
            default_data_type=str(payload.get('default_data_type', 'float')),
            default_poll_group=str(payload.get('default_poll_group', '')))
        if parsed['errors']:
            raise HTTPException(status_code=422, detail={"errors": parsed['errors']})
        m = parsed.get('meta') or {}
        tpl = {"device_template": {
            "id": str(payload.get('id') or m.get('id') or 'imported_device').strip(),
            "name": str(payload.get('name') or m.get('name') or m.get('title') or 'Imported device').strip(),
            "vendor": str(payload.get('vendor') or m.get('vendor') or ''),
            "model": str(payload.get('model') or m.get('model') or ''),
            "source_document": "YAML import",
            "registers": parsed['registers'],
        }}
        proto = _protocol_from(payload, m)
        if proto:
            tpl["device_template"]["protocol"] = proto
        return {
            "device_template": tpl,
            "register_count": len(parsed['registers']),
            "warnings": parsed['warnings'],
            "validation_errors": validate_template(tpl),
        }

    return r
