# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Test on a serial port that does not exist says so (FINDINGS QA-D-10).

The saved-device Test of a poller whose port is missing went through the
shared-bus path and answered "No answer from unit 3 … — the bus itself works
(device 'rtu1' reads on it)": the bus did not work, nothing could be asked.
Now the port is said missing, and when the host has a port by that name
that is not mapped into the container, the line to add is said too."""
from multibus import serial_inventory, serial_ports
from tests.test_devices_api import make_app, needs_tc

YAML = """
devices:
  - id: rtu1
    enabled: true
    connection: {{ protocol: rtu, serial_port: {sp}, unit_id: 3, timeout: 0.2 }}
"""


@needs_tc
def test_a_missing_port_is_said_missing_not_a_working_bus(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=YAML.format(sp="/dev/ttyQA_NOPE1"))
    r = client.post("/api/devices/rtu1/test").json()
    assert r["ok"] is False
    assert "the bus itself works" not in r["message"], r
    assert "/dev/ttyQA_NOPE1" in r["message"] and "does not exist" in r["message"], r


@needs_tc
def test_an_adhoc_test_on_a_missing_port_says_it_too(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=YAML.format(sp="/dev/ttyQA_NOPE1"))
    r = client.post("/api/devices/test", json={"connection": {
        "protocol": "rtu", "serial_port": "/dev/ttyQA_NOPE2", "unit_id": 1, "timeout": 0.2}}).json()
    assert r["ok"] is False and "does not exist" in r["message"], r


@needs_tc
def test_a_port_on_the_host_not_mapped_gives_the_line_to_add(tmp_path, monkeypatch):
    _cfg, client = make_app(tmp_path, extra_yaml=YAML.format(sp="/dev/ttyQA_NOPE1"))
    monkeypatch.setattr(serial_inventory, "inventory", lambda *a, **k: {"ports": [
        {"name": "ttyQA_NOPE1", "path": "/dev/ttyQA_NOPE1", "state": "not_mapped", "mapped": False,
         "fix": {"compose": "- /dev/ttyQA_NOPE1:/dev/ttyQA_NOPE1",
                 "docker_run": "--device /dev/ttyQA_NOPE1:/dev/ttyQA_NOPE1"}}], "hidden": 0})
    r = client.post("/api/devices/rtu1/test").json()
    assert "not mapped into the gateway's container" in r["message"], r
    assert "- /dev/ttyQA_NOPE1:/dev/ttyQA_NOPE1" in r["message"], r


@needs_tc
def test_a_port_that_exists_keeps_the_shared_bus_wording(tmp_path, monkeypatch):
    tty = tmp_path / "ttyQA0"
    tty.write_text("")
    _cfg, client = make_app(tmp_path, extra_yaml=YAML.format(sp=str(tty)))
    r = client.post("/api/devices/rtu1/test").json()
    assert "does not exist" not in r["message"], r
    assert serial_ports is not None


@needs_tc
def test_a_network_bus_never_says_a_serial_port_is_missing(tmp_path):
    """Only a serial port on this host can be missing (review n2): a stray
    serial_port beside an RTU-over-TCP bus is not what was asked."""
    _cfg, client = make_app(tmp_path, extra_yaml="""
devices:
  - id: net1
    enabled: true
    connection: { protocol: rtu-tcp, host: 10.255.255.1, port: 4196, unit_id: 3, timeout: 0.2 }
""")
    r = client.post("/api/devices/test", json={"connection": {
        "protocol": "rtu-tcp", "host": "10.255.255.1", "port": 4196, "unit_id": 2, "timeout": 0.2,
        "serial_port": "/dev/ttyQA_STRAY"}}).json()
    assert r.get("shared_with") == "net1", r
    assert "does not exist" not in r["message"], r
