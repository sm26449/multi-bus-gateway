# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""QA-R1-05: an installation made by POST, its group saved twice from the
Edit group dialog, says "unchanged" the second time (installations_step3_e2e
"Edit group: saved again, unchanged"). The body is the one the page sends."""
from tests.test_devices_api import make_app, needs_tc

CREATE = {"id": "e2ebank", "name": "E2E bank", "enabled": False, "template": "fronius_sunspec_meter",
          "connection": {"protocol": "tcp", "host": "127.0.0.1", "port": 15504},
          "groups": [{"id": "units", "role": "meter", "units": [1, 2]}]}
# what the Edit group dialog sends, untouched (captured from the page)
PAGE = {"id": "e2ebank", "name": "E2E bank", "enabled": False,
        "connection": {"protocol": "tcp", "host": "127.0.0.1", "port": 15504},
        "template": "fronius_sunspec_meter", "units": [1, 2],
        "groups": [{"id": "units", "role": "meter", "enabled": True, "units": [1, 2]}]}


@needs_tc
def test_an_untouched_group_save_is_unchanged_the_second_time(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=CREATE).status_code == 200
    first = client.put("/api/endpoints/e2ebank", json=PAGE)
    assert first.status_code == 200, first.text
    second = client.put("/api/endpoints/e2ebank", json=PAGE)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "unchanged"


@needs_tc
def test_the_same_under_the_gateway_scope(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/config/identity", json={"gateway_id": "site-a", "identity_scope": "gateway"})
    assert r.status_code == 200, r.text
    assert client.post("/api/endpoints", json=CREATE).status_code == 200
    assert client.put("/api/endpoints/e2ebank", json=PAGE).status_code == 200
    assert client.put("/api/endpoints/e2ebank", json=PAGE).json()["status"] == "unchanged"
