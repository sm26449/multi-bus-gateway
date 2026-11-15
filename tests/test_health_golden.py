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
"""Golden characterization of every client's ``data_health()`` dict.

Phase-1 §5 R4 (ADR-0033 step 4a) extracts the duplicated staleness math into
``multibus.transports.health`` and routes each client through it. This file
pins the EXACT dict each client returns across its reachable branches BEFORE
that refactor, so the one-client-at-a-time adoption is proven to keep every
key and every value byte-identical — the whole point of "every current key
kept". It passes on the pre-R4 tree and must keep passing after.

Time is frozen (``time.monotonic``/``time.time``) so ``staleness_age_s`` is
deterministic; the clients' internal ``*_mono`` timestamps are set relative to
that frozen now.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tests.test_driver_protocol import _build

NOW_MONO = 1000.0
NOW_WALL = 5000.0


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: NOW_MONO)
    monkeypatch.setattr(time, "time", lambda: NOW_WALL)
    return SimpleNamespace(mono=NOW_MONO, wall=NOW_WALL)


def _reg(name="r"):
    return SimpleNamespace(name=name, address=0, poll_group="normal")


# ── Modbus ────────────────────────────────────────────────────────────────────

def test_modbus_idle_no_registers(frozen):
    m = _build()["modbus"]
    assert m.data_health() == {
        "status": "idle", "stale": False, "staleness_age_s": None,
        "last_success_ts": None, "connected": False}


def test_modbus_selected_but_no_poller(frozen):
    m = _build()["modbus"]
    m.registers = [_reg()]
    assert m.data_health() == {
        "status": "ok", "stale": False, "staleness_age_s": None,
        "last_success_ts": None, "connected": False}


# ── HTTP ──────────────────────────────────────────────────────────────────────

def test_http_idle_no_registers(frozen):
    h = _build()["http"]
    assert h.data_health() == {
        "status": "idle", "stale": False, "staleness_age_s": None}


def test_http_selected_but_no_poller(frozen):
    h = _build()["http"]
    h.registers = [_reg()]
    assert h.data_health() == {
        "status": "ok", "stale": False, "staleness_age_s": None}


# ── MQTT ──────────────────────────────────────────────────────────────────────

def test_mqtt_idle_no_registers(frozen):
    q = _build()["mqtt"]
    assert q.data_health() == {
        "status": "idle", "stale": False, "staleness_age_s": None}


def test_mqtt_cold_start_branches(frozen):
    q = _build()["mqtt"]
    q.registers = [_reg()]
    q.last_msg_mono = None
    q.connected = False
    assert q.data_health() == {
        "status": "down", "stale": False, "staleness_age_s": None}
    q.connected = True
    assert q.data_health() == {
        "status": "ok", "stale": False, "staleness_age_s": None}


def test_mqtt_fresh_and_stale(frozen):
    q = _build()["mqtt"]
    q.registers = [_reg()]
    q.connected = True
    q.last_msg_mono = NOW_MONO - 2
    assert q.data_health() == {
        "status": "ok", "stale": False, "staleness_age_s": 2.0}
    q.last_msg_mono = NOW_MONO - 9999
    assert q.data_health() == {
        "status": "down", "stale": True, "staleness_age_s": 9999.0}


# ── CAN ───────────────────────────────────────────────────────────────────────

def test_can_not_connected(frozen):
    c = _build()["can"]
    c.connected = False
    assert c.data_health() == {
        "status": "down", "stale": True, "staleness_age_s": None,
        "reason": "not listening"}


def test_can_no_frame_yet(frozen):
    c = _build()["can"]
    c.connected = True
    c.last_rx_mono = None
    assert c.data_health() == {
        "status": "stale", "stale": True, "staleness_age_s": None,
        "reason": "no frame heard yet"}


def test_can_fresh_and_stale(frozen):
    c = _build()["can"]
    c.connected = True
    c.last_rx_mono = NOW_MONO - 1
    assert c.data_health() == {
        "status": "ok", "stale": False, "staleness_age_s": 1.0}
    c.last_rx_mono = NOW_MONO - 9999
    assert c.data_health() == {
        "status": "stale", "stale": True, "staleness_age_s": 9999.0,
        "reason": "no frame for 9999s"}


# ── RTU tap ───────────────────────────────────────────────────────────────────

def _tap(frozen):
    rt = _build()["rtu_tap"]
    rt._reader.open_error = None
    rt._reader.host = ""          # connected property reads r.host / r._serial
    rt.conn = SimpleNamespace(stale_after_s=None)
    rt.last_rx_mono = None
    return rt


def test_rtu_not_attached(frozen):
    rt = _tap(frozen)
    assert rt.data_health() == {"status": "down", "reason": "not attached"}


def test_rtu_no_traffic_fresh_stale(frozen):
    rt = _tap(frozen)
    rt._registered = True
    assert rt.data_health() == {"status": "stale", "reason": "no traffic observed yet"}
    rt.last_rx_mono = NOW_MONO - 1
    assert rt.data_health() == {"status": "ok"}
    rt.last_rx_mono = NOW_MONO - 9999
    assert rt.data_health() == {"status": "stale", "reason": "no traffic for 9999s"}


def test_rtu_open_error(frozen):
    rt = _tap(frozen)
    rt._registered = True
    rt._reader.open_error = "boom"
    assert rt.data_health() == {"status": "down", "reason": "boom"}


# ── Seplos V2 ─────────────────────────────────────────────────────────────────

def test_seplos_not_attached(frozen):
    sp = _build()["seplos"]
    assert sp.data_health() == {"status": "down", "reason": "not attached"}


def test_seplos_config_error(frozen):
    sp = _build()["seplos"]
    sp.config_error = "cfg boom"
    assert sp.data_health() == {"status": "down", "reason": "cfg boom"}


# ── MeterOnly ─────────────────────────────────────────────────────────────────

def test_meter_only_no_sources(frozen):
    mo = _build()["meter_only"]
    assert mo.data_health() == {
        "status": "down", "stale": True, "staleness_age_s": None,
        "last_success_ts": None, "connected": False,
        "sources": {}, "sources_down": [], "measured_by": []}


# ── MultiSource ───────────────────────────────────────────────────────────────

def test_multi_source_one_idle_source(frozen):
    ms = _build()["multi_source"]
    assert ms.data_health() == {
        "status": "idle", "stale": False, "staleness_age_s": None,
        "sources": {"s": "idle"}, "sources_down": []}
