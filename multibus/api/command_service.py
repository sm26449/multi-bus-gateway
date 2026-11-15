"""CommandService — the device-command seam REST / MQTT / OPC UA all call.

The command catalog (build/list/find), the readback-refresh scheduler, command
admission -> lane -> execute -> record, the named-command run/await/publish
path, the binding CRUD (enable/bind/channel), lease-pace warnings, and the MQTT
command handler + topic sync move VERBATIM out of create_api (P0.8b Lot F) into
this class via the closure-holder pattern: every captured create_api
collaborator becomes a constructor parameter, the function bodies are
byte-identical, and each is exposed as self.<name> so the @app.* routes and the
three callers (REST routes, the MQTT handler, OPC UA) still reach the same
callables through thin create_api aliases.

Stays in the api layer (like Lot B/E): _cmd_gate, _own_device_for_binding,
_store_device_raw, _save_binding raise HTTPException, which a lower layer may
not; a move to control/ waits on the deferred exception-seam step. logger is
injected (kept named multibus.api). The readback-refresh state (_cmd_refresh,
_cmd_refresh_lock, _cmd_refresh_gen) lives inside the instance, a single shared
copy as before. The meters sub-cluster (_device_reads/_save_meters) is left in
create_api for the Lot G routes pass.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request

from multibus.control.commands import (CommandDef, assign_channels, normalize_params,
                                        parse_command_def, parse_payload)
from multibus.control.command_dispatch import Job as _CmdJob
from multibus.control.command_transport import (CommandTransports,
                                                modbus_driver_of as _modbus_driver_of,
                                                mqtt_driver_of as _mqtt_driver_of)
from multibus.control.lanes import BusBusy as _BusBusy
from multibus.core.config import NETWORK_PROTOCOLS, SERIAL_PROTOCOLS
from multibus.devices.validation import (CHANNEL_OVERRIDE_KEYS,
                                          channel_override_errors as _channel_override_errors,
                                          override_cid as _override_cid)
from multibus.api.validation import validate_commands

# verbatim from create_api: every Modbus way but listening (mirrors
# control/raw_writes.py and api/endpoints_service.py).
_POLLED_MODBUS = NETWORK_PROTOCOLS + SERIAL_PROTOCOLS


class CommandService:
    """Device-command catalog, admission, dispatch, record and MQTT seam (ADR-0038 #2)."""

    def __init__(self, *, config, registry, template_registry, logger, app, REVERT_REREAD_AT_S, _api_key, _arm_revert_reread, _budget_deadline, _bus_lock, _cmd_dispatch, _cmd_lock_for, _cmds_gen, _device_runtime, _device_write, _find_device, _lane_key, _lease_mgr, _make_command_lease_revert, _make_lease_revert, _mqtt_write_command, _pub, _push_readback_to_store, _sync_device_discovery, audit_log, auth_state, calc_engine, gw_role, _cmd_last, _device_mutation_lock, _write_rate_ok) -> None:
        # _modbus_driver_of / _mqtt_driver_of are the ONE shared bodies imported
        # from control/command_transport.py (M1); exposed as self._… below for
        # lanes/raw_writes/rules_seq/command_exec/routes.components.

        def _gateway_command_topics(dev_cfg) -> List[str]:
            """Topics the gateway itself takes commands on: a device must never
            be commanded by publishing there (the gateway would take it as a new
            command). The device's own tree covers its /cmd/ and HA /set topics."""
            topics = [f"{dev_cfg.mqtt_topic_prefix}/#"] if getattr(dev_cfg, 'mqtt_topic_prefix', '') else []
            if _pub.mqtt is not None:
                topics.extend(list(getattr(_pub.mqtt, '_topic_handlers', {}) or {}))
            return topics

        # the one seam the Modbus/MQTT command branches collapse into (ADR-0036,
        # §2 L0): lane/exec-timeout routed through it from step 2, the rest from
        # steps 3-5. lane_key/lock_for delegate to the shared lanes helpers, so
        # the one `_cmd_locks` table stays shared (ADR-0036 point 5).
        _transports = CommandTransports(
            registry=registry, forbidden_topics=_gateway_command_topics,
            push_readback_to_store=_push_readback_to_store, arm_revert_reread=_arm_revert_reread,
            revert_reread_at_s=REVERT_REREAD_AT_S, lane_key=_lane_key, lock_for=_cmd_lock_for)


        def _modbus_template_of(dev_cfg):
            """The template whose registers a command writes: the Modbus source's,
            else the MQTT source's, else the device's own."""
            for proto in (_POLLED_MODBUS, ('mqtt',)):
                for s in (dev_cfg.sources or []):
                    if str(s.protocol or 'tcp').lower() in proto and s.template:
                        return template_registry.get(s.template)
            return template_registry.get(dev_cfg.template) if dev_cfg.template else None

        # per-device channel settings a device may override (ADR-0019); `combine`
        # stays the template's — what the setting means is not a device's choice
        from multibus.control.command_dispatch import DEFAULT_MAX_AGE_S
        _cmds_memo: Dict[str, Tuple] = {}

        def _commands_changed() -> None:
            """A binding, a channel override or a template changed: the memoized
            command lists and the rules' channel cache are rebuilt (review 11)."""
            _cmds_gen[0] += 1
            _cmds_memo.clear()
            rt = getattr(app.state, 'rules_runtime', None)
            if rt is not None:
                rt.__dict__.setdefault('_chan_cache', {}).clear()

        _chan_conflicts_warned: set = set()

        def _channel_overrides(dev_cfg, defs: Dict[str, Any], ids: Dict[str, str]):
            """The device's overrides by channel id, the key each was taken from,
            the keys that no longer name a channel (kept, listed as orphan) and
            the keys that name a channel another key already overrides. A key is
            a declared channel name or a member command's name — never a derived
            `a+b` id. Two keys for one channel (`motor` and `set_frequency`):
            the declared name wins whatever the order in the file, the other is
            a conflict, listed and warned (CR2-14)."""
            declared = {d.channel for d in defs.values() if d.channel}
            by_cid: Dict[str, Dict] = {}
            key_of: Dict[str, str] = {}
            orphans: List[str] = []
            conflicts: List[str] = []
            stored = getattr(dev_cfg, 'channels', None) or {}
            # declared names first, then member names, each sorted: deterministic
            for key in sorted(stored, key=lambda k: (k not in declared, k)):
                ov = stored[key]
                cid = _override_cid(key, defs, ids)
                if cid is None or not isinstance(ov, dict):
                    orphans.append(key)
                    continue
                if cid in key_of:
                    conflicts.append(key)
                    if (dev_cfg.id, key) not in _chan_conflicts_warned:
                        _chan_conflicts_warned.add((dev_cfg.id, key))
                        logger.warning("device %s: channel override '%s' names channel '%s', already set "
                                       "under '%s' — ignored", dev_cfg.id, key, cid, key_of[cid])
                    continue
                key_of[cid] = key
                by_cid[cid] = {k: v for k, v in ov.items() if k in CHANNEL_OVERRIDE_KEYS}
            return by_cid, key_of, sorted(orphans), sorted(conflicts)

        def _commands_for(dev_cfg) -> List[Dict]:
            """What this device offers: the template's presets, each enabled only by
            a binding on the device (or its group), plus inline recipes declared
            on the device. Order: as declared in the template, then inline ones.
            Memoized per device id: it is asked several times per command (review
            13); another template, binding or override rebuilds it — keyed by
            what it is built from, not by the object, so the registry's and the
            config's copies of one device share an entry (CR2-9)."""
            tpl = _modbus_template_of(dev_cfg)
            sig = (repr(dev_cfg.commands), repr(getattr(dev_cfg, 'channels', None)))
            hit = _cmds_memo.get(dev_cfg.id)
            if hit is not None and hit[1] is tpl and hit[2] == sig and hit[3] == _cmds_gen[0]:
                return list(hit[4])
            out = _build_commands(dev_cfg, tpl)
            if hit is not None:
                # the device, its template, a binding or an override changed (an
                # upsert, a channel PUT): the rules' channel view goes with it,
                # not at their next tick (review 11)
                rt = getattr(app.state, 'rules_runtime', None)
                if rt is not None:
                    rt.__dict__.setdefault('_chan_cache', {}).clear()
            _cmds_memo[dev_cfg.id] = (dev_cfg, tpl, sig, _cmds_gen[0], out)
            return list(out)

        def _build_commands(dev_cfg, tpl) -> List[Dict]:
            presets = dict((tpl.commands or {}) if tpl else {})
            tpl_channels = dict(getattr(tpl, 'channels', None) or {}) if tpl else {}
            bindings = {str(b.get('name', '')): b for b in (dev_cfg.commands or []) if isinstance(b, dict)}
            out = []
            for name, raw in presets.items():
                b = bindings.get(name) or {}
                tpl_safe = dict(raw.get('safe') or {}) or None
                safe_from = 'template' if tpl_safe else None
                if isinstance(b.get('safe'), dict) and b['safe']:
                    # the release value belongs to the site (a grid contract, a
                    # cable): the device binding sets it OVER the template's, key
                    # by key — a binding's {value: 50} keeps the template's
                    # revert_s & co. instead of dropping them to defaults (CR2-13)
                    raw = {**raw, 'safe': {**(tpl_safe or {}), **dict(b['safe'])}}
                    safe_from = 'binding+template' if set(tpl_safe or {}) - set(b['safe']) else 'device'
                out.append({'def': parse_command_def(name, raw), 'from_template': True, 'template_safe': tpl_safe,
                            'binding_safe': dict(b['safe']) if isinstance(b.get('safe'), dict) else None,
                            'enabled': bool(b) and bool(b.get('enabled', True)),
                            'faces': dict(b.get('faces') or {'api': True, 'mqtt': True, 'ha': True}),
                            'confirm': bool(b.get('confirm', raw.get('confirm', True))),
                            'lease_s': int(b.get('lease_s', 0) or 0), 'safe_from': safe_from})
            for name, b in bindings.items():
                if name in presets or not b.get('writes'):
                    continue
                out.append({'def': parse_command_def(name, b), 'from_template': False,
                            'enabled': bool(b.get('enabled', True)),
                            'faces': dict(b.get('faces') or {'api': True, 'mqtt': True, 'ha': True}),
                            'confirm': bool(b.get('confirm', True)), 'lease_s': int(b.get('lease_s', 0) or 0),
                            'safe_from': 'device' if b.get('safe') else None})
            defs = {c['def'].name: c['def'] for c in out}
            ids = assign_channels(defs) if defs else {}
            overrides, key_of, orphans, conflicts = _channel_overrides(dev_cfg, defs, ids)
            # template + this device's overrides: the arbiter (rules and every
            # outside writer) reads `channels`, the listing shows both
            channels = {cid: dict(cfg) for cid, cfg in tpl_channels.items()}
            for cid, ov in overrides.items():
                channels[cid] = {**channels.get(cid, {}), **ov}
            # the key an editor saves a channel's override under: the one that
            # won for it (CR2-14), else the declared name, else a member command's
            # name (never a derived `a+b` id, review 11)
            stored_key = key_of
            for c in out:
                cid = ids.get(c['def'].name, c['def'].name)
                members = sorted(n for n, i in ids.items() if i == cid and not defs[n].alias)
                c.update({'channels': channels, 'template_channels': tpl_channels,
                          'channel': cid, 'orphan_channels': orphans, 'channel_conflicts': conflicts,
                          'channel_key': stored_key.get(cid) or (cid if '+' not in cid else (members or [cid])[0]),
                          'channel_override': dict(overrides.get(cid) or {})})
            return out

        def _group_cmd_prefix(pid: str, gid: str, devs) -> str:
            """Where a group is told something: the units' common parent topic
            (`pv/inverters` for `pv/inverters/1`), else a path of the gateway's."""
            prefixes = {d.mqtt_topic_prefix.rsplit('/', 1)[0] for d in devs}
            # only when the units sit at `<parent>/<unit id>` is the parent theirs
            # alone; `pv/${device_id}` shares its parent with every other group
            if len(prefixes) == 1 and all(d.mqtt_topic_prefix == f"{next(iter(prefixes))}/{d.connection.unit_id}" for d in devs):
                return prefixes.pop()
            return f"mbg/endpoints/{pid}/{gid}"

        def _group_commands(pid: str, gid: str) -> List[Dict]:
            first = next((d for d in config.endpoint_devices(pid)
                          if (getattr(d, 'group_id', '') or 'units') == gid), None)
            if first is None:
                return []
            bindings = {str(b.get('name', '')): b for b in (first.commands or []) if isinstance(b, dict)}
            out = []
            for c in _commands_for(first):
                d = c['def'].to_dict()
                out.append({'name': d['name'], 'label': d['label'], 'params': d['params'],
                            'alias': d.get('alias'), 'enabled': c['enabled'], 'confirm': c['confirm'],
                            'from_template': c['from_template'], 'binding': bindings.get(d['name']),
                            # the group editor edits the same binding and channel
                            # fields as a device's Commands tab (ADR-0019)
                            'safe': d.get('safe') or None, **_binding_view(c)})
            return out

        def _binding_view(c: Dict) -> Dict:
            """A command's binding and its channel's pace, as an editor shows
            them: what runs (effective), the template's beside it, and what the
            device (or its group) says of its own (ADR-0019)."""
            cid = c.get('channel') or c['def'].name
            eff = (c.get('channels') or {}).get(cid) or {}
            tch = (c.get('template_channels') or {}).get(cid) or {}
            bsafe = c.get('binding_safe') or {}
            return {'lease_s': c['lease_s'], 'safe_from': c.get('safe_from'),
                    # where each key of the effective safe comes from (CR2-13)
                    'safe_from_keys': {k: ('device' if k in bsafe else 'template') for k in (c['def'].safe or {})},
                    'template_safe': c.get('template_safe'),
                    # the channel's pace as this device runs it, and the
                    # template's beside it
                    'channel': cid, 'channel_key': c.get('channel_key') or cid,
                    'min_interval_s': eff.get('min_interval_s') or 0,
                    'template_min_interval_s': tch.get('min_interval_s') or 0,
                    'max_age_s': eff.get('max_age_s') or DEFAULT_MAX_AGE_S,
                    'channel_settings': {k: eff[k] for k in CHANNEL_OVERRIDE_KEYS if k in eff},
                    'channel_override': dict(c.get('channel_override') or {}),
                    'template_channel': dict(tch)}

        def _command_entry(dev_cfg, c: Dict) -> Dict:
            d = c['def'].to_dict()
            d.update({'enabled': c['enabled'], 'from_template': c['from_template'], 'faces': c['faces'],
                      'confirm': c['confirm'], **_binding_view(c),
                      'counters': _cmd_dispatch.counters(dev_cfg.id, c['def'].name),
                      'mqtt_topic': f"{dev_cfg.mqtt_topic_prefix}/cmd/{c['def'].name}",
                      'last': _cmd_last.get(f"{dev_cfg.id}:{c['def'].name}"),
                      'refreshing': f"{dev_cfg.id}:{c['def'].name}" in _cmd_refresh})
            return d

        def _find_command(dev_cfg, name: str):
            return next((c for c in _commands_for(dev_cfg) if c['def'].name == name), None)

        # commands whose setting the device forgets by itself (refresh_s): the
        # gateway writes the same parameters again until the safe ones are sent
        _cmd_refresh: Dict[str, threading.Timer] = {}
        _cmd_refresh_lock = threading.Lock()
        # every command run on a device's command (other than a refresh) moves its
        # generation on, under the command lock: a refresh armed before it — even
        # one already waiting for that lock — then finds itself superseded and
        # writes nothing (it must never put back what a newer command replaced)
        _cmd_refresh_gen: Dict[str, int] = {}

        def _cancel_refresh(dev_id: str, name: str) -> None:
            with _cmd_refresh_lock:
                t = _cmd_refresh.pop(f"{dev_id}:{name}", None)
            if t is not None:
                t.cancel()

        def _cancel_device_refreshes(dev_id: str) -> None:
            """A deleted or stopped device keeps no refresh: re-enabled days
            later, it must not be told an old setting again. Nor what waits for
            its bus (ADR-0018 D12)."""
            _cmd_dispatch.cancel(device=dev_id, reason=f"device '{dev_id}' was deleted or stopped — nothing written")
            _cmds_memo.pop(dev_id, None)                 # rebuilt if it comes back (CR2-9)
            _cmd_dispatch.forget(dev_id)
            with _cmd_refresh_lock:
                keys = [k for k in _cmd_refresh if k.split(':', 1)[0] == dev_id]
                timers = [_cmd_refresh.pop(k) for k in keys]
                for k in keys:
                    _cmd_refresh_gen[k] = _cmd_refresh_gen.get(k, 0) + 1
            for t in timers:
                t.cancel()

        def _arm_refresh(dev_id: str, name: str, params: Dict, every: float) -> None:
            key = f"{dev_id}:{name}"
            with _cmd_refresh_lock:
                gen = _cmd_refresh_gen.get(key, 0)

            def _drop():
                with _cmd_refresh_lock:
                    if _cmd_refresh.get(key) is timer:
                        _cmd_refresh.pop(key, None)

            def fire():
                with _cmd_refresh_lock:
                    if _cmd_refresh.get(key) is not timer:
                        return                             # replaced or cancelled meanwhile
                _i, dcfg, cl = _find_device(dev_id)
                if dcfg is None or not dcfg.enabled:
                    _drop()                                # deleted or stopped: the refresh ends here
                    return
                code, res = _run_named_command(dcfg, cl, name, dict(params), who='refresh', ip='-',
                                               via='refresh', refresh_gen=gen)
                if res.get('superseded'):
                    _drop()                                # a newer command owns the setting now
                elif code in (409, 429, 502):
                    # the device or the bus is away for now: keep the setting alive
                    logger.warning("command refresh %s %s: %s — trying again", dev_id, name, res.get('reason') or res.get('status'))
                    _arm_refresh(dev_id, name, params, every)
                elif code >= 400:
                    _drop()
                    logger.warning("command refresh %s %s stopped: %s", dev_id, name, res.get('reason') or res.get('status'))
            timer = threading.Timer(every, fire)
            timer.daemon = True
            with _cmd_refresh_lock:
                old = _cmd_refresh.pop(key, None)
                _cmd_refresh[key] = timer
            if old is not None:
                old.cancel()
            timer.start()

        # ── the run: admission (cheap, final refusals) → the lane → the record ──
        # (ADR-0018). Admission answers at once; what spends something — the rule
        # pause, the write-rate token, the safety holder — happens in the lane,
        # under the bus lock, when the command is about to write (D7).
        _CMD_ID_MAX = 64

        def _cmd_kind(via: str) -> str:
            return ('revert' if via == 'lease-revert' else 'refresh' if via == 'refresh'
                    else 'sequence' if via.startswith('sequence:') else 'command')

        def _cmd_id_error(cmd_id) -> Optional[str]:
            if cmd_id is None or (isinstance(cmd_id, str) and 0 < len(cmd_id) <= _CMD_ID_MAX):
                return None
            return f"id: a text of 1..{_CMD_ID_MAX} characters — it is echoed in the result"

        def _admit_command(dev_cfg, client, name: str, params: Dict, *, who, ip, via, dry_run,
                           lease_s, refresh_gen, still_wanted, is_current, cmd_id, recheck=None):
            """Every refusal that costs nothing → (http_status, result); else the
            context the lane runs."""
            why = _cmd_id_error(cmd_id)
            if why:
                return 422, {'status': 'rejected', 'reason': why, 'fields': {'id': f'1..{_CMD_ID_MAX} characters'}}
            # the dead-man's time is checked BEFORE anything is written: a bad
            # lease_s once failed after the write, leaving the setting on the
            # device with no lease to take it back (a 500)
            if lease_s is not None and not (isinstance(lease_s, int) and not isinstance(lease_s, bool)
                                            and 0 <= lease_s <= 86400):
                return 422, {'status': 'rejected', 'reason': 'lease_s: a whole number of seconds, 0..86400',
                             'fields': {'lease_s': '0..86400 seconds'}}
            c = _find_command(dev_cfg, name)
            if c is None:
                return 404, {'status': 'rejected', 'reason': f"device '{dev_cfg.id}' offers no command '{name}'"}
            # an alias is another command with its parameters filled in: resolved
            # here, so the slot is the channel of what is really written (review 8)
            for _hop in range(5):
                if not c['def'].alias:
                    break
                target = c['def'].alias.get('command')
                merged = dict(c['def'].alias.get('params') or {})
                merged.update({k: v for k, v in (params or {}).items() if k == 'source'})
                params, name = merged, target
                c = _find_command(dev_cfg, target)
                if c is None:
                    return 404, {'status': 'rejected', 'reason': f"device '{dev_cfg.id}' offers no command '{target}'"}
            else:
                return 422, {'status': 'rejected', 'reason': f"command '{name}': aliases loop"}
            cmd: CommandDef = c['def']
            # a dry run only reads: it previews a command before enabling it. A
            # lease revert writes the template's own safe recipe: turning the
            # command off while its lease runs must not strand the device at the
            # commanded value (API-A5)
            if not c['enabled'] and not dry_run and via != 'lease-revert':
                return 403, {'status': 'rejected', 'reason': f"command '{name}' is not enabled on '{dev_cfg.id}'"}
            face = via.split('-')[0]                   # api | mqtt | ha; the lease is the gateway's own
            # a sequence writes from outside the rules too: it pauses the channel's
            # lower rules for its run (override_s) and a safety rule stops it
            outside = face in ('api', 'mqtt', 'ha', 'opcua') or via.startswith('sequence:')
            if face in ('api', 'mqtt', 'ha', 'opcua') and not c['faces'].get(face, True):
                return 403, {'status': 'rejected', 'reason': f"command '{name}' does not accept the {face} face on '{dev_cfg.id}'"}
            rt = getattr(app.state, 'rules_runtime', None)
            _pause = None
            if rt is not None and outside and not dry_run:
                owner = rt.owner_of(dev_cfg.id, cmd.name)
                holder = rt.safety_holder(dev_cfg.id, cmd.name) if owner is not None else None
                if holder is not None:
                    # a protection rule acting now is never paused from outside
                    # (phase 3: safety sits above manual)
                    return 409, {'status': 'rejected', 'reason':
                                 f"safety rule '{holder}' holds this channel now — it cannot be paused from outside"}
                if owner is not None:
                    from multibus.control.rules_runtime import MAX_OVERRIDE_S
                    try:
                        secs = float((params or {}).get('override_s') or 0)
                    except (TypeError, ValueError):
                        secs = 0.0
                    if not (0 < secs <= MAX_OVERRIDE_S):      # NaN fails both sides
                        return 409, {'status': 'rejected', 'reason':
                                     f"owned by rule '{owner}' — pass override_s (1..{int(MAX_OVERRIDE_S)} s) to pause it"}
                    # the rule is paused only once the command is about to write,
                    # in the lane — a rejected, superseded or expired command must
                    # not silence a rule (F-09, 3.83.0; ADR-0018 D7)
                    _pause = (owner, secs)
            params = {k: v for k, v in (params or {}).items() if k != 'override_s'}
            # a dead-man revert restores the SAFE value the template declares; it
            # runs even if writes were disabled or the device locked after the
            # limit was placed — otherwise the limit would stay forever (F-11,
            # 3.83.0). Every other face meets the gates.
            _is_revert = via == 'lease-revert'
            # a standby gateway writes nothing — not even a revert: the device
            # belongs to the active one (503, so a refresh timer stops quietly)
            if not gw_role.active and not dry_run:
                return 503, {'status': 'rejected', 'reason': gw_role.status_text()}
            if not config.security.allow_writes and not dry_run and not _is_revert:
                return 403, {'status': 'rejected', 'reason': 'Writes are disabled — set security.allow_writes=true to enable'}
            if dev_cfg.write_locked and not dry_run and not _is_revert:
                return 403, {'status': 'rejected', 'reason': f"device '{dev_cfg.id}' is write-locked"}
            try:
                norm = normalize_params(cmd, params or {})
            except ValueError as e:
                return 422, {'status': 'rejected', 'reason': str(e)}
            # the transport seam (ADR-0036 §2 L0 step 5): one resolution replaces
            # the drv/mq dichotomy; None keeps the exact 409 message for the
            # Modbus/MQTT 'no source' case
            transport = _transports.transport_of(client)
            if transport is None:
                return 409, {'status': 'rejected', 'reason': 'no Modbus or MQTT source is running for this unit — nothing can write'}
            tpl = _modbus_template_of(dev_cfg)
            cid = c.get('channel') or cmd.name
            ctx = {'dev': dev_cfg, 'client': client, 'c': c, 'cmd': cmd, 'norm': norm, 'via': via, 'who': who,
                   'ip': ip, 'dry_run': dry_run, 'lease_s': lease_s, 'refresh_gen': refresh_gen,
                   'still_wanted': still_wanted, 'is_current': is_current, 'id': cmd_id, 'rt': rt,
                   'recheck': recheck,
                   # a revert run by a clean shutdown waits for the bus only within
                   # the shutdown's budget (ADR-0022 D8, review HIGH 1): its deadline
                   # travels with the job — the budget is the stopping thread's own
                   'deadline': _budget_deadline() if _is_revert else None,
                   'pause': _pause, 'outside': outside, 'is_revert': _is_revert, 'transport': transport,
                   'regs': {r.name: r for r in (tpl.registers if tpl else [])}, 'channel': cid,
                   'max_age_s': float(((c.get('channels') or {}).get(cid) or {}).get('max_age_s')
                                      or DEFAULT_MAX_AGE_S)}
            # the held channel answers at once (D5) — when nothing waits ahead of
            # this command in its slot; it is asked again at the head of the lane
            if _hold_applies(ctx) and _cmd_dispatch.pending(ctx['transport'].lane_key(dev_cfg, client), dev_cfg.id, cid) == 0:
                held = _held(ctx)
                if held is not None:
                    return held
            return ctx

        def _hold_applies(ctx) -> bool:
            # the channel's protection (minimum run / stop, starts per hour, the
            # pace) holds whoever writes — a rule's own writes meet it in the
            # referee, the dead-man never waits
            return (ctx['rt'] is not None and not ctx['dry_run'] and not ctx['via'].startswith('rule:')
                    and not ctx['is_revert'])

        def _held(ctx, check_already: bool = True):
            """(409, held) when the channel's protection holds this command now —
            unless the device already holds what it says (a renewal that changes
            nothing is never held: CMD-Q-5)."""
            got = ctx['rt'].channel_hold_s(ctx['dev'], ctx['cmd'].name, ctx['norm'])
            if not got:
                return None
            if check_already and _already_set(ctx) is not None:
                return None
            _cmd_dispatch.count(ctx['dev'].id, ctx['cmd'].name, 'held')
            return 409, {'status': 'rejected', 'held': True, 'reason': f"channel {got[0]}",
                         'retry_after_s': round(max(0.1, got[1]), 1)}

        def _already_set(ctx) -> Optional[Dict]:
            """D6: the device holds what this command says — asked in the lane
            against the reading, after what waited in the slot was superseded
            (never at admission against the reading alone: review 1). An MQTT
            source alive with every check a written register (its own already-set
            test); a Modbus device only by the template's `skip_if_set`."""
            # the MQTT dry-run vs the Modbus `skip_if_set` test now lives on the
            # transport adapter (ADR-0036 §2 L0 step 3), byte-identical
            return ctx['transport'].already_set(ctx)

        def _exec_timeout(ctx) -> float:
            """How long a running command may take (ADR-0018 D4). Routed through
            the transport seam (ADR-0036 §2 L0 step 2): the Modbus (connection
            timeout × (retries+1) × transaction count + settle) vs MQTT
            (settle + 5) branch now lives on the adapter, byte-identical."""
            return ctx['transport'].exec_timeout(ctx)

        def _execute(ctx, job=None):
            """The lane's part, under the bus lock: the last checks (the lease
            still current, the role, the hold), what it spends (the rate token,
            the rule pause), the write, then the record."""
            dev_cfg, cmd, norm, via, who, rt = ctx['dev'], ctx['cmd'], ctx['norm'], ctx['via'], ctx['who'], ctx['rt']
            dry_run = ctx['dry_run']          # regs now read via the transport (A1/H5: drop the unused unpack)
            queued_ms = (round((job.started - job.admitted) * 1000.0, 1)
                         if job is not None and job.started is not None else None)
            _rkey = f"{dev_cfg.id}:{cmd.name}"
            already = None
            try:
                with _bus_lock(ctx['transport'].lock_for(ctx), ctx.get('deadline')):
                    if ctx['is_revert'] and ctx['is_current'] is not None and not ctx['is_current']():
                        # asked again at the head of the lane (review 2): renewed
                        # or cleared while the revert waited — nothing to undo
                        return 409, {'status': 'cancelled', 'lease_renewed': True,
                                     'reason': 'the lease was renewed or cleared while the revert waited — nothing written'}
                    still_wanted = ctx['still_wanted']
                    if still_wanted is not None and not dry_run and not still_wanted():
                        # queued behind the bus while its rule left armed: the
                        # rule no longer drives this target — nothing is written
                        res = {'status': 'cancelled', 'params': norm,
                               'reason': 'its rule left armed before the bus was free — nothing written',
                               'device': dev_cfg.id, 'via': via, 'by': who, 'ts': round(time.time(), 3)}
                        audit_log.append(user=who, ip=ctx['ip'], action="command", target=f"{dev_cfg.id} {cmd.name}",
                                         status='cancelled', detail={'params': norm, 'via': via, 'reason': res['reason']})
                        logger.info("COMMAND CANCELLED: device=%s %s %s via %s", dev_cfg.id, cmd.name, norm, via)
                        return 409, res
                    recheck = ctx.get('recheck')
                    if recheck is not None and not dry_run:
                        # a rule's job is asked again here, under the bus lock,
                        # on what it captured when it decided: paused, yielded or
                        # held since (CMD-Q-8, ADR-0021 D3) — and a standby takes
                        # it back, it is no failure (D6). Before the rate token
                        # and any write (OV-F4-R3)
                        got = recheck()
                        if got is None and not gw_role.active:
                            got = ('standby', gw_role.status_text())
                        if got is not None:
                            return _dropped(ctx, *got)
                    if not dry_run and not gw_role.active:
                        return 503, {'status': 'rejected', 'reason': gw_role.status_text()}
                    if not dry_run and not ctx['is_revert']:
                        # re-check the per-device write lock at the HEAD of the lane,
                        # as the raw head does (M5 / REFAC-SAFE-1): a command admitted
                        # then queued behind a long guard read must not write to a
                        # device that was locked while it waited. A lease-revert is
                        # exempt — it only writes the declared safe value (F-11).
                        _ci, _cur, _cc = _find_device(dev_cfg.id)
                        if _cur is None or _cc is None:
                            # removed or stopped while it waited: nothing to write to
                            # (the raw head answers the same, raw_writes._execute_raw)
                            return 409, {'status': 'error', 'reason':
                                         f"device {dev_cfg.id} is not running — nothing written"}
                        if _cc is not ctx['client']:
                            # the device was rebuilt while this job waited: its transport still
                            # holds the OLD client, which reconnects to its old wire on demand
                            # (R2D-AW-3). The registry's record now describes the NEW client, so
                            # nothing below would refuse it — refuse here; the caller re-issues.
                            return 409, {'status': 'error', 'reason':
                                         f"device {dev_cfg.id} was rebuilt while the command waited "
                                         "— nothing written, send it again"}
                        if _cur.write_locked:
                            return 403, {'status': 'rejected', 'reason':
                                         f"device '{dev_cfg.id}' was write-locked while the command "
                                         "waited — nothing written"}
                        # ADR-0042: a retarget (import/restore/edit moved the wire)
                        # does not rebuild a non-primary client, so the kept client
                        # would write the OLD physical device. Refuse until a restart
                        # rebuilds it. Lease-revert is exempt above (is_revert) — it
                        # must still reach the old wire it wrote.
                        _aw = getattr(app.state, 'applied_wire', None)
                        if _aw is not None:
                            from multibus.core.config.models import device_wire_identity
                            _applied = _aw.get(_cur.id)
                            if _applied is not None and device_wire_identity(_cur) != _applied:
                                return 403, {'status': 'rejected', 'reason':
                                             f"device '{_cur.id}' was retargeted — a restart is "
                                             "required before it can be written"}
                    # a rule's write behaves exactly as before 3.93 until the rules
                    # lot (CR2-6): no renewal shortcut, the rate token, the stamp
                    if not dry_run and not via.startswith('rule:'):
                        already = _already_set(ctx)
                    if not dry_run and already is None:
                        # the hold again, under the bus lock: two callers that both
                        # passed it at admission cannot both write (review 12)
                        if _hold_applies(ctx):
                            held = _held(ctx, check_already=False)
                            if held is not None:
                                return held
                        if not _write_rate_ok(f'{via}:{dev_cfg.id}'):
                            return 429, {'status': 'rejected', 'reason': 'write rate limit exceeded'}
                    if ctx['outside'] and rt is not None and not dry_run:
                        # asked always, not only when a rule owned the channel at
                        # admission: one armed (or reloaded) while this waited is
                        # a safety rule too (RD-3). Never owner_of here: it takes
                        # the rules lock (ADR-0021, lock order bus → rt._lock)
                        holder = rt.safety_holder(dev_cfg.id, cmd.name)
                        if holder is not None:
                            return 409, {'status': 'rejected', 'reason':
                                         f"safety rule '{holder}' holds this channel now — it cannot be paused from outside"}
                    if ctx['pause'] is not None and not dry_run:
                        rt.pause_channel(dev_cfg.id, cmd.name, ctx['pause'][1], who=who, via=via)
                    if not dry_run:
                        with _cmd_refresh_lock:
                            if via == 'refresh':
                                if _cmd_refresh_gen.get(_rkey, 0) != ctx['refresh_gen']:
                                    return 409, {'status': 'superseded', 'superseded': True,
                                                 'reason': 'a newer command replaced this setting — refresh ended'}
                            else:
                                _cmd_refresh_gen[_rkey] = _cmd_refresh_gen.get(_rkey, 0) + 1
                    if already is not None:
                        res = dict(already, status='success')
                    else:
                        # the Modbus run_command vs MQTT run_mqtt_command call now
                        # lives on the transport (ADR-0036 §2 L0 step 4); the
                        # `already` shortcut and the under-lock order stay here
                        res = ctx['transport'].run(ctx)
                    if not dry_run:
                        _stamp(ctx, res)
            except ValueError as e:                       # a bad definition — the operator's to fix
                return 422, {'status': 'rejected', 'reason': f"command definition: {e}"}
            except _BusBusy as e:
                return 409, {'status': 'cancelled', 'reason': str(e)}
            if queued_ms is not None:
                res['queued_ms'] = queued_ms
            return _record(ctx, res)

        def _stamp(ctx, res: Dict) -> None:
            """The channel's view of a command that ran — the last step under the
            bus lock (ADR-0021 D5): a writer waiting for the bus sees it, never a
            gap between the lock released and the stamp (CMD-Q-8, mirror)."""
            rt, via = ctx['rt'], ctx['via']
            ctx['stamped'] = True
            if res.get('status') not in ('success', 'mismatch', 'unverified') or rt is None:
                return
            try:
                if res.get('already_set') and not via.startswith('rule:'):
                    # nothing went out: the pace is not stamped, an unknown
                    # on/off is learnt without a start (CMD-Q-5, review 10)
                    rt.channel_reconcile(ctx['dev'], ctx['cmd'].name, ctx['norm'])
                else:
                    rt.channel_note(ctx['dev'], ctx['cmd'].name, ctx['norm'])   # the motor's view, by whoever
            except Exception:  # noqa: BLE001
                pass

        def _dropped(ctx, dropped: str, why: str):
            """A rule's job the head of the bus did not write (ADR-0021 D4):
            cancelled, said why, audited like the disarm cancel — no failure."""
            dev_cfg, cmd, norm, via, who = ctx['dev'], ctx['cmd'], ctx['norm'], ctx['via'], ctx['who']
            res = {'status': 'cancelled', 'dropped': dropped, 'params': norm, 'reason': why,
                   'device': dev_cfg.id, 'via': via, 'by': who, 'ts': round(time.time(), 3)}
            audit_log.append(user=who, ip=ctx['ip'], action="command", target=f"{dev_cfg.id} {cmd.name}",
                             status='cancelled', detail={'params': norm, 'via': via, 'dropped': dropped, 'reason': why})
            logger.info("COMMAND CANCELLED (%s): device=%s %s %s via %s — %s", dropped, dev_cfg.id, cmd.name, norm,
                        via, why)
            if dropped == 'held':
                _cmd_dispatch.count(dev_cfg.id, cmd.name, 'held')
            return 409, res

        def _record(ctx, res: Dict):
            """What a command that ran leaves behind: the log, the device's
            events, the audit, the last result, the channel's view, the read-back
            in the store, the result on MQTT, the dead-man, the refresh."""
            dev_cfg, cmd, norm, via, who = ctx['dev'], ctx['cmd'], ctx['norm'], ctx['via'], ctx['who']
            dry_run, regs, c, ip = ctx['dry_run'], ctx['regs'], ctx['c'], ctx['ip']
            lease_s = ctx['lease_s']
            if ctx.get('id') is not None:
                res['id'] = ctx['id']
            res.update({'device': dev_cfg.id, 'via': via, 'by': who, 'ts': round(time.time(), 3)})
            if dry_run:
                return 200, res
            ok = res['status'] == 'success'
            summary = (f"{cmd.name} {norm} · {res['status']}"
                       + (f" · {res['reason']}" if res.get('reason') else '')
                       + f" · via {via} by {who}")
            target = f"{dev_cfg.id} {cmd.name}"
            n_sent = int(res.get('frames_sent') or 0)
            # AUD-RET-1: the device already held the value and nothing went
            # out. An EMS re-sending its setpoint every few seconds made 62 %
            # of the audit and 40 % of the event ring out of these: one entry
            # when the streak starts, then a counted summary (hourly / when
            # the value changes); a real change or a failure is always its own
            noop = ok and bool(res.get('already_set')) and n_sent == 0
            detail = {'params': norm, 'via': via,
                      # `frames` is what the command WOULD write; for a no-op
                      # nothing went out, so it is empty — `frames_sent` says
                      'frames': [] if noop else res.get('frames'), 'frames_sent': n_sent,
                      'before': res.get('before'), 'after': res.get('after'),
                      'reason': res.get('reason'), 'ms': res.get('ms')}
            repeats = getattr(audit_log, 'repeats', None)
            if noop and repeats is not None:
                detail['already_set'] = True
                first = repeats.repeat((who, target), json.dumps(norm, sort_keys=True, default=str),
                                       user=who, ip=ip, action="command", target=target,
                                       status=res['status'], detail=detail)
            else:
                first = True
                if repeats is not None:      # the value moved: close its streaks first
                    repeats.end(lambda g, _t=target: g[1] == _t)
                audit_log.append(user=who, ip=ip, action="command", target=target,
                                 status=res['status'], detail=detail)
            # LOG-CMD-1: a success is INFO (a no-op repeat DEBUG), a refusal or
            # a failure stays WARNING — 2,570 WARNING lines in 21 h were successes
            level = logging.WARNING if not ok else logging.INFO if first else logging.DEBUG
            logger.log(level, "COMMAND %s: device=%s %s", res['status'].upper(), dev_cfg.id, summary)
            if first:                         # a no-op repeat does not evict real events
                try:
                    ctx['transport'].record_event(
                        'info' if ok else 'warn' if res['status'] in ('unverified', 'mismatch') else 'error',
                        'command', summary)
                except Exception:  # noqa: BLE001
                    pass
            _cmd_last[f"{dev_cfg.id}:{cmd.name}"] = {k: res.get(k) for k in ('status', 'params', 'before', 'after', 'reason', 'ms', 'via', 'by', 'ts')}
            if not ctx.get('stamped'):
                _stamp(ctx, res)                     # stamped under the bus lock already (D5)
            if res['status'] in ('success', 'mismatch'):
                # the read-back to the store + poll_now of the read-back group +
                # the device-side `revert_s` re-read now live on the transport
                # (ADR-0036 §2 L0 step 3): Modbus re-reads; MQTT reads back through
                # its own subscription, so its readback is a no-op (the old
                # `and mq is None` guard). Byte-identical.
                ctx['transport'].readback(ctx, res)
            # the dead-man: a controller that dies leaves the device where it put
            # it only until this lease expires — then the gateway runs `safe`
            # a refresh only repeats a setting: it must not renew the dead-man the
            # original command armed, or a dead controller's setting never ends
            # The lease is the OUTSIDE controller's dead-man (ADR-0019): a rule
            # never arms it — 90 s after an OV curtail it would put 100 % back
            # over the rule (RD-1) — and a rule's write that went out ends one an
            # outside controller left, whose revert would otherwise write over the
            # rule (RD-2). The rule's own release value is the rule's business.
            # A sequence is an outside writer: its lease behaves as before.
            is_rule = via.startswith('rule:')
            if is_rule and (ok or int(res.get('frames_sent') or 0) > 0):
                # the whole CHANNEL: an outside lease taken through another command
                # of it (keyed on that command's first register) ends too
                for oc in _commands_for(dev_cfg):
                    if oc.get('channel') != ctx['channel'] or not oc['def'].writes:
                        continue
                    first = regs.get(oc['def'].writes[0].get('register'))
                    if first is not None:
                        _lease_mgr.clear(dev_cfg.id, 'command', int(first.address))
            lease = 0 if (via == 'refresh' or is_rule) else int(lease_s if lease_s is not None else c['lease_s'] or 0)
            vp = cmd.value_param
            safe_keys = [vp.name] if (vp is not None and cmd.safe and vp.name in cmd.safe) else list((cmd.safe or {}).keys())
            is_safe = bool(cmd.safe) and all(norm.get(k) == cmd.safe[k] for k in safe_keys)
            # F-03 (3.83.0): frames that went out are on the device whatever the
            # read-back said — an unverified or mismatched command still needs
            # the dead-man, otherwise a dead controller leaves the limit in place
            sent = int(res.get('frames_sent') or 0) > 0
            if (ok or sent) and lease > 0 and cmd.safe and not is_safe:
                _revert = _make_command_lease_revert(dev_cfg.id, cmd.name)
                first = regs.get(cmd.writes[0]['register']) if cmd.writes else None
                addr = int(first.address) if first is not None else 0
                # the record is declarative for the listing and for boot recovery;
                # `command` is what recovery dispatches on — the raw fields are
                # informational and are never written as such
                # its own namespace ('command'): a raw leased write on the same
                # register must never replace the recipe revert (F-75, 3.83.0)
                _lease_mgr.arm(dev_cfg.id, 'command', addr, lease * 1000, _revert,
                               meta={'device': dev_cfg.id, 'register_type': 'command', 'address': addr,
                                     'data_type': 'command', 'scale': 1.0, 'offset': 0.0,
                                     'safe_value': cmd.safe.get('value'), 'lease_ms': lease * 1000,
                                     'command': cmd.name})
                res['lease_armed'] = True
            elif lease > 0 and not cmd.safe and (ok or sent):
                # a dead-man without a release value never fires: said, not hidden
                res.update(lease_armed=False, lease_reason='no safe value on the binding or the template — '
                                                          'the lease cannot release anything')
            elif ok and is_safe and cmd.writes:
                first = regs.get(cmd.writes[0]['register'])
                if first is not None:
                    _lease_mgr.clear(dev_cfg.id, 'command', int(first.address))
            if cmd.refresh_s:
                if is_safe and (ok or sent):
                    _cancel_refresh(dev_cfg.id, cmd.name)     # the safe value ends the refresh
                elif ok or sent:
                    _arm_refresh(dev_cfg.id, cmd.name, norm, float(cmd.refresh_s))
            _publish_command_result(dev_cfg, cmd.name, res)
            ctx['published'] = True
            code = 200 if res['status'] in ('success', 'mismatch', 'unverified') else 422 if res['status'] == 'rejected' else 502
            return code, res

        def _submit_named_command(dev_cfg, client, name: str, params: Dict, *, who='?', ip='?', via='api',
                                  dry_run=False, lease_s=None, refresh_gen=None, still_wanted=None,
                                  is_current=None, cmd_id=None, tell=False, recheck=None):
            """Admission, then the command in its lane. Returns (http_status,
            result) when it was refused or ran inline, else the queued Job.
            ``tell``: every outcome goes to cmd/result (a command that came over
            MQTT / HA: CMD-Q-4)."""
            adm = _admit_command(dev_cfg, client, name, params, who=who, ip=ip, via=via, dry_run=dry_run,
                                 lease_s=lease_s, refresh_gen=refresh_gen, still_wanted=still_wanted,
                                 is_current=is_current, cmd_id=cmd_id, recheck=recheck)
            if isinstance(adm, tuple):
                code, res = adm
                res.update({k: v for k, v in (('device', dev_cfg.id), ('via', via), ('by', who), ('id', cmd_id))
                            if v is not None and k not in res})
                # the parameters as asked: a client without ids still matches a
                # refusal to its request (CR2-12)
                res.setdefault('params', {k: v for k, v in (params or {}).items() if k != 'override_s'})
                return code, res
            if dry_run or via.startswith('rule:'):
                # a dry run only reads; a rule's write travels on its own
                # per-device writer with the referee (D13: moved in the rules lot)
                return _execute(adm)
            cmd = adm['cmd']

            def told(code, res, _ctx=adm):
                # what the lane said without running it (superseded, expired,
                # held at the head, cancelled): an executed one is already out
                if tell and not _ctx.get('published'):
                    _publish_command_result(dev_cfg, cmd.name,
                                            {'params': dict(_ctx['norm']), **res, 'device': dev_cfg.id, 'via': via,
                                             'by': who, 'ts': round(time.time(), 3)})
            job = _CmdJob(lane=adm['transport'].lane_key(dev_cfg, client), device=dev_cfg.id, channel=adm['channel'],
                          command=cmd.name, params=dict(adm['norm']), run=lambda j: _execute(j.ctx, j),
                          kind=_cmd_kind(via), coalesce=cmd.coalesce, max_age_s=adm['max_age_s'],
                          id=cmd_id, on_done=told, ctx=adm)
            return _cmd_dispatch.submit(job)

        def _await_command(job) -> Tuple[int, Dict]:
            """A queued command's verdict, waited for at most its max_age_s and
            its execution time (D4) — a shutdown revert at most its budget: past
            it, one not started is withdrawn and one running is said to run; the
            lease stays on disk for boot recovery (ADR-0022, review HIGH 1)."""
            wait = job.ctx['max_age_s'] + _exec_timeout(job.ctx) + 1.0
            deadline = job.ctx.get('deadline')
            if deadline is not None:
                wait = max(0.0, min(wait, deadline - time.monotonic()))
            code, res = _cmd_dispatch.wait_for(job, wait)
            if code == 504 and job.kind == 'revert' and deadline is None:
                # a revert that runs is in flight, not failed: its waiter (the lease
                # sweep's thread, which keeps the key in flight) waits for the end —
                # a 504 here used to read as a failure and fire it again (CR2-3)
                job.wait()
                code, res = job.result
            res.setdefault('device', job.device)
            return code, res

        def _run_named_command(dev_cfg, client, name: str, params: Dict, *, who='?', ip='?',
                               via='api', dry_run=False, lease_s=None, refresh_gen=None, still_wanted=None,
                               is_current=None, cmd_id=None, recheck=None):
            """Run one command on one unit with every gate, then record it —
            whichever face asked — and wait for it. Returns (http_status, result).
            `still_wanted` (a rule's write) is asked once the bus is ours: a rule
            that left armed meanwhile writes nothing (OV-F4); `is_current` (a
            lease revert) likewise. `recheck` (a rule's write, rules.recheck on)
            is asked there too: paused, yielded or held since it decided
            (ADR-0021)."""
            got = _submit_named_command(dev_cfg, client, name, params, who=who, ip=ip, via=via, dry_run=dry_run,
                                        lease_s=lease_s, refresh_gen=refresh_gen, still_wanted=still_wanted,
                                        is_current=is_current, cmd_id=cmd_id, recheck=recheck)
            return got if isinstance(got, tuple) else _await_command(got)

        _APPLIED = ('success', 'mismatch', 'unverified')

        def _publish_command_result(dev_cfg, name: str, res: Dict) -> None:
            # a standby hears the same broker: it never answers for the active
            # gateway (review 5)
            if not gw_role.active:
                return
            if not _pub.mqtt or not getattr(_pub.mqtt, 'connected', False):
                return
            try:
                body = json.dumps({'command': name, **res}, default=str)
                _pub.mqtt.client.publish(f"{dev_cfg.mqtt_topic_prefix}/cmd/result", body, qos=1, retain=False)
                # the last APPLIED command, retained: a controller that restarts
                # learns what the device was told without asking the wire — a
                # refusal or an error is not what the device holds (CMD-Q-9)
                if res.get('status') in _APPLIED:
                    _pub.mqtt.client.publish(f"{dev_cfg.mqtt_topic_prefix}/cmd/{name}/state",
                                                  json.dumps({k: res.get(k) for k in ('status', 'params', 'after', 'ts', 'via', 'by')}, default=str),
                                                  qos=1, retain=True)
            except Exception:  # noqa: BLE001
                pass


        def _cmd_gate(request: Request, dry_run=False):
            if not dry_run and not config.security.allow_writes:
                raise HTTPException(status_code=403, detail={"errors": [
                    "Writes are disabled — set security.allow_writes=true to enable"]})
            if not dry_run and not (auth_state.enabled or _api_key):
                raise HTTPException(status_code=403, detail={"errors": [
                    "writes require authentication — enable login (ui.auth) or set an API_KEY"]})
            who = getattr(request.state, "user", None) or ("api-key" if _api_key else "anon")
            return who, (request.client.host if request.client else "?")


        def _own_device_for_binding(device_id: str, what: str):
            """(dev_cfg, client) of a device of its own; an installation's unit
            takes its bindings and channels from its group (ADR-0019)."""
            _i, dev_cfg, client = _find_device(device_id)
            if dev_cfg is None:
                raise HTTPException(status_code=404, detail="device not found")
            if dev_cfg.endpoint_id:
                # an installation with groups keeps them per group; one without, on
                # itself (CR2-4: the pointer named a group that does not exist)
                grouped = bool((config.get_raw_endpoint(dev_cfg.endpoint_id) or {}).get('groups'))
                where = (f"in the installation's group '{getattr(dev_cfg, 'group_id', '') or 'units'}'" if grouped
                         else "on the installation itself")
                raise HTTPException(status_code=422, detail={"errors": [
                    f"'{device_id}' belongs to installation '{dev_cfg.endpoint_id}' — set its {what} {where}"]})
            return dev_cfg, client


        def _store_device_raw(device_id: str, client, mutate) -> Tuple[Dict, Dict]:
            """Change the stored device under the mutation lock — a write-lock or
            an edit landing between the read and the upsert would otherwise be
            lost. ``mutate(raw)`` edits the copy; returns (old raw, new raw)."""
            with _device_mutation_lock:
                raw = next((copy.deepcopy(d) for d in config._raw_devices if d.get('id') == device_id), None)
                if raw is None:
                    raise HTTPException(status_code=404, detail="device not found")
                old = copy.deepcopy(raw)
                mutate(raw)
                try:
                    new_cfg = config.upsert_raw_device(raw)
                except ValueError as e:
                    raise HTTPException(status_code=422, detail={"errors": [str(e)]})
                registry.replace(device_id, new_cfg, add_if_missing=False)   # config only: client kept
                _commands_changed()
            return old, raw


        _BINDING_KEYS = ('enabled', 'lease_s', 'safe', 'faces', 'confirm')


        def _binding_errors(c: Dict, merged: Dict, changed, tpl_safe) -> Dict[str, str]:
            """What a save CHANGES in a binding: the fields' shapes, `safe`
            through the command's own bounds (CMD-Q-11), and a dead-man that has
            something to release (CMD-Q-6). What is stored and not touched is
            not judged (ADR-0006): turning a command off is never refused (CR2-1)."""
            errs: List[str] = []
            validate_commands([{'name': merged['name'], **{k: merged[k] for k in changed if k in merged}}],
                              errs, safe_def=c['def'])
            fields = {e.split(':', 1)[0].replace('commands[0].', ''): e.split(':', 1)[1].strip() for e in errs}
            safe = merged.get('safe') if isinstance(merged.get('safe'), dict) else tpl_safe
            if not fields and ({'lease_s', 'safe'} & set(changed)) and int(merged.get('lease_s') or 0) > 0 \
                    and not safe:
                fields['lease_s'] = ('a lease without a safe value never fires — set safe '
                                     '(what the gateway writes when the lease runs out)')
            return fields


        def _save_binding(device_id: str, name: str, changes: Dict, request: Optional[Request]) -> Dict:
            dev_cfg, client = _own_device_for_binding(device_id, 'command bindings')
            c = next((x for x in _commands_for(dev_cfg) if x['def'].name == name and x['from_template']), None)
            if c is None:
                raise HTTPException(status_code=404, detail=f"the template offers no command '{name}'")
            said: Dict[str, Dict] = {}

            def mutate(raw):
                # read from what is stored, under the lock: the file is the truth
                prev = next((dict(b) for b in (raw.get('commands') or [])
                             if isinstance(b, dict) and b.get('name') == name), {})
                # off keeps the binding and what it says (safe values, faces,
                # lease): turning it back on finds them, not the template's. A
                # null removes a field (the template's again)
                merged = {**prev, 'name': name, 'enabled': bool(prev.get('enabled', True)) if prev else False}
                for k, v in changes.items():
                    if v is None:
                        merged.pop(k, None)
                    else:
                        merged[k] = v
                _tpl = _modbus_template_of(dev_cfg)
                fields = _binding_errors(c, merged, [k for k in changes if k != 'enabled'],
                                         dict(((_tpl.commands or {}).get(name) or {}).get('safe') or {}) if _tpl else {})
                if fields:
                    raise HTTPException(status_code=422, detail={"errors": [f"{k}: {v}" for k, v in fields.items()],
                                                                 "fields": fields})
                binds = [dict(b) for b in (raw.get('commands') or []) if isinstance(b, dict) and b.get('name') != name]
                binds.append(merged)
                raw['commands'] = binds
                said.update(prev=prev, merged=merged)
            _store_device_raw(device_id, client, mutate)
            prev, merged = said['prev'], said['merged']
            if not merged.get('enabled'):
                _cancel_refresh(device_id, name)            # a disabled command stops refreshing
            if request is not None:
                audit_log.append(user=getattr(request.state, "user", None) or ("api-key" if _api_key else "anon"),
                                 ip=request.client.host if request.client else "?", action="command binding",
                                 target=f"{device_id} {name}", status="ok",
                                 detail={'old': prev or None, 'new': merged})
            # rules bound to it re-bind, its MQTT command topic appears or goes
            _sync_device_discovery()
            return merged


        def _lease_pace_warnings(dev_cfg, channel: Optional[str] = None, command: Optional[str] = None) -> List[str]:
            """A binding's dead-man shorter than its channel's min_interval_s: a
            controller renewing at the lease's pace is held, and the lease may
            run out between renewals (ADR-0022, starvation said)."""
            out = []
            for c in _commands_for(dev_cfg):
                cid = c.get('channel') or c['def'].name
                if (channel is not None and cid != channel) or (command is not None and c['def'].name != command):
                    continue
                lease = int(c.get('lease_s') or 0)
                try:
                    mi = float((((c.get('channels') or {}).get(cid)) or {}).get('min_interval_s') or 0)
                except (TypeError, ValueError):
                    mi = 0.0
                if lease > 0 and mi > lease:
                    out.append(f"{c['def'].name}: lease_s {lease} is shorter than channel {cid}'s min_interval_s "
                               f"{mi:g} — a controller renewing at the lease's pace is held, and the dead-man may "
                               f"fire between renewals")
            return out


        def _mqtt_writes_refused() -> Optional[str]:
            """Why a write arriving over MQTT must be ignored, or None. Both gates
            must be on, AND the broker session must be authenticated: with
            allow_write_entities on, a broker publish IS a hardware write, so an
            anonymous broker would hand that to anyone on the LAN (3.80.0)."""
            if not gw_role.active:
                return gw_role.status_text()
            if not (config.security.allow_writes and config.mqtt.allow_write_entities):
                return "writes over MQTT are off (security.allow_writes + mqtt.allow_write_entities)"
            if not str(config.mqtt.username or "").strip():
                return ("mqtt.allow_write_entities is on but the broker connection is "
                        "anonymous — set mqtt.username/password (broker ACLs are the "
                        "only authentication a publish carries)")
            return None


        def _mqtt_command(device_ids, name: str, payload: str) -> None:
            """A controller's command over MQTT. The broker is not a trusted
            caller: the same gates as the API, plus mqtt.allow_write_entities.
            Decoded and handed to the device's lane — the MQTT worker never waits
            for a bus (CMD-Q-1) — and every outcome, a refusal included, is told
            on cmd/result with the request's `id` (CMD-Q-4)."""
            refused = _mqtt_writes_refused()
            if refused:
                logger.warning("MQTT command %s ignored: %s", name, refused)
            for did in device_ids:
                _i, d, c = _find_device(did)
                if d is None:
                    continue
                cm = _find_command(d, name)
                if cm is None:
                    continue
                try:
                    params = parse_payload(cm['def'], payload) if str(payload).strip() else {}
                except ValueError as e:
                    # nothing parsed: the payload as it came, for a client to match (CR2-12)
                    _publish_command_result(d, name, {'status': 'rejected', 'reason': str(e), 'device': d.id, 'via': 'mqtt',
                                                      'payload': str(payload)[:200]})
                    continue
                cmd_id = params.pop('id', None)
                if isinstance(cmd_id, (int, float)) and not isinstance(cmd_id, bool):
                    cmd_id = str(cmd_id)                  # a number is an id too
                who = str(params.pop('source', '') or 'mqtt')[:64]
                # a controller's envelope (the legacy collector's schema: command,
                # device_id, ts) says who and what — the parameters are the rest
                for k in ('command', 'device_id', 'ts'):
                    params.pop(k, None)
                why = refused or _cmd_id_error(cmd_id)
                if why:
                    _publish_command_result(d, name, {'status': 'rejected', 'reason': why, 'device': d.id, 'via': 'mqtt',
                                                      'by': who, 'params': dict(params),
                                                      **({} if _cmd_id_error(cmd_id) else {'id': cmd_id})})
                    continue
                got = _submit_named_command(d, c, name, params, who=who, ip='mqtt', via='mqtt', cmd_id=cmd_id, tell=True)
                if isinstance(got, tuple):
                    _publish_command_result(d, name, got[1])


        _device_cmd_topics: set = set()

        def _sync_command_topics() -> None:
            """Subscribe `<unit prefix>/cmd/<name>` for every enabled command with
            an MQTT face — endpoint units and devices of their own alike — and
            `<group prefix>/cmd/<name>` for an endpoint's group. Only the topics
            this function made are dropped on a rebuild: the rules' and the
            sequences' own command topics are not its to remove (they used to be,
            and a sequence's run topic stayed gone until a restart)."""
            if not _pub.mqtt or not hasattr(_pub.mqtt, 'register_command'):
                return
            for t in list(_device_cmd_topics):
                if hasattr(_pub.mqtt, 'unregister_command'):
                    _pub.mqtt.unregister_command(t)
                else:
                    _pub.mqtt.unregister_commands(t)
            _device_cmd_topics.clear()

            def reg(topic, fn):
                _pub.mqtt.register_command(topic, fn)
                _device_cmd_topics.add(topic)

            def unit_topics(dev):
                names = []
                for c in _commands_for(dev):
                    if not c['enabled'] or not c['faces'].get('mqtt', True):
                        continue
                    name = c['def'].name
                    reg(f"{dev.mqtt_topic_prefix}/cmd/{name}",
                        lambda payload, ids=(dev.id,), n=name: _mqtt_command(ids, n, payload))
                    names.append(name)
                return names

            in_endpoints = set()
            for p in config.endpoints:
                pid = p.get('id')
                by_group: Dict[str, Dict[str, list]] = {}
                for dev in config.endpoint_devices(pid):
                    in_endpoints.add(dev.id)
                    for name in unit_topics(dev):
                        by_group.setdefault(getattr(dev, 'group_id', '') or 'units', {}).setdefault(name, []).append(dev)
                for gid, cmds in by_group.items():
                    for name, devs in cmds.items():
                        gp = _group_cmd_prefix(pid, gid, devs)
                        reg(f"{gp}/cmd/{name}",
                            lambda payload, ids=tuple(d.id for d in devs), n=name: _mqtt_command(ids, n, payload))
            for dev in list(config.devices):
                if dev.id not in in_endpoints and not dev.primary and getattr(dev, 'mqtt_topic_prefix', ''):
                    unit_topics(dev)



        self._modbus_driver_of = _modbus_driver_of
        self._gateway_command_topics = _gateway_command_topics
        self._mqtt_driver_of = _mqtt_driver_of
        self._modbus_template_of = _modbus_template_of
        self._commands_changed = _commands_changed
        self._override_cid = _override_cid
        self._channel_overrides = _channel_overrides
        self._commands_for = _commands_for
        self._build_commands = _build_commands
        self._group_cmd_prefix = _group_cmd_prefix
        self._group_commands = _group_commands
        self._binding_view = _binding_view
        self._command_entry = _command_entry
        self._find_command = _find_command
        self._cancel_refresh = _cancel_refresh
        self._cancel_device_refreshes = _cancel_device_refreshes
        self._arm_refresh = _arm_refresh
        self._cmd_kind = _cmd_kind
        self._cmd_id_error = _cmd_id_error
        self._admit_command = _admit_command
        self._hold_applies = _hold_applies
        self._held = _held
        self._already_set = _already_set
        self._exec_timeout = _exec_timeout
        self._execute = _execute
        self._stamp = _stamp
        self._dropped = _dropped
        self._record = _record
        self._submit_named_command = _submit_named_command
        self._await_command = _await_command
        self._run_named_command = _run_named_command
        self._publish_command_result = _publish_command_result
        self._cmd_gate = _cmd_gate
        self._own_device_for_binding = _own_device_for_binding
        self._store_device_raw = _store_device_raw
        self._binding_errors = _binding_errors
        self._save_binding = _save_binding
        self._lease_pace_warnings = _lease_pace_warnings
        self._channel_override_errors = _channel_override_errors
        self._mqtt_writes_refused = _mqtt_writes_refused
        self._mqtt_command = _mqtt_command
        self._sync_command_topics = _sync_command_topics
