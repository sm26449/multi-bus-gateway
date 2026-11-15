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
"""Device / group commands — list, enable, bind, channel override, run, history.

Ten endpoints moved VERBATIM out of create_api (P0.8b Lot G) into a
``build(ctx) -> APIRouter`` module: the catalog (list_device_commands), the
binding CRUD (set_device_command_enabled / _binding / set_device_channel), the
run path (run_device_command / dry_run / run_group_command and the 3.72 power_limit
aliases) and command_history. Bodies are byte-identical; @_device_write ->
@ctx.device_write, @_device_runtime -> @ctx.device_runtime. The command-seam
functions these routes call are the CommandService instance (ADR-0038 #2,
extracted in Lot F), reached through ``ctx.cs``; _find_device and the usual
collaborators come from ctx. _BINDING_KEYS and PACE_KEYS (used only by the
binding/channel routes) move here as a module const / import.
"""
from __future__ import annotations

import json
from typing import Dict

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from multibus.control.commands import assign_channels
from multibus.control.rules import PACE_KEYS

_BINDING_KEYS = ('enabled', 'lease_s', 'safe', 'faces', 'confirm')


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["commands"])

    _cs = ctx.cs
    config = ctx.config
    app = ctx.app
    auth_state = ctx.auth_state
    audit_log = ctx.audit_log
    registry = ctx.registry
    _api_key = ctx.api_key
    _find_device = ctx.find_device
    _commands_for = _cs._commands_for
    _command_entry = _cs._command_entry
    _own_device_for_binding = _cs._own_device_for_binding
    _save_binding = _cs._save_binding
    _find_command = _cs._find_command
    _lease_pace_warnings = _cs._lease_pace_warnings
    _channel_override_errors = _cs._channel_override_errors
    _override_cid = _cs._override_cid
    _store_device_raw = _cs._store_device_raw
    _cmd_gate = _cs._cmd_gate
    _run_named_command = _cs._run_named_command
    _submit_named_command = _cs._submit_named_command
    _await_command = _cs._await_command

    @r.get("/api/devices/{device_id}/commands")
    def list_device_commands(device_id: str):
        """What this device can be told: each command's parameters (bounds,
        defaults, units), faces, whether it is enabled, its last result. A
        controller discovers this instead of hard-coding registers."""
        _i, dev_cfg, _c = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        cmds = _commands_for(dev_cfg)
        return {'device': device_id, 'commands': [_command_entry(dev_cfg, c) for c in cmds],
                # overrides whose channel the template no longer has: kept, said
                'orphan_channels': list(cmds[0].get('orphan_channels') or []) if cmds else [],
                # two keys for one channel: the one not taken (CR2-14)
                'channel_conflicts': list(cmds[0].get('channel_conflicts') or []) if cmds else []}

    @r.put("/api/devices/{device_id}/commands/{name}/enabled")
    @ctx.device_write
    def set_device_command_enabled(device_id: str, name: str, request: Request, payload: Dict = Body(...)):
        """Enable or disable one of the template's commands on a device of its
        own (an installation's units are enabled from the installation's
        group). Nothing is written to the device."""
        _own_device_for_binding(device_id, 'commands')
        # a JSON true/false, said: "false" (a string) once read as true and
        # ENABLED the command; a missing key disabled it
        on = payload.get('enabled')
        if not isinstance(on, bool):
            raise HTTPException(status_code=422, detail={"errors": ["enabled: true or false"],
                                                         "fields": {"enabled": "true or false"}})
        _save_binding(device_id, name, {'enabled': on}, request)
        return {'device': device_id, 'command': name, 'enabled': on}

    @r.put("/api/devices/{device_id}/commands/{name}")
    @ctx.device_write
    def set_device_command_binding(device_id: str, name: str, request: Request, payload: Dict = Body(...)):
        """A template command's binding on a device of its own: {enabled,
        lease_s, safe, faces, confirm}, merged into what is stored (a key not
        sent is kept, null removes it; off keeps the binding). `safe` is
        checked against the command's bounds; a lease needs a safe value.
        Audited old / new; nothing is written to the device (ADR-0019)."""
        if not isinstance(payload, dict) or not payload:
            raise HTTPException(status_code=422, detail={"errors": [
                f"send any of {', '.join(_BINDING_KEYS)}"]})
        unknown = sorted(set(payload) - set(_BINDING_KEYS))
        if unknown:
            raise HTTPException(status_code=422, detail={"errors": [
                f"{', '.join(unknown)}: not a binding field ({', '.join(_BINDING_KEYS)})"],
                "fields": {k: 'unknown' for k in unknown}})
        merged = _save_binding(device_id, name, dict(payload), request)
        dev_cfg = config.get_device(device_id)
        c = _find_command(dev_cfg, name) if dev_cfg is not None else None
        warns = _lease_pace_warnings(dev_cfg, command=name) if dev_cfg is not None else []
        return {'device': device_id, 'command': name, 'binding': merged,
                **({'entry': _command_entry(dev_cfg, c)} if c is not None else {}),
                **({'warnings': warns} if warns else {})}

    @r.put("/api/devices/{device_id}/channels/{key}")
    @ctx.device_write
    def set_device_channel(device_id: str, key: str, request: Request, payload: Dict = Body(...)):
        """This device's override of one of its template's channels — the key
        is a declared channel name or a member command's name: {min_interval_s,
        min_on_s, min_off_s, max_starts_per_h, max_age_s}, a null removes one
        (the template's again). Lower than the template is allowed (60 → 5 is
        the point); audited old / new (ADR-0019)."""
        dev_cfg, client = _own_device_for_binding(device_id, 'channel settings')
        cmds = _commands_for(dev_cfg)
        defs = {c['def'].name: c['def'] for c in cmds}
        ids = assign_channels(defs) if defs else {}
        declared = {d.channel for d in defs.values() if d.channel}
        if key not in declared and key not in ids:
            raise HTTPException(status_code=422, detail={"errors": [
                f"'{key}' is not a channel of '{device_id}' — name a channel "
                f"({', '.join(sorted(declared)) or 'none declared'}) or one of its commands"],
                "fields": {'key': 'unknown channel'}})
        errs = _channel_override_errors(payload)
        if errs:
            raise HTTPException(status_code=422, detail={"errors": [f"{k}: {v}" for k, v in errs.items()],
                                                         "fields": errs})
        # one channel, one key: `motor` and `set_frequency` both name the VFD's
        # motor — which one won depended on the file's order (CR2-14)
        _cid = _override_cid(key, defs, ids)
        other = next((k for k in (getattr(dev_cfg, 'channels', None) or {})
                      if k != key and _override_cid(k, defs, ids) == _cid), None)
        if other is not None:
            raise HTTPException(status_code=422, detail={"errors": [
                f"channel '{_cid}' is already overridden under '{other}' — edit that one"],
                "fields": {'key': f"already set under '{other}'"}})
        # a pace on a channel an armed steps rule with a fast step drives is
        # refused (ADR-0022, operator Q5) — judged on what this save changes:
        # a pace already stored is not set again by an edit of max_age_s (RP-8)
        _rt = getattr(app.state, 'rules_runtime', None)
        _before = ((getattr(dev_cfg, 'channels', None) or {}).get(key)) or {}
        _before = _before if isinstance(_before, dict) else {}
        _paced = sorted(k for k, v in payload.items() if k in PACE_KEYS and v and v != _before.get(k))
        _fast = _rt.fast_rule_on(device_id, _cid) if (_rt is not None and _paced) else None
        if _fast is not None:
            raise HTTPException(status_code=422, detail={"errors": [
                f"channel '{_cid}' is driven by the armed rule '{_fast}', a steps rule with a fast step: a pace "
                f"({', '.join(_paced)}) would hold its other reasserts while its emergency step passes it — "
                f"put the rule in shadow or remove `fast` first"],
                "fields": {k: f"refused: rule '{_fast}' has a fast step" for k in _paced}})
        said: Dict[str, Dict] = {}

        def mutate(raw):
            chans = {k: dict(v) for k, v in (raw.get('channels') or {}).items() if isinstance(v, dict)}
            prev = dict(chans.get(key) or {})
            merged = {**prev, **{k: v for k, v in payload.items() if v is not None}}
            for k, v in payload.items():
                if v is None:
                    merged.pop(k, None)
            if merged:
                chans[key] = merged
            else:
                chans.pop(key, None)
            if chans:
                raw['channels'] = chans
            else:
                raw.pop('channels', None)
            said.update(prev=prev, merged=merged)
        _store_device_raw(device_id, client, mutate)
        prev, merged = said['prev'], said['merged']
        audit_log.append(user=getattr(request.state, "user", None) or ("api-key" if _api_key else "anon"),
                         ip=request.client.host if request.client else "?", action="channel override",
                         target=f"{device_id} {key}", status="ok",
                         detail={'old': prev or None, 'new': merged or None,
                                 'template': (cmds[0].get('template_channels') or {}).get(ids.get(key, key))
                                 if cmds else None})
        dev_cfg = config.get_device(device_id)
        warns = _lease_pace_warnings(dev_cfg, channel=_cid)
        return {'device': device_id, 'channel': key, 'override': merged or None,
                'commands': [_command_entry(dev_cfg, c) for c in _commands_for(dev_cfg)
                             if c.get('channel') == (key if key in declared else ids.get(key))],
                **({'warnings': warns} if warns else {})}

    @r.post("/api/devices/{device_id}/commands/{name}")
    @ctx.device_runtime
    def run_device_command(device_id: str, name: str, request: Request, payload: Dict = Body(default={})):
        """Run a command on one unit. Body: its parameters (a bare number is
        `value`). Answers the result with its verdict: 200 success / mismatch /
        unverified, 422 rejected, 502 error, 403 gates, 404 unknown, 409 no
        Modbus source. Audited."""
        who, ip = _cmd_gate(request)
        _i, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        params = dict(payload or {})
        lease = params.pop('lease_s', None)
        cmd_id = params.pop('id', None)
        code, res = _run_named_command(dev_cfg, client, name, params, who=who, ip=ip, via='api', lease_s=lease,
                                       cmd_id=cmd_id)
        return JSONResponse(res, status_code=code)

    @r.post("/api/devices/{device_id}/commands/{name}/dry-run")
    @ctx.device_runtime
    def dry_run_device_command(device_id: str, name: str, request: Request, payload: Dict = Body(default={})):
        """The guard's current readings and the exact frames a run would write
        — nothing on the wire beyond the reads."""
        who, ip = _cmd_gate(request, dry_run=True)
        _i, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        params = dict(payload or {})
        # the id a run takes is taken (and echoed) by its preview too (CR2-11)
        cmd_id = params.pop('id', None)
        code, res = _run_named_command(dev_cfg, client, name, params, who=who, ip=ip, via='api', dry_run=True,
                                       cmd_id=cmd_id)
        return JSONResponse(res, status_code=code)

    @r.post("/api/endpoints/{endpoint_id}/groups/{group_id}/commands/{name}")
    def run_group_command(endpoint_id: str, group_id: str, name: str, request: Request, payload: Dict = Body(default={})):
        """The same command on every unit of a group, one result each; a unit
        that refuses does not stop the others, and nothing is rolled back."""
        who, ip = _cmd_gate(request)
        if config.get_raw_endpoint(endpoint_id) is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        results = []
        params = dict(payload or {})
        # the lease is the dead-man's time, as on one device — never a command
        # parameter, and checked (once, by the runner) before anything is written
        lease = params.pop('lease_s', None)
        cmd_id = params.pop('id', None)
        # every unit is submitted first, then all are waited for: units on
        # different wires run in parallel (ADR-0018 D4)
        queued = []
        for dev in config.endpoint_devices(endpoint_id):
            if (getattr(dev, 'group_id', '') or 'units') != group_id:
                continue
            _i, _c, client = registry.find(dev.id)
            queued.append((dev, _submit_named_command(dev, client, name, dict(params), who=who, ip=ip,
                                                      via='api', lease_s=lease, cmd_id=cmd_id)))
        for dev, got in queued:
            code, res = got if isinstance(got, tuple) else _await_command(got)
            results.append({'device': dev.id, 'unit_id': dev.connection.unit_id, 'http_status': code, **res})
        if not results:
            raise HTTPException(status_code=404, detail="group has no units")
        return {'endpoint': endpoint_id, 'group': group_id, 'command': name,
                'ok': all(r.get('status') == 'success' for r in results), 'units': results}

    @r.get("/api/commands/history")
    def command_history(request: Request, device: str = "", limit: int = 100):
        """Past invocations, from the audit log (newest first). It IS the audit
        log, filtered — admin only while login is on (F-33, 3.83.0)."""
        if auth_state.enabled and getattr(request.state, "role", None) != "admin":
            raise HTTPException(status_code=403, detail={"errors": ["the command history is the audit log — admin only"]})
        rows = []
        try:
            for rec in audit_log.recent(limit=max(limit * 10, 500), q='"command"'):
                if rec.get('action') != 'command':
                    continue
                if device and not str(rec.get('target', '')).startswith(device + ' '):
                    continue
                d = rec.get('detail')
                if isinstance(d, str):
                    try:
                        rec = {**rec, 'detail': json.loads(d)}
                    except ValueError:
                        pass
                rows.append(rec)
                if len(rows) >= limit:
                    break
        except Exception:  # noqa: BLE001
            pass
        return {'history': rows}

    # the 3.72.0 routes stay as aliases of the power_limit command
    @r.post("/api/devices/{device_id}/actions/power_limit")
    @ctx.device_runtime
    def device_power_limit(device_id: str, request: Request, payload: Dict = Body(...)):
        return run_device_command(device_id, 'power_limit', request, payload)

    @r.post("/api/endpoints/{endpoint_id}/groups/{group_id}/actions/power_limit")
    def group_power_limit(endpoint_id: str, group_id: str, request: Request, payload: Dict = Body(...)):
        return run_group_command(endpoint_id, group_id, 'power_limit', request, payload)

    return r
