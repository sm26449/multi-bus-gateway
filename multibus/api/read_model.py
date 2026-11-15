# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Device read primitives + ETag write decorators + REST-push helpers, extracted whole from create_api (P0.8b squeeze).

DeviceReadModel holds the low-level device helpers that every service and route builds on:
the lookup/find, the stored-raw / etag / if-match machinery, the ETag write-guard decorators
(device_write / device_runtime), the per-source read helpers (specs, meters, opcua, declared
writes) and the REST-push config helpers. Built early so the services can inject these
directly; kept as verbatim closures with an explicit keyword constructor.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Any, Dict, Optional

from fastapi import HTTPException

from multibus import write_guard as _etag_guard
from multibus.core.config import NETWORK_PROTOCOLS, SERIAL_PROTOCOLS

logger = logging.getLogger("multibus.api")  # preserve caplog logger name
_POLLED_MODBUS = NETWORK_PROTOCOLS + SERIAL_PROTOCOLS      # every Modbus way but listening


class DeviceReadModel:
    """The device read-model + write-decorator seam (ADR-0038)."""

    def __init__(self, *, app, config, registry, template_registry, calc_engine, calc_runners, current_values, rest_push_manager, _cmd_dispatch, _entry_locks, _entry_locks_guard, _etag_key, _REST_HDR_MASK, _raw_channel_conflict, _words_of, make_data_callback, _device_entry):
        def _rest_provider(device_id, primary):
            def provider():
                return current_values if primary else (registry.store_for(device_id) or {})
            return provider

        def _rest_cfg(dev_cfg):
            cfg = dict(dev_cfg.rest_push or {})
            cfg.setdefault('name', dev_cfg.name)
            return cfg

        def _apply_rest_push(dev_cfg):
            rest_push_manager.apply(dev_cfg.id, _rest_cfg(dev_cfg),
                                    _rest_provider(dev_cfg.id, dev_cfg.primary))

        def _rest_push_public(dev_cfg):
            """The device's REST push config for the API — header VALUES masked."""
            rp = dict(dev_cfg.rest_push or {})
            if rp.get('headers'):
                rp['headers'] = {k: _REST_HDR_MASK for k in rp['headers']}
            rp['last'] = rest_push_manager.status(dev_cfg.id)
            return rp

        def _device_type_of(dev):
            """(type, its English label) of a device, as `<dev>/meta` and the
            device list say it (ADR-0027); None for no such device."""
            if dev is None:
                return None
            from multibus.catalog import device_types as _dtypes
            t_ = _dtypes.device_type(dev, template_registry)
            return t_, _dtypes.label(t_)

        def _find_device(device_id: str):
            return registry.find(device_id)

        def _writes_by_mqtt(dev_cfg) -> bool:
            """A unit whose writes go out over MQTT: it has an MQTT source and no
            Modbus one. Such a unit takes only commands — there is no raw
            register write to publish."""
            protos = {str(s.protocol or 'tcp').lower() for s in (dev_cfg.sources or [])} or {str(dev_cfg.protocol or 'tcp').lower()}
            return 'mqtt' in protos and not (protos & set(_POLLED_MODBUS))

        def _write_rule(dev_cfg, address: int, rtype: str):
            """The TemplateRegister governing writes to (address, register_type) on
            this device, or None. Carries writable + bounds + safe value. A register
            absent here (or not writable) cannot be written — the safety allowlist.
            A unit read through sources carries its maps on the Modbus SOURCE, so
            those templates are consulted too."""
            tids = [dev_cfg.template] if dev_cfg.template else []
            tids += [s.template for s in (dev_cfg.sources or [])
                     if s.template and str(s.protocol or 'tcp').lower() in _POLLED_MODBUS + ('mqtt',)]
            for tid in tids:
                tpl = template_registry.get(tid)
                if tpl is None:
                    continue
                for r in tpl.registers:
                    if r.address == address and (r.register_type or 'holding') == rtype:
                        return r
            return None

        def _declared_writes(dev_cfg) -> int:
            """How many registers an operator may write through /write: declared
            writable holdings and coils, as _write_rule finds them (the first
            template wins an address), less what /write refuses even so — a
            register a command writes (ADR-0022 D4) or one scaled by a live
            factor — and nothing for a device written over MQTT (commands only)."""
            if _writes_by_mqtt(dev_cfg):
                return 0
            tids = [dev_cfg.template] if dev_cfg.template else []
            tids += [s.template for s in (dev_cfg.sources or [])
                     if s.template and str(s.protocol or 'tcp').lower() in _POLLED_MODBUS + ('mqtt',)]
            seen = set()
            for tid in dict.fromkeys(tids):
                tpl = template_registry.get(tid) if template_registry else None
                for r in (getattr(tpl, 'registers', None) or []):
                    rtype = r.register_type or 'holding'
                    if not getattr(r, 'writable', False) or rtype not in ('holding', 'coil'):
                        continue
                    if _write_rule(dev_cfg, r.address, rtype) is not r or getattr(r, 'scale_from', None):
                        continue
                    try:
                        if _raw_channel_conflict(dev_cfg, rtype, r.address, _words_of(r.data_type or 'uint16', rtype)):
                            continue
                    except Exception:  # noqa: BLE001 — a count never fails a list
                        continue
                    seen.add((rtype, r.address))
            return len(seen)

        def _start_device_client(dev_cfg):
            """Create + wire + background-start the client for a device.

            Goes through the SAME builder the boot path uses
            (device_runtime.build_device_client), so a device added at runtime and
            the same device after a restart are byte-identical. There used to be two
            copies of this logic and they drifted, invisibly, until a restart
            brought a device back decoding differently."""
            from multibus.devices.device_runtime import build_device_client
            client = build_device_client(
                config, template_registry, dev_cfg,
                allow_nonlan=config.security.allow_nonlan_http_devices)
            if client is None:
                # nothing runs for it (disabled, nothing to read): what its meters
                # gave it stops too, until it runs again (review N1); nor is what
                # waited for its bus written (CR2-8)
                calc_runners.pop(dev_cfg.id, None)
                _cmd_dispatch.cancel(device=dev_cfg.id, reason=f"device '{dev_cfg.id}' was disabled — nothing written")
                return None
            client.publish_callback = make_data_callback(dev_cfg)
            # its derived fields (template-shipped or the operator's) — loaded at
            # boot for every device, and here for one created, edited or restarted
            # at runtime: until now those ran only after the next restart
            try:
                calc_engine.load(dev_cfg.id)
            except Exception as e:  # noqa: BLE001 — a bad formula must not stop the device
                logger.warning("device %s: calculated fields not loaded — %s", dev_cfg.id, e)

            def _bg():
                if client.connect():
                    logger.info(f"device {dev_cfg.id}: connected")
                else:
                    logger.warning(f"device {dev_cfg.id}: connect failed — pollers will retry")
                client.start_polling()

            threading.Thread(target=_bg, daemon=True,
                             name=f"Device-Init-{dev_cfg.id}").start()
            return client

        def _opcua_sink(dev_cfg) -> Dict:
            ctl = getattr(app.state, 'opcua', None)
            try:
                st = ctl.status() if ctl is not None else {}
            except Exception:  # noqa: BLE001
                st = {}
            running = bool(st.get('running'))
            return {'enabled': dev_cfg.opcua_enabled, 'available': bool(st.get('enabled')),
                    'connected': running, 'active': dev_cfg.opcua_enabled and running,
                    'fields': list(dev_cfg.opcua_fields), 'endpoint': st.get('endpoint', ''),
                    'shared_by': dev_cfg.group_id or dev_cfg.endpoint_id if dev_cfg.endpoint_id else ''}

        def _opcua_block(raw) -> Dict:
            """A device's `opcua:` output: {enabled, fields} (fields: the value
            names served; omitted = every value)."""
            if not isinstance(raw, dict):
                raise HTTPException(status_code=422, detail={"errors": ["opcua: an object {enabled, fields}"]})
            fields = raw.get('fields') or []
            if not isinstance(fields, list) or not all(isinstance(f, str) and f.strip() for f in fields):
                raise HTTPException(status_code=422, detail={"errors": ["opcua.fields: a list of value names"]})
            out = {'enabled': bool(raw.get('enabled', False))}
            names = list(dict.fromkeys(f.strip() for f in fields))
            if names:
                out['fields'] = names
            return out

        def _meters_status(device_id: str) -> Dict:
            """The last evaluation's verdict of a device's input / output pair,
            with its age — never a judgement made again at request time."""
            if device_id not in calc_engine.pairs:
                return {}
            v = calc_engine.pair_verdicts.get(device_id)
            if v is None:
                wait = {'state': 'waiting', 'reason': 'not_evaluated'}
                return {'power_loss': dict(wait), 'efficiency_pct': dict(wait), 'load_pct': dict(wait),
                        'age_s': None}
            return {**{k: dict(x) if isinstance(x, dict) else x for k, x in v.items() if k != 'at'},
                    'age_s': round(max(0.0, time.time() - v['at']), 1)}

        def _raw_index() -> Dict[str, Dict]:
            """Every stored entry by device id, units and components expanded
            once — what a whole list reads (review: no re-expansion per row)."""
            out = {str(d.get('id')): d for d in config._raw_devices if isinstance(d, dict)}
            try:
                for _pid, d in config._expand_endpoints():
                    out.setdefault(str(d.get('id')), d)
                for _pid, _c, d in config._expand_components():
                    out.setdefault(str(d.get('id')), d)
            except Exception:  # noqa: BLE001 — a row falls back to its own lookup
                logger.debug("raw index incomplete", exc_info=True)
            return out

        def _stored_device_raw(dev_cfg, raw_index=None) -> Dict:
            """The device as stored (or, for the primary, an installation's unit
            and a component, as its stored definition expands): what the spec
            reads and the ETag hashes."""
            if raw_index is not None and not dev_cfg.primary and dev_cfg.id in raw_index:
                return raw_index[dev_cfg.id]
            if dev_cfg.primary:
                m = config.modbus
                return {'id': dev_cfg.id, 'connection': {'protocol': 'tcp', 'host': m.host, 'port': m.port,
                                                         'unit_id': m.unit_id, 'timeout': m.timeout,
                                                         'retry_attempts': m.retry_attempts,
                                                         'retry_delay': m.retry_delay},
                        'ha_discovery_enabled': bool(config.mqtt.ha_discovery_enabled)}
            raw = config.get_raw_device(dev_cfg.id)
            if raw is not None:
                return raw
            if dev_cfg.endpoint_id:
                ep = config.get_raw_endpoint(dev_cfg.endpoint_id)
                for _pid, d in (config._expand_endpoints([ep]) if ep else []):
                    if d.get('id') == dev_cfg.id:
                        return d
            if dev_cfg.component_of:
                for _pid, _c, d in config._expand_components():
                    if d.get('id') == dev_cfg.id:
                        return d
            return {'id': dev_cfg.id, 'connection': {'protocol': dev_cfg.protocol}}

        def _primary_kept(which: str) -> Dict[str, Any]:
            """The primary's opcua / rest_push section in the form its file keeps
            (memory set by any route agrees; a hand-loaded one too)."""
            from multibus.core.config import primary_opcua_kept, primary_rest_push_kept
            return (primary_opcua_kept(config.opcua_primary) if which == 'opcua'
                    else primary_rest_push_kept(config.rest_push_primary))

        def _device_etag(dev_cfg, raw_index=None) -> str:
            """An HMAC of the stored entry (sorted keys): the same while nothing
            stored changes, a reload included (ADR-0026 §6)."""
            stored = _stored_device_raw(dev_cfg, raw_index)
            if dev_cfg.primary:
                # the primary's other flat sections are its stored entry too: an
                # output or its write lock changed is a new version (L3.11-F1)
                stored = {**stored, '_flat': {'http_output': bool(config.http_output_primary_enabled),
                                              'opcua_output': _primary_kept('opcua'),
                                              'rest_push': _primary_kept('rest_push'),
                                              'pq_recorder': config.pq_recorder_primary or {},
                                              'write_locked': bool(config.security.primary_write_locked)}}
            body = json.dumps(stored, sort_keys=True, default=str, separators=(',', ':'))
            return '"' + hmac.new(_etag_key, body.encode('utf-8'), hashlib.sha256).hexdigest()[:32] + '"'

        def _check_if_match(if_match: Optional[str], device_id: str) -> None:
            """412 with the current entry when ``If-Match`` names another version
            of the device; nothing to check without the header (as before)."""
            if if_match is None or not device_id:
                return
            _i, dev_cfg, client = _find_device(device_id)
            if dev_cfg is None:
                return                                  # the caller answers 404
            cur = _device_etag(dev_cfg)
            if _etag_guard.matches(if_match, cur):
                return
            raise HTTPException(status_code=412, detail={
                "errors": ["If-Match: the device changed since it was read — reload it and apply the change again"],
                "current": _device_entry(dev_cfg, client), "etag": cur}, headers={'ETag': cur})

        def _device_tag_now(device_id) -> Optional[str]:
            """The device's ETag now (None once it is gone)."""
            if not device_id:
                return None
            _i, dev_cfg, _c = _find_device(str(device_id))
            return _device_etag(dev_cfg) if dev_cfg is not None else None

        def _entry_lock(device_id) -> Optional[threading.Lock]:
            if not device_id:
                return None
            _i, dev_cfg, _c = _find_device(str(device_id))
            if dev_cfg is None:
                return None                          # the route answers 404
            key = (f"dev:{dev_cfg.component_of}" if dev_cfg.component_of
                   else f"ep:{dev_cfg.endpoint_id}" if dev_cfg.endpoint_id else f"dev:{dev_cfg.id}")
            with _entry_locks_guard:
                return _entry_locks.setdefault(key, threading.Lock())

        def _device_write(fn):
            """A route under /api/devices/{device_id} that changes the stored
            entry: a stale If-Match answers 412, every answer says the tag after
            it (ADR-0030). Check, write and tag run under the entry's lock; put
            it ABOVE @_serialized_mutation (lock order)."""
            return _etag_guard.guard(fn, 'check',
                                      check=lambda im, kw: _check_if_match(im, kw.get('device_id')),
                                      tag=lambda kw: _device_tag_now(kw.get('device_id')),
                                      lock=lambda kw: _entry_lock(kw.get('device_id')))

        def _device_write_query(fn):
            """The same, for a route that names its device by the ``device``
            query parameter (empty: the primary) — state kept in the entry but
            written outside /api/devices/{id} (the PQ recorder)."""
            def _did(kw):
                return kw.get('device') or config.primary_id
            return _etag_guard.guard(fn, 'check',
                                     check=lambda im, kw: _check_if_match(im, _did(kw)),
                                     tag=lambda kw: _device_tag_now(_did(kw)),
                                     lock=lambda kw: _entry_lock(_did(kw)))

        def _device_runtime(fn):
            """A route under /api/devices/{device_id} that acts but stores
            nothing (a write to the device, a command, a test): never 412, the
            current tag said (ADR-0030)."""
            return _etag_guard.guard(fn, 'runtime', tag=lambda kw: _device_tag_now(kw.get('device_id')))

        def _device_specs(dev_cfg, raw_index=None):
            """(connection_spec, source_specs) as the API shows them."""
            from multibus.transports.serial.bridges import TYPES as _BT
            from multibus.devices.connection_spec import device_specs, public
            dev, per = device_specs(_stored_device_raw(dev_cfg, raw_index), config.bridges, _BT)
            return public(dev), [{'id': p['id'], **public(p)} for p in per]

        def _refuse_prefix(field: str, prefix, own) -> None:
            """422 when a prefix being set breaks the rule (ADR-0026 §4): a
            duplicate, an overlap either way, a wildcard, the gateway's root."""
            from multibus.devices.topic_prefix import prefix_errors
            errs = prefix_errors(prefix, config, own)
            if errs:
                raise HTTPException(status_code=422, detail={
                    "errors": [f"{field}: {e}" for e in errs], "fields": {field: errs[0]}})

        def _drop_spec_keys(payload):
            """A spec is output only (ADR-0026 §1): connection_spec.drop_spec_keys."""
            from multibus.devices.connection_spec import drop_spec_keys
            return drop_spec_keys(payload)

        def _set_new_node(mqtt_block: Dict, did: str) -> None:
            """The Home Assistant node a NEW device (or installation, by
            pattern) stores beside its prefix (ADR-0026 §3): under `gateway`
            `mbg_<gateway>_dev_<id>`; under `legacy` nothing (`mbg_dev_<id>`).
            Never what a payload says."""
            from multibus.core.identity import new_node
            node = new_node(did, config.identity_scope, config.gateway_id)
            if node:
                mqtt_block['ha_node'] = node
            else:
                mqtt_block.pop('ha_node', None)

        # --- public surface -------------------------------------------------
        self.rest_provider = _rest_provider
        self.rest_cfg = _rest_cfg
        self.apply_rest_push = _apply_rest_push
        self.rest_push_public = _rest_push_public
        self.device_type_of = _device_type_of
        self.find_device = _find_device
        self.writes_by_mqtt = _writes_by_mqtt
        self.write_rule = _write_rule
        self.declared_writes = _declared_writes
        self.start_device_client = _start_device_client
        self.opcua_sink = _opcua_sink
        self.opcua_block = _opcua_block
        self.meters_status = _meters_status
        self.raw_index = _raw_index
        self.stored_device_raw = _stored_device_raw
        self.primary_kept = _primary_kept
        self.device_etag = _device_etag
        self.check_if_match = _check_if_match
        self.device_tag_now = _device_tag_now
        self.entry_lock = _entry_lock
        self.device_write = _device_write
        self.device_write_query = _device_write_query
        self.device_runtime = _device_runtime
        self.device_specs = _device_specs
        self.refuse_prefix = _refuse_prefix
        self.drop_spec_keys = _drop_spec_keys
        self.set_new_node = _set_new_node
