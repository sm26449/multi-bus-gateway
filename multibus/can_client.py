# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""MOVED to multibus.transports.can.can_client. Back-compat shim: it binds the
old import path to the *same* module object (structure audit §5.1), so
monkeypatch / patch.object and the conftest resets still reach the real code.
Package code imports the new path; this serves tests, tools and external users
only. Remove after 2027-01-31."""
import importlib as _il
import sys as _sys

_sys.modules[__name__] = _il.import_module("multibus.transports.can.can_client")
