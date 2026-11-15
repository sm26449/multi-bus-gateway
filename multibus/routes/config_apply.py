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
"""Config apply / reload — reconnect services after a settings change.

Two endpoints (POST /api/config/apply, POST /api/config/reload-registers) moved
VERBATIM out of create_api (P0.8b Lot G) into a ``build(ctx) -> APIRouter``
module. ``apply_config`` MUTATES the publisher holder (``_pub.mqtt`` /
``_pub.influx`` are rebound when MQTT/InfluxDB are enabled at runtime) and
mirrors the new object onto ``ctx.mqtt_publisher`` / ``ctx.influxdb_publisher``
so the other route modules reading those at request time see the live one — so
the holder itself is carried on ``ctx.pub`` and ``ctx`` is written through
directly, exactly as create_api did. The logger keeps the name ``multibus.api``.
"""
from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException

from multibus.outputs.influxdb_publisher import InfluxDBPublisher
from multibus.outputs.mqtt_publisher import MQTTPublisher

logger = logging.getLogger("multibus.api")


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["config"])

    config = ctx.config
    modbus_client = ctx.modbus_client
    current_values = ctx.current_values
    alert_mgr = ctx.alert_mgr
    app = ctx.app
    _pub = ctx.pub

    @r.post("/api/config/apply")
    def apply_config():
        """Apply configuration changes by reconnecting all services."""

        results = {"modbus": False, "mqtt": False, "influxdb": False}

        try:
            # Reconnect Modbus
            if modbus_client:
                modbus_client.update_config(config.modbus)
                modbus_client.update_registers(config.selected_registers, config.poll_groups)
                results["modbus"] = modbus_client.reconnect()
                # the primary's client now sits on the configured wire (ADR-0042, R2D-AW-4)
                _reg, _prim = getattr(ctx, "registry", None), config.primary_device
                if _reg is not None and _prim is not None:
                    _reg.client_rebuilt(_prim)
                # drop store ghosts at deselected addresses (audit M2) — an
                # apply after a config import/restore can change the register set
                from multibus.devices.device_registry import purge_deselected
                purge_deselected(current_values, config.selected_registers)

            # Handle MQTT - create if needed
            if config.mqtt.enabled:
                if _pub.mqtt:
                    _pub.mqtt.update_config(config.mqtt)
                    _pub.mqtt.update_registers(config.selected_registers)
                    results["mqtt"] = _pub.mqtt.reconnect()
                else:
                    # Create new MQTT publisher (MQTT was off at boot / just
                    # enabled) and re-wire it into the virtual-meter manager so
                    # its state + HA discovery publish through the LIVE ref
                    # instead of the old None — otherwise vmeter state/discovery
                    # stays dark until a restart.
                    _pub.mqtt = MQTTPublisher(
                        config=config.mqtt,
                        registers=config.selected_registers,
                        publish_mode=config.mqtt.publish_mode,
                        heartbeat_interval=getattr(config.mqtt, 'heartbeat_interval', 0),
                        has_primary=config.primary_device is not None,
                    )
                    ctx.mqtt_publisher = _pub.mqtt   # mirror for route modules
                    alert_mgr.mqtt = _pub.mqtt       # else alerts publish to the dead ref
                    vmgr = getattr(app.state, "vmeter_manager", None)
                    if vmgr is not None:
                        vmgr.mqtt_publisher = _pub.mqtt
                    # REFAC-HOLDER-1 (ADR-0038 amendment): RulesRuntime and SequenceRunner
                    # captured the boot-time None publisher at construction, so re-point them
                    # at the new object too — otherwise rules/sequences publish nothing until
                    # the next restart.
                    _rt = getattr(app.state, "rules_runtime", None)
                    if _rt is not None:
                        _rt.mqtt = _pub.mqtt
                    _seq = getattr(app.state, "sequences", None)
                    if _seq is not None:
                        _seq.mqtt = _pub.mqtt
                    # Connect in background
                    def connect_mqtt():
                        if _pub.mqtt.connect():
                            logger.info("MQTT connected after enable")
                            if config.mqtt.ha_discovery_enabled:
                                _pub.mqtt.publish_ha_discovery()
                            # (re)start the vmeter state publisher on the new ref
                            if vmgr is not None:
                                try:
                                    vmgr.start_state_publisher()
                                    vmgr.publish_ha_discovery()
                                except Exception as e:  # noqa: BLE001
                                    logger.warning("vmeter re-wire after MQTT enable failed: %s", e)
                    threading.Thread(target=connect_mqtt, daemon=True).start()
                    results["mqtt"] = True
            elif _pub.mqtt:
                # Disable MQTT
                _pub.mqtt.disconnect()
                results["mqtt"] = True

            # Handle InfluxDB - create if needed
            if config.influxdb.enabled:
                if _pub.influx:
                    _pub.influx.update_config(config.influxdb)
                    _pub.influx.update_registers(config.selected_registers)
                    results["influxdb"] = _pub.influx.reconnect()
                else:
                    # Create new InfluxDB publisher
                    _pub.influx = InfluxDBPublisher(
                        config=config.influxdb,
                        registers=config.selected_registers,
                        publish_mode=config.influxdb.publish_mode,
                        buffer_dir=config.config_path.parent
                    )
                    ctx.influxdb_publisher = _pub.influx   # mirror for route modules
                    # REFAC-HOLDER-1: re-point RulesRuntime's influx ref too (SequenceRunner
                    # and AlertManager have no influx sink of their own).
                    _rt = getattr(app.state, "rules_runtime", None)
                    if _rt is not None:
                        _rt.influx = _pub.influx
                    results["influxdb"] = _pub.influx.connected
                    logger.info(f"InfluxDB publisher created, connected: {_pub.influx.connected}")
            elif _pub.influx:
                # Disable InfluxDB
                _pub.influx.close()
                results["influxdb"] = True

            # the Virtual BMS instances follow the configuration too (WK7-LIFE-1): a removed
            # one is stopped, a changed one rebuilt disarmed, an unchanged one left as it is —
            # and a publisher created above gets their command topics
            _vbm = getattr(app.state, "vbms_manager", None)
            if _vbm is not None:
                _vbm.reconcile(config)
                _bridge = getattr(app.state, "vbms_command_bridge", None)
                if _bridge is not None:
                    _bridge.sync()

            return {
                "status": "ok",
                "results": results,
                "message": "Configuration applied"
            }
        except Exception as e:
            logger.exception("Error applying config")
            raise HTTPException(status_code=500,
                                detail=f"internal error ({type(e).__name__}) — see the server log")

    @r.post("/api/config/reload-registers")
    def reload_registers():
        """Reload registers without full reconnect."""
        try:
            # Reload config
            config._load_selected_registers()

            # Update clients
            if modbus_client:
                modbus_client.update_registers(config.selected_registers, config.poll_groups)
                modbus_client.reload_registers()
            # drop store ghosts at deselected addresses (audit M2)
            from multibus.devices.device_registry import purge_deselected
            purge_deselected(current_values, config.selected_registers)

            if _pub.mqtt:
                _pub.mqtt.update_registers(config.selected_registers)

            if _pub.influx:
                _pub.influx.update_registers(config.selected_registers)

            return {
                "status": "ok",
                "count": len(config.selected_registers),
                "message": "Registers reloaded"
            }
        except Exception as e:
            logger.exception("Error reloading registers")
            raise HTTPException(status_code=500,
                                detail=f"internal error ({type(e).__name__}) — see the server log")

    return r
