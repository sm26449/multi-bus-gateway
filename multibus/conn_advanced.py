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
"""The advanced keys of a device's connection: their ranges and defaults, in
one place (L3-F7).

``config._modbus_from_conn`` reads these keys with the defaults below; a
save checks them here. A value kept as stored is never judged again (a value
written by hand before the check existed never blocks an edit of something
else — ADR-0006): only a key the save sets to a new value is checked.

``DEFAULTS`` is what the API serves beside the form (``defaults``).
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

# key → (kind, low, high, default, what it is). kind: int | float | bool | addresses
ADVANCED: Dict[str, tuple] = {
    'retry_attempts': ('int', 0, 10, 3, "attempts per read before the cycle gives up"),
    'retry_delay': ('float', 0.0, 60.0, 1.0, "seconds between two attempts"),
    'stale_after_s': ('int', 0, 86400, 30, "no good read for this long: the data is stale (0 = never)"),
    'max_gap': ('int', 0, 125, 10, "registers a merged read may bridge (0 = read each run alone)"),
    'illegal_registers': ('addresses', 0, 0xFFFF, [], "addresses the device refuses: never read in a batch"),
    'drop_all_zero': ('bool', None, None, False, "a block of all zeros is dropped as a bad answer"),
    'startup_jitter_s': ('float', 0.0, 3600.0, None, "random wait before a group's first read "
                         "(0 = off; empty = the gateway's)"),
    'serialize_endpoint': ('bool', None, None, True, "one transaction at a time on this host:port"),
    'share_transport': ('bool', None, None, True, "one socket with every device behind the same access point"),
    'max_connections': ('int', 1, 8, 1, "sockets this access point is worth"),
    'endpoint_wait_s': ('float', 0.1, 600.0, 10.0, "how long a read may wait for its turn"),
    'endpoint_min_gap_s': ('float', 0.0, 60.0, 0.0, "seconds between two transactions on the access point"),
    'connect_settle_s': ('float', 0.0, 30.0, None, "seconds to wait after connecting (empty = the template's)"),
}

DEFAULTS: Dict[str, Any] = {k: v[3] for k, v in ADVANCED.items()}

# empty = someone else's value (the template's settle, the gateway's jitter)
NULLABLE = ('connect_settle_s', 'startup_jitter_s')

# CAN carries its own stale_after_s rule (can_client.connection_errors)
_OWN_RULES = {'can': ('stale_after_s',)}

MAX_ADDRESSES = 1000


def _range_text(kind: str, lo, hi) -> str:
    if kind == 'int':
        return f"a whole number, {lo}..{hi}"
    return f"a number, {lo:g}..{hi:g}"


def _number(v) -> Optional[float]:
    """A number given as a number or as its text; None for anything else
    (a boolean, NaN, inf, words)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        try:
            v = float(v.strip())
        except ValueError:
            return None
    if not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def key_error(key: str, value: Any) -> Optional[str]:
    """What is wrong with one advanced key's value, worded for the field; None
    when it is fine (or the key is not an advanced one)."""
    spec = ADVANCED.get(key)
    if spec is None:
        return None
    kind, lo, hi, _default, _what = spec
    field = f"connection.{key}"
    if key in NULLABLE and value in (None, ''):
        return None                                   # the template's / the gateway's
    if kind == 'bool':
        return None if isinstance(value, bool) else f"{field}: true or false"
    if kind == 'addresses':
        if not isinstance(value, list) or len(value) > MAX_ADDRESSES:
            return f"{field}: a list of register addresses (up to {MAX_ADDRESSES})"
        for a in value:
            try:
                n = int(a, 0) if isinstance(a, str) else (None if isinstance(a, bool) else int(a))
            except (TypeError, ValueError):
                n = None
            if n is None or (isinstance(a, float) and not a.is_integer()) or not (lo <= n <= hi):
                return f"{field}: {a!r} is not a register address (0..65535, decimal or 0x…)"
        return None
    n = _number(value)
    if n is None or not (lo <= n <= hi) or (kind == 'int' and not n.is_integer()):
        return f"{field}: {_range_text(kind, lo, hi)}"
    return None


def errors(conn: Dict[str, Any], stored: Optional[Dict[str, Any]] = None,
           protocol: str = '') -> List[str]:
    """Every advanced key the save sets to a value other than the stored one,
    checked; a list of ``connection.<key>: …`` errors."""
    if not isinstance(conn, dict):
        return []
    stored = stored if isinstance(stored, dict) else {}
    skip = set()
    for fam, keys in _OWN_RULES.items():
        if protocol.startswith(fam):
            skip |= set(keys)
    out = []
    for key in ADVANCED:
        if key in skip or key not in conn or conn[key] is None:
            continue                       # null = unset: the default (a GET echoes it so)
        if key in stored and conn[key] == stored[key]:
            continue
        e = key_error(key, conn[key])
        if e:
            out.append(e)
    return out


def described() -> List[Dict[str, Any]]:
    """The keys as the API describes them: {key, kind, min, max, default, help}."""
    return [{'key': k, 'kind': kind, 'min': lo, 'max': hi, 'default': d, 'help': what}
            for k, (kind, lo, hi, d, what) in ADVANCED.items()]
