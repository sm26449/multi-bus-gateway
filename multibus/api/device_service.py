# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Device CRUD / lifecycle, extracted whole from create_api (P0.8b squeeze).

DeviceService holds the device create/update/patch/delete orchestration, the primary-
connection handling, the restore/teardown/dependents logic and the component CRUD. Kept as
closures built in __init__ so the bodies are verbatim; collaborators are an explicit keyword
constructor. Layer-clean intermediate in the api layer (still raises HTTPException, like
DeviceValidator/EndpointService). NOTE: ~1.4k lines — to be split by sub-responsibility
(create_update / primary / lifecycle) in phase 1; this move is the create_api <600 squeeze.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import HTTPException

from multibus.core.config import SERIAL_PROTOCOLS, VIRTUAL_MAP_SERIAL, heartbeat_seconds
from multibus.payload import flag
from multibus.transports.driver import DeviceDriver
from multibus.devices.device_validation import _is_serial_path, _serial_host_error
from multibus.api.validation import _DEVICE_ID_RE, validate_commands, validate_channels
from multibus.outputs.vbms_registry import taken_by_vbms, vbms_id_error
from multibus.devices.save_merge import MASKED, BridgeConflict, keep_unsaid, merge_connection

logger = logging.getLogger("multibus.api")  # preserve caplog logger name


class DeviceService:
    """The device CRUD / lifecycle seam (ADR-0038 service, api-layer for now)."""

    def __init__(self, *, app, config, registry, template_registry, attributes, calc_engine, calc_runners, modbus_client, _pub, _lease_mgr, rest_push_manager, _device_mutation_lock, _fields_cache, _meta_sent, _write_rl_lock, _write_rl_state, _DEVICE_OWN_KEYS, _FIELD_PATH_RE, _REST_HDR_MASK, _check_if_match, _entry_lock, _device_specs, _stored_device_raw, _primary_kept, _device_type_of, _opcua_block, _drop_spec_keys, _set_new_node, _apply_rest_push, _find_device, _start_device_client, _sync_device_discovery, _device_claim_check, _validate_device_payload, _timeout_error, _device_entry, _cancel_device_refreshes, _restart_devices):
        def _apply_routing_defaults(raw: Dict) -> Dict:
            """Fill missing topic prefix / bucket from the configured {device}
            patterns so a new device always has sane routing."""
            did = raw['id']
            raw.setdefault('mqtt', {})
            if not raw['mqtt'].get('topic_prefix'):
                raw['mqtt']['topic_prefix'] = config.default_topic_prefix(did)
            raw.setdefault('influxdb', {})
            if not raw['influxdb'].get('bucket'):
                raw['influxdb']['bucket'] = config.default_bucket(did)
            return raw

        def _device_put_raw(device_id: str, dev_cfg, payload: Dict, prev: Dict) -> Dict:
            """What saving ``payload`` over the stored device writes: the stored
            connection merged under the sent one (save_merge.py), validated, with
            everything the payload did not speak of kept and the routing identity
            pinned. Raises HTTPException / BridgeConflict."""
            payload = dict(payload)
            for k in ('name', 'template', 'enabled'):
                if k not in payload and k in prev:
                    payload[k] = prev[k]
            conn = payload.get('connection')
            if isinstance(conn, dict):
                conn = dict(conn)
                if conn.get('password') in ('', MASKED):
                    conn.pop('password')               # echoed masked / left blank = keep
                old_h = (prev.get('connection') or {}).get('headers') or {}
                if isinstance(conn.get('headers'), dict):
                    # a masked echo is the stored value; masked with nothing stored is nothing
                    conn['headers'] = {k: (old_h[k] if v == MASKED else v)
                                       for k, v in conn['headers'].items() if v != MASKED or k in old_h}
            payload['connection'] = merge_connection(conn, prev.get('connection'), config.bridges)
            raw = _apply_routing_defaults(_validate_device_payload(payload, existing_id=device_id,
                                                                   stored_conn=prev.get('connection')))
            raw = keep_unsaid(raw, prev, payload, own=_DEVICE_OWN_KEYS)
            _device_claim_check(raw, device_id, prev)
            # Routing identity (topic prefix / bucket / Influx tag) is FIXED after
            # creation — changing it would re-route future data and orphan the
            # device's history + Home Assistant entities (the enable flags and
            # HA discovery stay editable); an API caller cannot bypass it either.
            raw.setdefault('mqtt', {})['topic_prefix'] = dev_cfg.mqtt_topic_prefix
            # …and so is its Home Assistant node (stored, or none: the legacy one)
            _node = ((prev.get('mqtt') or {}).get('ha_node'))
            if _node:
                raw['mqtt']['ha_node'] = _node
            else:
                raw['mqtt'].pop('ha_node', None)
            raw.setdefault('influxdb', {})['bucket'] = dev_cfg.influxdb_bucket
            raw['influxdb']['device_tag'] = dev_cfg.influxdb_device_tag
            return raw

        def _built_sig(cfgs, also_skip: Tuple[str, ...] = ()) -> Optional[str]:
            """What a set of devices RUNS on — every built field but the name and
            the kind (labels, refreshed in place) and ``also_skip`` — or None when
            it cannot be told."""
            def _runs_on(d: Dict) -> Dict:
                # a block another protocol reads means nothing here (a TCP device's
                # `http` echoes its timeout — not a reason to restart it)
                proto = str(d.get('protocol', ''))
                for k, keep in (('http', proto == 'http'), ('mqtt_in', proto == 'mqtt')):
                    if not keep:
                        d.pop(k, None)
                if proto not in SERIAL_PROTOCOLS + ('rtu_tap',) + VIRTUAL_MAP_SERIAL:
                    # a line another protocol never opens (an MQTT device's echoed
                    # `serial_port: ''`) is not what it runs on
                    d.pop('serial', None)
                if d.get('serial'):
                    # the raw line as written; a default spelled out is the same line
                    d['serial'] = {'baudrate': 9600, 'parity': 'N', 'stopbits': 1, 'bytesize': 8, **d['serial']}
                if isinstance(d.get('mqtt_in'), dict):
                    # what the MQTT input reads absent the same as empty / false /
                    # the default port (mqtt_input.py) — not a reason to reconnect
                    mi = dict(d['mqtt_in'])
                    for k in ('username', 'password', 'tls', 'tls_ca', 'tls_insecure', 'accept_retained',
                              'stale_after_s', 'vars'):
                        if k in mi and not mi[k]:
                            mi.pop(k)
                    if str(mi.get('port', '')) == '1883':
                        mi.pop('port')
                    d['mqtt_in'] = mi
                for src in d.get('sources') or []:
                    if isinstance(src, dict):
                        _runs_on(src)
                return d
            try:
                # (a kind and a meter link change what is shown and computed, not
                # what is read)
                skip = ('name', 'kind', 'energy_meter', 'meters', 'meter_options') + tuple(also_skip)
                return json.dumps([_runs_on({k: v for k, v in dataclasses.asdict(c).items() if k not in skip})
                                   for c in cfgs], sort_keys=True, default=str)
            except Exception:  # noqa: BLE001
                return None

        def _drop_echoed_defaults(raw: Dict, base: Dict) -> Dict:
            """A key the stored device does not have, sent with the value the
            device already runs on (a form spells out timeout 3, username '',
            tls false, outputs enabled…), is not written: the first save of an
            untouched device is then `unchanged`, not a rewrite — or, for an MQTT
            input, a reconnect. `base` is the stored device through
            the same save pipeline. Judged on the built device, so a key that
            changes anything stays; the whole built device must match, so the
            shown values are kept by everything that reads them."""
            def _extra(r):
                out = [(None, k) for k in r if k not in base and not isinstance(r[k], dict)]
                for blk in ('connection', 'mqtt', 'influxdb'):
                    if isinstance(r.get(blk), dict):
                        old = base.get(blk) if isinstance(base.get(blk), dict) else {}
                        out += [(blk, k) for k in r[blk] if k not in old]
                return out
            extra = _extra(raw)
            if not extra:
                return raw
            try:
                sig = _built_sig(config.preview_devices(device=raw))
                if sig is None:
                    return raw
                for blk, k in extra:
                    trial = copy.deepcopy(raw)
                    (trial[blk] if blk else trial).pop(k, None)
                    if blk and not trial[blk] and blk not in base:
                        trial.pop(blk)
                    if _built_sig(config.preview_devices(device=trial)) == sig:
                        raw = trial
            except Exception as e:  # noqa: BLE001 — never fails a save
                logger.debug("echoed-default check skipped (%s)", e)
            return raw

        def _autoselect_template_registers(dev_cfg):
            """Seed a new device with its template's registers — extracted to
            device_seed.py so the boot path (endpoint units) uses the same logic."""
            from multibus.devices.device_seed import autoselect_template_registers
            autoselect_template_registers(config, template_registry, dev_cfg)

        def _ensure_device_bucket(dev_cfg):
            """Auto-create the device's InfluxDB bucket (off-thread) so its history/
            energy work without manual setup. Non-fatal."""
            if not (_pub.influx and dev_cfg.influxdb_enabled and dev_cfg.influxdb_bucket):
                return
            threading.Thread(target=_pub.influx.ensure_bucket,
                             args=(dev_cfg.influxdb_bucket,), daemon=True,
                             name=f"Bucket-{dev_cfg.id}").start()

        def _errors_detail(errors: List[str]) -> Dict:
            """A 422's detail: `errors` as always, plus `fields` — each error that
            names a field (`connection.host: required`), by that field, so a form
            can say it next to the input. Additive: `errors` stays the whole truth."""
            fields: Dict[str, str] = {}
            for e in errors:
                path, sep, msg = str(e).partition(': ')
                if sep and _FIELD_PATH_RE.match(path):
                    fields.setdefault(path, msg)
            return {"errors": list(errors), **({"fields": fields} if fields else {})}

        def _field_errors(fn):
            """Every 422 a device save answers carries `fields` (_errors_detail)."""
            import functools

            @functools.wraps(fn)
            def _w(*a, **kw):
                try:
                    return fn(*a, **kw)
                except HTTPException as e:
                    d = e.detail
                    if e.status_code == 422 and isinstance(d, dict) and isinstance(d.get('errors'), list) \
                            and 'fields' not in d:
                        raise HTTPException(status_code=422, detail={**d, **_errors_detail(d['errors'])})
                    raise
            return _w

        def _serialized_mutation(fn):
            import functools

            @functools.wraps(fn)
            def _w(*a, **kw):
                with _device_mutation_lock:
                    try:
                        return fn(*a, **kw)
                    finally:
                        # what a rule may read changed (or may have): judged afresh
                        _fields_cache.clear()
            return _w

        def _dry_run_connection(payload: Dict, existing_id: Optional[str] = None) -> Dict:
            """``POST /api/devices?dry_run=true&only=connection`` (L3-F3): the
            connection judged as a save judges it — its fields, the matrix and
            the edit locks, the bus claims (ADR-0025), the Seplos mode rule —
            without an id, a name or a template (one sent is used: a map written
            for listening decides the role). Nothing is stored, no probe device
            is ever made. 422 names only the connection's fields.
            ``existing_id`` (the device page's preview of a saved device): judged
            as a save of THAT device — its own address is not a clash with
            itself, its edit locks apply (L3.9)."""
            import secrets as _secrets
            conn = payload.get('connection') if isinstance(payload, dict) else None
            if not isinstance(conn, dict) or not conn:
                raise HTTPException(status_code=422, detail={"errors": ["connection: required"],
                                                             "fields": {"connection": "required"}})
            tid = str(payload.get('template') or '').strip()
            if existing_id and registry.has(existing_id):
                probe = existing_id
            else:
                existing_id = None
                probe = 'probe-' + _secrets.token_hex(6)
                while registry.has(probe):
                    probe = 'probe-' + _secrets.token_hex(6)
            body = _drop_spec_keys({'id': probe, 'name': probe, 'template': tid, 'enabled': False,
                                    'connection': dict(conn)})

            def _own(e) -> bool:
                path, sep, _m = str(e).partition(': ')
                if not sep or not _FIELD_PATH_RE.match(path):
                    return existing_id is not None or probe not in str(e)   # a bus claim or a store refusal
                return path.split('.', 1)[0] in ('connection', 'sources')

            errs: List[str] = []
            fields: Dict[str, str] = {}
            try:
                raw = _validate_device_payload(body, existing_id=existing_id)
            except HTTPException as e:
                d = e.detail if isinstance(e.detail, dict) else {}
                if e.status_code != 422 or not isinstance(d.get('errors'), list):
                    raise
                errs = [x for x in d['errors'] if _own(x)]
                fields = {f: m for f, m in (d.get('fields') or {}).items()
                          if f.split('.', 1)[0] in ('connection', 'sources')}
                raw = None
            # the bus claim is said whatever else the fields said (m6): judged on
            # the connection as sent when the fields did not pass
            claim_raw = raw if raw is not None else {'id': probe, 'name': probe, 'template': tid,
                                                     'enabled': False, 'connection': dict(conn)}
            try:
                _device_claim_check(claim_raw, probe)
            except HTTPException as ce:
                cd = ce.detail if isinstance(ce.detail, dict) else {}
                errs += [x for x in (cd.get('errors') or []) if x not in errs]
            except Exception:  # noqa: BLE001 — a shape that cannot be built: the bus is unknown
                logger.debug("only=connection: claim check skipped", exc_info=True)
            try:
                bus = _bus_of_raw(claim_raw, probe)
            except Exception:  # noqa: BLE001
                bus = None
            if bus is None and raw is None:
                bus = 'unknown'                 # not told: the connection did not pass
            if errs:
                det = _errors_detail(errs)
                for f, m in fields.items():
                    det.setdefault('fields', {}).setdefault(f, m)
                det['bus'] = bus
                raise HTTPException(status_code=422, detail=det)
            return {"status": "valid", "would": "create", "only": "connection", "bus": bus}

        def _bus_of_raw(raw: Dict, did: str) -> Optional[Dict]:
            """The bus a device definition would join: {key, where, role, pollers,
            listeners} (the others already on it), or None (HTTP, MQTT, CAN…)."""
            from multibus.transports.serial.bus_claims import listeners, pollers, source_claims
            built = config.preview_devices(device=raw)
            if not built:
                return None
            claims = source_claims(built[0], template_registry)
            if not claims:
                return None
            _i, key, listens, where = claims[0]
            return {'key': key, 'where': where, 'role': 'listen' if listens else 'master',
                    'pollers': pollers(config, key, template_registry, {did}),
                    'listeners': listeners(config, key, template_registry, {did}, standby=False)}

        def _would_be_entry(raw: Dict) -> Dict:
            """What a create would store, as the device will be seen: the resolved
            topic prefix, bucket and Home Assistant node, the wire it speaks, its
            write lock (always on), its setup (pending when its template has
            setup writes), the bus it joins with the devices already on it, and
            the components that come with it (ADR-0031 §5)."""
            from multibus.transports.serial.bridges import TYPES as _BT
            from multibus.devices.connection_spec import public, spec_of
            did = raw['id']
            built = config.preview_devices(device=raw)
            dev = built[0] if built else None
            tpl = template_registry.get(raw.get('template') or '') if raw.get('template') else None
            try:
                wire = public(spec_of(raw.get('connection'), config.bridges, _BT))
            except Exception:  # noqa: BLE001 — the entry is a courtesy of the dry run
                wire = None
            return {
                'id': did, 'name': raw.get('name') or did, 'template': raw.get('template') or '',
                'enabled': bool(raw.get('enabled', True)), 'type': raw.get('kind') or '',
                'protocol': dev.protocol if dev else str((raw.get('connection') or {}).get('protocol', '')),
                'wire': wire,
                'topic_prefix': dev.mqtt_topic_prefix if dev else raw['mqtt']['topic_prefix'],
                'bucket': dev.influxdb_bucket if dev else raw['influxdb']['bucket'],
                'device_tag': dev.influxdb_device_tag if dev else did,
                'ha_node': (raw.get('mqtt') or {}).get('ha_node') or f"mbg_dev_{did}",
                'write_locked': True,
                'opcua_enabled': bool((raw.get('opcua') or {}).get('enabled', False)),
                'setup': 'pending' if getattr(tpl, 'setup', None) else 'none',
                'bus': _bus_of_raw(raw, did),
                'components': [{'id': c['id'], 'type': c['type'], 'instance': c['instance'], 'name': c['name']}
                               for c in raw.get('components') or []],
            }

        def _components_on_create(raw: Dict, payload: Dict) -> List[Dict]:
            """The components a create brings ([{type, instance, name?}]), checked
            as one added later is (_new_component) — the entries to store, or 422
            for any of them: all or nothing."""
            comps = payload.get('components') if isinstance(payload, dict) else None
            if comps is None or comps == []:
                return []
            if not isinstance(comps, list) or len(comps) > 64:
                raise HTTPException(status_code=422, detail={"errors": [
                    "components: a list of {type, instance, name} (up to 64)"]})
            did = raw['id']
            errors: List[str] = []
            if str((raw.get('connection') or {}).get('protocol', 'tcp')).lower() != 'mqtt':
                errors.append("components: they are read over MQTT only, for now")
            tpl = template_registry.get(raw.get('template') or '') if raw.get('template') else None
            types = dict(getattr(tpl, 'components', None) or {})
            held: Set[tuple] = set()
            taken: Set[str] = {did}
            out: List[Dict] = []
            for i, c in enumerate(comps):
                if not isinstance(c, dict):
                    errors.append(f"components[{i}]: an object {{type, instance, name}}")
                    continue
                entry, cid, errs = _new_component(did, types, c, held, taken)
                errors += [f"components[{i}].{e}" for e in errs]
                if entry is not None:
                    held.add((entry['type'], entry['instance']))
                    taken.add(cid)
                    out.append(dict(entry, id=cid))
            if errors:
                raise HTTPException(status_code=422, detail=_errors_detail(errors))
            return out

        def _start_new_component(new):
            """A component just stored: its template's registers, a clean slate of
            attributes, its bucket, its client."""
            from multibus.devices.device_seed import autoselect_template_registers
            autoselect_template_registers(config, template_registry, new, topics_must_match=True)
            try:
                attributes.drop('device', new.id, reason='a new component at this id')
            except Exception:  # noqa: BLE001
                logger.exception("attributes: could not clear what %s held", new.id)
            _ensure_device_bucket(new)
            registry.add(new, _start_device_client(new))
            return new

        def _restore_resets(raw: Optional[Dict]) -> Dict[str, Any]:
            """What a restore of ``raw`` (a device's stored entry) sets back to safe
            and so does NOT bring back as it was (ADR-0025 ADD-12): the write lock
            when it was off, the command bindings that were on. ``{}``: a restore
            brings it back exactly. Said by the delete and by the restore, so
            Undo's toast says it plainly (ADR-0032 §8)."""
            out: Dict[str, Any] = {}
            raw = raw if isinstance(raw, dict) else {}
            if raw.get('write_locked') is not True:
                out['write_locked'] = True
            cmds = raw.get('commands') if isinstance(raw.get('commands'), list) else []
            # a binding with no name is said by its place (review n4)
            on = [str(c.get('name') or f"#{i + 1}") for i, c in enumerate(cmds)
                  if isinstance(c, dict) and c.get('enabled')]
            if on:
                out['commands'] = on
            # its components come back write-locked too (review M1, ADR-0025)
            comps = raw.get('components') if isinstance(raw.get('components'), list) else []
            unlocked = [_comp_id(raw, c) for c in comps if isinstance(c, dict) and c.get('write_locked') is False]
            if unlocked:
                out['components'] = unlocked
            return out

        def _comp_id(parent: Dict, c: Dict) -> str:
            """A component entry's id: its own, else the one the config derives."""
            return str(c.get('id') or config.component_id(str(parent.get('id', '')), str(c.get('type', '')),
                                                           str(c.get('instance', ''))))

        def _restore_resumes(raw: Optional[Dict]) -> List[Dict[str, str]]:
            """What a restore of ``raw`` starts again beside reading (review m6):
            its REST push (and its components'), each with the host it pushes to —
            never the URL's path, query or credentials."""
            from urllib.parse import urlparse
            raw = raw if isinstance(raw, dict) else {}
            out: List[Dict[str, str]] = []
            comps = raw.get('components') if isinstance(raw.get('components'), list) else []
            for did, ent in [(raw.get('id'), raw)] + [(_comp_id(raw, c), c) for c in comps if isinstance(c, dict)]:
                rp = ent.get('rest_push') if isinstance(ent, dict) else None
                if isinstance(rp, dict) and rp.get('enabled') and str(rp.get('url') or '').strip():
                    host = ''
                    try:
                        u = urlparse(str(rp['url']))
                        host = u.hostname or ''
                        if ':' in host:
                            host = f"[{host}]"            # IPv6, as a URL writes it (R2)
                        if u.port:
                            host = f"{host}:{u.port}"
                    except ValueError:
                        pass                              # a port that is not one: the host is still said
                    out.append({'what': 'rest_push', 'device': str(did or ''), 'host': host})
            return out

        def _restore_plan(device_id: str) -> Dict:
            """The entry a restore of ``device_id`` would store, every check a
            restore makes passed — or the HTTPException it refuses with. Nothing
            is stored; the restore and the list's ``?check=true`` ask the same.
            Kept settings the checks cannot even read (a planted or hand-edited
            file) are a 422, never a 500 (review M2)."""
            try:
                return _restore_plan_checked(device_id)
            except HTTPException:
                raise
            except Exception:  # noqa: BLE001 — the detail is the log's, not the operator's (R4)
                logger.warning("restore %s: kept settings unreadable", device_id, exc_info=True)
                raise HTTPException(status_code=422, detail={"errors": [
                    "its kept settings are damaged and cannot be restored — Forget them"]})

        def _restore_plan_checked(device_id: str) -> Dict:
            if any(d.id == device_id for d in config.devices):
                raise HTTPException(status_code=409, detail={"errors": [
                    f"device '{device_id}' already exists"], "code": "exists"})
            try:
                raw = config.load_deleted_device(device_id)
            except ValueError as e:
                raise HTTPException(status_code=422, detail={"errors": [str(e)]})
            if raw is None:
                raise HTTPException(status_code=404, detail="no restorable device with that id")
            if not isinstance(raw, dict):
                raise HTTPException(status_code=422, detail={"errors": ["its kept settings are not a device"]})
            # re-validate the tombstone through the same SSRF/LAN + protocol/port
            # checks create_device enforces (raises 422 on failure) — a planted or
            # edited tombstone must not reactivate a definition that create would
            # reject. Returns the normalized raw dict.
            checked = _validate_device_payload(raw, stored_conn=raw.get('connection'))
            # what validation does not rebuild is the device's too — its enabled
            # commands, linked meter, write lock, outputs, components — and comes
            # back, each checked; nothing else from the file does
            _errs: List[str] = []
            # the tombstone's bindings were stored: a safe the template's bounds
            # moved away from does not block the restore (CR2-5)
            validate_commands(raw.get('commands'), _errs, tpl=template_registry.get(raw.get('template') or ''),
                              stored=raw.get('commands'))
            validate_channels(raw.get('channels'), _errs)
            _comps = raw.get('components')
            if _comps is not None and (not isinstance(_comps, list) or any(
                    not isinstance(c, dict) or (c.get('id') and not _DEVICE_ID_RE.match(str(c['id'])))
                    for c in _comps)):
                _errs.append("components: a list of {type, instance, id?, name?, enabled?}")
            elif _comps:
                # the components come back under their ids: one a Virtual BMS took while the
                # device was deleted is refused like a new one (R2D-NAMESPACE-1)
                from multibus.outputs.vbms_registry import device_namespace_ids
                _errs += vbms_id_error(device_namespace_ids({'devices': [{**raw, 'id': device_id}]}) - {device_id},
                                       getattr(config, 'virtual_bms', None), 'components')
            if _errs:
                raise HTTPException(status_code=422, detail={"errors": _errs})
            for _k in ('mqtt', 'influxdb'):
                if isinstance(raw.get(_k), dict) and isinstance(checked.get(_k), dict):
                    checked[_k] = {**raw[_k], **checked[_k]}
            # every key a device's own routes manage comes back as it was (the
            # validation above rebuilds only what the device form edits)
            for _k in _DEVICE_OWN_KEYS:
                if _k in raw and _k not in checked:
                    checked[_k] = raw[_k]
            raw = checked
            # a restored device comes back safe (ADR-0025, ADD-12): write-locked,
            # every command binding off until someone turns it on again
            raw['write_locked'] = True
            if isinstance(raw.get('commands'), list):
                raw['commands'] = [{**c, 'enabled': False} if isinstance(c, dict) else c for c in raw['commands']]
            # its components too: each is a device a write could reach (review M1)
            if isinstance(raw.get('components'), list):
                raw['components'] = [{**c, 'write_locked': True} if isinstance(c, dict) else c for c in raw['components']]
            _device_claim_check(raw, device_id)
            return raw

        def _primary_connection(conn: Dict) -> Dict[str, Any]:
            """The primary's connection checked (PUT and PATCH, one validator —
            ADR-0032 §6): {host, port, unit_id, timeout, retry_attempts,
            retry_delay} for config.update_modbus (a retry key left out: None,
            kept). A key left out keeps what the meter runs on; an empty host is
            still refused. Raises the 422: the class lock first, alone (with its
            field), then every value refused."""
            from multibus.devices.connection_spec import edit_errors, spec_of
            _lock = edit_errors(None, spec_of({'protocol': conn.get('protocol', 'tcp')}, []), {'primary': True})
            if _lock:
                raise HTTPException(status_code=422, detail={"errors": [e for _f, e in _lock],
                                                             "fields": {f: e for f, e in _lock}})
            # Validate like a secondary device — the primary was skipping this, so a
            # bad port/unit_id (e.g. "abc") was persisted and broke the primary client.
            errors = []
            m = config.modbus
            host = str(conn.get('host', m.host) or '').strip()
            if not host:
                errors.append("connection.host: required for Modbus TCP")
            elif _is_serial_path(host):
                errors.append(_serial_host_error("connection.host", host))
            from multibus.devices.conn_advanced import _number

            def _whole(v):
                # a whole number given as a number or its text; never a boolean
                # or a fraction (int(2.9) was 2, int(True) was 1 — review #3)
                n = _number(v)
                return int(n) if n is not None and n.is_integer() else None
            port = _whole(conn.get('port', m.port))
            if port is None or not (1 <= port <= 65535):
                errors.append("connection.port: must be 1..65535")
            unit = _whole(conn.get('unit_id', m.unit_id))
            if unit is None or not (0 <= unit <= 255):
                errors.append("connection.unit_id: must be 0..255")
            timeout = _number(conn.get('timeout', m.timeout))
            if timeout is None:
                errors.append("connection.timeout: must be a number")
            else:
                _te = _timeout_error({'timeout': timeout}, {'timeout': float(m.timeout)})
                if _te:
                    errors.append(_te)
            # the retries by the rule every device's advanced keys follow (a value
            # as stored is never judged again — ADR-0006); text that is a number
            # is stored as one (L3.11-F2: it was stored as text and broke the meter)
            from multibus.devices.conn_advanced import errors as _advanced_errors
            retry = {k: conn.get(k) for k in ('retry_attempts', 'retry_delay')}
            errors += _advanced_errors({k: v for k, v in retry.items() if v is not None},
                                       {'retry_attempts': m.retry_attempts, 'retry_delay': m.retry_delay})
            if errors:
                raise HTTPException(status_code=422, detail={"errors": errors})
            # stored as the numbers they are, whatever form they came in (a 4.0
            # stored as a float broke range() in the meter's read loop — review #2)
            if retry['retry_attempts'] is not None:
                retry['retry_attempts'] = int(_number(retry['retry_attempts']))
            if retry['retry_delay'] is not None:
                retry['retry_delay'] = float(_number(retry['retry_delay']))
            return {'host': host, 'port': port, 'unit_id': unit, 'timeout': timeout, **retry}

        def _primary_connection_same(vals: Dict[str, Any]) -> bool:
            """What it reads with now: a save that says the same is no save — it
            would rewrite config.yaml and reconnect the meter live systems read."""
            m = config.modbus
            return (vals['host'] == str(m.host) and vals['port'] == int(m.port) and vals['unit_id'] == int(m.unit_id)
                    and float(vals['timeout']) == float(m.timeout)
                    and vals['retry_attempts'] in (None, m.retry_attempts)
                    and vals['retry_delay'] in (None, m.retry_delay))

        def _apply_primary_or_say(apply: Dict[str, Any], pg_saved: bool = False) -> None:
            """config.apply_primary, its failures said as what was stored: a save
            that failed stored nothing (memory put back) — 503, with the poll
            intervals as a partial save when they went in first; a rebuild that
            failed after the save — 503 saying it is saved and the gateway must
            be restarted to run on it (review #9), never a bare 500."""
            from multibus.core.config import PrimaryRebuildError
            try:
                config.apply_primary(**apply)
            except PrimaryRebuildError as e:
                logger.error("primary: saved, but the devices could not be rebuilt (%s)", e)
                saved = (['polling.poll_groups'] if pg_saved else []) + ['entry']
                raise HTTPException(status_code=503, detail={
                    "errors": [f"saved to config.yaml, but the running devices could not be rebuilt from it ({e}) — "
                               "restart the gateway to run on it"],
                    "partial": {"saved": saved, "failed": ["apply"]}})
            except Exception as e:  # noqa: BLE001 — put back in memory by apply_primary
                logger.warning("primary: config not written (%s)", e)
                det: Dict[str, Any] = {"errors": [f"the configuration could not be written ({type(e).__name__}) — "
                                                  + ("only the poll intervals were saved" if pg_saved
                                                     else "nothing was saved")]}
                if pg_saved:
                    det["partial"] = {"saved": ["polling.poll_groups"], "failed": ["entry"]}
                raise HTTPException(status_code=503, detail=det)
            # _build_devices() rebuilt EVERY DeviceConfig — re-sync the whole list
            # (clients matched by id), so pollers and config.get_device() never
            # diverge on derived fields after a primary edit.
            registry.resync(config.devices, modbus_client)

        def _write_primary_modbus(conn: Dict[str, Any]) -> None:
            """Settings → Modbus (POST /api/config/modbus), the third writer of
            ``modbus:`` (review #7): the primary's one validator and one write,
            under its entry lock; no reconnect (Settings' Apply does it)."""
            import contextlib
            # the lock order of every checked device write: the entry's, then
            # the device-mutation lock
            entry = (_entry_lock(config.primary_id) if config.primary_id else None) or contextlib.nullcontext()
            with entry, _device_mutation_lock:
                vals = _primary_connection(conn)
                if not _primary_connection_same(vals):
                    config.apply_primary(modbus=vals)
                    registry.resync(config.devices, modbus_client)

        def _update_primary_device(payload: Dict, dry_run: bool = False):
            """Edit device #1 in place: connection → config.modbus, HA flag →
            global mqtt; name, template and routing identity (topic prefix /
            bucket / device tag) stay FIXED for byte-identical migration.
            Reconnects the primary client live. `dry_run`: checked, never saved."""
            vals = _primary_connection(payload.get('connection', {}) or {})
            same = (_primary_connection_same(vals)
                    and ('ha_discovery_enabled' not in payload
                         or flag(payload, 'ha_discovery_enabled') == bool(config.mqtt.ha_discovery_enabled)))
            if same:
                if dry_run:
                    return {"status": "valid", "would": "unchanged"}
                prim = config.primary_device
                return {"status": "unchanged", "device": _device_entry(prim, modbus_client)}
            if dry_run:
                # anything else rewrites the config and reconnects the meter
                return {"status": "valid", "would": "update_restart"}
            _apply_primary_or_say(dict(modbus=vals, ha_discovery=(flag(payload, 'ha_discovery_enabled')
                                                                  if 'ha_discovery_enabled' in payload else None)))
            prim = config.primary_device
            # _build_devices() rebuilt EVERY DeviceConfig — re-sync the whole list
            # (clients matched by id), so pollers and config.get_device() never
            # diverge on derived fields after a primary edit.
            registry.resync(config.devices, modbus_client)
            if modbus_client:
                modbus_client.update_config(config.modbus)
                modbus_client.reconnect()
                registry.client_rebuilt(prim)          # reconnected onto the new wire (ADR-0042)
            if _pub.mqtt and config.mqtt.ha_discovery_enabled:
                try:
                    _pub.mqtt.publish_ha_discovery()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"primary HA re-publish failed: {e}")
            return {"status": "updated", "device": _device_entry(prim, modbus_client)}

        def _update_device_locked(device_id: str, payload: Dict, dry_run: bool):
            idx, dev_cfg, client = _find_device(device_id)
            if dev_cfg.primary:
                return _update_primary_device(payload, dry_run=dry_run)
            if dev_cfg.component_of:
                raise HTTPException(status_code=422, detail={"errors": [
                    f"'{device_id}' is a component of '{dev_cfg.component_of}' — manage it from that device"]})
            _why = config.raw_device_refusal(device_id)     # an installation's unit: dry run or not
            if _why:
                raise HTTPException(status_code=422, detail={"errors": [_why]})
            _prev_raw = copy.deepcopy(config.get_raw_device(device_id) or {})
            try:
                raw = _device_put_raw(device_id, dev_cfg, payload, _prev_raw)
            except BridgeConflict as e:
                raise HTTPException(status_code=422, detail={"errors": [str(e)]})
            # an unchanged save changes nothing: the stored device through the
            # same pipeline is what this save would write. A device that should
            # run and does not is saved anyway — saving is how one reconnects it.
            try:
                _base = _device_put_raw(device_id, dev_cfg, copy.deepcopy(_prev_raw), _prev_raw)
            except Exception as e:  # noqa: BLE001 — the comparison never fails a save
                logger.debug("device %s: unchanged-check skipped (%s)", device_id, e)
                _base = None
            if _base is not None:
                raw = _drop_echoed_defaults(raw, _base)
            _same = _base is not None and raw == _base
            _stalled = dev_cfg.enabled and client is None
            if _same and not _stalled:
                if dry_run:
                    return {"status": "valid", "would": "unchanged"}
                return {"status": "unchanged", "device": _device_entry(dev_cfg, client)}
            # (its components read through its connection: while they exist, the
            # device keeps its template and stays MQTT — an edit lock, held by
            # _validate_device_payload with the others)
            # Persist FIRST (upsert is transactional and rolls back on failure);
            # only tear down the old client once the new config is committed, so a
            # rejected edit leaves the running device untouched instead of
            # disconnected-and-not-restarted.
            # nothing it runs on changed (a rename, an explicit default written
            # down): the config is saved and the running device keeps going —
            # a cosmetic save must never punch a hole in acquisition
            _old_sig = _built_sig(config.preview_devices(device=_prev_raw))
            _quiet = (not _stalled and _old_sig is not None
                      and _old_sig == _built_sig(config.preview_devices(device=raw)))
            if dry_run:
                if not config.preview_devices(device=raw):
                    raise HTTPException(status_code=422, detail={"errors": [
                        f"device {device_id!r} could not be built from the given data"]})
                return {"status": "valid", "would": "update" if _quiet else "update_restart"}
            try:
                new_cfg = config.upsert_raw_device(raw)
            except ValueError as e:
                raise HTTPException(status_code=422, detail={"errors": [str(e)]})
            if _quiet:
                registry.replace(device_id, new_cfg)
                _sync_device_discovery()
                logger.info(f"device {device_id}: updated (nothing it runs on changed — kept running)")
                return {"status": "updated", "restarted": False,
                        "device": _device_entry(new_cfg, registry.find(device_id)[2])}
            if client:
                client.disconnect()
            _autoselect_template_registers(new_cfg)   # first-time template assignment
            _ensure_device_bucket(new_cfg)            # influx just enabled / bucket changed
            new_client = _start_device_client(new_cfg)
            # re-resolve by id: a concurrent delete may have shifted the index
            registry.replace(device_id, new_cfg, client=new_client, add_if_missing=True)
            _apply_rest_push(new_cfg)                  # restart pusher with new config
            # its components read through its connection: they follow it
            _restart_devices([d.id for d in config.component_devices(device_id)])
            _sync_device_discovery()
            logger.info(f"device {device_id}: updated")
            return {"status": "updated", "device": _device_entry(new_cfg, new_client)}

        def _patch_locks(dev_cfg) -> List[Dict[str, Any]]:
            """The dry run's ``locks``: the device's edit locks exactly as
            ``GET /api/devices/{id}`` says them (device_summary.locks, L3.2a)."""
            from multibus.devices.device_summary import device_view
            try:
                spec, _per = _device_specs(dev_cfg)
            except Exception:  # noqa: BLE001 — device_view derives it then
                spec = None
            return device_view(config, dev_cfg, template_registry, raw=_stored_device_raw(dev_cfg),
                               spec=spec)['locks']

        def _poll_groups_now(dev_cfg) -> Dict[str, Dict]:
            try:
                _regs, groups = config.load_device_registers(dev_cfg)
            except Exception:  # noqa: BLE001
                return {}
            return {n: {"interval": g.interval, "description": g.description} for n, g in (groups or {}).items()}

        def _primary_state() -> Dict[str, Any]:
            """The primary's flat sections as one entry-shaped dict — what its
            PATCH diffs (device_patch.changes places each key in its section),
            each output in the form its file keeps (review #5: a change that
            never reaches the file is no change)."""
            m = config.modbus
            return {'connection': {'host': m.host, 'port': m.port, 'unit_id': m.unit_id, 'timeout': m.timeout,
                                   'retry_attempts': m.retry_attempts, 'retry_delay': m.retry_delay},
                    'http_output': {'enabled': bool(config.http_output_primary_enabled)},
                    'opcua': copy.deepcopy(_primary_kept('opcua')),
                    'rest_push': copy.deepcopy(_primary_kept('rest_push'))}

        def _patch_primary(dev_cfg, secs: Dict[str, Dict], dry_run: bool, intent: Optional[str]):
            """PATCH on the primary meter (ADR-0032 §6, L3-F19): the sections
            mapped onto its flat config — connection → ``modbus:`` (the PUT's
            validator, _primary_connection), http_output / opcua / rest_push →
            their primary sections (the sub-routes' validators),
            polling.poll_groups → its register file (the poll-group route's).
            Anything else — the gateway's HA switch included — is refused per
            field in its locks' words. One config write (config.apply_primary),
            one restart decision: the meter reconnects only when its connection
            changes."""
            from multibus.core.config import primary_opcua_kept, primary_rest_push_kept
            from multibus.devices import device_patch as _dp
            device_id = dev_cfg.id
            if intent == 'replace':
                raise HTTPException(status_code=422, detail=_errors_detail([
                    "intent: replace is not offered for the primary meter — its counters keep no reset marker"]))
            if 'connection' in secs:          # a spec is output only (ADR-0026 §1): dropped, as for any device
                secs = {**secs, 'connection': _drop_spec_keys({'connection': secs['connection']})['connection']}
            errors = _dp.primary_refusals(secs, {
                'name': dev_cfg.name, 'type': _device_type_of(dev_cfg)[0], 'template': dev_cfg.template,
                'ha_discovery': bool(config.mqtt.ha_discovery_enabled),
                # what GET serves: sent back as served, a key it does not use is no change
                'connection': _device_entry(dev_cfg, modbus_client)['connection'] if 'connection' in secs else {}})
            if errors:
                raise HTTPException(status_code=422, detail=_errors_detail(errors))
            before = _primary_state()
            after = copy.deepcopy(before)
            apply: Dict[str, Any] = {}
            restart = False
            if 'connection' in secs:
                vals = _primary_connection(_dp.primary_connection_keys(secs['connection']))
                if not _primary_connection_same(vals):
                    # the meter reconnects only when what it reads with changes;
                    # said as it is, modbus: is not rewritten (as PUT: unchanged)
                    after['connection'].update({k: v for k, v in vals.items() if v is not None})
                    apply['modbus'] = vals
                    restart = True
            pol, plan = secs.get('polling') or {}, {}
            if 'outputs' in secs:
                plan, oerr = _dp.outputs_plan(_dp.primary_outputs(secs['outputs']),
                                              old_rest_headers=dev_cfg.rest_push.get('headers') or {},
                                              mask=_REST_HDR_MASK, flag=flag, heartbeat_ok=heartbeat_seconds,
                                              opcua_block=_opcua_block)
                errors += oerr
            pg_clean: Dict = {}
            if 'poll_groups' in pol:
                pg_clean, why = _dp.poll_groups_clean(pol['poll_groups'])
                if why:
                    errors.append(f"polling.poll_groups: {why}")
            if errors:
                raise HTTPException(status_code=422, detail=_errors_detail(errors))
            pg_changes: List[Dict] = []
            if pg_clean:
                _refuse_busy_poll_groups(dev_cfg, pg_clean, field='polling.poll_groups')
                pg_changes = _dp.poll_group_changes(_poll_groups_now(dev_cfg), pg_clean)
            if 'http_output' in plan:
                after['http_output']['enabled'] = plan['http_output']
            if 'opcua' in plan:
                after['opcua'] = primary_opcua_kept(plan['opcua'])
            if 'rest_push' in plan:
                after['rest_push'] = primary_rest_push_kept(plan['rest_push'])
            # only what changes is written (an output said as it is stays as stored)
            if after['http_output'] != before['http_output']:
                apply['http_output'] = after['http_output']['enabled']
            if after['opcua'] != before['opcua']:
                apply['opcua'] = plan['opcua']
            if after['rest_push'] != before['rest_push']:
                apply['rest_push'] = plan['rest_push']
            entry_changes = _dp.changes(before, after)
            changes = entry_changes + pg_changes
            effects: List[str] = []
            if restart:
                effects.append("acquisition restarts (connection): the meter reconnects")
            elif changes:
                effects.append("applies live — acquisition keeps running")
            if 'rest_push' in apply:
                effects.append("the REST push restarts with its new settings")
            if pg_changes:
                effects.append("the new poll intervals apply at once")
            would = 'update_restart' if restart else ('update' if changes else 'unchanged')
            if dry_run:
                return {"status": "valid", "would": would, "changes": changes, "restart_required": restart,
                        "effects": effects, "locks": _patch_locks(dev_cfg)}
            if not changes:
                return {"status": "unchanged", "restarted": False, "changes": [],
                        "device": _device_entry(dev_cfg, modbus_client)}
            if pg_changes:
                # the intervals first, as for any device: a file that cannot be
                # written stores nothing at all
                try:
                    config.save_device_poll_groups(device_id, pg_clean)
                except Exception as e:  # noqa: BLE001 — a corrupt file, a full disk: said, nothing stored
                    logger.warning("primary: poll intervals not written (%s) — the edit is not saved", e)
                    raise HTTPException(status_code=422 if isinstance(e, ValueError) else 503, detail={
                        "errors": [f"polling.poll_groups: the register file could not be written ({type(e).__name__}: "
                                   f"{e}) — nothing was saved"],
                        "fields": {"polling.poll_groups": "could not be written"}})
            if entry_changes:
                try:
                    _apply_primary_or_say(apply, pg_saved=bool(pg_changes))     # the one config write
                except HTTPException:
                    if pg_changes:            # the intervals went in: they apply
                        _reload_poll_groups(dev_cfg, modbus_client)
                    raise
            prim = config.primary_device
            if restart and modbus_client:
                modbus_client.update_config(config.modbus)
                modbus_client.reconnect()
                if prim is not None:
                    registry.client_rebuilt(prim)      # reconnected onto the new wire (ADR-0042)
            if pg_changes:
                _reload_poll_groups(prim, modbus_client)
            if 'rest_push' in apply:
                _apply_rest_push(prim)
            logger.info(f"device {device_id}: patched ({', '.join(sorted({c['section'] for c in changes}))})"
                        f"{' — reconnected' if restart else ' — kept running'}")
            return {"status": "updated", "restarted": restart, "changes": changes,
                    "device": _device_entry(prim, modbus_client)}

        def _refresh_shared(dev_cfg, scope: str) -> None:
            """An installation's unit sets an output for every unit of its
            installation (scope 'endpoint': HTTP / JSON, REST push) or of its
            group ('group': OPC UA, the publishing rhythm): every one of them
            is drawn again in the registry (the unit written is already), so
            each says it at once (review of L3.11b, m3). A REST push is
            (re)started for each."""
            if not getattr(dev_cfg, 'endpoint_id', None):
                return
            for d in list(config.devices):
                if d.id == dev_cfg.id or d.endpoint_id != dev_cfg.endpoint_id:
                    continue
                if scope == 'group' and d.group_id != dev_cfg.group_id:
                    continue
                try:
                    registry.replace(d.id, d)               # keep the running client
                    if scope == 'rest':
                        _apply_rest_push(d)
                except Exception:  # noqa: BLE001 — a sibling's refresh never fails the write
                    logger.exception(f"device {d.id}: refresh after {dev_cfg.id}'s shared output")

        def _publishing(dev_cfg) -> Dict:
            """How a device publishes: its own choice where it has one (`own`), the
            gateway's otherwise (`settings`), and what applies (`effective`)."""
            settings = {'mqtt': {'publish_mode': config.mqtt.publish_mode,
                                 'heartbeat_interval': config.mqtt.heartbeat_interval},
                        'influxdb': {'publish_mode': config.influxdb.publish_mode,
                                     'write_interval': config.influxdb.write_interval,
                                     'heartbeat_interval': config.influxdb.heartbeat_interval}}
            own = {'mqtt': {'publish_mode': dev_cfg.mqtt_publish_mode or None,
                            'heartbeat_interval': dev_cfg.mqtt_heartbeat_s},
                   'influxdb': {'publish_mode': dev_cfg.influxdb_publish_mode or None,
                                'write_interval': dev_cfg.influxdb_write_interval_s,
                                'heartbeat_interval': dev_cfg.influxdb_heartbeat_s}}
            if dev_cfg.primary:
                own = {s: {k: None for k in v} for s, v in own.items()}
            eff = {s: {k: (own[s][k] if own[s][k] is not None else settings[s][k]) for k in settings[s]}
                   for s in settings}
            return {'own': own, 'settings': settings, 'effective': eff, 'editable': not dev_cfg.primary}

        def _write_rate_ok(ip: str) -> bool:
            rate = config.security.write_rate_limit_per_s
            if rate <= 0:
                return True
            cap = max(rate, 1.0)
            now = time.monotonic()
            with _write_rl_lock:
                tokens, last = _write_rl_state.get(ip, (cap, now))
                tokens = min(cap, tokens + (now - last) * rate)
                if tokens < 1.0:
                    _write_rl_state[ip] = (tokens, now)
                    return False
                _write_rl_state[ip] = (tokens - 1.0, now)
                return True

        def _refuse_busy_poll_groups(dev_cfg, clean: Dict, field: str = "poll_groups") -> None:
            """A group removed falls back to the gateway's own group of that name;
            one the gateway does not have would send its registers to 'normal'
            (an hourly group polled every few seconds) — refused (422) while in use."""
            dropped = [n for n, g in clean.items() if g is None and n not in config.default_poll_groups]
            if not dropped:
                return
            used = set()
            for src in [None] + list(getattr(dev_cfg, 'sources', None) or []):
                try:
                    regs, _g = config.load_device_registers(dev_cfg, source=src)
                except Exception:  # noqa: BLE001
                    continue
                used |= {r.poll_group for r in regs}
            busy = [n for n in dropped if n in used]
            if busy:
                msgs = {f"{field}.{n}": (f"{n}: selected registers still poll in this group and the gateway "
                                         f"has no group of that name — removing it would poll them at "
                                         f"'normal'; move them to another group first") for n in busy}
                raise HTTPException(status_code=422, detail={"errors": list(msgs.values()), "fields": msgs})

        def _reload_poll_groups(dev_cfg, client) -> None:
            """Live-restart a device's pollers with its new intervals."""
            target = modbus_client if dev_cfg.primary else client
            if target:
                regs, groups = config.load_device_registers(dev_cfg)
                target.update_registers(regs, groups)
                if isinstance(target, DeviceDriver):
                    target.reload_registers()

        def _vmeter_sources() -> Dict[str, Set[str]]:
            """Virtual-meter template id -> the device ids it reads: the
            instance's own source device and every `<device>.<register>` in its
            rows — live, sum and failover (`combined`) alike. Each candidate id
            is every prefix of a reference up to one of its dots; the caller
            keeps those that are devices. A template that cannot be read raises
            (deleting a device it may read is then refused, not allowed)."""
            from multibus.lifecycle.dependents import Unreadable
            from multibus.outputs.virtual_meter import load_template as _lt
            mgr = getattr(app.state, "vmeter_manager", None)
            out: Dict[str, Set[str]] = {}
            if mgr is None:
                return out

            def strings(x):
                if isinstance(x, str):
                    yield x
                elif isinstance(x, dict):
                    for v in x.values():
                        yield from strings(v)
                elif isinstance(x, (list, tuple)):
                    for v in x:
                        yield from strings(v)
            try:
                instances = mgr._load_cfg().get("instances", []) or []
            except Exception as e:  # noqa: BLE001
                raise Unreadable(f"the virtual meters' configuration could not be read ({e})")
            for i in instances:
                tid = str(i.get("template") or "")
                names = out.setdefault(tid, set())
                if i.get("device"):
                    names.add(str(i["device"]))
                try:
                    t = _lt(str(mgr.templates_dir / f"{tid}.yaml"))
                except Exception as e:  # noqa: BLE001
                    raise Unreadable(f"virtual meter template {tid!r} could not be read ({e})")
                for r in t.registers:
                    if r.source_kind in ("const", "const_str"):
                        continue
                    for ref in strings(r.source):
                        j = ref.find('.')
                        while j > 0:
                            names.add(ref[:j])
                            j = ref.find('.', j + 1)
            return out

        def _teardown_device(device_id: str, dev_cfg, client) -> None:
            """Stop a device's runtime side effects before it is removed: client,
            REST pusher, dead-man leases, retained HA discovery."""
            if client:
                client.disconnect()
            rest_push_manager.apply(device_id, {'enabled': False}, lambda: {})  # stop pusher
            _lease_mgr.clear_device(device_id)        # drop any dead-man leases
            # clear the device's retained HA discovery so HA drops its entities —
            # every kind it may have announced (sensor, number, select, its
            # connectivity), not only the sensors (HA-DISC-3); the publisher's
            # one clear, which also drops its command subscriptions
            if _pub.mqtt and getattr(_pub.mqtt, "connected", False):
                try:
                    regs = config.unit_registers(dev_cfg) + [e['_reg'] for e in calc_engine.store.get(device_id, []) if e.get('_reg') is not None]
                    _pub.mqtt.clear_device_discovery(device_id, regs, ha_node=dev_cfg.ha_node, every_kind=True)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"clearing discovery for {device_id} failed: {e}")
            # what its meters gave it stops here, and a device later created with
            # this id starts with no memory of this one (review N1, N6)
            calc_runners.pop(device_id, None)
            calc_engine.forget(device_id)

        def _device_value_topics(dev_cfg) -> set:
            """The retained value topics a device publishes (rows and calculated)."""
            if not _pub.mqtt:
                return set()
            try:
                regs = config.unit_registers(dev_cfg) + [
                    e['_reg'] for e in calc_engine.store.get(dev_cfg.id, []) if e.get('_reg') is not None]
                topics = set(_pub.mqtt.register_topics(regs, dev_cfg.mqtt_topic_prefix))
                if dev_cfg.mqtt_topic_prefix:
                    topics.add(f"{dev_cfg.mqtt_topic_prefix}/meta")       # its attributes go with it
                    _meta_sent.pop(f"{dev_cfg.mqtt_topic_prefix}/meta", None)
                return topics
            except Exception as e:  # noqa: BLE001
                logger.warning(f"listing the topics of {dev_cfg.id} failed: {e}")
                return set()

        def _clear_device_values(topics: set) -> None:
            """A device that is GONE (deleted, not restarted) takes its retained
            values with it: nobody must read a deleted device as live. Run AFTER
            its client stopped, or a last poll would publish them again. Not part
            of the teardown, which an edit also runs."""
            if topics and _pub.mqtt and getattr(_pub.mqtt, "connected", False):
                try:
                    _pub.mqtt.clear_topics(topics)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"clearing retained values failed: {e}")

        def _reload_linked_to(meter_id: str) -> None:
            """Devices whose electricity the deleted device measured lose their
            power_electric / energy_electric now, not at the next restart."""
            from multibus.devices.meter_links import served_by
            for did in {d for d, _r, _i in served_by(config, meter_id)}:
                try:
                    calc_engine.load(did)
                except Exception:  # noqa: BLE001
                    pass

        def _dep_sources():
            from multibus.lifecycle.dependents import Sources
            rt = getattr(app.state, 'rules_runtime', None)

            def _rules():
                if rt is None:
                    return {}
                with rt._lock:                  # a copy, briefly: parsed outside the lock
                    return dict(rt.raw)

            def _sequences():
                store = getattr(app.state, 'sequence_store', None)
                return store.load() if store is not None else []

            def _attributes():
                svc = getattr(app.state, 'attributes', None)
                return svc.store.read()[0] if svc is not None else {}

            def _calculated(device_id: str):
                # straight from the file, and loud: a file that cannot be read
                # must not read as "no formulas here"
                path = config.device_registers_path(device_id)
                try:
                    with open(path) as f:
                        return (json.load(f) or {}).get("calculated", []) or []
                except FileNotFoundError:
                    return []

            def _unreadable():
                out = []
                if rt is not None and getattr(rt.store, 'load_error', None):
                    out.append(f"rules.yaml could not be read ({rt.store.load_error})")
                store = getattr(app.state, 'sequence_store', None)
                if store is not None and getattr(store, 'load_error', None):
                    out.append(f"sequences.yaml could not be read ({store.load_error})")
                return out
            return Sources(config=config, rules=_rules, sequences=_sequences,
                           vmeter_sources=_vmeter_sources, templates=template_registry.all,
                           attributes=_attributes, calculated=_calculated, unreadable=_unreadable)

        def _dependents(kind: str, oid: str) -> Dict:
            """The answer, or 503 when a source of references cannot be read —
            then what uses it is not known, and nothing is deleted."""
            from multibus.lifecycle.dependents import Unreadable, dependents_of
            try:
                return dependents_of(kind, oid, _dep_sources())
            except Unreadable as e:
                raise HTTPException(status_code=503, detail={"errors": [
                    f"what uses {oid!r} cannot be told: {e} — fix that first"]})

        def _refuse_if_used(kind: str, oid: str, what: str, dry_run: bool) -> Optional[Dict]:
            """409 while anything blocks; on a dry run the answer that a delete
            would get. None: go ahead."""
            from multibus.lifecycle.dependents import summary
            deps = _dependents(kind, oid)
            if deps['blocking']:
                raise HTTPException(status_code=409, detail={
                    "errors": [summary(deps, what)], "dependents": deps})
            if dry_run:
                return {"status": "valid", "would": "delete", "dependents": deps}
            return None

        def _device_deletable(device_id: str):
            """404 / 422 for what is never deleted here; else (dev_cfg, client)."""
            _idx, dev_cfg, client = _find_device(device_id)
            if dev_cfg is None:
                raise HTTPException(status_code=404, detail="device not found")
            if dev_cfg.primary:
                raise HTTPException(status_code=422, detail={"errors": [
                    "the primary device cannot be deleted"]})
            if dev_cfg.endpoint_id:
                raise HTTPException(status_code=422, detail={"errors": [
                    f"device '{device_id}' is materialized from endpoint "
                    f"'{dev_cfg.endpoint_id}' — edit or delete the endpoint instead"]})
            if dev_cfg.component_of:
                raise HTTPException(status_code=422, detail={"errors": [
                    f"'{device_id}' is a component of '{dev_cfg.component_of}' — remove it there"]})
            return dev_cfg, client

        def _delete_device_locked(device_id: str, if_match: Optional[str] = None):
            dev_cfg, client = _device_deletable(device_id)
            _check_if_match(if_match, device_id)          # again, now nothing can change under it
            # anything that uses it (or one of its components) would break —
            # again, now nothing can change under it
            _refuse_if_used('device', device_id, f"'{device_id}'", False)
            comps = config.component_devices(device_id)
            # what Undo (its restore) will set back to safe, said with the answer
            _raw_now = config.get_raw_device(device_id)
            resets, resumes = _restore_resets(_raw_now), _restore_resumes(_raw_now)
            # its components go with it (the tombstone keeps their list)
            for c in comps:
                _drop_component_runtime(c)
            _cancel_device_refreshes(device_id)
            _topics = _device_value_topics(dev_cfg)
            _teardown_device(device_id, dev_cfg, client)
            _clear_device_values(_topics)
            config.remove_raw_device(device_id)
            # re-resolve by id (idx may be stale after a concurrent mutation);
            # drops the pair AND its value store in one atomic step
            registry.remove(device_id)
            # DUP-13: the publishers' change-detection caches (MQTT topic-keyed,
            # InfluxDB (device_id, address)-keyed) are never pruned on delete —
            # drop them so a deleted device leaks no cache entries.
            if _pub.mqtt:
                _pub.mqtt.forget_device(dev_cfg.mqtt_topic_prefix)
            if _pub.influx:
                _pub.influx.forget_device(device_id)
            for _x in [device_id] + [c.id for c in comps]:
                _reload_linked_to(_x)
            _sync_device_discovery()
            logger.info(f"device {device_id}: deleted")
            # what an Undo names (review m1); none kept: nothing to undo (m2)
            _ts = config.last_tombstone_ts(device_id)
            return {"status": "deleted", "restorable": _ts is not None, "deleted_ts": _ts,
                    "restore_resets": resets, "restore_resumes": resumes}

        def _drop_component_runtime(dev) -> None:
            """Stop a component and clear what it published (its entry stays)."""
            _i, _c, client = registry.find(dev.id)
            _cancel_device_refreshes(dev.id)
            topics = _device_value_topics(dev)
            _teardown_device(dev.id, dev, client)
            _clear_device_values(topics)
            registry.remove(dev.id)
            if _pub.mqtt:                              # DUP-13 (component delete path)
                _pub.mqtt.forget_device(dev.mqtt_topic_prefix)
            if _pub.influx:
                _pub.influx.forget_device(dev.id)

        def _component_types(dev_cfg) -> Dict[str, Dict]:
            tpl = template_registry.get(dev_cfg.template) if dev_cfg.template else None
            return dict(getattr(tpl, 'components', None) or {})

        def _component_type_name(dev) -> str:
            """A component's type as its parent's template names it ("Battery"),
            else the type key."""
            parent = config.get_device(dev.component_of) if dev.component_of else None
            ct = _component_types(parent).get(dev.component_type) if parent is not None else None
            return str((ct or {}).get('name') or dev.component_type or '')

        def _component_view(dev) -> Dict:
            _i, _c, client = registry.find(dev.id)
            e = _device_entry(dev, client)
            return {'id': dev.id, 'type': dev.component_type, 'type_name': _component_type_name(dev),
                    'instance': dev.component_instance,
                    'name': dev.name, 'enabled': dev.enabled, 'connected': e.get('connected'),
                    'data_health': e.get('data_health'), 'template': dev.template,
                    'mqtt_topic_prefix': dev.mqtt_topic_prefix}

        def _parent_or_404(device_id: str):
            _i, dev_cfg, client = _find_device(device_id)
            if dev_cfg is None:
                raise HTTPException(status_code=404, detail="device not found")
            return dev_cfg, client

        def _component_or_404(device_id: str, component_id: str):
            _i, dev, client = _find_device(component_id)
            if dev is None or dev.component_of != device_id:
                raise HTTPException(status_code=404, detail=f"no component '{component_id}' on '{device_id}'")
            return dev, client

        def _new_component(parent_id: str, types: Dict, payload: Dict, held: Set[tuple],
                           taken: Set[str]) -> Tuple[Optional[Dict], str, List[str]]:
            """One component a request adds, checked: (its entry without the id,
            its id, errors). ``held``: the (type, instance) the parent holds
            already; ``taken``: ids the same request takes (a create's own)."""
            errors: List[str] = []
            ctype = str(payload.get('type', '') or '').strip()
            inst = '' if payload.get('instance') is None else str(payload.get('instance')).strip()
            if ctype not in types:
                errors.append(f"type: '{ctype}' is not one of {', '.join(sorted(types)) or 'none (the template declares no components)'}")
            if not config.COMPONENT_INSTANCE_RE.match(inst):
                errors.append("instance: 1-32 letters, digits, _ . -")
            elif (ctype, inst) in held:
                errors.append(f"instance: {ctype} {inst} is already a component of '{parent_id}'")
            cid = str(payload.get('id') or config.component_id(parent_id, ctype, inst)).strip().lower()
            if not _DEVICE_ID_RE.match(cid):
                errors.append(f"id: '{cid}' is not a device id (a-z 0-9 - _, 2-64 chars) — give it a shorter id")
            elif cid in taken or registry.has(cid) or config.get_device(cid) is not None:
                errors.append(f"id: '{cid}' already exists")
            elif taken_by_vbms([cid], getattr(config, 'virtual_bms', None)):
                # a component is a device on the topics (R2D-NAMESPACE-1)
                errors += vbms_id_error([cid], getattr(config, 'virtual_bms', None))
            else:
                from multibus.devices.topic_prefix import prefix_errors
                errors += [f"mqtt.topic_prefix: {e}"
                           for e in prefix_errors(config.default_topic_prefix(cid), config, {f"device:{cid}"})]
            if errors:
                return None, cid, errors
            name = str(payload.get('name') or f"{types[ctype].get('name') or ctype} {inst}").strip()[:80]
            entry = {'type': ctype, 'instance': inst, 'name': name,
                     'enabled': flag(payload, 'enabled', True),
                     # its routing identity, fixed from now on
                     'mqtt': {'topic_prefix': config.default_topic_prefix(cid)}}
            _set_new_node(entry['mqtt'], cid)
            return entry, cid, []

        def _kind_of_payload(dev, payload: Dict) -> Tuple[str, Optional[str]]:
            """The stored form of the type a payload gives a device ({type} or
            {kind}; null = derive it again): (kind, None), or ('', why not).
            The rule of /organise, which PATCH's identity.type shares."""
            from multibus.catalog import device_types as _dtypes
            raw_kind, why = _dtypes.from_payload(payload)
            if why:
                return '', why
            if raw_kind is None:
                kind = ''
            else:
                msg = _dtypes.override_error(raw_kind)
                if msg:
                    return '', msg
                kind = _dtypes.stored_form(raw_kind)
            if dev.protocol == 'none':
                # a device measured only by its meters has no template to derive a
                # kind from, and is never a meter itself (nor `other`)
                from multibus.catalog.device_kind import METER_ONLY
                msg = (_dtypes.override_error(kind, meter_only=True) if kind else
                       f"a device measured only by its meters is one of {', '.join(METER_ONLY)} or a user type "
                       "(it has no template to derive a kind from, and it is not a meter)")
                if msg:
                    return '', msg
            return kind, None

        _unstartable: Dict[str, Any] = {}      # id -> the config whose start built no client

        # --- public surface -------------------------------------------------
        def _reconcile_runtime(old_devices, new_devices, old_clients, start_added=True):
            """Apply a device-set CHANGE to the running registry after a config reload (ADR-0047
            P2). It runs right after ``registry.resync(config.devices)``, which already re-paired
            every KEPT device (unchanged / config-only edit / RETARGET) with its OLD client and
            dropped REMOVED devices from the pairs while leaving their client running. So resync
            + the ADR-0042 retarget guard own the kept devices — a retargeted device deliberately
            keeps its old client and refuses writes until an EXPLICIT restart; auto-rebuilding it
            here would silently write the new wire. This reconcile closes the two gaps resync
            leaves: **start** ADDED devices (resync paired them with ``client=None``) and **stop**
            REMOVED ones (disconnect the orphaned client ``old_clients[id]`` + drop the store). The
            PRIMARY is excluded (reload rebuilds it; a device changing primary trips
            ``primary_changed`` → full restart). Serialized with the device-mutation lock so it
            can't interleave with device CRUD on the same registry. Returns (diff, failures).

            ``start_added=False`` is the reload that only SAVES (``apply=false``): removed devices
            are still stopped — resync has already dropped them from the registry, so their
            client would otherwise run on, unreachable (RECON-REMOVE-2) — but nothing new is
            started; added devices wait, client-less, for an apply or a restart."""
            from multibus.devices.reconcile import apply_diff, diff_devices
            old = [d for d in old_devices if not getattr(d, "primary", False)]
            new = [d for d in new_devices if not getattr(d, "primary", False)]
            diff = diff_devices(old, new)
            if diff.is_empty() and not start_added:
                return diff, []
            old_by = {d.id: d for d in old}
            new_by = {d.id: d for d in new}

            def _stop(did):                           # tear down the orphan resync left running
                old_cfg = old_by.get(did)
                if old_cfg is None:
                    return
                client = old_clients.get(did)
                _cancel_device_refreshes(did)
                topics = _device_value_topics(old_cfg)
                _teardown_device(did, old_cfg, client)
                _clear_device_values(topics)
                registry.remove(did)                  # drops the lingering value store
                if _pub.mqtt:                          # DUP-13 cache prune
                    _pub.mqtt.forget_device(old_cfg.mqtt_topic_prefix)
                if _pub.influx:
                    _pub.influx.forget_device(did)

            def _start(did):                          # build the client resync left as None
                cfg = new_by[did]
                _autoselect_template_registers(cfg)
                _ensure_device_bucket(cfg)
                client = _start_device_client(cfg)
                registry.replace(did, cfg, client=client, add_if_missing=True)
                _apply_rest_push(cfg)
                if client is None:
                    _unstartable[did] = cfg            # nothing to run for it as configured
                else:
                    _unstartable.pop(did, None)

            def _pending_restart(ids):
                # RETARGET: resync kept the old client and config.get_device() now reports the new
                # wire, so the ADR-0042 head refuses writes until an EXPLICIT restart — do NOT
                # rebuild here (that would silently start writing the new wire).
                if ids:
                    logger.info("reconcile: %d device(s) retargeted — pending explicit restart "
                                "(ADR-0042): %s", len(ids), ", ".join(ids))

            with _device_mutation_lock:
                if start_added:
                    # what must RUN is judged against what runs, not against the last config:
                    # a device saved earlier with apply=false, or whose first start failed, is
                    # "unchanged" to the diff and would wait for a restart for ever
                    # (R2D-APPLY-1). Judged under the lock, so a device being created right
                    # now is not started twice; one that had nothing to run (no buildable
                    # source) is not tried again until its config changes.
                    known = set(diff.added) | set(diff.removed)
                    diff.added.extend(
                        d.id for d in new if d.id not in known and getattr(d, "enabled", True)
                        and registry.find(d.id)[2] is None and _unstartable.get(d.id) != d)
                failed = apply_diff(diff, stop=_stop, rebuild=_pending_restart,
                                    update=lambda _i: None,   # resync already re-paired config-only edits
                                    start=_start if start_added else (lambda _i: None))
                _sync_device_discovery()
            logger.info("reconcile: +%d%s -%d (retarget %d, edit %d)%s", len(diff.added),
                        "" if start_added else " (not started — saved without apply)",
                        len(diff.removed), len(diff.rebuilt), len(diff.updated),
                        f", {len(failed)} FAILED" if failed else "")
            return diff, failed

        self.reconcile_runtime = _reconcile_runtime
        self.apply_routing_defaults = _apply_routing_defaults
        self.device_put_raw = _device_put_raw
        self.built_sig = _built_sig
        self.drop_echoed_defaults = _drop_echoed_defaults
        self.autoselect_template_registers = _autoselect_template_registers
        self.ensure_device_bucket = _ensure_device_bucket
        self.errors_detail = _errors_detail
        self.field_errors = _field_errors
        self.serialized_mutation = _serialized_mutation
        self.dry_run_connection = _dry_run_connection
        self.bus_of_raw = _bus_of_raw
        self.would_be_entry = _would_be_entry
        self.components_on_create = _components_on_create
        self.start_new_component = _start_new_component
        self.restore_resets = _restore_resets
        self.comp_id = _comp_id
        self.restore_resumes = _restore_resumes
        self.restore_plan = _restore_plan
        self.restore_plan_checked = _restore_plan_checked
        self.primary_connection = _primary_connection
        self.primary_connection_same = _primary_connection_same
        self.apply_primary_or_say = _apply_primary_or_say
        self.write_primary_modbus = _write_primary_modbus
        self.update_primary_device = _update_primary_device
        self.update_device_locked = _update_device_locked
        self.patch_locks = _patch_locks
        self.poll_groups_now = _poll_groups_now
        self.primary_state = _primary_state
        self.patch_primary = _patch_primary
        self.refresh_shared = _refresh_shared
        self.publishing = _publishing
        self.write_rate_ok = _write_rate_ok
        self.refuse_busy_poll_groups = _refuse_busy_poll_groups
        self.reload_poll_groups = _reload_poll_groups
        self.vmeter_sources = _vmeter_sources
        self.teardown_device = _teardown_device
        self.device_value_topics = _device_value_topics
        self.clear_device_values = _clear_device_values
        self.reload_linked_to = _reload_linked_to
        self.dep_sources = _dep_sources
        self.dependents = _dependents
        self.refuse_if_used = _refuse_if_used
        self.device_deletable = _device_deletable
        self.delete_device_locked = _delete_device_locked
        self.drop_component_runtime = _drop_component_runtime
        self.component_types = _component_types
        self.component_type_name = _component_type_name
        self.component_view = _component_view
        self.parent_or_404 = _parent_or_404
        self.component_or_404 = _component_or_404
        self.new_component = _new_component
        self.kind_of_payload = _kind_of_payload
