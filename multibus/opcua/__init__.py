# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""MOVED to multibus.outputs.opcua. Back-compat shim (structure audit §5.1) —
same module objects at the old paths, for tests/tools/external use. Package code
imports the new path. Remove after 2027-01-31.

Both the package and its ``server`` submodule are aliased in ``sys.modules`` so
that ``from multibus.opcua.server import X`` (dotted-path form) resolves to the
real moved module object, not a second copy loaded off the shim's __path__."""
import importlib as _il
import sys as _sys

_sys.modules[__name__] = _il.import_module("multibus.outputs.opcua")
_sys.modules[__name__ + ".server"] = _il.import_module("multibus.outputs.opcua.server")
