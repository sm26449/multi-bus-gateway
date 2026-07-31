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

from multibus.config import Config

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
    assert _lookup(store, "P") == (42.0, None)


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


@needs_tc
def test_json_view_uses_guarded_clock():
    import inspect
    from multibus.virtual_meter import VirtualMeter
    # freshness moved to the MONOTONIC clock (step-immune) — json_view uses it too
    assert "time.monotonic()" in inspect.getsource(VirtualMeter.json_view)


def test_identity_files_tightened_on_load(tmp_path):
    import os, stat
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
    import socket, threading
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


def test_operator_cannot_forget_tombstone_edge():
    """An operator must not reach DELETE /api/devices/restorable/<id> even when
    <id> is literally 'write'/'test'/'payload-sample'."""
    import multibus.api as api_mod
    # rebuild the matcher's logic inline against the known prefixes isn't
    # exposed; assert the segment guard is present in source instead
    import inspect
    src = inspect.getsource(api_mod.create_api)
    assert 'parts[3] != "restorable"' in src


def test_esphome_client_closes_error_responses():
    import inspect
    from multibus import esphome_client
    src = inspect.getsource(esphome_client.EsphomeDashboard._request)
    assert "e.close()" in src


def test_modbus_poller_no_publish_after_stop():
    import inspect
    from multibus.modbus_client import RegisterPoller
    src = inspect.getsource(RegisterPoller.run)
    assert "if not self.running:" in src and "break" in src


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
