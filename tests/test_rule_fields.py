# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""GET /api/rule-fields: what the editors' `device.field` autocomplete offers
is exactly what a rule's inputs are judged against."""
from tests.test_devices_api import needs_tc


@needs_tc
def test_a_device_lists_what_it_reads_with_its_units(tmp_path):
    from tests.test_commands import _Inverter
    from tests.test_rules_api import _feed, _rule_app
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    _feed(app, clock, 'pv-u1', {'voltage_l1_n': 230, 'power_limit_pct': 100})
    rt._fields_of.cache_clear()
    r = client.get("/api/rule-fields", params={"device": "pv-u1"})
    assert r.status_code == 200, r.text
    names = [f["name"] for f in r.json()["fields"]]
    assert "voltage_l1_n" in names and names == sorted(names)
    # the same set the rule inputs check uses
    assert set(names) == set(rt._fields_of("pv-u1"))


@needs_tc
def test_an_unknown_id_is_a_404_not_an_empty_list(tmp_path):
    from tests.test_commands import _Inverter
    from tests.test_rules_api import _rule_app
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    assert client.get("/api/rule-fields", params={"device": "nosuch"}).status_code == 404


@needs_tc
def test_a_device_not_reading_yet_still_has_its_units(tmp_path):
    # the units the driver is told, before any value has been read
    from types import SimpleNamespace
    from tests.test_commands import _Inverter
    from tests.test_rules_api import _rule_app
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    _i, _cfg, dev = app.state.registry.find('pv-u1')
    regs = [SimpleNamespace(name='voltage_l1_n', unit='V'), SimpleNamespace(name='power_limit_pct', unit='%')]
    dev.parts[0] = (dev.parts[0][0], SimpleNamespace(registers=regs))     # its first source's driver
    rt._fields_of.cache_clear()
    fields = client.get("/api/rule-fields", params={"device": "pv-u1"}).json()["fields"]
    units = {f["name"]: f["unit"] for f in fields}
    assert units["voltage_l1_n"] == "V" and units["power_limit_pct"] == "%", fields


@needs_tc
def test_an_installation_with_no_totals_yet_cannot_tell(tmp_path, monkeypatch):
    from multibus import endpoint_aggregator
    from tests.test_commands import _Inverter
    from tests.test_rules_api import _rule_app
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    monkeypatch.setattr(cfg, "_raw_endpoints", [{"id": "site"}])
    monkeypatch.setitem(endpoint_aggregator._latest_totals, "site", ({}, 0.0))
    rt._fields_of.cache_clear()
    r = client.get("/api/rule-fields", params={"device": "site"})
    assert r.status_code == 200 and r.json()["fields"] is None
