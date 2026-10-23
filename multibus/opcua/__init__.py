# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""OPC UA — the northbound face for SCADA and HMI panels (docs/opcua.md).

Kept as its own package behind a small interface (start / stop / status),
so it could move into a companion container later without a rewrite. The
``asyncua`` library is imported only when the server is enabled.
"""
from .server import OpcUaServer, quality_of  # noqa: F401
