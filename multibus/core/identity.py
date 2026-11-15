# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""How new devices are named: per gateway, or as before (ADR-0026 §3).

Two gateways on one broker and one InfluxDB each had a device `deye`: both
published to `mbg/devices/deye`, wrote the bucket `deye` and announced the
HA unique_id `mbg_dev_deye_*` (FINDINGS ADD-11). A fresh install names its
devices after its own `gateway.id`; an existing one keeps `legacy` until an
operator changes it. Stored prefixes and buckets never move — only the
defaults for devices created afterwards, and the HA ids, follow the scope.
"""
from __future__ import annotations

import re
import secrets
from typing import Optional

SCOPES = ('gateway', 'legacy')
GATEWAY_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,31}$')
# the defaults a scope gives a new device (an explicit pattern wins)
DEFAULT_PATTERNS = {
    'legacy': {'topic': 'mbg/devices/{device}', 'bucket': '{device}'},
    'gateway': {'topic': 'mbg/{gateway}/devices/{device}', 'bucket': '{gateway}-{device}'},
}
# a pattern equal to one of these is a default, not a choice: every save
# before the scope existed wrote the legacy ones out, and a Settings form
# loaded before a scope switch sends the other scope's back (review M2)
BUILTIN_TOPIC_PATTERNS = tuple(p['topic'] for p in DEFAULT_PATTERNS.values())
BUILTIN_BUCKET_PATTERNS = tuple(p['bucket'] for p in DEFAULT_PATTERNS.values())
# ids that would put the scoped topics under the gateway's own root
RESERVED_IDS = ('gateway',)


def new_gateway_id() -> str:
    """`mbg-` and 6 hex characters: unique enough on one broker."""
    return f"mbg-{secrets.token_hex(3)}"


def gateway_id_error(value) -> Optional[str]:
    if not isinstance(value, str) or not GATEWAY_ID_RE.match(value):
        return "lowercase letters, digits and - (2-32 characters, starts with a letter or digit)"
    if value in RESERVED_IDS:
        return f"'{value}' is reserved (mbg/{value}/… is the gateway's own root) — choose another"
    return None


def fill(pattern: str, device_id: str, gateway_id: str) -> str:
    """A pattern with its placeholders filled ({device}, {device_id}, {gateway})."""
    return (str(pattern).replace('{device}', device_id).replace('{device_id}', device_id)
            .replace('{gateway}', gateway_id))


def pattern_error(pattern: str, gateway_id: str) -> Optional[str]:
    """Why a default pattern cannot be used now, or None."""
    if '{gateway}' in str(pattern or '') and not gateway_id:
        return "{gateway} needs a gateway id — set it first (Settings → General)"
    return None


def legacy_node(device_id: str) -> str:
    """The Home Assistant node of a device that stores none: every device
    created before the scope existed, and every one created under `legacy`."""
    return f"mbg_dev_{device_id}"


def new_node(device_id: str, scope: str, gateway_id: str) -> Optional[str]:
    """The Home Assistant node a NEW device stores, next to its topic prefix
    (review M1): `mbg_<gateway>_dev_<id>` under `gateway`; None under
    `legacy` (nothing stored: the legacy node). Stored once, it never follows
    a later change of the id or the scope. ``device_id`` may be a pattern
    (`${device_id}`, an installation's units)."""
    if scope == 'gateway' and gateway_id:
        return f"mbg_{gateway_id}_dev_{device_id}"
    return None
