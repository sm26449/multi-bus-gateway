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
"""Clock-step immunity + per-device connection uptime.

Born from a live incident (2026-07-28): installing chrony stepped the host
clock +138s and both virtual meters fail-safe-stopped over perfectly fresh
data. The guard detects wall-vs-monotonic drift changes and holds the stale
stop for one window; up_since_s is measured on the monotonic clock."""
from unittest.mock import MagicMock

import pytest

from multibus import virtual_meter as vm

from tests.test_devices import write_config
from tests.test_devices_api import needs_tc


@pytest.fixture
def clock(monkeypatch):
    t = {"wall": 1000.0, "mono": 500.0}
    monkeypatch.setattr(vm.time, "time", lambda: t["wall"])
    monkeypatch.setattr(vm.time, "monotonic", lambda: t["mono"])
    return t


def test_clock_guard_ignores_normal_time_flow(clock):
    g = vm.VirtualMeter.ClockStepGuard(15)
    assert g.tick() is False                      # first sample: baseline
    for _ in range(10):
        clock["wall"] += 1.0
        clock["mono"] += 1.0
        assert g.tick() is False                  # drift unchanged
    assert g.in_grace is False


def test_clock_guard_detects_forward_step_and_grace_expires(clock):
    g = vm.VirtualMeter.ClockStepGuard(15)
    g.tick()
    clock["wall"] += 138.36                       # the chrony incident, replayed
    assert g.tick() is True
    assert g.last_step_s == pytest.approx(138.36)
    assert g.in_grace is True
    clock["mono"] += 14.9                         # inside the window
    assert g.in_grace is True
    clock["mono"] += 0.2                          # window over
    assert g.in_grace is False


def test_clock_guard_detects_backward_step(clock):
    g = vm.VirtualMeter.ClockStepGuard(15)
    g.tick()
    clock["wall"] -= 30.0
    assert g.tick() is True and g.last_step_s == pytest.approx(-30.0)
    assert g.in_grace is True


def test_supervisor_skips_stale_stop_during_grace():
    """The stop branch is gated on `not clock_guard.in_grace` — pin that the
    wiring exists (the guard logic itself is unit-tested above)."""
    import inspect
    src = inspect.getsource(vm.VirtualMeter._supervise)
    assert "clock_guard.tick()" in src
    assert "not clock_guard.in_grace" in src


# ---------------------------------------------------------------------------
# /api/status: up_since_s per device
# ---------------------------------------------------------------------------

@needs_tc
def test_up_since_tracks_health_transitions(tmp_path):
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    cfg = write_config(tmp_path)
    client = MagicMock()
    client.get_stats.return_value = {"connected": True}
    client.data_health.return_value = {"status": "ok"}
    app, _ = create_api(cfg, None, None, None,
                        devices=[(cfg.devices[0], client)])
    tc = TestClient(app, raise_server_exceptions=False)

    d = tc.get("/api/status").json()["devices"][0]
    assert d["up_since_s"] == 0                    # first sighting
    d = tc.get("/api/status").json()["devices"][0]
    assert d["up_since_s"] >= 0                    # same health → keeps counting

    client.data_health.return_value = {"status": "down"}
    d = tc.get("/api/status").json()["devices"][0]
    assert d["up_since_s"] == 0                    # transition resets the clock
    client.data_health.return_value = {"status": "ok"}
    d = tc.get("/api/status").json()["devices"][0]
    assert d["up_since_s"] == 0                    # recovery starts a new count


# ---------------------------------------------------------------------------
# LOT A — backup completeness round-trip
# ---------------------------------------------------------------------------

def test_snapshot_bundle_includes_new_artifacts(tmp_path):
    import io, zipfile
    from multibus.snapshots import SnapshotStore, write_bundle_files
    cfg = write_config(tmp_path)
    (tmp_path / "builder_profiles.json").write_text('[{"id": "p1"}]')
    (tmp_path / "passkeys.json").write_text('{"creds": []}')
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "my_vm.yaml").write_text("kind: flat\n")
    store = SnapshotStore(tmp_path, tmp_path / "device_templates",
                          device_ids=lambda: [], registers_path_for=None)
    names = set(zipfile.ZipFile(io.BytesIO(store.build_bundle_bytes())).namelist())
    for expected in ("builder_profiles.json", "passkeys.json", "templates/my_vm.yaml"):
        assert expected in names, names

    # restore round-trip into a fresh dir
    out = tmp_path / "restore"
    out.mkdir(); (out / "config.yaml").write_text("modbus: {host: x}\n")
    zf = zipfile.ZipFile(io.BytesIO(store.build_bundle_bytes()))
    write_bundle_files(zf, cfg_dir=out, user_tpl_dir=out / "device_templates",
                       registers_path_for=lambda d: out / "devices" / d / "selected_registers.json",
                       replace_config=True)
    assert (out / "builder_profiles.json").exists()
    assert (out / "passkeys.json").exists()
    assert (out / "templates" / "my_vm.yaml").exists()


# ---------------------------------------------------------------------------
# LOT D — freshness clock is step-immune, not just the stop action
# ---------------------------------------------------------------------------

def test_freshness_now_rebases_during_grace(clock):
    g = vm.VirtualMeter.ClockStepGuard(15)
    g.tick()
    base = g.freshness_now()
    assert base == clock["wall"]                    # no step → raw wall clock
    clock["wall"] += 138.36                         # the incident
    g.tick()
    # during grace the freshness clock is rebased to the PRE-step wall time,
    # so a sample stored a moment ago is NOT falsely aged
    assert abs(g.freshness_now() - base) < 0.001
    # once grace ends, it tracks the (new) wall clock again
    clock["mono"] += g.grace_s + 0.1
    assert g.freshness_now() == clock["wall"]


def test_freshness_now_accumulates_multiple_steps(clock):
    """Two steps inside one window both get removed; real elapsed time (both
    clocks advancing together) is preserved, not cancelled."""
    g = vm.VirtualMeter.ClockStepGuard(15)
    g.tick()
    base = g.freshness_now()
    clock["wall"] += 100.0; g.tick()                # step 1: +100
    clock["wall"] += 1.0; clock["mono"] += 1.0      # 1s of NORMAL time passes
    clock["wall"] += 50.0;  g.tick()                # step 2: +50, still in grace
    # net wall jump is +151, but 1s was real → freshness clock = base + 1
    assert abs(g.freshness_now() - (base + 1.0)) < 0.001


def test_short_stale_window_gets_floored_grace():
    g = vm.VirtualMeter.ClockStepGuard(0.25)        # a 250ms meter
    assert g.grace_s >= 5.0                          # floored so it can ride a step


def test_rebuild_block_uses_guard_clock():
    """The per-register freshness in _rebuild_block reads freshness_now(),
    not raw time.time() — the whole verdict is step-immune."""
    import inspect
    src = inspect.getsource(vm.VirtualMeter._rebuild_block)
    assert "self._clock_guard.freshness_now()" in src
    q = inspect.getsource(vm.VirtualMeter._quality_words)
    assert "self._clock_guard.freshness_now()" in q
