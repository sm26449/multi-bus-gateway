# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
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
- ``/api/serial-ports`` — local ``/dev`` lines visible to THIS container (direct
  RTU via a compose ``devices:`` mapping); empty when MBG has no ``/dev`` access.
- ``/api/bridge/adapters`` — adapter inventory from the serial-over-TCP bridge
  (RTU-over-network). Both are self-contained: no shared gateway state.
"""
from __future__ import annotations

import glob
import json
import os
import urllib.request
from urllib.parse import urlparse

from fastapi import APIRouter

from ..redact import redact_url

_BRIDGE_URL = os.environ.get("SERIAL_BRIDGE_URL", "http://mbg-serial-bridge:7000")


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["commissioning"])

    @r.get("/api/serial-ports")
    def list_serial_ports():
        """Local /dev serial lines visible to THIS container — for direct RTU
        mode (a compose `devices:` mapping). Empty when MBG has no /dev access
        (the default now: RTU goes through the bridge). Never raises."""
        ports = []
        for dev in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
            entry = {"dev": dev}
            try:
                name = os.path.basename(dev)
                usb = os.path.realpath(f"/sys/class/tty/{name}/device")
                for _ in range(8):
                    if os.path.exists(os.path.join(usb, "idVendor")):
                        break
                    usb = os.path.dirname(usb)

                def _read(p):
                    try:
                        with open(os.path.join(usb, p)) as f:
                            return f.read().strip()
                    except OSError:
                        return ""
                entry.update(vendor_id=_read("idVendor"), product_id=_read("idProduct"),
                             serial=_read("serial"), model=_read("product"))
            except Exception:  # noqa: BLE001
                pass
            ports.append(entry)
        return {"ports": ports}

    @r.get("/api/bridge/adapters")
    def bridge_adapters():
        """Live adapter inventory from the serial-over-TCP bridge, so the UI can
        scan for RTU-over-network devices. Returns available=false (not an error)
        when the bridge is unreachable, so the UI degrades gracefully. Adapters
        carry the bridge host + their stable tcp_port to prefill the add form."""
        host = urlparse(_BRIDGE_URL).hostname or "mbg-serial-bridge"
        try:
            with urllib.request.urlopen(f"{_BRIDGE_URL}/adapters", timeout=4) as resp:
                data = json.loads(resp.read().decode())
            for a in data.get("adapters", []):
                a["bridge_host"] = host        # what an rtu-tcp device sets as connection.host
            return {"available": True, "bridge_host": host, **data}
        except Exception as e:  # noqa: BLE001
            return {"available": False, "bridge_host": host, "adapters": [],
                    "error": f"serial bridge unreachable at {redact_url(_BRIDGE_URL)}: {e}"}

    return r
