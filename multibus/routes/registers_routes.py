"""Register catalog/selection + ad-hoc queries + search + poll groups.

Moved verbatim from create_api(). ``modbus_client`` (the primary's client) is
a stable reference; the mqtt/influx publishers are read from ctx at request
time (rebindable via /api/config/apply).
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

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

    def _template_catalog(template_id: str) -> Dict:
        """Build the register catalog for a device from its TEMPLATE, in the
        exact shape the Registers page already parses
        (``{measurements: {<category>: {entries: [...]}}}``)."""
        t = template_registry.get(template_id)
        if t is None:
            return {"measurements": {}}
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
                "register_type": getattr(x, 'register_type', 'holding'),
                "poll_group": x.poll_group,
            })
        return {"measurements": cats,
                "device_template": {"id": t.id, "name": t.name,
                                    "registers": len(t.registers)}}

    @r.get("/api/registers/all")
    async def get_all_registers(device: str = Query(default="")):
        """Register catalog for the Registers page. EVERY device — including the
        primary — now draws its catalog from its device template (the uniform
        Tier 2 model: the map lives on the template, not a fixed file). The
        primary falls back to the legacy modbus_data.json only if its template
        can't be resolved, so the picker never regresses. Switching the catalog
        source does NOT touch selected_registers.json, so what is polled — and
        therefore the MQTT/InfluxDB output — is byte-identical."""
        if device:
            _i, dev_cfg, _c = registry.find(device)
            if dev_cfg is None:
                raise HTTPException(status_code=404, detail="device not found")
            if not dev_cfg.primary:
                return _template_catalog(dev_cfg.template)
            prim = dev_cfg
        else:
            prim = next((d for d in config.devices if d.primary), None)
        if prim is not None and prim.template and template_registry.get(prim.template) is not None:
            return _template_catalog(prim.template)
        return config.all_registers            # defensive fallback (template missing)

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
                if dev_client:
                    regs, groups = config.load_device_registers(dev_cfg)
                    dev_client.update_registers(regs, groups)
                    if hasattr(dev_client, 'reload_registers'):
                        dev_client.reload_registers()
                return {"status": "ok", "count": len(reg_list), "device": device}

            config.save_selected_registers(reg_list)

            # Auto-reload pollers with new registers
            if modbus_client:
                modbus_client.update_registers(config.selected_registers, config.poll_groups)
                modbus_client.reload_registers()

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

    @r.post("/api/query/register")
    async def query_register(query: RegisterQuery):
        """Query a single register on-demand."""
        if not modbus_client:
            raise HTTPException(status_code=503, detail="Modbus client not available")

        rt = 'input' if str(query.register_type).lower() in ('input', 'ir', 'fc4', '4') else 'holding'
        value = modbus_client.read_register(query.address, query.data_type, rt)
        if value is not None:
            return {
                "address": query.address,
                "value": value,
                "data_type": query.data_type,
                "register_type": rt,
                "timestamp": datetime.now().isoformat(),
            }
        raise HTTPException(status_code=500, detail="Failed to read register")

    @r.post("/api/query/batch")
    async def query_batch(query: RegisterBatchQuery):
        """Query multiple registers on-demand."""
        if not modbus_client:
            raise HTTPException(status_code=503, detail="Modbus client not available")

        registers = [{"address": x.address, "data_type": x.data_type,
                      "register_type": ('input' if str(x.register_type).lower() in ('input', 'ir', 'fc4', '4') else 'holding')}
                     for x in query.registers]
        results = modbus_client.read_registers_batch(registers)

        return {
            "values": {
                str(addr): value for addr, value in results.items()
            },
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

    return r
