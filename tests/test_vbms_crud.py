# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Virtual BMS user-facing CRUD routes (ADR-0045): the port picker, source packs, profiles, and
create/edit/delete persisting to config.virtual_bms + reconfiguring the live manager. A light
harness (fake ctx, real VbmsManager + TemplateRegistry, monkeypatched CAN interfaces) exercises the
router without the full create_api device stack or the auth middleware."""
import types

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False

from multibus.catalog.device_template import TemplateRegistry
from multibus.outputs.vbms_manager import VbmsManager
from multibus.outputs.vbms_wiring import make_load_profile
from multibus.routes import vbms as vbms_routes

pytestmark = pytest.mark.skipif(not _HAS_TC, reason="fastapi TestClient not installed")

_FAKE_ITFS = [
    {"name": "can0", "up": True, "listen_only": True, "bitrate": 500000, "state": "ERROR-ACTIVE", "devices": []},
    {"name": "can1", "up": True, "listen_only": False, "bitrate": 500000, "state": "ERROR-ACTIVE", "devices": []},
]


class _Dev:
    def __init__(self, did, name, template, *, protocol=None, channel=None):
        self.id, self.name, self.template = did, name, template
        self.protocol = protocol
        self.sources = ([types.SimpleNamespace(can={"channel": channel})]
                        if channel is not None else None)


class _Cfg:
    def __init__(self):
        self.virtual_bms = []
        self.devices = [_Dev("pack1", "Pack 1", "seplos_bms_v2_tap"),
                        _Dev("pack2", "Pack 2", "seplos_bms_v2_tap")]
        self.security = types.SimpleNamespace(allow_writes=True)
        self.saves = 0

    def save_yaml_config(self):
        self.saves += 1


class _FakeTx:
    def __init__(self, channel): self.channel = channel
    def open(self): pass
    def send(self, *a): pass
    def close(self): pass


@pytest.fixture()
def client(monkeypatch):
    from multibus.transports.can import can_interfaces
    monkeypatch.setattr(can_interfaces, "interfaces", lambda **kw: list(_FAKE_ITFS))
    cfg = _Cfg()
    treg = TemplateRegistry()
    mgr = VbmsManager(cfg, lambda d, f: None, make_load_profile(treg), tx_factory=_FakeTx)
    ctx = types.SimpleNamespace(config=cfg, template_registry=treg)
    app = FastAPI()
    app.include_router(vbms_routes.build(ctx))
    app.state.vbms_manager = mgr
    app.state.vbms_command_bridge = types.SimpleNamespace(sync=lambda: None)
    c = TestClient(app)
    c.cfg, c.mgr = cfg, mgr
    return c


def _new(**over):
    body = {"id": "deye-vbms", "profile": "pylontech_can_inverter", "channel": "can1",
            "packs": ["pack1", "pack2"], "bounds": {"charge_current_limit": [0, 80]},
            "floor": {"charge_current_limit": 0}}
    body.update(over)
    return body


def test_ports_sources_profiles(client):
    ports = client.get("/api/vbms/ports").json()["ports"]
    assert {p["name"] for p in ports} == {"can0", "can1"}
    assert next(p for p in ports if p["name"] == "can0")["listen_only"] is True
    sources = client.get("/api/vbms/sources").json()["sources"]
    assert {s["id"] for s in sources} == {"pack1", "pack2"}                 # the battery devices
    profiles = {p["id"] for p in client.get("/api/vbms/profiles").json()["profiles"]}
    assert "pylontech_can_inverter" in profiles                            # an emulation profile


def test_create_persists_and_reaches_the_manager(client):
    r = client.post("/api/vbms", json=_new())
    assert r.status_code == 200 and r.json()["instance"] == "deye-vbms"
    assert client.cfg.saves == 1 and client.cfg.virtual_bms[0]["id"] == "deye-vbms"
    assert "deye-vbms" in client.mgr.instances and client.mgr.status()["deye-vbms"]["armed"] is False


def test_create_rejects_bad_input(client):
    assert client.post("/api/vbms", json=_new(channel="can9")).status_code == 422   # not an interface
    assert client.post("/api/vbms", json=_new(packs=["nope"])).status_code == 422   # not a pack
    assert client.post("/api/vbms", json=_new(profile="seplos_bms_v2_tap")).status_code == 422  # not emulator


def test_a_port_is_reserved_once_taken(client):
    assert client.post("/api/vbms", json=_new(id="a", channel="can1")).status_code == 200
    # can1 now has a publisher → the picker marks it reserved and a second create is refused
    ports = {p["name"]: p for p in client.get("/api/vbms/ports").json()["ports"]}
    assert ports["can1"]["availability"] == "reserved" and ports["can1"]["publisher"] == "a"
    r = client.post("/api/vbms", json=_new(id="b", channel="can1"))
    assert r.status_code == 422 and "already used by" in str(r.json())
    # editing 'a' still sees can1 as available (its own claim excluded)
    ep = {p["name"]: p for p in client.get("/api/vbms/ports?editing=a").json()["ports"]}
    assert ep["can1"]["availability"] == "available"


def test_edit_and_delete(client):
    client.post("/api/vbms", json=_new())
    r = client.patch("/api/vbms/deye-vbms", json={"floor": {"charge_current_limit": 10}})
    assert r.status_code == 200 and client.cfg.virtual_bms[0]["floor"]["charge_current_limit"] == 10
    r = client.delete("/api/vbms/deye-vbms")
    assert r.status_code == 200 and client.cfg.virtual_bms == [] and "deye-vbms" not in client.mgr.instances


def test_preview_endpoint_and_send_optional(client):
    client.post("/api/vbms", json=_new())
    pv = client.get("/api/vbms/deye-vbms/preview").json()
    assert pv["packs_total"] == 2 and "sends" in pv and pv["send_optional"] is True
    assert "0x370" in pv["frames"]                                     # the detail frame is on by default
    # turn optional detail frames off via the edit → the preview narrows
    assert client.patch("/api/vbms/deye-vbms", json={"fields": {"send_optional": False}}).status_code == 200
    pv2 = client.get("/api/vbms/deye-vbms/preview").json()
    assert "0x351" in pv2["frames"] and "0x370" not in pv2["frames"]
    assert client.cfg.virtual_bms[0]["fields"]["send_optional"] is False   # persisted


def test_create_refuses_external_bms_collision(client, monkeypatch):
    # a CAN device on can1 that decodes the Pylontech BMS frames (a real BMS on the bus)
    from multibus.transports.can import can_interfaces
    client.cfg.devices.append(_Dev("deye-bms", "Deye BMS link", "pylontech_can_inverter",
                                   protocol="can", channel="can1"))
    busy = [dict(i, devices=["deye-bms"]) if i["name"] == "can1" else i for i in _FAKE_ITFS]
    monkeypatch.setattr(can_interfaces, "interfaces", lambda **kw: list(busy))
    r = client.post("/api/vbms", json=_new(channel="can1"))
    assert r.status_code == 422 and "collide" in str(r.json())


def test_create_refuses_bitrate_mismatch(client, monkeypatch):
    from multibus.transports.can import can_interfaces
    slow = [dict(i, bitrate=250000) if i["name"] == "can1" else i for i in _FAKE_ITFS]
    monkeypatch.setattr(can_interfaces, "interfaces", lambda **kw: list(slow))
    r = client.post("/api/vbms", json=_new(channel="can1"))
    assert r.status_code == 422 and "bit/s" in str(r.json())


def test_create_refuses_circular_pack_on_output_bus(client):
    # a battery pack read over CAN on the very bus the Virtual BMS will transmit on
    client.cfg.devices.append(_Dev("can-pack", "CAN pack", "pylontech_can_inverter",
                                   protocol="can", channel="can1"))
    r = client.post("/api/vbms", json=_new(channel="can1", packs=["can-pack"]))
    assert r.status_code == 422 and "circular" in str(r.json())


def test_arm_refused_when_the_port_bitrate_drifts_from_the_profile(client, monkeypatch):
    # created on can1 @ 500k (passes save); the port is later brought up at 250k → arm is refused
    from multibus.transports.can import can_interfaces
    client.post("/api/vbms", json=_new(channel="can1"))
    slow = [dict(i, bitrate=250000) if i["name"] == "can1" else i for i in _FAKE_ITFS]
    monkeypatch.setattr(can_interfaces, "interfaces", lambda **kw: list(slow))
    r = client.post("/api/vbms/deye-vbms/arm")
    assert r.status_code == 409
    ids = [p["id"] for p in r.json()["detail"]["prerequisites"]]
    assert "can_bitrate" in ids
    fix = next(p for p in r.json()["detail"]["prerequisites"] if p["id"] == "can_bitrate")["fix"]
    assert fix["url"] == "/api/can-interfaces/can1" and fix["body"]["bitrate"] == 500000
    assert client.mgr.instances["deye-vbms"].armed is False          # never armed


def test_edit_and_delete_refused_while_armed(client):
    client.post("/api/vbms", json=_new())
    client.post("/api/vbms/deye-vbms/arm")                                 # allow_writes is true in the fixture
    assert client.mgr.instances["deye-vbms"].armed is True
    assert client.patch("/api/vbms/deye-vbms", json={"floor": {}}).status_code == 409
    assert client.delete("/api/vbms/deye-vbms").status_code == 409
    client.post("/api/vbms/deye-vbms/disarm")
    assert client.delete("/api/vbms/deye-vbms").status_code == 200         # allowed once disarmed
