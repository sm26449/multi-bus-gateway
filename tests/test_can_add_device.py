# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""A CAN device added, edited and tested from the UI's forms:
what the API accepts and refuses, the row the form opens from, and a Test that
says whether the interface exists, is up, is listen-only and what it heard —
on a receive-only socket, never a transmit."""
from types import SimpleNamespace

import pytest

from multibus import can_client
from tests.test_devices_api import make_app, needs_tc

CAN_YAML = """
devices:
  - id: bms-can
    name: Battery (CAN)
    template: pylontech_can_inverter
    connection: {protocol: can, carrier: socketcan, channel: can0, bitrate: 500000, stale_after_s: 5}
"""
NEW = {"id": "bank-can", "name": "Bank (CAN)", "enabled": False, "template": "pylontech_can_inverter",
       "connection": {"protocol": "can", "carrier": "socketcan", "channel": "can0",
                      "bitrate": 250000, "stale_after_s": 7}}


class FakeCarrier:
    """What a socketcan carrier offers: open, recv, close — and nothing to send."""

    def __init__(self, frames=(), open_error=None):
        self.frames, self.open_error = list(frames), open_error
        self.calls = []

    def open(self):
        self.calls.append("open")
        if self.open_error:
            raise OSError(100, self.open_error)

    def recv(self):
        self.calls.append("recv")
        return self.frames.pop(0) if self.frames else None

    def close(self):
        self.calls.append("close")


def _link(monkeypatch, *, exists=True, up=True, listen_only=True, frames=(), open_error=None):
    car = FakeCarrier(frames, open_error)
    monkeypatch.setattr(can_client, "link_state", lambda ch: {"exists": exists, "up": up})
    monkeypatch.setattr(can_client, "listen_only", lambda ch: listen_only)
    monkeypatch.setattr(can_client, "carrier_for", lambda cfg: car)
    return car


FRAMES = [(0x351, False, False, False, bytes(8)), (0x355, False, False, False, bytes(8)),
          (0x351, False, False, False, bytes(8))]


# ── create / edit ───────────────────────────────────────────────────────────

@needs_tc
def test_a_can_device_is_added_and_the_form_reopens_with_its_fields(tmp_path):
    _cfg, cl = make_app(tmp_path)
    r = cl.post("/api/devices", json=NEW)
    assert r.status_code == 200, r.text
    row = next(d for d in cl.get("/api/devices").json()["devices"] if d["id"] == "bank-can")
    assert row["protocol"] == "can"
    c = row["connection"]
    assert (c["carrier"], c["channel"], c["bitrate"], c["stale_after_s"]) == ("socketcan", "can0", 250000, 7)


@needs_tc
def test_an_edit_changes_one_field_and_keeps_the_rest(tmp_path):
    _cfg, cl = make_app(tmp_path, extra_yaml=CAN_YAML)
    r = cl.put("/api/devices/bms-can", json={"id": "bms-can", "template": "pylontech_can_inverter",
                                             "connection": {"protocol": "can", "channel": "can1"}})
    assert r.status_code == 200, r.text
    c = next(d for d in cl.get("/api/devices").json()["devices"] if d["id"] == "bms-can")["connection"]
    assert (c["channel"], c["bitrate"], c["stale_after_s"]) == ("can1", 500000, 5)


@needs_tc
@pytest.mark.parametrize("conn, field", [
    ({"carrier": "bridge"}, "connection.carrier"),
    ({"channel": ""}, "connection.channel"),
    ({"channel": "can0; reboot"}, "connection.channel"),
    ({"bitrate": 7}, "connection.bitrate"),
    ({"bitrate": "fast"}, "connection.bitrate"),
    ({"stale_after_s": 0}, "connection.stale_after_s"),
    ({"stale_after_s": 99999}, "connection.stale_after_s"),
])
def test_what_a_can_connection_may_not_be(tmp_path, conn, field):
    _cfg, cl = make_app(tmp_path)
    body = {**NEW, "connection": {**NEW["connection"], **conn}}
    r = cl.post("/api/devices", json=body)
    assert r.status_code == 422, r.text
    assert field in r.text, r.text


@needs_tc
def test_a_can_template_on_a_modbus_device_is_refused(tmp_path):
    _cfg, cl = make_app(tmp_path)
    r = cl.post("/api/devices", json={**NEW, "connection": {"protocol": "tcp", "host": "192.0.2.5"}})
    assert r.status_code == 422 and "CAN map" in r.text, r.text


# ── Test ────────────────────────────────────────────────────────────────────

@needs_tc
def test_test_says_a_missing_interface_plainly(tmp_path):
    # the test container has no CAN interface at all, as the production host
    _cfg, cl = make_app(tmp_path)
    r = cl.post("/api/devices/test", json={"connection": {**NEW["connection"], "channel": "mbgnocan9"},
                                           "template": "pylontech_can_inverter"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is False
    assert d["can"]["exists"] is False and d["can"]["frames"] == 0
    assert "no CAN interface 'mbgnocan9'" in d["message"], d["message"]


@needs_tc
def test_test_reports_listen_only_and_what_it_heard(tmp_path, monkeypatch):
    car = _link(monkeypatch, frames=FRAMES)
    _cfg, cl = make_app(tmp_path)
    d = cl.post("/api/devices/test", json={"connection": NEW["connection"]}).json()
    assert d["ok"] is True, d
    assert d["can"]["exists"] and d["can"]["up"] and d["can"]["listen_only"] is True
    assert d["can"]["frames"] == 3 and d["can"]["ids"] == {"0x351": 2, "0x355": 1}
    assert "3 frames" in d["message"] and "listen-only on" in d["message"], d["message"]
    # it only opened, listened and closed: nothing else is there to call
    assert set(car.calls) == {"open", "recv", "close"} and car.calls[-1] == "close"


@needs_tc
def test_test_warns_when_the_interface_is_not_listen_only(tmp_path, monkeypatch):
    _link(monkeypatch, listen_only=False, frames=FRAMES[:1])
    _cfg, cl = make_app(tmp_path)
    d = cl.post("/api/devices/test", json={"connection": NEW["connection"]}).json()
    assert d["can"]["listen_only"] is False
    assert "NOT listen-only" in d["message"] and "listen-only on" in d["message"], d["message"]


@needs_tc
def test_test_says_a_down_interface_and_a_silent_bus(tmp_path, monkeypatch):
    _cfg, cl = make_app(tmp_path)
    _link(monkeypatch, up=False)
    d = cl.post("/api/devices/test", json={"connection": NEW["connection"]}).json()
    assert d["ok"] is False and d["can"]["up"] is False and "down" in d["message"], d
    _link(monkeypatch)
    d = cl.post("/api/devices/test", json={"connection": NEW["connection"]}).json()
    assert d["ok"] is False and d["can"]["frames"] == 0 and "silent" in d["message"], d


@needs_tc
def test_test_refuses_a_bad_can_connection(tmp_path):
    _cfg, cl = make_app(tmp_path)
    r = cl.post("/api/devices/test", json={"connection": {"protocol": "can", "carrier": "bridge",
                                                          "channel": "can0"}})
    assert r.status_code == 422 and "connection.carrier" in r.text, r.text


@needs_tc
def test_a_saved_can_device_is_tested_on_its_own_interface(tmp_path, monkeypatch):
    _link(monkeypatch, frames=FRAMES)
    _cfg, cl = make_app(tmp_path, extra_yaml=CAN_YAML)
    d = cl.post("/api/devices/bms-can/test").json()
    assert d["ok"] is True and d["can"]["channel"] == "can0" and d["can"]["frames"] == 3, d


# ── the counters in the list and on the status page ────────────────────────

class RunningCan:
    def get_stats(self):
        return {"connected": True, "successful_reads": 9, "failed_reads": 1, "poll_rate": None,
                "staleness_age_s": 0.4, "error_counts": {"unknown_frames": 4, "decode_errors": 1, "error_frames": 0},
                "can": {"carrier": "socketcan", "channel": "can0", "bitrate": 500000, "frames": 14,
                        "matched_frames": 10, "unknown_frames": 4, "unknown_ids": {"0x123": 4},
                        "decode_errors": 1, "failed_frames": 1, "listen_only": True, "error_frames": 0,
                        "open_error": None}}

    def data_health(self, *a):
        return {"status": "ok"}


@needs_tc
def test_the_list_and_the_status_page_show_the_can_counters(tmp_path):
    from fastapi.testclient import TestClient

    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml=CAN_YAML)
    devices = [(d, RunningCan() if d.id == "bms-can" else None) for d in cfg.devices]
    app, _ = create_api(cfg, None, None, None, devices=devices)
    cl = TestClient(app, raise_server_exceptions=False)
    row = next(d for d in cl.get("/api/devices").json()["devices"] if d["id"] == "bms-can")
    want = {"frames": 14, "unknown_frames": 4, "unknown_ids": 1, "decode_errors": 1,
            "listen_only": True, "error_frames": 0}
    assert {k: row["can"][k] for k in want} == want, row.get("can")
    st = next(d for d in cl.get("/api/status").json()["devices"] if d["id"] == "bms-can")
    assert {k: st["can"][k] for k in want} == want, st.get("can")


def test_hear_needs_no_interface_to_say_it_is_missing():
    h = can_client.hear({"carrier": "socketcan", "channel": "mbgnocan9"}, seconds=0.1)
    assert h["exists"] is False and h["frames"] == 0
    ok, msg = can_client.hear_verdict(h)
    assert ok is False and "no CAN interface" in msg


def test_hear_stops_after_its_window(monkeypatch):
    now = SimpleNamespace(t=0.0)

    def clock():
        now.t += 0.5
        return now.t
    car = _link(monkeypatch, frames=FRAMES * 10)
    h = can_client.hear({"channel": "can0"}, seconds=2.0, clock=clock)
    assert 0 < h["frames"] < 30 and car.calls[-1] == "close"
