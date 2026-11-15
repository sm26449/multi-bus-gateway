# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Register the devices/transports implementations into the core host port
(ADR-0034). This module lives in the ``devices`` layer, which may import
``transports`` (downward) and ``core`` (downward), so it is the one place that
can wire both sides of the port. Call ``register()`` once at startup (the app
does it in ``main.py``; tests via ``tests/conftest.py``). Idempotent; imports
are function-local so they run only on the call, never at module import.
"""
from __future__ import annotations


def register() -> None:
    from multibus.core import host_port
    from multibus.devices import meter_links as _ml
    from multibus.devices import device_patch as _dp
    from multibus.transports.serial import bridge_guard as _bg
    from multibus.transports.serial import bridges as _br

    host_port.register(
        meter_parse_device=_ml.parse_device,
        meter_parse_options=_ml.parse_options,
        meter_stored_form=_ml.stored_form,
        apply_http_output=_dp.apply_http_output,
        apply_sinks=_dp.apply_sinks,
        apply_publishing=_dp.apply_publishing,
        apply_opcua=_dp.apply_opcua,
        apply_rest_push=_dp.apply_rest_push,
        bridge_guard_configure=_bg.configure,
        bridge_guard_set_recorder=_bg.set_recorder,
        bridge_types=lambda: _br.TYPES,
        bridge_resolve_connection=_br.resolve_connection,
    )
