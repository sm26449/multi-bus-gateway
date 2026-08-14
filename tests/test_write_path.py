# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Modbus write path (FC5/6/15/16) + coil/discrete read (FC1/2)."""
import pytest

from multibus.config import normalize_register_type
from multibus.modbus_client import ModbusClient, ModbusConnection, ModbusConfig

try:
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False
needs_tc = pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")


class _Ok:
    def isError(self): return False


class _FakeClient:
    def __init__(self): self.calls = []
    def is_socket_open(self): return True
    def connect(self): return True
    def close(self): pass
    def write_register(self, address, value, device_id):
        self.calls.append(('w1', address, value)); return _Ok()
    def write_registers(self, address, values, device_id):
        self.calls.append(('w16', address, list(values))); return _Ok()
    def write_coil(self, address, value, device_id):
        self.calls.append(('w5', address, bool(value))); return _Ok()
    def write_coils(self, address, values, device_id):
        self.calls.append(('w15', address, list(values))); return _Ok()
    def read_coils(self, address, count, device_id):
        self.calls.append(('r1', address, count))
        return type('R', (), {'isError': lambda s: False, 'bits': [True] * count})()
    def read_discrete_inputs(self, address, count, device_id):
        self.calls.append(('r2', address, count))
        return type('R', (), {'isError': lambda s: False, 'bits': [False] * count})()


def _conn():
    c = ModbusConnection(ModbusConfig())
    c.client = _FakeClient(); c.connected = True
    return c


# ── register-type normalization ───────────────────────────────────────────────

def test_normalize_coil_and_discrete():
    assert normalize_register_type('coil') == 'coil'
    assert normalize_register_type('FC1') == 'coil'
    assert normalize_register_type('discrete') == 'discrete'
    assert normalize_register_type('fc2') == 'discrete'
    assert normalize_register_type('holding') == 'holding'
    assert normalize_register_type('input') == 'input'


# ── connection-level dispatch ─────────────────────────────────────────────────

def test_write_dispatch_fc5_fc6_fc16():
    c = _conn()
    c.write(10, register_type='coil', coils=True)
    c.write(20, register_type='holding', values=[0x1234], prefer_fc6=True)
    c.write(30, register_type='holding', values=[0x1, 0x2])
    assert c.client.calls == [('w5', 10, True), ('w1', 20, 0x1234), ('w16', 30, [0x1, 0x2])]


def test_write_refuses_readonly_types():
    ok, err = _conn().write(0, register_type='input', values=[1])
    assert not ok and 'read-only' in err


def test_read_bits_dispatch_fc1_fc2():
    c = _conn()
    assert c.read_bits(5, 3, 'coil') == [True, True, True]
    assert c.read_bits(7, 2, 'discrete') == [False, False]
    assert c.client.calls == [('r1', 5, 3), ('r2', 7, 2)]


# ── client-level encode round-trip ────────────────────────────────────────────

def test_write_value_encodes_scaled_int32_big_endian():
    mc = ModbusClient.__new__(ModbusClient)
    mc.byte_order = 'big'
    from multibus.register_parser import RegisterParser
    mc.parser = RegisterParser('big')
    mc.connection = _conn()
    # write 1500 W with scale 100 -> raw 150000 -> int32 big-endian words
    ok, err, words = mc.write_value(40, 'holding', 'int32', 1500, scale=100)
    assert ok and err is None
    # FC16 issued with the encoded words
    assert mc.connection.client.calls[-1][0] == 'w16'
    assert mc.connection.client.calls[-1][2] == words
    # decode the words back through the parser and un-scale -> 1500
    assert mc.parser.parse_value(words, 'int32') / 100 == 1500


def test_write_value_coil():
    mc = ModbusClient.__new__(ModbusClient)
    mc.byte_order = 'big'; mc.connection = _conn()
    ok, err, words = mc.write_value(3, 'coil', 'bool', 1)
    assert ok and mc.connection.client.calls[-1] == ('w5', 3, True)


# ── API gating (secure-by-default) ────────────────────────────────────────────

def test_read_register_returns_raw_so_verify_must_divide_by_scale():
    # The write endpoint's read-back verify compares read_register() to the wanted
    # ENGINEERING value. read_register returns the RAW register value (no /scale),
    # so the endpoint must divide by scale — otherwise `verified` is always false
    # for any scale != 1. This guards that read_register is indeed raw.
    from unittest.mock import MagicMock
    from multibus.modbus_client import ModbusClient
    from multibus.config import ModbusConfig
    mc = ModbusClient(config=ModbusConfig(), registers=[], poll_groups={}, byte_order='big')
    mc.connection = MagicMock()
    mc.connection.read_registers.return_value = [(150000 >> 16) & 0xFFFF, 150000 & 0xFFFF]
    raw = mc.read_register(40, 'int32', 'holding')
    assert raw == 150000                    # RAW — read_register does NOT apply scale
    assert raw / 100 == 1500                # the endpoint's fix: /scale → engineering == want


def test_coil_truthy():
    from multibus.modbus_client import coil_truthy
    assert coil_truthy("false") is False and coil_truthy("0") is False
    assert coil_truthy("off") is False and coil_truthy("") is False and coil_truthy(0) is False
    assert coil_truthy("1") is True and coil_truthy("true") is True and coil_truthy("on") is True
    assert coil_truthy(1) is True and coil_truthy(True) is True
    # numeric-looking string zero must be OFF (asymmetry with the numeric path)
    assert coil_truthy("0.0") is False and coil_truthy("-0") is False
    assert coil_truthy("2") is True and coil_truthy("0.1") is True


def _app(tmp_path, auth=False, **sec):
    from tests.test_devices import write_config
    from multibus.api import create_api
    from multibus import auth as _authmod
    cfg = write_config(tmp_path)
    for k, v in sec.items():
        setattr(cfg.security, k, v)
    if auth:
        cfg.ui.auth_enabled = True
        cfg.ui.auth_username = "admin"
        cfg.ui.auth_password = _authmod.hash_password("pw")
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    if auth:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
    return cfg, client


@needs_tc
def test_write_refused_when_disabled(tmp_path):
    _cfg, client = _app(tmp_path)                         # allow_writes defaults False
    r = client.post("/api/devices/umg512/write",
                    json={"address": 10, "value": 1, "register_type": "holding"})
    assert r.status_code == 403 and "disabled" in str(r.json())


@needs_tc
def test_write_requires_authentication(tmp_path):
    # writes enabled but no login and no API key → must be refused
    _cfg, client = _app(tmp_path, allow_writes=True)
    r = client.post("/api/devices/umg512/write",
                    json={"address": 10, "value": 1, "register_type": "holding"})
    assert r.status_code == 403 and "authentication" in str(r.json())


@needs_tc
def test_primary_is_read_only_even_when_writes_enabled(tmp_path):
    _cfg, client = _app(tmp_path, auth=True, allow_writes=True)
    r = client.post("/api/devices/umg512/write",
                    json={"address": 10, "value": 1, "register_type": "holding"})
    assert r.status_code == 403 and "read-only" in str(r.json())


@needs_tc
def test_write_rate_limit_returns_429(tmp_path):
    # rate=1/s: the first write passes the limiter (then 403 read-only on primary),
    # the second immediate write is refused by the limiter with 429
    _cfg, client = _app(tmp_path, auth=True, allow_writes=True, write_rate_limit_per_s=1)
    body = {"address": 10, "value": 1, "register_type": "holding"}
    r1 = client.post("/api/devices/umg512/write", json=body)
    r2 = client.post("/api/devices/umg512/write", json=body)
    assert r1.status_code == 403                     # passed the limiter, then read-only
    assert r2.status_code == 429 and "rate limit" in str(r2.json())


@needs_tc
def test_input_registers_rejected(tmp_path):
    _cfg, client = _app(tmp_path, auth=True, allow_writes=True)
    dev = next((d for d in _cfg.devices if not d.primary), None)
    if dev is None:
        pytest.skip("no non-primary device in fixture")
    r = client.post(f"/api/devices/{dev.id}/write",
                    json={"address": 5, "value": 1, "register_type": "input"})
    assert r.status_code == 400 and "read-only" in str(r.json())
