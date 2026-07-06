"""General (non-security) app settings — currently the report timezone.

``ui.timezone`` drives the monthly-energy calendar boundaries
(routes/energy.py reads it per request), so a change applies live — no
restart. Validated against the IANA database before it is persisted.
"""
from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Body, HTTPException


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["config"])
    config = ctx.config

    @r.get("/api/config/general")
    async def get_general_config():
        """General settings + the server's IANA zone list (for the picker)."""
        try:
            from zoneinfo import available_timezones
            zones = sorted(available_timezones())
        except Exception:  # noqa: BLE001 — tzdata missing → picker degrades to free text
            zones = []
        return {"timezone": config.ui.timezone or "Europe/Bucharest",
                "timezones": zones,
                "default_colors": config.ui.default_colors or {}}

    @r.post("/api/config/general")
    async def update_general_config(payload: Dict = Body(...)):
        """Persist general settings. The timezone must be a valid IANA name —
        a typo here would silently shift every monthly report's boundaries."""
        tz = str(payload.get("timezone", "") or "").strip()
        if not tz:
            raise HTTPException(status_code=422, detail={"errors": ["timezone is required"]})
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception:
            raise HTTPException(status_code=422, detail={"errors": [
                f"'{tz}' is not a valid IANA timezone (e.g. Europe/Bucharest)"]})
        # Default widget colors (optional): phase convention + category hues.
        # Hex values are sanity-checked; the UI resolves the actual defaults.
        if "default_colors" in payload:
            dc = payload.get("default_colors") or {}
            if not isinstance(dc, dict):
                raise HTTPException(status_code=422, detail={"errors": [
                    "default_colors must be an object"]})
            conv = str(dc.get("phase_convention", "distinct") or "distinct")
            if conv not in ("distinct", "iec", "rst", "custom"):
                raise HTTPException(status_code=422, detail={"errors": [
                    "phase_convention must be distinct|iec|rst|custom"]})
            import re as _re
            hexre = _re.compile(r"^#[0-9a-fA-F]{6}$")
            for key in ("phase_custom",):
                vals = dc.get(key) or []
                if vals and (not isinstance(vals, list) or len(vals) != 3
                             or not all(isinstance(v, str) and hexre.match(v) for v in vals)):
                    raise HTTPException(status_code=422, detail={"errors": [
                        f"{key} must be a list of 3 hex colors"]})
            cats = dc.get("categories") or {}
            if not isinstance(cats, dict) or any(
                    not (isinstance(v, str) and hexre.match(v)) for v in cats.values()):
                raise HTTPException(status_code=422, detail={"errors": [
                    "categories values must be hex colors (#rrggbb)"]})
            config.ui.default_colors = {"phase_convention": conv,
                                        "phase_custom": dc.get("phase_custom") or [],
                                        "categories": cats}
        config.ui.timezone = tz
        config.save_yaml_config()
        return {"status": "ok", "timezone": tz, "applies": "live",
                "default_colors": config.ui.default_colors}

    return r
