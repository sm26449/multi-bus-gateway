# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Core: the gateway's own foundations — configuration (load/save, dataclasses,
protocol constants), config store, the audit trail and event log, gateway role
and identity, the safe expression evaluator, clock/sun, text checks, redaction
and the tombstone store. Sits just above codec; imports nothing higher."""
