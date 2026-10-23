# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A yes/no field is a JSON true/false. `bool("false")` is True in Python:
a client sending the string once ENABLED the command or the rule it meant
to turn off. Turning a command off keeps what its binding says."""
import pytest
from fastapi import HTTPException

from multibus.payload import flag
from tests.test_devices_api import make_app, needs_tc


def test_flag_takes_only_true_or_false():
    assert flag({'enabled': True}, 'enabled') is True
    assert flag({'enabled': False}, 'enabled') is False
    assert flag({}, 'enabled', True) is True
    assert flag({'enabled': None}, 'enabled', False) is False
    for bad in ('false', 'true', 0, 1, 'no', [], {}):
        with pytest.raises(HTTPException) as e:
            flag({'enabled': bad}, 'enabled')
        assert e.value.status_code == 422 and 'enabled' in e.value.detail['fields']
    with pytest.raises(HTTPException):
        flag({}, 'enabled')                      # required when it has no default


@needs_tc
def test_a_command_is_enabled_only_by_true_and_turning_it_off_keeps_its_binding(tmp_path):
    cfg, client = make_app(tmp_path)
    assert client.post("/api/devices", json={"id": "inv9", "template": "fronius_sunspec_inverter", "enabled": False,
                                             "connection": {"protocol": "tcp", "host": "192.0.2.9", "port": 502,
                                                            "unit_id": 1}}).status_code == 200
    en = lambda: {c['name']: c['enabled'] for c in client.get("/api/devices/inv9/commands").json()['commands']}  # noqa: E731
    url = "/api/devices/inv9/commands/power_limit/enabled"
    # "false" as a string used to read as true and ENABLE the command
    assert client.put(url, json={"enabled": "false"}).status_code == 422
    assert client.put(url, json={}).status_code == 422
    assert en()['power_limit'] is False
    # on, then a site override on its binding, then off: the override stays
    assert client.put(url, json={"enabled": True}).status_code == 200
    # a site override on its binding (as a hand edit or the API would set it)
    raw = next(d for d in cfg._raw_devices if d['id'] == 'inv9')
    raw['commands'] = [dict(b, lease_s=600) if b.get('name') == 'power_limit' else b for b in raw['commands']]
    assert client.put(url, json={"enabled": False}).status_code == 200
    assert en()['power_limit'] is False
    raw = next(d for d in cfg._raw_devices if d['id'] == 'inv9')
    bind = next(b for b in raw['commands'] if b.get('name') == 'power_limit')
    assert bind['enabled'] is False and bind.get('lease_s') == 600
    assert client.put(url, json={"enabled": True}).status_code == 200
    bind = next(b for b in next(d for d in cfg._raw_devices if d['id'] == 'inv9')['commands'] if b.get('name') == 'power_limit')
    assert bind['enabled'] is True and bind.get('lease_s') == 600


@needs_tc
def test_a_rule_is_enabled_only_by_true(tmp_path):
    from tests.test_commands import _Inverter
    from tests.test_rules_api import _rule_app
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    assert client.post("/api/rules/ov-u1/enable", json={"enabled": False}).status_code == 200
    assert rt.rules['ov-u1'].enabled is False
    # "false" or nothing used to ENABLE it
    assert client.post("/api/rules/ov-u1/enable", json={"enabled": "false"}).status_code == 422
    assert client.post("/api/rules/ov-u1/enable", json={}).status_code == 422
    assert rt.rules['ov-u1'].enabled is False
    assert client.post("/api/rules/ov-u1/enable", json={"enabled": True}).status_code == 200
    assert rt.rules['ov-u1'].enabled is True


@needs_tc
def test_a_bad_lease_is_refused_before_anything_is_written(tmp_path):
    """A lease the caller got wrong used to be found AFTER the write went
    out: the inverter was limited, and the reply said the request was bad."""
    from tests.test_commands import _Inverter
    from tests.test_commands_api import _app
    inv = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    for bad in ("abc", -1, 90000, 1.5, True):
        r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 50, "lease_s": bad})
        assert r.status_code == 422 and 'lease_s' in r.text, (bad, r.status_code, r.text)
    assert inv.writes == []
    # a good lease still goes through
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 50, "lease_s": 60})
    assert r.status_code == 200, r.text
    assert inv.writes


@needs_tc
def test_the_write_master_switch_and_anonymous_opcua_take_only_true_or_false(tmp_path):
    # review lot A: these still read bool(...) — "false" turned writes ON
    cfg, client = make_app(tmp_path)
    before = cfg.security.allow_writes
    for bad in ("false", "true", 0, 1):
        assert client.post("/api/config/security", json={"allow_writes": bad}).status_code == 422
        assert cfg.security.allow_writes == before
        assert client.post("/api/config/security",
                           json={"allow_nonlan_http_devices": bad}).status_code == 422
    assert client.post("/api/config/security", json={"allow_writes": False}).status_code == 200
    assert cfg.security.allow_writes is False
    anon = cfg.opcua.allow_anonymous
    assert client.put("/api/opcua", json={"allow_anonymous": "false"}).status_code == 422
    assert cfg.opcua.allow_anonymous == anon


def test_a_rule_written_with_enabled_as_a_string_is_refused_and_never_enabled():
    from multibus.rules import parse_rule_def, validate_rule_def
    raw = {'id': 'r1', 'enabled': 'false', 'mode': 'shadow', 'kind': 'steps',
           'target': {'device': 'd', 'command': 'c'}, 'signal': 'd.v', 'steps': []}
    assert any(e.startswith('enabled') for e in validate_rule_def(raw))
    assert parse_rule_def(raw).enabled is False
    assert parse_rule_def(dict(raw, enabled=True)).enabled is True
    assert parse_rule_def({k: v for k, v in raw.items() if k != 'enabled'}).enabled is True


@needs_tc
def test_mqtt_disables_a_rule_on_false_and_ignores_what_it_cannot_read(tmp_path):
    from tests.test_commands import _Inverter
    from tests.test_rules_api import _rule_app
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    rt._mqtt_set('ov-u1', '{"enabled": "maybe"}')
    assert rt.rules['ov-u1'].enabled is True
    rt._mqtt_set('ov-u1', '{"enabled": true}')          # enabling stays with the API
    assert rt.rules['ov-u1'].enabled is True
    rt._mqtt_set('ov-u1', '{"enabled": "false"}')       # a controller taking over: off
    assert rt.rules['ov-u1'].enabled is False
