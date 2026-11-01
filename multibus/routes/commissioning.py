# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""RTU commissioning helpers — enumerate the serial lines a new device can use.

Two read-only discovery endpoints the add-device wizard calls:
- ``/api/serial-ports`` — the serial ports of this host from sysfs, each with
  its state (polled, listened, not mapped into this container, …):
  ``serial_inventory``, ADR-0028.
- ``/api/bridge/adapters`` — adapter inventory from the serial-over-TCP bridge
  (RTU-over-network). Self-contained: no shared gateway state.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from urllib.parse import urlparse

from fastapi import APIRouter

from ..redact import redact_url

logger = logging.getLogger(__name__)
_BRIDGE_URL = os.environ.get("SERIAL_BRIDGE_URL", "http://mbg-serial-bridge:7000")


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["commissioning"])

    @r.get("/api/serial-ports")
    def list_serial_ports(all: bool = False):  # noqa: A002 — the query parameter's name
        """The serial ports of this host, the way the bridge lists them
        (ADR-0028): every port in /sys, with its kind, stable path and one
        state (free, polled, listened, busy, not_mapped, unknown; console
        only with ``?all=1``). Read-only: sysfs, /dev and /proc, no port is
        opened. The keys it answered with before (dev, vendor_id, …) stay.
        Never raises; a failure is logged, the answer says no internals."""
        from .. import serial_inventory
        try:
            return serial_inventory.inventory(ctx.config, getattr(ctx, "template_registry", None),
                                              show_all=all)
        except Exception:  # noqa: BLE001 — an odd sysfs never breaks the form
            logger.warning("serial ports could not be listed", exc_info=True)
            return {"ports": [], "hidden": 0,
                    "error": "the serial ports could not be listed (see the gateway log)"}

    @r.get("/api/bridge/adapters")
    def bridge_adapters():
        """Live adapter inventory from the serial-over-TCP bridge, so the UI can
        scan for RTU-over-network devices. Returns available=false (not an error)
        when the bridge is unreachable, so the UI degrades gracefully. Adapters
        carry the bridge host + their stable tcp_port to prefill the add form."""
        host = urlparse(_BRIDGE_URL).hostname or "mbg-serial-bridge"
        try:
            _req = urllib.request.Request(f"{_BRIDGE_URL}/adapters")
            if os.environ.get("SERIAL_BRIDGE_TOKEN"):
                # a bridge started with BRIDGE_TOKEN answers only with it
                _req.add_header("Authorization", f"Bearer {os.environ['SERIAL_BRIDGE_TOKEN']}")
            with urllib.request.urlopen(_req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
            for a in data.get("adapters", []):
                a["bridge_host"] = host        # what an rtu-tcp device sets as connection.host
            return {"available": True, "bridge_host": host, **data}
        except Exception as e:  # noqa: BLE001
            return {"available": False, "bridge_host": host, "adapters": [],
                    "error": f"serial bridge unreachable at {redact_url(_BRIDGE_URL)}: {e}"}

    return r
