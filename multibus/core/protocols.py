# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The protocols a device may speak and the register-type machinery — the few
constants and pure helpers that `transports/*`, `codec/*`, `catalog/*` and the
config loader all read. They name no device and hold no state, so they live
below config with no dependency of their own."""
from typing import Any, Dict

_INPUT_REGISTER_ALIASES = {'input', 'inputregister', 'inputregisters', 'ir', 'fc4', '4'}
_COIL_ALIASES = {'coil', 'coils', 'fc1', 'fc5', '1'}
_DISCRETE_ALIASES = {'discrete', 'discreteinput', 'discreteinputs', 'di', 'fc2', '2'}


# Modbus transports, in one place — every check and builder reads these:
#   tcp        Modbus TCP (MBAP)                udp        Modbus TCP frames over UDP
#   rtu        RTU on a serial port here        ascii      ASCII on a serial port here
#   rtu-tcp    RTU frames through a TCP socket  ascii-tcp  ASCII frames through a TCP socket
#   rtu_tap    listen-only (rtu_tap.py)
SERIAL_PROTOCOLS = ('rtu', 'ascii')                 # a serial port on this host
BUS_OVER_TCP_PROTOCOLS = ('rtu-tcp', 'ascii-tcp')   # a serial bus reached through a socket
NETWORK_PROTOCOLS = ('tcp', 'udp') + BUS_OVER_TCP_PROTOCOLS
MODBUS_PROTOCOLS = NETWORK_PROTOCOLS + SERIAL_PROTOCOLS + ('rtu_tap',)
# Buses that are not Modbus, read by their own driver into a fixed virtual
# register map (input table), so templates use the normal register machinery:
#   seplos_v2      Seplos V2 BMS ASCII protocol on a serial port here (seplos_v2.py)
#   seplos_v2-tcp  the same frames through a TCP socket to a serial bridge
# `connection.mode` is poll (the gateway asks) or listen (it never transmits).
# They are read-only: the driver has no write path.
VIRTUAL_MAP_SERIAL = ('seplos_v2',)
VIRTUAL_MAP_OVER_TCP = ('seplos_v2-tcp',)
VIRTUAL_MAP_PROTOCOLS = VIRTUAL_MAP_SERIAL + VIRTUAL_MAP_OVER_TCP
# a protocol's register-map family: a template is written for one of them
VIRTUAL_MAP_FAMILY = {'seplos_v2': 'seplos_v2', 'seplos_v2-tcp': 'seplos_v2'}
# every protocol whose template is a register map read by address
REGISTER_MAP_PROTOCOLS = MODBUS_PROTOCOLS + VIRTUAL_MAP_PROTOCOLS
# nothing can be written through these (they listen, or their driver is read-only)
# CAN (can_client.py): frames from a carrier, listen-only by construction
CAN_PROTOCOLS = ('can',)
READ_ONLY_PROTOCOLS = ('rtu_tap',) + VIRTUAL_MAP_PROTOCOLS + CAN_PROTOCOLS
# how the frames look on the wire (the bus monitor decodes by this)
WIRE_FRAMING = {'tcp': 'tcp', 'udp': 'tcp', 'rtu': 'rtu', 'rtu-tcp': 'rtu', 'rtu_tap': 'rtu',
                'ascii': 'ascii', 'ascii-tcp': 'ascii'}


def _can_kwargs(reg: Dict) -> Dict[str, Any]:
    """A selection row's CAN fields as SelectedRegister keywords ({} for a
    row that is not a CAN signal)."""
    from multibus.codec.can_signal import parse_can_id
    cid = parse_can_id(reg.get('can_id'))
    if cid is None:
        return {}
    out: Dict[str, Any] = {'can_id': cid, 'can_id_extended': bool(reg.get('can_id_extended', False)),
                           'byte_order': str(reg.get('byte_order') or '').lower()}
    for k in ('start_bit', 'bit_length'):
        try:
            out[k] = int(reg[k]) if reg.get(k) is not None else None
        except (TypeError, ValueError):
            out[k] = None
    return out


def protocol_family(protocol: str) -> str:
    """The template family a device protocol reads: 'http', 'mqtt', a
    virtual-map family ('seplos_v2'), else 'modbus'."""
    p = str(protocol or '').lower()
    if p in ('http', 'mqtt', 'none') + CAN_PROTOCOLS:
        return p
    return VIRTUAL_MAP_FAMILY.get(p, 'modbus')


# The live store keys a value by its address — and a Modbus device may hold
# the SAME address in several tables (coil 0, holding 0, input 0 are three
# different things). The key adds a per-table offset: holding keeps its bare
# address (every existing store, URL and dashboard key stays as it was), the
# others move above the 16-bit address space and stay below the calculated
# fields (calc_engine.CALC_ADDR_BASE).
REGISTER_TYPE_KEY_OFFSET = {'holding': 0, 'input': 100_000, 'coil': 200_000, 'discrete': 300_000}


def store_key(address: int, register_type: str = 'holding') -> int:
    """The live-store key of a register: its address in its own table."""
    return int(address) + REGISTER_TYPE_KEY_OFFSET[normalize_register_type(register_type)]


def normalize_register_type(v) -> str:
    """Normalize a register-type to one of holding (FC3) / input (FC4) /
    coil (FC1/FC5) / discrete (FC2). Unknown/empty -> 'holding' (the Modbus
    default and the Janitza's type). Coils are writable; input/discrete are not."""
    s = str(v or '').strip().lower().replace(' ', '').replace('_', '').replace('-', '')
    if s in _COIL_ALIASES:
        return 'coil'
    if s in _DISCRETE_ALIASES:
        return 'discrete'
    if s in _INPUT_REGISTER_ALIASES:
        return 'input'
    return 'holding'
