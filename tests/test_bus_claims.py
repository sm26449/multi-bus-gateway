# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""A listened bus is never transmitted on (ADR-0025 decisions 1 and 5,
FINDINGS ADD-9, ADD-13).

Listening is a claim on the BUS, not a property of one device: a tap, a
Seplos V2 device in listen mode or a device whose template only listens
holds the bus, and every path that would ask a question there — a Test,
a unit sweep, a SunSpec walk — is refused with 409 and the listeners named.
The bus is the canonical serial path (a by-id link is the same tty) or the
lower-cased host:port after a bridge is resolved."""
import os

from tests.test_devices_api import make_app, needs_tc


def _ttys(tmp_path):
    """A tty stand-in and a /dev/serial/by-id style link to it."""
    tty = tmp_path / "ttySC0"
    tty.write_text("")
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    alias = by_id / "usb-FTDI_RS485_A1B2-if00-port0"
    os.symlink(tty, alias)
    tty2 = tmp_path / "ttySC1"
    tty2.write_text("")
    return str(tty), str(alias), str(tty2)


def _yaml(tty, tty2, *, tap_enabled=True):
    return f"""
bridges:
  - id: ws
    name: Panel
    type: rtu_transparent
    host: 192.0.2.60
    ports: [{{port: 4196}}, {{port: 4197}}]
devices:
  - id: tap1
    enabled: {str(tap_enabled).lower()}
    connection: {{ protocol: rtu_tap, serial_port: {tty}, unit_id: 1 }}
  - id: sv2
    enabled: true
    connection: {{ protocol: seplos_v2, serial_port: {tty2}, unit_id: 1, mode: listen }}
  - id: brtap
    enabled: {str(tap_enabled).lower()}
    connection: {{ protocol: rtu_tap, bridge: ws, bridge_port: 4196, unit_id: 2 }}
  - id: poller
    enabled: false
    connection: {{ protocol: rtu, serial_port: {tty}, unit_id: 5, timeout: 0.2 }}
"""


def _app(tmp_path, **kw):
    tty, alias, tty2 = _ttys(tmp_path)
    cfg, client = make_app(tmp_path, extra_yaml=_yaml(tty, tty2, **kw))
    return cfg, client, tty, alias, tty2


def _refused(r, *ids):
    assert r.status_code == 409, (r.status_code, r.text)
    det = r.json()["detail"]
    assert set(ids) <= set(det["listeners"]), det
    assert all(i in " ".join(det["errors"]) for i in ids), det


# ── the claim itself ─────────────────────────────────────────────────────
def test_the_bus_is_the_canonical_path_or_the_lowercased_host_and_port(tmp_path):
    from multibus.bus_claims import bus_of, canonical_serial
    tty, alias, _tty2 = _ttys(tmp_path)
    assert canonical_serial(alias) == os.path.realpath(tty) == canonical_serial(f" {tty} ")
    assert canonical_serial(" /dev/ttyNOPE9 ") == "/dev/ttyNOPE9"        # not there (yet): the string
    assert bus_of("rtu", {"serial_port": alias}) == bus_of("rtu_tap", {"serial_port": tty})
    assert bus_of("seplos_v2", {"serial_port": alias}) == os.path.realpath(tty)
    assert bus_of("tcp", {"host": "GW.Local", "port": 502}) == "gw.local:502"
    assert bus_of("rtu-tcp", {"host": "192.0.2.60", "port": "4196"}) == "192.0.2.60:4196"
    assert bus_of("http", {"url": "http://x"}) == "" and bus_of("can", {"channel": "can0"}) == ""


@needs_tc
def test_who_listens_taps_seplos_listeners_and_listening_templates_enabled_only(tmp_path):
    from multibus.bus_claims import listeners
    tty, alias, tty2 = _ttys(tmp_path)
    cfg, client = make_app(tmp_path, extra_yaml=_yaml(tty, tty2) + """
  - id: v3tap
    enabled: true
    template: seplos_bms_v3_rtu_tap
    connection: { protocol: rtu, serial_port: /dev/ttyV3, unit_id: 1 }
  - id: sv2poll
    enabled: true
    connection: { protocol: seplos_v2, serial_port: /dev/ttySV2POLL, unit_id: 1, mode: poll }
  - id: offtap
    enabled: false
    connection: { protocol: rtu_tap, serial_port: /dev/ttyOFF, unit_id: 1 }
""")
    tr = client.app.state.template_registry
    assert listeners(cfg, os.path.realpath(alias), tr) == ["tap1"]
    assert listeners(cfg, tty2, tr) == ["sv2"]
    assert listeners(cfg, "192.0.2.60:4196", tr) == ["brtap"]
    assert listeners(cfg, "192.0.2.60:4197", tr) == []
    assert listeners(cfg, "/dev/ttyV3", tr) == ["v3tap"]       # its template only listens
    assert listeners(cfg, "/dev/ttySV2POLL", tr) == []         # polled, not listened to
    assert listeners(cfg, "/dev/ttyOFF", tr) == []             # disabled: nobody listens


# ── every path that transmits is refused ─────────────────────────────────
@needs_tc
def test_an_adhoc_test_is_refused_on_a_tapped_tty_through_its_by_id_alias(tmp_path):
    _cfg, client, _tty, alias, _tty2 = _app(tmp_path)
    r = client.post("/api/devices/test", json={"connection": {
        "protocol": "rtu", "serial_port": alias, "unit_id": 3, "timeout": 0.2}})
    _refused(r, "tap1")
    assert "only listens" in r.text


@needs_tc
def test_a_saved_devices_test_is_refused_on_a_listened_bus(tmp_path):
    _cfg, client, *_ = _app(tmp_path)
    _refused(client.post("/api/devices/poller/test"), "tap1")


@needs_tc
def test_the_listening_devices_own_test_still_listens(tmp_path):
    _cfg, client, *_ = _app(tmp_path)
    r = client.post("/api/devices/tap1/test")
    assert r.status_code == 200 and r.json()["ok"] is False and "listening" in r.json()["message"]
    r = client.post("/api/devices/sv2/test")
    assert r.status_code == 200


@needs_tc
def test_modbus_and_seplos_polls_are_refused_on_a_seplos_listened_bus(tmp_path):
    _cfg, client, _tty, _alias, tty2 = _app(tmp_path)
    r = client.post("/api/devices/test", json={"connection": {
        "protocol": "rtu", "serial_port": tty2, "unit_id": 3, "timeout": 0.2}})
    _refused(r, "sv2")
    r = client.post("/api/devices/test", json={"connection": {
        "protocol": "seplos_v2", "serial_port": tty2, "unit_id": 2, "mode": "poll", "timeout": 0.2}})
    _refused(r, "sv2")


@needs_tc
def test_a_seplos_poll_is_refused_on_a_tapped_tty(tmp_path):
    _cfg, client, _tty, alias, _tty2 = _app(tmp_path)
    r = client.post("/api/devices/test", json={"connection": {
        "protocol": "seplos_v2", "serial_port": alias, "unit_id": 2, "mode": "poll", "timeout": 0.2}})
    _refused(r, "tap1")


@needs_tc
def test_tcp_test_unit_sweeps_and_sunspec_walk_are_refused_on_a_bridge_tapped_port(tmp_path):
    _cfg, client, _tty, alias, _tty2 = _app(tmp_path)
    r = client.post("/api/devices/test", json={"connection": {
        "protocol": "tcp", "host": "192.0.2.60", "port": 4196, "unit_id": 1, "timeout": 0.2}})
    _refused(r, "brtap")
    r = client.post("/api/devices/test", json={"connection": {
        "bridge": "ws", "bridge_port": 4196, "unit_id": 9, "timeout": 0.2}})
    _refused(r, "brtap")
    r = client.post("/api/discover/modbus/units", json={
        "protocol": "tcp", "host": "192.0.2.60", "port": 4196, "unit_start": 1, "unit_end": 2, "timeout": 0.1})
    _refused(r, "brtap")
    r = client.post("/api/discover/modbus/units", json={
        "protocol": "rtu", "serial_port": alias, "unit_start": 1, "unit_end": 2, "timeout": 0.1})
    _refused(r, "tap1")
    r = client.post("/api/discover/sunspec", json={"host": "192.0.2.60", "port": 4196, "unit_id": 1})
    _refused(r, "brtap")


@needs_tc
def test_the_bridge_port_scan_names_the_listeners_through_the_same_claim(tmp_path):
    _cfg, client, *_ = _app(tmp_path)
    r = client.post("/api/bridges/ws/ports/4196/scan", json={})
    assert r.status_code == 409 and "brtap" in " ".join(r.json()["detail"]["errors"])
    assert client.post("/api/bridges/ws/probe", json={"port": 4196, "unit_id": 1}).status_code == 409


@needs_tc
def test_a_disabled_listener_holds_nothing(tmp_path):
    _cfg, client, *_ = _app(tmp_path, tap_enabled=False)
    r = client.post("/api/devices/test", json={"connection": {
        "protocol": "tcp", "host": "192.0.2.60", "port": 4196, "unit_id": 1, "timeout": 0.2}})
    assert r.status_code == 200, r.text


# ── ADD-13: one tty under two names is one line ──────────────────────────
@needs_tc
def test_a_polled_device_on_the_by_id_alias_of_a_tapped_tty_is_refused(tmp_path):
    _cfg, client, _tty, alias, _tty2 = _app(tmp_path)
    r = client.post("/api/devices", json={"id": "m9", "enabled": False, "template": "eastron_sdm120",
                                          "connection": {"protocol": "rtu", "serial_port": alias, "unit_id": 9}})
    assert r.status_code == 422 and "polled or tapped" in r.text and "tap1" in r.text
