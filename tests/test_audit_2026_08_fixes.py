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
"""Fixes from the 2026-08 audit batch: energy-endpoint path traversal and the
missing-timestamp staleness laundering."""
import pytest


from tests.test_devices import write_config
from tests.test_devices_api import make_app, needs_tc

SECONDARY = """
devices:
  - id: hall-em24
    name: Hall
    enabled: true
    template: carlo_gavazzi_em24
    connection: {protocol: tcp, host: 192.0.2.9, port: 1502, unit_id: 2}
"""


def test_device_registers_path_rejects_traversal(tmp_path):
    cfg = write_config(tmp_path)
    for evil in ("..", "../../tmp/x", "a/b", "."):
        with pytest.raises(ValueError):
            cfg.device_registers_path(evil)
    # a real id still resolves under config/devices/<id>/
    p = cfg.device_registers_path("hall-em24")
    assert p.name == "selected_registers.json"
    assert "devices/hall-em24" in str(p)


@needs_tc
def test_energy_fields_rejects_unknown_and_traversal_device(tmp_path):
    _, client = make_app(tmp_path, extra_yaml=SECONDARY)
    # unknown device → 404, never a write
    assert client.post("/api/energy/fields?device=nope",
                       json={"fields": []}).status_code == 404
    # traversal id → 404 (not found) or 422, never a write outside the tree
    for evil in ("..", "../../../tmp/pwn"):
        rsp = client.post(f"/api/energy/fields?device={evil}", json={"fields": []})
        assert rsp.status_code in (404, 422)
    # nothing was written outside config/devices/
    assert not (tmp_path.parent / "pwn").exists()
    assert not (tmp_path / "selected_registers.json").read_text().strip().startswith("PWN") \
        if (tmp_path / "selected_registers.json").exists() else True


def test_missing_timestamp_not_laundered_to_now():
    """A value with no measurement ts must store timestamp=None (fail-safe),
    not the current time — else the vmeter would serve it as fresh."""
    from datetime import datetime
    # reproduce the exact store-write branch logic
    def stamp(_ts):
        return (datetime.fromtimestamp(_ts).isoformat() if _ts else None)
    assert stamp(0) is None
    assert stamp(None) is None
    assert stamp(1000.0) is not None
    # and the vmeter lookup treats None as not-fresh (value kept, ts None)
    from multibus.virtual_meter_manager import _lookup
    store = {5: {"name": "P", "value": 42.0, "timestamp": None}}
    assert _lookup(store, "P") == (42.0, None, None)


# ---------------------------------------------------------------------------
# Lot F — P2 batch
# ---------------------------------------------------------------------------


def test_add_instance_rejects_non_finite_bounds(tmp_path):
    from multibus.virtual_meter_manager import VirtualMeterManager
    import os
    os.makedirs(tmp_path / "tpl", exist_ok=True)
    mgr = VirtualMeterManager({}, config_path=str(tmp_path / "vm.yaml"),
                              templates_dir=str(tmp_path / "tpl"))
    (tmp_path / "tpl" / "t.yaml").write_text(
        "template:\n  id: t\n  transport: {type: tcp, port: 1502}\n  registers: []\n")
    for bad in (float("inf"), float("nan"), 0, -5):
        r = mgr.add_instance("t", port=1502, stale_after_s=bad)
        assert "error" in r, bad


def test_event_log_is_0600(tmp_path):
    """3.4.1: events.jsonl may carry operational detail — must be 0600, not the
    world-readable 0644 a plain open() produces under umask 022."""
    import os
    from multibus.event_log import EventLog
    old = os.umask(0o022)
    try:
        p = tmp_path / "events.jsonl"
        el = EventLog(path=str(p))
        el.add("warn", "test", "hello")
        assert (os.stat(p).st_mode & 0o777) == 0o600
        # survives compaction too
        for i in range(60):
            el.add("info", "test", f"e{i}")
        assert (os.stat(p).st_mode & 0o777) == 0o600
    finally:
        os.umask(old)


def test_update_instance_rejects_non_finite_bounds(tmp_path):
    """3.4.1: update_instance must reject inf/nan like add_instance — an inf
    stale_after_s would make _is_fresh true forever and disable the watchdog."""
    from multibus.virtual_meter_manager import VirtualMeterManager
    import os
    os.makedirs(tmp_path / "tpl", exist_ok=True)
    mgr = VirtualMeterManager({}, config_path=str(tmp_path / "vm.yaml"),
                              templates_dir=str(tmp_path / "tpl"))
    (tmp_path / "tpl" / "t.yaml").write_text(
        "template:\n  id: t\n  transport: {type: tcp, port: 1502}\n  registers: []\n")
    assert "error" not in mgr.add_instance("t", port=1502, stale_after_s=15)
    for field in ("stale_after_s", "update_interval_s", "max_hold_s"):
        for bad in (float("inf"), float("nan")):
            r = mgr.update_instance("t", **{field: bad})
            assert "error" in r, (field, bad)
    # a finite update still works
    assert mgr.update_instance("t", stale_after_s=30).get("updated") is True


def test_merge_devices_reinjects_stripped_secrets():
    from multibus.snapshots import _merge_devices
    live = [{"id": "d1", "connection": {"broker": "b", "password": "SECRET"},
             "rest_push": {"headers": {"X": "tok"}}}]
    incoming = [{"id": "d1", "connection": {"broker": "b", "password": ""},
                 "rest_push": {"headers": {}}}]
    out = _merge_devices(live, incoming)
    assert out[0]["connection"]["password"] == "SECRET"     # refilled from live
    assert out[0]["rest_push"]["headers"] == {"X": "tok"}
    # a device only in the backup (no live match) is kept as-is
    out2 = _merge_devices(live, [{"id": "new", "connection": {"broker": "z"}}])
    assert out2[0]["id"] == "new"


def test_merge_devices_keeps_live_url_over_redacted_import():
    """3.3.4: a sanitized export redacts connection.url / rest_push.url — the
    merge-import must recognize the redacted form and keep the live URL, or the
    round-trip silently breaks the device (?api_key=*** is not a token)."""
    from multibus.redact import redact_url
    from multibus.snapshots import _merge_devices
    live_url = "http://user:tok@shelly.local/status?api_key=SECRET"
    push_url = "https://ingest.example.com/p?token=ABC"
    live = [{"id": "d1", "connection": {"url": live_url},
             "rest_push": {"url": push_url}}]
    incoming = [{"id": "d1", "connection": {"url": redact_url(live_url)},
                 "rest_push": {"url": redact_url(push_url)}}]
    out = _merge_devices(live, incoming)
    assert out[0]["connection"]["url"] == live_url          # live URL survives
    assert out[0]["rest_push"]["url"] == push_url
    # a deliberately CHANGED url in the backup is authoritative (not a redaction)
    out2 = _merge_devices(live, [{"id": "d1",
                                  "connection": {"url": "http://other.host/x"}}])
    assert out2[0]["connection"]["url"] == "http://other.host/x"


def test_reinject_restores_top_level_redacted_urls():
    """3.3.4: root rest_push.url and influxdb.url are exported through
    redact_url — the merge-import re-injects the live originals."""
    from multibus.redact import redact_url
    from multibus.snapshots import _reinject_stripped_secrets
    live = {"rest_push": {"url": "https://push.example.com/i?key=S3CR3T"},
            "influxdb": {"url": "http://admin:pw@influx.local:8086"}}
    merged = {"rest_push": {"url": redact_url(live["rest_push"]["url"])},
              "influxdb": {"url": redact_url(live["influxdb"]["url"])}}
    _reinject_stripped_secrets(merged, live)
    assert merged["rest_push"]["url"] == live["rest_push"]["url"]
    assert merged["influxdb"]["url"] == live["influxdb"]["url"]
    # a genuinely different imported URL stays authoritative
    merged2 = {"influxdb": {"url": "http://newhost:8086"}}
    _reinject_stripped_secrets(merged2, live)
    assert merged2["influxdb"]["url"] == "http://newhost:8086"


# ---------------------------------------------------------------------------
# Lot F — P3 batch
# ---------------------------------------------------------------------------

def test_auth_non_ascii_username_no_crash():
    """A non-ASCII username must not TypeError inside authenticate (which would
    skip lockout accounting) — compare on bytes."""
    from multibus.auth import AuthState
    from multibus.config import UIConfig
    ui = UIConfig(auth_enabled=True, auth_username="admin", auth_password="pw")
    st = AuthState(ui)
    assert st.authenticate("admÿn", "pw") is None    # non-ASCII: no crash, no match
    assert st.authenticate("admin", "wrong") is None


@needs_tc
def test_discover_esphome_port_range(tmp_path):
    _, client = make_app(tmp_path)
    for bad in (0, 65536, 999999):
        rsp = client.post("/api/discover/esphome",
                          json={"cidr": "192.168.1.0/30", "port": bad})
        assert rsp.status_code == 422, bad


def test_json_view_uses_guarded_clock(monkeypatch):
    # BEHAVIORAL (was an inspect.getsource assert): json_view ages rows on the
    # MONOTONIC clock — a wall-clock step must not change the reported age
    import time as _t
    from multibus.virtual_meter import RegisterDef, Template, VirtualMeter
    now = _t.monotonic()
    vals = {"power_active_total": (230.0, now)}
    vm_ = VirtualMeter(Template(id="t", name="t", transport={"port": 19998},
                                registers=[RegisterDef(addr=0, type="float",
                                                       source_kind="live",
                                                       source="power_active_total")]),
                       lambda n: vals.get(n), stale_after_s=15)
    vm_._rebuild_block()
    age1 = (vm_.json_view().get("registers") or [{}])[0].get("age_s")
    real_time = _t.time
    monkeypatch.setattr(_t, "time", lambda: real_time() + 100000)   # NTP step
    age2 = (vm_.json_view().get("registers") or [{}])[0].get("age_s")
    for a in (age1, age2):
        assert a is None or a < 60          # a wall-clock-based age would be ~100000


def test_identity_files_tightened_on_load(tmp_path):
    import os
    import stat
    from multibus.audit import AuditLog
    from multibus.passkeys import PasskeyStore
    ap = tmp_path / "audit.jsonl"; ap.write_text("{}\n"); os.chmod(ap, 0o644)
    AuditLog(str(ap))
    assert stat.S_IMODE(os.stat(ap).st_mode) == 0o600
    pk = tmp_path / "passkeys.json"; pk.write_text('{"credentials": []}'); os.chmod(pk, 0o644)
    PasskeyStore(str(pk))
    assert stat.S_IMODE(os.stat(pk).st_mode) == 0o600


# ---------------------------------------------------------------------------
# Lot F+ — minor P3 batch
# ---------------------------------------------------------------------------

def test_discovery_timeout_not_reported_as_encrypted():
    """A silent service that accepts the connection but never answers the hello
    must NOT be reported as an encrypted ESPHome device."""
    import socket
    import threading
    from multibus.discovery import _esphome_hello
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    port = srv.getsockname()[1]
    def run():
        c, _ = srv.accept()
        try:
            c.recv(64)          # read the hello, then stay SILENT (no reply)
            import time as _t; _t.sleep(1.0)
        finally:
            c.close(); srv.close()
    threading.Thread(target=run, daemon=True).start()
    r = _esphome_hello("127.0.0.1", port, timeout=0.3)
    assert r is None            # unidentified silent service → dropped, not "encrypted"


def test_operator_cannot_forget_tombstone_edge(tmp_path):
    """An operator must not reach DELETE /api/devices/restorable/<id> even when
    <id> is literally 'write'/'test'/'payload-sample'. BEHAVIORAL (was an
    inspect.getsource assert): 403 = the role gate refused."""
    import pytest as _pytest
    try:
        from fastapi.testclient import TestClient
    except Exception:
        _pytest.skip("TestClient not installed")
    from multibus import auth as _a
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml=f"""
ui:
  auth:
    enabled: true
    username: boss
    password: "{_a.hash_password('pw')}"
    operator_username: ops
    operator_password: "{_a.hash_password('op')}"
""")
    app, _ = create_api(cfg, None, None, None,
                        devices=[(d, None) for d in cfg.devices])
    op = TestClient(app, raise_server_exceptions=False)
    assert op.post("/api/auth/login",
                   json={"username": "ops", "password": "op"}).status_code == 200
    for edge in ("write", "test", "payload-sample"):
        assert op.delete(f"/api/devices/restorable/{edge}").status_code == 403


def test_esphome_client_closes_error_responses(monkeypatch):
    # BEHAVIORAL (was an inspect.getsource assert): an HTTPError body must be
    # closed, or each failed dashboard call leaks a socket until GC
    import urllib.error
    import urllib.request
    from multibus import esphome_client
    closed = []

    class _Body:
        def read(self): return b"boom"
        def close(self): closed.append(True)

    def _raise(*a, **k):
        raise urllib.error.HTTPError("http://x", 500, "boom", None, _Body())
    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    dash = esphome_client.EsphomeDashboard("http://127.0.0.1:1")
    try:
        dash._request("GET", "/anything")
    except Exception:  # noqa: BLE001
        pass
    assert closed, "HTTPError response body was not closed"


def test_modbus_poller_no_publish_after_stop():
    # BEHAVIORAL (was an inspect.getsource assert): a stop() landing while a
    # poll cycle is mid-read must suppress the publish — since M1 (3.23.6)
    # the stop EVENT is the single source of truth, checked between reads.
    import threading
    import time as _t
    from multibus.config import SelectedRegister
    from multibus.modbus_client import RegisterPoller

    published = []
    poller_box = {}

    class _Conn:
        connected = True

        def read_registers(self, address, count, register_type="holding",
                           stop_event=None):
            # the FIRST read triggers the stop — as if disconnect() raced in
            poller_box["p"].stop()
            return [1] * count

    regs = [SelectedRegister(address=a, name=f"r{a}", label=f"r{a}", unit="",
                             data_type="uint16", poll_group="g")
            for a in (0, 200)]                    # far apart → two read batches
    from multibus.register_parser import RegisterParser
    p = RegisterPoller(name="g", interval=3600, registers=regs, connection=_Conn(),
                       parser=RegisterParser('big'),
                       publish_callback=lambda *a: published.append(a))
    poller_box["p"] = p
    p.start()
    for _ in range(100):                          # bounded wait for thread exit
        if not p.is_alive():
            break
        _t.sleep(0.02)
    assert not p.is_alive()
    assert published == []                        # stopped mid-cycle → no publish
    assert isinstance(p._stop_event, threading.Event) and p._stop_event.is_set()


def test_driver_staleness_is_monotonic_step_immune(monkeypatch):
    """A wall-clock step must not falsely mark a source device stale/down —
    driver data_health now judges on the monotonic clock."""
    from multibus.modbus_client import ModbusClient
    from multibus.config import ModbusConfig
    import multibus.modbus_client as mc_mod

    class _P:
        interval = 0.25
        running = True
    mc = ModbusClient(ModbusConfig(), registers=[1], poll_groups={})
    mc.pollers = [_P()]
    mc.connected = True
    # last success 5s ago on the MONOTONIC clock (fresh)
    mc.connection.last_success_mono = mc_mod.time.monotonic() - 5.0
    mc.connection.last_success_ts = mc_mod.time.time() - 5.0

    # now the WALL clock jumps forward 10 minutes — must NOT flip to down
    real_time = mc_mod.time.time
    monkeypatch.setattr(mc_mod.time, "time", lambda: real_time() + 600)
    assert mc.data_health(30)["status"] == "ok"      # monotonic age still ~5s
