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
"""Register catalog/selection + ad-hoc queries + search + poll groups.

Moved verbatim from create_api(). ``modbus_client`` (the primary's client) is
a stable reference; the mqtt/influx publishers are read from ctx at request
time (rebindable via /api/config/apply).
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from ._models import RegisterBatchQuery, RegisterQuery, SelectedRegisterUpdate


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["registers"])
    config, registry, template_registry = ctx.config, ctx.registry, ctx.template_registry
    modbus_client = ctx.modbus_client

    def _selected_register_out(x) -> Dict:
        """Serialize one SelectedRegister for the API (shared by the legacy
        and per-device endpoints)."""
        return {
            "address": x.address,
            "name": x.name,
            "description": x.description,
            "label": x.label,
            "unit": x.unit,
            "data_type": x.data_type,
            "poll_group": x.poll_group,
            "json_path": getattr(x, "json_path", ""),
            "topic": getattr(x, "topic", ""),
            "scale": getattr(x, "scale", 1.0),
            "nan": getattr(x, "nan", None),
            "register_type": getattr(x, "register_type", "holding"),
            "mqtt_enabled": x.mqtt_enabled,
            "mqtt_topic": x.mqtt_topic,
            "influxdb_enabled": x.influxdb_enabled,
            "influxdb_measurement": x.influxdb_measurement,
            "influxdb_tags": x.influxdb_tags,
            "ui_show_on_dashboard": x.ui_show_on_dashboard,
            "ui_widget": x.ui_widget,
            "ui_config": x.ui_config,
            "thresholds": x.thresholds if hasattr(x, 'thresholds') else None,
        }

    # Memoized catalog: rebuilding 4000+ entries + JSON-serializing ~1 MB on the
    # event loop every call stalled it (~9 ms Intel / ~81 ms RPi-3) at every UI
    # boot and device switch. Cache keyed on the template OBJECT identity, so a
    # template_registry.reload() (new objects) auto-invalidates it.
    _catalog_cache: Dict[str, tuple] = {}   # template_id -> (template_obj, catalog, etag)

    def _template_catalog(template_id: str) -> Dict:
        """Build the register catalog for a device from its TEMPLATE, in the
        exact shape the Registers page already parses
        (``{measurements: {<category>: {entries: [...]}}}``)."""
        t = template_registry.get(template_id)
        if t is None:
            return {"measurements": {}}
        _cached = _catalog_cache.get(template_id)
        if _cached is not None and _cached[0] is t:
            return _cached[1]
        cats: Dict[str, Dict] = {}
        ordered = sorted(t.categories.items(), key=lambda kv: kv[1].get('order', 99))
        for cid, cmeta in ordered:
            cats[cid] = {"name": cmeta.get('label', cid), "entries": []}
        for x in t.registers:
            cats.setdefault(x.category, {"name": x.category, "entries": []})
            cats[x.category]["entries"].append({
                "address": x.address, "name": x.name, "unit": x.unit,
                "description": x.description or x.label,
                "data_type": x.data_type, "access": x.access,
                "json_path": x.json_path, "topic": getattr(x, "topic", ""), "scale": x.scale,
                "nan": getattr(x, "nan", None),
                "register_type": getattr(x, 'register_type', 'holding'),
                "poll_group": x.poll_group,
            })
        catalog = {"measurements": cats,
                   "device_template": {"id": t.id, "name": t.name,
                                       "registers": len(t.registers)}}
        etag = f'"{template_id}-{len(t.registers)}-{id(t) & 0xffffff:x}"'
        _catalog_cache[template_id] = (t, catalog, etag)
        return catalog

    def _catalog_etag(template_id: str) -> str:
        t = template_registry.get(template_id)
        _cached = _catalog_cache.get(template_id)
        if _cached is None or _cached[0] is not t:
            _template_catalog(template_id)          # populate/refresh cache
            _cached = _catalog_cache.get(template_id)
        return _cached[2] if _cached else ""

    @r.get("/api/registers/all")
    async def get_all_registers(request: Request, device: str = Query(default="")):
        """Register catalog for the Registers page. EVERY device — including the
        primary — now draws its catalog from its device template (the uniform
        Tier 2 model: the map lives on the template, not a fixed file). The
        primary falls back to the legacy modbus_data.json only if its template
        can't be resolved, so the picker never regresses. Switching the catalog
        source does NOT touch selected_registers.json, so what is polled — and
        therefore the MQTT/InfluxDB output — is byte-identical.

        Served with an ETag: an unchanged catalog is a 304 (no 1 MB transfer),
        and the body itself is memoized so a cache-miss doesn't rebuild it."""
        _tpl = None
        if device:
            _i, dev_cfg, _c = registry.find(device)
            if dev_cfg is None:
                raise HTTPException(status_code=404, detail="device not found")
            if not dev_cfg.primary:
                _tpl = dev_cfg.template
            elif dev_cfg.template and template_registry.get(dev_cfg.template) is not None:
                _tpl = dev_cfg.template
        else:
            prim = next((d for d in config.devices if d.primary), None)
            if prim is not None and prim.template and template_registry.get(prim.template) is not None:
                _tpl = prim.template
        if _tpl is None:
            return config.all_registers        # defensive fallback (template missing)
        etag = _catalog_etag(_tpl)
        if etag and request.headers.get("if-none-match") == etag:
            return Response(status_code=304)
        return JSONResponse(_template_catalog(_tpl), headers={"ETag": etag} if etag else None)

    @r.get("/api/registers/selected")
    async def get_selected_registers(device: str = Query(default="")):
        """Get currently selected registers (optionally for a specific device)."""
        if device:
            _i, dev_cfg, _c = registry.find(device)
            if dev_cfg is None:
                raise HTTPException(status_code=404, detail="device not found")
            if not dev_cfg.primary:
                regs, groups = config.load_device_registers(dev_cfg)
                return {
                    "registers": [_selected_register_out(x) for x in regs],
                    "poll_groups": {name: {"interval": g.interval,
                                           "description": g.description}
                                    for name, g in groups.items()},
                }
        return {
            "registers": [_selected_register_out(x) for x in config.selected_registers],
            "poll_groups": {
                name: {"interval": g.interval, "description": g.description}
                for name, g in config.poll_groups.items()
            }
        }

    @r.post("/api/registers/selected")
    async def update_selected_registers(registers: List[SelectedRegisterUpdate],
                                        device: str = Query(default="")):
        """Update selected registers configuration (optionally per device —
        a non-primary device saves to its own file and hot-reloads only its
        own pollers)."""
        try:
            reg_list = [
                {
                    "address": x.address,
                    "name": x.name,
                    "description": x.description,
                    "label": x.label,
                    "unit": x.unit,
                    "data_type": x.data_type,
                    "poll_group": x.poll_group,
                    "json_path": x.json_path,
                    "topic": getattr(x, "topic", ""),
                    "scale": x.scale,
                    "nan": getattr(x, "nan", None),
                    "register_type": getattr(x, "register_type", "holding"),
                    "mqtt": {
                        "enabled": x.mqtt_enabled,
                        "topic": x.mqtt_topic,
                    },
                    "influxdb": {
                        "enabled": x.influxdb_enabled,
                        "measurement": x.influxdb_measurement,
                        "tags": x.influxdb_tags,
                    },
                    "ui": {
                        "show_on_dashboard": x.ui_show_on_dashboard,
                        "widget": x.ui_widget,
                        **x.ui_config,
                    },
                    "thresholds": x.thresholds.dict() if x.thresholds else None,
                }
                for x in registers
            ]

            # Non-primary device: own file + hot-reload of ITS pollers only.
            if device and device != config.primary_device.id:
                _i, dev_cfg, dev_client = registry.find(device)
                if dev_cfg is None:
                    raise HTTPException(status_code=404, detail="device not found")
                # Seed the device's poll-group intervals from its template (a new
                # device otherwise inherits the primary's fast realtime rate,
                # which is wrong for a slow HTTP/gateway source).
                tpg = None
                tpl = template_registry.get(dev_cfg.template) if dev_cfg.template else None
                if tpl and getattr(tpl, 'poll_groups', None):
                    tpg = {n: {"interval": g.get("interval", 5),
                               "description": g.get("description", "")}
                           for n, g in tpl.poll_groups.items()}
                config.save_device_registers(device, reg_list, poll_groups=tpg)
                regs, groups = config.load_device_registers(dev_cfg)
                if dev_client:
                    dev_client.update_registers(regs, groups)
                    if hasattr(dev_client, 'reload_registers'):
                        dev_client.reload_registers()
                # drop store ghosts at deselected addresses (M2: a re-select
                # keeping a name at a NEW address must not leave the old
                # entry to shadow it in name-based vmeter lookup) — NOT gated
                # on a live client: the store outlives client restarts
                from ..device_registry import purge_deselected
                _store = registry.store_for(device)
                if _store is not None:
                    purge_deselected(_store, regs)
                return {"status": "ok", "count": len(reg_list), "device": device}

            config.save_selected_registers(reg_list)

            # Auto-reload pollers with new registers
            if modbus_client:
                modbus_client.update_registers(config.selected_registers, config.poll_groups)
                modbus_client.reload_registers()
            # drop store ghosts at deselected addresses (M2 — see device path)
            from ..device_registry import purge_deselected
            purge_deselected(ctx.current_values, config.selected_registers)

            mqtt_publisher = ctx.mqtt_publisher              # request-time (rebindable)
            if mqtt_publisher:
                mqtt_publisher.update_registers(config.selected_registers)
                if config.mqtt.ha_discovery_enabled:
                    mqtt_publisher.publish_ha_discovery()

            influxdb_publisher = ctx.influxdb_publisher      # request-time (rebindable)
            if influxdb_publisher:
                influxdb_publisher.update_registers(config.selected_registers)

            return {"status": "ok", "count": len(reg_list)}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    def _read_client(device_id):
        """The Modbus client an on-demand query must read from: the named
        device's OWN client, or the primary when unset/primary. Without this a
        secondary device's 'Query now' would read the primary's bus and return
        the wrong device's data (and, for rtu-tcp, the wrong wire framing)."""
        if not device_id:
            if not modbus_client:
                raise HTTPException(status_code=503, detail="Modbus client not available")
            return modbus_client
        _i, dev_cfg, dev_client = registry.find(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail=f"device {device_id!r} not found")
        if dev_cfg.primary:
            if not modbus_client:
                raise HTTPException(status_code=503, detail="Modbus client not available")
            return modbus_client
        if dev_client is None or not hasattr(dev_client, 'read_register'):
            raise HTTPException(status_code=409,
                                detail=f"device {device_id!r} is not a live Modbus device")
        return dev_client

    def _apply_scale(value, scale):
        """Engineering value = raw / scale (a divisor). None/0/1 → raw."""
        if value is None or not scale or scale in (1, 1.0):
            return value
        try:
            return value / scale
        except TypeError:
            return value            # non-numeric (e.g. a bit) — leave as-is

    @r.post("/api/query/register")
    async def query_register(query: RegisterQuery):
        """Query a single register on-demand (on the primary or a named device)."""
        client = _read_client(query.device_id)
        rt = 'input' if str(query.register_type).lower() in ('input', 'ir', 'fc4', '4') else 'holding'
        value = client.read_register(query.address, query.data_type, rt)
        if value is not None:
            return {
                "address": query.address,
                "value": _apply_scale(value, query.scale),
                "data_type": query.data_type,
                "register_type": rt,
                "device_id": query.device_id,
                "timestamp": datetime.now().isoformat(),
            }
        raise HTTPException(status_code=500, detail="Failed to read register")

    @r.post("/api/query/batch")
    async def query_batch(query: RegisterBatchQuery):
        """Query multiple registers on-demand (on the primary or a named device)."""
        client = _read_client(query.device_id)
        registers = [{"address": x.address, "data_type": x.data_type,
                      "register_type": ('input' if str(x.register_type).lower() in ('input', 'ir', 'fc4', '4') else 'holding')}
                     for x in query.registers]
        results = client.read_registers_batch(registers)
        scale_by_addr = {x.address: x.scale for x in query.registers}

        return {
            "values": {
                str(addr): _apply_scale(value, scale_by_addr.get(addr))
                for addr, value in results.items()
            },
            "device_id": query.device_id,
            "timestamp": datetime.now().isoformat(),
        }

    def _matches_query(entry: Dict, query: str) -> bool:
        """Check if entry matches search query."""
        name = entry.get('name', '').lower()
        unit = entry.get('unit', '').lower()
        address = str(entry.get('address', ''))

        return query in name or query in unit or query == address

    @r.get("/api/search")
    async def search_registers(
        q: str = Query(..., min_length=1, description="Search query"),
        category: Optional[str] = Query(None, description="Filter by category")
    ):
        """Search available registers."""
        results = []
        query = q.lower()

        measurements = config.all_registers.get('measurements', {})

        for cat_name, cat_data in measurements.items():
            if category and cat_name != category:
                continue

            # Check entries
            if 'entries' in cat_data:
                for entry in cat_data['entries']:
                    if _matches_query(entry, query):
                        results.append({**entry, 'category': cat_name})

            # Check subtypes
            if 'subtypes' in cat_data:
                for subtype_name, subtype_data in cat_data['subtypes'].items():
                    for entry in subtype_data.get('entries', []):
                        if _matches_query(entry, query):
                            results.append({
                                **entry,
                                'category': cat_name,
                                'subtype': subtype_name
                            })

        return {"results": results[:100], "total": len(results)}

    @r.get("/api/poll-groups")
    async def get_poll_groups():
        """Get poll group configurations."""
        return {
            name: {"interval": g.interval, "description": g.description}
            for name, g in config.poll_groups.items()
        }

    @r.get("/api/canonical-fields")
    async def get_canonical_fields():
        """The canonical field dictionary — the single source of truth for
        register naming. The register editor reads it for inline autocomplete +
        'did you mean' guidance and to auto-fill the hierarchical MQTT topic +
        InfluxDB measurement, so a user names fields uniformly across devices."""
        from ..canonical_fields import CANONICAL_FIELDS
        return {
            "fields": {
                name: {"measurement": meas, "unit": unit,
                       "mqtt_topic": topic, "description": desc}
                for name, (meas, unit, topic, desc) in CANONICAL_FIELDS.items()
            }
        }

    @r.post("/api/canonical-fields/guess")
    async def guess_canonical_names(payload: Dict = Body(...)):
        """Batch canonical-name inference for the 'auto-canonicalize' editor
        button. Input: {registers:[{name,label,unit,description}]}. Returns a
        {guesses:[name|null]} list aligned by index — null where the classifier
        isn't confident (a wrong rename is worse than none). This is the single,
        unit-tested source of truth (the UI no longer classifies client-side)."""
        from ..canonical_fields import guess_canonical
        regs = payload.get("registers") or []
        if not isinstance(regs, list):
            raise HTTPException(status_code=422, detail="registers must be a list")
        # None for a non-dict item (never SKIP it) so guesses[i] stays aligned
        # with registers[i] — the caller renames registers[i] by that index.
        guesses = [guess_canonical(name=r.get("name", ""), label=r.get("label", ""),
                                   unit=r.get("unit", ""), description=r.get("description", ""))
                   if isinstance(r, dict) else None
                   for r in regs[:5000]]
        return {"guesses": guesses}

    return r
