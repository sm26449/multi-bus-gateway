# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The bus guard (multibus/bridge_guard.py, FINDINGS BR-E1-11): a device
reached through our serial bridge never reads — or writes — a bus other than
the one it was bound to, whatever the bridge's port map did (lost volume,
restore, narrowed range). The bridge's /adapters answer is faked; no socket
to a bus is ever opened."""
import pytest

from multibus import bridge_guard
from multibus.config import Config, ModbusConfig
from multibus.modbus_client import ModbusConnection, _TRANSPORTS
from tests.test_devices import write_config
from tests.test_devices_api import make_app, needs_tc

TPL = "fronius_smart_meter_65a"
YAML = f"""
bridges:
  - id: pi
    type: mbg_serial_bridge
    host: 192.0.2.50
    token: t0k
    ports:
      - port: 7001
        adapter: B0045K08
devices:
  - id: m1
    template: {TPL}
    enabled: false
    connection: {{bridge: pi, bridge_port: 7001, unit_id: 1}}
  - id: m2
    template: {TPL}
    enabled: false
    connection: {{bridge: pi, bridge_port: 7002, unit_id: 2}}
  - id: l1
    template: {TPL}
    enabled: false
    connection: {{protocol: rtu-tcp, host: mbg-serial-bridge, port: 7003, unit_id: 1, adapter: "spi0.0:0"}}
  - id: l2
    template: {TPL}
    enabled: false
    connection: {{protocol: rtu-tcp, host: mbg-serial-bridge, port: 7004, unit_id: 1}}
  - id: far
    template: {TPL}
    enabled: false
    connection: {{protocol: tcp, host: 192.0.2.99, port: 502, unit_id: 1}}
"""


class _Bridge:
    """What each bridge's /adapters says now: {url: {tcp_port: stable_id}}."""

    def __init__(self):
        self.maps = {"http://192.0.2.50:7000": {7001: "B0045K08", 7002: "1a86:7523@1-1"},
                     "http://mbg-serial-bridge:7000": {7003: "spi0.0:0", 7004: "spi0.0:1"}}
        self.extra = {}            # url → [adapter dicts listed but not served]
        self.down = set()
        self.asked = []

    def __call__(self, url, token):
        self.asked.append((url, token))
        if url in self.down:
            raise OSError("connection refused")
        served = [{"stable_id": sid, "tcp_port": p, "status": "ok"}
                  for p, sid in self.maps.get(url, {}).items()]
        return served + self.extra.get(url, [])


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.delenv("SERIAL_BRIDGE_URL", raising=False)
    monkeypatch.delenv("SERIAL_BRIDGE_TOKEN", raising=False)
    bridge_guard.reset()
    fake = _Bridge()
    monkeypatch.setattr(bridge_guard, "_fetch", fake)
    monkeypatch.setattr(bridge_guard, "CACHE_S", 0)
    yield fake
    bridge_guard.reset()
    bridge_guard.set_recorder(None)


@pytest.fixture
def cfg(tmp_path, bridge):
    return write_config(tmp_path, extra_yaml=YAML)


def dev(cfg, did):
    return next(d for d in cfg.devices if d.id == did).connection


def _raw_dev(cfg, did):
    return next(d for d in cfg._raw_devices if d["id"] == did)


# ── old configs and what a device carries ─────────────────────────────────────

def test_the_bus_adapter_reaches_the_connection(cfg):
    assert dev(cfg, "m1").adapter == "B0045K08"            # from the bridge's port entry
    assert dev(cfg, "l1").adapter == "spi0.0:0"            # from its own connection
    assert dev(cfg, "m2").adapter == "" and dev(cfg, "l2").adapter == ""   # old: nothing recorded
    assert ModbusConfig().adapter == ""


def test_a_host_that_is_not_our_bridge_is_never_asked(cfg, bridge):
    assert bridge_guard.allow(dev(cfg, "far")) is True
    assert bridge.asked == [] and bridge_guard.problems() == []


def test_the_right_adapter_connects(cfg, bridge):
    assert bridge_guard.allow(dev(cfg, "m1")) is True
    assert bridge_guard.allow(dev(cfg, "l1")) is True
    assert bridge.asked[0] == ("http://192.0.2.50:7000", "t0k")      # with the bridge's token


# ── mismatch, disappearance, unverifiable ──────────────────────────────────────

def test_mismatch_is_refused_and_says_where_the_adapter_went(cfg, bridge):
    bridge.maps["http://192.0.2.50:7000"] = {7001: "OTHER-ADAPTER", 7005: "B0045K08"}
    assert bridge_guard.allow(dev(cfg, "m1")) is False
    st = bridge_guard.state("192.0.2.50", 7001)
    assert st["status"] == "moved" and st["now_at"] == 7005 and st["found"] == "OTHER-ADAPTER"
    assert "B0045K08 is now on :7005" in st["detail"]
    assert bridge_guard.moves("192.0.2.50") == {7001: 7005}


def test_disappearance_is_refused(cfg, bridge):
    bridge.maps["http://mbg-serial-bridge:7000"] = {7004: "spi0.0:1"}
    assert bridge_guard.allow(dev(cfg, "l1")) is False
    st = bridge_guard.state("mbg-serial-bridge", 7003)
    assert st["status"] == "missing" and "not on the bridge" in st["detail"]
    # listed but not served (e.g. no-access after a kernel update): still missing, with why
    bridge.extra["http://mbg-serial-bridge:7000"] = [
        {"stable_id": "spi0.0:0", "tcp_port": None, "status": "no-access", "reason": "major 510"}]
    assert bridge_guard.allow(dev(cfg, "l1")) is False
    assert "no-access: major 510" in bridge_guard.state("mbg-serial-bridge", 7003)["detail"]


def test_an_unanswering_control_api_keeps_reading_then_a_mismatch_refuses(cfg, bridge):
    """The control API and the data ports fail independently (a token or a
    firewall on the control port): no evidence is not a refusal. The bus keeps
    reading on its recorded binding, said once as a warning; once the API
    answers again, a mismatch refuses as usual."""
    _TRANSPORTS.clear()
    bridge.down.add("http://192.0.2.50:7000")
    assert bridge_guard.allow(dev(cfg, "m1")) is True
    st = bridge_guard.state("192.0.2.50", 7001)
    assert st["status"] == "unverified" and "still reading as B0045K08" in st["detail"]
    assert [(p["port"], p["status"], p["allowed"]) for p in bridge_guard.problems()] == [
        (7001, "unverified", True)]                                   # listed, as a warning
    assert bridge_guard.allow(dev(cfg, "m2")) is True                # nothing recorded: as before

    class _Client:
        def connect(self):
            return True

        def close(self):
            pass

        def is_socket_open(self):
            return False
    c = ModbusConnection(dev(cfg, "m1"), trace_label="m1")
    c._new_client = _Client
    assert c.connect() is True and c.connect() is True              # reads go on
    kinds = [e.get("kind") for e in list(c.events)]
    assert kinds.count("bus_unverified") == 1                          # once per episode
    # the API is back and the adapter is elsewhere: refused, no socket
    bridge.down.clear()
    bridge.maps["http://192.0.2.50:7000"] = {7001: "OTHER", 7005: "B0045K08"}

    def boom():
        raise AssertionError("a socket to the wrong bus was opened")
    c._new_client = boom
    assert c.connect() is False and c.client is None
    assert bridge_guard.state("192.0.2.50", 7001)["status"] == "moved"
    assert [e.get("kind") for e in list(c.events)][-1] == "bus_moved"
    _TRANSPORTS.clear()


def test_the_connection_never_opens_a_socket_to_the_wrong_bus(cfg, bridge):
    """connect() and the read path's reconnect both stop before a client exists."""
    _TRANSPORTS.clear()
    bridge.maps["http://192.0.2.50:7000"] = {7001: "OTHER-ADAPTER"}
    c = ModbusConnection(dev(cfg, "m1"), trace_label="m1")

    def boom():
        raise AssertionError("a socket to the wrong bus was opened")
    c._new_client = boom
    assert c.connect() is False and c.client is None
    with c.lock:
        assert c._ensure_connected() is False
    assert any(e.get("kind") == "bus_missing" for e in list(c.events)), list(c.events)
    _TRANSPORTS.clear()


# ── backfill ───────────────────────────────────────────────────────────────────

def test_backfill_records_the_adapter_where_the_bus_is_declared(cfg, bridge, tmp_path):
    assert bridge_guard.allow(dev(cfg, "m2")) is True          # bridge record, port 7002
    assert bridge_guard.allow(dev(cfg, "l2")) is True          # host:port, 7004
    rec = next(b for b in cfg._raw_bridges if b["id"] == "pi")
    assert {"port": 7002, "adapter": "1a86:7523@1-1"} in rec["ports"]
    assert _raw_dev(cfg, "l2")["connection"]["adapter"] == "spi0.0:1"
    again = Config(str(tmp_path / "config.yaml"))              # saved: survives a restart
    assert dev(again, "m2").adapter == "1a86:7523@1-1" and dev(again, "l2").adapter == "spi0.0:1"


def test_after_a_backfill_a_change_is_caught(cfg, bridge):
    assert bridge_guard.allow(dev(cfg, "m2")) is True
    bridge.maps["http://192.0.2.50:7000"][7002] = "SOMETHING-ELSE"
    # the running client still has the config it was built with (adapter ""):
    # what was learned at the backfill guards it
    assert bridge_guard.allow(dev(cfg, "m2")) is False


def test_backfill_never_overwrites(cfg, bridge):
    assert cfg.record_bus_adapter("mbg-serial-bridge", 7003, "WRONG") is False
    assert _raw_dev(cfg, "l1")["connection"]["adapter"] == "spi0.0:0"
    assert cfg.record_bus_adapter("192.0.2.50", 7001, "WRONG") is False


def test_an_unserved_port_without_a_binding_reads_as_before(cfg, bridge):
    bridge.maps["http://mbg-serial-bridge:7000"] = {}
    assert bridge_guard.allow(dev(cfg, "l2")) is True
    assert bridge_guard.state("mbg-serial-bridge", 7004)["status"] == "unbound"
    assert "adapter" not in _raw_dev(cfg, "l2")["connection"]


# ── following the adapter ──────────────────────────────────────────────────────

def test_follow_swaps_two_swapped_buses(cfg, bridge):
    """The bridge swapped two adapters' ports: both buses follow at once — a
    one-by-one move would put both on the same port."""
    assert bridge_guard.allow(dev(cfg, "m2")) is True                       # backfill 7002
    bridge.maps["http://192.0.2.50:7000"] = {7001: "1a86:7523@1-1", 7002: "B0045K08"}
    assert bridge_guard.allow(dev(cfg, "m1")) is False
    m2 = ModbusConfig(host="192.0.2.50", port=7002, adapter="1a86:7523@1-1")
    assert bridge_guard.allow(m2) is False
    assert bridge_guard.moves("192.0.2.50") == {7001: 7002, 7002: 7001}
    moved = cfg.follow_bus_adapters("192.0.2.50", bridge_guard.moves("192.0.2.50"))
    bridge_guard.forget("192.0.2.50")
    assert sorted(moved) == ["m1", "m2"]
    assert (dev(cfg, "m1").port, dev(cfg, "m1").adapter) == (7002, "B0045K08")
    assert (dev(cfg, "m2").port, dev(cfg, "m2").adapter) == (7001, "1a86:7523@1-1")
    assert bridge_guard.allow(dev(cfg, "m1")) and bridge_guard.allow(dev(cfg, "m2"))


def test_follow_a_host_port_device(cfg, bridge):
    bridge.maps["http://mbg-serial-bridge:7000"] = {7006: "spi0.0:0", 7004: "spi0.0:1"}
    assert bridge_guard.allow(dev(cfg, "l1")) is False
    assert cfg.follow_bus_adapters("mbg-serial-bridge", bridge_guard.moves("mbg-serial-bridge")) == ["l1"]
    bridge_guard.forget("mbg-serial-bridge")
    assert (dev(cfg, "l1").port, dev(cfg, "l1").adapter) == (7006, "spi0.0:0")
    assert dev(cfg, "l2").port == 7004                                       # not touched
    assert bridge_guard.allow(dev(cfg, "l1")) is True


@needs_tc
def test_the_api_lists_problems_and_follows(tmp_path, bridge):
    cfg, client = make_app(tmp_path, extra_yaml=YAML)
    bridge.maps["http://192.0.2.50:7000"] = {7001: "OTHER", 7009: "B0045K08"}
    assert bridge_guard.allow(dev(cfg, "m1")) is False
    probs = client.get("/api/bridge-guard").json()["problems"]
    assert [(p["host"], p["port"], p["status"], p["now_at"]) for p in probs] == [
        ("192.0.2.50", 7001, "moved", 7009)]
    view = next(b for b in client.get("/api/bridges").json()["bridges"] if b["id"] == "pi")
    assert next(p for p in view["ports"] if p["port"] == 7001)["guard"]["status"] == "moved"
    r = client.post("/api/bridge-guard/follow", json={"host": "192.0.2.50"})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] == {"7001": 7009} and r.json()["devices"] == ["m1"]
    assert (dev(cfg, "m1").port, dev(cfg, "m1").adapter) == (7009, "B0045K08")
    assert client.get("/api/bridge-guard").json()["problems"] == []
    assert client.post("/api/bridge-guard/follow", json={"host": "192.0.2.50"}).status_code == 409


@needs_tc
def test_a_bridge_edit_keeps_the_recorded_adapters(tmp_path, bridge):
    cfg, client = make_app(tmp_path, extra_yaml=YAML)
    r = client.put("/api/bridges/pi", json={"type": "mbg_serial_bridge", "host": "192.0.2.51",
                                            "name": "Pi", "ports": [{"port": 7001}]})
    assert r.status_code == 200, r.text
    rec = next(b for b in cfg._raw_bridges if b["id"] == "pi")
    assert rec["ports"] == [{"port": 7001, "adapter": "B0045K08"}]


def test_a_tap_through_the_bridge_is_guarded_too(cfg, bridge):
    from multibus.rtu_tap import _TcpSource
    bridge.maps["http://mbg-serial-bridge:7000"] = {7003: "OTHER"}
    src = _TcpSource("mbg-serial-bridge", 7003, "spi0.0:0")
    with pytest.raises(ConnectionError, match="spi0.0:0"):
        src.read(10)
    assert src._sock is None
