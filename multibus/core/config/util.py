# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Small config helpers with no config dependency: register-identity validation,
the type-span table, the version tuple, and the 0600 secret-file copier. A true
leaf of the config package."""
import logging
import os
import re
import shutil
from typing import Dict, List

from multibus.core import catalog_port

logger = logging.getLogger("multibus.core.config")


# register spans (in 16-bit words) per data type — for overlap validation
_TYPE_SPANS = {'int16': 1, 'uint16': 1, 'sm16': 1, 'bool': 1,
               'int32': 2, 'uint32': 2, 'sm32': 2, 'float': 2, 'float32': 2,
               'int64': 4, 'uint64': 4, 'double': 4, 'float64': 4}


def validate_register_identity(registers: List[Dict]) -> None:
    """Reject identity collisions in one device's selection (audit DP-9).

    Nothing used to stop: (a) two registers at the same (register_type,
    address) — distinct Modbus address spaces collapse onto one store key,
    last-write-wins and the other register silently vanishes; (b) span
    overlaps (a float@100 plus an int16@101 decoding the float's low word as
    its own reading); (c) duplicate names — nondeterministic vmeter binding
    plus a shared MQTT topic and Influx series (the class caught LIVE in the
    per-phase energy leaves at Migration B). Template validation covered
    templates only; CSV/YAML/manual edits reached the store unchecked.
    Raises ValueError naming the exact collision.

    Span OVERLAPS are only WARNED about: live production maps legitimately
    read overlapping windows (fronius_rtu serves an int32@10 alongside a
    uint16@11), and on HTTP/MQTT devices the address is a synthetic index
    where spans mean nothing — a hard reject would break healthy configs."""
    seen_addr: Dict[tuple, str] = {}
    seen_name: Dict[str, int] = {}
    spans: Dict[str, List[tuple]] = {}
    for r in registers:
        name = str(r.get('name') or '')
        addr = int(r.get('address', -1))
        rtype = str(r.get('register_type') or 'holding')
        key = (rtype, addr)
        if key in seen_addr:
            raise ValueError(f"duplicate address {addr} ({rtype}): "
                             f"{seen_addr[key]!r} and {name!r} would overwrite "
                             f"each other in the live store")
        seen_addr[key] = name
        if name:
            if name in seen_name:
                raise ValueError(
                    f"duplicate register name {name!r} (addresses "
                    f"{seen_name[name]} and {addr}): the name drives the "
                    f"vmeter binding, the MQTT topic and the Influx series — "
                    f"they must be unique per device")
            seen_name[name] = addr
        # span overlap is a MODBUS concept (a multi-word read walking into its
        # neighbour); a register sourced from an MQTT topic or a JSON path has
        # a synthetic address, and consecutive ints are the NORM there — the
        # check would print one bogus warning per register at every boot
        if r.get('topic') or r.get('json_path') or r.get('can_id') not in (None, ''):
            continue
        dt = str(r.get('data_type') or 'uint16').lower()
        span = (max(1, int(r.get('length') or 1)) if dt == 'string'
                else _TYPE_SPANS.get(dt, 2))
        spans.setdefault(rtype, []).append((addr, addr + span, name))
    for rtype, rows in spans.items():
        rows.sort()
        for (s1, e1, n1), (s2, e2, n2) in zip(rows, rows[1:]):
            if s2 < e1:
                logger.warning(
                    "register span overlap (%s): %r @%s (ends %s) overlaps "
                    "%r @%s — fine if intentional (overlapping reads are "
                    "legal); check it if %r decodes garbage",
                    rtype, n1, s1, e1, n2, s2, n2)
    # Canonical-name unit contract (WARNING only — loosening-only rule: a
    # mismatch is legal but almost always a scale mistake). A canonical name
    # promises the canonical unit to every consumer; a kWh value under an
    # energy_* name is a silent 1000x error the moment the register feeds a
    # vmeter, a fallback twin or a cross-device dashboard.
    for r in registers:
        name = str(r.get('name') or '')
        cu = catalog_port.canonical_unit_for(name)
        unit = str(r.get('unit') or '').strip()
        if cu and unit and unit != cu:
            logger.warning(
                "register %r declares unit %r but the canonical unit for "
                "this name is %r — adjust the scale to deliver %s (e.g. a "
                "native kWh map needs scale/1000), or rename the register "
                "if it truly measures something else", name, unit, cu, cu)


def _devices_in(doc) -> int:
    if not isinstance(doc, dict):
        return 0
    d = doc.get('devices')
    return len(d) if isinstance(d, list) else 0


def _version_tuple(v: str) -> tuple:
    """Numeric-compare form of a version string; unparsable parts count as 0
    (fail-open: a malformed stamp must never block a config load)."""
    out = []
    for part in str(v).split("."):
        digits = re.match(r"\d+", part)
        out.append(int(digits.group()) if digits else 0)
    return tuple(out)

def _copy_private(src, dst) -> None:
    """Copy a secret-bearing file (config.yaml carries broker/Influx
    credentials) so the copy is 0600 from its first byte, like config.yaml
    itself — shutil.copyfile created the .good/.bad copies at the umask
    (3.80.0)."""
    src, dst = str(src), str(dst)
    tmp = dst + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
        shutil.copyfileobj(inp, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, dst)
