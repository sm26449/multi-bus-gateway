# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""MOVED to multibus.vendors.backfill. Back-compat shim (structure audit §5.1).

A sys.modules alias does not run under ``python -m``, and ``python -m
multibus.backfill`` is the documented entry (tools/run-backfill.sh, docs/MANUAL):
so when run as a module this delegates to the moved module's ``main()``;
otherwise it binds the old import path to the same object. Remove after
2027-01-31 (update the script + docs to ``-m multibus.vendors.backfill`` then)."""
import importlib as _il
import sys as _sys

_real = _il.import_module("multibus.vendors.backfill")

if __name__ == "__main__":
    _sys.exit(_real.main())
else:
    _sys.modules[__name__] = _real
