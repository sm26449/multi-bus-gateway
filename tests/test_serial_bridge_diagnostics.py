# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Serial-bridge diagnostics (ADR-0017): ser2net's admin text parsed, the
per-connection byte counters folded into totals, the kernel's line counters
read on ser2net's own file (faked), null — never 0 — for what a driver does
not keep, the event ring, and the API behind the token. No /dev, no ser2net."""
import errno
import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "serial-bridge" / "supervisor.py"

# what ser2net 4.3.11's admin `showport` printed (captured from the image)
SHOWPORT_CONNECTED = (
    "-> showport\r\nport: ttyUSB0\r\n  accepter: tcp,7001\r\n  enable state: on\r\n"
    "  timeout: 0\r\n  connected to: ipv4,172.20.0.5,43366\r\n    bytes read from TCP: 8\r\n"
    "    bytes written to TCP: 10\r\n  device: serialdev,/dev/ttyUSB0,9600n81,local\r\n"
    "  device config: ?\r\n  device controls: ?\r\n  tcp to device state: waiting output\r\n"
    "  device to tcp state: waiting output\r\n  bytes read from device: 10\r\n"
    "  bytes written to device: 8\r\n"
    "port: ttyUSB1\r\n  accepter: tcp,7002\r\n  enable state: on\r\n  timeout: 0\r\n"
    "  device: serialdev,/dev/ttyUSB1,9600n81,local\r\n  device config: 9600N81,CLOCAL\r\n"
    "  tcp to device state: waiting input\r\n  device to tcp state: waiting input\r\n"
    "  bytes read from device: 0\r\n  bytes written to device: 0\r\n-> \r\n")


def _load(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("BRIDGE_STATE", str(tmp_path / "portmap.json"))
    monkeypatch.setenv("SER2NET_CFG", str(tmp_path / "ser2net.yaml"))
    for k in ("BRIDGE_TOKEN", "BRIDGE_SERIAL_PARAMS", "BRIDGE_VERSION", "BRIDGE_HOSTNAME",
              "BRIDGE_PORT_LOW", "BRIDGE_PORT_HIGH", "BRIDGE_ADMIN_PORT", "BRIDGE_STATS_S"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("sb_supervisor_diag", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sv(monkeypatch, tmp_path):
    return _load(monkeypatch, tmp_path)


# ---- ser2net's admin text ---------------------------------------------------

def test_showport_parsed_per_tcp_port(sv):
    p = sv.parse_showport(SHOWPORT_CONNECTED)
    assert set(p) == {7001, 7002}
    assert p[7001] == {"name": "ttyUSB0", "remote": "172.20.0.5:43366", "net_in": 8,
                       "net_out": 10, "dev_in": 10, "dev_out": 8, "enabled": True}
    assert p[7002]["remote"] == "" and p[7002]["dev_in"] == 0


def test_showport_ipv6_and_bound_accepter_and_garbage(sv):
    text = ("port: x\n  accepter: tcp,0.0.0.0,7005\n  connected to: ipv6,::ffff:10.0.0.9,5000\n"
            "  bytes read from device: 7\n  enable state: off\n")
    p = sv.parse_showport(text)
    assert p[7005]["remote"] == "::ffff:10.0.0.9:5000" and p[7005]["dev_in"] == 7
    assert p[7005]["enabled"] is False
    assert sv.parse_showport("") == {} and sv.parse_showport("-> \r\n-> ") == {}
    assert sv.parse_showport("bytes read from device: 5\n") == {}      # no port block


def test_admin_block_only_on_localhost_and_only_with_adapters(sv):
    cfg = sv.render_ser2net_config([{"name": "ttyUSB0", "dev": "/dev/ttyUSB0", "tcp_port": 7001}])
    assert "admin:\n  accepter: tcp,127.0.0.1,6999\n" in cfg
    # always there, so 0 → 1 adapter needs no ser2net restart (review E1b #8)
    assert sv.render_ser2net_config([]) == "%YAML 1.1\n---\nadmin:\n  accepter: tcp,127.0.0.1,6999\n\n"
    off = sv.render_ser2net_config([{"name": "t", "dev": "/dev/t", "tcp_port": 7001}], admin_port=0)
    assert "admin:" not in off


def test_admin_port_never_collides(monkeypatch, tmp_path):
    m = _load(monkeypatch, tmp_path, BRIDGE_ADMIN_PORT="7005")       # inside the data range
    assert m.ADMIN_PORT == 0
    m = _load(monkeypatch, tmp_path, BRIDGE_ADMIN_PORT="7000")       # the control port
    assert m.ADMIN_PORT == 0
    m = _load(monkeypatch, tmp_path, BRIDGE_ADMIN_PORT="0")
    assert m.ADMIN_PORT == 0
    m = _load(monkeypatch, tmp_path, BRIDGE_ADMIN_PORT="6100")
    assert m.ADMIN_PORT == 6100


# ---- folding samples ----------------------------------------------------------

def _s(remote="", dev_in=0, dev_out=0, net_in=0, net_out=0):
    return {"name": "ttyUSB0", "remote": remote, "dev_in": dev_in, "dev_out": dev_out,
            "net_in": net_in, "net_out": net_out, "enabled": True}


def test_bytes_summed_across_connections_and_counters_reset(sv):
    st = sv.new_port_state(1000.0)
    ev = sv.apply_sample(st, _s("10.0.0.2:1", 10, 8, 8, 10), 1005.0)
    assert [e[0] for e in ev] == ["client_connected"] and st["connects"] == 1
    assert st["bytes"]["from_device"] == 10 and st["last_rx_ts"] == 1005.0
    sv.apply_sample(st, _s("10.0.0.2:1", 30, 16, 16, 30), 1010.0)
    assert st["bytes"] == {"from_device": 30, "to_device": 16, "from_net": 16, "to_net": 30}
    # the gateway reconnected: another source port, ser2net counts from 0 again
    ev = sv.apply_sample(st, _s("10.0.0.2:2", 5, 4, 4, 5), 1015.0)
    assert [e[0] for e in ev] == ["client_left", "client_connected"]
    assert st["bytes"]["from_device"] == 35 and st["connects"] == 2 and st["disconnects"] == 1
    # the same address came back in between two samples: a counter went down
    sv.apply_sample(st, _s("10.0.0.2:2", 2, 1, 1, 2), 1020.0)
    assert st["bytes"]["from_device"] == 37
    # nobody connected
    ev = sv.apply_sample(st, None, 1025.0)
    assert [e[0] for e in ev] == ["client_left"] and st["client"] == ""


def test_silent_bus_and_last_activity_per_direction(sv, monkeypatch):
    st = sv.new_port_state(1000.0)
    sv.apply_sample(st, _s("10.0.0.2:1", 10, 8), 1005.0)
    for t in (1010.0, 1015.0, 1020.0):          # the gateway asks, nothing answers
        prev = st["_last"]
        sv.apply_sample(st, _s("10.0.0.2:1", prev[0], prev[1] + 8), t)
    assert st["last_rx_ts"] == 1005.0 and st["last_tx_ts"] == 1020.0
    monkeypatch.setitem(sv._diag, "k", st)
    v = sv.port_stats("k", now=1065.0)
    assert v["silent_s"] == 60.0 and v["client"]["address"] == "10.0.0.2:1"
    assert v["connects_10min"] == 1 and v["source"]["transport"] == "ser2net-admin"
    assert v["line"] is None and v["source"]["line"] == "unavailable"
    # never anything from the bus: silent since the bridge started sampling it
    fresh = sv.new_port_state(500.0)
    monkeypatch.setitem(sv._diag, "n", fresh)
    assert sv.port_stats("n", now=800.0)["silent_s"] == 300.0


def test_connects_in_the_last_ten_minutes(sv, monkeypatch):
    st = sv.new_port_state(0.0)
    for i in range(8):                          # a reconnect storm: every sample a new client
        sv.apply_sample(st, _s(f"10.0.0.2:{i}", 1, 1), 100.0 + 5 * i)
    monkeypatch.setitem(sv._diag, "k", st)
    assert sv.port_stats("k", now=140.0)["connects_10min"] == 8
    assert sv.port_stats("k", now=100.0 + 5 * 7 + 601)["connects_10min"] == 0
    assert st["connects"] == 8


# ---- the kernel's line counters ---------------------------------------------

RAW = {"cts": 0, "dsr": 0, "rng": 0, "dcd": 0, "rx": 900, "tx": 800, "frame": 3,
       "overrun": 1, "parity": 2, "brk": 0, "buf_overrun": 0}


@pytest.mark.parametrize("driver,kind,inpck,nulls,verified", [
    ("sc16is7xx", "board", False, set(), True),            # serial_core keeps them all
    ("uart-pl011", "board", False, set(), True),
    ("ftdi_sio", "usb", False, set(), True),
    ("cp210x", "usb", False, {"frame", "parity", "overrun", "brk", "buf_overrun", "rx", "tx"}, True),
    ("cp210x", "usb", True, {"rx", "tx"}, True),
    ("cdc_acm", "usb", False, {"rx", "tx"}, True),
    ("pl2303", "usb", False, set(), False),                # not verified: reported as given
])
def test_what_each_driver_keeps(sv, driver, kind, inpck, nulls, verified):
    line, note, ver = sv.shape_line(RAW, driver, kind, inpck)
    assert {k for k, v in line.items() if v is None} == nulls
    assert ver is verified
    assert all(line[k] == RAW[k] for k in line if line[k] is not None)
    if driver == "cp210x" and not inpck:
        assert "parity checking" in note


def _proc_tree(tmp_path, pid, fds):
    d = tmp_path / "proc" / str(pid) / "fd"
    d.mkdir(parents=True)
    for n, target in fds.items():
        os.symlink(target, d / str(n))
    return str(tmp_path / "proc")


def test_line_counters_on_ser2nets_own_file(sv, tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "PROC", _proc_tree(tmp_path, 42, {3: "anon_inode:[eventpoll]",
                                                              8: "/dev/ttyUSB0"}))
    got = []
    monkeypatch.setattr(sv, "_grab_fd", lambda pid, fd: got.append((pid, fd)) or os.open(os.devnull, os.O_RDONLY))
    monkeypatch.setattr(sv, "_icount", lambda fd: (dict(RAW), False))
    line, note, ver = sv.line_counters("/dev/ttyUSB0", "ftdi_sio", "usb", 42)
    assert got == [(42, 8)] and line["frame"] == 3 and line["rx"] == 900 and ver
    # not open (no client): null, with the reason — never zeros
    line, note, _ = sv.line_counters("/dev/ttyUSB1", "ftdi_sio", "usb", 42)
    assert line is None and "not open" in note
    assert sv.line_counters("/dev/ttyUSB0", "ftdi_sio", "usb", None)[0] is None


@pytest.mark.parametrize("err,frag", [
    (errno.EPERM, "SYS_PTRACE"), (errno.ENOSYS, "5.6"), (errno.EBADF, "cannot read"),
])
def test_line_counters_unavailable_says_why(sv, tmp_path, monkeypatch, err, frag):
    monkeypatch.setattr(sv, "PROC", _proc_tree(tmp_path, 7, {5: "/dev/ttyUSB0"}))

    def refuse(pid, fd):
        raise OSError(err, os.strerror(err))
    monkeypatch.setattr(sv, "_grab_fd", refuse)
    line, note, _ = sv.line_counters("/dev/ttyUSB0", "ftdi_sio", "usb", 7)
    assert line is None and frag in note


def test_a_port_closed_during_the_read_is_not_trusted(sv, tmp_path, monkeypatch):
    """ser2net closed the port between the fd lookup and the ioctl: the
    reading is dropped (and our close was the last one — review E1b #9)."""
    monkeypatch.setattr(sv, "PROC", _proc_tree(tmp_path, 7, {5: "/dev/ttyUSB0"}))
    monkeypatch.setattr(sv, "_grab_fd", lambda pid, fd: os.open(os.devnull, os.O_RDONLY))

    def icount_then_gone(fd):
        os.unlink(os.path.join(sv.PROC, "7", "fd", "5"))
        return dict(RAW), False
    monkeypatch.setattr(sv, "_icount", icount_then_gone)
    line, note, _ = sv.line_counters("/dev/ttyUSB0", "ftdi_sio", "usb", 7)
    assert line is None and "closed while" in note


def test_ch340_reports_null_not_zero(sv, tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "PROC", _proc_tree(tmp_path, 7, {5: "/dev/ttyUSB0"}))
    monkeypatch.setattr(sv, "_grab_fd", lambda pid, fd: os.open(os.devnull, os.O_RDONLY))

    def no_icount(fd):
        raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")
    monkeypatch.setattr(sv, "_icount", no_icount)
    line, note, ver = sv.line_counters("/dev/ttyUSB0", "ch341-uart", "usb", 7)
    assert line is None and "CH340" in note and ver


def test_real_icount_on_a_pty_is_unsupported_not_zero(sv):
    """The real ioctl path: a pty has no get_icount, so the answer is an
    error, never a row of zeros."""
    m, s = os.openpty()
    try:
        with pytest.raises(OSError) as e:
            sv._icount(s)
        assert e.value.errno in (errno.ENOTTY, errno.EINVAL)
    finally:
        os.close(m)
        os.close(s)


def test_line_error_events_are_rate_limited(sv):
    st = sv.new_port_state(0.0)
    line = {f: 0 for f in sv.LINE_FIELDS}
    assert sv.apply_line(st, dict(line), "", True, 10.0) == []        # the base
    ev = sv.apply_line(st, {**line, "frame": 2}, "", True, 100.0)
    assert ev and ev[0][0] == "line_errors" and ev[0][2] == {"frame": 2}
    assert sv.apply_line(st, {**line, "frame": 5}, "", True, 110.0) == []   # within a minute
    ev = sv.apply_line(st, {**line, "frame": 5, "parity": 1}, "", True, 170.0)
    assert ev[0][2] == {"frame": 3, "parity": 1}                        # what grew since
    # adapter replugged: the counter starts again from 0 — a new base, no growth
    assert sv.apply_line(st, {**line, "frame": 1}, "", True, 300.0) == []
    assert st["_line_pending"] == {}
    ev = sv.apply_line(st, {**line, "frame": 3}, "", True, 400.0)
    assert ev[0][2] == {"frame": 2}
    # unavailable now: the last values stay, with their time
    sv.apply_line(st, None, "the port is not open", False, 400.0)
    assert st["line"]["frame"] == 3 and st["line_as_of"] == 400.0


# ---- events -------------------------------------------------------------------

def test_event_ring_bounded_and_since(sv, monkeypatch):
    monkeypatch.setattr(sv, "EVENTS_MAX", 5)
    for i in range(8):
        sv.add_event("x", f"m{i}", key="a" if i % 2 else "b")
    r = sv.events_since(0)
    assert [e["message"] for e in r["events"]] == ["m3", "m4", "m5", "m6", "m7"]
    assert r["lost"] is True and r["next"] == 8 and r["oldest"] == 4 and r["boot"] == sv.BOOT
    r = sv.events_since(6)
    assert [e["seq"] for e in r["events"]] == [7, 8] and r["lost"] is False
    assert sv.events_since(8)["events"] == [] and sv.events_since(8)["lost"] is False
    assert [e["message"] for e in sv.events_since(0, key="a")["events"]] == ["m3", "m5", "m7"]


def _a(sid, dev, port=None, status="ok", label=""):
    return {"stable_id": sid, "dev": dev, "tcp_port": port, "status": status, "label": label,
            "reason": ""}


def test_inventory_changes(sv):
    prev = [_a("A", "/dev/ttyUSB0", 7001), _a("B", "/dev/ttyUSB1", 7002), _a("C", "/dev/ttyUSB2", 7003)]
    new = [_a("A", "/dev/ttyUSB3", 7001), _a("B", "/dev/ttyUSB1", 7004),
           _a("D", "/dev/ttyUSB4", 7005), _a("E", "/dev/ttySC0", None, "no-access")]
    got = {(k, a["stable_id"]) for k, _m, a in sv.inventory_changes(prev, new, {"A", "B", "C", "E"})}
    assert got == {("dev_renamed", "A"), ("port_moved", "B"), ("adapter_new", "D"),
                   ("adapter_back", "E"), ("adapter_gone", "C")}
    st = sv.inventory_changes([_a("A", "/dev/ttyUSB0", 7001)],
                              [_a("A", "/dev/ttyUSB0", None, "busy")], {"A"})
    assert [k for k, _m, _a2 in st] == ["status"]


# ---- the whole loop and the API -----------------------------------------------

_FAKE = [
    {"dev": "/dev/ttyUSB0", "name": "ttyUSB0", "vendor_id": "0403", "product_id": "6001",
     "serial": "B0045K08", "manufacturer": "FTDI", "model": "FT232R", "port_path": "1-2",
     "stable_id": "B0045K08", "kind": "usb", "driver": "ftdi_sio", "label": "FT232R (B0045K08)"},
    {"dev": "/dev/ttyUSB1", "name": "ttyUSB1", "vendor_id": "1a86", "product_id": "7523",
     "serial": "", "manufacturer": "QinHeng", "model": "USB Serial", "port_path": "1-1",
     "stable_id": "1a86:7523@1-1", "kind": "usb", "driver": "ch341-uart", "label": "USB Serial (USB 1-1)"},
]


class _Proc:
    pid = 4242

    def poll(self):
        return None

    def send_signal(self, _sig):
        pass


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    servers = []

    def start(inventory=None, **env):
        m = _load(monkeypatch, tmp_path, BRIDGE_HOSTNAME="pi-bus", **env)
        inv = [dict(a) for a in (inventory or _FAKE)]
        monkeypatch.setattr(m, "enumerate_adapters", lambda: [dict(a) for a in inv])
        monkeypatch.setattr(m, "_ser2net", _Proc())
        monkeypatch.setattr(m, "_clear_dead_pid_locks", lambda: None)
        monkeypatch.setattr(m, "PROC", _proc_tree(tmp_path, 4242, {8: "/dev/ttyUSB0", 9: "/dev/ttyUSB1"}))
        monkeypatch.setattr(m, "_grab_fd", lambda pid, fd: os.open(os.devnull, os.O_RDONLY))

        def icount(fd):
            if m._test_ch340_next:
                m._test_ch340_next = False
                raise OSError(errno.ENOTTY, "no")
            return dict(RAW), False
        m._test_ch340_next = False
        monkeypatch.setattr(m, "_icount", icount)
        m.reconcile()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), m._Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return m, inv, f"http://127.0.0.1:{srv.server_address[1]}"
    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def _get(url, token=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


def test_adapters_carry_stats_after_a_sample(bridge):
    m, _inv, base = bridge()
    code, d = _get(base + "/adapters")
    assert code == 200 and all(a["stats"] is None for a in d["adapters"])  # nothing sampled yet
    assert d["diagnostics"]["boot"] == m.BOOT and d["diagnostics"]["interval_s"] == 5
    # the FTDI answers TIOCGICOUNT; the CH340 does not
    calls = iter([False, True])

    def icount(fd):
        if next(calls):
            raise OSError(errno.ENOTTY, "no")
        return dict(RAW), False
    m._icount = icount
    m.sample_once(now=2000.0, query=lambda: SHOWPORT_CONNECTED)
    code, d = _get(base + "/adapters")
    ftdi, ch = d["adapters"]
    assert ftdi["stats"]["client"]["address"] == "172.20.0.5:43366"
    assert ftdi["stats"]["line"]["frame"] == 3 and ftdi["stats"]["line_verified"] is True
    assert ftdi["stats"]["bytes"]["from_device"] == 10
    assert ch["stats"]["line"] is None and "CH340" in ch["stats"]["line_note"]
    assert ch["stats"]["client"] is None
    kinds = [e["kind"] for e in _get(base + "/events?since=0")[1]["events"]]
    assert "client_connected" in kinds


def test_stats_and_events_endpoints(bridge):
    m, _inv, base = bridge()
    m.sample_once(now=2000.0, query=lambda: SHOWPORT_CONNECTED)
    code, d = _get(base + "/adapters/B0045K08/stats")
    assert code == 200 and d["tcp_port"] == 7001 and d["stats"]["connects"] == 1
    assert [e["kind"] for e in d["events"]] == ["client_connected"]
    assert _get(base + "/adapters/nope/stats")[0] == 404
    code, ev = _get(base + "/events?since=0")
    assert code == 200 and ev["boot"] == m.BOOT and ev["next"] >= 1
    assert _get(base + "/events?since=x")[0] == 400
    assert _get(base + f"/events?since={ev['next']}")[1]["events"] == []


def test_new_endpoints_behind_the_token_health_unchanged(bridge):
    _m, _inv, base = bridge(BRIDGE_TOKEN="s3cret")
    for path in ("/events", "/events?since=0", "/adapters/B0045K08/stats", "/adapters"):
        assert _get(base + path)[0] == 401
        assert _get(base + path, token="wrong")[0] == 401
        assert _get(base + path, token="s3cret")[0] == 200
    code, h = _get(base + "/health")
    assert code == 200 and set(h) == {"status", "adapters", "version"}   # nothing new, anonymous


def test_an_unplugged_and_replugged_adapter_leaves_events(bridge):
    m, inv, base = bridge()
    gone = inv.pop(1)
    m.reconcile()
    inv.append(gone)
    m.reconcile()
    kinds = [(e["kind"], e["key"]) for e in m.events_since(0)["events"]]
    assert ("adapter_gone", "1a86:7523@1-1") in kinds and ("adapter_back", "1a86:7523@1-1") in kinds
    # the first scan is not a change
    assert not any(k == "adapter_new" for k, _ in kinds)


def test_admin_interface_down_keeps_the_last_view(bridge):
    m, _inv, base = bridge()
    m.sample_once(now=2000.0, query=lambda: SHOWPORT_CONNECTED)

    def down():
        raise ConnectionRefusedError(111, "refused")
    m.sample_once(now=2005.0, query=down)
    st = _get(base + "/adapters/B0045K08/stats")[1]["stats"]
    assert st["client"]["address"] == "172.20.0.5:43366"        # not invented as "left"
    assert "admin interface" in st["transport_error"] and st["source"]["transport"] == "unavailable"


def test_ser2net_exit_and_respawn_are_events(bridge):
    m, _inv, _base = bridge()

    class Dead:
        pid = 1

        def poll(self):
            return 1
    m._ser2net = Dead()
    m.sample_once(now=10.0, query=lambda: "")
    m.sample_once(now=15.0, query=lambda: "")                   # said once
    assert [e["kind"] for e in m.events_since(0)["events"]].count("ser2net_exited") == 1


def test_admin_query_waits_for_the_prompt(sv):
    """ser2net's admin interface drops what a client sends before its first
    prompt (found against the real 4.3.11: sending at once got only '-> ')."""
    import socket
    import time as _t
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = {}

    def serve():
        c, _ = srv.accept()
        with c:
            _t.sleep(0.2)
            c.setblocking(False)
            try:
                got["dropped"] = c.recv(4096)        # sent too early: thrown away
            except BlockingIOError:
                got["dropped"] = b""
            c.setblocking(True)
            c.sendall(b"-> ")
            line = b""
            while not line.endswith(b"\n"):
                line += c.recv(1)
            got["cmd"] = line
            c.sendall(b"showport\r\n" + SHOWPORT_CONNECTED.encode())
            c.recv(64)                                 # exit
    th = threading.Thread(target=serve, daemon=True)
    th.start()
    out = sv.admin_query(port=srv.getsockname()[1], timeout=3)
    th.join(3)
    srv.close()
    assert got["dropped"] == b"" and got["cmd"] == b"showport\r\n"
    assert sv.parse_showport(out)[7001]["remote"] == "172.20.0.5:43366"


def test_admin_query_off_or_silent(sv):
    with pytest.raises(OSError):
        sv.admin_query(port=0)
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        with pytest.raises(OSError):
            sv.admin_query(port=srv.getsockname()[1], timeout=0.3)   # never prompts
    finally:
        srv.close()
