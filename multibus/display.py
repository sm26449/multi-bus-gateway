# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
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
"""How a unit is presented — the template's `display` block, in one place.

The block is read LIVE from the template (never seeded into a unit), so an
improvement reaches every existing unit at once. Every view — the
installation page, the fleet, the dashboard — asks here instead of keeping
its own list of what a kind of device shows.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

SEVERITIES = ("warning", "danger")


def template_id_of(dev) -> str:
    """The template a device is read with: its own, else its first source's."""
    return getattr(dev, "template", "") or next(
        (getattr(sx, "template", "") for sx in (getattr(dev, "sources", None) or [])
         if getattr(sx, "template", "")), "")


def display_of(template_registry, dev) -> Dict[str, Any]:
    """The `display` block of a device's template — {} when it declares none."""
    tid = template_id_of(dev)
    tpl = template_registry.get(tid) if (template_registry and tid) else None
    return dict(getattr(tpl, "display", None) or {})


def _active(value: Any) -> bool:
    """A declared alarm field is ACTIVE when it says something: a non-zero
    number, a true flag, a non-empty word other than the usual "nothing"."""
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() not in ("", "0", "off", "none", "normal", "ok", "false")


def active_alarms(display: Dict[str, Any], store_entries: Iterable[Dict[str, Any]],
                  fresh: Optional[callable] = None) -> Dict[str, Any]:
    """Count the template-declared alarm fields that are active right now.

    ``display.alarms`` = ``[{field, severity}]`` — a BMS's alarm/protection/
    failure counts, an inverter's fault word. Explicit per-register thresholds
    are counted elsewhere; this is what the DEVICE says is wrong."""
    decl = [a for a in (display.get("alarms") or [])
            if isinstance(a, dict) and a.get("field")]
    out: Dict[str, Any] = {"danger": 0, "warning": 0, "active": []}
    if not decl:
        return out
    by_name = {}
    for e in store_entries:
        n = e.get("name")
        if n and (fresh is None or fresh(e)):
            by_name[n] = e
    for a in decl:
        e = by_name.get(a["field"])
        if e is None or not _active(e.get("value")):
            continue
        sev = a.get("severity") if a.get("severity") in SEVERITIES else "warning"
        out[sev] += 1
        out["active"].append({"field": a["field"], "label": e.get("label") or a["field"],
                              "severity": sev, "value": e.get("value")})
    return out
