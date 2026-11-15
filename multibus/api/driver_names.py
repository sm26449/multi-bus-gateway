# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The register names the running drivers of a device poll — read off the live
client (and each source of a multi-source device), not the template."""
from typing import Optional


def _driver_reg_names(client) -> Optional[set]:
    """The register names the running drivers of a device poll (every source
    of a multi-source device), or None when no driver lists them."""
    parts = getattr(client, 'parts', None)
    drivers = [d for _s, d in parts] if parts else [client]
    names, found = set(), False
    for drv in drivers:
        regs = getattr(drv, 'registers', None)
        if regs is None:
            continue
        found = True
        names |= {r.name for r in list(regs)}
    return names if found else None
