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
"""Device CRUD — list, create, restore, update, patch, delete and the single
device view.

Nine endpoints moved VERBATIM out of create_api (P0.8b Lot G) into a
``build(ctx) -> APIRouter`` module. ``get_device`` is the catch-all
``GET /api/devices/{device_id}`` and is defined LAST so the specific
``/api/devices/restorable`` route (defined earlier in this same module) is not
shadowed; the module is included last. Bodies are byte-identical;
@_device_write / @_device_runtime / @_serialized_mutation / @_field_errors become
the ctx form. The many create_api collaborators are aliased from ctx; the device
helpers shared with other modules stay in create_api. ``create_device`` is also
reached by mqtt_discovery_adopt through ctx.create_device, so it is re-exposed on
ctx here. The logger keeps the name ``multibus.api``.
"""
from __future__ import annotations

import copy
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple   # noqa: F401 — verbatim bodies

from fastapi import APIRouter, Body, HTTPException, Request, Response

from multibus.catalog.device_template import mppt_count_of as _mppt_count_of
from multibus.core.config import heartbeat_seconds
from multibus.devices.save_merge import BridgeConflict
from multibus.payload import flag

logger = logging.getLogger("multibus.api")


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["devices"])

    config = ctx.config
    registry = ctx.registry
    audit_log = ctx.audit_log
    template_registry = ctx.template_registry
    attributes = ctx.attributes
    _find_device = ctx.find_device
    _sync_device_discovery = ctx.sync_device_discovery
    _apply_rest_push = ctx.apply_rest_push
    _component_view = ctx.component_view
    _device_entry = ctx.device_entry
    _errors_detail = ctx.errors_detail
    _kind_of_payload = ctx.kind_of_payload
    _opcua_block = ctx.opcua_block
    _publish_attribute_meta = ctx.publish_attribute_meta
    _refuse_busy_poll_groups = ctx.refuse_busy_poll_groups
    _refuse_if_used = ctx.refuse_if_used
    _reload_poll_groups = ctx.reload_poll_groups
    _restart_devices = ctx.restart_devices
    _set_new_node = ctx.set_new_node
    _start_device_client = ctx.start_device_client
    _start_new_component = ctx.start_new_component
    _dry_run_connection = ctx.validate_connection
    _REST_HDR_MASK = ctx.rest_hdr_mask
    _dv = ctx.dv
    _validate_device_payload = _dv.validate_device_payload
    _device_claim_check = _dv.device_claim_check
    _apply_routing_defaults = ctx.apply_routing_defaults
    _autoselect_template_registers = ctx.autoselect_template_registers
    _built_sig = ctx.built_sig
    _components_on_create = ctx.components_on_create
    _delete_device_locked = ctx.delete_device_locked
    _device_deletable = ctx.device_deletable
    _device_etag = ctx.device_etag
    _device_put_raw = ctx.device_put_raw
    _drop_echoed_defaults = ctx.drop_echoed_defaults
    _drop_spec_keys = ctx.drop_spec_keys
    _ensure_device_bucket = ctx.ensure_device_bucket
    _patch_locks = ctx.patch_locks
    _patch_primary = ctx.patch_primary
    _poll_groups_now = ctx.poll_groups_now
    _raw_index = ctx.raw_index
    _refuse_prefix = ctx.refuse_prefix
    _req_ip = ctx.req_ip
    _req_user = ctx.req_user
    _restore_plan = ctx.restore_plan
    _restore_resets = ctx.restore_resets
    _restore_resumes = ctx.restore_resumes
    _update_device_locked = ctx.update_device_locked
    _would_be_entry = ctx.would_be_entry
    _LIVE_FIELDS = ctx.live_fields
    _COUNTER_RESETS_KEPT = ctx.counter_resets_kept

    @r.get("/api/devices")
    def list_devices(request: Request, q: Optional[str] = None, kind: Optional[str] = None,
                     state: Optional[str] = None, protocol: Optional[str] = None,
                     installation: Optional[str] = None, sort: Optional[str] = None,
                     type: Optional[str] = None, label: Optional[str] = None):  # noqa: A002
        """All southbound devices with live health (device #1 first).
        Optional: ``q`` (search), ``kind``, ``type`` (the device type, a user
        type included), ``label`` (its identity labels), ``state``
        (online/offline/disabled), ``protocol``, ``installation`` — each a
        comma list — and ``sort`` (name, id, kind, state, protocol; ``-`` for
        descending)."""
        # only admin sees raw credentials; viewer AND operator get redacted URLs
        _redact = getattr(request.state, "role", None) != "admin" and getattr(request.state, "role", None) is not None
        _idx = _raw_index()
        from collections import Counter
        _counts = Counter(d.component_of for d in config.devices if d.component_of)  # PERF-12: once per list
        from multibus.devices.device_summary import Claims
        _claims = Claims(config, template_registry)        # the bus claims, once per list
        entries = [(d, _device_entry(d, c, redact=_redact, raw_index=_idx, claims=_claims, comp_counts=_counts))
                   for d, c in registry]
        if all(v in (None, '') for v in (q, kind, state, protocol, installation, sort, type, label)):
            return {"devices": [e for _d, e in entries]}
        from multibus.list_filters import FilterError, device_protocols, device_search_text, filter_devices
        rows = [(e, device_protocols(d), device_search_text(d, e, template_registry, redact=_redact))
                for d, e in entries]
        try:
            return {"devices": filter_devices(rows, q=q, kind=kind, state=state, protocol=protocol,
                                              installation=installation, sort=sort, type=type, label=label)}
        except FilterError as err:
            raise HTTPException(status_code=422, detail={"errors": [str(err)], "fields": err.fields})


    @r.post("/api/devices")
    @ctx.serialized_mutation
    @ctx.field_errors
    def create_device(payload: Dict = Body(...), dry_run: bool = False, only: Optional[str] = None,
                      request: Request = None):
        """Create a device: validate → persist → auto-select its template
        registers + ensure its InfluxDB bucket → hot-start its poller and
        publish HA discovery (no restart). `?dry_run=true`: everything a save
        checks, nothing stored or started — {status: valid, would: create,
        entry: the would-be entry (ADR-0031 §5)}. `?dry_run=true&only=connection`:
        the connection alone (fields, matrix, bus claims, Seplos mode),
        without an id, a name or a template (L3-F3). ``components``
        ([{type, instance, name?}]) are created with the device, all or
        nothing.
        (The id is checked against every device, unit and installation; a
        component the new device declares is checked for a clash only by the
        real save.) A ``type`` (or ``kind``, its older word) sent is filed as
        the device's type (as ``/organise`` would); later edits keep it and
        only ``/organise`` changes it. ``attributes`` ({category: value}) are
        stored with the device; refused, nothing is created (ADR-0027)."""
        from multibus.catalog import device_types as _dtypes
        if only is not None:
            if only != 'connection' or not dry_run:
                _msg = "only=connection goes with dry_run=true"
                raise HTTPException(status_code=422, detail={"errors": [f"only: {_msg}"], "fields": {"only": _msg}})
            return _dry_run_connection(payload)
        # how Add's Check ended (L3.9-F3): the create's word only, for its
        # audit line — never stored on the device
        _check, _check_job = None, None
        if isinstance(payload, dict) and ('check' in payload or 'check_job' in payload):
            payload = dict(payload)
            _check, _check_job = payload.pop('check', None), payload.pop('check_job', None)
            if _check is not None and _check not in ('ok', 'failed', 'skipped'):
                _m = "check: ok, failed or skipped (how Add's Check ended)"
                raise HTTPException(status_code=422, detail={"errors": [_m], "fields": {"check": _m.split(': ', 1)[1]}})
            if _check_job is not None and not (isinstance(_check_job, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', _check_job)):
                _m = "check_job: the preview job's id (letters, digits, - and _)"
                raise HTTPException(status_code=422, detail={"errors": [_m], "fields": {"check_job": _m.split(': ', 1)[1]}})
        payload = _drop_spec_keys(payload)
        raw = _apply_routing_defaults(_validate_device_payload(payload))
        _set_new_node(raw['mqtt'], raw['id'])
        _refuse_prefix('mqtt.topic_prefix', raw['mqtt']['topic_prefix'].replace('${device_id}', raw['id'])
                       .replace('${id}', raw['id']), {f"device:{raw['id']}"})
        _k, _why = _dtypes.from_payload(payload)
        if _k is not None or _why:
            _field = 'type' if isinstance(payload, dict) and 'type' in payload else 'kind'
            _why = _why or _dtypes.override_error(_k)
            if _why:
                raise HTTPException(status_code=422, detail={"errors": [f"{_field}: {_why}"],
                                                             "fields": {_field: _why}})
            raw['kind'] = _dtypes.stored_form(_k)
        _attrs = None
        if isinstance(payload, dict) and payload.get('attributes') is not None:
            _tpl = template_registry.get(raw.get('template') or '') if raw.get('template') else None
            _attrs, _aerr = attributes.check_many('device', payload['attributes'],
                                                  mppt_count=(_mppt_count_of(_tpl) or None) if _tpl else None)
            if _aerr:
                raise HTTPException(status_code=422, detail={"errors": _aerr, "fields": {"attributes": '; '.join(_aerr)}})
            if _attrs and attributes.store.broken:
                raise HTTPException(status_code=503, detail={"errors": [
                    "attributes.yaml cannot be read — fix or restore it, or create the device without attributes"]})
        _comps = _components_on_create(raw, payload)
        if _comps:
            raw['components'] = _comps
        _device_claim_check(raw, raw['id'])
        _why = config.raw_device_refusal(raw['id'])     # what the store refuses, dry run or not
        if _why:
            raise HTTPException(status_code=422, detail={"errors": [_why]})
        if dry_run:
            if not config.preview_devices(device=raw):
                raise HTTPException(status_code=422, detail={"errors": [
                    f"device {raw['id']!r} could not be built from the given data"]})
            return {"status": "valid", "would": "create", "entry": _would_be_entry(raw)}
        # a new device is not served on OPC UA until someone says so (its
        # Outputs tab): a SCADA sees what it was given, not every new device
        raw.setdefault('opcua', {'enabled': False})
        # …and writes nothing until an admin unlocks it (/write-lock),
        # whatever the payload says (ADR-0025, ADD-12)
        raw['write_locked'] = True
        try:
            dev_cfg = config.upsert_raw_device(raw)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        if _comps:
            # all or nothing: a component the store could not build takes
            # its parent back with it
            _lost = [c['id'] for c in _comps if getattr(config.get_device(c['id']), 'component_of', None) != dev_cfg.id]
            if _lost:
                try:
                    config.discard_raw_device(dev_cfg.id)
                except Exception:  # noqa: BLE001
                    logger.exception("device %s: rollback failed", dev_cfg.id)
                raise HTTPException(status_code=422, detail={"errors": [
                    f"components: {', '.join(_lost)} could not be built (see the log) — nothing was created"]})
        _autoselect_template_registers(dev_cfg)   # before start → poller loads them
        # a NEW device at an old id: nothing an earlier device kept survives
        # (as for its registers); a restore keeps them
        try:
            attributes.drop('device', dev_cfg.id, reason='a new device at this id')
        except Exception:  # noqa: BLE001 — the device is saved; its attributes must not fail it
            logger.exception("attributes: could not clear what %s held", dev_cfg.id)
        if _attrs:
            try:
                attributes.put_many('device', dev_cfg.id, _attrs, who=_req_user(request), ip=_req_ip(request))
            except Exception as e:  # noqa: BLE001 — checked above: only a file that broke meanwhile
                # all or nothing: the device goes again
                logger.exception("attributes of %s could not be stored: the device is not created", dev_cfg.id)
                try:
                    config.discard_raw_device(dev_cfg.id)      # no tombstone: an older one stays as it was
                except Exception:  # noqa: BLE001
                    logger.exception("device %s: rollback failed", dev_cfg.id)
                raise HTTPException(status_code=503, detail={"errors": [
                    f"its attributes could not be stored ({e}) — nothing was created"]})
        _ensure_device_bucket(dev_cfg)
        client = _start_device_client(dev_cfg)
        registry.add(dev_cfg, client)
        _made = [_start_new_component(config.get_device(c['id'])) for c in _comps]
        _sync_device_discovery()
        logger.info(f"device {dev_cfg.id}: created "
                    f"({dev_cfg.protocol}, template={dev_cfg.template or '—'})"
                    + (f" with {len(_made)} component(s)" if _made else ""))
        try:
            audit_log.append(user=_req_user(request), ip=_req_ip(request), action="device created",
                             target=dev_cfg.id, status="ok", detail={
                                 "check": _check or "not said", "check_job": _check_job,
                                 "template": dev_cfg.template or "", "protocol": dev_cfg.protocol,
                                 "components": len(_made)})
        except Exception:  # noqa: BLE001 — the device is made; its trail line must not fail it
            logger.exception("audit: device %s created, line not written", dev_cfg.id)
        out = {"status": "created", "device": _device_entry(dev_cfg, client)}
        if _made:
            out["components"] = [_component_view(c) for c in _made]
        return out


    @r.get("/api/devices/restorable")
    def list_restorable_devices(check: bool = False):
        """Deleted devices whose full definition was kept (id no longer active).
        Restoring one rebuilds the exact device — connection, template AND its
        register selection — instead of starting from scratch.
        ``?check=true``: each also says, before the click, what a restore
        would refuse (``problems``, the restore's own words; [] = it can come
        back), what it would set back to safe (``resets``) and what it would
        start again (``resumes``). One kept device that cannot even be read
        says so in its own row; the list stands (review M2). The checks run
        outside the device lock: read-only, but one that runs during a
        mutation may report a passing problem (review n1)."""
        items = config.list_deleted_devices()
        if check:
            for it in items:
                try:
                    _restore_plan(it['id'])
                    it['problems'] = []
                except HTTPException as e:
                    det = e.detail
                    it['problems'] = ([str(x) for x in det.get('errors') or []] if isinstance(det, dict)
                                      else [str(det)])
                except Exception:  # noqa: BLE001 — a malformed kept device: said in its row
                    logger.warning("restorable %s: kept settings unreadable", it['id'], exc_info=True)
                    it['problems'] = ["its kept settings are damaged and cannot be restored — Forget them"]
                try:
                    raw = config.load_deleted_device(it['id'])
                    it['resets'] = _restore_resets(raw)
                    it['resumes'] = _restore_resumes(raw)
                except Exception:  # noqa: BLE001
                    it['resets'], it['resumes'] = {}, []
        return {"devices": items}


    @r.post("/api/devices/{device_id}/restore")
    @ctx.device_write
    @ctx.serialized_mutation
    def restore_device(device_id: str, deleted_ts: Optional[float] = None):
        """Re-create a previously deleted device from its kept tombstone. Its
        selected-registers file (kept on disk) is loaded by the poller, so the
        measurement selection returns intact; its attributes were kept. What
        ran beside it starts again (its REST push; its retained attributes
        meta follows as before). ``reset``: what came back set to safe instead of as it was;
        ``resumed``: what started again. ``deleted_ts`` (an Undo): the delete
        it undoes — a device kept by a later delete is not restored in its
        place (409 ``tombstone_changed``, review m1)."""
        raw = _restore_plan(device_id)
        if deleted_ts is not None:
            kept = config.deleted_device_ts(device_id)
            if kept is None or abs(kept - float(deleted_ts)) > 0.0005:
                raise HTTPException(status_code=409, detail={"errors": [
                    f"'{device_id}' was deleted again since this Undo was offered: its kept settings are another "
                    "device's — see Deleted devices"], "code": "tombstone_changed"})
        kept_raw = config.load_deleted_device(device_id)
        resets, resumes = _restore_resets(kept_raw), _restore_resumes(kept_raw)
        try:
            dev_cfg = config.upsert_raw_device(raw)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        # registers file is already on disk → _autoselect skips (keeps the
        # user's exact selection); only seeds if the file was lost.
        for d in [dev_cfg] + config.component_devices(dev_cfg.id):
            _autoselect_template_registers(d)
            _ensure_device_bucket(d)
            registry.add(d, _start_device_client(d))
            _apply_rest_push(d)                   # the delete stopped it (L3.11c)
        client = registry.find(dev_cfg.id)[2]
        _sync_device_discovery()
        logger.info(f"device {device_id}: restored from tombstone")
        return {"status": "restored", "device": _device_entry(dev_cfg, client), "reset": resets,
                "resumed": resumes}


    @r.delete("/api/devices/restorable/{device_id}")
    @ctx.serialized_mutation
    def forget_restorable_device(device_id: str):
        """Permanently drop a deleted device's kept settings (tombstone +
        registers). Irreversible; refuses to touch an active device."""
        try:
            kept = config.load_deleted_device(device_id) or {}
        except ValueError:
            kept = {}
        try:
            ok = config.forget_deleted_device(device_id)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        if not ok:
            raise HTTPException(status_code=404, detail="nothing to forget for that id")
        # its components kept their measurements beside it
        for c in config.component_entries(kept) if kept.get('components') else []:
            try:
                config.forget_deleted_device(c['id'])
                attributes.drop('device', c['id'], reason='forgotten')
            except Exception:  # noqa: BLE001 — an active id or a bad one: leave it
                pass
        try:
            attributes.drop('device', device_id, reason='forgotten')
        except Exception:  # noqa: BLE001
            logger.exception("attributes: could not drop what %s held", device_id)
        return {"status": "forgotten", "id": device_id}


    @r.put("/api/devices/{device_id}")
    @ctx.device_write
    @ctx.serialized_mutation
    @ctx.field_errors
    def update_device(device_id: str, payload: Dict = Body(...), dry_run: bool = False):
        """Update a device: stop its poller, persist, restart. The primary
        (UMG512) is editable too — its connection maps to the flat Modbus
        config; its routing identity stays fixed. `?dry_run=true`: everything
        the save checks, nothing stored, nothing restarted — {status: valid,
        would: unchanged | update | update_restart} (whether it would restart
        acquisition)."""
        payload = _drop_spec_keys(payload)
        idx, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        # a stale If-Match was answered 412 by @ctx.device_write, under the lock
        return _update_device_locked(device_id, payload, dry_run)


    @r.patch("/api/devices/{device_id}")
    @ctx.device_write
    @ctx.serialized_mutation
    @ctx.field_errors
    def patch_device(device_id: str, request: Request, payload: Dict = Body(...), dry_run: bool = False,
                     intent: Optional[str] = None):
        """Edit a device in sections (ADR-0031 §4–§5): {sections: {connection?,
        polling?: {enabled, poll_groups}, identity?: {name, type}, outputs?:
        {mqtt, influxdb, ha_discovery, http_output, opcua, rest_push},
        model?: {template}}}. ``If-Match`` is required (428 without; a stale
        one 412). Every section is judged by the validator its own route
        uses; then one config write, one audit record, one restart decision
        (acquisition restarts only when what it runs on changes; outputs and
        the name apply live). `?dry_run=true` stores nothing and answers
        {status, would, changes: [{section, field, from, to}],
        restart_required, effects, locks}; it does not need If-Match.
        `?intent=replace` (Replace hardware) also writes an energy-counter
        reset marker (``energy_counter_resets``). The write lock is not a
        section (its own route). The primary meter takes the same body
        through an adapter onto its flat config (_patch_primary, ADR-0032 §6):
        what applies to it is GET's ``edit_sections``, the rest is refused
        per field in the words of its ``locks``."""
        from multibus.devices import device_patch as _dp
        _i, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        if not dry_run and request.headers.get('if-match') is None:
            raise HTTPException(status_code=428, detail={"errors": [
                "If-Match: required — send the ETag of GET /api/devices/{id}; a PATCH never overwrites a "
                "version it did not read"]})
        if intent not in (None, '', 'replace'):
            raise HTTPException(status_code=422, detail={"errors": ["intent: replace, or none"],
                                                         "fields": {"intent": "replace, or none"}})
        try:
            secs = _dp.parse(payload)
        except _dp.PatchError as e:
            raise HTTPException(status_code=422, detail=_errors_detail(e.errors))
        if dev_cfg.primary:
            return _patch_primary(dev_cfg, secs, dry_run, intent)
        if dev_cfg.component_of:
            raise HTTPException(status_code=422, detail={"errors": [
                f"'{device_id}' is a component of '{dev_cfg.component_of}' — manage it from that device"]})
        _why = config.raw_device_refusal(device_id)     # an installation's unit
        if _why:
            raise HTTPException(status_code=422, detail={"errors": [_why]})
        prev = copy.deepcopy(config.get_raw_device(device_id) or {})
        errors: List[str] = []
        ident, model, pol = secs.get('identity', {}), secs.get('model', {}), secs.get('polling', {})
        put: Dict[str, Any] = {}
        if 'connection' in secs:
            put['connection'] = secs['connection']
        if 'name' in ident:
            if not isinstance(ident['name'], str) or len(ident['name'].strip()) > 80:
                errors.append("identity.name: text, up to 80 characters ('' = the id)")
            else:
                put['name'] = ident['name'].strip()
        if 'template' in model:
            if not isinstance(model['template'], str):
                errors.append("model.template: a template id")
            else:
                put['template'] = model['template']
        if 'enabled' in pol:
            if not isinstance(pol['enabled'], bool):
                errors.append("polling.enabled: true or false")
            else:
                put['enabled'] = pol['enabled']
        if errors:
            raise HTTPException(status_code=422, detail=_errors_detail(errors))
        before = prev
        raw = copy.deepcopy(prev)
        if put:
            try:
                raw = _device_put_raw(device_id, dev_cfg, _drop_spec_keys(put), prev)
            except BridgeConflict as e:
                raise HTTPException(status_code=422, detail={"errors": [str(e)]})
            try:
                base = _device_put_raw(device_id, dev_cfg, copy.deepcopy(prev), prev)
            except Exception as e:  # noqa: BLE001 — the comparison never fails a save
                logger.debug("device %s: unchanged-check skipped (%s)", device_id, e)
                base = None
            if base is not None:
                raw = _drop_echoed_defaults(raw, base)
                if raw == base:
                    raw = copy.deepcopy(prev)    # said what it was: the entry is not rewritten
                else:
                    before = base
        # identity.type: the rule of /organise
        if 'type' in ident:
            kind, why = _kind_of_payload(dev_cfg, {'type': ident['type']})
            if why:
                errors.append(f"identity.type: {why}")
            elif kind:
                raw['kind'] = kind
            else:
                raw.pop('kind', None)
        plan: Dict[str, Any] = {}
        if 'outputs' in secs:
            plan, oerr = _dp.outputs_plan(secs['outputs'], old_rest_headers=dev_cfg.rest_push.get('headers') or {},
                                          mask=_REST_HDR_MASK, flag=flag, heartbeat_ok=heartbeat_seconds,
                                          opcua_block=_opcua_block)
            errors += oerr
        pg_clean: Dict = {}
        pg_changes: List[Dict] = []
        if 'poll_groups' in pol:
            pg_clean, why = _dp.poll_groups_clean(pol['poll_groups'])
            if why:
                errors.append(f"polling.poll_groups: {why}")
        if errors:
            raise HTTPException(status_code=422, detail=_errors_detail(errors))
        if pg_clean:
            _refuse_busy_poll_groups(dev_cfg, pg_clean, field='polling.poll_groups')
            pg_changes = _dp.poll_group_changes(_poll_groups_now(dev_cfg), pg_clean)
        _dp.apply_outputs(raw, plan)
        if intent == 'replace':
            import datetime as _dt
            now = time.time()
            marks = [m for m in (raw.get('energy_counter_resets') or []) if isinstance(m, dict)]
            marks.append({'at': _dt.datetime.fromtimestamp(now, _dt.timezone.utc).isoformat(timespec='seconds'),
                          'ts': round(now, 3), 'reason': 'replace'})
            raw['energy_counter_resets'] = marks[-_COUNTER_RESETS_KEPT:]
        changes = _dp.changes(before, raw) + pg_changes
        entry_changed = raw != prev
        built = config.preview_devices(device=raw)
        if not built:
            raise HTTPException(status_code=422, detail={"errors": [
                f"device {device_id!r} could not be built from the given data"]})
        # a device that should run and does not: its connection saved is how
        # one reconnects it, changed or not (as PUT)
        reconnect = bool(dev_cfg.enabled and client is None and 'connection' in secs)
        old_sig = _built_sig(config.preview_devices(device=prev), _LIVE_FIELDS)
        new_sig = _built_sig(built, _LIVE_FIELDS)
        restart = reconnect or (entry_changed and (old_sig is None or new_sig is None or old_sig != new_sig))
        comps = [d.id for d in config.component_devices(device_id)]
        effects: List[str] = []
        if restart:
            why = sorted({c['section'] for c in changes if c['section'] in ('connection', 'model', 'polling')})
            effects.append("acquisition restarts" + (f" ({', '.join(why)})" if why else
                                                     " (it should run and does not: it reconnects)"))
            if comps:
                effects.append(f"its {len(comps)} component(s) restart with it")
        elif changes:
            effects.append("applies live — acquisition keeps running")
        if 'template' in put and any(c['field'] == 'template' for c in changes):
            effects.append("the new template's registers are selected where the device has none selected")
        if plan.get('sinks') and ({'mqtt', 'ha_discovery'} & set(plan['sinks'])):
            effects.append("Home Assistant discovery is published again")
        if 'rest_push' in plan and any(c['field'].startswith('rest_push') for c in changes):
            effects.append("the REST push restarts with its new settings")
        if pg_changes:
            effects.append("the new poll intervals apply at once")
        if intent == 'replace':
            effects.append("an energy-counter reset marker is written: the new meter's counters start a new "
                           "series (the Energy tab does not read it yet)")
        would = 'update_restart' if restart else ('update' if changes else 'unchanged')
        if dry_run:
            return {"status": "valid", "would": would, "changes": changes, "restart_required": restart,
                    "effects": effects, "locks": _patch_locks(dev_cfg)}
        if not changes and not restart:
            return {"status": "unchanged", "restarted": False, "changes": [],
                    "device": _device_entry(dev_cfg, client)}
        if pg_changes:
            # the intervals (already checked) live in the device's register
            # file: written FIRST, so a file that cannot be written stores
            # nothing at all — never an entry saved without its intervals
            try:
                config.save_device_poll_groups(device_id, pg_clean)
            except Exception as e:  # noqa: BLE001 — a corrupt file, a full disk: said, nothing stored
                logger.warning("device %s: poll intervals not written (%s) — the edit is not saved", device_id, e)
                raise HTTPException(status_code=422 if isinstance(e, ValueError) else 503, detail={
                    "errors": [f"polling.poll_groups: the register file could not be written ({type(e).__name__}: "
                               f"{e}) — nothing was saved"],
                    "fields": {"polling.poll_groups": "could not be written"}})
        new_cfg = dev_cfg
        if entry_changed:
            try:
                new_cfg = config.upsert_raw_device(raw)        # the one config write
            except ValueError as e:
                det: Dict[str, Any] = {"errors": [str(e)]}
                if pg_changes:
                    # the intervals went in: they apply, and the answer says so
                    _reload_poll_groups(dev_cfg, client)
                    det["partial"] = {"saved": ["polling.poll_groups"], "failed": ["entry"]}
                raise HTTPException(status_code=422, detail=det)
        if restart:
            if client:
                client.disconnect()
            _autoselect_template_registers(new_cfg)
            _ensure_device_bucket(new_cfg)
            new_client = _start_device_client(new_cfg)
            registry.replace(device_id, new_cfg, client=new_client, add_if_missing=True)
            _apply_rest_push(new_cfg)
            _restart_devices(comps)
        else:
            registry.replace(device_id, new_cfg)            # keep the running client
            new_client = registry.find(device_id)[2]
            if 'rest_push' in plan:
                _apply_rest_push(new_cfg)
            if pg_changes:
                _reload_poll_groups(new_cfg, new_client)
        _sync_device_discovery()
        if 'type' in ident:
            try:
                attributes.invalidate()
                _publish_attribute_meta()
            except Exception:  # noqa: BLE001 — publishing never fails a save
                logger.exception("attributes meta after a type change")
        logger.info(f"device {device_id}: patched ({', '.join(sorted({c['section'] for c in changes}))})"
                    f"{' — restarted' if restart else ' — kept running'}"
                    f"{' [replace: counter reset marker]' if intent == 'replace' else ''}")
        return {"status": "updated", "restarted": restart, "changes": changes,
                "device": _device_entry(new_cfg, new_client)}


    @r.delete("/api/devices/{device_id}")
    @ctx.device_write
    def delete_device(device_id: str, request: Request, dry_run: bool = False):
        """Delete a non-primary device (its selected-registers file is kept
        on disk for safety). Refused (409) while a rule, a sequence, another
        device's formula, a device it measures or a virtual meter uses it or
        one of its components. `?dry_run=true`: the same checks, nothing
        removed.

        Asked first outside the device lock (a dry run never takes it, and a
        disable or a write lock never waits on the walk), then asked again
        under the lock, where the delete happens."""
        _device_deletable(device_id)
        _if_match = request.headers.get('if-match')     # checked by @ctx.device_write; again under the lock
        ans = _refuse_if_used('device', device_id, f"'{device_id}'", dry_run)
        if ans is not None:
            return ans
        return _delete_device_locked(device_id, _if_match)


    @r.get("/api/devices/{device_id}")
    def get_device(device_id: str, request: Request, response: Response):
        """One device's entry (as in GET /api/devices), with its
        ``connection_spec``, and its ``ETag`` (what ``If-Match`` names on
        PUT / DELETE)."""
        _i, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        _role = getattr(request.state, "role", None)
        response.headers['ETag'] = _device_etag(dev_cfg)
        return {"device": _device_entry(dev_cfg, client, redact=_role is not None and _role != "admin")}


    # mqtt_discovery_adopt reuses the device-create path; it reads ctx.create_device
    # at request time, so exposing the (fully decorated) handler here is enough,
    # regardless of router include order.
    ctx.create_device = create_device
    return r
