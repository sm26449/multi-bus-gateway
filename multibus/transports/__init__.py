# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Transport drivers: a wire protocol <-> a bus. Subpackages: can, http, mqtt,
and (later batches) modbus, serial. A transport imports core/codec, never a
higher layer (ADR-0034)."""
