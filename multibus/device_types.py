# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""MOVED to multibus.catalog.device_types. Back-compat shim (structure audit §5.1) — same
module object at the old path, for tests/tools/external use. Package code
imports the new path. Remove after 2027-01-31."""
import importlib as _il
import sys as _sys

_sys.modules[__name__] = _il.import_module("multibus.catalog.device_types")
