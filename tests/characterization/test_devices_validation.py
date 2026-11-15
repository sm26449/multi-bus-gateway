# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""§1 Devices — D1 characterization of POST /api/devices validation outcomes.

§1 D2 folds the duplicated device-payload checks into one validation service.
This pins what that consolidation must preserve: the accept/reject status and
the exact error body (``errors`` list + ``fields`` map) for a representative
set of payloads, driven through the real HTTP route. The ADR-0030 etag write
path is already golden-pinned by tests/test_device_write_etag.py (a route-table
walk), so it is not re-pinned here.
"""
from __future__ import annotations

import pytest

from tests.test_devices_api import make_app

EXISTING = """
devices:
  - id: existing
    template: eastron_sdm630
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.50, unit_id: 1}
"""

VALID = {
    "valid_tcp": {"id": "d_tcp", "name": "TCP", "template": "eastron_sdm630", "enabled": False,
                  "connection": {"protocol": "tcp", "host": "192.0.2.1", "port": 502, "unit_id": 1}},
    "valid_http": {"id": "d_http", "name": "HTTP", "template": "fronius_solar_api_inverter",
                   "enabled": False, "connection": {"protocol": "http", "url": "http://192.0.2.2/x"}},
}

# payload -> the one field:message the validator rejects it with (status 422)
REJECT = {
    "bad_protocol": (
        {"id": "d_bad", "template": "eastron_sdm630",
         "connection": {"protocol": "carrier-pigeon", "host": "192.0.2.3"}},
        "connection.protocol",
        "must be one of tcp, udp, rtu-tcp, ascii-tcp, rtu, ascii, rtu_tap, "
        "seplos_v2, seplos_v2-tcp, http, mqtt, can"),
    "missing_connection": (
        {"id": "d_noconn", "template": "eastron_sdm630"},
        "connection.host", "required for Modbus TCP"),
    "dup_id": (
        {"id": "existing", "template": "eastron_sdm630",
         "connection": {"protocol": "tcp", "host": "192.0.2.4", "unit_id": 2}},
        "id", "'existing' already exists"),
    "bad_id": (
        {"id": "Bad Id!!", "template": "eastron_sdm630",
         "connection": {"protocol": "tcp", "host": "192.0.2.5"}},
        "id", "use a-z 0-9 - _ (2-64 chars, starts alphanumeric)"),
    "unknown_template": (
        {"id": "d_tpl", "template": "no_such_template_xyz",
         "connection": {"protocol": "tcp", "host": "192.0.2.6"}},
        "template", "'no_such_template_xyz' not found"),
}


@pytest.fixture
def client(tmp_path):
    _cfg, c = make_app(tmp_path, extra_yaml=EXISTING)
    return c


@pytest.mark.parametrize("name", list(VALID))
def test_valid_device_payloads_are_accepted(client, name):
    r = client.post("/api/devices", json=VALID[name])
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("name", list(REJECT))
def test_invalid_device_payloads_rejected_with_field_message(client, name):
    payload, field, message = REJECT[name]
    r = client.post("/api/devices", json=payload)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]          # HTTPException(detail={errors, fields})
    assert detail["fields"].get(field) == message, detail
    assert f"{field}: {message}" in detail["errors"], detail
