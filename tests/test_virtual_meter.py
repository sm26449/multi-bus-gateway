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
"""Conformance: the standalone EM24 reader (Victron-equivalent decode) reads the
virtual meter and gets back exactly the values we fed in. Template-derived
synthetic sources so it survives source-name changes."""
import time
from multibus.virtual_meter import load_template, VirtualMeter
from tools.read_em24 import read_em24

PORT = 15022


# Synthetic values keyed by EM24 register ADDRESS — the protocol contract the
# Victron reader polls. Source names are template-internal and rename freely
# (Janitza OEM -> canonical, 2026-08); deriving `fed` from the template keeps
# this test immune to such renames.
VALUES_BY_ADDR = {
    0x0028: -20049.0,    # total power W
    0x0033: 49.99,       # frequency Hz
    0x0034: 87992.0,     # forward (import) energy Wh
    0x004e: 27913246.0,  # reverse (export) energy Wh
    0x0000: 239.1, 0x0002: 238.2, 0x0004: 238.0,        # V L1-3
    0x000c: 27.96, 0x000e: 28.3, 0x0010: 28.1,          # A L1-3
    0x0012: -6659.0, 0x0014: -6716.0, 0x0016: -6656.0,  # W L1-3
}


def test_em24_conformance():
    t = load_template('config/templates/em24_av53.yaml')
    t.transport['port'] = PORT
    live_by_addr = {r.addr: r.source for r in t.registers
                    if r.source_kind == 'live'}
    missing = set(VALUES_BY_ADDR) - set(live_by_addr)
    assert not missing, f"template lost live registers at {sorted(map(hex, missing))}"
    # E1: every live row must resolve once or the meter is withheld
    fed = {name: 0.0 for name in live_by_addr.values()}
    fed.update({live_by_addr[a]: v for a, v in VALUES_BY_ADDR.items()})
    now = time.monotonic()
    vm = VirtualMeter(t, lambda n: (fed[n], now) if n in fed else None,
                      stale_after_s=60, update_interval_s=0.3)
    vm.start()
    try:
        for _ in range(40):
            time.sleep(0.25)
            try:
                got = read_em24('127.0.0.1', PORT, 1)
                break
            except SystemExit:
                continue
        else:
            raise AssertionError("virtual meter did not come up")

        assert got['model_id'] == 1651
        assert got['application'] == 7
        assert abs(got['power_total_W'] - (-20049.0)) < 0.5
        assert abs(got['frequency_Hz'] - 49.99) < 0.15        # 0.1 Hz resolution
        assert abs(got['L1_V'] - 239.1) < 0.2
        assert abs(got['L1_A'] - 27.96) < 0.01
        assert abs(got['L1_W'] - (-6659.0)) < 0.5
        assert abs(got['energy_export_kWh'] - 27913.246) < 0.2   # Wh->kWh*10 scale
    finally:
        vm.stop()
