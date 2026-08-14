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
data. As of 3.3.x freshness is judged on the MONOTONIC clock (driver 'mono'
stamps vs time.monotonic()), so it is immune to any wall-clock step by
construction; the ClockStepGuard survives only to emit a diagnostic event.
up_since_s is measured on the monotonic clock."""
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





def test_supervisor_ticks_guard_but_freshness_ignores_it():
    """Since freshness moved to the monotonic clock (step-immune), the guard is
    diagnostic-only: it is still ticked (for the clock_step event) but exposes
    no grace/freshness API for anything to consult."""
    import inspect
    src = inspect.getsource(vm.VirtualMeter._supervise)
    assert "clock_guard.tick()" in src                 # still logs clock steps
    g = vm.VirtualMeter.ClockStepGuard()
    assert not hasattr(g, "in_grace") and not hasattr(g, "freshness_now")


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
    import io
    import zipfile
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
# ClockStepGuard is diagnostic-only now — it just detects a step for the event
# ---------------------------------------------------------------------------

def test_clock_guard_detects_step_for_diagnostics(clock):
    g = vm.VirtualMeter.ClockStepGuard()
    assert g.tick() is False                 # first sample: baseline
    clock["wall"] += 1.0; clock["mono"] += 1.0
    assert g.tick() is False                 # normal flow: no step
    clock["wall"] += 138.0                    # NTP jump
    assert g.tick() is True
    assert g.last_step_s == pytest.approx(138.0)




def test_legacy_gate_future_or_missing_stamp_fails_closed():
    """Behavioral: the legacy per-row gate judges each stamped row on the
    monotonic clock — a fresh stamp opens the gate; a FUTURE monotonic stamp
    (corrupted) or a missing 'mono' fails the instance CLOSED, so the
    supervisor stops the meter instead of serving frozen values."""
    import time as _t
    from multibus.virtual_meter import VirtualMeter, Template, RegisterDef

    def mk(provider):
        return VirtualMeter(Template(id="t", name="t", kind="flat",
                                     transport={"port": 1502},
                                     registers=[RegisterDef(addr=0, type="float",
                                                source_kind="live", source="A")]),
                            provider, stale_after_s=15.0, on_stale="legacy")

    m = mk(lambda n: (10.0, _t.monotonic() - 3.0))
    m._rebuild_block()
    assert m._legacy_all_fresh is True             # fresh row → gate open
    m = mk(lambda n: (10.0, _t.monotonic() + 120.0))
    m._rebuild_block()
    assert m._legacy_all_fresh is False            # future stamp → fail closed
    m = mk(lambda n: (10.0, None))
    m._rebuild_block()
    assert m._legacy_all_fresh is False            # no mono stamp → fail closed


# ---------------------------------------------------------------------------
# P1 (2026-08 audit): a FUTURE timestamp is never fresh — closes the residual
# where a pre-backward-step stamp looked fresh AFTER the grace window ended
# ---------------------------------------------------------------------------

def test_future_timestamp_is_never_fresh():
    F = vm.VirtualMeter._is_fresh
    now = 1000.0
    assert F(now, 995.0, 15) is True          # 5s old, within bound
    assert F(now, 980.0, 15) is False         # 20s old, stale
    assert F(now, 1133.0, 15) is False        # 133s in the FUTURE → never fresh
    assert F(now, 1000.5, 15) is False        # slightly future → not fresh
    assert F(now, None, 15) is False          # no timestamp → not fresh



def test_json_view_marks_future_stamp_stale():
    """Behavioral: the JSON feed applies the same never-fresh rule to a FUTURE
    monotonic stamp — the row degrades to quality=stale, value=null."""
    import time as _t
    from multibus.virtual_meter import VirtualMeter, Template, RegisterDef
    m = VirtualMeter(Template(id="t", name="t", kind="flat",
                              transport={"port": 1502},
                              registers=[RegisterDef(addr=0, type="float",
                                         source_kind="live", source="A")]),
                     lambda n: (10.0, _t.monotonic() + 300.0), stale_after_s=15.0)
    view = m.json_view()
    assert view["values"]["A"]["quality"] == "stale"
    assert view["values"]["A"]["value"] is None
    assert view["complete"] is False


def test_health_state_reflects_supervisor_gate_immediately():
    """Behavioral: legacy health follows the supervisor's per-row gate — one
    stale row flips health to 'stale' at once, without waiting for
    _last_fresh_ts to age past the instance bound."""
    import time as _t
    from multibus.virtual_meter import VirtualMeter, Template, RegisterDef
    m = VirtualMeter(Template(id="t", name="t", kind="flat",
                              transport={"port": 1502},
                              registers=[RegisterDef(addr=0, type="uint16",
                                         source_kind="live", source="A")]),
                     lambda n: None, stale_after_s=15.0)
    m._last_fresh_ts = _t.monotonic() - 1.0        # recent — fresh by age alone
    m._policy_fresh = False                        # ...but the gate says stale
    assert m.health_state() == "stale"
    m._policy_fresh = True                         # gate open + recent age
    assert m.health_state() in ("ok", "down")      # (down: no server started)


def test_hold_policy_respects_future_ts_guard():
    """The 'hold' policy serves last-good within max_hold_s — a held stamp
    from before a backward clock step (future ts) must NOT extend the hold."""
    F = vm.VirtualMeter._is_fresh
    now, max_hold = 1000.0, 10.0
    assert F(now, 995.0, max_hold) is True         # held 5s ago, within hold
    assert F(now, 985.0, max_hold) is False        # held 15s ago, hold expired
    assert F(now, 1120.0, max_hold) is False        # held ts 120s in FUTURE → no hold
    # and the _rebuild_block hold branch actually uses the guard
    import inspect
    src = inspect.getsource(vm.VirtualMeter._rebuild_block)
    assert 'self._is_fresh(now, held[1]' in src


# ---------------------------------------------------------------------------
# Monotonic freshness — the DEFINITIVE clock-step immunity (3.3.0)
# ---------------------------------------------------------------------------

def test_freshness_is_immune_to_wall_clock_steps(monkeypatch):
    """Freshness now compares the driver's monotonic stamp against
    time.monotonic() — a wall-clock (time.time) jump of any size, in any
    direction, has ZERO effect on the verdict. This is the definitive fix for
    the whole clock-step class."""
    from multibus.virtual_meter import VirtualMeter

    mono = {"t": 1000.0}
    monkeypatch.setattr(vm.time, "monotonic", lambda: mono["t"])
    # a wildly lying wall clock — must not matter at all
    monkeypatch.setattr(vm.time, "time", lambda: 5.0e9)

    fed_mono = 995.0                     # value stamped 5s ago (monotonic)
    m = VirtualMeter(vm.Template(id="t", name="t", kind="flat",
                                 transport={"port": 1502},
                                 registers=[vm.RegisterDef(addr=0, type="uint16",
                                            source_kind="live", source="A")]),
                     lambda n: (10.0, fed_mono) if n == "A" else None,
                     stale_after_s=15.0, on_stale="fail")   # 'fail' populates _quality
    m._rebuild_block()
    assert m._quality.get("fresh", 0) == 1                  # fresh at 5s (monotonic)

    # advance ONLY monotonic well past the bound; wall clock still lying
    mono["t"] = 1020.0                   # now the value is 25s old (> 15s)
    monkeypatch.setattr(vm.time, "time", lambda: 1.0)   # wall jumps backward too
    m._rebuild_block()
    # genuinely stale on the monotonic clock, regardless of the wall chaos
    assert m._quality.get("stale", 0) >= 1 or m._quality.get("missing", 0) >= 1


def test_status_age_is_small_not_wall_timestamp():
    """Regression: with monotonic _last_fresh_ts, the reported freshness_age_s
    must be a small DURATION, never a ~1.7e9 wall-clock epoch."""
    from multibus.virtual_meter import VirtualMeter, Template, RegisterDef
    import time as _t
    m = VirtualMeter(Template(id="t", name="t", kind="flat",
                              transport={"port": 1502},
                              registers=[RegisterDef(addr=0, type="uint16")]),
                     lambda n: None)
    m._last_fresh_ts = _t.monotonic() - 3.0     # fresh 3s ago (monotonic)
    st = m.status()
    assert st["freshness_age_s"] is not None
    assert 0 <= st["freshness_age_s"] < 60      # a small age, NOT 1.7e9
    # last_fresh renders as a plausible RECENT wall time (this year), not 1970
    assert st["last_fresh"].startswith("20")
