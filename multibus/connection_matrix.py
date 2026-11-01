# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The matrix's other half (ADR-0031 §5, L3.2): what is never valid, and
the defaults.

``connection_spec.MATRIX`` lists the cells an editor may offer. A form that
keeps an unavailable option visible (``aria-disabled``) needs its reason too:
:data:`INVALID` lists the combinations no device may have, each with a code
and the words a save refuses it with. It is disjoint from the matrix: a cell
the matrix marks ``not_yet`` (Seplos V2 through a bridge record, CAN over a
bridge) carries its own ``note`` there and is not repeated here. :func:`defaults` gives, per protocol,
every advanced connection key with its effective default and where that
default comes from (``code``, ``gateway``, ``template`` or ``none``), so a form
shows each value with its source (FINDINGS L3-F7) and nothing is hidden.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Dict, List

from .connection_spec import PUBLIC_KEYS


def _cell(code, text, port, reach, protocol, framing=None, mode='poll', udp=False) -> Dict[str, Any]:
    keys = [k for k in PUBLIC_KEYS if k not in ('writable', 'dangling')]
    vals = dict(port=port, reach=reach, protocol=protocol, framing=framing, udp=udp, mode=mode)
    return {**{k: vals[k] for k in keys}, 'code': code, 'text': text}


_LISTEN_ASCII = ("listening decodes Modbus RTU frames: an ASCII bus is read by polling it, "
                 "or listened to only once its master speaks RTU")
_LISTEN_MBAP = ("a Modbus TCP server only answers the questions asked of it: there is no bus traffic "
                "to hear. Listen on the serial side: a port here or a transparent bridge")
_LISTEN_GATEWAY = ("this bridge is a Modbus TCP gateway: it only answers questions, it does not pass the "
                   "bus's traffic on. Listening needs a transparent bridge or the MBG serial bridge")
_CAN_POLL = ("CAN is read listen-only: the gateway opens the interface for reception and has no "
             "transmit path")

INVALID: Dict[str, List[Dict[str, Any]]] = {
    'device': [
        _cell('listen_ascii', _LISTEN_ASCII, 'serial', 'here', 'modbus', 'ascii', 'listen'),
        _cell('listen_ascii', _LISTEN_ASCII, 'serial', 'address', 'modbus', 'ascii', 'listen'),
        _cell('listen_ascii', _LISTEN_ASCII, 'serial', 'bridge', 'modbus', 'ascii', 'listen'),
        _cell('listen_mbap', _LISTEN_MBAP, 'ethernet', 'address', 'modbus', 'mbap', 'listen'),
        _cell('listen_mbap', _LISTEN_MBAP, 'ethernet', 'address', 'modbus', 'mbap', 'listen', udp=True),
        _cell('listen_gateway_bridge', _LISTEN_GATEWAY, 'ethernet', 'bridge', 'modbus', 'mbap', 'listen'),
        _cell('can_poll', _CAN_POLL, 'can', 'socketcan', 'can_signals', None, 'poll'),
        _cell('can_poll', _CAN_POLL, 'can', 'bridge', 'can_signals', None, 'poll'),
    ],
}


def _d(value, source='code', note='') -> Dict[str, Any]:
    out = {'value': value, 'source': source}
    if note:
        out['note'] = note
    return out


# the advanced Modbus keys a device may set (ModbusConfig), in form order
_MODBUS_KEYS = ('port', 'unit_id', 'timeout', 'retry_attempts', 'retry_delay', 'stale_after_s', 'max_gap',
                'startup_jitter_s', 'illegal_registers', 'drop_all_zero', 'serialize_endpoint',
                'share_transport', 'max_connections', 'endpoint_wait_s', 'endpoint_min_gap_s',
                'connect_settle_s', 'baudrate', 'parity', 'bytesize', 'stopbits')
_LINE_KEYS = ('baudrate', 'parity', 'bytesize', 'stopbits')


def _modbus_code_defaults() -> Dict[str, Any]:
    from .config import ModbusConfig
    out = {}
    for f in dataclasses.fields(ModbusConfig):
        if f.default is not dataclasses.MISSING:
            out[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:          # type: ignore[misc]
            out[f.name] = f.default_factory()                       # type: ignore[misc]
    return out


def defaults(config=None) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Per protocol axis, each advanced key's effective default and source."""
    from .conn_advanced import ADVANCED
    code = _modbus_code_defaults()
    jitter = float(getattr(getattr(config, 'modbus', None), 'startup_jitter_s', code['startup_jitter_s']) or 0.0)
    modbus = {}
    for k in _MODBUS_KEYS:
        if k == 'startup_jitter_s':
            modbus[k] = _d(jitter, 'gateway', "the gateway's modbus.startup_jitter_s")
        elif k == 'connect_settle_s':
            modbus[k] = _d(None, 'template', "the template's protocol.connect_settle_s, else 0 (no pause)")
        elif k == 'unit_id':
            modbus[k] = _d(code[k], 'code', "a template's protocol.default_unit_id is offered first")
        elif k in _LINE_KEYS:
            modbus[k] = _d(code[k], 'code', "a template's protocol.line, or the bus's line when it has devices")
        elif k in ADVANCED:
            # the advanced keys: one source of their defaults and ranges
            # (conn_advanced.py), the one a save checks against (L3-F7)
            dflt = ADVANCED[k][3]
            modbus[k] = _d(dflt)
        else:
            modbus[k] = _d(code[k])
        if k in ADVANCED and ADVANCED[k][1] is not None:
            modbus[k].update(min=ADVANCED[k][1], max=ADVANCED[k][2])
    seplos = {k: modbus[k] for k in ('unit_id', 'timeout', 'stale_after_s') + _LINE_KEYS}
    seplos['mode'] = _d(None, 'none', "never defaulted: choose Master (poll) or Listen-only")
    from .mqtt_input import MqttInputClient
    return {
        'modbus': modbus,
        'seplos_v2': seplos,
        'can_signals': {
            'carrier': _d('socketcan'),
            'channel': _d('can0'),
            'bitrate': _d(None, 'template', "the template's protocol.bitrate; the host sets the interface's"),
            'stale_after_s': _d(30),
        },
        'json_http': {'timeout': _d(8), 'verify_tls': _d(True)},
        'json_mqtt': {'port': _d(1883), 'tls': _d(False),
                      'stale_after_s': _d(int(MqttInputClient.stale_after_s))},
    }
