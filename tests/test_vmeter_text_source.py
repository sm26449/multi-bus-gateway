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
"""H1 (audit 2026-08-14): a text-valued source bound to a numeric vmeter row
(enum/bits decode, string registers) must degrade THAT ROW to missing — never
abort the whole block rebuild. An aborted rebuild left the served frame frozen
with the stale-stop fail-safe disarmed (freshness is computed only after a
full rebuild), i.e. a control loop kept reading old power values as live."""
import time

from multibus.virtual_meter import Template, RegisterDef, VirtualMeter
from multibus.virtual_meter_manager import VirtualMeterManager


def _vm(rows, provider, **kw):
    t = Template(id="t1", name="t1", transport={"port": 19999}, registers=rows)
    kw.setdefault("stale_after_s", 60)
    kw.setdefault("update_interval_s", 0.1)
    return VirtualMeter(t, provider, **kw)


def _rows():
    return [
        RegisterDef(addr=0x0000, type="int32", source_kind="live", source="power"),
        RegisterDef(addr=0x0002, type="int32", source_kind="live", source="status"),
    ]


def test_legacy_text_row_degrades_to_missing_not_frozen_block():
    now = time.monotonic()
    fed = {"power": (-1234.0, now), "status": ("Fault", now)}   # enum text
    vm = _vm(_rows(), lambda n: fed.get(n))
    newest = vm._rebuild_block()          # must not raise
    assert newest > 0.0                   # the numeric row resolved
    # the text row is a gap (missing), the numeric row is served
    served = dict(vm._regs_out)
    assert 0x0000 in served and 0x0002 not in served
    # the fail-safe verdict is still computed (rebuild completed): the numeric
    # row is fresh, the text row is a gap — legacy gap semantics keep all_fresh
    assert vm._legacy_all_fresh is True
    # edge-triggered: exactly one warn event for the unencodable row
    warns = [e for e in list(vm.stats.events) if e.get("kind") == "encode"]
    assert len(warns) == 1
    vm._rebuild_block()                   # second tick: no duplicate event
    warns = [e for e in list(vm.stats.events) if e.get("kind") == "encode"]
    assert len(warns) == 1


def test_stale_stop_still_reached_with_text_row_present():
    """The core H1 scenario: source goes stale WHILE a text row exists — the
    freshness verdict must still flip (pre-fix the rebuild aborted first)."""
    now = time.monotonic()
    fed = {"power": (-1234.0, now - 120), "status": ("Fault", now)}  # power stale
    vm = _vm(_rows(), lambda n: fed.get(n))
    vm._rebuild_block()
    assert vm._legacy_all_fresh is False  # stale row fails the instance


def test_policy_mode_counts_text_row_missing():
    now = time.monotonic()
    fed = {"power": (-1234.0, now), "status": ("Fault", now)}
    vm = _vm(_rows(), lambda n: fed.get(n), on_stale="fail")
    newest = vm._rebuild_block()
    assert newest > 0.0                   # server-up: one fresh row suffices
    assert vm._quality["fresh"] == 1 and vm._quality["missing"] == 1


def test_recovery_event_when_value_numeric_again():
    now = time.monotonic()
    fed = {"power": (-1234.0, now), "status": ("Fault", now)}
    vm = _vm(_rows(), lambda n: fed.get(n))
    vm._rebuild_block()
    fed["status"] = (3.0, time.monotonic())        # numeric again
    vm._rebuild_block()
    kinds = [(e.get("level"), e.get("kind")) for e in list(vm.stats.events)]
    assert ("info", "encode") in kinds             # recovery logged


def test_json_view_survives_text_in_sum():
    now = time.monotonic()
    fed = {"a": (1.0, now), "b": ("Fault", now)}
    rows = [RegisterDef(addr=0x0000, type="int32", source_kind="sum",
                        source=["a", "b"])]
    vm = _vm(rows, lambda n: fed.get(n))
    view = vm.json_view()                 # must not raise
    assert view["values"]["addr_0"]["quality"] == "missing"


def test_save_rejects_known_text_source_on_numeric_row(tmp_path):
    cur = {"1": {"name": "status", "label": "Status", "unit": "",
                 "value": "Fault", "timestamp": "2026-08-14T10:00:00"}}
    mgr = VirtualMeterManager(cur, config_path=str(tmp_path / "vm.yaml"),
                              templates_dir=str(tmp_path / "templates"))
    payload = {"id": "m1", "name": "M1", "byte_order": "big",
               "port": 1700, "unit_id": 1,
               "registers": [{"addr": "0x0000", "type": "int32",
                              "source_kind": "live", "source": "status"}]}
    out = mgr.save_template("m1", payload)
    assert "error" in out and "text" in out["error"]


def test_save_allows_text_source_on_string_row(tmp_path):
    cur = {"1": {"name": "status", "label": "Status", "unit": "",
                 "value": "Fault", "timestamp": "2026-08-14T10:00:00"}}
    mgr = VirtualMeterManager(cur, config_path=str(tmp_path / "vm.yaml"),
                              templates_dir=str(tmp_path / "templates"))
    payload = {"id": "m2", "name": "M2", "byte_order": "big",
               "port": 1700, "unit_id": 1,
               "registers": [{"addr": "0x0000", "type": "string", "length": 4,
                              "source_kind": "live", "source": "status"}]}
    out = mgr.save_template("m2", payload)
    assert out.get("saved") is True
