# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Shared connection + bus-claim validation (ADR-0040), in the devices layer so
both the device service (§1) and the installation/endpoint service (§3) compose
it instead of cross-injecting each other's closures.

Framework-free: these functions RETURN errors (a ``List[str]``) or a refusal
detail (``dict``), never raise ``HTTPException`` — the api layer wraps them and
raises. ``build_connection_claim_validators(config, template_registry)`` returns
the set as closures over that config/registry (the bodies are the ones that used
to live in ``DeviceValidator.__init__``, moved verbatim; the three that used to
raise now return their errors for the caller to raise).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from multibus.core.config import (MODBUS_PROTOCOLS, NETWORK_PROTOCOLS, SERIAL_PROTOCOLS,
                                  VIRTUAL_MAP_OVER_TCP, VIRTUAL_MAP_PROTOCOLS, VIRTUAL_MAP_SERIAL)
from multibus.devices.device_validation import _vmap_mode


def build_connection_claim_validators(config, template_registry) -> SimpleNamespace:
    """The connection-conflict and bus-claim validators, bound to ``config`` and
    ``template_registry``. Returned as a namespace; the api-layer validators
    compose them (and raise HTTPException on the error-returning ones)."""

    def _virtual_map_conflict(protocol: str, dproto: str, conn: Dict, d, where: str):
        """Two devices on one bus where either speaks a virtual-map protocol
        (seplos_v2): None when neither does (the Modbus rules apply); else the
        refusal, or [] when they may share it — the same protocol, the same
        mode (a bus is polled or listened to, never both), and each pack its
        own address."""
        if protocol not in VIRTUAL_MAP_PROTOCOLS and dproto not in VIRTUAL_MAP_PROTOCOLS:
            return None
        if protocol != dproto:
            return [f"connection.protocol: {where} is used by device '{d.id}' as {dproto} — "
                    f"one bus speaks one protocol"]
        theirs = _vmap_mode(d.connection)
        if _vmap_mode(conn) != theirs:
            return [f"connection.mode: {where} is {'listened to' if theirs == 'listen' else 'polled'} by "
                    f"device '{d.id}' — a bus is either polled or listened to, never both"]
        try:
            same_unit = int(conn.get('unit_id', 0) or 0) == int(getattr(d.connection, 'unit_id', -1))
        except (TypeError, ValueError):
            same_unit = False
        if same_unit:
            return [f"connection.unit_id: pack {conn.get('unit_id', 0)} on {where} is already read by "
                    f"device '{d.id}' — each pack on a bus has its own address"]
        return []

    def _serial_line_conflicts(protocol: str, sp: str, conn: Dict,
                               existing_id: str = None) -> List[str]:
        """Who else holds serial line `sp`. One physical line cannot be driven
        by two independent masters (each ModbusClient owns its own lock), and a
        tty opens once — so an active RTU device and a tap never share a port.
        Taps DO share one (a single TapReader per port fans frames out by unit
        id), but that reader opens the line once, with the first tap's settings:
        a second tap must agree on them or it would silently decode garbage.
        A line is its canonical path: a tty and its by-id link are one (ADD-13)."""
        from multibus.transports.serial.bus_claims import canonical_serial
        line = canonical_serial(sp)
        for d in config.devices:
            dproto = getattr(d, 'protocol', '')
            if (getattr(d, 'id', None) == existing_id
                    or dproto not in SERIAL_PROTOCOLS + ('rtu_tap',) + VIRTUAL_MAP_SERIAL
                    or canonical_serial(getattr(d.connection, 'serial_port', '')) != line):
                continue
            _vm = _virtual_map_conflict(protocol, dproto, conn, d, f"'{sp}'")
            if _vm is not None:
                if _vm:
                    return _vm
                ours = (int(conn.get('baudrate', 9600) or 9600), str(conn.get('parity', 'N') or 'N').upper())
                theirs = (int(d.connection.baudrate or 9600), str(d.connection.parity or 'N').upper())
                if ours != theirs:
                    return [f"connection.baudrate: '{sp}' is read by '{d.id}' at {theirs[0]} {theirs[1]} — "
                            f"every device on one line uses the same line settings"]
                continue
            if protocol in SERIAL_PROTOCOLS and dproto in SERIAL_PROTOCOLS and protocol != dproto:
                return [f"connection.protocol: '{sp}' is read as {dproto.upper()} by device '{d.id}' — "
                        f"the slaves on one line all speak one framing (RTU or ASCII)"]
            if protocol == dproto and protocol in SERIAL_PROTOCOLS:
                # several slaves on one bus: they share the open line and take
                # turns; the slave address must differ, the line settings agree
                if int(conn.get('unit_id', 1)) == int(getattr(d.connection, 'unit_id', -1)):
                    return [f"connection.unit_id: unit {conn.get('unit_id', 1)} on '{sp}' is "
                            f"already read by device '{d.id}' — each slave on a bus has its own address"]
            if protocol != dproto:
                return [f"connection.serial_port: '{sp}' is already used by device "
                        f"'{d.id}' as {dproto} — a serial line is either polled or "
                        f"tapped, never both"]
            ours = (int(conn.get('baudrate', 9600) or 9600),
                    str(conn.get('parity', 'N') or 'N').upper())
            theirs = (int(d.connection.baudrate or 9600),
                      str(d.connection.parity or 'N').upper())
            if ours != theirs:
                how = 'tapped' if dproto == 'rtu_tap' else 'read'
                return [f"connection.baudrate: '{sp}' is {how} by '{d.id}' at "
                        f"{theirs[0]} {theirs[1]} — every device on one line uses the "
                        f"same line settings"]
        return []

    def _network_bus_conflicts(protocol: str, conn: Dict, existing_id: str = None) -> List[str]:
        """Who else is on the bus behind host:port. An rtu-tcp host:port IS one
        RS-485 bus (and so is a Modbus TCP gateway's port reached through a
        bridge). Several slaves on it are several devices: they share ONE socket
        and take turns on it (the shared transport — a converter that accepts a
        single client is never fought over). What must not repeat is the slave
        address: two devices with one unit id on one bus would read the same
        slave and answer for each other. And, as on a serial line, a bus is
        either polled or tapped: a tap listens to another master, and the
        bridge's one connection cannot be both ours to ask and ours to hear."""
        _host = str(conn.get('host', '')).strip().lower()
        try:
            _port = int(conn.get('port', 502))
            _unit = int(conn.get('unit_id', 1))
        except (TypeError, ValueError):
            return []
        for d in config.devices:
            dproto = getattr(d, 'protocol', '')
            if (getattr(d, 'id', None) == existing_id
                    or dproto not in NETWORK_PROTOCOLS + ('rtu_tap',) + VIRTUAL_MAP_OVER_TCP
                    or str(getattr(d.connection, 'host', '')).strip().lower() != _host
                    or int(getattr(d.connection, 'port', 0) or 0) != _port
                    or (dproto == 'udp') != (protocol == 'udp')):     # a UDP port is not the TCP one
                continue
            _vm = _virtual_map_conflict(protocol, dproto, conn, d, f"{_host}:{_port}")
            if _vm is not None:
                if _vm:
                    return _vm
                continue
            if (dproto != protocol and 'rtu_tap' not in (dproto, protocol)):
                return [f"connection.protocol: {_host}:{_port} is read as {dproto} by device "
                        f"'{d.id}' — one port speaks one framing"]
            if (dproto == 'rtu_tap') != (protocol == 'rtu_tap'):
                return [f"connection.protocol: the bus at {_host}:{_port} is already "
                        f"{'tapped' if dproto == 'rtu_tap' else 'polled'} by device '{d.id}' — "
                        f"a bus is either polled or tapped, never both"]
            if dproto == protocol and int(getattr(d.connection, 'unit_id', -1)) == _unit:
                return [f"connection.unit_id: unit {_unit} on {_host}:{_port} is "
                        f"already {'tapped' if protocol == 'rtu_tap' else 'read'} by device "
                        f"'{d.id}' — each slave on a bus has its own address"]
        return []

    def _listened_refusal(what: str, protocol: str, conn) -> Optional[Dict]:
        """The 409 detail when a device listens on the bus ``conn`` is on
        (ADR-0025): another master owns it, so nothing is sent — no override.
        Returns the refusal detail, or None when the bus is free. (The api
        caller raises HTTPException(409, detail).)"""
        from multibus.transports.serial.bus_claims import bus_of, listeners, refusal_detail, where_of
        bus = bus_of(protocol, conn)
        ids = listeners(config, bus, template_registry)
        if ids:
            return refusal_detail(what, bus, ids, where_of(protocol, conn))
        return None

    def _claim_sig(built, standby=False):
        from multibus.transports.serial.bus_claims import source_claims
        return sorted((b, lis) for d in built for _i, b, lis, _w in source_claims(d, template_registry, standby))

    def _bus_claim_errors(built, exclude, field_of, prev_built=None) -> List[str]:
        """The bus claim on a save (ADR-0025): the same rule as a Test, both
        ways — a polled source on a bus another device only listens to, a
        listening one on a bus another device polls. Judged on what changes
        (ADR-0006): an edit that leaves every source on the bus it was on, in
        the role it had there, is not judged again."""
        from multibus.transports.serial.bus_claims import save_conflicts
        if prev_built is not None and _claim_sig(prev_built) == _claim_sig(built):
            return []
        out: List[str] = []
        for dev, i, why in save_conflicts(config, built, template_registry, exclude):
            e = f"{field_of(dev, i)}: {why}"
            if e not in out:
                out.append(e)
        return out

    def _listening_template(tid: str) -> bool:
        """A template that only listens (ADR-0025): its devices never transmit."""
        from multibus.catalog.device_template import template_listens
        tpl = template_registry.get(tid) if tid else None
        return tpl is not None and template_listens(tpl)

    def _device_claim_errors(raw: Dict, did: str, prev: Optional[Dict] = None) -> List[str]:
        """A device save that breaks the bus claim: the errors, field by field
        (the api caller raises 422 when non-empty)."""
        built = config.preview_devices(device=raw)
        prev_built = config.preview_devices(device=prev) if prev else None
        own = {did} | {c.id for c in config.component_devices(did)}   # read through it
        keyed = bool(raw.get('sources'))
        return _bus_claim_errors(built, own, lambda _d, i: (f"sources[{i}].protocol" if keyed
                                                            else "connection.protocol"), prev_built)

    def _endpoint_claim_errors(raw: Dict, pid: str, prev: Optional[Dict] = None) -> List[str]:
        """An installation save that breaks the bus claim, or puts a template
        written for listening on a unit that would poll (ADD-8): the errors,
        each at the group / source it comes from (the api caller raises 422)."""
        built = config.preview_devices(endpoint=raw)
        prev_built = config.preview_devices(endpoint=prev) if prev else None
        own = {d.id for d in built} | {d.id for d in config.endpoint_devices(pid)}
        groups = [g for g in (raw.get('groups') or []) if isinstance(g, dict)]
        by_gid = {str(g.get('id', '')).strip().lower(): (gi, g) for gi, g in enumerate(groups)}

        def field_of(d, i: int) -> str:
            gi, g = by_gid.get(str(getattr(d, 'group_id', '') or '').lower(), (None, None))
            if g is not None and g.get('sources'):
                return f"groups[{gi}].sources[{i}].protocol"
            if g is not None and g.get('connection'):
                return f"groups[{gi}].connection.protocol"
            return f"sources[{i}].protocol" if raw.get('sources') else "connection.protocol"

        def heard(devs):
            return sorted((f"{d.group_id}/{i}", str(s.protocol).lower(), s.template or d.template)
                          for d in devs for i, s in enumerate(d.sources or []))
        errs: List[str] = []
        if prev_built is None or heard(prev_built) != heard(built):
            for d in built:
                for i, s in enumerate(d.sources or []):
                    tid = s.template or d.template
                    p = str(s.protocol or '').lower()
                    if p in MODBUS_PROTOCOLS and p != 'rtu_tap' and _listening_template(tid):
                        e = (f"{field_of(d, i)}: template '{tid}' is only heard on a bus another master "
                             f"polls — choose listen only (rtu_tap)")
                        if e not in errs:
                            errs.append(e)
        errs += [e for e in _bus_claim_errors(built, own, field_of, prev_built) if e not in errs]
        return errs

    return SimpleNamespace(
        virtual_map_conflict=_virtual_map_conflict,
        serial_line_conflicts=_serial_line_conflicts,
        network_bus_conflicts=_network_bus_conflicts,
        listened_refusal=_listened_refusal,
        claim_sig=_claim_sig,
        bus_claim_errors=_bus_claim_errors,
        listening_template=_listening_template,
        device_claim_errors=_device_claim_errors,
        endpoint_claim_errors=_endpoint_claim_errors,
    )


# ── InfluxDB output-block shape (shared by device + endpoint, ADR-0040) ───────
import re as _re  # noqa: E402

_INFLUX_NAME_RE = _re.compile(r'^[A-Za-z][A-Za-z0-9_.\-]{0,63}$')
_INFLUX_RESERVED = {'_time', '_measurement', '_field', '_value', '_start', '_stop', 'time'}


def influx_block_errors(ix: Dict, w: str, *, outputs: bool) -> List[str]:
    """Shape-check an ``influxdb`` block's operator-editable parts: static
    ``tags`` (device and endpoint) and the endpoint's extra ``outputs``.
    Names must be Influx-safe and never one of its own columns."""
    errors: List[str] = []
    if not isinstance(ix, dict):
        return [f"{w}: must be an object"]

    def _tags(tags, where):
        if tags in (None, {}):
            return
        if not isinstance(tags, dict):
            errors.append(f"{where}: must be a mapping of name: value")
            return
        for k, v in tags.items():
            if not _INFLUX_NAME_RE.match(str(k)) or str(k) in _INFLUX_RESERVED:
                errors.append(f"{where}.{k}: tag names start with a letter "
                              "(letters, digits, _ . -) and are not Influx columns")
            elif v is None or not str(v).strip() or len(str(v)) > 128:
                errors.append(f"{where}.{k}: value required (1-128 chars)")

    _tags(ix.get('tags'), f"{w}.tags")
    outs = ix.get('outputs')
    if outs in (None, []):
        return errors
    if not outputs:
        return errors + [f"{w}.outputs: only an endpoint's totals have extra outputs"]
    if not isinstance(outs, list) or len(outs) > 16:
        return errors + [f"{w}.outputs: a list of at most 16 outputs"]
    ids = set()
    for i, o in enumerate(outs):
        ow = f"{w}.outputs[{i}]"
        if not isinstance(o, dict):
            errors.append(f"{ow}: must be an object")
            continue
        oid = str(o.get('id', '')).strip()
        if not _re.match(r'^[a-z0-9][a-z0-9_-]{0,31}$', oid):
            errors.append(f"{ow}.id: a-z 0-9 - _ (1-32 chars)")
        elif oid in ids:
            errors.append(f"{ow}.id: '{oid}' is used twice")
        ids.add(oid)
        if not _INFLUX_NAME_RE.match(str(o.get('measurement', ''))):
            errors.append(f"{ow}.measurement: required, starts with a letter "
                          "(letters, digits, _ . -)")
        b = str(o.get('bucket', '') or '')
        if b and not _re.match(r'^[A-Za-z0-9][A-Za-z0-9_.\-${}]{0,63}$', b):
            errors.append(f"{ow}.bucket: invalid name")
        if str(o.get('mode', 'changed') or 'changed') not in ('changed', 'every'):
            errors.append(f"{ow}.mode: changed or every")
        _tags(o.get('tags'), f"{ow}.tags")
        fl = o.get('fields')
        if not isinstance(fl, list) or not fl or len(fl) > 200:
            errors.append(f"{ow}.fields: 1-200 fields")
            continue
        names = set()
        for j, f in enumerate(fl):
            fw = f"{ow}.fields[{j}]"
            if not isinstance(f, dict):
                errors.append(f"{fw}: must be an object")
                continue
            n = str(f.get('name', ''))
            if not _INFLUX_NAME_RE.match(n) or n in _INFLUX_RESERVED:
                errors.append(f"{fw}.name: starts with a letter (letters, digits, _ . -)")
            elif n in names:
                errors.append(f"{fw}.name: '{n}' is written twice")
            names.add(n)
            if not str(f.get('source', '')).strip():
                errors.append(f"{fw}.source: which total feeds it")
            try:
                sc = float(f.get('scale', 1) if f.get('scale', 1) not in (None, '') else 1)
                if sc == 0 or sc != sc or sc in (float('inf'), float('-inf')):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"{fw}.scale: a non-zero number")
    return errors


# ── channel override shape (shared by command + endpoint, ADR-0040 D2c) ───────
CHANNEL_OVERRIDE_KEYS = ('min_interval_s', 'min_on_s', 'min_off_s', 'max_starts_per_h', 'max_age_s')


def channel_override_errors(value) -> Dict[str, str]:
    """Per-field errors for a channel-override block — the operator-editable
    pace/debounce keys a device may set. The template's own `combine` is not one
    of them. Shared by the command service and the endpoint validator so one
    block is judged the same wherever it is written (ADR-0040)."""
    if not isinstance(value, dict) or not value:
        return {'channel': f"an object of {', '.join(CHANNEL_OVERRIDE_KEYS)} (null removes one)"}
    errs: Dict[str, str] = {}
    for k, v in value.items():
        if k not in CHANNEL_OVERRIDE_KEYS:
            errs[k] = (f"not a device's to set ({', '.join(CHANNEL_OVERRIDE_KEYS)}; "
                       "`combine` is the template's)")
        elif v is None:
            continue
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 86400:
            errs[k] = '0..86400'
        elif k == 'max_starts_per_h' and not float(v).is_integer():
            errs[k] = 'a whole number'
        elif k == 'max_age_s' and v < 1:
            errs[k] = '1..86400 (0 would expire every command)'
    return errs


def override_cid(key: str, defs: Dict[str, Any], ids: Dict[str, str]) -> Optional[str]:
    """The channel id a channel-override key names: a declared channel name wins,
    otherwise the id assigned to that member command (ADR-0040)."""
    declared = {d.channel for d in defs.values() if d.channel}
    return key if key in declared else ids.get(key)
