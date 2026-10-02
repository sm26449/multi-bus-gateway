# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Pins for the 2026-10-01 audit fixes.

Covered here:
- write-lease: a CLEAN shutdown actually writes the safe value through a
  revert that honors the is_current() predicate (the production builders do);
  a superseded lease is kept for boot recovery.
- rules: the sample identity is the raw monotonic stamp (same reading across
  ticks = ONE sample for the debounce and the max_step plausibility guard);
  a valid signal whose age is unknown goes stale instead of staying fresh
  forever.
- MQTT/HA write parity with HTTP: write_allowed enforced, encoder clamping
  refused up front, a scale_from or command-fronted register never takes a
  raw write (even when the fronting command is disabled).
- sessions/passkeys: an import/restore that rotates identity revokes every
  session (and re-issues the admin caller's); passkey login resolves the
  LIVE account (current role, 401 when the account is gone);
  PasskeyStore.reload() picks up a file rewritten behind the process.
- operator: template commands (the bounded write form) are the operator's,
  group fan-out stays admin.
- expressions: a non-numeric constant is refused at EVAL time too (a tree
  that dodged validate_expression cannot allocate 'A' * 10**8).
"""
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from multibus.write_lease import WriteLeaseManager
from tests.test_devices import write_config

try:
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False

needs_tc = pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")


# ── write-lease: clean shutdown with predicate-honoring reverts ──────────────

def test_shutdown_revert_fires_through_the_is_current_predicate():
    """The production revert builders check is_current() and skip the write
    when it is False. Before the fix, stop() handed them UN-expired leases,
    so every clean-shutdown revert silently skipped the safe write while the
    lease file was emptied anyway — nothing on the wire AND nothing left for
    boot recovery."""
    mgr = WriteLeaseManager()
    wrote = []

    def guarded_revert(is_current):
        if not is_current():          # exactly what api.py's builders do
            return
        wrote.append("safe")
    mgr.arm("inv", "holding", 40232, lease_ms=600_000, revert=guarded_revert)
    mgr.stop(revert_now=True)
    assert wrote == ["safe"]
    assert mgr.snapshot() == []       # reverted → removed from the persisted set


def test_shutdown_keeps_a_lease_renewed_mid_revert():
    """A renewal that lands while stop() is reverting bumps the generation:
    the stale revert must skip the write and the NEWER lease must survive
    on disk for boot recovery."""
    mgr = WriteLeaseManager()
    wrote = []

    def renewing_revert(is_current):
        # a controller renews between the force-expiry and our write
        mgr.arm("inv", "holding", 40232, lease_ms=600_000, revert=renewing_revert)
        if not is_current():
            return
        wrote.append("safe")
    mgr.arm("inv", "holding", 40232, lease_ms=600_000, revert=renewing_revert)
    mgr.stop(revert_now=True)
    assert wrote == []                               # superseded → no write
    assert [(l['device'], l['address']) for l in mgr.snapshot()] == [("inv", 40232)]


# ── rules: sample identity + unknown-age fail-closed ─────────────────────────

def _steps_rule(**over):
    from multibus.rules import parse_rule_def
    raw = {"id": "ov", "label": "OV", "kind": "steps", "enabled": True,
           "mode": "armed", "signal": "voltage", "stale_after_s": 60,
           "every_s": 2, "target": {"command": "power_limit", "param": "value"},
           "signal_valid": {"min": 100, "max": 300, "max_step": 10},
           "steps": [{"above": 253, "value": 60}],
           "release_below": 250, "base": {"value": 100}}
    raw.update(over)
    return parse_rule_def(raw)


def test_same_mono_stamp_is_one_sample_for_the_plausibility_guard():
    """A 273 V artefact must be held until a DIFFERENT sample confirms it.
    With the stable mono identity, re-seeing the same reading on the next
    tick is NOT a confirmation (before the fix, now-age jitter made every
    tick a 'new sample' and the spike confirmed itself)."""
    from multibus.rules import RuleState
    st = RuleState(_steps_rule())
    st.evaluate(10.0, 252.0, 0.5, sample_ts=1000.0)          # a sane baseline
    d = st.evaluate(100.0, 273.0, 0.5, sample_ts=1050.0)     # the artefact
    assert d.action == 'ignored'                              # held, not acted on
    # ticks 2..5 re-see the SAME store value (same mono stamp)
    for t in (102.0, 104.0, 106.0, 108.0):
        d = st.evaluate(t, 273.0, 0.5 + (t - 100.0), sample_ts=1050.0)
        assert d.action == 'ignored', f"self-confirmed at t={t}"
    # a genuinely new sample at the same level IS a confirmation
    d = st.evaluate(110.0, 273.0, 0.5, sample_ts=1060.0)
    assert d.action != 'ignored'


def test_unknown_age_signal_goes_stale_not_fresh_forever():
    """age=None (no monotonic stamp on any input) used to set last_valid_ts
    to 'now' — never stale, an armed rule frozen in its current want."""
    from multibus.rules import RuleState, STALE
    st = RuleState(_steps_rule(on_stale="hold"))
    st.evaluate(0.0, 252.0, 0.5, sample_ts=1.0)              # fresh + valid once
    # the signal keeps evaluating but its inputs lost their stamps
    st.evaluate(30.0, 252.0, None, sample_ts=None)
    assert st.state != STALE                                  # within stale_after_s
    d = st.evaluate(120.0, 252.0, None, sample_ts=None)       # 120 s > 60 s
    assert st.state == STALE and d.action == 'stale'


def test_runtime_passes_the_raw_mono_stamp_through():
    """_signal returns the oldest input's raw mono stamp and tick hands it to
    evaluate unchanged — no reconstruction, no jitter."""
    from multibus.rules_runtime import RulesRuntime

    store = {"voltage": (230.0, 123.456)}

    def resolver_factory(_):
        def resolve(name):
            v = store.get(name)
            if v is None:
                return None
            resolve.touched_mono.append(v[1])
            return v[0]
        resolve.touched_mono = []
        resolve.touched_ts = []
        resolve.touched_interval = []
        return resolve

    rt = RulesRuntime.__new__(RulesRuntime)
    rt._resolver_factory = resolver_factory
    rt._mono = lambda: 200.0
    val, age, smono = rt._signal(_steps_rule(), now=0.0)
    assert val == 230.0 and smono == 123.456
    assert age == pytest.approx(200.0 - 123.456)


# ── MQTT/HA write parity with HTTP ───────────────────────────────────────────

def _parity_tpl():
    from multibus.device_template import parse_template
    return parse_template({"device_template": {"schema_version": 1, "id": "parity_tpl",
        "name": "Parity", "protocol": {}, "categories": {}, "registers": [
            {"address": 100, "name": "limit", "data_type": "uint16",
             "register_type": "holding", "writable": True, "write_min": 0, "write_max": 100},
            {"address": 110, "name": "onoff", "data_type": "uint16",
             "register_type": "holding", "writable": True, "write_allowed": [0, 1]},
            {"address": 120, "name": "scaled", "data_type": "uint16",
             "register_type": "holding", "writable": True, "scale": 100.0},
            {"address": 130, "name": "pct_sf", "data_type": "uint16",
             "register_type": "holding", "writable": True, "scale_from": "the_sf"},
            {"address": 131, "name": "the_sf", "data_type": "int16",
             "register_type": "holding"},
            {"address": 140, "name": "fronted", "data_type": "uint16",
             "register_type": "holding", "writable": True, "write_min": 0, "write_max": 100},
        ],
        "commands": {
            "set_fronted": {"params": {"value": {"min": 0, "max": 100, "required": True}},
                            "writes": [{"register": "fronted", "value": "${value}"}]},
        }}}, builtin=True)


class _FakeClient:
    def __init__(self):
        self.reg = {}

    def write_value(self, address, register_type, data_type, value,
                    scale=1.0, offset=0.0, prefer_fc6=False):
        self.reg[address] = value
        return True, None, [int(float(value))]

    def read_register(self, address, data_type, register_type):
        return self.reg.get(address)


def _reg(address, name, enum=None):
    from multibus.config import SelectedRegister
    return SelectedRegister(address=address, name=name, label=name, unit="",
                            data_type="uint16", poll_group="normal", enum=enum)


@pytest.fixture
def mqtt_handler(tmp_path):
    from multibus.api import create_api
    from multibus.config import DeviceConfig
    cfg = write_config(tmp_path)
    cfg.security.allow_writes = True
    cfg.mqtt.allow_write_entities = True
    cfg.mqtt.username = "gw"
    dev = DeviceConfig(id="par", name="par", template="parity_tpl", protocol="tcp")
    fake = _FakeClient()
    mock_mqtt = MagicMock()
    app, _ = create_api(cfg, None, mock_mqtt, None,
                        devices=[(d, None) for d in cfg.devices] + [(dev, fake)])
    app.state.template_registry._templates["parity_tpl"] = _parity_tpl()
    handler = mock_mqtt.set_command_write_handler.call_args[0][0]
    return handler, fake, cfg


@needs_tc
def test_mqtt_write_enforces_write_allowed(mqtt_handler):
    handler, fake, _ = mqtt_handler
    handler("par", _reg(110, "onoff"), "2")          # not in [0, 1]
    assert 110 not in fake.reg
    handler("par", _reg(110, "onoff"), "1")
    assert fake.reg[110] == 1


@needs_tc
def test_mqtt_write_refuses_what_the_encoder_would_clamp(mqtt_handler):
    handler, fake, _ = mqtt_handler
    # scale 100: 700 encodes to 70000 > uint16 max — refused, not clamped
    handler("par", _reg(120, "scaled"), "700")
    assert 120 not in fake.reg
    handler("par", _reg(120, "scaled"), "123")
    assert fake.reg[120] == 123


@needs_tc
def test_mqtt_write_refuses_scale_from_register(mqtt_handler):
    """HTTP 422s a scale_from register (3.83.0); the MQTT face used to encode
    it with the STATIC scale and 'verify' the wrong value."""
    handler, fake, _ = mqtt_handler
    handler("par", _reg(130, "pct_sf"), "50")
    assert 130 not in fake.reg


@needs_tc
def test_mqtt_write_refuses_fronted_register_when_command_unavailable(mqtt_handler):
    """The fronting command exists in the template but has NO binding (not
    enabled): the raw fallback used to write the register directly — now it
    refuses, like HTTP."""
    handler, fake, _ = mqtt_handler
    handler("par", _reg(140, "fronted"), "50")
    assert 140 not in fake.reg


@needs_tc
def test_discovery_skips_scale_from_and_unfronted_command_registers(tmp_path):
    """HA must not advertise a number the handler would refuse (or worse,
    used to mis-write)."""
    from multibus.api import create_api
    from multibus.config import DeviceConfig
    cfg = write_config(tmp_path)
    cfg.security.allow_writes = True
    cfg.mqtt.allow_write_entities = True
    cfg.mqtt.username = "gw"
    dev = DeviceConfig(id="par", name="par", template="parity_tpl", protocol="tcp")
    cfg.unit_registers = lambda d: [_reg(100, "limit"), _reg(130, "pct_sf"),
                                    _reg(140, "fronted")]
    mock_mqtt = MagicMock()
    mock_mqtt.connected = True
    captured = {}
    mock_mqtt.publish_device_discovery = lambda did, name, prefix, regs, model=None, write_rules=None: \
        captured.update({did: dict(write_rules or {})})
    app, _ = create_api(cfg, None, mock_mqtt, None,
                        devices=[(d, None) for d in cfg.devices] + [(dev, _FakeClient())])
    app.state.template_registry._templates["parity_tpl"] = _parity_tpl()
    for h in mock_mqtt.discovery_hooks:
        h()
    rules = captured.get("par", {})
    assert 100 in rules                      # plain writable: advertised
    assert 130 not in rules                  # scale_from, no HA command: skipped
    assert 140 not in rules                  # fronted, command not enabled: skipped


# ── sessions / passkeys on import & restore ──────────────────────────────────

def _auth_app(tmp_path):
    from multibus import auth as _a
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=f"""
ui:
  auth:
    enabled: true
    username: boss
    password: "{_a.hash_password('pw')}"
""")
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    return cfg, app


def _login(app, user="boss", pw="pw"):
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return c


def _bundle(cfgyaml):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("config.yaml", cfgyaml)
        z.writestr("manifest.json", '{"backup_version": 1}')
    return buf.getvalue()


@needs_tc
def test_import_that_rotates_identity_revokes_other_sessions(tmp_path):
    """F-16 completion: import/restore reach auth_state.reload() directly and
    used to skip the revocation — a stolen cookie survived 'I restored last
    week's snapshot to kick the attacker'. The caller's own session is
    re-issued (same pattern as the ui-security card)."""
    from multibus import auth as _a
    cfg, app = _auth_app(tmp_path)
    admin = _login(app)
    other = _login(app)                               # the "attacker's" cookie
    new_pw = _a.hash_password("rotated")
    r = admin.post("/api/config/import?apply=false", content=_bundle(
        "modbus:\n  host: 192.168.1.207\n"
        "ui:\n  auth:\n    enabled: true\n    username: boss\n"
        f'    password: "{new_pw}"\n'),
        headers={"Content-Type": "application/zip"})
    assert r.status_code == 200, r.text
    assert other.get("/api/audit").status_code == 401        # revoked
    assert admin.get("/api/audit").status_code == 200        # re-issued


@needs_tc
def test_import_without_identity_change_keeps_sessions(tmp_path):
    cfg, app = _auth_app(tmp_path)
    admin = _login(app)
    other = _login(app)
    r = admin.post("/api/config/import?apply=false", content=_bundle(
        "modbus:\n  host: 192.168.1.207\n"),
        headers={"Content-Type": "application/zip"})
    assert r.status_code == 200, r.text
    assert other.get("/api/audit").status_code == 200        # untouched


@needs_tc
def test_import_prunes_passkeys_of_gone_accounts(tmp_path):
    """An import whose config renames the admin leaves the old account's
    passkeys dead AND removed from the registry (mirrors the ui-security
    pruning, F-16)."""
    from multibus import auth as _a
    cfg, app = _auth_app(tmp_path)
    store = app.state.ctx.passkey_store
    store.add(cred_id=b"old-cred", public_key=b"k", sign_count=0,
              user="boss", role="admin", rp_id="testserver", label="old boss key")
    admin = _login(app)
    r = admin.post("/api/config/import?apply=false", content=_bundle(
        "modbus:\n  host: 192.168.1.207\n"
        "ui:\n  auth:\n    enabled: true\n    username: newboss\n"
        f'    password: "{_a.hash_password("pw2")}"\n'),
        headers={"Content-Type": "application/zip"})
    assert r.status_code == 200, r.text
    assert store.list() == []                        # boss is gone → key pruned


def test_passkey_store_reload_rereads_the_file(tmp_path):
    from multibus.passkeys import PasskeyStore
    p = tmp_path / "passkeys.json"
    s = PasskeyStore(str(p))
    s.add(cred_id=b"a", public_key=b"k", sign_count=0,
          user="boss", role="admin", rp_id="gw.lan")
    # a restore rewrites the file behind the live store
    p.write_text(json.dumps({"credentials": []}))
    assert len(s.list()) == 1                        # stale view before reload
    s.reload()
    assert s.list() == []                            # and the next _save keeps it


@needs_tc
def test_passkey_login_resolves_the_live_account(tmp_path, monkeypatch):
    """login_finish mints the CURRENT role of a still-existing account; a
    credential whose user vanished (import/restore) is a dead key — 401."""
    import webauthn
    cfg, app = _auth_app(tmp_path)
    store = app.state.ctx.passkey_store
    # enrolled long ago as admin under 'ghost' (account no longer configured)
    store.add(cred_id=b"ghost-cred", public_key=b"k", sign_count=0,
              user="ghost", role="admin", rp_id="testserver")
    # and one for the real admin, but frozen with a WRONG role in the blob
    store.add(cred_id=b"boss-cred", public_key=b"k", sign_count=0,
              user="boss", role="viewer", rp_id="testserver")
    monkeypatch.setattr(webauthn, "verify_authentication_response",
                        lambda **kw: SimpleNamespace(new_sign_count=1))
    c = TestClient(app, raise_server_exceptions=False)

    def _finish(cred_b64):
        begin = c.post("/api/auth/passkey/login/begin")
        assert begin.status_code == 200, begin.text
        return c.post("/api/auth/passkey/login/finish",
                      json={"state": begin.json()["state"],
                            "credential": {"id": cred_b64}},
                      headers={"Origin": "http://testserver"})

    from multibus.passkeys import _b64u
    r = _finish(_b64u(b"ghost-cred"))
    assert r.status_code == 401                      # account gone → dead key
    r = _finish(_b64u(b"boss-cred"))
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"               # LIVE role, not the frozen one


# ── operator: commands are the bounded live-write form ───────────────────────

@needs_tc
def test_operator_may_run_commands_but_not_group_fanout(tmp_path):
    from tests.test_operator_role import _build_clients
    clients = _build_clients(tmp_path)
    clients.cfg.security.allow_writes = True
    # unknown device → 404 proves the 403 wall is gone for the operator
    assert clients.op.post("/api/devices/nope/commands/power_limit",
                           json={"value": 60}).status_code == 404
    assert clients.op.post("/api/devices/nope/commands/power_limit/dry-run",
                           json={"value": 60}).status_code == 404
    assert clients.op.post("/api/devices/nope/actions/power_limit",
                           json={"limit_pct": 60}).status_code == 404
    # the viewer still cannot
    assert clients.viewer.post("/api/devices/nope/commands/power_limit",
                               json={"value": 60}).status_code == 403
    # group fan-out stays admin-only
    assert clients.op.post("/api/endpoints/e/groups/g/commands/power_limit",
                           json={"value": 60}).status_code == 403
    assert clients.op.post("/api/endpoints/e/groups/g/actions/power_limit",
                           json={"limit_pct": 60}).status_code == 403


# ── expressions: eval-time constant guard ────────────────────────────────────

def test_eval_rejects_string_constants_even_without_validate():
    """Defense in depth: a tree compiled straight from a hand-edited file must
    not evaluate 'A' * N — the allocation would happen before any result cap."""
    from multibus import expressions
    tree = expressions.compile_expression("'A' * 100000000")
    with pytest.raises(expressions.ExpressionError):
        expressions.evaluate_tree(tree, lambda n: None)


def test_calc_engine_marks_invalid_entries_and_skips_them(tmp_path, monkeypatch):
    from multibus.calc_engine import CalcEngine

    class _Cfg:
        def load_calculated(self, device_id):
            return [{"name": "bad", "expr": "'A' * 100000000"},
                    {"name": "good", "expr": "1 + 1"}]
    eng = CalcEngine(_Cfg(), lambda d: {}, publishers=SimpleNamespace())
    built = eng.load("dev")
    assert built[0]["_invalid"] is True and built[0]["_tree"] is None
    assert built[1]["_invalid"] is False and built[1]["_tree"] is not None
    # addresses keep their index-based slots (golden routing invariant)
    assert built[1]["_reg"].address == built[0]["_reg"].address + 1


# ── datapath: counter filter, vmeter stats, MQTT input, endpoint aggregates ──

def test_monotonic_filter_does_not_freeze_a_fast_growing_counter():
    """Steady growth >50 %/read (a freshly reset counter, an EV session near
    zero) used to trip the upward-jump guard forever: the ±5 % spread test can
    never be met by >5 %/read growth and only accepted values advanced the
    baseline. Ascending continuation now confirms."""
    from multibus.counter_filter import MonotonicFilter
    f = MonotonicFilter(reset_confirm=3)
    out = [f.feed(v) for v in (100, 210, 440, 920, 1900, 3900)]
    assert out[0] == 100
    assert any(v is not None for v in out[1:]), "baseline froze"


def test_monotonic_filter_still_drops_the_lone_flipped_word():
    from multibus.counter_filter import MonotonicFilter
    f = MonotonicFilter(reset_confirm=3)
    f.feed(1_000_000)
    assert f.feed(2_148_483_648) is None          # glitch held
    assert f.feed(1_000_100) == 1_000_100         # normal resumes at once
    assert f.feed(2_200_000_000) is None          # next lone spike held again


def test_vmeter_req_rate_decays_to_zero_when_traffic_stops():
    """The window used to anchor on the LAST traffic bucket, so a vanished
    consumer showed its old req/s forever (and an alert rule on req_rate
    never saw 0)."""
    import time as _time
    from multibus.virtual_meter import VMeterStats
    st = VMeterStats()
    old = int(_time.time()) - 3600
    st.rate.append((old, 50))                      # traffic an hour ago
    assert st.req_rate(window_s=10) == 0.0


def test_mqtt_input_scales_a_scale_from_register():
    """apply_corrections ran without `siblings` here, so a scale_from register
    on an MQTT source was unconditionally dropped (sf_missing) — despite the
    shared-pipeline claim."""
    from types import SimpleNamespace as NS
    from multibus import mqtt_input as mi
    from multibus.config import SelectedRegister

    def reg(addr, name, **kw):
        return SelectedRegister(address=addr, name=name, label=name, unit="",
                                data_type="float", poll_group="normal",
                                topic="sensors/x", json_path=name, **kw)
    regs = [reg(1, "pct", scale_from="pct_sf"), reg(2, "pct_sf")]
    cli = mi.MqttInputClient({"topic": "sensors/x"}, regs)
    got = {}
    cli.publish_callback = lambda pg, data: got.update(data)
    # SF arrives in the SAME message as its dependent: prescan must catch it
    cli._on_message(None, None, NS(topic="sensors/x",
                                   payload=b'{"pct": 600, "pct_sf": -1}'))
    assert got and got[1]["value"] == 60.0         # 600 × 10^-1


def test_mqtt_input_update_registers_unsubscribes_removed_topics():
    from multibus import mqtt_input as mi
    from multibus.config import SelectedRegister

    def reg(addr, name, topic):
        return SelectedRegister(address=addr, name=name, label=name, unit="",
                                data_type="float", poll_group="normal",
                                topic=topic, json_path=name)
    cli = mi.MqttInputClient({"topic": ""}, [reg(1, "a", "t/a"), reg(2, "b", "t/b")])
    calls = {"sub": [], "unsub": []}
    cli._client = SimpleNamespace(subscribe=lambda t: calls["sub"].append(t),
                                  unsubscribe=lambda t: calls["unsub"].append(t))
    cli.connected = True
    cli.update_registers([reg(1, "a", "t/a")])
    assert calls["unsub"] == ["t/b"] and "t/a" in calls["sub"]


def test_endpoint_influx_buffers_during_an_outage():
    """The old `connected` gate dropped the endpoint's own series while every
    unit's series was buffered and replayed — a permanent hole in the
    installation total after each outage."""
    pytest.importorskip("influxdb_client")
    from multibus.endpoint_aggregator import EndpointAggregator

    class _Influx:
        connected, publish_mode = False, "changed"     # OUTAGE
        def __init__(self): self.points = []
        def write_point(self, p, ts=None, bucket=None): self.points.append(bucket)

    ix = _Influx()
    agg = EndpointAggregator.__new__(EndpointAggregator)
    agg._get_influx = lambda: ix
    agg._influx_last = {}
    agg._ensured = set()
    agg._publish_influx({"influxdb": {"enabled": True, "bucket": "b"}},
                        "plant", {"power_active_total": 1.0})
    assert ix.points, "aggregate was dropped instead of buffered"


def test_poller_sf_map_is_shared_across_poll_groups():
    """A *_SF register in one poll group must still scale its dependents in
    another: the refs set and last-good memo are device-wide now."""
    from multibus.modbus_client import RegisterPoller
    from multibus.config import SelectedRegister

    def reg(addr, name, group, **kw):
        return SelectedRegister(address=addr, name=name, label=name, unit="",
                                data_type="int16", poll_group=group, **kw)
    shared_refs = {"w_sf"}
    shared_memo = {}
    # group A polls only the SF; group B polls only the dependent
    pa = RegisterPoller.__new__(RegisterPoller)
    pb = RegisterPoller.__new__(RegisterPoller)
    for p, regs in ((pa, [reg(10, "w_sf", "slow")]),
                    (pb, [reg(20, "w", "normal", scale_from="w_sf")])):
        p._sf_refs = shared_refs
        p._sf_last_good = shared_memo
        p.registers = regs
    # group A's prescan records the exponent into the shared memo …
    pa._sf_last_good["w_sf"] = -1
    # … and group B's corrections see it
    from multibus.value_decode import apply_corrections
    v = apply_corrections(600, pb.registers[0], siblings=pb._sf_last_good)
    assert v == 60.0
