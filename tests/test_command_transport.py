"""Contract tests for the CommandTransport seam (ADR-0036, §2 L0 step 1).

The adapters must reproduce today's Modbus/MQTT branches so steps 2-5 can route
api/command_service.py through them byte-identically. No caller is wired yet;
these pin each adapter method against fixtures shaped like the current inputs.
"""
import time

import pytest

from multibus.control import command_transport as ct


# ── fakes shaped like the real collaborators ────────────────────────────────
class FakeReg:
    def __init__(self, name, address=40001, register_type="holding", write_topic=None):
        self.name = name
        self.address = address
        self.register_type = register_type
        self.write_topic = write_topic


class FakeCmd:
    def __init__(self, *, name="power", settle_s=0, guard=None, writes=None, verify=None,
                 skip_if_set=False, refresh_s=0, params=None, readback_group=None):
        self.name = name
        self.settle_s = settle_s
        self.guard = guard or []
        self.writes = writes or []
        self.verify = verify or []
        self.skip_if_set = skip_if_set
        self.refresh_s = refresh_s
        self.params = params or {}
        self.readback_group = readback_group


class FakeDev:
    def __init__(self, id="dev1"):
        self.id = id


class FakeConn:
    def __init__(self, timeout=3, retry_attempts=3):
        self.config = type("Cfg", (), {"timeout": timeout, "retry_attempts": retry_attempts})()
        self.events = []

    def read_registers(self, *a, **k):
        return {}

    def record_event(self, level, kind, summary):
        self.events.append((level, kind, summary))


class FakeModbusDriver:
    """Satisfies RegisterReadable (read_register / read_registers_batch / poll_now)."""
    def __init__(self, byte_order="big", max_read=0):
        self.connection = FakeConn()
        self.byte_order = byte_order
        self.max_read = max_read
        self.polled = []

    def read_register(self, *a, **k):
        return None

    def read_registers_batch(self, registers):
        return {}

    def poll_now(self, *a, **k):
        self.polled.append((a, k))


class FakeMqtt:
    def __init__(self):
        self.events = []

    def publish(self, *a, **k):
        pass

    def wait_for(self, *a, **k):
        return True

    def record_event(self, level, kind, summary):
        self.events.append((level, kind, summary))


class FakeRegistry:
    def __init__(self, store=None):
        self._store = store or {}

    def store_for(self, dev_id):
        return self._store


def make_deps(*, registry=None, forbidden=None, pushes=None, arms=None,
              lane_key=None, lock_for=None):
    pushes = pushes if pushes is not None else []
    arms = arms if arms is not None else []
    # a lock table shared across calls, keyed on the first arg's identity —
    # mimics control/lanes.py `_cmd_locks` (ADR-0036 point 5: one shared table)
    locks: dict = {}
    lane_key = lane_key or (lambda dev_cfg, client: f"lane:{getattr(dev_cfg, 'id', '?')}")
    lock_for = lock_for or (lambda first, dev_cfg=None: locks.setdefault(id(first), object()))
    return ct.CommandTransports(
        registry=registry or FakeRegistry(),
        forbidden_topics=forbidden or (lambda dev: ["gw/#"]),
        push_readback_to_store=lambda *a: pushes.append(a),
        arm_revert_reread=lambda *a: arms.append(a),
        revert_reread_at_s=(1.0, 2.0),
        lane_key=lane_key, lock_for=lock_for,
    ), pushes, arms


def modbus_client():
    return FakeModbusDriver()


def mqtt_client():
    return FakeMqtt()


def multi_source_client(parts):
    c = type("MultiSource", (), {})()
    c.parts = parts
    return c


# ── resolution (replaces _modbus_driver_of / _mqtt_driver_of) ────────────────
def test_transport_of_resolves_modbus_client():
    deps, _, _ = make_deps()
    t = deps.transport_of(modbus_client())
    assert isinstance(t, ct.ModbusCommandTransport) and t.kind == "modbus"


def test_transport_of_resolves_mqtt_client():
    deps, _, _ = make_deps()
    t = deps.transport_of(mqtt_client())
    assert isinstance(t, ct.MqttCommandTransport) and t.kind == "mqtt"


def test_transport_of_prefers_modbus_over_mqtt_in_a_multisource():
    deps, _, _ = make_deps()
    src_mb = type("S", (), {"protocol": "tcp"})()
    src_mq = type("S", (), {"protocol": "mqtt"})()
    client = multi_source_client([(src_mq, mqtt_client()), (src_mb, modbus_client())])
    t = deps.transport_of(client)
    assert t.kind == "modbus"          # Modbus resolved first, as today


def test_transport_of_finds_mqtt_source_when_no_modbus():
    deps, _, _ = make_deps()
    src_mq = type("S", (), {"protocol": "mqtt"})()
    client = multi_source_client([(src_mq, mqtt_client())])
    t = deps.transport_of(client)
    assert t.kind == "mqtt"


def test_transport_of_none_when_no_writable_source():
    deps, _, _ = make_deps()
    assert deps.transport_of(None) is None
    assert deps.transport_of(type("X", (), {})()) is None   # the 409 'no transport' case


def test_capability_flags():
    deps, _, _ = make_deps()
    mb = deps.transport_of(modbus_client())
    mq = deps.transport_of(mqtt_client())
    assert (mb.supports_commands, mb.supports_raw_write) == (True, True)
    assert (mq.supports_commands, mq.supports_raw_write) == (True, False)   # MQTT: commands only


# ── lane_key + lock_for (L0 step 2: routed through the transport) ────────────
def test_lane_key_delegates_with_dev_and_client():
    seen = []
    deps, _, _ = make_deps(lane_key=lambda dev, cl: seen.append((dev, cl)) or "lane:x")
    client = modbus_client()
    t = deps.transport_of(client)
    dev = FakeDev("d1")
    assert t.lane_key(dev, client) == "lane:x"
    assert seen == [(dev, client)]


def test_modbus_lock_for_passes_the_driver():
    seen = []
    deps, _, _ = make_deps(lock_for=lambda first, dev=None: seen.append((first, dev)) or "LOCK")
    client = modbus_client()
    t = deps.transport_of(client)
    dev = FakeDev("d1")
    assert t.lock_for({"client": client, "dev": dev}) == "LOCK"
    assert seen == [(t.driver, dev)]          # Modbus passes its resolved driver, as today


def test_mqtt_lock_for_passes_the_client():
    seen = []
    deps, _, _ = make_deps(lock_for=lambda first, dev=None: seen.append((first, dev)) or "LOCK")
    client = mqtt_client()
    t = deps.transport_of(client)
    dev = FakeDev("d1")
    assert t.lock_for({"client": client, "dev": dev}) == "LOCK"
    assert seen == [(client, dev)]            # MQTT passes the client (drv is None today)


def test_lock_identity_same_bus_across_calls():
    """ADR-0036 point 5: the one shared lock table — the same lock object for the
    same bus across repeated transport_of()/lock_for() calls, never a fresh one."""
    deps, _, _ = make_deps()                  # default lock_for backed by a shared dict
    client = modbus_client()
    dev = FakeDev("d1")
    lock_a = deps.transport_of(client).lock_for({"client": client, "dev": dev})
    lock_b = deps.transport_of(client).lock_for({"client": client, "dev": dev})
    assert lock_a is lock_b                   # same driver identity -> same lock object


# ── exec_timeout ─────────────────────────────────────────────────────────────
def test_modbus_exec_timeout_matches_formula():
    deps, _, _ = make_deps()
    t = deps.transport_of(modbus_client())          # timeout 3, retry 3 -> per 12
    cmd = FakeCmd(writes=[{"register": "r"}])        # n = 0 + 1 + 0 + 1 = 2 -> max(3,2)=3
    assert t.exec_timeout({"cmd": cmd}) == pytest.approx(3 * 12 + 0)


def test_mqtt_exec_timeout_is_settle_plus_5():
    deps, _, _ = make_deps()
    t = deps.transport_of(mqtt_client())
    assert t.exec_timeout({"cmd": FakeCmd(settle_s=2)}) == pytest.approx(7.0)


# ── already_set (Modbus skip_if_set against the polled store) ────────────────
def _modbus_already_ctx(store_value):
    """A ctx + the matching polled store (the value the device last reported)."""
    from multibus.core.protocols import store_key
    reg = FakeReg("r1", address=40010, register_type="holding")
    wval = 50
    store = {store_key(40010, "holding"): {"value": store_value, "mono": time.monotonic(),
                                           "interval": 5.0}}
    cmd = FakeCmd(name="limit", skip_if_set=True, writes=[{"register": "r1", "value": wval}],
                  verify=[])
    ctx = {"cmd": cmd, "norm": {}, "dev": FakeDev(), "regs": {"r1": reg}}
    return ctx, store


def test_modbus_already_set_hit():
    ctx, store = _modbus_already_ctx(store_value=50)      # device holds 50, command writes 50
    deps, _, _ = make_deps(registry=FakeRegistry(store))
    t = deps.transport_of(modbus_client())
    res = t.already_set(ctx)
    assert res and res["already_set"] and res["frames_sent"] == 0


def test_modbus_already_set_miss_when_value_differs():
    ctx, store = _modbus_already_ctx(store_value=99)      # device holds 99, command writes 50
    deps, _, _ = make_deps(registry=FakeRegistry(store))
    t = deps.transport_of(modbus_client())
    assert t.already_set(ctx) is None


def test_modbus_already_set_skipped_when_flag_off():
    reg = FakeReg("r1", address=40010)
    cmd = FakeCmd(name="x", skip_if_set=False, writes=[{"register": "r1", "value": 1}])
    deps, _, _ = make_deps()
    t = deps.transport_of(modbus_client())
    assert t.already_set({"cmd": cmd, "norm": {}, "dev": FakeDev(), "regs": {"r1": reg}}) is None


# ── run (wraps run_command / run_mqtt_command) ───────────────────────────────
def test_modbus_run_calls_run_command(monkeypatch):
    seen = {}

    def fake_run_command(cmd, params, conn, regs, *, byte_order, dry_run, max_read):
        seen.update(cmd=cmd, conn=conn, byte_order=byte_order, dry_run=dry_run, max_read=max_read)
        return {"status": "success"}
    monkeypatch.setattr(ct, "run_command", fake_run_command)
    deps, _, _ = make_deps()
    drv = modbus_client()
    drv.byte_order, drv.max_read = "little", 120
    t = ct.ModbusCommandTransport(drv, deps)
    cmd = FakeCmd()
    out = t.run({"cmd": cmd, "norm": {"v": 1}, "regs": {}, "dry_run": False})
    assert out["status"] == "success"
    assert seen["conn"] is drv.connection and seen["byte_order"] == "little" and seen["max_read"] == 120


def test_mqtt_run_calls_run_mqtt_command_with_forbidden_topics(monkeypatch):
    seen = {}

    def fake_run_mqtt(cmd, params, src, regs, *, dry_run, forbidden_topics):
        seen.update(src=src, dry_run=dry_run, forbidden=forbidden_topics)
        return {"status": "success"}
    monkeypatch.setattr(ct, "run_mqtt_command", fake_run_mqtt)
    deps, _, _ = make_deps(forbidden=lambda dev: ["gw/a", "gw/b"])
    mq = mqtt_client()
    t = ct.MqttCommandTransport(mq, deps)
    out = t.run({"cmd": FakeCmd(), "norm": {}, "regs": {}, "dry_run": True, "dev": FakeDev()})
    assert out["status"] == "success"
    assert seen["src"] is mq and seen["dry_run"] is True and seen["forbidden"] == ["gw/a", "gw/b"]


# ── record_event ─────────────────────────────────────────────────────────────
def test_modbus_record_event_on_connection():
    deps, _, _ = make_deps()
    drv = modbus_client()
    t = ct.ModbusCommandTransport(drv, deps)
    t.record_event("info", "command", "did a thing")
    assert drv.connection.events == [("info", "command", "did a thing")]


def test_mqtt_record_event_on_source():
    deps, _, _ = make_deps()
    mq = mqtt_client()
    t = ct.MqttCommandTransport(mq, deps)
    t.record_event("warn", "command", "hmm")
    assert mq.events == [("warn", "command", "hmm")]


# ── readback (Modbus re-reads; MQTT no-ops) ──────────────────────────────────
def test_modbus_readback_pushes_store_and_polls_group():
    deps, pushes, arms = make_deps()
    drv = modbus_client()
    t = ct.ModbusCommandTransport(drv, deps)
    reg = FakeReg("out", address=40020, register_type="holding")
    cmd = FakeCmd(name="limit", readback_group="controls")
    ctx = {"dev": FakeDev("d9"), "cmd": cmd, "norm": {}, "regs": {"out": reg}}
    t.readback(ctx, {"status": "success", "after": {"out": 42}})
    assert pushes == [("d9", 40020, 42, "holding")]
    assert drv.polled and drv.polled[0][0] == ("controls",)
    assert arms == []                      # no revert_s -> no revert re-read armed


def test_modbus_readback_arms_revert_reread_when_revert_s():
    deps, pushes, arms = make_deps()
    drv = modbus_client()
    t = ct.ModbusCommandTransport(drv, deps)
    reg = FakeReg("out", address=40020)
    cmd = FakeCmd(name="limit", readback_group="controls")
    ctx = {"dev": FakeDev("d9"), "cmd": cmd, "norm": {"revert_s": 30}, "regs": {"out": reg}}
    t.readback(ctx, {"status": "success", "after": {"out": 42}})
    assert arms and arms[0][0] == "d9" and arms[0][2] == "controls"
    assert arms[0][3] == (31.0, 32.0)      # revert + REVERT_REREAD_AT_S (1.0, 2.0)


def test_mqtt_readback_is_noop():
    deps, pushes, arms = make_deps()
    t = ct.MqttCommandTransport(mqtt_client(), deps)
    assert t.readback({"dev": FakeDev(), "cmd": FakeCmd(), "norm": {}, "regs": {}},
                      {"status": "success", "after": {"x": 1}}) is None
    assert pushes == [] and arms == []


# ── target_id (CMD-6) ────────────────────────────────────────────────────────
def test_modbus_target_id():
    deps, _, _ = make_deps()
    t = deps.transport_of(modbus_client())
    reg = FakeReg("r", address=40232, register_type="holding")
    cmd = FakeCmd(writes=[{"register": "r"}])
    assert t.target_id(cmd, {"r": reg}) == "modbus:holding@40232"


def test_mqtt_target_id_uses_write_topic():
    deps, _, _ = make_deps()
    t = deps.transport_of(mqtt_client())
    reg = FakeReg("r", write_topic="dev/set/power")
    cmd = FakeCmd(name="power", writes=[{"register": "r"}])
    assert t.target_id(cmd, {"r": reg}) == "mqtt:dev/set/power"
    assert t.target_id(FakeCmd(name="power", writes=[]), {}) == "mqtt:power"
