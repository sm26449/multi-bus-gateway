"""What the 2026-10-21 UI audit found broken or misleading (section B):
a Test that only spoke Modbus, a footer time read in the wrong zone, an
installation test the operator could not run, a rule nobody could enable."""
from types import SimpleNamespace

from tests.test_devices_api import make_app, needs_tc
from tests.test_operator_role import _build_clients

MQTT_DEV = """
devices:
  - id: gx
    name: GX
    template: victron_gx_mqtt
    enabled: false
    connection: {protocol: mqtt, broker: 192.0.2.10, port: 1883, topic: 'N/c0ffee000001/#',
                 vars: {portal: c0ffee000001}}
"""


class _Mq:
    """The running MQTT input, as the registry holds it."""

    def __init__(self, connected=True, messages=0, health=None):
        self.st = {'connected': connected, 'messages': messages}
        self.health = health or {'status': 'ok', 'stale': False, 'staleness_age_s': 1.5}

    def get_stats(self):
        return dict(self.st)

    def data_health(self):
        return dict(self.health)


@needs_tc
def test_an_mqtt_device_test_says_what_its_input_sees(tmp_path):
    cfg, client = make_app(tmp_path, MQTT_DEV)
    app = client.app
    r = client.post("/api/devices/gx/test").json()
    assert r["ok"] is None and "not listening" in r["message"]           # nothing running
    for fake, ok, word in ((_Mq(connected=False), False, "not connected"),
                           (_Mq(messages=0), None, "nothing on its topics"),
                           (_Mq(messages=42), True, "42 messages, last 1.5 s ago"),
                           (_Mq(messages=3, health={'status': 'down', 'stale': True, 'staleness_age_s': 900}),
                            False, "stale limit"),
                           (_Mq(messages=0, health={'status': 'down', 'stale': True, 'available': False,
                                                    'staleness_age_s': 5}), False, "offline"),
                           (_Mq(messages=0, health={'status': 'idle', 'stale': False}), None, "no value selected")):
        dev = cfg.get_device("gx")
        fake.broker, fake.port = "192.0.2.10", 1883
        app.state.registry.replace("gx", dev, client=SimpleNamespace(parts=[(dev.sources[0], fake)]))
        r = client.post("/api/devices/gx/test").json()
        assert r["ok"] is ok and word in r["message"], r
        assert "host required" not in r["message"]


@needs_tc
def test_an_http_device_test_runs_its_stored_request(tmp_path, monkeypatch):
    cfg, client = make_app(tmp_path, """
devices:
  - id: inv
    name: Inverter
    template: fronius_solar_api_inverter
    enabled: false
    connection: {protocol: http, url: 'http://192.0.2.20/solar_api/v1/x', headers: {Authorization: secret}}
""")
    import multibus.http_client as hc
    seen = []

    def fake_test_read(self):
        seen.append(self)
        return {"ok": True, "message": "HTTP OK — 3/3 paths resolved"}
    monkeypatch.setattr(hc.HttpClient, "test_read", fake_test_read)
    r = client.post("/api/devices/inv/test").json()
    assert seen, "the stored request was not run"
    assert r["ok"] is True and "paths resolved" in r["message"] and "secret" not in str(r)


@needs_tc
def test_the_operator_may_test_an_installation_but_not_change_it(tmp_path):
    c = _build_clients(tmp_path)
    r = c.op.post("/api/endpoints/nope/test")
    assert r.status_code == 404                      # through the gate, then not found
    assert c.op.put("/api/endpoints/nope", json={}).status_code == 403
    assert c.viewer.post("/api/endpoints/nope/test").status_code == 403


@needs_tc
def test_the_live_messages_carry_an_epoch_time(tmp_path):
    cfg, client = make_app(tmp_path)
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "init" and "ts" in msg


@needs_tc
def test_a_rule_is_enabled_and_disabled_through_the_api(tmp_path):
    from tests.test_rules_api import _rule_app
    from tests.test_commands import _Inverter
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    assert client.post("/api/rules/ov-u1/enable", json={"enabled": False}).json()["rule"]["enabled"] is False
    assert client.post("/api/rules/ov-u1/enable", json={"enabled": True}).json()["rule"]["enabled"] is True
    assert "safe_params" in client.get("/api/rules").json()["rules"][0]


@needs_tc
def test_a_rule_enabled_again_commands_at_once_and_releases_only_once(tmp_path):
    """Enabled again, an armed rule asks for its want on the next tick — not
    after a reassert window; disabled then put in shadow, it releases once."""
    from tests.test_rules_api import _rule_app, _feed
    from tests.test_commands import _Inverter
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    for _ in range(3):
        _feed(app, clock, 'pv-u1', {'voltage_l1_n': 252.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231, 'power_limit_pct': 100})
        rt.tick(clock()); clock.t += 2
    assert inv.writes and inv.writes[-1][1][0] == 6000                     # 60 %
    assert client.post("/api/rules/ov-u1/enable", json={"enabled": False}).status_code == 200
    for f in list(rt.__dict__.get('_releasing', [])):
        f.result(timeout=5)
    assert inv.writes[-1][1][0] == 10000                                     # released to 100 %
    n = len(inv.writes)
    _feed(app, clock, 'pv-u1', {'voltage_l1_n': 252.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231, 'power_limit_pct': 100})
    # disabled, then shadow: no second release
    assert client.post("/api/rules/ov-u1/mode", json={"mode": "shadow"}).status_code == 200
    for f in list(rt.__dict__.get('_releasing', [])):
        f.result(timeout=5)
    assert len(inv.writes) == n
    # armed and enabled again: the want goes out on the next tick
    client.post("/api/rules/ov-u1/mode", json={"mode": "armed"})
    client.post("/api/rules/ov-u1/enable", json={"enabled": True})
    clock.t += 2
    _feed(app, clock, 'pv-u1', {'voltage_l1_n': 252.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231, 'power_limit_pct': 100})
    rt.tick(clock())
    assert len(inv.writes) > n and inv.writes[-1][1][0] == 6000


@needs_tc
def test_an_edited_connection_is_tested_with_the_stored_secrets(tmp_path, monkeypatch):
    """The device page tests an EDITED connection ad hoc; the password it does
    not show is the stored one — used to connect, never echoed."""
    cfg, client = make_app(tmp_path, """
security: {allow_nonlan_http_devices: true}
devices:
  - id: gx
    name: GX
    template: victron_gx_mqtt
    enabled: false
    connection: {protocol: mqtt, broker: 192.0.2.10, port: 1883, topic: 'N/c0ffee000001/#',
                 username: u, password: s3cret, vars: {portal: c0ffee000001}}
""")
    import paho.mqtt.client as paho
    used = {}

    class _Cli:
        def __init__(self, *a, **k):
            self.on_connect = self.on_message = None

        def username_pw_set(self, u, p):
            used['user'], used['pw'] = u, p

        def connect(self, host, port, keepalive=60):
            used['host'] = host
            raise OSError("unreachable in the test")

        def loop_start(self):
            pass

        def loop_stop(self):
            pass

        def disconnect(self):
            pass
    monkeypatch.setattr(paho, "Client", _Cli)
    r = client.post("/api/devices/test", json={"device_id": "gx", "connection": {
        "protocol": "mqtt", "broker": "192.0.2.11", "port": 1883, "topic": "N/c0ffee000001/#",
        "username": "u", "password": ""}})
    assert r.status_code == 200, r.text
    assert used == {"user": "u", "pw": "s3cret", "host": "192.0.2.11"}
    assert "s3cret" not in r.text
