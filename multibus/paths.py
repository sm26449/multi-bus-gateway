# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Resolved paths to the package's bundled data directories.

``device_templates/`` and ``bridge_types/`` are *data*, not code, so they stay
at the package root through the refactor. Modules that move into subpackages import
the directory from here instead of computing ``Path(__file__).parent``, which
would otherwise point one level too deep after the move. This module lives at
the package root, so the paths are correct no matter where the callers end up.
"""
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent
DEVICE_TEMPLATES_DIR = PACKAGE_DIR / "device_templates"
BRIDGE_TYPES_DIR = PACKAGE_DIR / "bridge_types"
