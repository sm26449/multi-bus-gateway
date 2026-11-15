# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Modbus TCP/UDP/RTU: the client (transports, poller, errors, writes), the
frame-level bus trace, connect-time setup writes, and the listen-only RTU tap.
May use the serial bus model (transports/serial)."""
