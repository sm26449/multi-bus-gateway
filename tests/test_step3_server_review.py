# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Step 3, after its server review: the primary's dry run, poll groups
removed, an id an installation holds, the names a rule's inputs are judged
on (from memory, refreshed when devices change), a virtual meter's refresh,
and a pause no broken rule holds."""
import json

import yaml

from tests.test_devices_api import make_app, needs_tc
from tests.test_unchanged_save import DEVICES, PLANT
from tests.test_vmeter_lifecycle import PORT_A, _save_template, app_with_mgr  # noqa: F401


@needs_tc
def test_a_primary_poll_group_removed_goes_back_to_the_default_in_memory_too(tmp_path):
    cfg, client = make_app(tmp_path)
    default = cfg.default_poll_groups["normal"].interval
    assert client.post("/api/devices/umg512/poll-groups", json={"poll_groups": {"normal": {"interval": 9}}}).status_code == 200
    assert cfg.poll_groups["normal"].interval == 9
    assert client.post("/api/devices/umg512/poll-groups", json={"poll_groups": {"normal": None}}).status_code == 200
    assert cfg.poll_groups["normal"].interval == default      # the running poller and GET agree with the file


@needs_tc
def test_a_group_its_registers_still_use_and_the_gateway_lacks_is_not_removed(tmp_path):
    cfg, client = make_app(tmp_path, DEVICES)
    path = cfg.device_registers_path("udp")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": "1.0", "poll_groups": {"static": {"interval": 3600}},
                                "registers": [{"address": 0, "name": "serial", "poll_group": "static"}]}))
    r = client.post("/api/devices/udp/poll-groups", json={"poll_groups": {"static": None}})
    assert r.status_code == 422, r.text
    d = r.json()["detail"]
    assert "poll_groups.static" in d["fields"] and "normal" in d["errors"][0]
    assert json.loads(path.read_text())["poll_groups"]["static"]["interval"] == 3600
    # a gateway group may go (it falls back to the gateway's own rhythm)
    path.write_text(json.dumps({"version": "1.0", "poll_groups": {"slow": {"interval": 120}},
                                "registers": [{"address": 0, "name": "e", "poll_group": "slow"}]}))
    assert client.post("/api/devices/udp/poll-groups", json={"poll_groups": {"slow": None}}).status_code == 200


@needs_tc
def test_a_device_cannot_take_an_installations_id(tmp_path):
    cfg, client = make_app(tmp_path, PLANT)
    body = {"id": "pv", "name": "PV", "template": "eastron_sdm630",
            "connection": {"protocol": "tcp", "host": "192.0.2.77", "port": 502, "unit_id": 1}}
    for url in ("/api/devices?dry_run=true", "/api/devices"):
        r = client.post(url, json=body)
        assert r.status_code == 422 and "installation" in r.json()["detail"]["fields"]["id"], r.text
    assert cfg.get_device("pv") is None


@needs_tc
def test_a_rules_inputs_are_judged_from_memory_never_from_disk(tmp_path, monkeypatch):
    from tests.test_commands import _Inverter
    from tests.test_rules import OV
    from tests.test_rules_api import _feed, _rule_app
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    _feed(app, clock, 'pv-u1', {'voltage_l1_n': 230, 'power_limit_pct': 100})

    def _no_disk(*a, **k):
        raise AssertionError("a GET read the registers file")
    monkeypatch.setattr(cfg, "load_device_registers", _no_disk)
    rt._fields_of.cache_clear()
    r = client.post("/api/rules", json=dict(OV, id='typo', signal='pv-u1.voltage_l1'))
    assert r.status_code == 200, r.text
    assert [p['problem'] for p in r.json()['rule']['inputs']] == ['no_field']
    assert client.get("/api/rules").status_code == 200


@needs_tc
def test_a_device_created_is_known_to_the_rules_at_once(tmp_path):
    from tests.test_commands import _Inverter
    from tests.test_rules import OV
    from tests.test_rules_api import _rule_app
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    r = client.post("/api/rules", json=dict(OV, id='later', signal='emlater.voltage_l1_n', mode='shadow'))
    assert r.status_code == 200, r.text
    assert any(p['problem'] == 'no_device' for p in r.json()['rule']['inputs'])
    r = client.post("/api/devices", json={"id": "emlater", "name": "Later", "template": "eastron_sdm630",
                                          "connection": {"protocol": "tcp", "host": "192.0.2.78",
                                                         "port": 502, "unit_id": 1}})
    assert r.status_code == 200, r.text
    got = next(x for x in client.get("/api/rules").json()["rules"] if x["id"] == "later")
    assert not any(p['problem'] == 'no_device' for p in got['inputs'])     # not 30 s later


@needs_tc
def test_a_virtual_meters_refresh_is_a_number_in_range_and_the_default_stays_absent(app_with_mgr):  # noqa: F811
    client, mgr = app_with_mgr
    _save_template(client)
    for bad in ([1], {"x": 1}, "fast", 0.01, 7200, True):
        r = client.post("/api/virtual-meters", json={"template": "t_em", "port": PORT_A, "update_interval_s": bad})
        assert r.status_code == 400 and "update_interval_s" in r.text, (bad, r.status_code, r.text)
    r = client.post("/api/virtual-meters", json={"template": "t_em", "port": PORT_A, "update_interval_s": 1})
    assert r.status_code == 200, r.text
    stored = next(i for i in yaml.safe_load(mgr.config_path.read_text())["instances"] if i["template"] == "t_em")
    assert "update_interval_s" not in stored                    # the default is absent
    r = client.patch("/api/virtual-meters/t_em", json={"update_interval_s": 0.01})
    assert r.status_code == 400 and "update_interval_s" in r.text


@needs_tc
def test_a_rule_in_error_holds_no_pause_on_its_channel(tmp_path):
    from tests.test_commands import _Inverter
    from tests.test_rules_api import _rule_app
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    assert client.post("/api/rules/ov-u1/mode", json={"mode": "armed"}).status_code == 200
    assert client.post("/api/rules/ov-u1/override", json={"seconds": 600}).status_code == 200
    assert client.get("/api/channels/pv-u1").json()["channels"][0]["paused_until_s"]
    rt.errors['ov-u1'] = "its formula no longer resolves"
    try:
        assert client.get("/api/channels/pv-u1").json()["channels"][0]["paused_until_s"] is None
    finally:
        rt.errors.pop('ov-u1', None)


def test_the_names_a_device_reads_come_from_its_running_drivers():
    """Every source of a multi-source device; None when nothing runs."""
    from multibus import api

    class _Drv:
        def __init__(self, names):
            self.registers = [type("R", (), {"name": n})() for n in names]

    class _Multi:
        parts = [(None, _Drv(['a'])), (None, _Drv(['b'])), (None, object())]

    assert api._driver_reg_names(_Multi()) == {'a', 'b'}
    assert api._driver_reg_names(_Drv(['c'])) == {'c'}
    assert api._driver_reg_names(object()) is None


@needs_tc
def test_an_installation_asleep_cannot_judge_a_rules_inputs(tmp_path, monkeypatch):
    # every unit asleep at night leaves the totals empty: that says nothing
    # about the names, so a rule reading them is not "reads nothing" nightly
    from multibus import endpoint_aggregator
    from tests.test_commands import _Inverter
    from tests.test_rules_api import _rule_app
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    monkeypatch.setattr(cfg, "_raw_endpoints", [{"id": "site"}])
    monkeypatch.setitem(endpoint_aggregator._latest_totals, "site", ({}, 0.0))
    rt._fields_of.cache_clear()
    assert rt._fields_of("site") is None
    monkeypatch.setitem(endpoint_aggregator._latest_totals, "site", ({"power_active_total": 1.0}, 0.0))
    rt._fields_of.cache_clear()
    assert rt._fields_of("site") == {"power_active_total"}
