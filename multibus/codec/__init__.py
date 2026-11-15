# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Codec: the lowest layer — raw Modbus words / CAN frames <-> typed engineering
values. Register parsing, value decode/corrections, encoding, monotonic-counter
hygiene and CAN-signal schema. Depends on nothing else in multibus."""
