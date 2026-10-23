# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The OPC UA server, with a real client: every value with its quality and
the time it was read, stable node ids, the gateway's own accounts, and
encryption that is really required when it says so."""
import asyncio
import socket
import time
from types import SimpleNamespace

import pytest

asyncua = pytest.importorskip("asyncua")

from multibus.opcua import OpcUaServer, quality_of  # noqa: E402
from multibus.opcua.server import LOCKOUT_FAILS, _Users  # noqa: E402


# a server's first start makes its certificate and its address space: on a
# loaded host (the full suite, one CPU busy) that took longer than the 15 s
# the gateway waits — the server came up, the test had given up on it
START_WAIT_S = 60.0


def _port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _port_frees(port, within_s=5.0):
    # the server's listening socket closes on its own loop: under a loaded
    # suite that can take a moment after "running: false" is answered
    end = time.monotonic() + within_s
    while True:
        with socket.socket() as s:
            try:
                s.bind(('127.0.0.1', port))
                return True
            except OSError:
                if time.monotonic() > end:
                    return False
        time.sleep(0.05)


def _cfg(**kw):
    base = dict(enabled=True, host='127.0.0.1', port=_port(), encryption='off', allow_anonymous=False, update_s=0.2)
    base.update(kw)
    return SimpleNamespace(**base)


class _Plant:
    def __init__(self):
        now = time.monotonic()
        self.live = True
        self.store = {
            40000: {'name': 'power_active_total', 'label': 'Active power', 'unit': 'W', 'value': 1234.5,
                    'ts': 1_800_000_000.0, 'mono': now, 'interval': 1.0},
            40002: {'name': 'mode', 'label': 'Mode', 'unit': '', 'value': 'Running', 'ts': 1_800_000_001.0,
                    'mono': now, 'interval': 5.0},
            40004: {'name': 'old', 'label': 'Old value', 'unit': 'V', 'value': 230.0, 'ts': 1_800_000_002.0,
                    'mono': now - 600, 'interval': 1.0},
        }

    def devices(self):
        yield 'meter1', 'Grid meter', self.live, 'ok' if self.live else 'down', self.store


def _auth(user, pw):
    return 'viewer' if (user, pw) == ('scada', 'right-pass') else None


def _run(coro):
    return asyncio.run(coro)


# ── the pure parts ───────────────────────────────────────────────────────────

def test_asyncua_does_not_flood_the_log_with_every_subscription():
    import logging

    from multibus.opcua import server  # noqa: F401
    assert logging.getLogger("asyncua").level >= logging.WARNING      # its own, whatever the root says


def test_quality_says_fresh_old_down_and_waiting():
    now = 1000.0
    fresh = {'value': 1, 'mono': 999.0, 'interval': 1.0}
    assert quality_of(fresh, True, now) == 'good'
    assert quality_of({**fresh, 'mono': 900.0}, True, now) == 'stale'          # 100 s > 60 s floor
    assert quality_of({**fresh, 'mono': None}, True, now) == 'stale'           # no read time: fail closed
    assert quality_of(fresh, False, now) == 'down'
    assert quality_of({'value': None}, True, now) == 'waiting' and quality_of(None, True, now) == 'waiting'


def test_users_are_the_gateways_accounts_with_a_lockout():
    t = {'now': 0.0}
    u = _Users(_auth, allow_anonymous=False, clock=lambda: t['now'])
    assert u.get_user(None) is None                                           # anonymous refused
    assert u.get_user(None, 'scada', b'right-pass').name == 'scada'
    for _ in range(LOCKOUT_FAILS):
        assert u.get_user(None, 'scada', 'wrong') is None
    assert u.get_user(None, 'scada', 'right-pass') is None                    # locked, even with the right one
    t['now'] = 400.0
    assert u.get_user(None, 'scada', 'right-pass') is not None                # five minutes later
    assert _Users(_auth, allow_anonymous=True).get_user(None).name == 'anonymous'


# ── a real server, a real client ─────────────────────────────────────────────

def _server(tmp_path, plant, **kw):
    srv = OpcUaServer(_cfg(**kw), devices=plant.devices, authenticate=_auth, config_dir=tmp_path)
    assert srv.start(wait_s=START_WAIT_S), srv.error or 'not ready within START_WAIT_S'
    deadline = time.time() + 30
    while srv.status()['variables'] < 5 and time.time() < deadline:      # 3 values + _Online + _Health
        time.sleep(0.1)
    return srv


async def _read(url, nodes, user='scada', pw='right-pass', security=None):
    from asyncua import Client
    c = Client(url, timeout=5)
    if user:
        c.set_user(user)
        c.set_password(pw)
    if security:
        await c.set_security_string(security)
    async with c:
        idx = await c.get_namespace_index('urn:multi-bus-gateway')
        out = {}
        for n in nodes:
            node = c.get_node(f"ns={idx};s=Devices/meter1/{n}")
            out[n] = await node.read_data_value(raise_on_bad_status=False)
        name = await c.get_node(f"ns={idx};s=Devices/meter1/power_active_total").read_display_name()
        return out, name.Text


def test_values_with_quality_and_read_time_under_stable_node_ids(tmp_path):
    from asyncua import ua
    plant = _Plant()
    srv = _server(tmp_path, plant)
    try:
        url = srv.endpoint.replace('0.0.0.0', '127.0.0.1')
        vals, label = _run(_read(url, ['power_active_total', 'mode', 'old', '_Online', '_Health']))
        p = vals['power_active_total']
        assert p.Value.Value == 1234.5 and p.StatusCode.is_good()
        assert p.SourceTimestamp.timestamp() == pytest.approx(1_800_000_000.0)    # when it was READ
        assert label == 'Active power'
        assert vals['mode'].Value.Value == 'Running'
        assert vals['old'].StatusCode.value == ua.StatusCodes.UncertainLastUsableValue
        assert vals['_Online'].Value.Value is True and vals['_Health'].Value.Value == 'ok'
        # the device stops answering: the last value stays, marked so
        plant.live = False
        time.sleep(0.8)
        vals, _ = _run(_read(url, ['power_active_total', '_Online']))
        assert vals['power_active_total'].Value.Value == 1234.5
        assert vals['power_active_total'].StatusCode.value == ua.StatusCodes.UncertainNoCommunicationLastUsableValue
        assert vals['_Online'].Value.Value is False
        st = srv.status()
        assert st['running'] and st['devices'] == 1 and st['variables'] == 5
    finally:
        srv.stop()


DECLARED = [('power_active_total', 'Active power', 'W', 'Double'), ('mode', 'Mode', '', 'Double'),
            ('old', 'Old value', 'V', 'Double')]


class _Restarted:
    """The gateway just started: its devices are configured and declare what
    they will read; nothing is read yet."""
    def __init__(self, devices=('meter1',), chosen=None):
        self.store, self.ids, self.chosen = {}, devices, chosen

    def devices(self):
        for d in self.ids:
            live = bool(self.store) and d == 'meter1'          # online from its first reading
            yield d, 'Grid meter', live, 'ok' if live else 'waiting', self.store if d == 'meter1' else {}, {}, DECLARED, self.chosen


def _start(tmp_path, plant):
    srv = OpcUaServer(_cfg(), devices=plant.devices, authenticate=_auth, config_dir=tmp_path)
    assert srv.start(wait_s=START_WAIT_S), srv.error or 'not ready within START_WAIT_S'
    return srv


def _client(url):
    from asyncua import Client
    c = Client(url, timeout=5)
    c.set_user('scada')
    c.set_password('right-pass')
    return c


def _put_value(plant, name, value):
    plant.store[name] = {'name': name, 'label': name, 'unit': '', 'value': value,
                         'ts': time.time(), 'mono': time.monotonic(), 'interval': 1.0}


def _watch(url, name, change):
    """Subscribe to Devices/meter1/<name> while it waits, call change(), and
    return [(value, status)] the subscription received."""
    from asyncua import ua

    async def scenario():
        got = []

        class Handler:
            def datachange_notification(self, node, val, data):
                got.append((val, data.monitored_item.Value.StatusCode.value))

        async with _client(url) as c:
            idx = await c.get_namespace_index('urn:multi-bus-gateway')
            node = c.get_node(f"ns={idx};s=Devices/meter1/{name}")
            first = await node.read_data_value(raise_on_bad_status=False)
            assert first.StatusCode.value == ua.StatusCodes.BadWaitingForInitialData
            sub = await c.create_subscription(100, Handler())
            await sub.subscribe_data_change(node)
            change()
            for _ in range(40):
                if any(s == ua.StatusCodes.Good for _v, s in got):
                    break
                await asyncio.sleep(0.1)
            return got, await node.read_data_type_as_variant_type()

    return _run(scenario())


def test_every_declared_value_is_there_before_its_first_reading(tmp_path):
    """A SCADA reconnects the moment the gateway is back and subscribes to
    every node it knew. A node that is not there answers BadNodeIdUnknown and
    most clients (FUXA) never ask again: the tag would freeze. So what a
    device declares (its template, its calculated values) is there from the
    start, waiting, and the subscription gets the value when it is read."""
    from asyncua import ua
    plant = _Restarted()
    srv = _start(tmp_path, plant)
    try:
        url = srv.endpoint.replace('0.0.0.0', '127.0.0.1')

        async def look():
            async with _client(url) as c:
                idx = await c.get_namespace_index('urn:multi-bus-gateway')
                p = c.get_node(f"ns={idx};s=Devices/meter1/power_active_total")
                return (await p.read_data_type_as_variant_type(), (await p.read_display_name()).Text,
                        (await p.read_description()).Text)

        assert _run(look()) == (ua.VariantType.Double, 'Active power', 'Active power [W]')
        got, _t = _watch(url, 'power_active_total', lambda: _put_value(plant, 'power_active_total', 99.5))
        assert (99.5, ua.StatusCodes.Good) in got, got
    finally:
        srv.stop()


def test_a_value_declared_as_a_number_that_reads_as_text_changes_type_in_place(tmp_path):
    """An HTTP template that says nothing of a field is taken for a number;
    if it reads as text, the node takes the real type and keeps its id, so a
    subscription made before still gets it."""
    from asyncua import ua
    plant = _Restarted()
    srv = _start(tmp_path, plant)
    try:
        url = srv.endpoint.replace('0.0.0.0', '127.0.0.1')
        got, vtype = _watch(url, 'mode', lambda: _put_value(plant, 'mode', 'Running'))
        assert ('Running', ua.StatusCodes.Good) in got, got
        assert vtype == ua.VariantType.String
    finally:
        srv.stop()


def test_only_the_devices_and_values_chosen_for_opc_ua_are_served(tmp_path):
    plant = _Restarted(devices=('meter1', 'meter2'), chosen={'power_active_total'})
    srv = _start(tmp_path, plant)
    try:
        url = srv.endpoint.replace('0.0.0.0', '127.0.0.1')

        async def tree():
            async with _client(url) as c:
                idx = await c.get_namespace_index('urn:multi-bus-gateway')
                out = {}
                for d in await c.get_node(f"ns={idx};s=Devices").get_children():
                    out[(await d.read_browse_name()).Name] = sorted(
                        [(await v.read_browse_name()).Name for v in await d.get_children()])
                return out

        assert _run(tree()) == {'meter1': ['_Health', '_Online', 'power_active_total'],
                                'meter2': ['_Health', '_Online', 'power_active_total']}
        _put_value(plant, 'mode', 'Running')                 # read, but not chosen
        plant.ids = ('meter1',)                              # meter2 taken off OPC UA
        time.sleep(0.8)
        assert _run(tree()) == {'meter1': ['_Health', '_Online', 'power_active_total']}
        plant.chosen = None                                  # every value again
        time.sleep(0.8)
        assert _run(tree())['meter1'] == ['_Health', '_Online', 'mode', 'old', 'power_active_total']
    finally:
        srv.stop()


def test_a_wrong_password_and_anonymous_are_refused(tmp_path):
    srv = _server(tmp_path, _Plant())
    try:
        url = srv.endpoint.replace('0.0.0.0', '127.0.0.1')
        with pytest.raises(Exception):
            _run(_read(url, ['mode'], pw='nope'))
        with pytest.raises(Exception):
            _run(_read(url, ['mode'], user=None))
    finally:
        srv.stop()


def test_required_encryption_refuses_a_plain_client_and_serves_an_encrypted_one(tmp_path):
    from cryptography.x509.oid import ExtendedKeyUsageOID
    from asyncua.crypto.cert_gen import setup_self_signed_certificate
    srv = _server(tmp_path, _Plant(), encryption='required')
    try:
        url = srv.endpoint.replace('0.0.0.0', '127.0.0.1')
        assert (tmp_path / 'opcua' / 'server_cert.der').exists()
        assert oct((tmp_path / 'opcua' / 'server_key.pem').stat().st_mode)[-3:] == '600'
        with pytest.raises(Exception):
            _run(_read(url, ['mode']))                                       # no security: no endpoint for it
        key, cert = tmp_path / 'client_key.pem', tmp_path / 'client_cert.der'
        _run(setup_self_signed_certificate(key, cert, 'urn:test:client', 'localhost',
                                           [ExtendedKeyUsageOID.CLIENT_AUTH], {'commonName': 'test client'}))
        vals, _ = _run(_read(url, ['mode'], security=f"Basic256Sha256,SignAndEncrypt,{cert},{key}"))
        assert vals['mode'].Value.Value == 'Running'
    finally:
        srv.stop()


def test_behind_a_load_balancer_clients_are_told_its_address_and_the_certificate_names_it(tmp_path):
    from asyncua import Client
    from cryptography import x509
    public = 'opc.tcp://scada-gw.example:4999/plant'
    srv = _server(tmp_path, _Plant(), encryption='optional', public_url=public)
    try:
        assert srv.endpoint == public
        direct = f"opc.tcp://127.0.0.1:{srv.cfg.port}/mbg"                    # it still listens on its own port

        async def endpoints():
            c = Client(direct, timeout=5)
            return await c.connect_and_get_server_endpoints()
        # asyncua answers in the host:port the client used (through the
        # balancer: the balancer's), with the public path
        assert {e.EndpointUrl for e in _run(endpoints())} == {f"opc.tcp://127.0.0.1:{srv.cfg.port}/plant"}
        vals, _ = _run(_read(direct, ['mode']))
        assert vals['mode'].Value.Value == 'Running'
        cert = x509.load_der_x509_certificate((tmp_path / 'opcua' / 'server_cert.der').read_bytes())
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert 'scada-gw.example' in names.get_values_for_type(x509.DNSName)
    finally:
        srv.stop()


# ── through the API ──────────────────────────────────────────────────────────

def test_enabled_from_the_settings_it_starts_says_so_and_stops(tmp_path):
    from fastapi.testclient import TestClient

    from multibus import auth as _authmod
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    cfg.ui.auth_enabled, cfg.ui.auth_username = True, "admin"
    cfg.ui.auth_password = _authmod.hash_password("pw")
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
        st = client.get("/api/opcua").json()
        assert st['enabled'] is False and st['running'] is False                 # off by default
        assert client.put("/api/opcua", json={'port': 70000}).status_code == 422
        assert client.put("/api/opcua", json={'encryption': 'maybe'}).status_code == 422
        for bad in ('http://lb:4840', 'opc.tcp://', 'opc.tcp://lb lb:4840'):
            assert client.put("/api/opcua", json={'public_url': bad}).status_code == 422
        port = _port()
        st = client.put("/api/opcua", json={'enabled': True, 'port': port, 'host': '127.0.0.1',
                                            'encryption': 'required'}).json()
        assert st['running'] and st['endpoint'] == f"opc.tcp://127.0.0.1:{port}/mbg" and st['auth_enabled'], st
        assert cfg.opcua.enabled and 'opcua' in (tmp_path / 'config.yaml').read_text()
        r = client.get("/api/opcua/certificate")
        assert r.status_code == 200 and r.content[:1] == b'\x30'                 # a DER certificate
        st = client.put("/api/opcua", json={'public_url': 'opc.tcp://lb.lan:4840/mbg'}).json()
        assert st['public_url'] == 'opc.tcp://lb.lan:4840/mbg' and st['endpoint'] == 'opc.tcp://lb.lan:4840/mbg'
        assert 'public_url: opc.tcp://lb.lan:4840/mbg' in (tmp_path / 'config.yaml').read_text()
        assert client.put("/api/opcua", json={'enabled': False}).json()['running'] is False
        assert _port_frees(port), f"port {port} still held after stop"            # the port is free again


# ── commands: set points and methods ────────────────────────────────────────

def _auth_roles(user, pw):
    return {('op', 'op-pass'): 'operator', ('scada', 'right-pass'): 'viewer'}.get((user, pw))


class _CmdPlant(_Plant):
    COMMANDS = {'run_at': {'label': 'Run at a frequency', 'params': {
        'value': {'label': 'Frequency', 'unit': 'Hz', 'min': 0, 'max': 50, 'default': None, 'required': True}}},
        'power_limit': {'label': 'Power limit', 'params': {
            'value': {'label': 'Limit', 'unit': '%', 'min': 0, 'max': 100, 'default': None, 'required': True},
            'revert_s': {'label': 'Reverts after', 'unit': 's', 'min': 0, 'max': 65535, 'default': 600, 'required': False}}}}

    def __init__(self):
        super().__init__()
        self.calls, self.answer = [], (200, {'status': 'success'})

    def devices(self):
        yield 'meter1', 'Grid meter', self.live, 'ok', self.store, self.COMMANDS

    def run(self, dev, name, params, user):
        self.calls.append((dev, name, params, user))
        return self.answer


def _cmd_server(tmp_path, plant, **kw):
    srv = OpcUaServer(_cfg(**kw), devices=plant.devices, authenticate=_auth_roles, config_dir=tmp_path,
                      run_command=plant.run)
    assert srv.start(wait_s=START_WAIT_S), srv.error or 'not ready within START_WAIT_S'
    deadline = time.time() + 10
    while srv.status()['commands'] < 2 and time.time() < deadline:
        time.sleep(0.1)
    return srv


async def _session(url, user, pw, fn):
    from asyncua import Client
    c = Client(url, timeout=10)
    if user:
        c.set_user(user)
        c.set_password(pw)
    async with c:
        idx = await c.get_namespace_index('urn:multi-bus-gateway')
        return await fn(c, idx)


def test_an_operator_writes_a_set_point_and_it_runs_the_command(tmp_path):
    from asyncua import ua
    plant = _CmdPlant()
    srv = _cmd_server(tmp_path, plant)
    url = srv.endpoint.replace('0.0.0.0', '127.0.0.1')

    async def write(c, idx, value, vt=ua.VariantType.Double):
        node = c.get_node(f"ns={idx};s=Devices/meter1/Commands/run_at/value")
        try:
            await node.write_value(ua.DataValue(ua.Variant(value, vt)))
            code = 'Good'
        except ua.UaStatusCodeError as e:
            code = type(e).__name__
        back = await node.read_data_value(raise_on_bad_status=False)
        result = await c.get_node(f"ns={idx};s=Devices/meter1/Commands/run_at/LastResult").read_value()
        return code, back.Value.Value, result
    try:
        code, back, result = _run(_session(url, 'op', 'op-pass', lambda c, i: write(c, i, 25.0)))
        assert code == 'Good' and back == 25.0 and result.startswith('success')
        assert plant.calls == [('meter1', 'run_at', {'value': 25.0}, 'op')]
        # a viewer reads everything, commands nothing
        code, _b, result = _run(_session(url, 'scada', 'right-pass', lambda c, i: write(c, i, 30.0)))
        assert code == 'BadUserAccessDenied' and 'may not run commands' in result and len(plant.calls) == 1
        # the gateway refuses (an armed rule owns the drive): the reason is in LastResult
        plant.answer = (409, {'status': 'rejected', 'reason': "owned by rule 'day'"})
        code, back, result = _run(_session(url, 'op', 'op-pass', lambda c, i: write(c, i, 40.0)))
        assert code == 'BadInvalidState' and "owned by rule 'day'" in result and back == 25.0   # unchanged
        # text where a number belongs
        code, _b, _r = _run(_session(url, 'op', 'op-pass', lambda c, i: write(c, i, 'fast', ua.VariantType.String)))
        assert code in ('BadTypeMismatch', 'BadUserAccessDenied') and len(plant.calls) == 2
    finally:
        srv.stop()


def test_a_method_takes_every_parameter_and_measured_values_stay_read_only(tmp_path):
    from asyncua import ua
    plant = _CmdPlant()
    srv = _cmd_server(tmp_path, plant)
    url = srv.endpoint.replace('0.0.0.0', '127.0.0.1')

    async def call(c, idx):
        obj = c.get_node(f"ns={idx};s=Devices/meter1/Commands/power_limit")
        out = await obj.call_method(ua.NodeId("Devices/meter1/Commands/power_limit/Run", idx),
                                    ua.Variant(60.0, ua.VariantType.Double), ua.Variant(300.0, ua.VariantType.Double))
        args = await c.get_node(f"ns={idx};s=Devices/meter1/Commands/power_limit/Run").get_child(
            ["0:InputArguments"])
        names = [a.Name for a in await args.read_value()]
        measured = c.get_node(f"ns={idx};s=Devices/meter1/power_active_total")
        try:
            await measured.write_value(ua.DataValue(ua.Variant(1.0, ua.VariantType.Double)))
            ro = 'written'
        except ua.UaStatusCodeError as e:
            ro = type(e).__name__
        return out, names, ro
    try:
        out, names, ro = _run(_session(url, 'op', 'op-pass', call))
        assert out.startswith('success') and names == ['value', 'revert_s']
        assert plant.calls == [('meter1', 'power_limit', {'value': 60.0, 'revert_s': 300.0}, 'op')]
        assert ro == 'BadUserAccessDenied'                                   # a reading is never written
    finally:
        srv.stop()


def test_from_opc_ua_to_the_drive_through_the_gateways_command_path(tmp_path):
    """End to end: an operator's OPC UA write lands on the drive; with an
    armed rule on the channel it is refused, and says why."""
    from asyncua import ua

    from tests.test_rules_arbiter import ALWAYS, _drive_app, _freqs, _rule
    app, client, rt, drive, ck = _drive_app(tmp_path)
    ctl = app.state.opcua
    assert ctl.status()['enabled'] is False
    port = _port()
    r = client.put("/api/opcua", json={'enabled': True, 'port': port, 'host': '127.0.0.1', 'encryption': 'off'})
    assert r.status_code == 200 and r.json()['running'], r.text
    try:
        deadline = time.time() + 10
        while ctl.status().get('commands', 0) < 1 and time.time() < deadline:
            time.sleep(0.1)
        url = f"opc.tcp://127.0.0.1:{port}/mbg"

        async def write(c, idx, v):
            node = c.get_node(f"ns={idx};s=Devices/vfd1/Commands/run_at/value")
            try:
                await node.write_value(ua.DataValue(ua.Variant(v, ua.VariantType.Double)))
                code = 'Good'
            except ua.UaStatusCodeError as e:
                code = type(e).__name__
            return code, await c.get_node(f"ns={idx};s=Devices/vfd1/Commands/run_at/LastResult").read_value()
        code, result = _run(_session(url, 'admin', 'pw', lambda c, i: write(c, i, 25.0)))
        assert code == 'Good' and _freqs(drive)[-1] == 25, result
        assert client.post("/api/rules", json=_rule('day', 'schedule', ALWAYS, 30)).status_code == 200
        rt.tick()
        code, result = _run(_session(url, 'admin', 'pw', lambda c, i: write(c, i, 40.0)))
        assert code == 'BadInvalidState' and "owned by rule 'day'" in result
        lines = (tmp_path / 'audit.jsonl').read_text().splitlines() if (tmp_path / 'audit.jsonl').exists() else []
        assert any('"opcua:admin"' in ln and '"command"' in ln for ln in lines), lines[-3:]   # audited as the OPC UA user
    finally:
        client.put("/api/opcua", json={'enabled': False})


def test_a_service_token_logs_in_under_its_own_name_with_its_role(tmp_path):
    """A SCADA is a program: it signs in with a service token (user = the
    token's name, password = the token), never with a person's password."""
    from fastapi.testclient import TestClient

    from multibus import auth as _authmod
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    cfg.ui.auth_enabled, cfg.ui.auth_username = True, "admin"
    cfg.ui.auth_password = _authmod.hash_password("pw")
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
        tok = client.post("/api/auth/service-tokens", json={'name': 'fuxa', 'role': 'viewer'}).json()['token']
        port = _port()
        assert client.put("/api/opcua", json={'enabled': True, 'port': port, 'host': '127.0.0.1',
                                              'encryption': 'off'}).json()['running']
        try:
            url = f"opc.tcp://127.0.0.1:{port}/mbg"

            async def browse(c, idx):
                return len(await c.get_node(f"ns={idx};s=Devices").get_children())
            assert _run(_session(url, 'fuxa', tok, browse)) >= 1                   # in, as fuxa
            with pytest.raises(Exception):
                _run(_session(url, 'someone-else', tok, browse))                   # the token is fuxa's only
            client.delete("/api/auth/service-tokens/fuxa")
            with pytest.raises(Exception):
                _run(_session(url, 'fuxa', tok, browse))                           # revoked: out
        finally:
            client.put("/api/opcua", json={'enabled': False})


class _WithAttributes(_Restarted):
    """A device that carries asset attributes (docs/attributes.md)."""
    def __init__(self):
        super().__init__(devices=('meter1',), chosen={'power_active_total'})
        self.attrs = {'grid_connection.max_export_kw': 45, 'grid_connection.role': 'connection_point',
                      'pv_input.2.tilt_deg': 40.0}

    def devices(self):
        for item in super().devices():
            yield (*item, self.attrs)


def test_asset_attributes_are_served_and_follow_their_edits(tmp_path):
    plant = _WithAttributes()
    srv = _start(tmp_path, plant)
    try:
        url = srv.endpoint.replace('0.0.0.0', '127.0.0.1')

        async def attrs():
            async with _client(url) as c:
                idx = await c.get_namespace_index('urn:multi-bus-gateway')
                out = {}
                for v in await c.get_node(f"ns={idx};s=Devices/meter1").get_children():
                    name = (await v.read_browse_name()).Name
                    if name.startswith('Attributes.'):
                        out[name] = await v.read_value()
                return out

        deadline = time.time() + 5
        got = {}
        while time.time() < deadline and len(got) < 3:
            got = _run(attrs())
            time.sleep(0.2)
        assert got == {'Attributes.grid_connection.max_export_kw': 45.0,
                       'Attributes.grid_connection.role': 'connection_point',
                       'Attributes.pv_input.2.tilt_deg': 40.0}
        # served whatever values are chosen; an edit is followed, a removal too
        plant.attrs = {'grid_connection.max_export_kw': 50}
        time.sleep(0.8)
        assert _run(attrs()) == {'Attributes.grid_connection.max_export_kw': 50.0}
    finally:
        srv.stop()
