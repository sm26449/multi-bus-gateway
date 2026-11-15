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
"""Register writes, write locks, write info/leases and poll groups.

Seven endpoints moved VERBATIM out of create_api (P0.8b Lot G) into a
``build(ctx) -> APIRouter`` module: device_events, write_device_register (the
Modbus write path, @_device_runtime), set_device_write_lock, get_write_info,
list_write_leases, get_device_poll_groups, set_device_poll_groups. Bodies are
byte-identical; the decorators become the ctx form. The raw-write engine
(Lot C, RawWriteExecutor) is reached through ``ctx.rw``; the lease manager, the
command dispatcher, the write-rate limiter and the write/poll-group helpers come
from ctx. The logger keeps the name ``multibus.api``.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Set   # noqa: F401 — verbatim route bodies

from fastapi import APIRouter, Body, HTTPException, Query, Request

from multibus.control.command_dispatch import DEFAULT_MAX_AGE_S
from multibus.core.config import READ_ONLY_PROTOCOLS
from multibus.payload import flag

logger = logging.getLogger("multibus.api")


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["writes"])

    config = ctx.config
    registry = ctx.registry
    modbus_client = ctx.modbus_client
    audit_log = ctx.audit_log
    auth_state = ctx.auth_state
    gw_role = ctx.gw_role
    _api_key = ctx.api_key
    _find_device = ctx.find_device
    _lease_mgr = ctx.lease_mgr
    _cmd_dispatch = ctx.cmd_dispatch
    _write_rate_ok = ctx.write_rate_ok
    _write_rule = ctx.write_rule
    _writes_by_mqtt = ctx.writes_by_mqtt
    _refuse_busy_poll_groups = ctx.refuse_busy_poll_groups
    _reload_poll_groups = ctx.reload_poll_groups
    _rw = ctx.rw
    _words_of = _rw.words_of
    _submit_raw = _rw.submit_raw
    _publish_raw_result = _rw.publish_raw_result      # CMD-10: tell REST raw writes too
    _conflict_text = _rw.conflict_text
    _raw_channel_conflict = _rw.raw_channel_conflict
    _raw_exec_timeout = _rw.raw_exec_timeout

    @r.get("/api/devices/{device_id}/events")
    def device_events(device_id: str, limit: int = Query(200, ge=1, le=500),
                      level: str = Query("")):
        """What this device's acquisition has been doing, as a readable log.

        The connection has always kept a timestamped ring of its own troubles —
        unreachable, recovered, forced reopen, bus busy, failed batch — plus the
        poller's overrun episodes. Until now the only reader was the alert
        harvester, which fired on them and dropped them, so diagnosing a
        misbehaving endpoint meant grepping container logs for facts the process
        already had in memory and could not be asked for.

        Returned alongside the log is the live per-group state (interval, last
        sweep, reads per sweep, overruns), because "what happened" and "what it
        is doing now" are the same question when an endpoint is struggling.
        """
        _i, dev_cfg, client = _find_device(device_id)
        if client is None:
            raise HTTPException(status_code=404,
                                detail=f"device '{device_id}' is not running")
        try:
            st = client.get_stats() or {}
        except Exception:  # noqa: BLE001 — a stats blip must not 500 the log
            st = {}
        events = list(st.get('events') or [])
        if level:
            keep = {x.strip() for x in level.split(',') if x.strip()}
            events = [e for e in events if e.get('level') in keep]
        # newest first: an operator opening the page is asking "what just
        # happened", not "what happened when the process started"
        events = events[::-1][:limit]
        return {
            "device_id": device_id,
            "name": (dev_cfg.name or dev_cfg.id) if dev_cfg else device_id,
            # A unit reached several ways needs both halves of the story: how
            # each source is doing, and which source currently owns each field.
            # Flat JSON on purpose — a Node-RED http-request node reads this
            # without unwrapping anything.
            "sources": st.get('sources') or [],
            "provenance": st.get('provenance') or {},
            "field_handovers": st.get('field_handovers', 0),
            "events": events,
            "truncated": len(st.get('events') or []) >= 500,
            "poll_groups": st.get('poll_groups_detail') or [],
            "counters": {
                "successful_reads": st.get('successful_reads'),
                "failed_reads": st.get('failed_reads'),
                "batch_failures": getattr(getattr(client, 'connection', None),
                                           'batch_failures', None),
                "forced_reopens": getattr(getattr(client, 'connection', None),
                                          'forced_reopens', None),
                "error_counts": st.get('error_counts') or {},
                "last_latency_ms": st.get('last_latency_ms'),
                "last_success_ts": st.get('last_success_ts'),
                "last_failure_ts": st.get('last_failure_ts'),
            },
        }

    @r.post("/api/devices/{device_id}/write")
    @ctx.device_runtime
    def write_device_register(device_id: str, request: Request, payload: Dict = Body(...)):
        """Write a value to a device (Modbus FC5 coil / FC6+FC16 holding).

        F3a trust model — CAPABILITY IS THE BASE, GUARDS ARE OPT-IN:
        - armed by the security.allow_writes master switch (default off);
        - per-device `write_locked` refuses everything (the primary ships
          locked via security.primary_write_locked — configuration, not code);
        - a register DECLARED writable writes with its template encoding, and
          any guards the user declared (write_min/write_max/write_allowed)
          enforce — they are the user's own declaration, not a product limit;
        - an UNDECLARED register can still be written through the raw path by
          passing `unguarded: true` (+ its data_type/scale) — the deliberate
          statement that no envelope exists;
        - HTTP/JSON devices and input/discrete registers cannot be written;
        - authenticated + rate-limited + audited; the value is read back.
        """
        from multibus.core.config import normalize_register_type
        if not gw_role.active:
            raise HTTPException(status_code=503, detail={"errors": [gw_role.status_text()]})
        if not config.security.allow_writes:
            raise HTTPException(status_code=403, detail={"errors": [
                "Writes are disabled — set security.allow_writes=true to enable"]})
        # Writes must be authenticated so every write is attributable — refuse
        # unless login is enabled or an API key is configured. (Reads stay open
        # on a trusted LAN; writes touch hardware, so they always need a credential.)
        if not (auth_state.enabled or _api_key):
            raise HTTPException(status_code=403, detail={"errors": [
                "writes require authentication — enable login (ui.auth) or set an API_KEY"]})
        if not _write_rate_ok(request.client.host if request.client else "?"):
            raise HTTPException(status_code=429, detail={"errors": [
                f"write rate limit exceeded (> {config.security.write_rate_limit_per_s}/s) — slow down"]})
        _idx, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        # what can never be written is said first (a listened bus stays one
        # whether locked or not), then the lock — the order the device
        # menus give their reasons in (ADR-0032 §3, L3.10 review m2)
        if dev_cfg.protocol == 'http':
            raise HTTPException(status_code=400, detail={"errors": [
                "HTTP/JSON devices cannot be written"]})
        _protos = [str(getattr(x, 'protocol', '') or '').lower()
                   for x in (dev_cfg.sources or []) if getattr(x, 'enabled', True)] or [dev_cfg.protocol]
        if all(p_ in READ_ONLY_PROTOCOLS for p_ in _protos):
            raise HTTPException(status_code=400, detail={"errors": [
                f"{', '.join(dict.fromkeys(_protos))} devices are read-only — the gateway never "
                f"writes to that bus"]})
        if dev_cfg.write_locked:
            raise HTTPException(status_code=403, detail={"errors": [
                f"device '{device_id}' is write-locked — unlock it "
                + ("via security.primary_write_locked" if dev_cfg.primary
                   else "from the device's ⋯ → Unlock writes… (admin)")]})
        rtype = normalize_register_type(payload.get('register_type') or payload.get('fc') or 'holding')
        if rtype in ('input', 'discrete'):
            raise HTTPException(status_code=400, detail={"errors": [
                f"{rtype} registers are read-only — write holding (FC16) or coil (FC5)"]})
        try:
            address = int(payload.get('address'))
            assert 0 <= address <= 65535
        except (TypeError, ValueError, AssertionError):
            raise HTTPException(status_code=422, detail={"errors": ["address must be an integer 0..65535"]})
        if 'value' not in payload:
            raise HTTPException(status_code=422, detail={"errors": ["value is required"]})
        if _writes_by_mqtt(dev_cfg):
            # as on HA (ADR-0022, review MED 4e): there is no register to write
            raise HTTPException(status_code=422, detail={"errors": [
                f"'{device_id}' is read over MQTT — it is written only through an enabled command: "
                f"GET /api/devices/{device_id}/commands"]})
        # ── write envelope: declared path (guards enforce) or raw path (opt-in) ──
        rule = _write_rule(dev_cfg, address, rtype)
        unguarded = False
        if rule is not None and rule.writable:
            # What a command writes is refused raw (D4, below); a register whose
            # engineering value depends on a live scale factor is written
            # THROUGH its command — alone, the raw path would encode it without
            # the scale factor and "verify" the wrong value (3.83.0)
            _k = _raw_channel_conflict(dev_cfg, rtype, address, _words_of(rule.data_type or 'uint16', rtype))
            if _k is not None:
                raise HTTPException(status_code=422, detail={"errors": [
                    _conflict_text(address, getattr(rule, 'name', ''), _k)],
                    "status": "rejected", "through_command": _k['command'], "channel": _k['channel']})
            if getattr(rule, 'scale_from', None):
                raise HTTPException(status_code=422, detail={"errors": [
                    f"register {address} ({getattr(rule, 'name', '?')}) scales by a live factor — "
                    f"it is written through the command that owns it"]})
            # DECLARED: encoding is a property of the register (its template
            # row), NOT caller-controlled — a declared register always writes
            # with its declared type and scale, so a caller can't corrupt it
            # (or an adjacent register) with a mismatched data_type/word-count.
            data_type = (rule.data_type or 'uint16').lower()
            scale = float(rule.scale if rule.scale is not None else 1.0)
            _w_offset = float(getattr(rule, 'offset', 0.0) or 0.0)
        elif flag(payload, 'unguarded', False):
            # RAW (L0): the caller explicitly states no envelope exists. The
            # payload owns the encoding; nothing is clamped or checked beyond
            # type sanity. Master switch + device lock + auth + rate limit +
            # audit still apply — safety by configuration, not prohibition.
            # Bypassing the template envelope is an ADMIN act: the operator
            # role is "live actions within the declared envelope" (3.80.0).
            # With login on, the API key is NOT a substitute for the role —
            # every operator must present it on every write, so it would be
            # a free escalation (3.83.0); it stands in only on an auth-off box.
            _role = getattr(request.state, 'role', None)
            if auth_state.enabled and _role != 'admin':
                raise HTTPException(status_code=403, detail={"errors": [
                    "unguarded (raw) writes need the admin account"]})
            unguarded = True
            rule = None
            data_type = str(payload.get('data_type') or 'uint16').lower()
            from multibus.codec.register_parser import RegisterParser as _RP
            if data_type not in _RP.REGISTER_COUNTS or data_type.startswith('string'):
                raise HTTPException(status_code=422, detail={"errors": [
                    f"data_type '{data_type}' is not writable — use one of: "
                    + ", ".join(sorted(k for k in _RP.REGISTER_COUNTS
                                       if not k.startswith('string')))]})
            try:
                scale = float(payload.get('scale', 1) or 1)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail={"errors": ["scale must be numeric"]})
            _w_offset = 0.0
        else:
            raise HTTPException(status_code=403, detail={"errors": [
                f"address {address} ({rtype}) is not declared writable on this "
                f"device. Either declare it writable in the device template "
                f"(guards optional — each one you declare is enforced and "
                f"improves the UI), or pass unguarded:true for a raw write "
                f"with your own data_type/scale."]})
        # What any command writes, at any position, on this physical slave is
        # never written raw — guarded or unguarded, admin or not (ADR-0022 D4,
        # operator Q2): WMaxLim_Ena = 0 alone lifts an OV curtail while the
        # percentage still reads 50, a drift the rule never sees
        _k = _raw_channel_conflict(dev_cfg, rtype, address, _words_of(data_type, rtype))
        if _k is not None:
            raise HTTPException(status_code=422, detail={"errors": [
                _conflict_text(address, getattr(rule, 'name', '') if rule is not None else '', _k)],
                "status": "rejected", "through_command": _k['command'], "channel": _k['channel']})
        prefer_fc6 = flag(payload, 'prefer_fc6', False)
        if rtype == 'holding':
            try:
                _fval = float(payload.get('value'))
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail={"errors": ["value must be numeric"]})
            # NaN/Inf survive the min/max guards below (every comparison against
            # a non-finite float is False) and struct.pack would ship the raw
            # bit pattern to a real device — reject up front. (The encoder's
            # deliberate NaN sentinel path is unaffected; that never comes from
            # a write request.)
            import math as _math
            if not _math.isfinite(_fval):
                raise HTTPException(status_code=422, detail={"errors": [
                    "value must be a finite number (NaN/Infinity rejected)"]})
            # declared guards enforce — each one is the user's own declaration
            if rule is not None and rule.write_min is not None and _fval < rule.write_min:
                raise HTTPException(status_code=422, detail={"errors": [
                    f"value {_fval} is below the register minimum {rule.write_min}"]})
            if rule is not None and rule.write_max is not None and _fval > rule.write_max:
                raise HTTPException(status_code=422, detail={"errors": [
                    f"value {_fval} is above the register maximum {rule.write_max}"]})
            if (rule is not None and getattr(rule, 'write_allowed', None)
                    and _fval not in [float(v) for v in rule.write_allowed]):
                raise HTTPException(status_code=422, detail={"errors": [
                    f"value {_fval} is not in the register's allowed set "
                    f"{rule.write_allowed}"]})
        _safe = rule.write_safe if rule is not None else None
        lease_ms = int(payload.get('lease_ms', 0) or 0)
        if lease_ms > 0 and _safe is None:
            raise HTTPException(status_code=422, detail={"errors": [
                "lease requested but the register has no write_safe value in "
                "the template (raw unguarded writes cannot lease)"]})
        if client is None:
            raise HTTPException(status_code=409, detail={"errors": ["device is not running"]})
        # what the encoder would have to clamp is refused here (F-15, 3.83.0):
        # a clamped word is a value nobody asked for, and it was "verified"
        from multibus.codec.encoder import RegisterEncoder as _Enc
        _rng = _Enc.int_range(data_type)
        if _rng is not None and rtype != 'coil':
            try:
                _raw = int(round((float(payload.get('value')) - _w_offset) * float(scale)))
            except (TypeError, ValueError):
                _raw = None
            if _raw is not None and not (_rng[0] <= _raw <= _rng[1]):
                raise HTTPException(status_code=422, detail={"errors": [
                    f"value {payload.get('value')} encodes to {_raw}, outside {data_type} "
                    f"[{_rng[0]}, {_rng[1]}]"]})
        _who = getattr(request.state, "user", None) or ("api-key" if _api_key else "anon")
        _src = request.client.host if request.client else "?"
        ctx = {'dev': dev_cfg, 'rtype': rtype, 'address': address,
               'register': getattr(rule, 'name', '') if rule is not None else '',
               'data_type': data_type, 'scale': float(scale), 'offset': _w_offset, 'value': payload.get('value'),
               'prefer_fc6': prefer_fc6, 'unguarded': unguarded, 'lease_ms': lease_ms, 'safe': _safe,
               'via': 'api', 'who': _who, 'ip': _src}
        # a lane job (ADR-0022 D5): latest-wins on the register, an expiry,
        # the bus never held by the HTTP thread; waited for here. Every outcome
        # is told on cmd/result too (CMD-10, §2 L1), as HA raw writes are.
        def _told(_code, res, _d=dev_cfg):
            res = {'command': f"raw:{rtype}@{address}",
                   'register': getattr(rule, 'name', '') if rule is not None else None,
                   'params': {'value': payload.get('value')}, 'device': device_id, 'via': 'api',
                   'by': _who, 'ts': round(time.time(), 3), **res}
            _publish_raw_result(_d, res)
        job = _submit_raw(ctx, client, on_done=_told)
        code, res = _cmd_dispatch.wait_for(job, DEFAULT_MAX_AGE_S + _raw_exec_timeout(client) + 1.0)
        if code != 200:
            # the shape the UI reads (res.detail.errors), with the lane's word
            raise HTTPException(status_code=code, detail={
                "errors": [res.get('reason') or res.get('status') or 'not written'],
                "status": res.get('status'),
                **{k: res[k] for k in ('queued_ms', 'words', 'id') if res.get(k) is not None}})
        out = {"ok": True, "device": device_id, "address": address, "register_type": rtype,
               "data_type": data_type, "written": payload.get('value'),
               "words": res.get('words'), "read_back": res.get('read_back'), "verified": res.get('verified'),
               "lease_ms": lease_ms or None,
               "reverts_to": _safe if lease_ms > 0 else None,
               "unguarded": unguarded or None}
        if res.get('queued_ms') is not None:
            out['queued_ms'] = res['queued_ms']
        if res.get('reason'):
            out['reason'] = res['reason']                   # why it is unverified (RP-11)
        return out

    @r.post("/api/devices/{device_id}/write-lock")
    @ctx.device_write
    @ctx.serialized_mutation
    def set_device_write_lock(device_id: str, request: Request, payload: Dict = Body(...)):
        """Set/clear the per-device write lock (F3a). A locked device refuses
        every write regardless of guards. Endpoint units lock at the ENDPOINT level
        (one physical endpoint — its units lock together); the primary maps to
        security.primary_write_locked. Audited."""
        if 'locked' not in payload:
            raise HTTPException(status_code=422, detail={"errors": ["'locked' (bool) is required"]})
        locked = flag(payload, 'locked')
        try:
            config.set_write_locked(device_id, locked)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        # refresh the registry snapshot so /api/devices reflects the new lock
        registry.resync(config.devices, modbus_client)
        _who = getattr(request.state, "user", None) or ("api-key" if _api_key else "anon")
        _src = request.client.host if request.client else "?"
        audit_log.append(user=_who, ip=_src, action="write-lock",
                         target=device_id, status="ok",
                         detail={"locked": locked})
        logger.warning("WRITE-LOCK %s: device=%s by=%s@%s",
                       "SET" if locked else "CLEARED", device_id, _who, _src)
        return {"ok": True, "device": device_id, "write_locked": locked}

    @r.get("/api/devices/{device_id}/write-info/{register_type}/{address}")
    def get_write_info(device_id: str, register_type: str, address: int):
        """What a write to (register_type, address) would look like: declared?
        guards? lock state? — drives the write dialog's affordances (bounded
        input / dropdown / raw-with-confirmation)."""
        from multibus.core.config import normalize_register_type
        _i, dev_cfg, _c = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        rtype = normalize_register_type(register_type)
        rule = _write_rule(dev_cfg, address, rtype)
        declared = bool(rule is not None and rule.writable)
        # a register a command writes is never written raw (ADR-0022 D4): the
        # dialog points to the command instead
        try:
            _k = _raw_channel_conflict(dev_cfg, rtype, int(address),
                                       _words_of(rule.data_type if declared else 'uint16', rtype))
        except Exception:  # noqa: BLE001
            _k = None
        return {
            "device": device_id, "address": address, "register_type": rtype,
            "through_command": _k['command'] if _k else None,
            "channel": _k['channel'] if _k else None,
            "writes_enabled": bool(config.security.allow_writes),
            "write_locked": bool(dev_cfg.write_locked),
            "declared": declared,
            "data_type": (rule.data_type if declared else None),
            "scale": (rule.scale if declared else None),
            "write_min": (rule.write_min if declared else None),
            "write_max": (rule.write_max if declared else None),
            "write_allowed": (getattr(rule, 'write_allowed', None) if declared else None),
            "write_safe": (rule.write_safe if declared else None),
        }

    @r.get("/api/writes/leases")
    def list_write_leases():
        """Active write-leases (dead-man switches) with time remaining."""
        return {"leases": _lease_mgr.snapshot()}

    @r.get("/api/devices/{device_id}/poll-groups")
    def get_device_poll_groups(device_id: str):
        """Current poll-group intervals for a device (from its registers file).
        Only groups the device's registers actually USE are returned — the
        merged global groups (e.g. the primary's 0.25 s realtime) used to show
        on every device and read as if the device polled at that rate."""
        _i, dev_cfg, _c = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        regs, groups = config.load_device_registers(dev_cfg)
        used = {r.poll_group or 'normal' for r in regs}
        if used:                       # no registers yet → show all (seed view)
            groups = {n: g for n, g in groups.items() if n in used}
        return {"device": device_id, "poll_groups": {
            n: {"interval": g.interval, "description": g.description} for n, g in groups.items()}}

    @r.post("/api/devices/{device_id}/poll-groups")
    @ctx.device_write
    def set_device_poll_groups(device_id: str, payload: Dict = Body(...)):
        """Update a device's poll-group intervals and live-restart its pollers so
        each group re-samples at its own rate (e.g. slow down a fragile HTTP/
        gateway source)."""
        idx, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        from multibus.devices.device_patch import poll_groups_clean
        groups_in = payload.get("poll_groups", payload) or {}
        if not isinstance(groups_in, dict):
            groups_in = {}
        clean, why = poll_groups_clean(groups_in)
        if why:
            raise HTTPException(status_code=400, detail=why)
        _refuse_busy_poll_groups(dev_cfg, clean)
        try:
            clean = config.save_device_poll_groups(device_id, clean)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        _reload_poll_groups(dev_cfg, client)
        return {"status": "ok", "device": device_id, "poll_groups": clean}

    return r
