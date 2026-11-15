# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Register the catalog implementations into the core catalog port (ADR-0034).

The ``catalog`` layer may import ``core`` (downward), so it supplies the
implementations ``core.catalog_port`` forwards to. Call ``register()`` once at
startup — the app does it in ``api`` (beside the user-field load) and the test
suite does it in ``tests/conftest.py``. Idempotent, so calling it more than once
is harmless. Imports are function-local and run only when ``register()`` is
called (never while this module is itself mid-import), so there is no
catalog-package import cycle.
"""
from __future__ import annotations


def register() -> None:
    from multibus.core import catalog_port
    from multibus.catalog.canonical_fields import canonical_unit_for
    from multibus.catalog.device_kind import OVERRIDABLE, normalize
    from multibus.catalog.device_types import is_user_type, set_user_types, type_ids
    from multibus.catalog import attribute_catalog as _ac

    catalog_port.register(
        canonical_unit_for=canonical_unit_for,
        kind_normalize=normalize,
        overridable_kinds=lambda: OVERRIDABLE,
        is_user_type=is_user_type,
        type_ids=type_ids,
        set_user_types=set_user_types,
        attribute_catalog=lambda: _ac.CATALOG,
    )
