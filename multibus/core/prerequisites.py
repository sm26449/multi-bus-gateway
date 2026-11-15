# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Guided prerequisites (ADR-0046): an action declares the settings it needs; when one is unmet,
the endpoint answers 409 with a ``prerequisites`` payload the UI renders as one consistent
"need X — [Enable now] / [Later — menu]" dialog. Nothing is enabled silently.

Pure and declarative: a ``Prereq`` is ``{id, title, where, met, fix}``. ``fix`` (optional) is the
call the UI makes on "Enable now" — ``{method, url, body?}`` — pointing at an existing, audited
endpoint; the UI then retries the original action. ``require(prereqs)`` raises the 409.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Prereq:
    """One precondition of an action. ``title``/``where`` are English fallbacks — the UI may
    localize by ``id`` (``prereq.<id>.title`` …) and fall back to these."""
    id: str
    title: str
    where: str
    met: bool
    fix: Optional[Dict[str, Any]] = None       # {method, url, body?} — advisory, for API-drivability
    params: Optional[Dict[str, Any]] = None    # what the UI needs to build its OWN known fix call

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"id": self.id, "title": self.title, "where": self.where, "met": self.met}
        if self.fix is not None:
            d["fix"] = self.fix
        if self.params is not None:
            d["params"] = self.params
        return d


# ── the declared prerequisites (v1: config-only, no privilege) ───────────────
def allow_writes(config: Any) -> Prereq:
    """Hardware writes are armed by the ``security.allow_writes`` master switch."""
    sec = getattr(config, "security", None)
    return Prereq(
        id="allow_writes", title="Modbus writes", where="Settings → Security",
        met=bool(getattr(sec, "allow_writes", False)),
        fix={"method": "POST", "url": "/api/config/security", "body": {"allow_writes": True}})


def allow_write_entities(config: Any) -> Prereq:
    """Writes that travel over MQTT need ``mqtt.allow_write_entities`` as well."""
    mqtt = getattr(config, "mqtt", None)
    return Prereq(
        id="allow_write_entities", title="Writes over MQTT", where="Settings → Security",
        met=bool(getattr(mqtt, "allow_write_entities", False)),
        fix={"method": "POST", "url": "/api/config/security", "body": {"allow_write_entities": True}})


def can_transmit(channel: str, interface: Optional[Dict[str, Any]]) -> Prereq:
    """A Virtual BMS can transmit on a CAN port only when the interface is OUT of listen-only and
    UP. ``interface`` is its entry from ``can_interfaces.interfaces()`` (``None`` if not present).
    The fix switches it to Transmit (and up) via ``POST /api/can-interfaces/<channel>`` — needs
    MBG to hold ``CAP_NET_ADMIN`` (ADR-0046 L2)."""
    itf = interface or {}
    met = bool(itf) and itf.get("listen_only") is not True and itf.get("up") is not False
    return Prereq(
        id="can_transmit", title=f"Transmit on {channel}", where="Settings → CAN", met=met,
        fix={"method": "POST", "url": f"/api/can-interfaces/{channel}",
             "body": {"mode": "transmit", "up": True}},
        params={"channel": channel})


def can_bitrate(channel: str, interface: Optional[Dict[str, Any]],
                expected: Optional[int]) -> Prereq:
    """A Virtual BMS must transmit at the bitrate its profile expects (ADR-0045 §6). ``interface``
    is the port's entry from ``can_interfaces.interfaces()``; ``expected`` is the profile's
    ``protocol.bitrate``. When either is unknown — a down port reports no bitrate, a profile
    declares none — there is nothing to check, so the prerequisite is met (the save-time check
    already covers every up-and-reporting port; this closes the gap for a port that was down at
    save and brought up at the wrong bitrate later). The fix sets the port's bitrate and brings it
    up via ``POST /api/can-interfaces/<channel>`` — needs ``CAP_NET_ADMIN`` (ADR-0046 L2)."""
    actual = (interface or {}).get("bitrate")
    met = (not expected) or (not actual) or (actual == expected)
    return Prereq(
        id="can_bitrate", title=f"{channel} at {expected} bit/s", where="Settings → CAN", met=met,
        fix={"method": "POST", "url": f"/api/can-interfaces/{channel}",
             "body": {"bitrate": expected, "up": True}},
        params={"channel": channel, "bitrate": expected})


# ── the mechanism ────────────────────────────────────────────────────────────
def unmet(prereqs: List[Prereq]) -> List[Prereq]:
    return [p for p in prereqs if not p.met]


def detail(prereqs: List[Prereq]) -> Dict[str, Any]:
    """The 409 body for a set of (unmet) prerequisites. The web layer raises the HTTPException —
    this module stays free of FastAPI (the import-linter keeps core off the web stack)."""
    return {"prerequisites": [p.to_dict() for p in prereqs]}
