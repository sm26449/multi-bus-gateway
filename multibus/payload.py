# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Small readers for request bodies, shared by every route."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import HTTPException

_REQUIRED = object()


def flag(payload: Dict[str, Any], key: str, default: Any = _REQUIRED) -> bool:
    """A yes/no field as a JSON true/false — nothing else. ``bool("false")``
    is True in Python: a client sending the string once ENABLED what it
    meant to disable (a command, a rule). A missing key takes ``default``;
    with no default it is refused."""
    if key not in payload or payload[key] is None:
        if default is _REQUIRED:
            raise HTTPException(status_code=422, detail={
                "errors": [f"{key}: say true or false"], "fields": {key: "true or false"}})
        return default
    v = payload[key]
    if isinstance(v, bool):
        return v
    raise HTTPException(status_code=422, detail={
        "errors": [f"{key}: true or false (got {v!r})"], "fields": {key: "true or false"}})
