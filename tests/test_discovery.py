"""Modbus auto-discovery: CIDR/LAN validation + endpoint guards."""
import pytest

from multibus import discovery as disc
from tests.test_devices_api import make_app, needs_tc


def test_hosts_from_cidr_expands_private():
    hosts = disc.hosts_from_cidr("192.168.1.0/30")
    assert hosts == ["192.168.1.1", "192.168.1.2"]


def test_hosts_from_cidr_rejects_public():
    with pytest.raises(ValueError):
        disc.hosts_from_cidr("8.8.8.0/30")


def test_hosts_from_cidr_rejects_too_large():
    with pytest.raises(ValueError):
        disc.hosts_from_cidr("10.0.0.0/16")


def test_hosts_from_cidr_rejects_garbage():
    with pytest.raises(ValueError):
        disc.hosts_from_cidr("not-a-cidr")


def test_hosts_from_cidr_allow_nonlan():
    hosts = disc.hosts_from_cidr("8.8.8.0/30", allow_nonlan=True)
    assert hosts == ["8.8.8.1", "8.8.8.2"]


def test_lan_host_error():
    assert disc.lan_host_error("192.168.1.5") is None          # private IP ok
    assert disc.lan_host_error("8.8.8.8")                       # public → error string
    assert disc.lan_host_error("8.8.8.8", allow_nonlan=True) is None
    assert disc.lan_host_error("")                              # empty → error


@needs_tc
def test_scan_rejects_public_cidr(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/discover/modbus/scan", json={"cidr": "8.8.8.0/30"})
    assert r.status_code == 422


@needs_tc
def test_scan_small_private_range_runs(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/discover/modbus/scan",
                    json={"cidr": "10.255.255.252/30", "timeout": 0.1})
    assert r.status_code == 200
    body = r.json()
    assert body["scanned"] == 2 and isinstance(body["results"], list)


@needs_tc
def test_units_requires_host_for_tcp(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert client.post("/api/discover/modbus/units", json={"protocol": "tcp"}).status_code == 422


@needs_tc
def test_units_requires_serial_for_rtu(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert client.post("/api/discover/modbus/units", json={"protocol": "rtu"}).status_code == 422
