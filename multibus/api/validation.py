# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Device-payload validation, extracted whole from ``create_api`` (P0.8b Lot B).

``DeviceValidator`` holds the 16 validation closures that used to live nested in
``create_api``: the influx-block shape check, the bus-claim checks, the serial /
network / virtual-map conflict checks, the edit-lock check and the big
``validate_device_payload`` that ties them together. They are kept as closures
built in ``__init__`` so the bodies are **verbatim** — the collaborators they
captured from ``create_api`` (``config``, ``template_registry``, ``registry``
and three sibling closures) arrive as constructor arguments under the same
names, and the pure helpers/constants they used are module-level here.

This stays in the ``api`` layer for now (it still raises ``HTTPException`` and
relies on the ``_field_errors`` enrichment around its callers). ADR-0038's end
state moves it to ``devices/validation.py`` raising domain exceptions
(``api/errors.py``); that move is a separate, deliberately-gated step because it
is cross-cutting across every ``except HTTPException`` site that wraps a caller.

The public surface (what ``create_api`` and the routes call) is the nine methods
exposed at the end of ``__init__``; the rest are internal.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from multibus.devices.device_validation import (
    _is_serial_path, _serial_host_error, _vmap_mode, _vmap_mode_missing)
from multibus.core.config import (
    BUS_OVER_TCP_PROTOCOLS, CAN_PROTOCOLS, MODBUS_PROTOCOLS, NETWORK_PROTOCOLS,
    SERIAL_PROTOCOLS, VIRTUAL_MAP_PROTOCOLS,
    VIRTUAL_MAP_SERIAL, protocol_family)
from multibus.devices.connection_spec import METER_ONLY_TEXT
from multibus.devices.validation import (influx_block_errors, channel_override_errors,
                                          override_cid, CHANNEL_OVERRIDE_KEYS)
from multibus.control.commands import assign_channels, normalize_params, parse_command_def
from multibus.control.rules import PACE_KEYS
from multibus.payload import flag
from multibus.outputs.vbms_registry import vbms_id_error

logger = logging.getLogger(__name__)

# moved verbatim from create_api's module/body scope (used only by the checks
# below, plus a few sibling routes that import them back from api/__init__)
_DEVICE_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{1,63}$')
_PROTO_WORDS = {'tcp': 'Modbus TCP', 'udp': 'Modbus UDP', 'rtu-tcp': 'Modbus RTU over TCP',
                'ascii-tcp': 'Modbus ASCII over TCP', 'seplos_v2': 'Seplos V2',
                'seplos_v2-tcp': 'Seplos V2 over TCP'}
_VMAP_MODES = ('poll', 'listen')


# ── command + channel binding validation (ADR-0040 D2c) ───────────────────────
# Shared by the device (§1), command (§2) and installation (§3) services, so a
# binding is judged the same wherever it is written. api-layer because they need
# both control.commands/rules (parse/normalize/assign, PACE_KEYS) and the
# devices-layer channel-override primitives — only api sits above both. The
# installation service owns neither any more; these are imported, not injected.
def validate_commands(cmds, errors, prefix: str = "", tpl=None, safe_def=None, warnings=None, stored=None):
    """Bindings: `{name}` enables a template preset; one that carries `writes` is
    an inline recipe and must parse as a command. A `safe` is checked through the
    command's own bounds — a revert that would fail only when the dead-man fires
    is refused now (CMD-Q-11); a lease without any safe value is said in
    ``warnings`` (stored configs keep loading, ADR-0019)."""
    if cmds is None:
        return
    if not isinstance(cmds, list):
        errors.append(f"{prefix}commands: must be a list")
        return
    seen = set()
    for i, b in enumerate(cmds):
        w = f"{prefix}commands[{i}]"
        if not isinstance(b, dict):
            errors.append(f"{w}: must be an object")
            continue
        name = str(b.get('name', '') or '')
        if not re.match(r'^[a-z][a-z0-9_]{0,31}$', name):
            errors.append(f"{w}.name: use a-z 0-9 _ (1-32 chars, starts with a letter)")
        elif name in seen:
            errors.append(f"{w}.name: '{name}' is declared twice")
        seen.add(name)
        for k in ('enabled', 'confirm'):
            if k in b and not isinstance(b[k], bool):
                errors.append(f"{w}.{k}: must be true or false")
        if 'faces' in b and (not isinstance(b['faces'], dict)
                             or set(b['faces']) - {'api', 'mqtt', 'ha', 'opcua'}
                             or not all(isinstance(v, bool) for v in b['faces'].values())):
            errors.append(f"{w}.faces: an object of api / mqtt / ha / opcua → true|false")
        if 'lease_s' in b and not (isinstance(b['lease_s'], int) and not isinstance(b['lease_s'], bool)
                                   and 0 <= b['lease_s'] <= 86400):
            errors.append(f"{w}.lease_s: 0..86400 seconds")
        cdef = safe_def
        if b.get('writes') is not None:
            try:
                cdef = parse_command_def(name or 'x', b)
            except (ValueError, TypeError, KeyError) as e:
                errors.append(f"{w}: {e}")
        elif cdef is None and tpl is not None and name in (getattr(tpl, 'commands', None) or {}):
            try:
                cdef = parse_command_def(name, tpl.commands[name])
            except (ValueError, TypeError, KeyError):
                cdef = None
        safe = b.get('safe')
        # a stored safe that this save does not change is not judged again:
        # a template update that moved a bound must not block an unrelated
        # save (ADR-0006, CR2-5)
        kept = next((x.get('safe') for x in (stored or []) if isinstance(x, dict)
                     and x.get('name') == name), None)
        if safe is not None and not (stored is not None and safe == kept):
            if not isinstance(safe, dict) or not safe:
                errors.append(f"{w}.safe: an object {{param: value}}, e.g. {{value: 0}}")
            elif cdef is not None:
                try:
                    # every parameter it names exists and is within the
                    # command's bounds, as a run would be judged
                    normalize_params(cdef, {**{k: v.default for k, v in cdef.params.items()
                                               if v.default is not None},
                                            **(cdef.safe or {}), **safe})
                except ValueError as e:
                    errors.append(f"{w}.safe: {e}")
        if isinstance(b.get('lease_s'), int) and b['lease_s'] > 0 \
                and not safe and not (cdef is not None and cdef.safe):
            msg = f"{w}: lease_s {b['lease_s']} without a safe value never fires"
            if warnings is not None:
                warnings.append(msg)
            else:
                logger.warning("command binding: %s", msg)


def validate_channels(chs, errors, prefix: str = "", tpl=None, stored=None, units=None, rules_runtime=None):
    """Channel overrides `{<channel or command name>: {min_interval_s, …}}`
    (ADR-0019). The keys are judged against the template only when the block
    changes (ADR-0006): a template update that drops a channel leaves an orphan,
    listed, never a save that fails. ``rules_runtime`` (the live runtime, or None)
    arms the fast-step pace check — the caller passes ``app.state.rules_runtime``
    rather than this module reaching for ``app`` (ADR-0038 framework-free)."""
    if chs is None:
        return
    if not isinstance(chs, dict):
        errors.append(f"{prefix}channels: an object {{channel: {{{', '.join(CHANNEL_OVERRIDE_KEYS)}}}}}")
        return
    for key, val in chs.items():
        for k, why in channel_override_errors(val).items():
            errors.append(f"{prefix}channels.{key}.{k}: {why}" if k != 'channel'
                          else f"{prefix}channels.{key}: {why}")
    if tpl is not None and chs != (stored or {}):
        defs = {}
        for n, raw in (getattr(tpl, 'commands', None) or {}).items():
            try:
                defs[n] = parse_command_def(n, raw)
            except (ValueError, TypeError, KeyError):
                continue
        declared = {d.channel for d in defs.values() if d.channel}
        ids = assign_channels(defs) if defs else {}
        taken: Dict[str, str] = {}
        for key in sorted(chs, key=lambda k: (k not in declared, k)):
            if key not in declared and key not in defs:
                errors.append(f"{prefix}channels.{key}: not a channel or a command of template '{tpl.id}'")
                continue
            cid = override_cid(key, defs, ids)
            if cid in taken:
                # one channel, one key (CR2-14)
                errors.append(f"{prefix}channels.{key}: channel '{cid}' is already overridden under "
                              f"'{taken[cid]}'")
            else:
                taken[cid] = key
            # a pace this save sets on a channel an armed steps rule with a
            # fast step drives on one of these units: refused (ADR-0022, Q5)
            val, before = chs.get(key), ((stored or {}).get(key) or {})
            paced = sorted(k for k in PACE_KEYS if isinstance(val, dict) and val.get(k)
                           and val.get(k) != (before.get(k) if isinstance(before, dict) else None))
            for dev in (units or []) if (paced and rules_runtime is not None) else []:
                rid = rules_runtime.fast_rule_on(dev, cid)
                if rid is not None:
                    errors.append(f"{prefix}channels.{key}: channel '{cid}' of {dev} is driven by the armed "
                                  f"rule '{rid}', a steps rule with a fast step — a pace ({', '.join(paced)}) "
                                  f"would hold its other reasserts while its emergency step passes it; put "
                                  f"the rule in shadow or remove `fast` first")
                    break


class DeviceValidator:
    """The device-payload validation seam (ADR-0038 service, api-layer for now).

    Collaborators are injected explicitly, making visible what used to be
    captured implicitly from ``create_api``'s 10k-line closure:

      config, template_registry, registry  — the stores it reads
      stored_device_raw(dev_cfg)            — the stored raw of a device (edit locks)
      opcua_block(raw)                      — normalise an opcua block (reused on save)
      errors_detail(errors)                 — build a 422 ``{errors, fields}`` body
      protocols_by_context                  — the per-context protocol allow-lists
    """

    def __init__(self, config, template_registry, registry, *,
                 stored_device_raw, opcua_block, errors_detail, protocols_by_context):
        # The verbatim bodies below close over these names exactly as they did
        # in create_api. config/template_registry/registry are the parameters;
        # the three sibling closures and the protocol table are rebound under
        # the names the bodies use.
        _stored_device_raw = stored_device_raw
        _opcua_block = opcua_block
        _errors_detail = errors_detail
        PROTOCOLS_BY_CONTEXT = protocols_by_context

        # Connection-conflict + bus-claim validation lives in the shared
        # devices-layer module (ADR-0040); compose it here. The three that
        # answer with an HTTP status wrap the error-returning shared versions.
        from multibus.devices.validation import build_connection_claim_validators
        _cv = build_connection_claim_validators(config, template_registry)
        _virtual_map_conflict = _cv.virtual_map_conflict
        _serial_line_conflicts = _cv.serial_line_conflicts
        _network_bus_conflicts = _cv.network_bus_conflicts
        _claim_sig = _cv.claim_sig
        _bus_claim_errors = _cv.bus_claim_errors
        _listening_template = _cv.listening_template

        def _refuse_if_listened(what: str, protocol: str, conn) -> None:
            detail = _cv.listened_refusal(what, protocol, conn)
            if detail is not None:
                raise HTTPException(status_code=409, detail=detail)

        def _device_claim_check(raw: Dict, did: str, prev: Optional[Dict] = None) -> None:
            errs = _cv.device_claim_errors(raw, did, prev)
            if errs:
                raise HTTPException(status_code=422, detail={"errors": errs})

        def _serial_missing(sp: str, busy_id: str = '') -> str:
            """'' when the serial port is there; else what a Test says (QA-D-10):
            missing, and — when the host has a port by that name the container
            does not — the line that maps it."""
            from multibus.transports.serial import serial_inventory, serial_ports
            if not sp or os.path.exists(serial_ports._p(serial_inventory.SYSROOT, sp)):
                return ''
            msg = (f"Serial port {sp} does not exist in the gateway's container — nothing was asked"
                   + (f", and device '{busy_id}' cannot read on it either" if busy_id else ''))
            try:
                name = os.path.basename(sp)
                port = next((p_ for p_ in serial_inventory.inventory(config, template_registry).get('ports', [])
                             if p_.get('state') == 'not_mapped' and (p_.get('name') == name or p_.get('path') == sp)), None)
            except Exception:  # noqa: BLE001 — the inventory only adds a hint
                port = None
            if port and port.get('fix'):
                msg += (f". The host has {port.get('name')}, but it is not mapped into the gateway's container: "
                        f"add `{port['fix'].get('compose', '')}` under the gateway's devices: in docker-compose "
                        f"(or `{port['fix'].get('docker_run', '')}`) and recreate the container")
            else:
                msg += ": check the path, that the adapter is plugged in and that it is mapped into the container"
            return msg

        def _test_on_shared_bus(conn: Dict, unit_id: int, address: int, timeout: float,
                                where: str, busy_id: str) -> Dict:
            """One read on a bus a running device already holds, through the same
            socket and lock (a temporary unit on the shared transport)."""
            from multibus.core.config import ModbusConfig
            from multibus.transports.modbus.modbus_client import ModbusConnection
            mc = ModbusConnection(ModbusConfig(
                host=str(conn.get('host', '')), port=int(conn.get('port', 502) or 502),
                unit_id=unit_id, timeout=timeout, retry_attempts=1, retry_delay=0,
                protocol=str(conn.get('protocol', 'rtu-tcp')),
                serial_port=str(conn.get('serial_port', '') or ''),
                baudrate=int(conn.get('baudrate', 9600) or 9600), parity=str(conn.get('parity', 'N') or 'N'),
                stopbits=int(conn.get('stopbits', 1) or 1)), trace_label=f"test:{where}")
            t0 = time.perf_counter()
            try:
                words = mc.read_registers(address, 2, "holding")
            except Exception as e:  # noqa: BLE001
                words, err = None, str(e)
            else:
                err = ""
            finally:
                mc.disconnect()                     # releases the claim, keeps the socket
            lat = round((time.perf_counter() - t0) * 1000, 1)
            # only a serial port on this host can be missing (a bus over TCP has none)
            serial = str(conn.get('protocol', '')).lower() in SERIAL_PROTOCOLS
            missing = ('' if words is not None or not serial
                       else _serial_missing(str(conn.get('serial_port', '') or ''), busy_id))
            if missing:
                return {"ok": False, "latency_ms": lat, "shared_with": busy_id, "message": missing}
            if words is not None:
                return {"ok": True, "latency_ms": lat, "shared_with": busy_id,
                        "message": f"Unit {unit_id} answered in {lat} ms — through the connection "
                                   f"device '{busy_id}' already uses on this bus (one connection, "
                                   f"taking turns)"}
            return {"ok": False, "latency_ms": lat, "shared_with": busy_id,
                    "message": f"No answer from unit {unit_id} on {where} — the bus itself works "
                               f"(device '{busy_id}' reads on it): check the unit id and that the "
                               f"slave is wired to this bus{(' — ' + err) if err else ''}"}

        def _meter_only_raw(payload: Dict, did: str, template_id: str, errors: List[str],
                            existing_id: Optional[str]) -> Dict:
            """A device that IS its meters (a UPS with no communication of its
            own): ``connection: {protocol: none}``, no template, no address. It
            is created with its meters (``meters: [...]``, at least one) and its
            kind (``kind``, e.g. ups); it reads nothing, writes nothing, and is
            online when its meters are (MeterOnlyClient)."""
            from multibus.devices import meter_links
            from multibus.core.config import DeviceConfig
            from multibus.catalog.device_kind import METER_ONLY
            fields: Dict[str, str] = {}
            if template_id:
                fields['template'] = 'a meter-only device has no template — it reads nothing itself'
            extra = sorted(k for k in (payload.get('connection') or {}) if k != 'protocol')
            if extra:
                fields['connection'] = f"a meter-only device has no address ({', '.join(extra)})"
            raw = {'id': did, 'name': str(payload.get('name', '') or did), 'template': '',
                   'enabled': flag(payload, 'enabled', True), 'connection': {'protocol': 'none'}}
            if not existing_id:
                from multibus.catalog import device_types as _dtypes
                k, why = _dtypes.from_payload(payload)
                why = why or (_dtypes.override_error(k, meter_only=True) if k is not None else None)
                if k is None or why:
                    fields['type' if 'type' in payload else 'kind'] = (
                        why or f"what it is — one of {', '.join(METER_ONLY)} or a user type (it has no template "
                               "to say it; a meter is not measured by meters)")
                else:
                    raw['kind'] = _dtypes.stored_form(k)
                probe = DeviceConfig(id=did, name=raw['name'], protocol='none')
                links, lerrs = (meter_links.validate(config, probe, payload.get('meters'))
                                if payload.get('meters') is not None
                                else ([], {'meters': 'a meter-only device is its meters — name at least one'}))
                fields.update(lerrs)
                if not lerrs and not links:
                    fields['meters'] = 'a meter-only device is its meters — name at least one'
                if not lerrs and links:
                    legacy, listed = meter_links.stored_form(links)
                    if legacy:
                        raw['energy_meter'] = legacy
                    else:
                        raw['meters'] = listed
                if 'options' in payload:
                    opts, oerrs = meter_links.check_options(payload['options'])
                    fields.update(oerrs)
                    pair = {ln['role'] for ln in (links or [])} >= {'input', 'output'}
                    if not oerrs and opts and not pair:
                        fields['options'] = ('thresholds belong to an input and an output meter — '
                                             'this device has no pair')
                    elif not oerrs and opts:
                        raw['meter_options'] = opts
            mqtt_block = dict(payload.get('mqtt') or {})
            if 'ha_discovery_enabled' in payload:
                mqtt_block['ha_discovery'] = flag(payload, 'ha_discovery_enabled')
            if mqtt_block:
                raw['mqtt'] = mqtt_block
            if payload.get('influxdb'):
                raw['influxdb'] = payload['influxdb']
            if errors or fields:
                raise HTTPException(status_code=422, detail={
                    "errors": errors + [f"{k}: {v}" for k, v in fields.items()], "fields": fields})
            return raw

        def _timeout_error(conn: Dict, stored: Optional[Dict]) -> Optional[str]:
            """A Modbus timeout a save sets: 1..30 s, as the form says. One kept
            as stored (a sub-second value written by hand) is not judged again —
            it never blocks an edit of something else."""
            if 'timeout' not in conn or conn.get('timeout') == (stored or {}).get('timeout'):
                return None
            try:
                t = float(conn['timeout'])
            except (TypeError, ValueError):
                return "connection.timeout: must be 1..30 seconds"
            return None if 1 <= t <= 30 else "connection.timeout: must be 1..30 seconds"

        def _edit_locks(conn: Dict, template_id: str, existing_id: Optional[str]) -> List[Tuple[str, str]]:
            """What the edit locks refuse in a device save (connection_spec.edit_errors):
            the stored device's spec against the one this save would store, with
            its template's family and the components it holds."""
            from multibus.transports.serial.bridges import TYPES as _BT, resolve_connection as _resolve
            from multibus.devices.connection_spec import edit_errors, spec_of
            from multibus.catalog.device_template import template_transport
            conn = dict(conn or {})
            if conn.get('bridge') and str(conn.get('protocol', '')).lower() in VIRTUAL_MAP_PROTOCOLS:
                conn = {k: v for k, v in conn.items() if k not in ('bridge', 'bridge_port')}   # refused below
            new = spec_of(conn, config.bridges, _BT)
            old, was = None, None
            if existing_id:
                was = config.get_device(existing_id)
                if was is not None:
                    old = spec_of((_stored_device_raw(was).get('connection')
                                   or ((config.get_raw_device(existing_id) or {}).get('sources') or [{}])[0]),
                                  config.bridges, _BT)
            proto = str((_resolve(conn, config.bridges) if conn.get('bridge') else conn).get('protocol', 'tcp')).lower()
            tpl = template_registry.get(template_id) if template_id else None
            fam = (template_transport(tpl) if tpl is not None and not tpl.component_of
                   and proto in PROTOCOLS_BY_CONTEXT['device'] else None)
            return edit_errors(old, new, {
                'id': existing_id or '', 'components': len(config.component_devices(existing_id)) if was else 0,
                'template_changed': was is not None and template_id != (was.template or ''),
                'template': template_id, 'template_family': fam, 'protocol': proto})

        def _validate_device_payload(payload: Dict, *, existing_id: str = None,
                                     stored_conn: Optional[Dict] = None) -> Dict:
            """Normalize + validate the raw device dict from the UI. Raises
            HTTPException(422) with a per-field error list. ``stored_conn``: the
            connection saved before (an edit, a restore), whose values stand."""
            errors = []
            did = str(payload.get('id', existing_id or '')).strip().lower()
            if not _DEVICE_ID_RE.match(did):
                errors.append("id: use a-z 0-9 - _ (2-64 chars, starts alphanumeric)")
            if existing_id and did != existing_id:
                errors.append("id: cannot be changed after creation")
            if not existing_id and registry.has(did):
                errors.append(f"id: '{did}' already exists")
            if not existing_id:
                # both publish under the device topic pattern: one id, one MQTT identity
                # (R2D-NAMESPACE-1 — the Virtual BMS side already refuses a device's id)
                errors += vbms_id_error([did], getattr(config, 'virtual_bms', None))
            if not existing_id and any(str(e.get('id')) == did for e in (config.endpoints or [])):
                # a formula asks for the device first: it would hide the installation
                errors.append(f"id: '{did}' is an installation's id — a device by that name would hide it in formulas")
            template_id = str(payload.get('template', '')).strip()
            conn = payload.get('connection', {}) or {}
            _conn_sent = conn          # as sent: a bridge's own values are not the device's to judge
            _none = str(conn.get('protocol', '')).lower() == 'none'
            # the edit locks, in one place (ADR-0026 §2): judged on the stored
            # device's spec and the one this save would store
            _locks = _edit_locks(conn, template_id, existing_id)
            if _locks and _locks[0][1] == METER_ONLY_TEXT:
                # read through a connection, or measured by its meters: decided at
                # creation (its meters and its kind come with it) — never by an
                # edit that would leave a device with neither (review N3)
                raise HTTPException(status_code=422, detail={
                    "errors": [METER_ONLY_TEXT], "fields": {"connection.protocol": "fixed after creation"}})
            _lock_fields = {f: e.partition(': ')[2] if e.startswith(f + ': ') else e for f, e in _locks}
            if _none:
                return _meter_only_raw(payload, did, template_id, errors, existing_id)
            _bridge_ref = None
            if conn.get('bridge') and str(conn.get('protocol', '')).lower() in VIRTUAL_MAP_PROTOCOLS:
                errors.append(f"connection.bridge: a {conn.get('protocol')} bus is reached by its serial "
                              f"port or a bridge's host and port, not through a bridge record")
                conn = {k: v for k, v in conn.items() if k not in ('bridge', 'bridge_port')}
            if conn.get('bridge'):
                # a device on a bridge: the bridge must exist and own that port;
                # host, port and protocol then come from it (checked as such below)
                _br = config.get_raw_bridge(str(conn['bridge']))
                if _br is None:
                    errors.append(f"connection.bridge: no bridge '{conn['bridge']}' — add it on the Devices page first")
                else:
                    _bports = [int(p.get('port')) for p in (_br.get('ports') or []) if p.get('port')]
                    from multibus.transports.serial.bridges import TYPES as _BT, resolve_connection as _resolve
                    _bt = _BT.get(_br.get('type'), {})
                    if conn.get('bridge_port') is not None and _bt.get('discovery') == 'manual' \
                            and int(conn.get('bridge_port')) not in _bports:
                        errors.append(f"connection.bridge_port: {conn.get('bridge_port')} is not a port of "
                                      f"'{_br['id']}' ({', '.join(map(str, _bports)) or 'none declared'})")
                    # what is SAVED is the reference; what is CHECKED is what it resolves to
                    # (everything but the address, which is the bridge's: a tuning
                    # key such as max_gap is the device's own, bridge or not)
                    _bridge_ref = {k: v for k, v in conn.items() if v is not None and k not in (
                        'host', 'port', 'serial_port', 'url', 'broker', 'protocol', 'max_connections',
                        'baudrate', 'parity', 'stopbits', 'bytesize')}   # the line is the bridge's
                    if str(conn.get('protocol', '')).lower() == 'rtu_tap':
                        # listening needs the bus's own bytes: a gateway turns them
                        # into Modbus TCP and only answers questions asked of it
                        if _bt.get('framing', 'rtu') == 'modbus_tcp':
                            errors.append(f"connection.protocol: '{_br['id']}' is a Modbus TCP gateway — it only "
                                          "answers questions, it does not pass the bus's traffic on; listening "
                                          "needs a transparent bridge or the MBG serial bridge")
                        elif _bt.get('framing', 'rtu') != 'rtu':
                            errors.append(f"connection.protocol: '{_br['id']}' carries {_bt.get('framing')} frames — "
                                          "listening decodes Modbus RTU")
                        _bridge_ref['protocol'] = 'rtu_tap'
                    conn = _resolve(conn, config.bridges)
                    _bridge_ref.setdefault('bridge_port', conn.get('port'))
            protocol = str(conn.get('protocol', 'tcp')).lower()
            _asks = ((protocol in MODBUS_PROTOCOLS and protocol != 'rtu_tap')
                     or (protocol in VIRTUAL_MAP_PROTOCOLS and _vmap_mode(conn) == 'poll'))
            if protocol not in PROTOCOLS_BY_CONTEXT['device']:
                errors.append("connection.protocol: must be one of " + ", ".join(PROTOCOLS_BY_CONTEXT['device']))
            elif _asks:
                _te = _timeout_error(conn, stored_conn)
                if _te:
                    errors.append(_te)
            # A template's register map is transport-specific (Modbus reads by address,
            # HTTP/MQTT by json_path), so the device protocol MUST match the template's
            # transport class — otherwise every read silently resolves to nothing.
            _tpl = template_registry.get(template_id) if template_id else None
            if template_id and _tpl is None:
                errors.append(f"template: '{template_id}' not found")
            elif _tpl is not None and _tpl.component_of:
                errors.append(f"template: '{template_id}' is a component of '{_tpl.component_of}' — "
                              "add the device, then this as one of its components")
                _tpl = None
            if _locks:
                # a template's family, the class a device keeps, a device holding
                # components (edit_errors): where the family check always stood
                errors.extend(e for _f, e in _locks)
            if (_tpl is not None and protocol in PROTOCOLS_BY_CONTEXT['device']
                    and not any(f == 'template' for f, _e in _locks)):
                # the template's family matches: does it only ever listen?
                from multibus.catalog.device_template import template_listens, template_listens_only
                if (template_listens_only(_tpl) and protocol in VIRTUAL_MAP_PROTOCOLS
                        and _vmap_mode(conn) != 'listen'):
                    errors.append(f"connection.mode: template '{template_id}' is only heard on a bus "
                                  "another master polls — choose listen")
                elif protocol_family(protocol) == 'modbus' and protocol != 'rtu_tap' and template_listens(_tpl):
                    # a map written for listening never polls (ADR-0025, ADD-8)
                    errors.append(f"connection.protocol: template '{template_id}' is only heard on a bus "
                                  "another master polls — choose listen only (rtu_tap)")
            if protocol == 'http':
                url = str(conn.get('url', '')).strip()
                if not (url.startswith('http://') or url.startswith('https://')):
                    errors.append("connection.url: required (http:// or https://) for HTTP/JSON")
                elif not config.security.allow_nonlan_http_devices:
                    from multibus.transports.http.http_client import lan_url_error
                    _e = lan_url_error(url)                # SSRF guard
                    if _e:
                        errors.append(f"connection.url: {_e} — set "
                                      "security.allow_nonlan_http_devices=true to allow it")
            elif protocol == 'mqtt':
                if not str(conn.get('broker', '')).strip():
                    errors.append("connection.broker: required for MQTT input")
                if not str(conn.get('topic', '')).strip():
                    errors.append("connection.topic: required for MQTT input")
                else:
                    from multibus.core.text_check import topic_issue
                    _ti = topic_issue(str(conn.get('topic')), "connection.topic")
                    if _ti:
                        errors.append(_ti)
                from multibus.transports.mqtt.mqtt_input import keepalive_errors
                errors.extend(keepalive_errors(conn.get('keepalive'), 'connection.keepalive'))
                if conn.get('tls_ca') is not None and not isinstance(conn.get('tls_ca'), str):
                    errors.append("connection.tls_ca: the path of a CA certificate file")
                _vars = conn.get('vars')
                if _vars is not None:
                    from multibus.transports.mqtt.mqtt_input import VAR_VALUE_RE
                    if not isinstance(_vars, dict) or len(_vars) > 16:
                        errors.append("connection.vars: a mapping of up to 16 topic variables")
                    else:
                        for _k, _v in _vars.items():
                            from multibus.core.text_check import topic_issue
                            _ti = topic_issue(str(_v), f"connection.vars.{_k}")
                            if _ti:
                                errors.append(_ti)
                                continue
                            if not re.match(r"^[a-z][a-z0-9_]{0,31}$", str(_k)) or not VAR_VALUE_RE.match(str(_v)):
                                errors.append(f"connection.vars.{_k}: a name (a-z 0-9 _) and topic levels without + or #")
                try:
                    mp = int(conn.get('port', 1883))
                    if not (1 <= mp <= 65535):
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append("connection.port: must be 1..65535")
            elif protocol in CAN_PROTOCOLS:
                # a CAN bus listened to through a carrier (can_client.py)
                from multibus.transports.can.can_client import connection_errors as _can_errors
                errors.extend(_can_errors(conn))
            elif protocol in VIRTUAL_MAP_PROTOCOLS:
                # a non-Modbus bus with its own driver: a serial port here, or a
                # bridge's raw byte stream at host:port; polled or listened to
                if _vmap_mode(conn) not in _VMAP_MODES:
                    errors.append("connection.mode: poll (the gateway asks) or listen (it never transmits)")
                elif _vmap_mode_missing(conn, stored_conn):
                    errors.append("connection.mode: choose poll (the gateway asks) or listen (it never "
                                  "transmits) — a bus a master pack already polls must be listened to")
                if protocol in VIRTUAL_MAP_SERIAL:
                    sp = str(conn.get('serial_port', '')).strip()
                    if not sp:
                        errors.append(f"connection.serial_port: required for {_PROTO_WORDS.get(protocol, protocol)}")
                    else:
                        errors.extend(_serial_line_conflicts(protocol, sp, conn, existing_id))
                    for fld, lo, hi in (("baudrate", 300, 4_000_000), ("stopbits", 1, 2), ("bytesize", 5, 8)):
                        if fld in conn:
                            try:
                                if not (lo <= int(conn.get(fld)) <= hi):
                                    raise ValueError
                            except (TypeError, ValueError):
                                errors.append(f"connection.{fld}: must be {lo}..{hi}")
                    if str(conn.get('parity', 'N')).upper() not in ('N', 'E', 'O'):
                        errors.append("connection.parity: must be N, E or O")
                else:
                    if not str(conn.get('host', '')).strip():
                        errors.append(f"connection.host: required for {_PROTO_WORDS.get(protocol, protocol)}")
                    elif _is_serial_path(conn.get('host')):
                        errors.append(_serial_host_error("connection.host", conn.get('host')))
                    try:
                        if not (1 <= int(conn.get('port', 0)) <= 65535):
                            raise ValueError
                    except (TypeError, ValueError):
                        errors.append("connection.port: must be 1..65535")
                    else:
                        errors.extend(_network_bus_conflicts(protocol, conn, existing_id))
                try:
                    if not (0 <= int(conn.get('unit_id', 0)) <= 255):
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append("connection.unit_id: must be 0..255 (the pack address)")
            elif protocol == 'rtu_tap' and _bridge_ref:
                # a tap through a bridge: the bus is the bridge's port
                errors.extend(_network_bus_conflicts(protocol, conn, existing_id))
                try:
                    if not (0 <= int(conn.get('unit_id', 1)) <= 255):
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append("connection.unit_id: must be 0..255")
            elif protocol in SERIAL_PROTOCOLS + ('rtu_tap',):
                sp = str(conn.get('serial_port', '')).strip()
                if not sp:
                    errors.append(f"connection.serial_port: required for Modbus {'ASCII' if protocol == 'ascii' else 'RTU'}")
                else:
                    errors.extend(_serial_line_conflicts(protocol, sp, conn, existing_id))
                for fld, lo, hi in (("baudrate", 300, 4_000_000), ("stopbits", 1, 2),
                                    ("bytesize", 5, 8), ("unit_id", 0, 255)):
                    if fld in conn or fld == "unit_id":
                        try:
                            v = int(conn.get(fld, {"stopbits": 1, "bytesize": 8, "unit_id": 1,
                                                   "baudrate": 9600}[fld]))
                            if not (lo <= v <= hi):
                                raise ValueError
                        except (TypeError, ValueError):
                            errors.append(f"connection.{fld}: must be {lo}..{hi}")
                if str(conn.get('parity', 'N')).upper() not in ('N', 'E', 'O'):
                    errors.append("connection.parity: must be N, E or O")
            else:
                if protocol in NETWORK_PROTOCOLS and not str(conn.get('host', '')).strip():
                    errors.append(f"connection.host: required for {_PROTO_WORDS.get(protocol, protocol)}")
                elif _is_serial_path(conn.get('host')):
                    errors.append(_serial_host_error("connection.host", conn.get('host')))
                try:
                    port = int(conn.get('port', 502))
                    if not (1 <= port <= 65535):
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append("connection.port: must be 1..65535")
                if protocol in BUS_OVER_TCP_PROTOCOLS or _bridge_ref:
                    errors.extend(_network_bus_conflicts(protocol, conn, existing_id))
                try:
                    unit = int(conn.get('unit_id', 1))
                    if not (0 <= unit <= 255):
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append("connection.unit_id: must be 0..255")
            # the advanced keys a save sets anew, each in its range (L3-F7)
            from multibus.devices.conn_advanced import errors as _advanced_errors
            errors.extend(_advanced_errors(_conn_sent if isinstance(_conn_sent, dict) else {}, stored_conn, protocol))
            if errors:
                _d = _errors_detail(errors)
                for _f, _m in _lock_fields.items():
                    _d.setdefault('fields', {}).setdefault(_f, _m)
                raise HTTPException(status_code=422, detail=_d)
            raw = {
                'id': did,
                'name': str(payload.get('name', '') or did),
                'template': template_id,
                'enabled': flag(payload, 'enabled', True),
                # a device on a bridge keeps only the reference — host, port and
                # protocol are the bridge's, resolved whenever the list is built
                'connection': _bridge_ref or conn,
            }
            mqtt_block = dict(payload.get('mqtt') or {})
            # per-device HA discovery toggle lives under mqtt.ha_discovery
            if 'ha_discovery_enabled' in payload:
                mqtt_block['ha_discovery'] = flag(payload, 'ha_discovery_enabled')
            if mqtt_block:
                raw['mqtt'] = mqtt_block
            if payload.get('influxdb'):
                raw['influxdb'] = payload['influxdb']
                _e = influx_block_errors(raw['influxdb'], 'influxdb', outputs=False)
                if _e:
                    raise HTTPException(status_code=422, detail={"errors": _e})
            if 'opcua' in payload:
                raw['opcua'] = _opcua_block(payload['opcua'])
            return raw

        # --- public surface (what create_api / the routes call) ---------------
        self.validate_device_payload = _validate_device_payload
        self.device_claim_check = _device_claim_check
        self.influx_block_errors = influx_block_errors
        self.refuse_if_listened = _refuse_if_listened
        self.timeout_error = _timeout_error
        self.serial_missing = _serial_missing
        self.test_on_shared_bus = _test_on_shared_bus
        self.listening_template = _listening_template
