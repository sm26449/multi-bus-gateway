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
"""Meter links — which meters measure a device, and in what role.

Three endpoints (/api/meter-roles, PUT /api/devices/{id}/meters, PUT
/api/devices/{id}/energy-meter) plus their two create_api helpers
(``_device_reads``, ``_save_meters``, used only here — the meters sub-cluster
Lot F left behind) moved VERBATIM out of create_api (P0.8b Lot G) into a
``build(ctx) -> APIRouter`` module. Bodies are byte-identical; the create_api
collaborators are aliased from ``ctx`` (the ``@_device_write`` guard becomes
``@ctx.device_write``). No meter is ever written — these only store links.
"""
from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Body, HTTPException


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["meters"])

    config = ctx.config
    registry = ctx.registry
    calc_engine = ctx.calc_engine
    _device_mutation_lock = ctx.mutation_lock
    _find_device = ctx.find_device
    _fields_cache = ctx.fields_cache
    _sync_device_discovery = ctx.sync_device_discovery

    def _device_reads(dev_cfg) -> set:
        """The names a device reads itself (registers and its own calculated
        fields) — a meter link may not give it one of them a second time."""
        names = {r.name for r in config.unit_registers(dev_cfg)}
        try:
            names |= {str(c.get('name')) for c in config.load_calculated(dev_cfg.id) or [] if isinstance(c, dict)}
        except Exception:  # noqa: BLE001 — unreadable calculated file: the registers still judge
            pass
        return names

    _ABSENT = object()

    def _save_meters(device_id: str, raw_links, dry_run: bool = False, consumption=None,
                     raw_options=_ABSENT) -> Dict:
        """Validate and store a device's meter links (meter_links.py), in the
        device lock: the device re-reads its linked values, what a rule may
        read is judged afresh, and HA is offered the new fields. A save of
        what is stored answers `unchanged`; nothing is written to any meter.
        ``consumption`` (the earlier route): only the consumption link is
        set, from {device, power, energy} or {} — the other links, and the
        sign of a link to the same meter, are read inside the lock."""
        from multibus.devices import meter_links
        with _device_mutation_lock:
            try:
                _i, dev_cfg, client = _find_device(device_id)
                if dev_cfg is None:
                    raise HTTPException(status_code=404, detail="device not found")
                if consumption is not None:
                    now = meter_links.link_for(dev_cfg, 'consumption') or {}
                    raw_links = [ln for ln in meter_links.links_of(dev_cfg) if ln['role'] != 'consumption']
                    if consumption.get('device'):
                        sign = now.get('sign', 'auto') if now.get('device') == consumption['device'] else 'auto'
                        raw_links.append({'role': 'consumption', **consumption, 'sign': sign})
                links, errs = meter_links.validate(config, dev_cfg, raw_links,
                                                   reserved_names=_device_reads(dev_cfg))
                # the honesty thresholds: absent keeps the stored ones, {} the
                # defaults; they belong to an input / output pair only
                options = None
                has_pair = ({ln['role'] for ln in links} >= {'input', 'output'}) if not errs else True
                if raw_options is not _ABSENT:
                    options, oerrs = meter_links.check_options(raw_options)
                    errs = {**errs, **oerrs}
                    if not oerrs and not has_pair and raw_options:
                        errs['options'] = 'thresholds belong to an input and an output meter — this device has no pair'
                if not errs and not has_pair and getattr(dev_cfg, 'meter_options', None):
                    options = {}              # the pair is gone: its thresholds with it
                if not errs and dev_cfg.protocol == 'none' and not links:
                    errs['meters'] = ('a meter-only device is its meters — '
                                      'delete the device instead of removing them all')
                if errs:
                    raise HTTPException(status_code=422, detail={"errors": [f"{k}: {v}" for k, v in errs.items()],
                                                                 "fields": errs})
                same_opts = options is None or options == dict(getattr(dev_cfg, 'meter_options', None) or {})
                if links == meter_links.links_of(dev_cfg) and same_opts:
                    return {'status': 'unchanged', 'device': device_id, 'meters': links,
                            'options': meter_links.options_of(dev_cfg)}
                if dry_run:
                    return {'status': 'valid', 'would': 'update', 'device': device_id, 'meters': links,
                            'options': {**meter_links.OPTION_DEFAULTS,
                                        **(options if options is not None
                                           else getattr(dev_cfg, 'meter_options', None) or {})}}
                try:
                    config.set_meters(device_id, links, options=options)
                except ValueError as e:
                    raise HTTPException(status_code=422, detail={"errors": [str(e)]})
                new_cfg = config.get_device(device_id)
                # no restart: what a device reads through its meters is a
                # calculated value, recomputed in place
                registry.replace(device_id, new_cfg, add_if_missing=False)   # config only: client kept
                calc_engine.load(device_id)
            finally:
                _fields_cache.clear()
        _sync_device_discovery()
        return {'status': 'updated', 'device': device_id, 'meters': meter_links.links_of(new_cfg),
                'energy_meter': dict(new_cfg.energy_meter or {}),
                'options': meter_links.options_of(new_cfg)}

    @r.get("/api/meter-roles")
    def meter_roles():
        """The roles a meter can have on a device, and how its power reads."""
        from multibus.devices import meter_links
        return meter_links.catalog()

    @r.put("/api/devices/{device_id}/meters")
    @ctx.device_write
    def set_device_meters(device_id: str, payload: Dict = Body(...), dry_run: bool = False):
        """The meters that measure this device, each in a role — the whole
        list: {meters: [{role, device, power, energy, sign}]}, [] for none —
        and, for an input / output pair, when its loss and efficiency are
        published: {options: {min_load_w, tolerance_pct, max_skew_s}}
        (absent: kept; {} or a key left out: its default; null refused).
        `?dry_run=true`: every check, nothing stored. Refusals carry `fields`
        keyed `meters[i].power` / `options.min_load_w`."""
        if not isinstance(payload, dict) or 'meters' not in payload:
            raise HTTPException(status_code=422, detail={"errors": ["send {meters: [...]}"],
                                                         "fields": {"meters": "required"}})
        return _save_meters(device_id, payload.get('meters'), dry_run=dry_run,
                            raw_options=payload['options'] if 'options' in payload else _ABSENT)

    @r.put("/api/devices/{device_id}/energy-meter")
    @ctx.device_write
    def set_device_energy_meter(device_id: str, payload: Dict = Body(...)):
        """The earlier form: name the device that measures this one's
        electricity — {device, power, energy} — or {} to have none. It sets
        the consumption link (meters with input / output: use /meters)."""
        from multibus.devices import meter_links
        link = {k: str(payload.get(k) or '') for k in ('device', 'power', 'energy') if payload.get(k)}
        try:
            _save_meters(device_id, None, consumption=link)
        except HTTPException as e:
            d = e.detail
            if e.status_code == 422 and isinstance(d, dict):
                # the earlier answer: "power: …", no meters[i] paths
                raise HTTPException(status_code=422, detail={
                    "errors": [f"{k.rsplit('.', 1)[-1]}: {v}" if '.' in k else v
                               for k, v in (d.get('fields') or {}).items()] or d.get('errors', [])})
            raise
        dev = config.get_device(device_id)
        cons = meter_links.link_for(dev, 'consumption') if dev else None
        return {'device': device_id,
                'energy_meter': {k: v for k, v in (cons or {}).items()
                                 if k != 'role' and (k != 'sign' or v != 'auto')}}

    return r
