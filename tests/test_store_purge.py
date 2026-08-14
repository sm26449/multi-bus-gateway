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
"""M2 (audit 2026-08-14): live-store entries at DESELECTED addresses must be
purged on register reload. A template re-select that moves a register to a new
address while keeping its canonical name left the old entry frozen in the
store; name-based lookup (vmeter sources) binds the first match in insertion
order, so rows read the ghost's frozen timestamp and fail-stop the meter."""
import time

from multibus.device_registry import purge_deselected
from multibus.calc_engine import CALC_ADDR_BASE
from multibus.virtual_meter_manager import _lookup

from tests.test_devices_api import make_app, needs_tc


class _Reg:
    def __init__(self, address):
        self.address = address


def test_purge_drops_deselected_keeps_selected_and_calc():
    store = {
        100: {"name": "power", "value": 1.0},
        200: {"name": "old_power", "value": 2.0},          # deselected → out
        CALC_ADDR_BASE + 1: {"name": "calc", "value": 3.0},  # calc → untouched
        "weird": {"name": "nonint-key"},                   # non-int key kept
    }
    dropped = purge_deselected(store, [_Reg(100)])
    assert dropped == 1
    assert 100 in store and CALC_ADDR_BASE + 1 in store and "weird" in store
    assert 200 not in store


def test_purge_unblocks_name_lookup_after_readdress():
    """The exact M2 failure: same canonical name moves 100→110; before the
    purge, _lookup (insertion order) binds the FROZEN ghost at 100."""
    now = time.monotonic()
    store = {
        100: {"name": "power_active_total", "value": -1.0, "mono": now - 9999},
        110: {"name": "power_active_total", "value": -2.0, "mono": now},
    }
    got = _lookup(store, "power_active_total")
    assert got[0] == -1.0                                  # the ghost wins — the bug
    purge_deselected(store, [_Reg(110)])
    got = _lookup(store, "power_active_total")
    assert got[0] == -2.0 and got[1] == now                # fresh entry after purge


@needs_tc
def test_reload_registers_endpoint_purges_primary_store(tmp_path):
    _cfg, client = make_app(tmp_path)
    store = client.app.state.current_values
    store[64000] = {"name": "ghost", "value": 1.0, "mono": time.monotonic()}
    r = client.post("/api/config/reload-registers")
    assert r.status_code == 200
    assert 64000 not in store


@needs_tc
def test_device_reselect_purges_its_store(tmp_path):
    secondary = """
devices:
  - id: hall-em24
    name: Hall EM24
    enabled: true
    connection: {protocol: tcp, host: 192.0.2.9, port: 1502, unit_id: 2}
"""
    _cfg, client = make_app(tmp_path, extra_yaml=secondary)
    # first selection: name at address 0
    client.post("/api/registers/selected?device=hall-em24", json=[
        {"address": 0, "name": "voltage_l1_n", "label": "L1", "unit": "V",
         "data_type": "float", "poll_group": "realtime"}])
    dev_store = client.app.state.device_values.setdefault("hall-em24", {})
    dev_store[0] = {"name": "voltage_l1_n", "value": 230.0,
                    "mono": time.monotonic() - 9999}       # will become the ghost
    # re-select: SAME name at a NEW address (the template re-select scenario)
    r = client.post("/api/registers/selected?device=hall-em24", json=[
        {"address": 2, "name": "voltage_l1_n", "label": "L1", "unit": "V",
         "data_type": "float", "poll_group": "realtime"}])
    assert r.status_code == 200
    assert 0 not in dev_store                              # ghost purged
