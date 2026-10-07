# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Serial-bridge supervisor: pure helpers (serial params, ser2net config,
token check, portmap migration, routing) and the control API over a real
socket with a fake adapter inventory — no /dev, no ser2net."""
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "serial-bridge" / "supervisor.py"


def _load(monkeypatch, tmp_path, **env):
    """Import supervisor.py fresh with the given env (module-level config)."""
    monkeypatch.setenv("BRIDGE_STATE", str(tmp_path / "portmap.json"))
    monkeypatch.setenv("SER2NET_CFG", str(tmp_path / "ser2net.yaml"))
    for k in ("BRIDGE_TOKEN", "BRIDGE_SERIAL_PARAMS", "BRIDGE_VERSION",
              "BRIDGE_HOSTNAME", "BRIDGE_PORT_LOW", "BRIDGE_PORT_HIGH"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("sb_supervisor", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sv(monkeypatch, tmp_path):
    return _load(monkeypatch, tmp_path)


# ---- serial params ---------------------------------------------------------

def test_default_params_parse(sv):
    assert sv.DEFAULT_SERIAL == {"baud": 9600, "parity": "N", "databits": 8, "stopbits": 1}
    assert sv.serial_text(sv.DEFAULT_SERIAL) == "9600 8N1"
    assert sv.ser2net_params(sv.DEFAULT_SERIAL) == "9600n81"


def test_env_default_params(monkeypatch, tmp_path):
    m = _load(monkeypatch, tmp_path, BRIDGE_SERIAL_PARAMS="19200e81")
    assert m.serial_text(m.DEFAULT_SERIAL) == "19200 8E1"


def test_invalid_env_default_falls_back(monkeypatch, tmp_path):
    m = _load(monkeypatch, tmp_path, BRIDGE_SERIAL_PARAMS="9601x91")
    assert m.ser2net_params(m.DEFAULT_SERIAL) == "9600n81"


def test_validate_merges_partial_body(sv):
    out = sv.validate_serial({"baud": 19200, "parity": "e"}, sv.DEFAULT_SERIAL)
    assert out == {"baud": 19200, "parity": "E", "databits": 8, "stopbits": 1}
    assert sv.validate_serial({"baud": "4800", "stopbits": "2"}, sv.DEFAULT_SERIAL)["baud"] == 4800


@pytest.mark.parametrize("body", [
    {"baud": 9601}, {"baud": 230400}, {"baud": True}, {"baud": "fast"},
    {"parity": "X"}, {"parity": 1}, {"databits": 6}, {"stopbits": 3},
    {"flow": "rtscts"}, [9600],
])
def test_validate_rejects(sv, body):
    with pytest.raises(ValueError):
        sv.validate_serial(body, sv.DEFAULT_SERIAL)


@pytest.mark.parametrize("baud", [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200])
def test_all_standard_bauds(sv, baud):
    assert sv.validate_serial({"baud": baud}, sv.DEFAULT_SERIAL)["baud"] == baud


# ---- ser2net config --------------------------------------------------------

def test_render_per_adapter_params(sv):
    cfg = sv.render_ser2net_config([
        {"name": "ttyUSB0", "dev": "/dev/ttyUSB0", "tcp_port": 7001},
        {"name": "ttyUSB1", "dev": "/dev/ttyUSB1", "tcp_port": 7002,
         "serial": {"baud": 19200, "parity": "E", "databits": 8, "stopbits": 1}},
    ])
    assert cfg.startswith("%YAML 1.1\n---\n")
    assert "connection: &ttyUSB0\n  accepter: tcp,7001\n  connector: serialdev,/dev/ttyUSB0,9600n81,local" in cfg
    assert "  accepter: tcp,7002\n  connector: serialdev,/dev/ttyUSB1,19200e81,local" in cfg
    assert cfg.count("max-connections: 1") == 2


def test_render_empty(sv):
    assert sv.render_ser2net_config([]) == "%YAML 1.1\n---\n"


# ---- token -----------------------------------------------------------------

def test_check_token(sv):
    assert sv.check_token(None, "")                       # auth off
    assert sv.check_token("anything", "")
    assert sv.check_token("Bearer s3cret", "s3cret")
    assert sv.check_token("bearer  s3cret ", "s3cret")
    assert not sv.check_token(None, "s3cret")
    assert not sv.check_token("", "s3cret")
    assert not sv.check_token("Bearer wrong", "s3cret")
    assert not sv.check_token("Basic s3cret", "s3cret")
    assert not sv.check_token("s3cret", "s3cret")
    assert not sv.check_token("Bearer ", "s3cret")


# ---- portmap state / migration ---------------------------------------------

def test_parse_legacy_flat_portmap(sv):
    ports, serial = sv.parse_state({"B0045K08": 7001, "1a86:7523@1-1": 7002})
    assert ports == {"B0045K08": 7001, "1a86:7523@1-1": 7002}
    assert serial == {}
    assert sv.serial_for("B0045K08", serial) == sv.DEFAULT_SERIAL


def test_parse_drops_invalid_entries(sv):
    ports, serial = sv.parse_state({
        "a": 7001, "dup": 7001, "low": 80, "str": "7003", "bool": True,
        sv.SERIAL_STATE_KEY: {
            "a": {"baud": 19200},                         # partial → defaults fill
            "bad": {"baud": 12345},                       # dropped
            "junk": "9600n81",                            # dropped
        },
    })
    assert ports == {"a": 7001}
    assert serial == {"a": {"baud": 19200, "parity": "N", "databits": 8, "stopbits": 1}}


def test_parse_rejects_non_object(sv):
    with pytest.raises(ValueError):
        sv.parse_state([1, 2])


def test_state_roundtrip_and_downgrade_shape(sv):
    ports = {"1a86:7523@1-1": 7001}
    serial = {"1a86:7523@1-1": {"baud": 4800, "parity": "O", "databits": 7, "stopbits": 2}}
    dumped = sv.dump_state(ports, serial)
    assert dumped["1a86:7523@1-1"] == 7001               # ports stay top-level
    assert sv.parse_state(json.loads(json.dumps(dumped))) == (ports, serial)
    assert sv.SERIAL_STATE_KEY not in sv.dump_state(ports, {})


def test_load_state_from_disk_legacy_and_corrupt(sv, tmp_path):
    p = tmp_path / "portmap.json"
    assert sv._load_state() == ({}, {})
    p.write_text('{"X1": 7005}')
    assert sv._load_state() == ({"X1": 7005}, {})
    p.write_text("")                                     # the 0-byte map of DP-30
    assert sv._load_state() == ({}, {})


def test_assign_port_stable_and_range(monkeypatch, tmp_path):
    m = _load(monkeypatch, tmp_path, BRIDGE_PORT_LOW="7001", BRIDGE_PORT_HIGH="7002")
    pmap = {}
    assert m.assign_port("a", pmap) == 7001
    assert m.assign_port("b", pmap) == 7002
    assert m.assign_port("a", pmap) == 7001
    with pytest.raises(RuntimeError):
        m.assign_port("c", pmap)
    # a port outside a narrowed range is dropped on load
    assert m.parse_state({"a": 7001, "z": 7050})[0] == {"a": 7001}


# ---- routing ---------------------------------------------------------------

def test_route(sv):
    assert sv.route("GET", "/health") == ("health", None)
    assert sv.route("GET", "/adapters/") == ("adapters", None)
    assert sv.route("GET", "/adapters?x=1") == ("adapters", None)
    assert sv.route("POST", "/adapters/1a86%3A7523%401-1/serial") == ("serial", "1a86:7523@1-1")
    assert sv.route("POST", "/adapters/1a86:7523@1-1/serial") == ("serial", "1a86:7523@1-1")
    assert sv.route("POST", "/adapters/a%2Fb/serial") == ("serial", "a/b")
    assert sv.route("GET", "/adapters/x/serial") == ("method_not_allowed", "x")
    assert sv.route("POST", "/adapters") == ("method_not_allowed", None)
    assert sv.route("GET", "/nope") == ("not_found", None)
    assert sv.route("POST", "/adapters//serial") == ("not_found", None)


# ---- control API over a socket ---------------------------------------------

_FAKE = [
    {"dev": "/dev/ttyUSB0", "name": "ttyUSB0", "vendor_id": "1a86", "product_id": "7523",
     "serial": "", "manufacturer": "QinHeng", "model": "USB Serial", "port_path": "1-1",
     "stable_id": "1a86:7523@1-1"},
    {"dev": "/dev/ttyUSB1", "name": "ttyUSB1", "vendor_id": "0403", "product_id": "6001",
     "serial": "B0045K08", "manufacturer": "FTDI", "model": "FT232R", "port_path": "1-2",
     "stable_id": "B0045K08"},
]


class _FakeProc:
    def __init__(self):
        self.hups = 0

    def poll(self):
        return None

    def send_signal(self, _sig):
        self.hups += 1


@pytest.fixture
def api(monkeypatch, tmp_path):
    def start(**env):
        m = _load(monkeypatch, tmp_path, BRIDGE_HOSTNAME="pi-bus", BRIDGE_VERSION="9.9.9", **env)
        proc = _FakeProc()
        monkeypatch.setattr(m, "enumerate_adapters", lambda: [dict(a) for a in _FAKE])
        monkeypatch.setattr(m, "_ser2net", proc)
        monkeypatch.setattr(m, "_clear_dead_pid_locks", lambda: None)
        m.reconcile()
        proc.hups = 0                                     # count only what the test does
        srv = ThreadingHTTPServer(("127.0.0.1", 0), m._Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return m, proc, f"http://127.0.0.1:{srv.server_address[1]}"
    servers = []
    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def _call(url, method="GET", body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


def test_no_token_unchanged_behaviour(api):
    _m, _p, base = api()
    code, data = _call(base + "/adapters")
    assert code == 200
    assert data["version"] == "9.9.9" and data["hostname"] == "pi-bus" and data["auth"] is False
    a = data["adapters"][0]
    # every field MBG's commissioning route + wizard read
    for k in ("stable_id", "dev", "tcp_port", "available", "excluded", "serial",
              "model", "manufacturer", "vendor_id"):
        assert k in a
    assert a["tcp_port"] == 7001 and a["serial"] == ""
    assert a["serial_params"] == {"baud": 9600, "parity": "N", "databits": 8, "stopbits": 1}
    assert a["serial_text"] == "9600 8N1" and a["serial_custom"] is False
    code, h = _call(base + "/health")
    assert code == 200 and h == {"status": "ok", "adapters": 2, "version": "9.9.9",
                                 "hostname": "pi-bus"}


def test_token_required(api):
    _m, _p, base = api(BRIDGE_TOKEN="s3cret")
    assert _call(base + "/adapters")[0] == 401
    assert _call(base + "/adapters", token="wrong")[0] == 401
    assert _call(base + "/nope")[0] == 401                # no anonymous probing
    assert _call(base + "/adapters/B0045K08/serial", "POST", {"baud": 19200})[0] == 401
    code, data = _call(base + "/adapters", token="s3cret")
    assert code == 200 and data["auth"] is True and len(data["adapters"]) == 2
    # /health stays open but reveals no device details nor the hostname
    code, h = _call(base + "/health")
    assert code == 200 and h == {"status": "ok", "adapters": 2, "version": "9.9.9"}
    assert _call(base + "/health", token="s3cret")[1]["hostname"] == "pi-bus"


def test_post_serial_persists_and_reloads(api, tmp_path):
    m, proc, base = api(BRIDGE_TOKEN="t")
    key = "1a86%3A7523%401-1"
    code, a = _call(f"{base}/adapters/{key}/serial", "POST",
                    {"baud": 19200, "parity": "E", "databits": 8, "stopbits": 1}, token="t")
    assert code == 200
    assert a["stable_id"] == "1a86:7523@1-1" and a["serial_text"] == "19200 8E1"
    assert a["serial_custom"] is True and a["tcp_port"] == 7001
    assert proc.hups == 1                                 # one SIGHUP, ser2net restarts that port
    cfg = (tmp_path / "ser2net.yaml").read_text()
    assert "serialdev,/dev/ttyUSB0,19200e81,local" in cfg
    assert "serialdev,/dev/ttyUSB1,9600n81,local" in cfg
    state = json.loads((tmp_path / "portmap.json").read_text())
    assert state["1a86:7523@1-1"] == 7001 and state["B0045K08"] == 7002
    assert state[m.SERIAL_STATE_KEY]["1a86:7523@1-1"]["baud"] == 19200
    # a later periodic reconcile sees no change → no extra reload
    m.reconcile()
    assert proc.hups == 1
    # partial update keeps the rest
    code, a = _call(f"{base}/adapters/{key}/serial", "POST", {"stopbits": 2}, token="t")
    assert code == 200 and a["serial_text"] == "19200 8E2"
    listed = _call(base + "/adapters", token="t")[1]["adapters"][0]
    assert listed["serial_text"] == "19200 8E2"


def test_post_serial_errors(api):
    _m, proc, base = api()
    assert _call(base + "/adapters/B0045K08/serial", "POST", {"baud": 9601})[0] == 400
    assert _call(base + "/adapters/B0045K08/serial", "POST", {"parity": "Z"})[0] == 400
    assert _call(base + "/adapters/NOPE/serial", "POST", {"baud": 9600})[0] == 404
    assert _call(base + "/adapters/B0045K08/serial", "GET")[0] == 405
    req = urllib.request.Request(base + "/adapters/B0045K08/serial", data=b"{not json",
                                 method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    e.value.close()
    assert e.value.code == 400
    assert proc.hups == 0                                 # nothing applied


def test_health_down_when_ser2net_dead(api, monkeypatch):
    m, _p, base = api(BRIDGE_TOKEN="t")

    class _Dead:
        def poll(self):
            return 1
    monkeypatch.setattr(m, "_ser2net", _Dead())
    code, h = _call(base + "/health")
    assert code == 500 and h["status"] == "down" and "hostname" not in h
