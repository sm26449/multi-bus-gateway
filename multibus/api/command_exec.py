# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""MQTT-face write command and lease-revert machinery, extracted whole from create_api (P0.8b squeeze).

CommandExec holds the command write-path closures that used to be nested in create_api and
passed into CommandService / registered on the broker: make_command_lease_revert, the revert
re-read pair (reread_after_revert / arm_revert_reread), push_readback_to_store, and the big
mqtt_write_command handler. Built after CommandService's facades exist (it uses several of
them directly); the four funcs CommandService needs at its own construction are threaded in
as late-bound thunks. Kept as closures built in __init__ so the bodies are verbatim.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from multibus.core.config import store_key
from multibus.transports.driver import RegisterReadable

logger = logging.getLogger("multibus.api")  # preserve caplog logger name


class CommandExec:
    """The command write-path seam (ADR-0038 service, api-layer for now)."""

    def __init__(self, *, registry, _find_device, _write_rule, _writes_by_mqtt, _revert_rereads, _revert_rereads_lock, _commands_for, _submit_named_command, _publish_command_result, _modbus_driver_of, _mqtt_writes_refused, _submit_raw, _publish_raw_result, _raw_channel_conflict, _words_of, _conflict_text):
        def _push_readback_to_store(device_id, address, value, register_type='holding'):
            """Write-then-refresh: push a just-written register's read-back value into
            the live store so the vmeters / UI reflect the new value AT ONCE instead
            of at the next poll (a slow-group setpoint could otherwise lag 60 s)."""
            if value is None:
                return
            store = registry.store_for(device_id)
            address = store_key(address, register_type)        # the key of THAT table's entry
            if store is not None and address in store:
                # whole-dict swap, like the poller (audit DP-35): field-by-field
                # mutation was the one non-atomic store write — a concurrent
                # reader could see the new value with the old mono stamp
                e = dict(store[address])
                e['value'] = value
                e['ts'] = time.time()
                e['mono'] = time.monotonic()
                store[address] = e

        def _reread_after_revert(device_id: str, cmd_name: str, group: str,
                                 remaining: tuple = (), gen: Optional[int] = None) -> None:
            """Timer callback: sweep the read-back group once the device-side
            revert timer of ``cmd_name`` should have fired (device looked up
            again — it may have been rebuilt or removed meanwhile), then arm the
            next sweep of the series — unless a newer command superseded it."""
            with _revert_rereads_lock:
                cur = _revert_rereads.get((device_id, cmd_name))
            if gen is not None and cur is not None and cur[0] != gen:
                return
            try:
                found = registry.find(device_id)
                drv = _modbus_driver_of(found[2]) if found and found[2] is not None else None
                if isinstance(drv, RegisterReadable):
                    drv.poll_now(group)
                    logger.info("command %s on %s: read-back after the revert timer (%s)",
                                cmd_name, device_id, group)
            except Exception:  # noqa: BLE001
                pass
            if remaining:
                _arm_revert_reread(device_id, cmd_name, group, remaining, gen=gen)

        def _arm_revert_reread(device_id: str, cmd_name: str, group: str, delays: tuple,
                               gen: Optional[int] = None) -> None:
            """``delays`` are absolute offsets from the revert mark; arm the first,
            pass the rest on as offsets from that one. Without ``gen`` this starts
            a new series and cancels the one still pending for the same device
            and command; with it, it continues that series if it is still the
            current one."""
            first, rest = delays[0], tuple(d - delays[0] for d in delays[1:])
            key = (device_id, cmd_name)
            with _revert_rereads_lock:
                cur = _revert_rereads.get(key)
                if gen is None:
                    if cur is not None:
                        cur[1].cancel()
                    gen = (cur[0] + 1) if cur is not None else 1
                elif cur is not None and cur[0] != gen:
                    return
                t = threading.Timer(first, _reread_after_revert, args=(device_id, cmd_name, group, rest, gen))
                t.daemon = True
                _revert_rereads[key] = (gen, t)
                t.start()

        def _mqtt_write_command(device_id, register, payload):
            """Execute an HA number/select command as a Modbus write. The broker is
            NOT a trusted caller, so EVERYTHING is re-validated here regardless of
            what discovery advertised: both write gates, the template write envelope
            (writability + bounds), a rate limit, and an audit record. Runs on the
            MQTT command worker — never on paho's network thread (audit M4). A raw
            write is handed to the device's lane (ADR-0022 D5): the worker never
            waits for the bus, and every outcome is told on `cmd/result` (D6)."""
            import math as _math
            addr = getattr(register, 'address', '?')
            rname = getattr(register, 'name', '') or ''
            known = {}

            def refuse(why: str, fmt: str, *args) -> None:
                logger.warning(fmt, *args)
                d = known.get('dev')
                if d is not None:
                    _publish_raw_result(d, {'command': f"raw:holding@{addr}", 'register': rname or None,
                                            'params': {'value': payload}, 'status': 'rejected', 'reason': why,
                                            'device': d.id, 'via': 'ha', 'by': 'ha-mqtt', 'ts': round(time.time(), 3)})
            refused = _mqtt_writes_refused()
            if refused:
                # tell the refusal on cmd/result too (CMD-9, §2 L1): mirrors the
                # /cmd/ path (ADR-0018 D8, CMD-Q-4). Resolve the device for its
                # topic; refuse() logs the same line and publishes when it can.
                _i, _d, _c = _find_device(device_id)
                known['dev'] = _d if (_d is not None and not _d.primary and _d.protocol != 'http') else None
                return refuse(refused, "MQTT write on %s/%s ignored: %s", device_id, rname or '?', refused)
            _idx, dev_cfg, client = _find_device(device_id)
            if dev_cfg is None or client is None or dev_cfg.primary or dev_cfg.protocol == 'http':
                return
            known['dev'] = dev_cfg
            if dev_cfg.write_locked:
                # the lock is per device, whichever face asks (F-13, 3.83.0)
                return refuse(f"device '{device_id}' is write-locked",
                              "MQTT write REJECTED (device write-locked): device=%s addr=%s", device_id, addr)
            rule = _write_rule(dev_cfg, register.address, 'holding')
            if rule is None or not rule.writable:
                return refuse(f"register {addr} is not declared writable",
                              "MQTT write REJECTED (not writable): device=%s addr=%s", device_id, addr)
            cm = next((c for c in _commands_for(dev_cfg) if c['enabled'] and c['faces'].get('ha', True)
                       and c['def'].writes and c['def'].writes[0].get('register') == rname
                       and c['def'].value_param is not None), None)
            if cm is not None:
                # a register that a command writes first (WMaxLimPct) is written
                # THROUGH the command — alone it would do nothing, or harm
                try:
                    value = float(payload)
                except (TypeError, ValueError):
                    # REFAC-NUM-1: publish the rejection, don't just log — the bare
                    # register path (below) already refuses; this command-backed path
                    # used to swallow it, so HA saw no cmd/result for a bad number.
                    return refuse(f"not a number: {payload!r}", "HA command REJECTED (non-numeric %r): "
                                  "device=%s addr=%s", payload, device_id, addr)
                # handed to the device's lane: the MQTT worker does not wait for
                # the bus (ADR-0018 D11); every outcome is told on cmd/result
                got = _submit_named_command(dev_cfg, client, cm['def'].name, {'value': value},
                                            who='ha', ip='mqtt', via='ha', tell=True)
                if isinstance(got, tuple):
                    _publish_command_result(dev_cfg, cm['def'].name, got[1])
                return
            # A device read over MQTT takes only commands; a register whose
            # engineering value depends on a live scale factor, or that ANY command
            # writes at ANY position on this slave, never takes a raw write (D4,
            # ADR-0022): WMaxLim_Ena written alone lifts a curtail the OV rule
            # cannot see. Mirrors the HTTP route.
            if _writes_by_mqtt(dev_cfg):
                return refuse("a device read over MQTT is written only through an enabled command",
                              "HA write REJECTED: device=%s register=%s — a device read over MQTT is "
                              "written only through an enabled command", device_id, rname or '?')
            data_type = (rule.data_type or 'uint16').lower()
            k = _raw_channel_conflict(dev_cfg, 'holding', int(register.address), _words_of(data_type))
            if getattr(rule, 'scale_from', None) or k is not None:
                why = (_conflict_text(int(register.address), rname, k) if k is not None
                       else f"register {addr} ({rname}) scales by a live factor — written through its command")
                return refuse(why, "MQTT write REJECTED (register is written through the command %s): "
                                   "device=%s addr=%s", (k or {}).get('command') or "<scale_from>", device_id, addr)
            enum_map = getattr(register, 'enum', None)
            if enum_map:                                   # select: label → code
                value = next((int(k_) for k_, v in enum_map.items() if str(v) == payload), None)
                if value is None:
                    return refuse(f"unknown option {payload!r}", "MQTT write REJECTED (unknown option %r): "
                                  "device=%s addr=%s", payload, device_id, addr)
            else:                                          # number: parse
                try:
                    value = float(payload)
                except (TypeError, ValueError):
                    return refuse(f"not a number: {payload!r}", "MQTT write REJECTED (non-numeric %r): "
                                  "device=%s addr=%s", payload, device_id, addr)
            # bounds apply to BOTH a typed number and a mapped enum code
            if not _math.isfinite(value):
                return refuse("not a finite number", "MQTT write REJECTED (not finite): device=%s addr=%s",
                              device_id, addr)
            if rule.write_min is not None and value < rule.write_min:
                return refuse(f"{value:g} is below the register minimum {rule.write_min}",
                              "MQTT write REJECTED (%s < min %s): device=%s addr=%s",
                              value, rule.write_min, device_id, addr)
            if rule.write_max is not None and value > rule.write_max:
                return refuse(f"{value:g} is above the register maximum {rule.write_max}",
                              "MQTT write REJECTED (%s > max %s): device=%s addr=%s",
                              value, rule.write_max, device_id, addr)
            # the exact-allowlist guard the HTTP route enforces — a [0, 1] select
            # must not accept 2 just because it arrived over the broker
            if (getattr(rule, 'write_allowed', None)
                    and value not in [float(v) for v in rule.write_allowed]):
                return refuse(f"{value:g} is not in the register's allowed set {rule.write_allowed}",
                              "MQTT write REJECTED (%s not in allowed set %s): device=%s addr=%s",
                              value, rule.write_allowed, device_id, addr)
            scale = float(rule.scale if rule.scale is not None else 1.0)
            offset = float(getattr(rule, 'offset', 0.0) or 0.0)
            # what the encoder would have to clamp is refused here (F-15, 3.83.0,
            # same as HTTP): a clamped word is a value nobody asked for
            from multibus.codec.encoder import RegisterEncoder as _Enc
            _rng = _Enc.int_range(data_type)
            if _rng is not None:
                try:
                    _raw = int(round((float(value) - offset) * scale))
                except (TypeError, ValueError):
                    _raw = None
                if _raw is not None and not (_rng[0] <= _raw <= _rng[1]):
                    return refuse(f"{value:g} encodes to {_raw}, outside {data_type} [{_rng[0]}, {_rng[1]}]",
                                  "MQTT write REJECTED (%s encodes to %s, outside %s [%s, %s]): "
                                  "device=%s addr=%s", value, _raw, data_type, _rng[0], _rng[1], device_id, addr)
            ctx = {'dev': dev_cfg, 'rtype': 'holding', 'address': int(register.address), 'register': rname,
                   'data_type': data_type, 'scale': scale, 'offset': offset, 'value': value, 'via': 'ha',
                   'who': 'ha-mqtt', 'ip': 'mqtt', 'rate_key': 'mqtt:' + device_id}
            def told(code, res, _d=dev_cfg):
                # what the lane said without running it (superseded, expired,
                # cancelled) carries the write's own fields too
                res = {'command': f"raw:holding@{int(register.address)}", 'register': rname or None,
                       'params': {'value': value}, 'device': device_id, 'via': 'ha', 'by': 'ha-mqtt',
                       'ts': round(time.time(), 3), **res}
                if res.get('status') not in ('success', 'mismatch', 'unverified', 'error'):
                    # what did not reach the wire (rate limit, superseded, expired,
                    # standby, refused at the head) is said; a written one logged itself
                    logger.warning("MQTT write %s: device=%s addr=%s — %s", str(res.get('status')).upper(),
                                   device_id, addr, res.get('reason') or '')
                _publish_raw_result(_d, res)
            # in the lane: the rate token is spent there, never by a value a newer
            # one replaced (ADR-0018 D7); every outcome goes to cmd/result
            _submit_raw(ctx, client, on_done=told)

        # --- public surface -------------------------------------------------
        self.push_readback_to_store = _push_readback_to_store
        self.arm_revert_reread = _arm_revert_reread
        self.mqtt_write_command = _mqtt_write_command
