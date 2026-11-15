# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""§3 Installations — E1 characterization of POST /api/endpoints validation.

§3 finishes the shared-validation work (ADR-0040): D2c moves the command and
channel validators into devices/validation.py (removing the cross-injection),
and E3 makes the endpoint connection/claim checks compose §1's shared builder.
This pins what those must keep identical — the accept/reject status and the key
error message for a representative set, driven through the real HTTP route:
connection, units, template, the command and channel schemas, and duplicate id.
Captured on main @ 6ef1d34 (pre-D2c/E3).
"""
from __future__ import annotations

import pytest

from tests.test_endpoints import ENDPOINT_BODY, make_app


@pytest.fixture
def client(tmp_path):
    _cfg, c = make_app(tmp_path)
    return c


def _errors(r):
    j = r.json()
    d = j.get("detail") if isinstance(j.get("detail"), dict) else j
    return d.get("errors") or []


def test_valid_endpoint_is_created(client):
    assert client.post("/api/endpoints", json=ENDPOINT_BODY).status_code == 200


def test_duplicate_id_is_rejected(client):
    assert client.post("/api/endpoints", json=ENDPOINT_BODY).status_code == 200
    r = client.post("/api/endpoints", json=ENDPOINT_BODY)
    assert r.status_code == 422
    assert "id: 'sdm-endpoint' already exists" in _errors(r)


def test_empty_units_rejected(client):
    r = client.post("/api/endpoints", json=dict(ENDPOINT_BODY, id="e2", units=[]))
    assert r.status_code == 422
    assert "units: at least one valid unit id (0..255) is required" in _errors(r)


def test_unknown_template_rejected(client):
    r = client.post("/api/endpoints", json=dict(ENDPOINT_BODY, id="e3", template="no_such_template_xyz"))
    assert r.status_code == 422
    assert "template: 'no_such_template_xyz' not found" in _errors(r)


def test_serial_on_endpoint_connection_rejected(client):
    r = client.post("/api/endpoints", json=dict(
        ENDPOINT_BODY, id="e4", connection={"protocol": "rtu", "serial_port": "/dev/ttyUSB0"}))
    assert r.status_code == 422
    errs = _errors(r)
    assert "connection.protocol: must be one of tcp, udp, rtu-tcp, ascii-tcp, rtu_tap" in errs
    assert "connection.host: required" in errs


def test_bad_command_name_rejected(client):
    # exercises _validate_commands (moved to the shared module by D2c)
    r = client.post("/api/endpoints", json=dict(
        ENDPOINT_BODY, id="e5", commands=[{"name": "Bad Name!", "enabled": True}]))
    assert r.status_code == 422
    assert "commands[0].name: use a-z 0-9 _ (1-32 chars, starts with a letter)" in _errors(r)


def test_bad_channel_shape_rejected(client):
    # exercises _validate_channels (moved to the shared module by D2c)
    r = client.post("/api/endpoints", json=dict(
        ENDPOINT_BODY, id="e6", channels={"foo": {"min_interval_s": "notanumber"}}))
    assert r.status_code == 422
    errs = _errors(r)
    assert "channels.foo.min_interval_s: 0..86400" in errs
    assert "channels.foo: not a channel or a command of template 'eastron_sdm120'" in errs
