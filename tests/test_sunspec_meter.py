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
"""Conformance: SunSpec reader decodes the virtual Fronius meter correctly."""
import time
from multibus.virtual_meter import load_template, VirtualMeter
from tools.read_sunspec import read_sunspec

PORT = 15052


# Synthetic values keyed by SunSpec model-213 register ADDRESS — the protocol
# contract the reader polls. Source names are template-internal and rename
# freely; deriving `fed` from the template keeps this test immune to renames.
VALUES_BY_ADDR = {
    40072: 1.3,                                          # A total
    40074: 27.9, 40076: 28.3, 40078: 28.1,               # A L1-3
    40080: 235.9, 40082: 235.9,                          # PhV avg / PhVphA
    40084: 234.5, 40086: 234.0,                          # PhVphB / PhVphC
    40096: 50.01,                                        # Hz
    40098: -20000.0,                                     # W total
    40100: -6659.0, 40102: -6716.0, 40104: -6656.0,      # W L1-3
    40130: 27922776.0,                                   # TotWhExp
    40138: 88052.0,                                      # TotWhImp
}


def test_sunspec_conformance():
    t = load_template('config/templates/fronius_sunspec_meter.yaml')
    t.transport['port'] = PORT
    live_by_addr = {r.addr: r.source for r in t.registers
                    if r.source_kind == 'live'}
    missing = set(VALUES_BY_ADDR) - set(live_by_addr)
    assert not missing, f"template lost live registers at {sorted(missing)}"
    fed = {live_by_addr[a]: v for a, v in VALUES_BY_ADDR.items()}
    now = time.monotonic()
    vm = VirtualMeter(t, lambda n: (fed[n], now) if n in fed else None,
                      stale_after_s=60, update_interval_s=0.3)
    vm.start()
    try:
        for _ in range(40):
            time.sleep(0.25)
            try:
                got = read_sunspec('127.0.0.1', PORT, 1); break
            except SystemExit:
                continue
        else:
            raise AssertionError("sunspec meter did not come up")
        assert got['SunS'] == 'SunS'
        assert got['model_id'] == 213                   # float 3-phase (Fronius requires it)
        assert got['Manufacturer'] == 'Fronius'         # DataManager checks this for detection
        assert abs(got['V_L1'] - 235.9) < 0.1           # float — exact
        assert abs(got['Hz'] - 50.01) < 0.01
        assert abs(got['W_total'] - (-20000)) < 1       # float — exact
        assert abs(got['Wh_export'] - 27922776) < 4     # float32 precision at 2.8e7
        assert abs(got['Wh_import'] - 88052) < 1
    finally:
        vm.stop()
