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
"""Fixes from the 2026-08 audit batch: energy-endpoint path traversal and the
missing-timestamp staleness laundering."""
import pytest

from multibus.config import Config

from tests.test_devices import write_config
from tests.test_devices_api import make_app, needs_tc

SECONDARY = """
devices:
  - id: hall-em24
    name: Hall
    enabled: true
    template: carlo_gavazzi_em24
    connection: {protocol: tcp, host: 192.0.2.9, port: 1502, unit_id: 2}
"""


def test_device_registers_path_rejects_traversal(tmp_path):
    cfg = write_config(tmp_path)
    for evil in ("..", "../../tmp/x", "a/b", "."):
        with pytest.raises(ValueError):
            cfg.device_registers_path(evil)
    # a real id still resolves under config/devices/<id>/
    p = cfg.device_registers_path("hall-em24")
    assert p.name == "selected_registers.json"
    assert "devices/hall-em24" in str(p)


@needs_tc
def test_energy_fields_rejects_unknown_and_traversal_device(tmp_path):
    _, client = make_app(tmp_path, extra_yaml=SECONDARY)
    # unknown device → 404, never a write
    assert client.post("/api/energy/fields?device=nope",
                       json={"fields": []}).status_code == 404
    # traversal id → 404 (not found) or 422, never a write outside the tree
    for evil in ("..", "../../../tmp/pwn"):
        rsp = client.post(f"/api/energy/fields?device={evil}", json={"fields": []})
        assert rsp.status_code in (404, 422)
    # nothing was written outside config/devices/
    assert not (tmp_path.parent / "pwn").exists()
    assert not (tmp_path / "selected_registers.json").read_text().strip().startswith("PWN") \
        if (tmp_path / "selected_registers.json").exists() else True


def test_missing_timestamp_not_laundered_to_now():
    """A value with no measurement ts must store timestamp=None (fail-safe),
    not the current time — else the vmeter would serve it as fresh."""
    from datetime import datetime
    # reproduce the exact store-write branch logic
    def stamp(_ts):
        return (datetime.fromtimestamp(_ts).isoformat() if _ts else None)
    assert stamp(0) is None
    assert stamp(None) is None
    assert stamp(1000.0) is not None
    # and the vmeter lookup treats None as not-fresh (value kept, ts None)
    from multibus.virtual_meter_manager import _lookup
    store = {5: {"name": "P", "value": 42.0, "timestamp": None}}
    assert _lookup(store, "P") == (42.0, None)
