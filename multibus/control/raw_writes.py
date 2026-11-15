"""Raw register writes — the write path (ADR-0022 D4–D8).

A raw write is a register written by address, not through a command: an HA
entity, ``POST /api/devices/{id}/write`` (guarded or ``unguarded``), the raw
lease revert. What a command writes — at any position — is never written raw
(D4); everything else travels the device's lane as a job (D5).

Extracted verbatim from ``create_api`` (P0.8b Lot C). The executor keeps every
gate, verification, audit line and log line unchanged. Its collaborators are
injected: ``control`` must not import the api layer, so the api-layer helpers
(``_find_device``, ``_modbus_driver_of``, ``_push_readback_to_store``,
``_write_rate_ok``, ``_commands_for``, ``_modbus_template_of``) arrive as
callables, some late-bound because they are defined further down in create_api
(the forward-reference pattern the module already relies on, RP-12). Lane keys
and bus locks come from :class:`multibus.control.lanes.CommandLanes`.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from multibus.control.command_dispatch import DEFAULT_MAX_AGE_S, Job as _CmdJob
from multibus.control.lanes import BusBusy as _BusBusy
from multibus.control.write_lease import revert_budget_s as write_lease_budget_s
from multibus.core.config import NETWORK_PROTOCOLS, SERIAL_PROTOCOLS

_POLLED_MODBUS = NETWORK_PROTOCOLS + SERIAL_PROTOCOLS      # every Modbus way but listening


class RawWriteExecutor:
    """The raw-write engine: conflict detection, the executor under the bus
    lock, lane submission, the dead-man revert, and the HA result publish."""

    def __init__(self, *, lanes, registry, gw_role, lease_mgr, cmd_dispatch,
                 audit_log, pub, app, logger, find_device,
                 push_readback_to_store, modbus_driver_of, modbus_template_of,
                 commands_for, write_rate_ok, cmds_gen) -> None:
        # instances / closures available where the executor is built
        _lane_key = lanes.lane_key
        _cmd_lock_for = lanes.cmd_lock_for
        _bus_lock = lanes.bus_lock
        _lease_mgr = lease_mgr
        _cmd_dispatch = cmd_dispatch
        _pub = pub
        _find_device = find_device
        # api-layer helpers (some late-bound: defined further down in create_api)
        _push_readback_to_store = push_readback_to_store
        _modbus_driver_of = modbus_driver_of
        _modbus_template_of = modbus_template_of
        _commands_for = commands_for
        _write_rate_ok = write_rate_ok
        _cmds_gen = cmds_gen

        # ── raw register writes (ADR-0022 D4–D8) ─────────────────────────────────
        #
        # A raw write is a register written by address, not through a command: an
        # HA entity, POST /api/devices/{id}/write (guarded or `unguarded`), the raw
        # lease revert. What a command writes — at any position — is never written
        # raw (D4); everything else travels the device's lane as a job (D5).

        _cmd_regs_memo: Dict[str, Tuple] = {}

        def _words_of(data_type, rtype: str = 'holding') -> int:
            if str(rtype).lower() == 'coil':
                return 1
            from multibus.codec.register_parser import RegisterParser as _RP
            return max(1, int(_RP.REGISTER_COUNTS.get(str(data_type or 'uint16').lower(), 1)))

        def _slave_of(dev_cfg) -> Tuple[str, Any]:
            """The physical slave a device entry reads: its Modbus bus and unit id.
            Two entries on one inverter (a twin, another template) are one slave.
            A unit read only over HTTP/MQTT is no Modbus slave: its `protocol`
            stays 'tcp' and its connection the empty default, which made twins
            across installations one slave (':502', unit) — RP-7."""
            srcs = list(dev_cfg.sources or [])
            for s in srcs:
                if str(s.protocol or 'tcp').lower() in _POLLED_MODBUS and getattr(s, 'connection', None) is not None:
                    return (s.connection.bus, int(s.connection.unit_id))
            if srcs:
                return ('device', dev_cfg.id)
            if str(dev_cfg.protocol or 'tcp').lower() in _POLLED_MODBUS and getattr(dev_cfg, 'connection', None) is not None:
                return (dev_cfg.connection.bus, int(dev_cfg.connection.unit_id))
            return ('device', dev_cfg.id)

        def _command_registers(dev_cfg) -> List[Tuple[str, int, int, str, str, str]]:
            """(register type, first address, end, command, channel, register) of
            every register any command of the device writes, at any position,
            enabled or not — what assign_channels unites into channels."""
            cmds = _commands_for(dev_cfg)
            if not cmds:
                return []
            tpl = _modbus_template_of(dev_cfg)
            # memoized as _commands_for is (asked per register by HA discovery)
            key = (id(tpl), _cmds_gen[0], repr([(c['def'].name, c['def'].writes) for c in cmds]))
            hit = _cmd_regs_memo.get(dev_cfg.id)
            if hit is not None and hit[0] == key:
                return hit[1]
            regs = {r.name: r for r in (tpl.registers if tpl else [])}
            out = []
            for c in cmds:
                for w in (c['def'].writes or []):
                    r = regs.get(w.get('register'))
                    if r is None:
                        continue
                    rtype = (getattr(r, 'register_type', 'holding') or 'holding').lower()
                    start = int(r.address)
                    out.append((rtype, start, start + _words_of(getattr(r, 'data_type', None), rtype),
                                c['def'].name, c.get('channel') or c['def'].name, r.name))
            _cmd_regs_memo[dev_cfg.id] = (key, out)
            return out

        def _raw_channel_conflict(dev_cfg, rtype: str, address: int, words: int) -> Optional[Dict[str, Any]]:
            """The command whose register a raw write of [address, address+words)
            would touch on the same physical slave, or None (D4, review MED 4)."""
            rtype = str(rtype or 'holding').lower()
            lo, hi = int(address), int(address) + max(1, int(words))
            slave = _slave_of(dev_cfg)
            for d, _cl in list(registry):
                if d.id != dev_cfg.id and _slave_of(d) != slave:
                    continue
                try:
                    regs = _command_registers(d)
                except Exception:  # noqa: BLE001 — a device with a broken template writes nothing anyway
                    continue
                for t, start, end, cmd, ch, name in regs:
                    if t == rtype and start < hi and lo < end:
                        return {'device': d.id, 'command': cmd, 'channel': ch, 'register': name, 'address': start}
            return None

        def _conflict_text(address: int, regname: str, k: Dict[str, Any]) -> str:
            return (f"register {address} ({regname or k['register']}) is written through the command "
                    f"{k['command']} (channel {k['channel']}"
                    + (f" of {k['device']}" if k.get('device') else '') + "): "
                    f"POST /api/devices/{k['device']}/commands/{k['command']}")

        def _raw_exec_timeout(client) -> float:
            """A raw job's running time: one write and one read, each up to the
            connection's timeout × (retries + 1)."""
            drv = _modbus_driver_of(client)
            cc = getattr(getattr(drv, 'connection', None), 'config', None)
            try:
                per = float(getattr(cc, 'timeout', 3) or 3) * (int(getattr(cc, 'retry_attempts', 3) or 0) + 1)
            except (TypeError, ValueError):
                per = 12.0
            return 3 * per

        def _read_back_failure(client) -> str:
            """Why a read-back gave nothing: the connection's word for this
            thread's last read (exception_2, timeout, …), or what None means."""
            kind = None
            try:
                conn = getattr(_modbus_driver_of(client), 'connection', None)
                kind = conn.last_read_error() if conn is not None and hasattr(conn, 'last_read_error') else None
            except Exception:  # noqa: BLE001
                kind = None
            if kind:
                return f"no answer ({kind})"
            return "no value (no refusal from the device: the shared endpoint's turn, a stop or a retired connection)"

        def _execute_raw(ctx: Dict[str, Any], job=None) -> Tuple[int, Dict[str, Any]]:
            """One raw write under the bus lock, in its lane (D5): the lease still
            current (a revert), the role,
            D4 again (a template edited since admission), the rate token, the
            write, the read-back; then the store, the log, the audit, the lease."""
            dev_id, rtype, address = ctx['dev'].id, ctx['rtype'], int(ctx['address'])
            via, who, is_revert = ctx['via'], ctx['who'], ctx.get('is_revert', False)
            value, data_type = ctx['value'], ctx['data_type']
            scale, offset = float(ctx['scale']), float(ctx.get('offset') or 0.0)
            base = {'command': f"raw:{rtype}@{address}", 'register': ctx.get('register') or None,
                    'params': {'value': value}, 'device': dev_id, 'via': via, 'by': who}
            _i, cur_cfg, c = _find_device(dev_id)
            if c is None:
                return 409, {**base, 'status': 'error', 'reason': f"device {dev_id} is not running — nothing written",
                             'ts': round(time.time(), 3)}
            cur_cfg = cur_cfg or ctx['dev']
            stamp = None
            try:
                with _bus_lock(_cmd_lock_for(c, cur_cfg), ctx.get('deadline')):
                    if is_revert and ctx.get('is_current') is not None and not ctx['is_current']():
                        return 409, {**base, 'status': 'cancelled', 'lease_renewed': True, 'ts': round(time.time(), 3),
                                     'reason': 'the lease was renewed or cleared while the revert waited — nothing written'}
                    if not gw_role.active:
                        return 503, {**base, 'status': 'rejected', 'reason': gw_role.status_text(),
                                     'ts': round(time.time(), 3)}
                    # the policy is read from the CURRENT config, resolved under the
                    # bus lock (REFAC-SAFE-2): /write-lock rebuilds the DeviceConfig
                    # (config.set_write_locked → registry.resync), so the copy taken
                    # before the lock — only needed for the lock key — can be stale
                    _i2, cur2, c2 = _find_device(dev_id)
                    if c2 is None:
                        return 409, {**base, 'status': 'error', 'ts': round(time.time(), 3),
                                     'reason': f"device {dev_id} is not running — nothing written"}
                    if c2 is not c and not is_revert:
                        # rebuilt while this write waited for the bus: `c` is the OLD client
                        # (R2D-AW-3) — never write through it
                        return 409, {**base, 'status': 'error', 'ts': round(time.time(), 3),
                                     'reason': f"device {dev_id} was rebuilt while the write waited "
                                               "— nothing written, send it again"}
                    cur_cfg = cur2 or cur_cfg
                    if cur_cfg.write_locked and not is_revert:
                        # locked while the job waited: the lock is asked again at
                        # the head, as the command path asks it (RP-10); a revert
                        # to the safe value still runs, as a command's does
                        return 403, {**base, 'status': 'rejected', 'ts': round(time.time(), 3),
                                     'reason': f"device '{dev_id}' was write-locked while the write waited — "
                                               "nothing written"}
                    # ADR-0042: a retarget does not rebuild a non-primary client, so
                    # the kept client would write the OLD wire — refuse until a
                    # restart rebuilds it. A lease-revert (is_revert) is exempt: it
                    # must still reach the old wire it wrote.
                    _aw = getattr(app.state, 'applied_wire', None)
                    if _aw is not None and not is_revert:
                        from multibus.core.config.models import device_wire_identity
                        _applied = _aw.get(dev_id)
                        if _applied is not None and device_wire_identity(cur_cfg) != _applied:
                            return 403, {**base, 'status': 'rejected', 'ts': round(time.time(), 3),
                                         'reason': f"device '{dev_id}' was retargeted — a restart is "
                                                   "required before it can be written"}
                    k = _raw_channel_conflict(cur_cfg, rtype, address, _words_of(data_type, rtype))
                    if k is not None and not is_revert:
                        return 422, {**base, 'status': 'rejected', 'ts': round(time.time(), 3),
                                     'reason': _conflict_text(address, ctx.get('register'), k)}
                    if not is_revert and ctx.get('rate_key') and not _write_rate_ok(ctx['rate_key']):
                        return 429, {**base, 'status': 'rejected', 'reason': 'write rate limit exceeded',
                                     'ts': round(time.time(), 3)}
                    ok, err, words = c.write_value(address, rtype, data_type, value, scale=scale, offset=offset,
                                                   prefer_fc6=bool(ctx.get('prefer_fc6')))
                    read_back, verified, rb_why = None, None, None
                    if ok and not is_revert:
                        try:
                            raw_back = c.read_register(address, data_type, rtype)
                            if raw_back is None:
                                # the read-back's own word: why it says nothing (RP-11)
                                rb_why = _read_back_failure(c)
                            elif rtype == 'coil':
                                from multibus.transports.modbus.modbus_client import coil_truthy as _coil_truthy
                                read_back = bool(raw_back)
                                verified = read_back == _coil_truthy(value)
                            else:
                                read_back = float(raw_back) / (scale or 1.0) + offset
                                verified = abs(read_back - float(value)) <= max(1e-6, abs(float(value)) * 1e-4)
                        except Exception as e:  # noqa: BLE001
                            rb_why = f"{type(e).__name__}: {e}"
                        if rb_why is not None:
                            logger.debug("raw write read-back failed: device=%s %s@%s: %s", dev_id, rtype, address, rb_why)
                    if ok and is_revert and k is not None:
                        # a legacy raw lease on what is now a command's register
                        # (ADR-0022 D8): the pace is stamped, not the on/off
                        stamp = k
                        rt = getattr(app.state, 'rules_runtime', None)
                        if rt is not None:
                            try:
                                rt.channel_note(cur_cfg, k['command'], {}, pace_only=True)
                            except Exception:  # noqa: BLE001
                                pass
            except _BusBusy as e:
                return 409, {**base, 'status': 'cancelled', 'reason': str(e), 'ts': round(time.time(), 3)}
            queued_ms = (round((job.started - job.admitted) * 1000.0, 1)
                         if job is not None and job.started is not None else None)
            if ok and read_back is not None:
                _push_readback_to_store(dev_id, address, read_back, rtype)     # write-then-refresh
            # the log and the audit as before 3.95, plus where it waited
            if via == 'ha':
                logger.warning("MODBUS WRITE %s (via HA): device=%s addr=%s dtype=%s value=%r%s",
                               "OK" if ok else "FAILED", dev_id, address, data_type, value, "" if ok else f" err={err}")
                detail = {"device": dev_id, "address": address, "value": value, "via": "ha-write-entity"}
                audit = dict(user="ha-mqtt", ip="mqtt", action="modbus write", status="ok" if ok else "fail")
            elif is_revert:
                logger.warning("MODBUS WRITE (lease-revert) %s: device=%s addr=%s safe=%r%s",
                               "OK" if ok else "FAILED", dev_id, address, value, "" if ok else f" err={err}")
                detail = {"value": value, "data_type": data_type, "words": list(words) if words else None,
                          "via": "lease-revert", **({"stamped": stamp['channel']} if stamp else {})}
                audit = dict(user="lease", ip="-", action="modbus write", target=f"{dev_id} {rtype}@{address}",
                             status="ok" if ok else "failed")
            else:
                logger.warning("MODBUS WRITE %s: by=%s@%s device=%s addr=%s type=%s dtype=%s value=%r words=%s%s",
                               "OK" if ok else "FAILED", who, ctx.get('ip'), dev_id, address, rtype, data_type,
                               value, words, "" if ok else f" err={err}")
                detail = {"value": value, "data_type": data_type, "words": list(words) if words else None,
                          "lease_ms": int(ctx.get('lease_ms') or 0),
                          **({"unguarded": True} if ctx.get('unguarded') else {})}
                audit = dict(user=who, ip=ctx.get('ip') or '?', action="modbus write",
                             target=f"{dev_id} {rtype}@{address}", status="ok" if ok else "failed")
            if queued_ms is not None:
                detail['queued_ms'] = queued_ms
            try:
                audit_log.append(**audit, detail=detail)
            except Exception:  # noqa: BLE001
                pass
            if not ok:
                return 502, {**base, 'status': 'error', 'reason': f"write failed: {err}",
                             'words': list(words) if words else None, 'ts': round(time.time(), 3)}
            if via == 'api':
                # the dead-man of this register: armed / renewed, or cancelled
                lease_ms = int(ctx.get('lease_ms') or 0)
                if lease_ms > 0:
                    _lease_mgr.arm(dev_id, rtype, address, lease_ms,
                                   _make_lease_revert(dev_id, rtype, address, data_type, scale, ctx.get('safe'),
                                                      off=offset),
                                   meta={'device': dev_id, 'register_type': rtype, 'address': address,
                                         'data_type': data_type, 'scale': scale, 'offset': offset,
                                         'safe_value': ctx.get('safe'), 'lease_ms': lease_ms})
                else:
                    _lease_mgr.clear(dev_id, rtype, address)
            status = 'success' if verified else 'mismatch' if verified is False else 'unverified'
            res = {**base, 'status': status, 'read_back': read_back, 'verified': verified,
                   'words': list(words) if words else None, 'ts': round(time.time(), 3)}
            if status == 'unverified' and rb_why is not None:
                res['reason'] = f"read-back failed: {rb_why}"
            if queued_ms is not None:
                res['queued_ms'] = queued_ms
            return 200, res

        def _submit_raw(ctx: Dict[str, Any], client, on_done=None):
            """A raw write admitted: its lane's job (D5) — the slot is the
            register, so a newer value of the same encoding replaces one still
            waiting, whichever face sent either (review MED 5)."""
            dev, rtype, address = ctx['dev'], ctx['rtype'], int(ctx['address'])
            revert = bool(ctx.get('is_revert'))
            job = _CmdJob(lane=_lane_key(dev, client), device=dev.id, channel=f"reg:{rtype}@{address}",
                          command=f"raw:{rtype}@{address}",
                          params={'value': ctx['value'], 'data_type': ctx['data_type'], 'scale': ctx['scale'],
                                  'offset': ctx.get('offset') or 0.0, 'prefer_fc6': bool(ctx.get('prefer_fc6')),
                                  'unguarded': bool(ctx.get('unguarded')), 'lease_ms': int(ctx.get('lease_ms') or 0)},
                          run=lambda j: _execute_raw(j.ctx, j), kind='revert' if revert else 'command',
                          coalesce=not revert, max_age_s=None if revert else DEFAULT_MAX_AGE_S,
                          on_done=on_done, ctx=ctx)
            return _cmd_dispatch.submit(job)

        def _publish_raw_result(dev_cfg, res: Dict[str, Any]) -> None:
            """An HA raw write's outcome on `<prefix>/cmd/result`, not retained
            (D6); the retained `cmd/<name>/state` is a command's only. A standby
            answers nothing (ADR-0018 D10)."""
            if not gw_role.active or not _pub.mqtt or not getattr(_pub.mqtt, 'connected', False):
                return
            try:
                _pub.mqtt.client.publish(f"{dev_cfg.mqtt_topic_prefix}/cmd/result",
                                              json.dumps(res, default=str), qos=1, retain=False)
            except Exception:  # noqa: BLE001
                pass

        def _budget_deadline() -> Optional[float]:
            """The clean shutdown's revert deadline on this thread (monotonic),
            or None outside a shutdown revert."""
            left = write_lease_budget_s()
            return None if left is None else time.monotonic() + left

        def _make_lease_revert(dev, rt, addr, dt, sc, sv, off=0.0):
            """Build the dead-man revert: resolve the *current* device client at
            revert time (surviving device restarts) and write the safe value. Used by
            both the live write path and boot recovery, so they behave identically.
            A lane `revert` job (ADR-0022 D8): the head of its lane, never
            superseded, `is_current()` asked again under the bus lock (RD-4). A
            clean shutdown's revert waits only within its budget (RC-5, review
            HIGH 1)."""
            def _revert(is_current):
                # Re-check right before the (blocking) write: if the lease was renewed
                # while we waited, abort so we don't clobber the fresh setpoint.
                if not is_current():
                    return
                _i, _c, c = _find_device(dev)
                if c is None or _c is None:
                    # device gone/restarting — raise so the dead-man retries instead
                    # of silently dropping the lease with a live setpoint
                    raise RuntimeError(f"device {dev} not running; cannot revert to safe")
                ctx = {'dev': _c, 'rtype': rt, 'address': int(addr), 'data_type': dt, 'scale': float(sc),
                       'offset': float(off or 0.0), 'value': sv, 'via': 'lease-revert', 'who': 'lease', 'ip': '-',
                       'is_revert': True, 'is_current': is_current, 'deadline': _budget_deadline()}
                job = _submit_raw(ctx, c)
                left = None if ctx['deadline'] is None else max(0.0, ctx['deadline'] - time.monotonic())
                got = job.wait(left)
                if got is None:
                    # past the budget: withdrawn if it has not started; one that
                    # runs gives up at the same deadline. Either way the lease
                    # stays on disk for boot recovery
                    if _cmd_dispatch.withdraw(job, 'the shutdown budget ran out before the bus was free '
                                                   '— left for boot recovery'):
                        raise TimeoutError(f"device {dev}: the bus stayed busy past the shutdown budget; "
                                           "left for boot recovery")
                    got = job.wait(0)
                    if got is None:
                        raise TimeoutError(f"device {dev}: the revert still runs past the shutdown budget; "
                                           "left for boot recovery")
                code, res = got
                if res.get('lease_renewed'):
                    return
                if res.get('status') not in ('success', 'mismatch', 'unverified'):
                    raise RuntimeError(f"lease-revert failed: {res.get('reason') or res.get('status')}")
            return _revert


        # public surface (create_api keeps thin facade aliases onto these)
        self.words_of = _words_of
        self.slave_of = _slave_of
        self.command_registers = _command_registers
        self.raw_channel_conflict = _raw_channel_conflict
        self.conflict_text = _conflict_text
        self.raw_exec_timeout = _raw_exec_timeout
        self.read_back_failure = _read_back_failure
        self.execute_raw = _execute_raw
        self.submit_raw = _submit_raw
        self.publish_raw_result = _publish_raw_result
        self.budget_deadline = _budget_deadline
        self.make_lease_revert = _make_lease_revert
