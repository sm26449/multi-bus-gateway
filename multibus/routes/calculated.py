"""Calculated registers — presets/functions/templates + per-device CRUD/test.

Moved verbatim from create_api() (routes only; the runtime engine lives in
janitza/calc_engine.py)."""
from __future__ import annotations

import re
from typing import Dict

from fastapi import APIRouter, Body, HTTPException

from .. import expressions

_CALC_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_\[\]]*$')


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["calculated"])
    config, registry, calc_engine = ctx.config, ctx.registry, ctx.calc_engine

    @r.get("/api/calculated/presets")
    async def calculated_presets():
        """Built-in parameterized formula presets for the UI builder."""
        return {"presets": expressions.PRESETS}

    @r.get("/api/calculated/functions")
    async def calculated_functions():
        """Allowed functions + operators, for the expression helper."""
        return {"functions": expressions.FUNCTIONS, "operators": expressions.OPERATORS}

    @r.get("/api/calculated/templates")
    async def get_calc_templates():
        """The user's saved reusable calculated presets."""
        return {"templates": config.load_calculated_templates()}

    @r.post("/api/calculated/templates")
    def save_calc_template(payload: Dict = Body(...)):
        """Save (or replace) a reusable calculated preset from a formula."""
        name = str(payload.get('name', '')).strip()
        expr = str(payload.get('expr', '')).strip()
        if not name:
            raise HTTPException(status_code=422, detail={"errors": ["name is required"]})
        ok, err, _refs = expressions.validate_expression(expr)
        if not ok:
            raise HTTPException(status_code=422, detail={"errors": [err]})
        tid = str(payload.get('id', '') or '').strip().lower()
        if not tid:
            tid = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'preset'
        dec = payload.get('decimals')
        tpl = {"id": tid, "name": name, "label": str(payload.get('label', '') or name),
               "unit": str(payload.get('unit', '')), "expr": expr,
               "decimals": int(dec) if isinstance(dec, int) and not isinstance(dec, bool) else None}
        tpls = [t for t in config.load_calculated_templates() if t.get('id') != tid]
        tpls.append(tpl)
        config.save_calculated_templates(tpls)
        return {"status": "ok", "id": tid, "templates": tpls}

    @r.delete("/api/calculated/templates/{tid}")
    def delete_calc_template(tid: str):
        tpls = [t for t in config.load_calculated_templates() if t.get('id') != tid]
        config.save_calculated_templates(tpls)
        return {"status": "ok", "templates": tpls}

    @r.get("/api/devices/{device_id}/calculated")
    async def get_calculated(device_id: str):
        if config.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="device not found")
        return {"calculated": config.load_calculated(device_id)}

    @r.post("/api/devices/{device_id}/calculated")
    def set_calculated(device_id: str, payload: Dict = Body(...)):
        """Replace a device's calculated registers. Every expression is validated
        (safe AST) before it is saved; the runtime cache is refreshed in place."""
        if config.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="device not found")
        items = payload.get('calculated', [])
        if not isinstance(items, list):
            raise HTTPException(status_code=422, detail={"errors": ["'calculated' must be a list"]})
        errors, clean, seen = [], [], set()
        for i, it in enumerate(items):
            name = str(it.get('name', '')).strip()
            expr = str(it.get('expr', '')).strip()
            if not name:
                errors.append(f"#{i+1}: name is required")
            elif not _CALC_NAME_RE.match(name):
                errors.append(f"#{i+1}: name '{name}' — letters, digits, _ and [] only")
            elif name in seen:
                errors.append(f"#{i+1}: duplicate name '{name}'")
            seen.add(name)
            ok, err, _refs = expressions.validate_expression(expr)
            if not ok:
                errors.append(f"#{i+1} ({name or '?'}): {err}")
            dec = it.get('decimals')
            clean.append({
                'name': name, 'label': str(it.get('label', '') or name),
                'unit': str(it.get('unit', '')), 'expr': expr,
                'poll_group': str(it.get('poll_group', '') or 'normal'),
                'decimals': int(dec) if isinstance(dec, int) and not isinstance(dec, bool) else None,
            })
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        config.save_calculated(device_id, clean)
        calc_engine.load(device_id)               # refresh runtime cache in place
        return {"status": "ok", "calculated": clean, "count": len(clean)}

    @r.post("/api/devices/{device_id}/calculated/test")
    def test_calculated(device_id: str, payload: Dict = Body(...)):
        """Evaluate an expression against the device's CURRENT live values — the
        UI's live preview. Returns the value or a helpful error + missing refs."""
        dev = config.get_device(device_id)
        if dev is None:
            raise HTTPException(status_code=404, detail="device not found")
        expr = str(payload.get('expr', '')).strip()
        ok, err, refs = expressions.validate_expression(expr)
        if not ok:
            return {"ok": False, "error": err, "refs": [], "missing": []}
        store = registry.store_for(device_id) or {}
        resolve = calc_engine.resolver(store)
        missing = [r for r in refs if resolve(r) is None]
        # Stateful formulas (prev()/dt) can't be previewed one-shot — there is no
        # history yet. Report it so the UI shows a note instead of a fake value.
        if re.search(r'\bprev\s*\(', expr) or re.search(r'\bdt\b', expr):
            return {"ok": True, "stateful": True, "refs": refs, "missing": missing}
        try:
            val = expressions.evaluate(expr, resolve)
        except expressions.MissingValue as e:
            return {"ok": False, "error": f"no live value for '{e}'", "refs": refs, "missing": missing}
        except expressions.ExpressionError as e:
            return {"ok": False, "error": str(e), "refs": refs, "missing": missing}
        if isinstance(val, bool):
            val = 1 if val else 0
        return {"ok": True, "value": val, "refs": refs, "missing": missing}

    return r
