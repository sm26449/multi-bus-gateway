# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""What the device page (lot 8a) asks of the server: the kinds a device that
IS its meters may be, said by the gateway (never a meter, never `other`),
and a device's own thresholds apart from the defaults, so an editor sends
back only what is the device's."""
from tests.test_devices_api import make_app, needs_tc
from tests.test_meter_links import IN, OUT, _devices


def test_the_meter_roles_name_the_kinds_a_meter_only_device_may_be():
    from multibus import meter_links
    k = meter_links.catalog()['meter_only_kinds']
    assert k['first'] == ['ups', 'charger', 'heat_pump', 'drive']
    every = k['first'] + k['more']
    assert 'meter' not in every and 'other' not in every
    assert len(every) == len(set(every)) and {'inverter', 'battery', 'site', 'sensor'} <= set(k['more'])


@needs_tc
def test_a_meter_only_device_is_never_a_meter(tmp_path):
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client, ids=("em-in", "em-out"))
    body = {"id": "ups", "name": "UPS", "connection": {"protocol": "none"}, "meters": [IN, OUT]}
    r = client.post("/api/devices", json={**body, "kind": "meter"})
    assert r.status_code == 422 and "kind" in r.json()["detail"]["fields"], r.text
    assert client.post("/api/devices", json={**body, "kind": "ups"}).status_code in (200, 201)


@needs_tc
def test_the_view_says_a_devices_own_thresholds_apart_from_the_defaults(tmp_path):
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client, ids=("em-in", "em-out"))
    r = client.post("/api/devices", json={"id": "ups", "name": "UPS", "kind": "ups",
                                          "connection": {"protocol": "none"}, "meters": [IN, OUT]})
    assert r.status_code in (200, 201), r.text
    row = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "ups")
    assert row["meter_options_own"] == {} and row["meter_options"]["min_load_w"] == 100.0
    assert client.put("/api/devices/ups/meters",
                      json={"meters": [IN, OUT], "options": {"min_load_w": 250}}).status_code == 200
    row = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "ups")
    assert row["meter_options_own"] == {"min_load_w": 250.0}
    assert row["meter_options"]["max_skew_s"] is None          # the default, never sent back


@needs_tc
def test_a_meter_only_device_cannot_be_refiled_as_a_meter_or_derived(tmp_path):
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client, ids=("em-in", "em-out"))
    assert client.post("/api/devices", json={"id": "ups", "name": "UPS", "kind": "ups",
                                             "connection": {"protocol": "none"}, "meters": [IN, OUT]}).status_code in (200, 201)
    for bad in ("meter", None, "other", "toaster"):
        r = client.patch("/api/devices/ups/organise", json={"kind": bad})
        assert r.status_code == 422 and "kind" in r.json()["detail"]["fields"], (bad, r.text)
    r = client.patch("/api/devices/ups/organise", json={"kind": "charger"})
    assert r.status_code == 200 and r.json()["kind"] == "charger", r.text
