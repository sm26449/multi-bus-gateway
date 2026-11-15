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
"""Device connection tests — probe a not-yet-saved connection, or a saved device.

Two endpoints (POST /api/devices/test, POST /api/devices/{id}/test) moved VERBATIM
out of create_api (P0.8b Lot G) into a ``build(ctx) -> APIRouter`` module. Bodies
are byte-identical; @_device_runtime becomes @ctx.device_runtime. The probe/test
ecosystem (modbus/tap/mqtt/virtual-map hearings, the saved-device tester, the
one-test-at-a-time gate, the shared source probe) stays in create_api and is
reached through ctx.
"""
from __future__ import annotations

import threading
from typing import Dict   # noqa: F401 — verbatim route bodies

from fastapi import APIRouter, Body, HTTPException

from multibus.devices.device_validation import _vmap_mode_missing
from multibus.core.config import (CAN_PROTOCOLS, NETWORK_PROTOCOLS, SERIAL_PROTOCOLS,
                                  VIRTUAL_MAP_PROTOCOLS)
from multibus.devices.save_merge import MASKED, BridgeConflict, merge_connection


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["device-tests"])

    config = ctx.config
    template_registry = ctx.template_registry
    _find_device = ctx.find_device
    _modbus_probe = ctx.modbus_probe
    _driver_of = ctx.driver_of
    _tap_hearing = ctx.tap_hearing
    _mqtt_hearing = ctx.mqtt_hearing
    _one_test_at_a_time = ctx.one_test_at_a_time
    _test_done = ctx.test_done
    _source_probe = ctx.source_probe
    _listening_template = ctx.dv.listening_template
    _can_test = ctx.can_test
    _test_saved_device = ctx.test_saved_device
    _virtual_map_probe = ctx.virtual_map_probe

    @r.post("/api/devices/test")
    def test_device_adhoc(payload: Dict = Body(...)):
        """Probe a connection that is not saved (yet): the wizard's step 1, or
        a device page whose connection was edited. With ``device_id`` the
        stored connection is merged under the form's, as a save would — so a
        password left blank, the headers, the rest of a serial line are the
        stored ones (used here, never echoed)."""
        conn = payload.get('connection', payload) or {}
        _did = str(payload.get('device_id') or '').strip()
        _stored = config.get_raw_device(_did) if _did else None
        if _stored and isinstance(conn, dict):
            conn = dict(conn)
            if conn.get('password') in ('', MASKED):
                conn.pop('password')
            try:
                conn = merge_connection(conn, _stored.get('connection'), config.bridges)
            except BridgeConflict as e:
                return {"ok": False, "message": str(e)}
        if conn.get('bridge'):
            from multibus.transports.serial.bridges import resolve_connection as _rb
            conn = _rb(conn, config.bridges)     # test the bus the device will use
        protocol = str(conn.get('protocol', 'tcp')).lower()
        if protocol == 'http':
            url = str(conn.get('url', '')).strip()
            if not (url.startswith('http://') or url.startswith('https://')):
                raise HTTPException(status_code=422, detail={"errors": ["connection.url required (http:// or https://)"]})
            from multibus.transports.http.http_client import HttpClient
            # If a template is named, resolve ITS json_paths against the live
            # response so the test reports "N/M paths resolved" (proves the map
            # fits the endpoint), not just that the URL is reachable.
            tpl_id = str(payload.get('template') or conn.get('template') or '').strip()
            tpl = template_registry.get(tpl_id) if tpl_id else None
            regs = list(tpl.registers) if tpl else []
            return HttpClient({'url': url, 'timeout': conn.get('timeout', 8)}, regs, {},
                              allow_nonlan=config.security.allow_nonlan_http_devices).test_read()
        if protocol == 'mqtt':
            broker = str(conn.get('broker', '')).strip()
            topic = str(conn.get('topic', '')).strip()
            if not broker or not topic:
                raise HTTPException(status_code=422, detail={"errors": ["connection.broker and connection.topic required"]})
            from multibus.commissioning.discovery import lan_host_error
            _e = lan_host_error(broker, config.security.allow_nonlan_http_devices)
            if _e:
                raise HTTPException(status_code=422, detail={"errors": [f"connection.broker: {_e}"]})
            import paho.mqtt.client as mqtt
            got = {"connected": False, "msg": None, "topic": None}
            ev = threading.Event()
            cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            if conn.get('username'):
                cli.username_pw_set(str(conn.get('username')), str(conn.get('password', '')))
            if conn.get('tls'):
                try:
                    if conn.get('tls_ca'):
                        cli.tls_set(ca_certs=str(conn['tls_ca']))
                    else:
                        cli.tls_set()
                    if conn.get('tls_insecure'):
                        cli.tls_insecure_set(True)      # a device's own certificate (a Victron GX)
                except Exception as e:  # noqa: BLE001
                    # fail CLOSED (external audit): falling through would send
                    # the operator's broker credentials over cleartext and
                    # report the TLS config as working
                    return {"ok": False, "error": f"TLS setup failed: {e}"}

            def _oc(c, u, f, rc, props=None):
                got["connected"] = True
                c.subscribe(topic)

            def _om(c, u, m):
                got["msg"] = m.payload.decode('utf-8', 'replace')[:400]
                got["topic"] = m.topic
                ev.set()
            cli.on_connect = _oc
            cli.on_message = _om
            try:
                cli.connect(broker, int(conn.get('port', 1883)), keepalive=10)
                cli.loop_start()
                ev.wait(timeout=3.0)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"connect failed: {e}"}
            finally:
                # both halves, on EVERY path — a connect that half-succeeded
                # must not leak its socket/network thread
                try:
                    cli.loop_stop()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    cli.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            if not got["connected"]:
                return {"ok": False, "message": "could not connect to the broker"}
            if got["msg"] is not None:
                return {"ok": True, "connected": True, "message_received": True,
                        "topic": got["topic"], "sample": got["msg"],
                        "message": f"connected · message on {got['topic']}: {got['msg'][:80]}"}
            return {"ok": True, "connected": True, "message_received": False,
                    "message": "connected — no message on the topic within 3s (it may be idle)"}
        if protocol in CAN_PROTOCOLS:
            from multibus.transports.can.can_client import connection_errors as _can_errors
            _ce = _can_errors(conn)
            if _ce:
                raise HTTPException(status_code=422, detail={"errors": _ce})
            return _can_test(conn)
        if protocol == 'rtu_tap':
            # nothing to ask: a tap never transmits, and the tty may already be
            # held by a running tap — the saved device's test reports what it hears
            return {"ok": None, "message": "listen-only — nothing to probe; save "
                                           "the device, then Test shows what the tap hears"}
        _tid = str(payload.get('template') or (conn.get('template') if isinstance(conn, dict) else '')
                   or (_stored or {}).get('template') or '').strip()
        if protocol in VIRTUAL_MAP_PROTOCOLS:
            # a map only ever heard (the Seplos inter-pack tap) is never asked,
            # whatever mode the form says: a question there collides with the
            # master pack (SV2-6)
            from multibus.catalog.device_template import template_listens_only
            _ttpl = template_registry.get(_tid) if _tid else None
            if _ttpl is not None and template_listens_only(_ttpl):
                return {"ok": None, "message": f"listen-only: template '{_tid}' is heard on a bus another "
                                               "master polls — nothing to ask; save the device, then Test "
                                               "shows what it hears"}
            if _vmap_mode_missing(conn, (_stored or {}).get('connection')):
                raise HTTPException(status_code=422, detail={"errors": [
                    "connection.mode: choose poll or listen before a test"]})
            return _virtual_map_probe(protocol, conn)
        if _listening_template(_tid):
            # a Modbus map written for listening runs as a tap whatever the
            # form says (ADR-0025): nothing to ask
            return {"ok": None, "message": f"listen-only: template '{_tid}' is heard on a bus another "
                                           "master polls — nothing to ask; save the device as listen only, "
                                           "then Test shows what it hears"}
        if protocol in SERIAL_PROTOCOLS and not str(conn.get('serial_port', '')).strip():
            raise HTTPException(status_code=422, detail={"errors": ["connection.serial_port required"]})
        if protocol in NETWORK_PROTOCOLS and not str(conn.get('host', '')).strip():
            raise HTTPException(status_code=422, detail={"errors": ["connection.host required"]})
        return _modbus_probe(conn, int(conn.get('unit_id', 1)),
                             float(conn.get('timeout', 3)),
                             int(payload.get('address', 0)))


    @r.post("/api/devices/{device_id}/test")
    @ctx.device_runtime
    def test_device(device_id: str):
        """Probe a saved device (TCP or RTU). Uses its first selected register
        (a real address beats address 0) when one exists."""
        _idx, dev_cfg, _client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        if dev_cfg.protocol == 'none':
            from multibus.devices.meter_links import links_of as _lo
            by = ', '.join(dict.fromkeys(ln['device'] for ln in _lo(dev_cfg))) or 'no meter'
            return {'ok': None, 'message': f"nothing to test — measured by {by}; test those devices"}
        _one_test_at_a_time(f"device:{device_id}")
        try:
            return _test_saved_device(dev_cfg, _client)
        finally:
            _test_done(f"device:{device_id}")

    return r
