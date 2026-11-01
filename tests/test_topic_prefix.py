# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Topic prefixes are checked where they are set (ADR-0026 §4, FINDINGS
ADD-11): a duplicate, an overlap either way, a wildcard or the gateway's own
root is refused with a field error. Stored prefixes that break the rule are
reported once in the log, never refused."""
import logging
import pathlib

import pytest

from tests.test_devices_api import make_app, needs_tc


@pytest.mark.parametrize("bad", ["", "/a/b", "a/b/", "a//b", "a/+/b", "a/#", "a/b#", "a\x00b", "  "])
def test_a_malformed_prefix_is_refused(bad):
    from multibus.topic_prefix import shape_error
    assert shape_error(bad) is not None, repr(bad)


@pytest.mark.parametrize("good", ["a", "a/b", "mbg/devices/x-1", "pv/meter/grid"])
def test_a_well_formed_prefix_passes(good):
    from multibus.topic_prefix import shape_error
    assert shape_error(good) is None


def test_overlap_is_a_path_prefix_either_way_not_a_string_prefix():
    from multibus.topic_prefix import overlap
    assert overlap("a/b", "a/b") == "equal"
    assert overlap("a/b/c", "a/b") == "under"
    assert overlap("a/b", "a/b/c") == "over"
    assert overlap("a/bc", "a/b") is None                 # a string prefix, not a path prefix
    assert overlap("a/b", "a/bc") is None


EXISTING = """
devices:
  - id: inv1
    name: Inverter 1
    template: ''
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.80, unit_id: 1}
    mqtt: {topic_prefix: site/inv1}
"""


def _body(prefix, did='newdev'):
    return {'id': did, 'name': 'New', 'enabled': False,
            'connection': {'protocol': 'tcp', 'host': '192.0.2.81', 'unit_id': 1},
            'mqtt': {'topic_prefix': prefix}}


@needs_tc
@pytest.mark.parametrize("prefix,word", [
    ("site/inv1", "inv1"),                   # a duplicate
    ("site/inv1/meter", "inv1"),             # under another device's
    ("site", "inv1"),                        # over another device's
    ("janitza/umg512", "gateway"),           # the gateway's own root (the primary's topics)
    ("janitza/umg512/x", "gateway"),
    ("site/+/x", "+"),
    ("site/x/", "/"),
])
def test_a_new_device_prefix_that_collides_is_refused_and_dry_run_says_the_same(tmp_path, prefix, word):
    cfg, client = make_app(tmp_path, extra_yaml=EXISTING)
    before = pathlib.Path(cfg.config_path).read_bytes()
    dry = client.post("/api/devices?dry_run=true", json=_body(prefix))
    real = client.post("/api/devices", json=_body(prefix))
    assert real.status_code == 422, real.text
    assert dry.status_code == real.status_code and dry.json() == real.json()
    assert 'mqtt.topic_prefix' in real.json()['detail']['fields']
    assert word in real.json()['detail']['fields']['mqtt.topic_prefix']
    assert pathlib.Path(cfg.config_path).read_bytes() == before
    assert cfg.get_device('newdev') is None


@needs_tc
def test_a_prefix_beside_the_others_is_accepted(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=EXISTING)
    r = client.post("/api/devices", json=_body("site/inv10"))     # site/inv1 is not a path prefix of it
    assert r.status_code == 200, r.text
    r = client.post("/api/devices", json={**_body(None, 'dflt'), 'mqtt': {}})   # the default pattern
    assert r.status_code == 200, r.text
    assert cfg.get_device('dflt').mqtt_topic_prefix == 'mbg/devices/dflt'


@needs_tc
def test_an_edit_never_judges_the_stored_prefix(tmp_path):
    # the prefix is fixed after creation: a stored one that breaks the rule
    # never blocks an edit of something else
    cfg, client = make_app(tmp_path, extra_yaml=EXISTING + """
  - id: twin
    name: Twin (same prefix, written by hand)
    template: ''
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.82, unit_id: 1}
    mqtt: {topic_prefix: site/inv1}
""")
    r = client.put("/api/devices/twin", json={'id': 'twin', 'name': 'Twin renamed', 'enabled': False,
                                              'connection': {'protocol': 'tcp', 'host': '192.0.2.82', 'unit_id': 1}})
    assert r.status_code == 200, r.text


@needs_tc
def test_a_component_whose_prefix_collides_is_refused(tmp_path):
    from tests.test_device_components import GX_YAML
    cfg, client = make_app(tmp_path, extra_yaml=GX_YAML + """
  - id: squatter
    name: Holds a component's prefix
    template: ''
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.83, unit_id: 1}
    mqtt: {topic_prefix: mbg/devices/gx-temperature-23}
""")
    r = client.post("/api/devices/gx/components", json={"type": "temperature", "instance": "23"})
    assert r.status_code == 422, r.text
    assert 'squatter' in r.text and 'mqtt.topic_prefix' in r.json()['detail']['fields']
    assert cfg.get_device('gx-temperature-23') is None
    r = client.post("/api/devices/gx/components", json={"type": "temperature", "instance": "24"})
    assert r.status_code == 200, r.text


ENDPOINT = {"id": "plant", "name": "Plant", "template": "eastron_sdm120", "enabled": False,
            "connection": {"protocol": "tcp", "host": "192.0.2.20", "port": 502}, "units": [1, 2]}


@needs_tc
@pytest.mark.parametrize("agg,ok", [("site/inv1", False), ("site", False), ("pv/#", False),
                                    ("janitza/umg512/plant", False), ("pv/plant", True)])
def test_an_installation_aggregate_prefix_is_checked(tmp_path, agg, ok):
    cfg, client = make_app(tmp_path, extra_yaml=EXISTING)
    r = client.post("/api/endpoints", json={**ENDPOINT, 'mqtt': {'aggregate_prefix': agg}})
    if ok:
        assert r.status_code == 200, r.text
    else:
        assert r.status_code == 422, r.text
        assert 'mqtt.aggregate_prefix' in r.json()['detail']['fields']
        assert cfg.get_raw_endpoint('plant') is None


@needs_tc
def test_an_installations_own_units_may_sit_under_its_root(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/endpoints", json={**ENDPOINT, 'mqtt': {'aggregate_prefix': 'pv',
                                                                  'topic_prefix': 'pv/units/${unit_id}'}})
    assert r.status_code == 200, r.text


@needs_tc
def test_stored_prefixes_that_break_the_rule_are_logged_once_never_refused(tmp_path, caplog):
    from multibus.api import create_api
    from tests.test_devices import write_config
    extra = """
devices:
  - id: dupe-a-77
    name: A
    template: ''
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.84, unit_id: 1}
    mqtt: {topic_prefix: same/place-77}
  - id: dupe-b-77
    name: B
    template: ''
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.85, unit_id: 1}
    mqtt: {topic_prefix: same/place-77}
"""
    caplog.set_level(logging.WARNING)
    cfg = write_config(tmp_path, extra_yaml=extra)
    assert {d.id for d in cfg.devices} >= {'dupe-a-77', 'dupe-b-77'}          # both load
    create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    hits = [r for r in caplog.records if 'same/place-77' in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in hits]
