# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Core-owned port for the ``devices`` / ``transports`` operations ``core.config``
needs (ADR-0034; .importlinter #4-8).

``core.config`` sits below ``devices`` and ``transports``, so it must not import
them. It used to, to parse meter links, patch a device's output blocks, and
configure the serial-bus guard from the raw bridge list. Those are the calls
this port forwards; the implementations are registered by
``multibus/devices/_host_port_impl.py`` (a ``devices``-layer module, which may
reach ``transports`` downward), wired at app startup, in tests, and in the
migrate script — exactly like ``catalog_port``.

Pure forwarding: ``meter_links``/``device_patch``/``bridge_guard``/``bridges``
keep all their logic and state; this only inverts the import direction, so
behaviour is identical (pinned by tests/characterization/).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

_IMPL: Dict[str, Callable] = {}


def register(**impls: Callable) -> None:
    """Install the devices/transports implementations. Idempotent."""
    _IMPL.update(impls)


def _get(name: str) -> Callable:
    impl = _IMPL.get(name)
    if impl is None:
        # Lazy bootstrap on first use (see multibus.core.catalog_port for the
        # rationale). This import is the one intentional edge up into devices
        # (allowlisted in .importlinter) — a wiring bootstrap, not a logic
        # dependency; config.py stays devices/transports-free.
        from multibus.devices import _host_port_impl
        _host_port_impl.register()
        impl = _IMPL.get(name)
    if impl is None:
        raise RuntimeError(
            f"multibus.core.host_port.{name} is not provided by the host "
            "registration (multibus.devices._host_port_impl.register)")
    return impl


# ── meter links (devices.meter_links) ────────────────────────────────────────
def meter_parse_device(d: Dict, did: str, ignored: Dict) -> Any:
    return _get("meter_parse_device")(d, did, ignored)


def meter_parse_options(raw: Any, did: str, ignored: Dict) -> Any:
    return _get("meter_parse_options")(raw, did, ignored)


def meter_stored_form(links: Any) -> Tuple[Any, Any]:
    return _get("meter_stored_form")(links)


# ── device output patches (devices.device_patch) ─────────────────────────────
def apply_http_output(raw: Dict, enabled: bool) -> None:
    _get("apply_http_output")(raw, enabled)


def apply_sinks(raw: Dict, **kw: Any) -> None:
    _get("apply_sinks")(raw, **kw)


def apply_publishing(target: Dict, **kw: Any) -> None:
    _get("apply_publishing")(target, **kw)


def apply_opcua(raw: Dict, block: Dict) -> None:
    _get("apply_opcua")(raw, block)


def apply_rest_push(raw: Dict, cfg: Dict) -> None:
    _get("apply_rest_push")(raw, cfg)


# ── serial bridges (transports.serial.{bridge_guard,bridges}) ─────────────────
def bridge_guard_configure(raw_bridges: List[Dict], bridge_types: Any) -> None:
    _get("bridge_guard_configure")(raw_bridges, bridge_types)


def bridge_guard_set_recorder(fn: Callable) -> None:
    _get("bridge_guard_set_recorder")(fn)


def bridge_types() -> Any:
    return _get("bridge_types")()


def bridge_resolve_connection(conn: Dict, bridges: List[Dict], *a: Any, **k: Any) -> Any:
    return _get("bridge_resolve_connection")(conn, bridges, *a, **k)
