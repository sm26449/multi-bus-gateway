# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""POST /api/can-interfaces/{name} (ADR-0046 L2; FINDINGS CANCTL-1): every field is checked
before the link is touched, taking a port out of listen-only obeys the master write switch,
and a port that carries an armed Virtual BMS is not cycled. `ip` is never run here."""
import types

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False

from multibus.routes import buses as buses_routes

pytestmark = pytest.mark.skipif(not _HAS_TC, reason="fastapi TestClient not installed")


@pytest.fixture()
def api(monkeypatch):
    from multibus.transports.can import can_ctl, can_interfaces
    itfs = [{"name": "can0", "up": True, "listen_only": True, "bitrate": 500000, "devices": ["bms-can"]},
            {"name": "can1", "up": True, "listen_only": True, "bitrate": 500000, "devices": []}]
    calls = []
    monkeypatch.setattr(can_interfaces, "interfaces", lambda **kw: [dict(i) for i in itfs])
    monkeypatch.setattr(can_interfaces, "devices_by_channel", lambda cfg: {})
    monkeypatch.setattr(can_ctl, "apply", lambda name, **kw: calls.append((name, kw)))
    cfg = types.SimpleNamespace(security=types.SimpleNamespace(allow_writes=True), devices=[])
    app = FastAPI()
    inst = types.SimpleNamespace(id="vb", channel="can1", armed=False, running=False)
    app.state.vbms_manager = types.SimpleNamespace(instances={"vb": inst})
    ctx = types.SimpleNamespace(config=cfg, registry=[], app=app, template_registry=None)
    app.include_router(buses_routes.build(ctx))
    c = TestClient(app)
    c.cfg, c.calls, c.inst = cfg, calls, inst
    return c


def test_a_valid_change_is_applied(api):
    r = api.post("/api/can-interfaces/can1", json={"mode": "transmit", "bitrate": 500000, "up": True})
    assert r.status_code == 200, r.text
    assert api.calls == [("can1", {"mode": "transmit", "bitrate": 500000, "up": True})]


@pytest.mark.parametrize("body", [
    {"bitrate": "fast"}, {"bitrate": 0}, {"bitrate": -500000}, {"bitrate": 123456},
    {"bitrate": 500000.5}, {"bitrate": True}, {"bitrate": [500000]},
    {"up": "yes"}, {"up": 1}, {"mode": "loud"}, {},
])
def test_a_bad_value_is_a_422_and_the_link_is_not_touched(api, body):
    r = api.post("/api/can-interfaces/can1", json=body)
    assert r.status_code == 422, (body, r.text)
    assert api.calls == []


def test_an_unknown_interface_is_a_404(api):
    assert api.post("/api/can-interfaces/can9", json={"up": True}).status_code == 404
    assert api.calls == []


def test_transmit_needs_the_master_write_switch(api):
    api.cfg.security.allow_writes = False
    r = api.post("/api/can-interfaces/can1", json={"mode": "transmit"})
    assert r.status_code == 409 and "allow_writes" in r.text and api.calls == []


def test_the_safe_direction_is_never_refused_for_the_write_switch(api):
    api.cfg.security.allow_writes = False
    assert api.post("/api/can-interfaces/can1", json={"mode": "listen_only"}).status_code == 200
    assert api.post("/api/can-interfaces/can1", json={"up": False}).status_code == 200
    assert len(api.calls) == 2


@pytest.mark.parametrize("body", [{"mode": "listen_only"}, {"bitrate": 250000}, {"up": False},
                                  {"mode": "transmit"}])
def test_a_port_with_an_armed_virtual_bms_is_not_cycled(api, body):
    api.inst.armed = True
    r = api.post("/api/can-interfaces/can1", json=body)
    assert r.status_code == 409 and "disarm" in r.text and api.calls == []
    assert api.post("/api/can-interfaces/can0", json={"up": True}).status_code == 200   # another port


def test_a_bitrate_change_on_a_bus_with_readers_needs_force(api):
    r = api.post("/api/can-interfaces/can0", json={"bitrate": 250000})
    assert r.status_code == 409 and "bms-can" in r.text and api.calls == []
    assert api.post("/api/can-interfaces/can0", json={"bitrate": 500000}).status_code == 200  # unchanged rate
    assert api.post("/api/can-interfaces/can0", json={"bitrate": 250000, "force": True}).status_code == 200
    assert len(api.calls) == 2


def test_the_reader_refusal_says_what_it_is_so_the_guided_fix_can_ask(api):
    """The arm dialog's bitrate fix (ui/js/app-core.js) reads `code` and `readers`, asks the
    operator, and repeats with force — R2D-CAN-1."""
    r = api.post("/api/can-interfaces/can0", json={"bitrate": 250000, "up": True})
    d = r.json()["detail"]
    assert r.status_code == 409 and d["code"] == "bus_has_readers"
    assert d["readers"] == ["bms-can"] and d["bitrate"] == 500000
