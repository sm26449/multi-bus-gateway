# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Bus diagnostics in the gateway (multibus/bridge_diag.py, ADR-0017): the
correlation table, the window arithmetic, the thresholds and their alerts,
and the poller's cache — one fetch per bridge per poll, never per request."""
from types import SimpleNamespace

import pytest

from multibus import bridge_diag as bd


# ── the correlation table ────────────────────────────────────────────────────
CLEAN = {"frame": 0, "parity": 0, "overrun": 0, "brk": 0, "buf_overrun": 0}


def W(**kw):
    """A window of evidence; by default a healthy, busy bus."""
    base = {"reachable": True, "window_s": 300.0,
            "mbg": {"ok": 600, "timeouts": 0, "connection": 0, "exceptions": 0, "other": 0, "crc": 0},
            "line": dict(CLEAN), "bytes": {"from_device": 9000, "to_device": 4800},
            "connects": 0, "silent_s": 1.0, "client": True}
    for k, v in kw.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


@pytest.mark.parametrize("name,w,code,confidence", [
    ("healthy", W(), "ok", "high"),
    ("bridge does not answer", {"reachable": False}, "no_data", "high"),
    ("first sample", W(window_s=0), "collecting", "high"),
    # timeouts + clean line + TCP reconnects → the network
    ("network", W(mbg={"timeouts": 12}, connects=5), "network", "high"),
    ("network, connection errors", W(mbg={"connection": 4}, connects=3), "network", "high"),
    ("network, adapter counts nothing", W(mbg={"timeouts": 12}, connects=5, line=None),
     "network", "low"),
    # timeouts/CRC + frame/parity errors → wiring, termination, baud
    ("wiring", W(mbg={"timeouts": 8}, line={"frame": 14}), "wiring", "high"),
    ("wiring, garbled (tap CRC)", W(mbg={"crc": 3}, line={"parity": 2}), "wiring", "high"),
    ("wiring beats network", W(mbg={"timeouts": 8}, line={"frame": 4}, connects=4), "wiring", "high"),
    # timeouts + nothing back → the device
    ("device silent", W(mbg={"timeouts": 30, "ok": 0}, bytes={"from_device": 0, "to_device": 2400},
                        silent_s=290.0), "device_silent", "high"),
    ("device silent, no reconnect storm blamed on the network",
     W(mbg={"timeouts": 30, "ok": 0}, bytes={"from_device": 0, "to_device": 2400}, connects=3,
       silent_s=290.0), "device_silent", "high"),
    # overrun → the host
    ("host overrun", W(line={"overrun": 3}), "host_overrun", "high"),
    ("host overrun before wiring", W(mbg={"timeouts": 2}, line={"overrun": 3, "frame": 5}),
     "host_overrun", "high"),
    ("buffer overrun", W(line={"buf_overrun": 1}), "host_overrun", "high"),
    # one slave missing among answering ones
    ("one slave times out", W(mbg={"timeouts": 6}), "timeouts", "high"),
    ("line errors, reads fine", W(line={"frame": 2}), "line_noise", "high"),
    ("nobody of ours on the bus", W(mbg=None), "ok", "high"),
    # an older bridge: nothing known about the line is not a healthy line
    ("bridge without diagnostics", W(supported=False, line=None, bytes=None, connects=None),
     "unsupported", "high"),
    ("served port not sampled yet", W(stats_known=False), "collecting", "high"),
])
def test_diagnose(name, w, code, confidence):
    v = bd.diagnose(w)
    assert v["code"] == code, (name, v)
    assert v["confidence"] == confidence, name
    assert v["severity"] == bd.SEVERITY[code]
    assert v["findings"][0] == code


def test_diagnose_lists_every_finding_in_order():
    v = bd.diagnose(W(mbg={"timeouts": 4}, line={"overrun": 1, "frame": 2}))
    assert v["findings"] == ["host_overrun", "wiring"]
    assert v["evidence"]["line_overrun"] == 1 and v["evidence"]["line_wiring"] == 2


def test_every_code_has_a_severity():
    assert set(bd.CODES) == set(bd.SEVERITY)


# ── counters → window ────────────────────────────────────────────────────────
def _stats(ok=0, timeout=0, connection=0, exc=0):
    return {"successful_reads": ok, "error_counts": {"timeout": timeout, "connection": connection,
                                                    "exception_2": exc, "other": 0}}


def test_mbg_counts_sum_the_bus():
    c = bd.mbg_counts([_stats(10, 2, 1, 1), _stats(5, 1), None,
                       {"successful_reads": 3, "error_counts": {"crc_errors": 4}}])
    assert c == {"ok": 18, "timeouts": 3, "connection": 1, "exceptions": 1, "other": 0,
                 "crc": 4, "devices": 3, "max_interval_s": None}
    slow = {**_stats(1), "poll_groups_detail": [{"interval": 5}, {"interval": 240}, {"interval": "x"}]}
    assert bd.mbg_counts([slow, _stats(1)])["max_interval_s"] == 240.0


def _sample(ts, frame=0, rx=0, tx=0, connects=1, ok=0, timeouts=0, line=True, silent=1.0):
    return {"ts": ts, "stats": {"line": ({**CLEAN, "frame": frame} if line else None),
                                "bytes": {"from_device": rx, "to_device": tx},
                                "connects": connects, "silent_s": silent,
                                "client": {"address": "10.0.0.2:1"}},
            "mbg": {"ok": ok, "timeouts": timeouts, "connection": 0, "exceptions": 0, "other": 0,
                    "crc": 0, "devices": 1}}


def test_window_uses_the_oldest_sample_inside_it():
    s = [_sample(0, frame=100), _sample(100, frame=110, rx=100, tx=50, ok=10),
         _sample(400, frame=130, rx=400, tx=200, ok=40, timeouts=3, connects=4)]
    w = bd.window_of(s, 400)
    assert w["window_s"] == 300                       # the 0 s sample is outside 5 min
    assert w["line"]["frame"] == 20 and w["bytes"] == {"from_device": 300, "to_device": 150}
    assert w["mbg"]["ok"] == 30 and w["mbg"]["timeouts"] == 3 and w["connects"] == 3
    r = bd.rates(w)
    assert r["frame_per_min"] == 4.0 and r["line_errors_per_min"] == 4.0
    assert r["mbg_timeouts_per_min"] == 0.6


def test_window_counter_reset_and_unknown_line():
    s = [_sample(0, frame=50), _sample(60, frame=3)]          # adapter replugged
    assert bd.window_of(s, 60)["line"]["frame"] == 0           # a new base, never a spike
    assert bd._delta(3, 50) == 0 and bd._delta(53, 50) == 3 and bd._delta(None, 1) is None
    s = [_sample(0, line=False), _sample(60, line=False)]     # a CH340
    w = bd.window_of(s, 60)
    assert w["line"] is None and bd.rates(w)["line_errors_per_min"] is None
    assert bd.window_of([], 0) == {"window_s": 0}
    one = bd.window_of([_sample(5)], 5)
    assert one["window_s"] == 0 and bd.diagnose(one)["code"] == "collecting"


# ── thresholds ───────────────────────────────────────────────────────────────
def test_thresholds_layer_port_over_bridge_over_global():
    b = {"id": "pi", "alerts": {"silent_s": 60},
         "ports": [{"port": 7001, "alerts": {"line_errors_per_min": 2}},
                   {"port": 7002, "alerts": {"enabled": False}}]}
    th = bd.thresholds_for({"reconnects_per_10min": 3}, b, 7001)
    assert th == {"line_errors_per_min": 2.0, "silent_s": 60.0, "reconnects_per_10min": 3.0,
                  "enabled": True}
    assert bd.thresholds_for({}, b, 7002)["enabled"] is False
    assert bd.thresholds_for(None, {"id": "x"}, 7009) == {**bd.DEFAULT_THRESHOLDS, "enabled": True}


def test_validate_thresholds():
    assert bd.validate_thresholds(None, "alerts") == []
    assert bd.validate_thresholds({"silent_s": 30, "enabled": True}, "alerts") == []
    errs = " | ".join(bd.validate_thresholds({"silent_s": -1, "x": 1, "enabled": "yes"}, "alerts"))
    assert "alerts.silent_s" in errs and "alerts.x" in errs and "alerts.enabled" in errs
    assert bd.validate_thresholds([1], "alerts")


def test_alert_conditions():
    th = {**bd.DEFAULT_THRESHOLDS, "enabled": True}
    assert bd.alert_conditions(W(), th) == {"line": None, "silent": None, "reconnects": None}
    c = bd.alert_conditions(W(line={"frame": 60}), th)                     # 12/min > 10
    assert c["line"] and "12/min" in c["line"]
    assert bd.alert_conditions(W(line={"frame": 40}), th)["line"] is None  # 8/min
    c = bd.alert_conditions(W(silent_s=200.0), th)
    assert c["silent"]
    # silent but nobody asks: not an alarm
    assert bd.alert_conditions(W(silent_s=200.0, bytes={"to_device": 0}), th)["silent"] is None
    assert bd.alert_conditions(W(connects=3), th)["reconnects"] is None   # 6 per 10 min: at the limit
    assert bd.alert_conditions(W(connects=4), th)["reconnects"]           # 8 per 10 min
    assert bd.alert_conditions(W(connects=4, window_s=600.0), th)["reconnects"] is None
    assert bd.alert_conditions(W(connects=7, window_s=600.0), th)["reconnects"]
    assert bd.alert_conditions(W(line={"frame": 600}), {**th, "enabled": False})["line"] is None
    # an adapter that counts nothing never raises a line alert
    assert bd.alert_conditions(W(line=None), th)["line"] is None


# ── the poller ───────────────────────────────────────────────────────────────
class FakeConfig:
    def __init__(self, bridges, devices=()):
        self.bridges = bridges
        self.devices = list(devices)

    def bridge_devices(self, bid):
        return [d for d in self.devices if getattr(d, "bridge", "") == bid]


class FakeClient:
    def __init__(self, st):
        self.st = st

    def get_stats(self):
        return dict(self.st)


class FakeRegistry:
    def __init__(self, pairs):
        self._pairs = pairs

    def pairs(self):
        return list(self._pairs)


class FakeAlerts:
    def __init__(self, on=True, th=None):
        self.sig_bridge = on
        self.bridge_thresholds = th or {}
        self.fired = []

    def fire(self, severity, key, source, message, transition=False):
        self.fired.append((severity, key, message, transition))


class FakeMqtt:
    def __init__(self):
        self.pub = []

    def publish_state(self, sub, payload, retain=True):
        self.pub.append((sub, payload))


class FakeInflux:
    def __init__(self):
        self.points = []

    def write_point(self, p, ts=None, bucket=None):
        self.points.append(p)


BRIDGE = {"id": "pi", "name": "Pi garage", "type": "mbg_serial_bridge", "host": "192.0.2.9",
          "token": "tok", "ports": [{"port": 7001, "adapter": "B0045K08"}]}


def _adapter(frame=0, rx=0, tx=0, connects=1, silent=1.0, line=True):
    return {"stable_id": "B0045K08", "tcp_port": 7001, "status": "ok", "label": "FT232R",
            "driver": "ftdi_sio",
            "stats": {"line": ({**CLEAN, "frame": frame, "rx": rx, "tx": tx} if line else None),
                      "line_note": "", "bytes": {"from_device": rx, "to_device": tx},
                      "connects": connects, "connects_10min": connects, "silent_s": silent,
                      "client": {"address": "172.20.0.5:40000", "since": 1.0}}}


class Bridge:
    """A fake bridge API: what /adapters and /events answer, and every call."""
    def __init__(self, boot="b1", diagnostics=True):
        self.adapters = [_adapter()]
        self.events = []
        self.boot = boot
        self.diagnostics = diagnostics
        self.calls = []
        self.down = False

    def __call__(self, url, token):
        self.calls.append((url, token))
        if self.down:
            raise OSError("connection refused")
        if url.endswith("/adapters"):
            out = {"version": "3.93.0", "adapters": [dict(a) for a in self.adapters]}
            if self.diagnostics:
                out["diagnostics"] = {"boot": self.boot, "interval_s": 5}
            return out
        since = int(url.split("since=")[1])
        evs = [e for e in self.events if e["seq"] > since]
        return {"boot": self.boot, "events": evs, "next": max([since] + [e["seq"] for e in evs]),
                "lost": False}


def _diag(bridge, clock, devices=(), pairs=(), alerts=None, active=True):
    mq, ix = FakeMqtt(), FakeInflux()
    d = bd.BridgeDiag(FakeConfig([BRIDGE], devices), FakeRegistry(pairs), alert_mgr=alerts,
                      publishers=lambda: (mq, ix), fetch=bridge, clock=lambda: clock[0],
                      is_active=lambda: active)
    return d, mq, ix


def test_one_fetch_per_poll_and_views_from_cache():
    br, clock = Bridge(), [1000.0]
    d, _mq, _ix = _diag(br, clock)
    d.poll_once()
    assert [u for u, _t in br.calls] == ["http://192.0.2.9:7000/adapters",
                                         "http://192.0.2.9:7000/events?since=0"]
    assert all(t == "tok" for _u, t in br.calls)
    n = len(br.calls)
    for _ in range(5):                                # a busy Devices page
        d.port_view("pi", 7001)
        d.bridge_view("pi")
    assert len(br.calls) == n
    assert d.port_view("pi", 7001)["diagnosis"]["code"] == "collecting"
    assert d.port_view("nope", 7001) is None and d.port_view("pi", 7999) is None


def test_poll_correlates_with_the_gateways_own_counters():
    br, clock = Bridge(), [1000.0]
    dev = SimpleNamespace(id="m1", bridge="pi", enabled=True,
                          connection=SimpleNamespace(host="192.0.2.9", port=7001))
    other = SimpleNamespace(id="m2", bridge="", enabled=True,
                            connection=SimpleNamespace(host="192.0.2.50", port=502))
    cl = FakeClient(_stats(ok=100))
    d, _mq, _ix = _diag(br, clock, devices=[dev, other],
                        pairs=[(dev, cl), (other, FakeClient(_stats(timeout=999)))])
    d.poll_once()
    # 60 s later: timeouts on our side, frame errors on the line → wiring
    clock[0] = 1060.0
    cl.st = _stats(ok=110, timeout=9)
    br.adapters = [_adapter(frame=12, rx=500, tx=400, connects=1)]
    d.poll_once()
    v = d.port_view("pi", 7001)
    assert v["diagnosis"]["code"] == "wiring"
    assert v["mbg"]["timeouts"] == 9 and v["mbg"]["ok"] == 10   # the other bus is not counted
    assert v["rates"]["frame_per_min"] == 12.0


def test_events_follow_since_and_a_restarted_bridge_starts_over():
    br, clock = Bridge(), [1000.0]
    br.events = [{"seq": 1, "kind": "started", "key": "", "tcp_port": None, "message": "x", "ts": 1},
                 {"seq": 2, "kind": "client_connected", "key": "B0045K08", "tcp_port": 7001,
                  "message": "client", "ts": 2}]
    d, _mq, _ix = _diag(br, clock)
    d.poll_once()
    clock[0] += 30
    d.poll_once()
    assert br.calls[-1][0].endswith("since=2")
    assert [e["seq"] for e in d.bridge_view("pi")["events"]] == [1, 2]
    assert [e["kind"] for e in d.port_view("pi", 7001)["events"]] == ["client_connected"]
    # the bridge restarted: new boot, its numbers began again
    br.boot, br.events = "b2", [{"seq": 1, "kind": "started", "key": "", "tcp_port": None,
                                 "message": "again", "ts": 3}]
    clock[0] += 30
    d.poll_once()
    assert br.calls[-1][0].endswith("since=0")
    assert d.bridge_view("pi")["events"][-1]["message"] == "again"
    assert len(d._bridges["pi"]["ports"][7001]["samples"]) == 1      # history restarted


def test_an_older_bridge_is_not_asked_for_events_and_never_reads_healthy():
    """Review E1b #1: a bridge without diagnostics (3.91) read `ok` with high
    confidence, a green chip, and published diagnosis=ok."""
    import json
    br, clock = Bridge(diagnostics=False), [1000.0]
    br.adapters = [{k: v for k, v in _adapter().items() if k != "stats"}]
    d, mq, ix = _diag(br, clock)
    for _ in range(3):
        d.poll_once()
        clock[0] += 30
    assert [u for u, _t in br.calls][:2] == ["http://192.0.2.9:7000/adapters"] * 2
    v = d.port_view("pi", 7001)
    assert v["supported"] is False and v["stats"] is None
    assert v["diagnosis"]["code"] == "unsupported" and v["diagnosis"]["severity"] == "unknown"
    assert all(json.loads(p)["diagnosis"] == "unsupported" for _t, p in mq.pub)
    assert ix.points == []                              # nothing measured, nothing written


def test_a_served_port_without_a_sample_yet_is_collecting():
    br, clock = Bridge(), [1000.0]
    br.adapters = [{**_adapter(), "stats": None}]
    d, _mq, _ix = _diag(br, clock)
    d.poll_once()
    clock[0] += 30
    d.poll_once()
    assert d.port_view("pi", 7001)["diagnosis"]["code"] == "collecting"


def test_an_unanswering_bridge_is_no_data():
    br, clock = Bridge(), [1000.0]
    d, _mq, _ix = _diag(br, clock)
    d.poll_once()
    br.down = True
    clock[0] += 30
    d.poll_once()
    v = d.port_view("pi", 7001)
    assert v["diagnosis"]["code"] == "no_data" and "no answer" in v["error"]


def _line_kinds(am, kind="line"):
    return [f[0] for f in am.fired if f[1] == f"bridge:pi:7001:{kind}"]


def test_alerts_need_a_full_window_two_polls_and_clear_below_80_percent():
    """Review E1b #2: edge-triggered with no hysteresis and judged from the
    first poll. Now: a full 5-minute window, two polls over the limit, and
    back to normal only after two polls under 80 % of it."""
    br, clock = Bridge(), [1000.0]
    am = FakeAlerts(on=True, th={"line_errors_per_min": 5})
    d, _mq, _ix = _diag(br, clock, alerts=am)
    first = None
    for i in range(20):                                 # 30 frame errors a minute
        clock[0] = 1000.0 + 30 * i
        br.adapters = [_adapter(frame=15 * i, rx=100 * i, tx=100 * i)]
        d.poll_once()
        if first is None and _line_kinds(am):
            first = i
    assert first == 11                  # history 300 s at poll 10 (over once), fired at 11
    for i in range(20, 40):                             # the errors stop
        clock[0] = 1000.0 + 30 * i
        br.adapters = [_adapter(frame=15 * 19, rx=100 * i, tx=100 * i)]
        d.poll_once()
    assert _line_kinds(am) == ["warn", "info"]
    assert all(f[3] is True for f in am.fired)


def test_step_hysteresis():
    st = {}
    lv = lambda v: (v, 10.0, "m")  # noqa: E731
    assert [bd.step(st, lv(v)) for v in (11, 9, 11, 9, 11, 9)] == [None] * 6  # never twice in a row
    assert bd.step(st, lv(11)) is None and bd.step(st, lv(11)) == "fire"
    assert bd.step(st, lv(11)) is None                                         # already active
    assert [bd.step(st, lv(v)) for v in (9, 7, 9, 7, 9)] == [None] * 5         # 9 is not under 80 %
    assert bd.step(st, lv(7)) is None and bd.step(st, lv(7)) == "clear"
    assert bd.step(st, None) is None and st["active"] is False


def test_scenario_b_just_restarted_no_rate_alert():
    """One reconnect and a burst of line errors in the first 30 s used to
    compute as 10 reconnects per 10 minutes."""
    br, clock = Bridge(), [1000.0]
    am = FakeAlerts(on=True)
    d, _mq, _ix = _diag(br, clock, alerts=am)
    for i in range(10):                                  # 270 s of history
        clock[0] = 1000.0 + 30 * i
        br.adapters = [_adapter(frame=40 if i else 0, connects=2 if i else 1, rx=10 * i, tx=10 * i)]
        d.poll_once()
    assert am.fired == []
    w = bd.window_of(list(d._bridges["pi"]["ports"][7001]["samples"]), clock[0])
    assert bd.alert_levels(w, {**bd.DEFAULT_THRESHOLDS, "enabled": True})["reconnects"] is None


def _slow_device(interval):
    dev = SimpleNamespace(id="hp", bridge="pi", enabled=True,
                          connection=SimpleNamespace(host="192.0.2.9", port=7001))
    st = {**_stats(ok=10), "poll_groups_detail": [{"interval": 5}, {"interval": interval}]}
    return dev, FakeClient(st)


@pytest.mark.parametrize("interval,fires", [(240, False), (5, True)])
def test_scenario_a_silent_bus_follows_the_slowest_poll(interval, fires):
    """A device read every 4 minutes is silent 4 minutes between reads: the
    silent limit is max(silent_s, 2 x the bus's longest poll interval)."""
    br, clock = Bridge(), [1000.0]
    am = FakeAlerts(on=True)
    dev, cl = _slow_device(interval)
    d, _mq, _ix = _diag(br, clock, devices=[dev], pairs=[(dev, cl)], alerts=am)
    for i in range(12):                    # asked every poll, nothing back for 200 s
        clock[0] = 1000.0 + 30 * i
        br.adapters = [_adapter(rx=50, tx=100 * (i + 1), silent=200.0 if i > 3 else 30.0)]
        d.poll_once()
    assert (_line_kinds(am, "silent") == ["warn"]) is fires, am.fired
    w = bd.window_of(list(d._bridges["pi"]["ports"][7001]["samples"]), clock[0])
    assert w["poll_interval_s"] == float(interval)


def test_no_alert_when_the_signal_is_off_or_on_a_standby():
    for alerts, active in ((FakeAlerts(on=False), True), (FakeAlerts(on=True), False)):
        br, clock = Bridge(), [1000.0]
        d, mq, ix = _diag(br, clock, alerts=alerts, active=active)
        d.poll_once()
        clock[0] = 1060.0
        br.adapters = [_adapter(frame=600, rx=10, tx=10)]
        d.poll_once()
        assert alerts.fired == []
        if not active:
            assert mq.pub == [] and ix.points == []    # a standby does not announce


def test_health_published_under_the_gateway_prefix_and_influx():
    br, clock = Bridge(), [1000.0]
    d, mq, ix = _diag(br, clock)
    d.poll_once()
    clock[0] = 1060.0
    br.adapters = [_adapter(frame=2, rx=50, tx=40)]
    d.poll_once()
    topics = {t for t, _p in mq.pub}
    assert topics == {"bridges/pi/7001/health"}
    import json
    last = json.loads(mq.pub[-1][1])
    assert last["diagnosis"] == "line_noise" and last["adapter"] == "B0045K08"
    assert last["client"] == "172.20.0.5:40000" and last["line"]["frame"] == 2
    lp = ix.points[-1].to_line_protocol()
    assert lp.startswith("bridge_port,adapter=B0045K08,bridge=pi,port=7001 ")
    assert "line_frame=2i" in lp and 'diagnosis="line_noise"' in lp


def test_one_bad_bridge_never_stops_the_others_nor_the_cleanup():
    good = Bridge()
    odd = {"id": "odd", "type": "mbg_serial_bridge", "host": "192.0.2.10"}
    bad = {"id": "bad", "type": "mbg_serial_bridge", "host": "192.0.2.11"}

    def fetch(url, token):
        if "192.0.2.10" in url:
            if url.endswith("/adapters"):
                return {"diagnostics": {"boot": "x"}, "adapters": [
                    "nonsense", {"tcp_port": "abc", "status": "ok"}, {"tcp_port": True, "status": "ok"},
                    {"tcp_port": 7009, "status": "ok", "stable_id": "Z", "stats": "garbage"}]}
            return {"events": "nope", "next": 0}
        if "192.0.2.11" in url:
            return ["not", "an", "object"]
        return good(url, token)
    clock = [1000.0]
    mq, ix = FakeMqtt(), FakeInflux()
    cfg = FakeConfig([bad, odd, BRIDGE])
    d = bd.BridgeDiag(cfg, FakeRegistry([]), publishers=lambda: (mq, ix), fetch=fetch,
                      clock=lambda: clock[0], is_active=lambda: True)
    d.poll_once()
    assert d.port_view("pi", 7001) is not None                    # the good one still polled
    assert d.port_view("odd", 7009)["stats"] is None              # garbage stats dropped
    assert set(d._bridges["odd"]["ports"]) == {7009}
    cfg.bridges = [bad]
    d.poll_once()                                                 # the bad one is polled first
    assert d.bridge_view("pi") is None and d.bridge_view("odd") is None


def test_http_get_caps_the_body_and_the_time():
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            if self.path == "/big":
                self.wfile.write(b"[" + b"1," * (1 << 20) + b"1]")
            elif self.path == "/slow":
                import time as _t
                for _ in range(20):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    _t.sleep(0.1)
                self.wfile.write(b"{}")
            elif self.path == "/list":
                self.wfile.write(b"[1]")
            else:
                self.wfile.write(b'{"ok": 1}')

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert bd._http_get(base + "/ok", "") == {"ok": 1}
        with pytest.raises(ValueError):
            bd._http_get(base + "/big", "")
        with pytest.raises(ValueError):
            bd._http_get(base + "/list", "")
        with pytest.raises(TimeoutError):
            bd._http_get(base + "/slow", "", timeout=0.5)
    finally:
        srv.shutdown()
        srv.server_close()


def test_alert_state_pruned_when_switched_off_or_gone():
    def storm(d, br, clock, start, n):
        for i in range(start, start + n):
            clock[0] = 1000.0 + 30 * i
            br.adapters = [_adapter(frame=15 * i, rx=10 * i, tx=10 * i)]
            d.poll_once()
    br, clock = Bridge(), [1000.0]
    am = FakeAlerts(on=True, th={"line_errors_per_min": 5})
    d, _mq, _ix = _diag(br, clock, alerts=am)
    storm(d, br, clock, 0, 13)
    assert _line_kinds(am) == ["warn"]
    am.sig_bridge = False                        # switched off: forgotten, no "back to normal"
    for i in range(13, 30):                      # and the bus goes quiet meanwhile
        clock[0] = 1000.0 + 30 * i
        br.adapters = [_adapter(frame=15 * 12, rx=10 * i, tx=10 * i)]
        d.poll_once()
        assert not [k for k in d._alerting if k.startswith("bridge:pi:")]
    am.sig_bridge = True
    for i in range(30, 40):
        clock[0] = 1000.0 + 30 * i
        br.adapters = [_adapter(frame=15 * 12, rx=10 * i, tx=10 * i)]
        d.poll_once()
    assert _line_kinds(am) == ["warn"]           # no orphan clear
    # gone with an alert active: dropped silently
    storm(d, br, clock, 40, 13)
    n = len(am.fired)
    d.config.bridges = []
    d.poll_once()
    assert len(am.fired) == n and not d._alerting


def test_a_standby_leaves_the_retained_health_alone():
    br, clock = Bridge(), [1000.0]
    d, mq, _ix = _diag(br, clock, active=True)
    d.poll_once()
    d.is_active = lambda: False
    n = len(mq.pub)
    d.config.bridges = []
    d.poll_once()
    assert len(mq.pub) == n


def test_a_removed_bridge_clears_its_retained_health():
    br, clock = Bridge(), [1000.0]
    d, mq, _ix = _diag(br, clock)
    d.poll_once()
    d.config.bridges = []
    d.poll_once()
    assert mq.pub[-1] == ("bridges/pi/7001/health", "")
    assert d.bridge_view("pi") is None


# ── the routes ───────────────────────────────────────────────────────────────
from tests.test_devices_api import make_app, needs_tc  # noqa: E402


@needs_tc
def test_diagnostics_route_serves_the_cache(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/bridges", json={"id": "pi", "type": "mbg_serial_bridge", "host": "192.0.2.9",
                                          "ports": [{"port": 7001}]})
    assert r.status_code == 200, r.text
    diag = client.app.state.bridge_diag
    br = Bridge()
    diag.fetch = br
    diag.is_active = lambda: True
    empty = client.get("/api/bridges/pi/diagnostics").json()
    assert empty["ports"] == {} and empty["poll_s"] == bd.POLL_S
    diag.poll_once()
    n = len(br.calls)
    d = client.get("/api/bridges/pi/diagnostics").json()
    assert d["ports"]["7001"]["adapter"] == "B0045K08" and d["supported"] is True
    card = client.get("/api/bridges/pi").json()
    port = next(p for p in card["ports"] if p["port"] == 7001)
    assert port["diag"]["diagnosis"]["code"] == "collecting"
    assert port["diag"]["client"] == "172.20.0.5:40000"
    assert len(br.calls) == n                          # no fetch from a request
    # a converter has no API to ask
    client.post("/api/bridges", json={"id": "ws", "type": "rtu_transparent", "host": "192.0.2.60",
                                      "ports": [{"port": 4196}]})
    assert client.get("/api/bridges/ws/diagnostics").status_code == 422


@needs_tc
def test_an_edit_never_writes_the_runtime_view_into_the_config(tmp_path):
    """BR-E1b-1: the edit form sends the ports back as GET /api/bridges gave
    them — guard, busy, tapped, wire (and now diag) were saved into
    config.yaml as if they were settings."""
    cfg, client = make_app(tmp_path)
    client.post("/api/bridges", json={"id": "ws", "type": "rtu_transparent", "host": "192.0.2.60",
                                      "ports": [{"port": 4196, "label": "bus A"}]})
    got = client.get("/api/bridges/ws").json()
    assert {"guard", "busy", "tapped", "wire"} <= set(got["ports"][0])
    body = {k: got[k] for k in ("name", "type", "host", "max_connections") if k in got}
    body["ports"] = [{k: v for k, v in p.items() if k != "devices"} for p in got["ports"]]
    r = client.put("/api/bridges/ws", json=body)
    assert r.status_code == 200, r.text
    saved = cfg.get_raw_bridge("ws")["ports"][0]
    assert set(saved) == {"port", "label"}, saved


@needs_tc
def test_alert_thresholds_on_a_bridge_are_validated_and_kept_by_an_edit(tmp_path):
    cfg, client = make_app(tmp_path)
    bad = client.post("/api/bridges", json={"id": "ws", "type": "rtu_transparent", "host": "192.0.2.60",
                                            "alerts": {"silent_s": -3},
                                            "ports": [{"port": 4196, "alerts": {"nope": 1}}]})
    assert bad.status_code == 422 and "alerts.silent_s" in bad.text and "ports[0].alerts.nope" in bad.text
    ok = client.post("/api/bridges", json={"id": "ws", "type": "rtu_transparent", "host": "192.0.2.60",
                                           "alerts": {"silent_s": 60},
                                           "ports": [{"port": 4196, "alerts": {"enabled": False}}]})
    assert ok.status_code == 200, ok.text
    # the form carries neither: an edit keeps both
    r = client.put("/api/bridges/ws", json={"name": "Panel", "type": "rtu_transparent",
                                            "host": "192.0.2.61", "ports": [{"port": 4196}]})
    assert r.status_code == 200, r.text
    raw = cfg.get_raw_bridge("ws")
    assert raw["alerts"] == {"silent_s": 60} and raw["ports"][0]["alerts"] == {"enabled": False}


@needs_tc
def test_alerts_config_carries_the_bridge_signal_and_thresholds(tmp_path):
    _cfg, client = make_app(tmp_path)
    a = client.get("/api/config/alerts").json()
    assert a["signals"]["bridge"] is False                       # off by default
    assert a["bridge"] == bd.DEFAULT_THRESHOLDS
    r = client.post("/api/config/alerts", json={"signals": {"bridge": True},
                                                "bridge": {"line_errors_per_min": 3, "silent_s": 90,
                                                           "reconnects_per_10min": 4}})
    assert r.status_code == 200, r.text
    a = client.get("/api/config/alerts").json()
    assert a["signals"]["bridge"] is True and a["bridge"]["silent_s"] == 90
    mgr = client.app.state.ctx.alert_mgr
    assert mgr.sig_bridge is True and mgr.bridge_thresholds["line_errors_per_min"] == 3
    bad = client.post("/api/config/alerts", json={"bridge": {"silent_s": 0}})
    assert bad.status_code == 422 and "bridge.silent_s" in bad.text
