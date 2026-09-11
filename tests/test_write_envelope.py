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
"""Write safety envelope: template allowlist + bounds, and the write-lease dead-man."""
import time

import pytest

from multibus.device_template import parse_template
from multibus.write_lease import WriteLeaseManager

try:
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False
needs_tc = pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")


def _wr_tpl():
    return {"device_template": {"schema_version": 1, "id": "wr_test", "name": "WR",
        "protocol": {}, "categories": {}, "registers": [
            {"address": 100, "name": "limit", "data_type": "uint16", "register_type": "holding",
             "writable": True, "write_min": 0, "write_max": 100, "write_safe": 0},
            {"address": 110, "name": "mode", "data_type": "uint16", "register_type": "holding",
             "writable": True, "write_allowed": [0, 1, 4]},   # dropdown-style guard
            {"address": 120, "name": "free", "data_type": "uint16", "register_type": "holding",
             "writable": True},                               # declared, NO guards (F3a legal)
            {"address": 200, "name": "ro", "data_type": "uint16"},          # not writable
        ]}}


# ── template schema round-trip ────────────────────────────────────────────────

def test_template_write_envelope_roundtrip():
    t = parse_template(_wr_tpl())
    by = {r.address: r for r in t.registers}
    assert by[100].writable and by[100].write_min == 0
    assert by[100].write_max == 100 and by[100].write_safe == 0
    assert not by[200].writable
    wr = by[100].to_dict()
    assert wr['writable'] is True and wr['write_max'] == 100 and wr['write_safe'] == 0
    assert 'writable' not in by[200].to_dict()


# ── WriteLeaseManager (dead-man switch) ───────────────────────────────────────

def test_lease_expires_and_reverts():
    mgr = WriteLeaseManager(tick_s=0.01)
    calls = []
    mgr.arm("dev", "holding", 100, lease_ms=1, revert=lambda ok: calls.append("revert"))
    time.sleep(0.03)
    mgr._sweep()
    assert calls == ["revert"]
    assert mgr.snapshot() == []                    # removed after revert


def test_lease_renew_prevents_revert():
    mgr = WriteLeaseManager()
    calls = []
    mgr.arm("dev", "holding", 100, lease_ms=100000, revert=lambda ok: calls.append("r"))
    mgr._sweep()
    assert calls == [] and len(mgr.snapshot()) == 1


def test_lease_revert_failure_retries():
    # if the revert can't reach the device, the dead-man must keep trying, not drop
    mgr = WriteLeaseManager()
    calls = []

    def bad(is_current):
        calls.append(1)
        raise OSError("device unreachable")
    mgr.arm("d", "holding", 1, lease_ms=1, revert=bad)
    time.sleep(0.02)
    mgr._sweep()
    assert calls == [1]                            # revert attempted
    assert len(mgr.snapshot()) == 1                # lease kept (re-armed) for retry


def test_lease_stale_revert_aborts_on_renewal():
    # a revert about to fire must abort if the lease was renewed while it waited
    # on the (blocking) device lock — otherwise it clobbers the fresh setpoint
    mgr = WriteLeaseManager()
    wrote = []

    def revert(is_current):
        # simulate the renewal landing *while* we were blocked on the device lock
        mgr.arm("d", "holding", 5, lease_ms=100000, revert=lambda ok: None)
        if not is_current():
            return                                 # correct: renewed → skip safe-write
        wrote.append("safe")                       # bug path: would clobber the renewal

    mgr.arm("d", "holding", 5, lease_ms=1, revert=revert)
    time.sleep(0.02)
    mgr._sweep()
    assert wrote == []                             # safe-write skipped
    assert len(mgr.snapshot()) == 1                # the renewed lease survives


def test_lease_clear_cancels_revert():
    mgr = WriteLeaseManager()
    calls = []
    mgr.arm("dev", "holding", 100, lease_ms=1, revert=lambda ok: calls.append("r"))
    mgr.clear("dev", "holding", 100)
    time.sleep(0.02)
    mgr._sweep()
    assert calls == [] and mgr.snapshot() == []


def test_lease_clear_device():
    mgr = WriteLeaseManager()
    mgr.arm("d1", "holding", 1, 99999, lambda ok: None)
    mgr.arm("d1", "coil", 2, 99999, lambda ok: None)
    mgr.arm("d2", "holding", 3, 99999, lambda ok: None)
    mgr.clear_device("d1")
    assert [l['device'] for l in mgr.snapshot()] == ["d2"]


# ── #12d: crash-safe persistence ──────────────────────────────────────────────

def test_lease_persists_declarative_and_reloads(tmp_path):
    p = tmp_path / "write_leases.json"
    mgr = WriteLeaseManager(persist_path=p)
    meta = {"device": "d1", "register_type": "holding", "address": 40, "data_type": "uint16",
            "scale": 1.0, "safe_value": 0, "lease_ms": 5000}
    mgr.arm("d1", "holding", 40, 5000, lambda ok: None, meta=meta)
    assert p.exists()
    # a fresh manager (simulating a restart) sees the declarative record
    reborn = WriteLeaseManager(persist_path=p)
    loaded = reborn.load_persisted()
    assert len(loaded) == 1 and loaded[0]["device"] == "d1" and loaded[0]["safe_value"] == 0


def test_lease_renewal_does_not_rewrite_but_clear_does(tmp_path):
    p = tmp_path / "write_leases.json"
    mgr = WriteLeaseManager(persist_path=p)
    meta = {"device": "d1", "register_type": "holding", "address": 40, "data_type": "uint16",
            "scale": 1.0, "safe_value": 0, "lease_ms": 5000}
    mgr.arm("d1", "holding", 40, 5000, lambda ok: None, meta=meta)
    mtime1 = p.stat().st_mtime_ns
    mgr.arm("d1", "holding", 40, 5000, lambda ok: None, meta=meta)   # renewal: same set
    assert p.stat().st_mtime_ns == mtime1                            # not rewritten
    mgr.clear("d1", "holding", 40)
    assert mgr.load_persisted() == []                               # cleared on disk too


def test_recovered_lease_fires_immediately(tmp_path):
    # fire_now marks the lease already-expired → the very next sweep reverts.
    p = tmp_path / "write_leases.json"
    mgr = WriteLeaseManager(tick_s=0.01, persist_path=p)
    calls = []
    meta = {"device": "d1", "register_type": "holding", "address": 40, "data_type": "uint16",
            "scale": 1.0, "safe_value": 0, "lease_ms": 5000}
    mgr.arm("d1", "holding", 40, 5000, lambda ok: calls.append("revert"), meta=meta, fire_now=True)
    mgr._sweep()
    assert calls == ["revert"]                                     # fired without waiting the TTL


def test_expired_revert_removes_from_disk(tmp_path):
    p = tmp_path / "write_leases.json"
    mgr = WriteLeaseManager(tick_s=0.01, persist_path=p)
    meta = {"device": "d1", "register_type": "holding", "address": 40, "data_type": "uint16",
            "scale": 1.0, "safe_value": 0, "lease_ms": 1}
    mgr.arm("d1", "holding", 40, 1, lambda ok: None, meta=meta)
    time.sleep(0.02)
    mgr._sweep()                                                    # successful revert → drop
    assert mgr.load_persisted() == []                              # and remove from disk


# ── API enforcement ───────────────────────────────────────────────────────────

class _FakeClient:
    def __init__(self): self.reg = {}
    def write_value(self, address, register_type, data_type, value, scale=1.0, offset=0.0, prefer_fc6=False):
        self.reg[address] = value
        return True, None, [int(float(value))]
    def read_register(self, address, data_type, register_type):
        return self.reg.get(address)


def _envelope_app(tmp_path):
    from tests.test_devices import write_config
    from multibus.api import create_api
    from multibus import auth as _authmod
    from multibus.config import DeviceConfig
    cfg = write_config(tmp_path)
    cfg.security.allow_writes = True
    cfg.ui.auth_enabled = True
    cfg.ui.auth_username = "admin"
    cfg.ui.auth_password = _authmod.hash_password("pw")
    dev = DeviceConfig(id="ctrl", name="ctrl", template="wr_test", protocol="tcp")
    fake = _FakeClient()
    app, _ = create_api(cfg, None, None, None,
                        devices=[(d, None) for d in cfg.devices] + [(dev, fake)])
    app.state.template_registry._templates["wr_test"] = parse_template(_wr_tpl(), builtin=True)
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
    return client, fake


@needs_tc
def test_write_undeclared_needs_explicit_unguarded_flag(tmp_path):
    """F3a: an undeclared register is refused by DEFAULT — but the refusal
    explains the raw path, and `unguarded: true` opens it."""
    client, fake = _envelope_app(tmp_path)
    r = client.post("/api/devices/ctrl/write",
                    json={"address": 200, "value": 5, "register_type": "holding", "data_type": "uint16"})
    assert r.status_code == 403 and "not declared writable" in str(r.json())
    assert "unguarded" in str(r.json())          # the message teaches the raw path
    r2 = client.post("/api/devices/ctrl/write",
                     json={"address": 200, "value": 5, "register_type": "holding",
                           "data_type": "uint16", "unguarded": True})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["ok"] is True and body["unguarded"] is True
    assert fake.reg.get(200) == 5                # the raw write reached the bus


def test_write_unguarded_rejects_bad_data_type_and_lease(tmp_path):
    client, _fake = _envelope_app(tmp_path)
    r = client.post("/api/devices/ctrl/write",
                    json={"address": 201, "value": 1, "register_type": "holding",
                          "data_type": "string:8", "unguarded": True})
    assert r.status_code == 422
    # raw writes cannot lease — there is no declared safe value to revert to
    r2 = client.post("/api/devices/ctrl/write",
                     json={"address": 201, "value": 1, "register_type": "holding",
                           "data_type": "uint16", "unguarded": True, "lease_ms": 5000})
    assert r2.status_code == 422 and "write_safe" in str(r2.json())


@needs_tc
def test_write_bounds_enforced(tmp_path):
    client, fake = _envelope_app(tmp_path)
    over = client.post("/api/devices/ctrl/write",
                       json={"address": 100, "value": 250, "register_type": "holding", "data_type": "uint16"})
    assert over.status_code == 422 and "maximum" in str(over.json())
    ok = client.post("/api/devices/ctrl/write",
                     json={"address": 100, "value": 42, "register_type": "holding", "data_type": "uint16"})
    assert ok.status_code == 200 and fake.reg[100] == 42


@needs_tc
def test_write_rejects_non_finite_value(tmp_path):
    """3.4.1: NaN/Infinity survive the min/max envelope (every comparison is
    False for NaN) and struct.pack would ship the raw bit pattern to a real
    device — the endpoint must reject non-finite up front with 422. JSON has no
    NaN literal, so send raw content (Starlette's json parser accepts the
    tokens, exactly the attack path)."""
    client, fake = _envelope_app(tmp_path)
    for tok in ("NaN", "Infinity", "-Infinity"):
        r = client.post("/api/devices/ctrl/write",
                        content=('{"address": 100, "value": %s, '
                                 '"register_type": "holding", "data_type": "uint16"}' % tok),
                        headers={"content-type": "application/json"})
        assert r.status_code == 422, (tok, r.status_code, r.text)
        assert "finite" in str(r.json()).lower()
    assert 100 not in fake.reg          # nothing reached the device


@needs_tc
def test_write_lease_armed_and_listed(tmp_path):
    client, _fake = _envelope_app(tmp_path)
    r = client.post("/api/devices/ctrl/write",
                    json={"address": 100, "value": 80, "register_type": "holding",
                          "data_type": "uint16", "lease_ms": 30000})
    assert r.status_code == 200
    body = r.json()
    assert body["lease_ms"] == 30000 and body["reverts_to"] == 0
    leases = client.get("/api/writes/leases").json()["leases"]
    assert any(l["address"] == 100 and l["device"] == "ctrl" for l in leases)


# ── F3a: opt-in guards, allowed sets, per-device lock, write-info ────────────

@needs_tc
def test_write_allowed_set_enforced(tmp_path):
    client, fake = _envelope_app(tmp_path)
    bad = client.post("/api/devices/ctrl/write",
                      json={"address": 110, "value": 3, "register_type": "holding"})
    assert bad.status_code == 422 and "allowed" in str(bad.json())
    ok = client.post("/api/devices/ctrl/write",
                     json={"address": 110, "value": 4, "register_type": "holding"})
    assert ok.status_code == 200 and fake.reg.get(110) == 4


@needs_tc
def test_declared_register_without_guards_writes_verbatim(tmp_path):
    """Guards are opt-in: a writable register with no envelope accepts any
    finite value — capability is the base, restrictions are the user's tools."""
    client, fake = _envelope_app(tmp_path)
    r = client.post("/api/devices/ctrl/write",
                    json={"address": 120, "value": 65000, "register_type": "holding"})
    assert r.status_code == 200, r.text
    assert r.json().get("unguarded") is None      # declared path, not the raw one
    assert fake.reg.get(120) == 65000


def test_validate_template_no_longer_requires_bounds():
    from multibus.device_template import validate_template
    t = _wr_tpl()
    assert validate_template(t) == []             # 'free' has no bounds — legal
    # but declared guards must be coherent
    t2 = _wr_tpl()
    t2["device_template"]["registers"][0]["write_min"] = 200   # min > max
    assert any("write_min" in e for e in validate_template(t2))
    t3 = _wr_tpl()
    t3["device_template"]["registers"][1]["write_allowed"] = ["x"]
    assert any("write_allowed" in e for e in validate_template(t3))


def _lock_app(tmp_path):
    """Like _envelope_app, but 'ctrl' is a REAL config device — the write-lock
    endpoint persists through config.set_write_locked, which only knows
    config-registered devices."""
    from tests.test_devices import write_config
    from multibus.api import create_api
    from multibus import auth as _authmod
    cfg = write_config(tmp_path)
    cfg.security.allow_writes = True
    cfg.ui.auth_enabled = True
    cfg.ui.auth_username = "admin"
    cfg.ui.auth_password = _authmod.hash_password("pw")
    cfg.upsert_raw_device({"id": "ctrl", "template": "wr_test", "enabled": True,
                           "connection": {"protocol": "tcp", "host": "192.0.2.9"}})
    fake = _FakeClient()
    pairs = [(d, (fake if d.id == "ctrl" else None)) for d in cfg.devices]
    app, _ = create_api(cfg, None, None, None, devices=pairs)
    app.state.template_registry._templates["wr_test"] = parse_template(_wr_tpl(), builtin=True)
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
    return client, fake, cfg


@needs_tc
def test_write_lock_endpoint_blocks_and_unblocks(tmp_path):
    client, fake, cfg = _lock_app(tmp_path)
    r = client.post("/api/devices/ctrl/write-lock", json={"locked": True})
    assert r.status_code == 200 and r.json()["write_locked"] is True
    blocked = client.post("/api/devices/ctrl/write",
                          json={"address": 100, "value": 10, "register_type": "holding"})
    assert blocked.status_code == 403 and "write-locked" in str(blocked.json())
    # the lock PERSISTS (survives a restart via config.yaml)
    import yaml as _yaml
    doc = _yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert any(d.get('id') == 'ctrl' and d.get('write_locked') is True
               for d in doc.get('devices', []))
    r2 = client.post("/api/devices/ctrl/write-lock", json={"locked": False})
    assert r2.status_code == 200
    ok = client.post("/api/devices/ctrl/write",
                     json={"address": 100, "value": 10, "register_type": "holding"})
    assert ok.status_code == 200 and fake.reg.get(100) == 10


@needs_tc
def test_write_info_drives_the_dialog(tmp_path):
    client, _fake = _envelope_app(tmp_path)
    g = client.get("/api/devices/ctrl/write-info/holding/100").json()
    assert g["declared"] is True and g["write_min"] == 0 and g["write_max"] == 100
    a = client.get("/api/devices/ctrl/write-info/holding/110").json()
    assert a["write_allowed"] == [0, 1, 4]
    u = client.get("/api/devices/ctrl/write-info/holding/999").json()
    assert u["declared"] is False and u["write_locked"] is False


def test_template_write_allowed_roundtrip():
    t = parse_template(_wr_tpl())
    by = {r.address: r for r in t.registers}
    assert by[110].write_allowed == [0, 1, 4]
    assert by[110].to_dict()["write_allowed"] == [0, 1, 4]
    assert "write_allowed" not in by[100].to_dict()   # unset stays absent
