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
"""General (non-security) app settings — currently the report timezone.

``ui.timezone`` drives the monthly-energy calendar boundaries
(routes/energy.py reads it per request), so a change applies live — no
restart. Validated against the IANA database before it is persisted.
"""
from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Body, HTTPException, Request


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
        from .. import clock
        clock.set_timezone(tz)          # rules and calculated fields read the hour in it
        config.save_yaml_config()
        return {"status": "ok", "timezone": tz, "applies": "live",
                "default_colors": config.ui.default_colors}

    # ── how new devices are named (ADR-0026 §3) ──────────────────────────
    def _identity_view() -> Dict:
        from ..identity import DEFAULT_PATTERNS
        return {"identity_scope": config.identity_scope, "gateway_id": config.gateway_id,
                "defaults": DEFAULT_PATTERNS,
                "default_topic_pattern": config.mqtt.default_topic_pattern,
                "default_bucket_pattern": config.influxdb.default_bucket_pattern,
                # the patterns an operator chose ('' = the scope's default)
                "explicit": {"topic": getattr(config, '_topic_pattern_explicit', ''),
                             "bucket": getattr(config, '_bucket_pattern_explicit', '')},
                # what a device created now would get
                "example": {"topic_prefix": config.default_topic_prefix("{device}"),
                            "bucket": config.default_bucket("{device}")}}

    @r.get("/api/config/identity")
    async def get_identity():
        """This gateway's id and how new devices are named."""
        return _identity_view()

    @r.post("/api/config/identity")
    def update_identity(request: Request, payload: Dict = Body(...)):
        """Set the gateway id and / or the identity scope: how devices
        created AFTERWARDS are named — topic, bucket and Home Assistant node.
        A device that exists keeps all three: they are stored with it
        (ADR-0026 §3), so nothing is renamed or re-announced."""
        from ..identity import DEFAULT_PATTERNS, SCOPES, fill, gateway_id_error, pattern_error
        from ..topic_prefix import overlap
        fields: Dict[str, str] = {}
        scope = payload.get("identity_scope", config.identity_scope)
        gid = payload.get("gateway_id", config.gateway_id)
        gid = str(gid if gid is not None else "").strip()
        if scope not in SCOPES:
            fields["identity_scope"] = "gateway (new devices are named after this gateway) or legacy"
        if gid and gateway_id_error(gid):
            fields["gateway_id"] = gateway_id_error(gid)
        elif not gid and scope == "gateway":
            fields["gateway_id"] = "the gateway scope names devices after this id — set one"
        elif not gid:
            for key, pat in (("mqtt.default_topic_pattern", config._topic_pattern_explicit),
                             ("influxdb.default_bucket_pattern", config._bucket_pattern_explicit)):
                if pattern_error(pat, gid):
                    fields["gateway_id"] = f"{key} uses {{gateway}} — keep an id"
        if not fields:
            # what a device created now would publish under must not sit
            # under (or over) the gateway's own root
            pat = config._topic_pattern_explicit or DEFAULT_PATTERNS[scope]["topic"]
            example = fill(pat, "device", gid)
            root = str(config.mqtt.topic_prefix or "")
            if root and overlap(example, root):
                fields["gateway_id"] = (f"new devices would publish under '{example}', inside the gateway's "
                                        f"own topics ('{root}') — choose another id")
        if fields:
            raise HTTPException(status_code=422, detail={
                "errors": [f"{k}: {v}" for k, v in fields.items()], "fields": fields})
        before = (config.identity_scope, config.gateway_id)
        lock = getattr(ctx, "mutation_lock", None)
        import contextlib
        with (lock if lock is not None else contextlib.nullcontext()):
            # under the device lock: a device created meanwhile is named one way
            config.set_identity(scope=scope, gateway_id=gid)
            try:
                config.save_yaml_config()
            except Exception:
                config.set_identity(scope=before[0], gateway_id=before[1])   # not saved: not in effect
                raise
        if (scope, gid) != before:
            audit_log = getattr(ctx, "audit_log", None)
            if audit_log is not None:
                audit_log.append(user=getattr(request.state, "user", "") or "-",
                                 ip=request.client.host if request.client else "-",
                                 action="gateway identity changed", target=gid or "-", status="ok",
                                 detail={"identity_scope": [before[0], scope], "gateway_id": [before[1], gid]})
        return {"status": "ok", **_identity_view()}

    return r
