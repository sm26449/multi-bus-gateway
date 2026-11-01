# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The read-only connection spec (ADR-0026 §1, FINDINGS ADD-3): every stored
shape reads out on its axes and back exactly; the matrix is what the API
accepts today; a spec sent back is never stored."""
import copy
import itertools
import json
import pathlib

import pytest
import yaml

from tests.test_devices_api import make_app, needs_tc

FIX = pathlib.Path(__file__).parent / "fixtures"
SHAPES = yaml.safe_load((FIX / "connform_shapes.yaml").read_text())

BRIDGES = [
    {'id': 'ws', 'type': 'rtu_transparent', 'host': '192.0.2.60', 'ports': [{'port': 4196}, {'port': 4197}]},
    {'id': 'gw', 'type': 'modbus_gateway', 'host': '192.0.2.61', 'ports': [{'port': 502}]},
]


def _spec(conn, bridges=BRIDGES):
    from multibus.connection_spec import spec_of
    return spec_of(conn, bridges)


def _axes(s):
    return tuple(s[k] for k in ('port', 'reach', 'protocol', 'framing', 'udp', 'mode'))


# design rev 2 §2.1, row by row: stored shape → (port, reach, protocol, framing, udp, mode)
ROWS = [
    ({'host': '192.0.2.1', 'unit_id': 1}, ('ethernet', 'address', 'modbus', 'mbap', False, 'poll')),
    ({'protocol': 'tcp', 'host': 'h'}, ('ethernet', 'address', 'modbus', 'mbap', False, 'poll')),
    ({'protocol': 'udp', 'host': 'h'}, ('ethernet', 'address', 'modbus', 'mbap', True, 'poll')),
    ({'protocol': 'rtu', 'serial_port': '/dev/ttyUSB0'}, ('serial', 'here', 'modbus', 'rtu', False, 'poll')),
    ({'protocol': 'ascii', 'serial_port': '/dev/ttyUSB0'}, ('serial', 'here', 'modbus', 'ascii', False, 'poll')),
    ({'protocol': 'rtu-tcp', 'host': 'h', 'port': 4001}, ('serial', 'address', 'modbus', 'rtu', False, 'poll')),
    ({'protocol': 'ascii-tcp', 'host': 'h', 'port': 4001}, ('serial', 'address', 'modbus', 'ascii', False, 'poll')),
    ({'protocol': 'rtu_tap', 'serial_port': '/dev/ttyS1'}, ('serial', 'here', 'modbus', 'rtu', False, 'listen')),
    ({'protocol': 'rtu_tap', 'host': 'h', 'port': 4196}, ('serial', 'address', 'modbus', 'rtu', False, 'listen')),
    ({'bridge': 'ws', 'bridge_port': 4196, 'unit_id': 2}, ('serial', 'bridge', 'modbus', 'rtu', False, 'poll')),
    ({'bridge': 'gw', 'unit_id': 4}, ('ethernet', 'bridge', 'modbus', 'mbap', False, 'poll')),
    ({'protocol': 'rtu_tap', 'bridge': 'ws', 'bridge_port': 4197}, ('serial', 'bridge', 'modbus', 'rtu', False, 'listen')),
    ({'protocol': 'seplos_v2', 'serial_port': '/dev/ttyBMS0'}, ('serial', 'here', 'seplos_v2', None, False, 'poll')),
    ({'protocol': 'seplos_v2', 'serial_port': '/dev/ttyBMS0', 'mode': 'listen'},
     ('serial', 'here', 'seplos_v2', None, False, 'listen')),
    ({'protocol': 'seplos_v2-tcp', 'host': 'h', 'port': 4198, 'mode': 'poll'},
     ('serial', 'address', 'seplos_v2', None, False, 'poll')),
    ({'protocol': 'can', 'channel': 'can0'}, ('can', 'socketcan', 'can_signals', None, False, 'listen')),
    ({'protocol': 'http', 'url': 'http://x'}, ('http', None, 'json_http', None, False, 'poll')),
    ({'protocol': 'mqtt', 'broker': 'b', 'topic': 't/#'}, ('mqtt', None, 'json_mqtt', None, False, 'listen')),
]


@pytest.mark.parametrize("conn,want", ROWS, ids=[json.dumps(r[0], sort_keys=True) for r in ROWS])
def test_every_row_of_the_design_table_reads_out_on_its_axes(conn, want):
    s = _spec(conn)
    assert _axes(s) == want
    assert s['dangling'] is False


def test_writable_is_derived_from_protocol_and_mode():
    w = {json.dumps(c, sort_keys=True): _spec(c)['writable'] for c, _w in ROWS}
    assert w[json.dumps({'protocol': 'tcp', 'host': 'h'}, sort_keys=True)] is True
    assert w[json.dumps({'protocol': 'rtu_tap', 'serial_port': '/dev/ttyS1'}, sort_keys=True)] is False
    assert _spec({'protocol': 'seplos_v2', 'serial_port': '/dev/x', 'mode': 'poll'})['writable'] is False
    assert _spec({'protocol': 'can', 'channel': 'can0'})['writable'] is False
    assert _spec({'protocol': 'http', 'url': 'http://x'})['writable'] is False
    assert _spec({'protocol': 'mqtt', 'broker': 'b', 'topic': 't'})['writable'] is True     # commands


def test_a_dangling_bridge_keeps_its_stored_keys_and_says_so():
    from multibus.connection_spec import conn_of
    for conn in ({'bridge': 'gone', 'bridge_port': 7001, 'unit_id': 3},
                 {'bridge': 'gone', 'protocol': 'rtu_tap', 'unit_id': 3}):
        s = _spec(conn)
        assert s['dangling'] is True and s['reach'] == 'bridge'
        assert s['mode'] == ('listen' if conn.get('protocol') == 'rtu_tap' else 'poll')
        assert conn_of(s) == conn


def test_a_tap_on_a_gateway_bridge_stays_listening_and_is_off_the_matrix():
    from multibus.connection_spec import matrix_cell
    s = _spec({'protocol': 'rtu_tap', 'bridge': 'gw'})
    assert s['mode'] == 'listen' and s['framing'] == 'mbap'       # ADD-7: never turned into a poll
    assert matrix_cell('device', s) is None


def test_meter_only_is_not_a_connection():
    from multibus.connection_spec import conn_of, public
    s = _spec({'protocol': 'none'})
    assert public(s) == {'port': None, 'reach': None, 'protocol': None, 'framing': None, 'udp': False,
                         'mode': None, 'writable': False, 'dangling': False}
    assert conn_of(s) == {'protocol': 'none'}


def _generated_shapes():
    """A generated set of stored shapes: every protocol string (and none),
    with and without each key the axes read, a bridge that exists, one that
    does not, odd spellings a hand edit leaves."""
    protos = [None, 'tcp', 'udp', 'rtu', 'ascii', 'rtu-tcp', 'ascii-tcp', 'rtu_tap', 'seplos_v2',
              'seplos_v2-tcp', 'can', 'http', 'mqtt', 'none', 'TCP', 'Rtu_Tap', 'bogus', '']
    bridges = [None, 'ws', 'gw', 'gone']
    extras = [{}, {'serial_port': '/dev/ttyUSB0'}, {'host': '192.0.2.5', 'port': 4196},
              {'mode': 'listen'}, {'mode': 'poll'}, {'mode': ''}, {'mode': 'Listen'},
              {'carrier': 'socketcan', 'channel': 'can0'}, {'carrier': 'bridge'},
              {'unit_id': 7, 'timeout': 2, 'max_gap': 5}]
    for p, b, e in itertools.product(protos, bridges, extras):
        c = dict(e)
        if p is not None:
            c['protocol'] = p
        if b is not None:
            c['bridge'] = b
            c['bridge_port'] = 4197
        yield c


def test_every_stored_shape_round_trips():
    from multibus.connection_spec import conn_of, spec_of
    shapes = list(_generated_shapes())
    shapes += [d['connection'] for d in SHAPES['devices']]
    shapes += [c for c, _w in ROWS]
    assert len(shapes) > 700
    for conn in shapes:
        before = copy.deepcopy(conn)
        s = spec_of(conn, BRIDGES)
        assert conn == before                          # read only
        assert conn_of(s) == conn, (conn, s)
        # and through JSON (what the API and a client see of it)
        assert conn_of(json.loads(json.dumps(s))) == conn


def test_a_form_built_spec_gives_the_legacy_stub():
    from multibus.connection_spec import conn_of
    base = {'udp': False, 'writable': True, 'dangling': False}
    assert conn_of({**base, 'port': 'serial', 'reach': 'address', 'protocol': 'modbus', 'framing': 'ascii',
                    'mode': 'poll'}) == {'protocol': 'ascii-tcp'}
    assert conn_of({**base, 'port': 'serial', 'reach': 'here', 'protocol': 'seplos_v2', 'framing': None,
                    'mode': 'listen'}) == {'protocol': 'seplos_v2', 'mode': 'listen'}
    assert conn_of({**base, 'port': 'serial', 'reach': 'bridge', 'protocol': 'modbus', 'framing': 'rtu',
                    'mode': 'listen', 'params': {'bridge': 'ws'}}) == {'protocol': 'rtu_tap', 'bridge': 'ws'}


def test_multi_source_devices_get_a_spec_per_source():
    from multibus.connection_spec import device_specs
    raw = {'id': 'pv', 'template': 'x', 'sources': [
        {'id': 'api', 'protocol': 'http', 'url': 'http://192.0.2.2/x'},
        {'id': 'mb', 'protocol': 'tcp', 'host': '192.0.2.2', 'unit_id': 1}]}
    dev, per = device_specs(raw, BRIDGES)
    assert dev['protocol'] == 'json_http'                  # the first source's, as dev.protocol is
    assert [(p['id'], p['protocol']) for p in per] == [('api', 'json_http'), ('mb', 'modbus')]
    dev1, per1 = device_specs({'id': 'one', 'connection': {'protocol': 'rtu', 'serial_port': '/dev/x'}}, BRIDGES)
    assert dev1['framing'] == 'rtu' and per1 == []


def test_template_speaks_is_derived_from_transports(tmp_path):
    from multibus.connection_spec import template_speaks
    from multibus.device_template import TemplateRegistry
    reg = TemplateRegistry(user_dir=tmp_path)
    want = {'eastron_sdm630': ('modbus', False), 'seplos_bms_v3_rtu_tap': ('modbus', True),
            'seplos_bms_v2': ('seplos_v2', None), 'fronius_solar_api_inverter': ('json_http', False),
            'victron_gx_mqtt': ('json_mqtt', False)}
    for tid, (proto, listen) in want.items():
        tpl = reg.get(tid)
        assert tpl is not None, tid
        got = template_speaks(tpl)
        assert got['protocol'] == proto, tid
        if listen is not None:
            assert got['listen_only'] is listen, tid


# ── the matrix ───────────────────────────────────────────────────────────
def test_the_matrix_lists_every_stored_string_each_context_accepts():
    from multibus.api import PROTOCOLS_BY_CONTEXT
    from multibus.connection_spec import MATRIX
    for ctx in ('device', 'source', 'endpoint', 'group'):
        cells = MATRIX[ctx]
        assert cells, ctx
        today = {c['connection'].get('protocol') for c in cells if c['status'] == 'today'}
        today.discard(None)
        assert today == set(PROTOCOLS_BY_CONTEXT[ctx]), ctx
        for c in cells:
            assert c['status'] in ('today', 'new', 'not_yet')


def _payload_for(cell, i):
    """A device payload in that cell, with the address its connection needs."""
    conn = dict(cell['connection'])
    proto = conn.get('protocol')
    if cell['port'] == 'can':
        pass                                     # a CAN carrier is not a bridge record
    elif cell['reach'] == 'bridge':
        conn['bridge'] = {'mbap': 'gw', 'ascii': 'asc'}.get(cell['framing'], 'ws')
    elif cell['reach'] == 'here' and cell['port'] == 'serial':
        conn.update(serial_port=f"/dev/ttyMX{i}")
    elif cell['reach'] == 'address':
        conn.update(host=f"192.0.2.{100 + i}", port=4000 + i)
    if proto == 'can' or cell['port'] == 'can':
        conn.setdefault('channel', 'can0')
    if proto == 'http':
        conn['url'] = f"http://192.0.2.{100 + i}/x"
    if proto == 'mqtt':
        conn.update(broker='192.0.2.10', topic=f"x{i}/#")
    conn.setdefault('unit_id', 1)
    return {'id': f"cell-{i}", 'name': f"cell {i}", 'enabled': False, 'connection': conn}


@needs_tc
def test_the_device_matrix_is_what_the_api_accepts_today(tmp_path, monkeypatch):
    from multibus import bridges
    from multibus.connection_spec import MATRIX, spec_of
    types = dict(bridges.TYPES)
    types['ascii_transparent'] = dict(types['rtu_transparent'], id='ascii_transparent', framing='ascii')
    monkeypatch.setattr(bridges, 'TYPES', types)
    cfg, client = make_app(tmp_path, extra_yaml="""
bridges:
  - {id: ws, type: rtu_transparent, host: 192.0.2.60, ports: [{port: 4196}]}
  - {id: gw, type: modbus_gateway, host: 192.0.2.61, ports: [{port: 502}]}
  - {id: asc, type: ascii_transparent, host: 192.0.2.62, ports: [{port: 4196}]}
""")
    for i, cell in enumerate(MATRIX['device']):
        body = _payload_for(cell, i)
        r = client.post("/api/devices?dry_run=true", json=body)
        if cell['status'] == 'today':
            assert r.status_code == 200, (cell, r.text)
            got = spec_of(body['connection'], cfg.bridges, types)
            assert all(got[k] == cell[k] for k in ('port', 'reach', 'protocol', 'framing', 'udp', 'mode')), cell
        else:
            assert r.status_code == 422, (cell, r.text)        # never widened


@needs_tc
def test_api_protocols_adds_the_matrix_and_the_labels_and_keeps_its_keys(tmp_path):
    _cfg, client = make_app(tmp_path)
    got = client.get("/api/protocols").json()
    for k in ('device', 'source', 'endpoint', 'group', 'meter_only', 'serial', 'network'):
        assert k in got, k
    assert set(got['matrix']) == {'device', 'source', 'endpoint', 'group'}
    assert {'port', 'reach', 'protocol', 'framing', 'mode'} <= set(got['labels'])
    cell = got['matrix']['device'][0]
    assert {'port', 'reach', 'protocol', 'framing', 'udp', 'mode', 'writable', 'status', 'connection'} <= set(cell)


# ── the API ──────────────────────────────────────────────────────────────
@needs_tc
def test_get_devices_carries_a_spec_per_device_and_per_source(tmp_path):
    extra = (FIX / "connform_shapes.yaml").read_text() + """
  - id: two-ways
    name: Read two ways
    template: eastron_sdm630
    enabled: false
    sources:
      - {id: api, protocol: http, url: "http://192.0.2.44/x", template: fronius_solar_api_inverter}
      - {id: mb, protocol: tcp, host: 192.0.2.44, unit_id: 1}
"""
    _cfg, client = make_app(tmp_path, extra_yaml=extra)
    by = {d['id']: d for d in client.get("/api/devices").json()['devices']}
    assert by['umg512']['connection_spec']['framing'] == 'mbap'
    assert by['on-bridge']['connection_spec']['reach'] == 'bridge'
    assert by['tap-bridge']['connection_spec']['mode'] == 'listen'
    assert by['seplos-net']['connection_spec']['mode'] == 'poll'          # mode not stored = poll
    assert 'params' not in by['tcp']['connection_spec']                   # the axes only
    assert 'spec' not in by['tcp'].get('connection', {})                  # never inside the connection
    assert [s['id'] for s in by['two-ways']['source_specs']] == ['api', 'mb']
    assert by['two-ways']['connection_spec']['protocol'] == 'json_http'
    assert 'source_specs' not in by['tcp']
    one = client.get("/api/devices/on-bridge")
    assert one.status_code == 200 and one.json()['device']['connection_spec'] == by['on-bridge']['connection_spec']
    assert client.get("/api/devices/nope").status_code == 404
    # the static routes beside it still answer
    assert client.get("/api/devices/restorable").status_code == 200


@needs_tc
def test_an_installation_unit_carries_its_spec(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml="""
endpoints:
  - id: plant
    name: Plant
    template: eastron_sdm630
    enabled: false
    connection: {protocol: rtu-tcp, host: 192.0.2.70, port: 4001}
    units: [{unit_id: 1}, {unit_id: 2}]
""")
    units = [d for d in client.get("/api/devices").json()['devices'] if d.get('endpoint_id') == 'plant']
    assert len(units) == 2
    assert all(u['connection_spec']['reach'] == 'address' and u['connection_spec']['framing'] == 'rtu'
               for u in units)


@needs_tc
def test_a_spec_sent_back_is_never_stored(tmp_path):
    cfg, client = make_app(tmp_path)
    spec = {'port': 'serial', 'reach': 'here', 'protocol': 'modbus'}
    body = {'id': 'echo', 'name': 'Echo', 'enabled': False, 'connection_spec': spec,
            'connection': {'protocol': 'tcp', 'host': '192.0.2.33', 'port': 502, 'unit_id': 1, 'spec': spec}}
    r = client.post("/api/devices", json=body)
    assert r.status_code == 200, r.text
    text = pathlib.Path(cfg.config_path).read_text()
    assert 'connection_spec' not in text and 'spec:' not in text
    raw = cfg.get_raw_device('echo')
    assert 'spec' not in raw['connection'] and 'connection_spec' not in raw
    # the whole GET entry echoed back changes nothing
    entry = client.get("/api/devices/echo").json()['device']
    before = pathlib.Path(cfg.config_path).read_bytes()
    put = dict(body, connection=dict(body['connection']), connection_spec=entry['connection_spec'])
    r = client.put("/api/devices/echo", json=put)
    assert r.status_code == 200 and r.json()['status'] == 'unchanged', r.text
    assert pathlib.Path(cfg.config_path).read_bytes() == before
