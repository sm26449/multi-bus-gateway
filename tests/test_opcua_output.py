# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Which devices, and which of their values, the OPC UA server serves: an
`opcua:` output per device, per endpoint and per endpoint group, like MQTT
and InfluxDB. A device from before the block is on; one made since is off
until someone turns it on."""
from multibus.config import Config

from tests.test_devices import write_config
from tests.test_devices_api import make_app, needs_tc

GROUPS_YAML = """
devices:
  - id: old-meter
    name: Old meter
    connection: { protocol: tcp, host: 192.0.2.20, port: 502, unit_id: 1 }
  - id: chosen-meter
    name: Chosen meter
    connection: { protocol: tcp, host: 192.0.2.21, port: 502, unit_id: 1 }
    opcua: { enabled: true, fields: [power_active_total, frequency] }
endpoints:
  - id: pv
    name: PV
    opcua: { enabled: false }
    groups:
      - id: inverters
        connection: { protocol: tcp, host: 192.0.2.9, port: 502 }
        units: [1, 2]
        opcua: { enabled: true, fields: [power_active_total] }
      - id: site
        connection: { protocol: tcp, host: 192.0.2.9, port: 502 }
        units: [{unit_id: 0, id: pv-site}]
"""

DEVICE = {"id": "em24", "name": "EM24", "enabled": False,
          "connection": {"protocol": "tcp", "host": "192.0.2.10", "port": 502, "unit_id": 5},
          "mqtt": {"topic_prefix": "meters/em24"}, "influxdb": {"bucket": "em24"}}


def test_a_device_from_before_the_block_is_on_and_a_block_chooses_values(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=GROUPS_YAML)
    old = cfg.get_device("old-meter")
    assert old.opcua_enabled is True and old.opcua_fields == []          # every value, as before
    chosen = cfg.get_device("chosen-meter")
    assert chosen.opcua_enabled is True and chosen.opcua_fields == ["power_active_total", "frequency"]
    assert cfg.get_device("umg512").opcua_enabled is True                 # the primary too


def test_an_endpoint_group_overrides_its_endpoint(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=GROUPS_YAML)
    inv = [d for d in cfg.endpoint_devices("pv") if d.group_id == "inverters"]
    assert len(inv) == 2 and all(d.opcua_enabled and d.opcua_fields == ["power_active_total"] for d in inv)
    site = cfg.get_device("pv-site")
    assert site.opcua_enabled is False                                    # the endpoint's own choice


def test_the_setter_writes_where_the_choice_lives_and_it_persists(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=GROUPS_YAML)
    cfg.set_opcua_output("old-meter", False)
    cfg.set_opcua_output("pv-u1", True, ["power_active_total", "power_dc_mppt1"])   # its group
    cfg.set_opcua_output("pv-site", True)                                           # its group, not the endpoint
    cfg.set_opcua_output("umg512", True, ["frequency"])
    re = Config(str(tmp_path / "config.yaml"))
    assert re.get_device("old-meter").opcua_enabled is False
    assert re.get_device("pv-u2").opcua_fields == ["power_active_total", "power_dc_mppt1"]
    groups = {g["id"]: g for g in re.get_raw_endpoint("pv")["groups"]}
    assert groups["inverters"]["opcua"] == {"enabled": True, "fields": ["power_active_total", "power_dc_mppt1"]}
    assert groups["site"]["opcua"] == {"enabled": True} and re.get_raw_endpoint("pv")["opcua"] == {"enabled": False}
    assert re.get_device("umg512").opcua_fields == ["frequency"]


def test_the_primary_at_its_default_writes_nothing(tmp_path):
    cfg = write_config(tmp_path)
    cfg.set_opcua_output("umg512", True)
    assert "opcua_output" not in (tmp_path / "config.yaml").read_text()
    cfg.set_opcua_output("umg512", False)
    assert "opcua_output" in (tmp_path / "config.yaml").read_text()


@needs_tc
def test_a_new_device_is_off_until_turned_on_and_an_edit_keeps_the_choice(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert client.post("/api/devices", json=DEVICE).status_code == 200
    dev = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "em24")
    assert dev["sinks"]["opcua"]["enabled"] is False
    r = client.post("/api/devices/em24/opcua-output", json={"enabled": True, "fields": ["power", "power"]})
    assert r.status_code == 200 and r.json()["opcua_enabled"] is True and r.json()["opcua_fields"] == ["power"]
    edited = dict(DEVICE, name="EM24 renamed")
    assert client.put("/api/devices/em24", json=edited).status_code == 200
    re = Config(str(tmp_path / "config.yaml")).get_device("em24")
    assert re.opcua_enabled is True and re.opcua_fields == ["power"]
    dev = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "em24")
    assert dev["sinks"]["opcua"]["enabled"] is True and dev["sinks"]["opcua"]["fields"] == ["power"]


@needs_tc
def test_wrong_fields_are_refused(tmp_path):
    _cfg, client = make_app(tmp_path)
    client.post("/api/devices", json=DEVICE)
    for bad in ({"enabled": True, "fields": "power"}, {"enabled": True, "fields": [""]}, ["x"]):
        assert client.post("/api/devices/em24/opcua-output", json=bad).status_code == 422
    assert client.post("/api/devices/nope/opcua-output", json={"enabled": True}).status_code == 404


@needs_tc
def test_an_endpoint_unit_sets_its_group_and_the_group_form_keeps_it(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=GROUPS_YAML)
    r = client.post("/api/devices/pv-u1/opcua-output", json={"enabled": False})
    assert r.status_code == 200 and sorted(r.json()["applies_to"]) == ["pv-u1", "pv-u2"]
    ep = client.get("/api/endpoints/pv").json()
    body = {k: ep[k] for k in ("id", "name") if k in ep}
    body["groups"] = [{"id": "inverters", "connection": {"protocol": "tcp", "host": "192.0.2.9", "port": 502},
                       "units": [1, 2]},
                      {"id": "site", "connection": {"protocol": "tcp", "host": "192.0.2.9", "port": 502},
                       "units": [{"unit_id": 0, "id": "pv-site"}]}]
    assert client.put("/api/endpoints/pv", json=body).status_code == 200, client.put("/api/endpoints/pv", json=body).text
    re = Config(str(tmp_path / "config.yaml"))
    assert re.get_device("pv-u2").opcua_enabled is False
    assert re.get_raw_endpoint("pv")["opcua"] == {"enabled": False}


@needs_tc
def test_the_server_is_fed_the_configured_devices_with_their_choice(tmp_path):
    """The devices come from the configuration, not from what has started
    polling: a unit found seconds after the start is served from the start."""
    _cfg, client = make_app(tmp_path, extra_yaml=GROUPS_YAML)
    ctl = client.app.state.opcua
    items = {i[0]: i for i in ctl._devices()}
    assert "pv-site" not in items                                       # off
    assert {"umg512", "old-meter", "chosen-meter", "pv-u1", "pv-u2"} <= set(items)
    assert items["pv-u1"][7] == {"power_active_total"}
    assert items["old-meter"][7] is None                                # every value
    assert isinstance(items["umg512"][6], list)                         # what it declares
