# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Core-owned port for the few catalog lookups the ``core`` layer needs.

ADR-0034 layering: ``catalog`` sits ABOVE ``core``, so ``core.config`` and
``core.expressions`` must not import it. They used to, at five edges
(.importlinter #1,2,3,9,16): the canonical-unit lookup, device-kind
normalization + the overridable-kind set, the user-device-type predicates, and
the attribute-category catalog. This module is the interface those two modules
call instead; the ``catalog`` layer registers its implementations
(``catalog/_port_impl.py``), wired at app startup (``api``) and in tests
(``tests/conftest.py``).

Pure forwarding: the data and state (user fields, user types) stay in
``catalog`` — this only inverts the import direction. It is also
storage-agnostic: ``canonical_unit_for`` does not care whether the vocabulary
lives in a file today or a database later, so that move stays a change behind
this port rather than a config rewrite.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

_IMPL: Dict[str, Callable] = {}


def register(**impls: Callable) -> None:
    """Install the catalog implementations. Idempotent; the last wins."""
    _IMPL.update(impls)


def _get(name: str) -> Callable:
    impl = _IMPL.get(name)
    if impl is None:
        # Lazy bootstrap: load and register the catalog implementations on first
        # use, so a Config built by ANY entrypoint (app, tool, test) just works
        # without that entrypoint having to wire the port — MBG runs a fixed
        # image main.py over a mounted multibus/, so there is no single startup
        # we can rely on. This import is the one intentional edge up into
        # catalog (allowlisted in .importlinter): a wiring bootstrap, not a
        # logic dependency — config.py / expressions.py stay catalog-free.
        from multibus.catalog import _port_impl
        _port_impl.register()
        impl = _IMPL.get(name)
    if impl is None:
        raise RuntimeError(
            f"multibus.core.catalog_port.{name} is not provided by the catalog "
            "registration (multibus.catalog._port_impl.register)")
    return impl


def canonical_unit_for(name: str) -> Optional[str]:
    return _get("canonical_unit_for")(name)


def kind_normalize(value: Any) -> Optional[str]:
    return _get("kind_normalize")(value)


def overridable_kinds() -> tuple:
    return _get("overridable_kinds")()


def is_user_type(value: Any) -> bool:
    return _get("is_user_type")(value)


def type_ids(include_other: bool = True) -> List[str]:
    return _get("type_ids")(include_other)


def set_user_types(raw: Any) -> Dict[str, str]:
    return _get("set_user_types")(raw)


def attribute_catalog() -> Dict[str, Any]:
    return _get("attribute_catalog")()
