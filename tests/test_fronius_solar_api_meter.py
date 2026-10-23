# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The Fronius Smart Meter read through the Solar API (fronius_solar_api_meter).

The fixture is a real GetMeterRealtimeData.cgi?Scope=Device&DeviceId=1 answer
(DataManager card SW 3.34.1-5, Smart Meter TS 5kA-3), anonymised. It goes
through the same poller an HTTP source runs, and every value must come out
under its canonical name, with the import/export signs the datalogger means."""
import json
import pathlib
import threading

import pytest

from multibus.canonical_fields import CANONICAL_NAMES
from multibus.device_template import load_template
from multibus.http_client import _JsonPoller

ROOT = pathlib.Path(__file__).resolve().parents[1]
TPL = ROOT / "multibus" / "device_templates" / "fronius_solar_api_meter.json"
DOC = json.loads((ROOT / "tests" / "fixtures" / "fronius_solar_api_meter_device.json").read_text())
SUNSPEC = ROOT / "multibus" / "device_templates" / "fronius_sunspec_meter.json"


@pytest.fixture(scope="module")
def tpl():
    return load_template(str(TPL), builtin=True)


class _Owner:
    def _note_success(self, n):
        pass

    def _note_failure(self):
        raise AssertionError("the fixture must parse")


def _poll_once(regs):
    """One round of the real HTTP poller over the fixture."""
    got, done = {}, threading.Event()

    def publish(_group, data):
        got.update({d['register'].name: d['value'] for d in data.values()})
        done.set()

    p = _JsonPoller("realtime", 5, regs, lambda: DOC, publish, _Owner())
    p.start()
    assert done.wait(5)
    p.stop()
    p.join(5)
    return got


def test_it_is_a_meter_read_over_http_in_one_request(tpl):
    assert tpl.id == "fronius_solar_api_meter" and tpl.kind == "meter"
    # the datalogger is shared with the inverters and collapses when driven
    # hard: one poll group = one GET per interval, and never faster than 5 s
    assert {r.poll_group for r in tpl.registers} == {"realtime"}
    assert tpl.poll_groups["realtime"]["interval"] >= 5


def test_every_value_comes_out_under_its_canonical_name(tpl):
    got = _poll_once(tpl.registers)
    assert set(got) == {r.name for r in tpl.registers}          # every path resolves to a number
    assert all(r.name in CANONICAL_NAMES for r in tpl.registers)
    d = DOC["Body"]["Data"]
    for r in tpl.registers:
        assert got[r.name] == pytest.approx(d[r.json_path.split(".")[-1]]), r.name
    assert got["voltage_l1_n"] == pytest.approx(234.8) and got["current_total"] == pytest.approx(19.212)


def test_signs_import_positive_and_the_counters_by_direction(tpl):
    got = _poll_once(tpl.registers)
    # captured while importing (the datalogger's P_Grid was +, from the grid)
    assert got["power_active_total"] > 0
    # Consumed = from the grid = import; Produced = to the grid = export (a PV site)
    assert got["energy_active_import"] == 285154 and got["energy_active_export"] == 67796704
    assert got["power_factor_total"] < 0                         # as the meter gives it, while importing


def test_a_field_keeps_its_identity_with_the_modbus_meter(tpl):
    # same SunSpec address, same name → the same HA unique_id and InfluxDB
    # series whichever way the meter is read
    modbus = {r.name: r.address for r in load_template(str(SUNSPEC), builtin=True).registers}
    for r in tpl.registers:
        if r.name in modbus:
            assert r.address == modbus[r.name], r.name
    shared = {r.name for r in tpl.registers} & set(modbus)
    assert {"power_active_total", "voltage_l1_n", "energy_active_import", "energy_active_export"} <= shared


def test_the_unreliable_or_unmatched_fields_ship_unticked(tpl):
    off = {r.name for r in tpl.registers if not r.defaults["mqtt"]["enabled"]}
    assert off == {"power_factor_l1", "power_factor_l2", "power_factor_l3",
                   "energy_reactive_import", "energy_reactive_export"}


def test_the_url_id_is_documented_as_not_the_unit(tpl):
    assert "DeviceId=240" in tpl.description and "1 on the captured installation" in tpl.description
    assert "12 requests/min" in tpl.description
