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
"""Installations / endpoints — protocols, list/get, probe, CRUD and
template-refresh.

Twelve endpoints moved VERBATIM out of create_api (P0.8b Lot G) into a
``build(ctx) -> APIRouter`` module (list_protocols, list_endpoints, get_endpoint,
test_endpoint, create_endpoint, update_endpoint, rename_endpoint_unit,
endpoint_template_changes, device_template_changes, refresh_endpoint_from_template,
refresh_device_from_template, delete_endpoint), with their exclusive helpers
(_connection_matrix_extras, _source_probe, _test_endpoint_units, the _unit_write
guard). Bodies are byte-identical. The endpoints domain logic is the Lot E
EndpointService (ctx.es); device validation the Lot B DeviceValidator (ctx.dv);
the probe/test collaborators and PROTOCOLS_BY_CONTEXT come from ctx. The logger
keeps the name ``multibus.api``.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional   # noqa: F401 — verbatim bodies/helpers

from fastapi import APIRouter, Body, HTTPException

from multibus import write_guard as _etag_guard
from multibus.core.config import (NETWORK_PROTOCOLS, SERIAL_PROTOCOLS,
                                   VIRTUAL_MAP_OVER_TCP, VIRTUAL_MAP_SERIAL)

# verbatim from create_api: every Modbus way but listening.
_POLLED_MODBUS = NETWORK_PROTOCOLS + SERIAL_PROTOCOLS

logger = logging.getLogger("multibus.api")


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["endpoints"])

    config = ctx.config
    _es = ctx.es
    _dv = ctx.dv
    PROTOCOLS_BY_CONTEXT = ctx.protocols_by_context
    _endpoint_entry = _es._endpoint_entry
    _listening_template = _dv.listening_template
    _modbus_probe = ctx.modbus_probe
    _driver_of = ctx.driver_of
    _tap_hearing = ctx.tap_hearing
    _mqtt_hearing = ctx.mqtt_hearing
    _one_test_at_a_time = ctx.one_test_at_a_time
    _test_done = ctx.test_done
    _source_probe = ctx.source_probe
    app = ctx.app
    registry = ctx.registry
    template_registry = ctx.template_registry
    _find_device = ctx.find_device
    _entry_lock = ctx.entry_lock
    _refuse_if_used = ctx.refuse_if_used
    _set_new_node = ctx.set_new_node
    _start_device_client = ctx.start_device_client
    _sync_device_discovery = ctx.sync_device_discovery
    _device_entry = ctx.device_entry
    _check_if_match = ctx.check_if_match
    _device_tag_now = ctx.device_tag_now
    _validate_endpoint_payload = _es._validate_endpoint_payload
    _stop_endpoint_devices = _es._stop_endpoint_devices
    _start_endpoint_devices = _es._start_endpoint_devices
    _endpoint_runtime_sig = _es._endpoint_runtime_sig
    _unit_device_id = _es._unit_device_id
    _update_endpoint_locked = _es._update_endpoint_locked
    _delete_endpoint_locked = _es._delete_endpoint_locked

    def _connection_matrix_extras() -> Dict[str, Any]:
        from multibus.devices.connection_matrix import INVALID, defaults
        return {'invalid': copy.deepcopy(INVALID), 'defaults': defaults(config)}

    @r.get("/api/protocols")
    def list_protocols():
        """The protocols each editor may offer (device, installation source,
        installation, group), and which of them are serial lines here."""
        from multibus.devices.connection_spec import LABELS, MATRIX
        return {**{k: list(v) for k, v in PROTOCOLS_BY_CONTEXT.items()},
                'serial': list(SERIAL_PROTOCOLS + ('rtu_tap',) + VIRTUAL_MAP_SERIAL),
                'network': list(NETWORK_PROTOCOLS + VIRTUAL_MAP_OVER_TCP),
                # the valid cells per editor, each with the connection it is
                # saved as, and the words for the axes (ADR-0026 §1)
                'matrix': copy.deepcopy(MATRIX), 'labels': copy.deepcopy(LABELS),
                # the cells no device may have, with their reasons, and every
                # advanced key's default with its source (ADR-0031 §5)
                **_connection_matrix_extras()}

    @r.get("/api/endpoints")
    def list_endpoints():
        """All endpoints with their materialized units and live status."""
        return {"endpoints": [_endpoint_entry(p) for p in config.endpoints]}

    @r.get("/api/endpoints/{endpoint_id}")
    def get_endpoint(endpoint_id: str):
        p = config.get_raw_endpoint(endpoint_id)
        if p is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        return _endpoint_entry(p)


    @r.post("/api/endpoints/{endpoint_id}/test")
    def test_endpoint(endpoint_id: str):
        """Probe every unit of an endpoint over EVERY source it is read through.

        One answer per (source, unit): only that tells "the datalogger is deaf"
        from "unit 3 is not configured on it" from "Modbus is fine but the Solar
        API is off". Before sources existed this probed the endpoint's top-level
        Modbus connection alone, which a grouped installation does not have — so
        an installation reading perfectly over HTTP reported every unit blocked.
        CAUTION: a Modbus probe opens ANOTHER client on that endpoint, and
        dataloggers serve only a few at once — operator-triggered, never a
        background poll."""
        p = config.get_raw_endpoint(endpoint_id)
        if p is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        _one_test_at_a_time(f"endpoint:{endpoint_id}")
        try:
            return _test_endpoint_units(endpoint_id, p)
        finally:
            _test_done(f"endpoint:{endpoint_id}")

    def _test_endpoint_units(endpoint_id: str, p: Dict) -> Dict:
        conn = dict(p.get('connection', {}) or {})
        timeout = float(conn.get('timeout', 3) or 3)
        rows = []
        for dev in config.endpoint_devices(endpoint_id):
            def address_of(src=None, _dev=dev):
                regs, _g = config.load_device_registers(_dev, source=src)
                # a real address beats address 0 — some gateways answer 0 blindly
                return regs[0].address if regs else 0
            srcs = [s for s in (dev.sources or []) if getattr(s, 'enabled', True)]
            if not srcs:
                res = _modbus_probe(conn, dev.connection.unit_id, timeout, address_of())
                rows.append({'source': 'default', 'protocol': conn.get('protocol', 'tcp'),
                             'unit_id': dev.connection.unit_id, 'device_id': dev.id,
                             'where': f"{conn.get('host', '')}:{conn.get('port', 502)}",
                             **res})
                continue
            for src in srcs:
                res = _source_probe(dev, src, address_of, registry.find(dev.id)[2])
                rows.append({'source': src.id, 'protocol': src.protocol,
                             'unit_id': dev.connection.unit_id, 'device_id': dev.id,
                             **res})
        # one line per source: how many of its units answered
        summary, order = {}, []
        for r in rows:
            k = r['source']
            if k not in summary:
                order.append(k)
                summary[k] = {'id': k, 'protocol': r['protocol'], 'answered': 0,
                              'probed': 0, 'total': 0, 'where': r.get('where', '')}
            summary[k]['total'] += 1
            if r.get('ok') is not None:
                summary[k]['probed'] += 1
                summary[k]['answered'] += 1 if r['ok'] else 0
        for v in summary.values():
            v['ok'] = (v['answered'] == v['probed']) if v['probed'] else None
        probed = [r for r in rows if r.get('ok') is not None]
        return {'endpoint': endpoint_id,
                'ok': bool(probed) and all(r['ok'] for r in probed),
                'units': rows,
                'sources': [summary[k] for k in order]}

    @r.post("/api/endpoints")
    @ctx.serialized_mutation
    def create_endpoint(payload: Dict = Body(...)):
        """Create an endpoint: validate → persist → materialize N devices, seed
        each from the template and hot-start their pollers (no restart)."""
        raw = _validate_endpoint_payload(payload)
        raw.setdefault('opcua', {'enabled': False})      # a new endpoint: not on OPC UA until chosen
        _set_new_node(raw.setdefault('mqtt', {}), '${device_id}')   # each unit's node, by pattern
        if not raw['mqtt']:
            # nothing to store (legacy scope, no route): an empty block would
            # read as changed against the validated one on every later save (QA-R1-05)
            raw.pop('mqtt')
        raw['write_locked'] = True                       # …and writes nothing until unlocked (ADR-0025)
        try:
            made = config.upsert_raw_endpoint(raw)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        devices_out = _start_endpoint_devices(made)
        _sync_device_discovery()
        logger.info(f"endpoint {raw['id']}: created with "
                    f"{len(made)} unit(s) (template={raw['template'] or '—'})")
        return {"status": "created", "endpoint": _endpoint_entry(raw),
                "devices": devices_out}

    # exposed for tests: the restart-or-not contract is worth pinning
    app.state.endpoint_runtime_sig = _endpoint_runtime_sig

    # what an installation's own routes manage: an edit never changes them
    @r.put("/api/endpoints/{endpoint_id}")
    @ctx.serialized_mutation
    def update_endpoint(endpoint_id: str, payload: Dict = Body(...)):
        """Update an endpoint. An edit that changes what the units are built from
        re-materializes them (stop → rebuild → start); an edit that only changes
        endpoint-level settings keeps every poller running and just refreshes the
        config behind it."""
        return _update_endpoint_locked(endpoint_id, payload)

    def _unit_write(fn):
        """A write to one unit of an installation, checked against the UNIT's
        device ETag (the tag its page holds, ADR-0030) and answering its new
        one; under the installation's entry lock."""
        def _did(kw):
            return _unit_device_id(kw.get('endpoint_id'), kw.get('unit'))
        return _etag_guard.guard(fn, 'check',
                                 check=lambda im, kw: _check_if_match(im, _did(kw)),
                                 tag=lambda kw: _device_tag_now(_did(kw)),
                                 lock=lambda kw: _entry_lock(_did(kw)))

    @r.patch("/api/endpoints/{endpoint_id}/units/{unit}")
    @_unit_write
    @ctx.serialized_mutation
    def rename_endpoint_unit(endpoint_id: str, unit: str, payload: Dict = Body(...)):
        """Rename one unit of an installation (L3-F12): {name} ('' = the
        installation's naming again). Only that unit's entry changes; nothing
        restarts. `If-Match`: the unit's own ETag (checked when sent)."""
        prev = config.get_raw_endpoint(endpoint_id)
        if prev is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        u = str(unit or '').strip()
        if u.isdigit():
            # a unit address written in several groups names no one unit
            where = []
            for g in ([g for g in (prev.get('groups') or []) if isinstance(g, dict)] or [prev]):
                for x in g.get('units') or []:
                    try:
                        if int(x.get('unit_id') if isinstance(x, dict) else x) == int(u):
                            where.append(str(g.get('id') or 'units'))
                    except (TypeError, ValueError):
                        continue
            if len(where) > 1:
                msg = (f"unit {u} is in several groups ({', '.join(where)}) — name it by its id "
                       f"({', '.join(d.id for d in config.endpoint_devices(endpoint_id) if int(d.connection.unit_id) == int(u))})")
                raise HTTPException(status_code=422, detail={"errors": [f"unit: {msg}"], "fields": {"unit": msg}})
        did = _unit_device_id(endpoint_id, unit)
        if did is None:
            raise HTTPException(status_code=404, detail=f"no unit '{unit}' on '{endpoint_id}'")
        if not isinstance(payload, dict) or set(payload) != {'name'} or not isinstance(payload.get('name'), str):
            raise HTTPException(status_code=422, detail={"errors": ["name: required, the only key — text ('' = "
                                                                    "the installation's naming)"],
                                                         "fields": {"name": "required"}})
        name = payload['name'].strip()
        if len(name) > 80:
            raise HTTPException(status_code=422, detail={"errors": ["name: up to 80 characters"],
                                                         "fields": {"name": "up to 80 characters"}})
        body = copy.deepcopy(prev)
        pid = str(body.get('id', endpoint_id))
        holders = [g for g in (body.get('groups') or []) if isinstance(g, dict)] or [body]
        hit = False
        for h in holders:
            units = h.get('units') or []
            for i, u in enumerate(units):
                uid = u.get('unit_id') if isinstance(u, dict) else u
                try:
                    own = str((u.get('id') if isinstance(u, dict) else None) or f"{pid}-u{int(uid)}").strip()
                except (TypeError, ValueError):
                    continue
                if own != did:
                    continue
                new = dict(u) if isinstance(u, dict) else {'unit_id': u}
                if name:
                    new['name'] = name
                else:
                    new.pop('name', None)
                units[i] = new if set(new) != {'unit_id'} or isinstance(u, dict) else u
                hit = True
                break                           # the one unit named, never a repeat of it
            if hit:
                break
        if not hit:
            raise HTTPException(status_code=404, detail=f"no unit '{unit}' on '{endpoint_id}'")
        out = _update_endpoint_locked(endpoint_id, body)
        _i, dev, client = registry.find(did)
        return {"status": out.get("status"), "endpoint_id": endpoint_id, "unit": did,
                "device": _device_entry(dev, client) if dev is not None else None,
                "restarted": out.get("restarted", [])}

    @r.get("/api/endpoints/{endpoint_id}/template-changes")
    def endpoint_template_changes(endpoint_id: str):
        """What "Update from template" would change on this installation's
        units — nothing is written. The page offers the update only when
        ``pending``, and shows exactly this list before it is applied."""
        from multibus.catalog.template_refresh import plan_devices
        if config.get_raw_endpoint(endpoint_id) is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        return plan_devices(config, template_registry, config.endpoint_devices(endpoint_id))

    @r.get("/api/devices/{device_id}/template-changes")
    def device_template_changes(device_id: str):
        """The same for one standalone device."""
        from multibus.catalog.template_refresh import plan_devices
        _idx, dev_cfg, _client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        if dev_cfg.primary:
            return {"pending": False, "units_total": 1, "units_affected": 0,
                    "templates": [], "changes": [], "new": [], "missing_templates": []}
        return plan_devices(config, template_registry, [dev_cfg])

    @r.post("/api/endpoints/{endpoint_id}/refresh-from-template")
    @ctx.serialized_mutation
    def refresh_endpoint_from_template(endpoint_id: str, add_new: bool = False):
        """Bring every unit's register metadata (labels, units, categories,
        bank totals) up to date with its template — the selection, dashboard
        flags, sinks and calculated expressions stay the operator's. The units
        restart so the new metadata reaches the pipeline."""
        from multibus.catalog.template_refresh import refresh_device
        if config.get_raw_endpoint(endpoint_id) is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        made = config.endpoint_devices(endpoint_id)
        _stop_endpoint_devices(endpoint_id)
        try:
            results = [refresh_device(config, template_registry, d, add_new=add_new) for d in made]
        finally:
            _start_endpoint_devices(config.endpoint_devices(endpoint_id))
            # the stop withdrew the units' HA entities — publish them again
            _sync_device_discovery()
        logger.info("endpoint %s: updated from template — %d register rows, "
                    "%d calculated rows changed, %d new calculated fields added "
                    "across %d units", endpoint_id,
                    sum(r['registers'] for r in results), sum(r['calculated'] for r in results),
                    sum(r['added'] for r in results), len(results))
        return {"status": "refreshed", "units": results}

    @r.post("/api/devices/{device_id}/refresh-from-template")
    @ctx.device_write
    @ctx.serialized_mutation
    def refresh_device_from_template(device_id: str, add_new: bool = False):
        """The same for one device (an installation's unit refreshes through
        its installation, so every unit stays alike)."""
        from multibus.catalog.template_refresh import refresh_device
        _idx, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        if dev_cfg.endpoint_id:
            raise HTTPException(status_code=422, detail={"errors": [
                f"'{device_id}' belongs to installation '{dev_cfg.endpoint_id}' — "
                "update it from the installation"]})
        if dev_cfg.primary:
            raise HTTPException(status_code=422, detail={"errors": [
                "the primary device's map is edited in Measurements"]})
        res = refresh_device(config, template_registry, dev_cfg, add_new=add_new)
        if client:
            client.disconnect()
        new_client = _start_device_client(dev_cfg)
        registry.replace(device_id, dev_cfg, client=new_client, add_if_missing=True)
        _sync_device_discovery()            # HA picks up the new labels
        logger.info("device %s: updated from template — %d register rows, %d calculated "
                    "rows changed, %d new calculated fields added", device_id,
                    res['registers'], res['calculated'], res['added'])
        return {"status": "refreshed", **res}

    @r.delete("/api/endpoints/{endpoint_id}")
    def delete_endpoint(endpoint_id: str, dry_run: bool = False):
        """Delete an endpoint and stop all its units (their register files are
        kept on disk, so re-adding the endpoint restores the selection).
        Refused (409) while a rule targets or reads it or one of its units,
        or a sequence, a formula or a virtual meter uses a unit (asked
        outside the device lock, then again under it)."""
        if config.get_raw_endpoint(endpoint_id) is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        ans = _refuse_if_used('installation', endpoint_id, f"installation '{endpoint_id}'", dry_run)
        if ans is not None:
            return ans
        return _delete_endpoint_locked(endpoint_id)

    return r
