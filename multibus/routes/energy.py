"""Energy tab — cumulative-counter selection + monthly report (InfluxDB).

Moved verbatim from create_api(). The InfluxDB publisher is read from ctx at
REQUEST time (never captured in build) — /api/config/apply may rebind it.
"""
from __future__ import annotations

import asyncio
from typing import Dict

from fastapi import APIRouter, Body, HTTPException, Query

from ._shared import device_influx

# Energy fields are cumulative counters (Wh/varh/VAh). Different meters expose
# different ones, so the Energy tab reads the DEVICE's own selection instead of
# a fixed Janitza-only list; base Wh/varh/VAh counters are shown in k-units.
_ENERGY_UNITS = {'wh', 'kwh', 'mwh', 'varh', 'kvarh', 'vah', 'kvah'}


def _energy_field_from_reg(r):
    u = (getattr(r, 'unit', '') or '').strip()
    ul = u.lower()
    if ul not in _ENERGY_UNITS:
        return None
    div, unit = (1000, 'k' + u) if ul in ('wh', 'varh', 'vah') else (1, u)
    return {"name": r.name, "label": r.label or r.name, "unit": unit, "div": div}


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["energy"])
    config, registry = ctx.config, ctx.registry

    def _energy_device(device):
        return device or next((d.id for d in config.devices if d.primary), "")

    def _energy_candidates(device_id):
        _i, dev_cfg, _c = registry.find(device_id)
        if dev_cfg is None:
            return []
        regs, _g = config.load_device_registers(dev_cfg)
        return [f for f in (_energy_field_from_reg(x) for x in regs) if f]

    def _energy_regs(device):
        """The counters to total for a device: its saved selection, else the
        auto-detected candidates, else the legacy Janitza defaults (primary only)."""
        did = _energy_device(device)
        regs = config.load_energy_fields(did) or _energy_candidates(did)
        if not regs and (not device or registry.find(did)[1] and registry.find(did)[1].primary):
            regs = [
                {"name": "_WH_V[4]", "label": "Consumption (import)", "unit": "kWh", "div": 1000},
                {"name": "_WH_Z[4]", "label": "Injection (export)", "unit": "kWh", "div": 1000},
                {"name": "_QH[4]", "label": "Reactive", "unit": "kvarh", "div": 1000},
                {"name": "_WH_S[4]", "label": "Apparent", "unit": "kVAh", "div": 1000},
            ]
        return regs

    @r.get("/api/energy/fields")
    async def get_energy_fields(device: str = Query(default="")):
        """The device's Energy-tab field selection + the auto-detected candidates
        (every cumulative energy counter in its polled registers)."""
        did = _energy_device(device)
        return {"device": did, "candidates": _energy_candidates(did),
                "selected": config.load_energy_fields(did)}

    @r.post("/api/energy/fields")
    async def set_energy_fields(payload: Dict = Body(...), device: str = Query(default="")):
        """Save which cumulative counters the Energy tab totals for this device."""
        did = _energy_device(device)
        fields = payload.get("fields") if isinstance(payload, dict) else payload
        if not isinstance(fields, list):
            raise HTTPException(status_code=422, detail={"errors": ["fields must be a list"]})
        clean = [{"name": str(f["name"]), "label": str(f.get("label") or f["name"]),
                  "unit": str(f.get("unit") or ""), "div": (float(f.get("div") or 1) or 1)}
                 for f in fields if isinstance(f, dict) and f.get("name")]
        config.save_energy_fields(did, clean)
        return {"status": "ok", "device": did, "selected": clean}

    @r.get("/api/energy/monthly")
    async def energy_monthly(year: int = Query(...), month: int = Query(..., ge=1, le=12),
                             device: str = Query(default="")):
        """Energy for a calendar month: monthly totals (deltas of the device's
        cumulative counters) + a per-day breakdown, from InfluxDB."""
        influxdb_publisher = ctx.influxdb_publisher          # request-time (rebindable)
        if influxdb_publisher is None or not influxdb_publisher.config.enabled:
            raise HTTPException(status_code=503, detail="InfluxDB not enabled")
        regs = _energy_regs(device)
        bucket, device_tag = device_influx(config, device)
        tz = (config.ui.timezone or "Europe/Bucharest")
        res = await asyncio.to_thread(influxdb_publisher.energy_report, year, month, regs,
                                      tz, bucket, device_tag)
        if "error" in res:
            err = res["error"]
            code = 503 if ("disabled" in err or "unavailable" in err) else 400
            raise HTTPException(status_code=code, detail=err)
        return res

    return r
