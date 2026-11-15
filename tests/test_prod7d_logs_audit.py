# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Seven days of production logs (lot H): what the log, the audit and the
event ring keep.

- LOG-CMD-1  a command that succeeded is INFO, a refusal/failure WARNING; the
             audit's echo in the process log is DEBUG (the file is the record)
- AUD-RET-1  a no-op command (the device already holds it, nothing sent) is one
             audit entry + counted summaries, not a line per repeat; it does
             not evict real events from the device ring; `frames` of a no-op
             is empty and `frames_sent` says what went out
- AUD-LOGIN-1 refused logins are coalesced per client IP
- LOG-ACC-1  2xx/3xx access lines of /api/role, /health, /metrics are dropped
- LOG-OVR-1  an overrun episode clears only after N in-time sweeps over ≥ 5 min;
             sub-second intervals are printed as such
"""
import json
import logging

import pytest

from multibus.audit import AuditLog


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _entries(a):
    out = list(reversed(a.recent(1000)))                      # oldest first
    for e in out:
        e["d"] = json.loads(e["detail"]) if e.get("detail") else {}
    return out


# ── LOG-CMD-1: the audit echo ────────────────────────────────────────────────

def test_the_audit_echo_in_the_process_log_is_debug_not_info(tmp_path, caplog):
    a = AuditLog(str(tmp_path / "audit.jsonl"))
    with caplog.at_level(logging.DEBUG, logger="multibus.core.audit"):
        a.append(user="u", ip="1.2.3.4", action="command", target="gx x")
    echoes = [r for r in caplog.records if r.getMessage().startswith("AUDIT ")]
    assert echoes and all(r.levelno == logging.DEBUG for r in echoes)


# ── AUD-RET-1: the coalescer ────────────────────────────────────────────────

def _coalescer(tmp_path, every_s=3600.0):
    from multibus.core.audit import AuditCoalescer
    a = AuditLog(str(tmp_path / "audit.jsonl"))
    clk = _Clock()
    return a, AuditCoalescer(a, every_s=every_s, clock=clk), clk


def _rep(c, value, user="ems-io", target="gx ess_setpoint"):
    return c.repeat((user, target), json.dumps({"value": value}), user=user, ip="mqtt",
                    action="command", target=target, status="success",
                    detail={"params": {"value": value}, "already_set": True})


def test_a_repeated_noop_is_one_entry_then_a_summary_when_the_value_changes(tmp_path):
    a, c, clk = _coalescer(tmp_path)
    assert _rep(c, 50) is True
    for _ in range(99):
        clk.t += 10
        assert _rep(c, 50) is False
    assert len(_entries(a)) == 1                         # 100 repeats, one line
    clk.t += 10
    assert _rep(c, 60) is True                           # a new value: summary + new start
    e = _entries(a)
    assert [x["d"]["coalesce"] for x in e] == ["start", "summary", "start"]
    s = e[1]["d"]
    assert s["count"] == 99 and s["reason"] == "changed" and s["params"] == {"value": 50}
    assert s["first_ts"] == 1000.0 and s["last_ts"] == 1990.0


def test_a_long_streak_is_summarised_every_hour(tmp_path):
    a, c, clk = _coalescer(tmp_path)
    _rep(c, 50)
    for _ in range(3 * 60):                              # three hours, once a minute
        clk.t += 60
        _rep(c, 50)
    sums = [x["d"] for x in _entries(a) if x["d"]["coalesce"] == "summary"]
    assert len(sums) == 3 and all(s["reason"] == "hourly" for s in sums)
    c.flush()                                            # the tail of the last hour
    sums = [x["d"] for x in _entries(a) if x["d"]["coalesce"] == "summary"]
    assert sum(s["count"] for s in sums) == 180          # nothing counted twice or lost


def test_end_closes_the_streak_and_an_idle_streak_is_summarised_by_any_later_call(tmp_path):
    a, c, clk = _coalescer(tmp_path)
    _rep(c, 50)
    _rep(c, 50)
    c.end(lambda g: g[1] == "gx ess_setpoint")           # the value moved
    assert [x["d"]["coalesce"] for x in _entries(a)] == ["start", "summary"]
    assert _entries(a)[1]["d"]["reason"] == "ended"
    _rep(c, 50, target="gx max_charge_current")
    _rep(c, 50, target="gx max_charge_current")
    clk.t += 4000                                        # the EMS stopped
    _rep(c, 7, target="other x")
    e = _entries(a)
    assert e[-2]["d"]["reason"] == "idle" and e[-2]["target"] == "gx max_charge_current"
    assert e[-1]["target"] == "other x"


def test_a_single_noop_writes_no_empty_summary(tmp_path):
    a, c, clk = _coalescer(tmp_path)
    _rep(c, 50)
    c.flush()
    assert len(_entries(a)) == 1


def test_retention_holds_more_than_before(tmp_path):
    from multibus.core import audit as A
    assert A._ROTATE_BYTES * (A._KEEP_FILES + 1) >= 10 * (1 << 20)


# ── AUD-RET-1 + LOG-CMD-1 through the command path ──────────────────────────


@pytest.fixture(scope="module")
def gx():
    from multibus.device_template import TemplateRegistry
    return TemplateRegistry().get("victron_gx_mqtt")


def _audit_for(h, target):
    ents = h.client.get("/api/audit?limit=500").json()["entries"]
    out = [e for e in reversed(ents) if e.get("target") == target]
    for e in out:
        e["d"] = json.loads(e["detail"]) if e.get("detail") else {}
    return out


def test_an_ems_resending_what_the_device_holds_leaves_one_entry_and_one_event(tmp_path, gx, caplog):
    from tests.test_command_binding import _Gx
    h = _Gx(tmp_path, gx)
    with caplog.at_level(logging.DEBUG):
        for i in range(6):                               # the GX holds 420 already
            h.mqtt("gx", "max_charge_current", 420, id=f"n{i}")
            res = h.result(f"n{i}")
            assert res["already_set"] is True and res["frames_sent"] == 0
    ents = _audit_for(h, "gx max_charge_current")
    assert len(ents) == 1, ents
    d = ents[0]["d"]
    assert d["coalesce"] == "start" and d["already_set"] is True
    assert d["frames"] == [] and d["frames_sent"] == 0   # nothing went on the wire
    cmd_events = [e for e in h.src["gx"].events if e["kind"] == "command"]
    assert len(cmd_events) == 1                          # repeats do not evict real events
    lines = [r for r in caplog.records if r.getMessage().startswith("COMMAND SUCCESS")]
    assert lines and all(r.levelno < logging.WARNING for r in lines)
    assert sum(r.levelno == logging.INFO for r in lines) == 1

    # a real change: the streak's summary first, then the change on its own
    h.t[0] += 3600
    h.mqtt("gx", "max_charge_current", 300, id="c1")
    assert h.result("c1")["status"] == "success"
    ents = _audit_for(h, "gx max_charge_current")
    assert [e["d"].get("coalesce") for e in ents] == ["start", "summary", None]
    assert ents[1]["d"]["count"] == 5
    assert ents[2]["d"]["frames_sent"] == 1 and len(ents[2]["d"]["frames"]) == 1


def test_a_refused_command_is_still_a_warning_and_its_own_entry(tmp_path, gx, caplog):
    from tests.test_command_binding import _Gx
    from tests.test_mqtt_commands import _say
    h = _Gx(tmp_path, gx)
    _say(h.src["gx"], "settings/0/Settings/CGwacs/Hub4Mode", 3)     # guard refuses
    with caplog.at_level(logging.DEBUG):
        for i in range(2):
            h.mqtt("gx", "ess_setpoint", -500, id=f"r{i}")
            assert h.result(f"r{i}")["status"] == "rejected"
    lines = [r for r in caplog.records if r.getMessage().startswith("COMMAND REJECTED")]
    assert len(lines) == 2 and all(r.levelno == logging.WARNING for r in lines)
    assert len(_audit_for(h, "gx ess_setpoint")) == 2


# ── AUD-LOGIN-1 ─────────────────────────────────────────────────────────────

def test_refused_logins_are_coalesced_per_ip_and_a_success_closes_the_streak(tmp_path):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from multibus import auth as _a
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    cfg.ui.auth_enabled = True
    cfg.ui.auth_username = "boss"
    cfg.ui.auth_password = _a.hash_password("pw")
    cfg.ui.lockout_threshold = 1000                          # keep it on "invalid credentials"
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    for u in ("boss", "root", "admin", "boss"):
        assert client.post("/api/auth/login", json={"username": u, "password": "x"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "boss", "password": "pw"}).status_code == 200
    ents = [e for e in reversed(client.get("/api/audit?limit=100").json()["entries"])
            if e["action"] == "login"]
    assert [e["status"] for e in ents] == ["invalid credentials", "invalid credentials", "ok"]
    s = json.loads(ents[1]["detail"])
    assert s["coalesce"] == "summary" and s["count"] == 3
    assert set(s["notes"]) == {"boss", "root", "admin"}


# ── LOG-ACC-1 ───────────────────────────────────────────────────────────────

def _acc(path, status, client="10.0.0.5:4242"):
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                             '%s - "%s %s HTTP/%s" %d', (client, "GET", path, "1.1", status), None)


def test_successful_probe_lines_are_dropped_and_everything_else_kept():
    from multibus import ws_log
    f = ws_log.AccessProbeFilter()
    for p in ("/api/role", "/health", "/metrics", "/metrics?x=1"):
        assert f.filter(_acc(p, 200)) is False
        assert f.filter(_acc(p, 304)) is False
    assert f.filter(_acc("/api/role", 403)) is True              # the refusals stay
    assert f.filter(_acc("/health", 500)) is True
    assert f.filter(_acc("/api/status", 200)) is True
    assert f.filter(_acc("/api/roles", 200)) is True
    other = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "hello %s", ("x",), None)
    assert f.filter(other) is True


def test_install_puts_the_access_filter_on_uvicorn_access_once():
    from multibus import ws_log
    lg = logging.getLogger("uvicorn.access")
    before = list(lg.filters)
    try:
        ws_log.install()
        ws_log.install()
        assert sum(isinstance(x, ws_log.AccessProbeFilter) for x in lg.filters) == 1
    finally:
        lg.filters[:] = before


# ── LOG-OVR-1 ───────────────────────────────────────────────────────────────

def _poller(interval):
    from multibus.modbus_client import RegisterPoller
    from multibus.register_parser import RegisterParser
    from tests.test_device_events import _conn
    c = _conn()
    return RegisterPoller("fast", interval, [], c, RegisterParser("big"), lambda *a: None, "u1"), c


def _kinds(c):
    return [e["kind"] for e in c.snapshot_events() if e["kind"].startswith("overrun")]


def test_one_in_time_sweep_does_not_clear_an_overrun_episode():
    from multibus.transports.modbus import modbus_client as M
    p, c = _poller(5)
    t = 0.0
    for _ in range(48):                       # an hour of hovering at the interval
        p._note_sweep(6.0, t); t += 6
        p._note_sweep(4.0, t); t += 5
    assert _kinds(c) == ["overrun"]
    assert p.overruns == 48
    # in time for N sweeps but under 5 min: still open
    for _ in range(M.OVERRUN_CLEAR_SWEEPS):
        p._note_sweep(4.0, t); t += 5
    assert _kinds(c) == ["overrun"]
    while t < 1000:                           # and well over 5 min: cleared, once
        p._note_sweep(4.0, t); t += 5
    assert _kinds(c) == ["overrun", "overrun_cleared"]
    p._note_sweep(6.0, t)
    assert _kinds(c) == ["overrun", "overrun_cleared", "overrun"]


def test_long_in_time_but_too_few_sweeps_does_not_clear():
    from multibus.transports.modbus import modbus_client as M
    p, c = _poller(120)
    p._note_sweep(130.0, 0.0)
    for i in range(1, M.OVERRUN_CLEAR_SWEEPS):
        p._note_sweep(1.0, i * 400.0)
    assert _kinds(c) == ["overrun"]
    p._note_sweep(1.0, 10_000.0)
    assert _kinds(c) == ["overrun", "overrun_cleared"]


def test_a_sub_second_interval_is_printed_as_such(caplog):
    p, c = _poller(0.25)
    with caplog.at_level(logging.WARNING):
        p._note_sweep(0.4, 0.0)
    msg = [e["message"] for e in c.snapshot_events() if e["kind"] == "overrun"][0]
    assert "interval is 0.25s" in msg and "took 0.4s" in msg
    assert any("interval is 0.25s" in r.getMessage() for r in caplog.records)
