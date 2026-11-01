# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
# pytest fixtures (slave, fake_serial, seplos_port) imported from a sibling test
# module are named as test parameters: ruff takes that for a redefinition
# ruff: noqa: F811
"""L3.7 second safety verification (2026-11-01):

1. a scan slows the bus's own polling: on a shared bus the 202 answer says
   by how much (per read and per poll cycle); a bus whose devices an ARMED
   rule reads or drives is refused (409) unless ``confirm_slow``, the rules
   and the delay named; audited;
2. fail closed: a bus list that cannot be read refuses the scan (503) —
   never a scan with no address known."""
import json
from types import SimpleNamespace

from tests.test_bus_scan import _wait
from tests.test_bus_scan_review import _serial_holder, _serial_scan
from tests.test_devices_api import needs_tc
from tests.test_preview import _app, _tcp, sdm_words, slave  # noqa: F401


def _armed(client, rules):
    client.app.state.rules_runtime = SimpleNamespace(armed_devices=lambda: rules)


@needs_tc
def test_a_shared_scan_says_how_much_it_slows_the_bus(tmp_path, slave, monkeypatch):
    _s, client, running, tty = _serial_holder(tmp_path, slave, monkeypatch, {1: sdm_words()}, timeout=1)
    _armed(client, {"other-rule": {"elsewhere"}})
    try:
        r = client.post("/api/bus-scan", json=_serial_scan(tty, **{"from": 2, "to": 2, "timeout": 0.3}))
        assert r.status_code == 202, r.text
        d = r.json()
        assert d["slow"]["devices"] == ["rs1"] and d["slow"]["per_read_s"] == 1.3
        assert d["slow"]["reads_per_cycle"] >= 1 and d["slow"]["rules"] == []
        assert any("may wait up to 1.3 s" in w for w in d["warnings"]), d
        _wait(client, d["job"])
    finally:
        running.disconnect()


@needs_tc
def test_an_armed_rule_on_the_bus_refuses_the_scan_unless_confirmed(tmp_path, slave, monkeypatch):
    s, client, running, tty = _serial_holder(tmp_path, slave, monkeypatch, {1: sdm_words()}, timeout=1)
    _armed(client, {"ov-u1": {"rs1"}, "other": {"pv-9"}})
    try:
        before = len(s.log)
        r = client.post("/api/bus-scan", json=_serial_scan(tty, **{"from": 2, "to": 3, "timeout": 0.3}))
        assert r.status_code == 409, r.text
        det = r.json()["detail"]
        assert det["rules"] == ["ov-u1"] and "confirm_slow" in det["errors"][0]
        assert det["slow"]["worst_per_cycle_s"] >= 1.3 and "ov-u1" in det["errors"][0]
        assert len(s.log) == before                               # nothing sent
        r = client.post("/api/bus-scan", json=_serial_scan(tty, **{"from": 2, "to": 3, "timeout": 0.3,
                                                                    "confirm_slow": True}))
        assert r.status_code == 202, r.text
        assert any("ov-u1" in w and "confirmed" in w for w in r.json()["warnings"])
        _wait(client, r.json()["job"])
    finally:
        running.disconnect()
    lines = [e for e in client.app.state.ctx.audit_log.recent(50) if e.get("action") == "bus-scan"]
    details = [json.loads(e["detail"]) for e in lines]
    assert any(e["status"].startswith("refused (409)") and d.get("rules") == ["ov-u1"]
               for e, d in zip(lines, details))
    assert any(d.get("confirm_slow") is True and d.get("slow", {}).get("rules") == ["ov-u1"] for d in details)


@needs_tc
def test_rules_that_cannot_be_read_refuse_like_an_armed_one(tmp_path, slave, monkeypatch):
    _s, client, running, tty = _serial_holder(tmp_path, slave, monkeypatch, {1: sdm_words()})

    def broken():
        raise RuntimeError("rules unreadable")
    client.app.state.rules_runtime = SimpleNamespace(armed_devices=broken)
    try:
        r = client.post("/api/bus-scan", json=_serial_scan(tty, **{"from": 2, "to": 2}))
        assert r.status_code == 409 and "could not be read" in r.text, r.text
    finally:
        running.disconnect()


@needs_tc
def test_the_runtime_names_the_devices_its_armed_rules_read_and_drive(tmp_path):
    from tests.test_rules_api import _Inverter, _rule_app
    _cfg, _app_, client, _clock, rt = _rule_app(tmp_path, {"pv-u1": _Inverter(sf=-2)}, mode="armed")
    assert rt.armed_devices() == {"ov-u1": {"pv-u1"}}
    assert client.put("/api/rules/ov-u1", json={**__import__("tests.test_rules_api", fromlist=["OV"]).OV,
                                                "mode": "shadow"}).status_code == 200
    assert rt.armed_devices() == {}                            # a shadow rule drives nothing


@needs_tc
def test_a_bus_list_that_cannot_be_read_refuses_the_scan(tmp_path, slave, monkeypatch):
    from multibus import buses
    s = slave(units={1: sdm_words()})
    _cfg, client = _app(tmp_path)

    def broken(*a, **k):
        raise RuntimeError("the bus list broke")
    monkeypatch.setattr(buses, "buses", broken)
    r = client.post("/api/bus-scan", json={"connection": _tcp(s.port), "from": 1, "to": 2, "confirm_open": True})
    assert r.status_code == 503 and "cannot read which addresses are in use" in r.text, r.text
    assert s.connections == 0 and s.log == []
