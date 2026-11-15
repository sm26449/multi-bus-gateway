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
"""Device output sinks — HTTP/JSON, MQTT/InfluxDB/HA, publishing rhythm, OPC UA,
generic REST push.

Six endpoints (set_device_http_output/outputs/publishing/opcua_output/rest_push,
test_device_rest_push) moved VERBATIM out of create_api (P0.8b Lot G) into a
``build(ctx) -> APIRouter`` module. Bodies are byte-identical; the create_api
collaborators are aliased from ``ctx`` (``@_device_write`` -> ``@ctx.device_write``,
``@_device_runtime`` -> ``@ctx.device_runtime``). None of these restart a poller:
they flip switches the poller reads at each reading. ``_publishing`` stays in
create_api (the device-entry builder also uses it) and is reached via ctx. The
logger keeps the name ``multibus.api``.
"""
from __future__ import annotations

import logging
from typing import Dict

from fastapi import APIRouter, Body, HTTPException

from multibus.core.config import heartbeat_seconds
from multibus.outputs.rest_push import RestPusher
from multibus.payload import flag

logger = logging.getLogger("multibus.api")


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["device-outputs"])

    config = ctx.config
    registry = ctx.registry
    rest_push_manager = ctx.rest_push_manager
    _REST_HDR_MASK = ctx.rest_hdr_mask
    _refresh_shared = ctx.refresh_shared
    _publishing = ctx.publishing
    _opcua_block = ctx.opcua_block
    _apply_rest_push = ctx.apply_rest_push
    _rest_push_public = ctx.rest_push_public
    _rest_cfg = ctx.rest_cfg
    _rest_provider = ctx.rest_provider
    _sync_device_discovery = ctx.sync_device_discovery

    @r.post("/api/devices/{device_id}/http-output")
    @ctx.device_write
    def set_device_http_output(device_id: str, payload: Dict = Body(...)):
        """Toggle the HTTP/JSON output sink for a device. Read-only feed, so no
        poller restart — just flip the flag, persist, and refresh the runtime
        cfg so /api/devices reflects it. Works for the primary too."""
        dev_cfg = config.get_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        enabled = flag(payload, 'enabled', False)
        try:
            config.set_http_output(device_id, enabled)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        new_cfg = config.get_device(device_id)
        registry.replace(device_id, new_cfg)       # keep the running client
        _refresh_shared(new_cfg, 'endpoint')
        logger.info(f"device {device_id}: http output {'enabled' if enabled else 'disabled'}")
        return {"status": "ok", "device_id": device_id,
                "http_output_enabled": enabled,
                "path": f"/api/meters/{device_id}"}

    @r.post("/api/devices/{device_id}/outputs")
    @ctx.device_write
    def set_device_outputs(device_id: str, payload: Dict = Body(...)):
        """Switch a device's MQTT / InfluxDB output or its Home Assistant
        discovery at once ({mqtt, influxdb, ha_discovery}, each optional). No
        poller restart: the poller reads the switches at every reading."""
        if config.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="device not found")
        flags = {k: payload[k] for k in ('mqtt', 'influxdb', 'ha_discovery') if k in payload}
        if not flags or not all(isinstance(v, bool) for v in flags.values()):
            raise HTTPException(status_code=422, detail={"errors": [
                "send at least one of mqtt, influxdb, ha_discovery, each true or false"]})
        try:
            config.set_device_sinks(device_id, **flags)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        new_cfg = config.get_device(device_id)
        registry.replace(device_id, new_cfg)       # keep the running client
        if 'ha_discovery' in flags or 'mqtt' in flags:
            _sync_device_discovery()
        logger.info(f"device {device_id}: outputs {flags}")
        return {"status": "ok", "device_id": device_id, "mqtt_enabled": new_cfg.mqtt_enabled,
                "influxdb_enabled": new_cfg.influxdb_enabled,
                "ha_discovery_enabled": new_cfg.ha_discovery_enabled}

    @r.post("/api/devices/{device_id}/publishing")
    @ctx.device_write
    def set_device_publishing(device_id: str, payload: Dict = Body(...)):
        """A device's own publishing rhythm, apart from Settings:
        {mqtt: {publish_mode, heartbeat_interval}, influxdb: {publish_mode,
        write_interval, heartbeat_interval}}; null = back to Settings, a key
        left out stays.
        Applies at the next reading, no poller restart."""
        if config.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="device not found")
        from multibus.devices.device_patch import publishing_clean
        out, errors = publishing_clean(payload, heartbeat_seconds)
        if not out and not errors:
            errors.append("send mqtt and/or influxdb")
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        try:
            config.set_device_publishing(device_id, **out)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        new_cfg = config.get_device(device_id)
        registry.replace(device_id, new_cfg)
        _refresh_shared(new_cfg, 'group')
        logger.info(f"device {device_id}: publishing {out}")
        return {"status": "ok", "device_id": device_id, "publishing": _publishing(new_cfg)}

    @r.post("/api/devices/{device_id}/opcua-output")
    @ctx.device_write
    def set_device_opcua_output(device_id: str, payload: Dict = Body(...)):
        """Serve a device on the OPC UA server or not, and which of its values
        (`fields`, omitted or [] = every one). No poller restart: the server
        picks it up on its next pass. An endpoint unit sets its whole group."""
        if config.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="device not found")
        block = _opcua_block(payload)
        try:
            config.set_opcua_output(device_id, block['enabled'], block.get('fields'))
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        new_cfg = config.get_device(device_id)
        registry.replace(device_id, new_cfg)       # keep the running client
        _refresh_shared(new_cfg, 'group')
        shared = [d.id for d in config.devices if new_cfg.endpoint_id and d.endpoint_id == new_cfg.endpoint_id
                  and d.group_id == new_cfg.group_id]
        logger.info(f"device {device_id}: opc ua output {'on' if block['enabled'] else 'off'}"
                    f"{' (' + ', '.join(block['fields']) + ')' if block.get('fields') else ''}")
        return {"status": "ok", "device_id": device_id, "opcua_enabled": new_cfg.opcua_enabled,
                "opcua_fields": list(new_cfg.opcua_fields), "applies_to": shared or [device_id]}

    @r.post("/api/devices/{device_id}/rest-push")
    @ctx.device_write
    def set_device_rest_push(device_id: str, payload: Dict = Body(...)):
        """Configure the generic REST push sink for a device (POST its values to
        an external URL on an interval). Header VALUES are masked on read and
        preserved on save; the pusher thread is (re)started in place."""
        dev = config.get_device(device_id)
        if dev is None:
            raise HTTPException(status_code=404, detail="device not found")
        # Preserve masked header secrets: a value equal to the mask keeps the stored one.
        from multibus.devices.device_patch import rest_push_clean
        cfg, errors = rest_push_clean(payload, dev.rest_push.get('headers') or {}, _REST_HDR_MASK, flag)
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        enabled = cfg['enabled']
        try:
            config.set_rest_push(device_id, cfg)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        new_cfg = config.get_device(device_id)
        registry.replace(device_id, new_cfg)       # keep the running client
        _apply_rest_push(new_cfg)
        _refresh_shared(new_cfg, 'rest')
        logger.info(f"device {device_id}: REST push {'enabled' if enabled else 'disabled'}")
        return {"status": "ok", "rest_push": _rest_push_public(new_cfg)}

    @r.post("/api/devices/{device_id}/rest-push/test")
    @ctx.device_runtime
    def test_device_rest_push(device_id: str):
        """Push once right now and report the result — the card's Test button.
        Works even before enabling, as long as a URL is configured."""
        dev = config.get_device(device_id)
        if dev is None:
            raise HTTPException(status_code=404, detail="device not found")
        cfg = _rest_cfg(dev)
        if not str(cfg.get('url', '')).strip():
            raise HTTPException(status_code=422, detail={"errors": ["configure a URL first"]})
        status = (rest_push_manager.push_now(device_id)
                  if rest_push_manager.running(device_id)
                  else RestPusher(device_id, cfg,
                                  _rest_provider(device_id, dev.primary)).push_once())
        return {"status": "ok", **(status or {})}

    return r
