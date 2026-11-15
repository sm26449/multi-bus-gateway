# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Lifecycle: config/state snapshots and rollback, the active/standby mirror,
and what blocks a delete (dependents). Imports lower layers only (ADR-0034)."""
