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
"""The template's `display` block: declared alarms, sections, bitmasks."""
import json
from pathlib import Path
from types import SimpleNamespace as NS

from multibus.device_template import validate_template
from multibus.display import active_alarms, display_of

DISP = {"alarms": [{"field": "alarm_count", "severity": "warning"},
                   {"field": "protection_count", "severity": "danger"},
                   {"field": "fault_word", "severity": "danger"}]}


def test_active_alarms_counts_what_the_device_says_is_wrong():
    store = [{"name": "alarm_count", "value": 2.0, "label": "Alarm Count"},
             {"name": "protection_count", "value": 0},
             {"name": "fault_word", "value": "OK"}]
    out = active_alarms(DISP, store)
    assert (out["warning"], out["danger"]) == (1, 0)
    assert out["active"][0]["label"] == "Alarm Count"
    store[1]["value"] = 1
    store[2]["value"] = "Overtemp"
    out = active_alarms(DISP, store)
    assert (out["warning"], out["danger"]) == (1, 2)


def test_stale_or_missing_alarm_fields_count_as_nothing():
    store = [{"name": "alarm_count", "value": 3, "ts": 0}]
    assert active_alarms(DISP, store, fresh=lambda e: False)["warning"] == 0
    assert active_alarms({}, store) == {"danger": 0, "warning": 0, "active": []}


def test_display_of_reads_the_units_template():
    reg = NS(get=lambda tid: NS(display={"glance": ["x"]}) if tid == "t1" else None)
    assert display_of(reg, NS(template="t1", sources=[])) == {"glance": ["x"]}
    assert display_of(reg, NS(template="", sources=[NS(template="t1")]))["glance"] == ["x"]
    assert display_of(reg, NS(template="nope", sources=[])) == {}


def test_display_alarms_sections_bitmasks_are_validated():
    p = Path("multibus/device_templates/seplos_bms_v3_rtu_tap.json")
    data = json.loads(p.read_text())
    assert validate_template(data) == []
    d = data["device_template"]["display"]
    d["alarms"].append({"field": "nope"})
    d["alarms"].append({"field": "alarm_count", "severity": "critical"})
    d["sections"]["nope"] = {"widget": "grid"}
    d["sections"]["cells"] = {"widget": "pie"}
    d["bitmasks"]["balancing_bits"] = "no placeholder"
    errs = " | ".join(validate_template(data))
    for frag in ("display.alarms[3]", "display.alarms[4]", "display.sections.nope",
                 "display.sections.cells", "display.bitmasks.balancing_bits"):
        assert frag in errs, frag
