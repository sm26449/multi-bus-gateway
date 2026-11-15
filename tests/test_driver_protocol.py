# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
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
"""ADR-0033 conformance: every device client satisfies the DeviceDriver
contract, the optional capability Protocols are declared not probed, and the
common signatures and behaviour hold.

Three things per class (ADR-0033 §1 point 3):
  - presence:  isinstance(client, DeviceDriver) and the optional Protocols it
               advertises;
  - signature: the contract call shapes bind (update_registers with just the
               register list; data_health with and without a threshold);
  - behaviour: data_health(None) returns the common keys; reload_registers()
               after update_registers(...) keeps the register set.

Written before the R1 adaptations, so it fails first on HttpClient /
MqttInputClient (no reload_registers) and on the Modbus/HTTP poll_groups and
data_health(None) defaults.
"""
import inspect
from types import SimpleNamespace

import pytest

from multibus.transports.driver import (
    DeviceDriver, RegisterReadable, KeepaliveCapable, PreviewSnapshot)

from multibus.config import ModbusConfig
from multibus.modbus_client import ModbusClient
from multibus.http_client import HttpClient
from multibus.mqtt_input import MqttInputClient
from multibus.can_client import CanClient
from multibus.rtu_tap import RtuTapClient
from multibus.seplos_v2 import SeplosV2Client
from multibus.meter_only import MeterOnlyClient
from multibus.multi_source import MultiSourceClient


# ── no-I/O construction (same shapes the per-client tests use) ───────────────

def _reg(addr=0, name='v'):
    return SimpleNamespace(address=addr, name=name, label=name, unit='',
                           data_type='uint16', register_type='input',
                           poll_group='realtime', scale=1.0, offset=0.0, nan=None,
                           monotonic=False, enum=None, bits=None, mask=None,
                           shift=None, scale_from='')


def _tap_conn():
    return SimpleNamespace(serial_port='/dev/null-tap', baudrate=9600, parity='N',
                           stopbits=1, bytesize=8, unit_id=1, stale_after_s=30)


def _seplos_conn():
    return SimpleNamespace(protocol='seplos_v2', serial_port='/dev/fake-seplos', host='',
                           port=0, baudrate=9600, parity='N', stopbits=1, bytesize=8,
                           unit_id=1, timeout=0.3, retry_attempts=1, retry_delay=0,
                           stale_after_s=30, mode='poll')


def _build():
    """One fresh instance of each of the eight client classes, no transport
    opened. Returns {name: client}."""
    tap = RtuTapClient(_tap_conn(), [], {})
    # bypass the shared-port reader registry so nothing touches a serial port
    tap._reader = SimpleNamespace(open_error=None, recent=[],
                                  _serial=SimpleNamespace(close=lambda: None))
    return {
        'modbus': ModbusClient(ModbusConfig(), [], {}),
        'http': HttpClient({"url": "http://127.0.0.1:1/x"}, [], {}),
        'mqtt': MqttInputClient({"topic": "x"}, []),
        'can': CanClient({}, [], carrier=object()),
        'rtu_tap': tap,
        'seplos': SeplosV2Client(_seplos_conn(), [], {}, device_id='p'),
        'meter_only': MeterOnlyClient(SimpleNamespace(get_device=lambda _id: None), 'm'),
        'multi_source': MultiSourceClient(
            'u', [(SimpleNamespace(id='s', protocol='http'),
                   HttpClient({"url": "http://127.0.0.1:1/x"}, [], {}))]),
    }


ALL = sorted(_build().keys())
# reload_registers() that re-applies the set without touching a transport
RELOAD_SAFE = ['modbus', 'http', 'mqtt', 'can']


def _accepts(func, *args, **kwargs):
    try:
        inspect.signature(func).bind(*args, **kwargs)
        return True
    except TypeError:
        return False


# ── presence ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('name', ALL)
def test_client_is_a_device_driver(name):
    drv = _build()[name]
    assert isinstance(drv, DeviceDriver), f"{name} does not satisfy DeviceDriver"


def test_advertised_capability_protocols():
    c = _build()
    # Modbus answers ad-hoc reads + on-demand poll; the push/listen clients do not.
    assert isinstance(c['modbus'], RegisterReadable)
    assert not isinstance(c['http'], RegisterReadable)
    assert not isinstance(c['mqtt'], RegisterReadable)
    # the MQTT input is the keepalive-capable one.
    assert isinstance(c['mqtt'], KeepaliveCapable)
    assert not isinstance(c['modbus'], KeepaliveCapable)
    # PreviewSnapshot: the Seplos and HTTP clients expose a public snapshot()
    # (Q1-A12), so preview/diagnostics never read their private state.
    assert isinstance(c['seplos'], PreviewSnapshot)
    assert isinstance(c['http'], PreviewSnapshot)
    assert not isinstance(c['can'], PreviewSnapshot)


# ── signature compatibility ───────────────────────────────────────────────────

@pytest.mark.parametrize('name', ALL)
def test_update_registers_accepts_poll_groups_default(name):
    drv = _build()[name]
    # the common call shape passes just the register list
    assert _accepts(type(drv).update_registers, drv, [_reg()]), \
        f"{name}.update_registers requires poll_groups (no default)"


@pytest.mark.parametrize('name', ALL)
def test_data_health_accepts_none_threshold_signature(name):
    drv = _build()[name]
    assert _accepts(type(drv).data_health, drv, None)
    assert _accepts(type(drv).data_health, drv)


# ── behaviour ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('name', ALL)
def test_data_health_none_returns_status(name):
    drv = _build()[name]
    h = drv.data_health(None)
    assert isinstance(h, dict) and 'status' in h, \
        f"{name}.data_health(None) did not return a status dict"
    # the client's own default must agree with explicit None
    assert drv.data_health()['status'] == h['status']


@pytest.mark.parametrize('name', RELOAD_SAFE)
def test_reload_registers_keeps_the_set(name):
    drv = _build()[name]
    regs = [_reg(1, 'a')]
    drv.update_registers(regs, {})
    drv.reload_registers()
    assert drv.registers == regs, f"{name}.reload_registers() lost the register set"


# ── PreviewSnapshot (Q1-A12): public snapshot() instead of private reads ──────

def test_seplos_snapshot_is_a_locked_copy_of_words():
    c = _build()['seplos']
    c.words = {0x1000: 5, 0x1001: 42}
    snap = c.snapshot()
    assert snap == {0x1000: 5, 0x1001: 42}
    snap[0x1000] = 999                 # a copy — mutating it must not touch the client
    assert c.words[0x1000] == 5


def test_http_snapshot_does_one_fetch_and_returns_the_doc():
    c = _build()['http']
    calls = []
    c._fetch = lambda: (calls.append(1), {"Body": {"P": 7}})[1]
    doc = c.snapshot()
    assert doc == {"Body": {"P": 7}}
    assert len(calls) == 1             # exactly one fetch, no hidden retries
