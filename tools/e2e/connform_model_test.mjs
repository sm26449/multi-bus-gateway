// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
//
// The connection form's model (ui/js/connform-model.js), with no browser:
// every way a device is reached, opened from the rows the server serves
// (tests/fixtures/connform_rows.json, kept current by
// tests/test_connform_payloads.py), saved untouched and with one field
// edited. What it would send is kept in tests/fixtures/connform_payloads.json,
// which the pytest then sends to the server.
//
//   node tools/e2e/connform_model_test.mjs           # check
//   node tools/e2e/connform_model_test.mjs --write   # after a deliberate change
import { createRequire } from 'module';
import { readFileSync, writeFileSync } from 'fs';
const require = createRequire(import.meta.url);
const CM = require('../../ui/js/connform-model.js');
const FIX = new URL('../../tests/fixtures/', import.meta.url);
const rows = JSON.parse(readFileSync(new URL('connform_rows.json', FIX)));

let failed = 0, n = 0;
const check = (name, cond, extra = '') => {
  n++; if (!cond) failed++;
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const edit = (row, changes) => {
  const M = CM.model(row);
  const shown = CM.display(M);
  const M2 = CM.apply(M, Object.assign({}, shown, changes), shown);
  return CM.payload(M2, M, { context: 'edit', primary: !!row.primary });
};

const out = { untouched: {}, edits: [], wire: {}, bodies: {} };
// the whole PUT the device page sends (its own fields beside the connection)
const body = (id, conn) => CM.pagePayload(id, Object.assign(CM.pageFields(rows[id]), { protocol: rows[id].protocol }), conn);
const push = (id, label, payload, extra = {}) =>
  out.edits.push(Object.assign({ id, label, payload, body: body(id, payload) }, extra));

// ── an untouched form sends nothing, for every way a device is reached ──
for (const [id, row] of Object.entries(rows)) {
  const M = CM.model(row);
  const shown = CM.display(M);
  const M2 = CM.apply(M, shown, shown);
  const p = CM.payload(M2, M, { context: 'edit', primary: !!row.primary });
  check(`${id}: untouched sends nothing`, eq(p, {}), p);
  check(`${id}: not dirty`, !CM.dirty(M2, M));
  check(`${id}: no local error on what is stored`, !Object.keys(CM.errors(M)).length, CM.errors(M));
  out.untouched[id] = p;
  out.bodies[id] = body(id, p);
  out.wire[id] = CM.wire(M);
}

// ── the mode each stored protocol opens as ──
const mode = id => CM.model(rows[id]);
check('udp opens as Modbus TCP over UDP', mode('udp').mode === 'tcp' && mode('udp').udp === true);
check('ascii opens as direct serial, ASCII framing', mode('ascii').mode === 'rtu' && mode('ascii').framing === 'ascii');
check('ascii-tcp opens as RTU over the network, ASCII', mode('ascii-tcp').mode === 'rtu-tcp' && mode('ascii-tcp').framing === 'ascii');
check('a gateway bus opens as Modbus TCP on that bus', mode('via-gw').mode === 'tcp' && mode('via-gw').bridge === 'gw');
check('a bridged device has no address of its own', mode('on-bridge').host === '' && mode('on-bridge').port === null);
check('the local tap keeps its serial path', mode('tap-local').serial_port === '/dev/ttySEPLOS');

// ── what the whole connection says, mode by mode ──
check('wire: a bridged device names the bus, not an address nor a line',
      eq(Object.keys(out.wire['on-bridge']).sort(), ['bridge', 'bridge_port', 'timeout', 'unit_id']), out.wire['on-bridge']);
check('wire: a tap through a bridge says it is a tap, with no line',
      eq(out.wire['tap-bridge'], { protocol: 'rtu_tap', bridge: 'ws', bridge_port: 4197, unit_id: 9 }), out.wire['tap-bridge']);
check('wire: a local tap has its serial port and no host',
      out.wire['tap-local'].serial_port === '/dev/ttySEPLOS' && !('host' in out.wire['tap-local']), out.wire['tap-local']);
check('wire: ASCII over the network is ascii-tcp', out.wire['ascii-tcp'].protocol === 'ascii-tcp');
check('wire: MQTT carries vars, the certificate choice and its own keepalive',
      eq(out.wire.gx.vars, { portal: 'c0ffee000001', vebus: '274' }) && out.wire.gx.tls_insecure === true
      && eq(out.wire.gx.keepalive, { every_s: 30, payload: '', topic: 'R/{portal}/keepalive' }), out.wire.gx);
check('wire: a stored password is never sent back', out.wire.gx.password === '');

// ── one edited field sends that field (and the protocol it rides on) ──
const cases = [
  ['tcp', 'port', { port: '1502' }, { port: 1502, protocol: 'tcp' }],
  ['udp', 'unit', { unit_id: '9' }, { unit_id: 9, protocol: 'udp' }],
  ['via-gw', 'unit on the gateway bus', { unit_id: '5' }, { unit_id: 5 }],
  ['rtu', 'baud', { baudrate: '9600' }, { baudrate: 9600, protocol: 'rtu' }],
  ['ascii', 'timeout', { timeout: '6' }, { timeout: 6, protocol: 'ascii' }],
  ['on-bridge', 'unit on the bridge', { unit_id: '3' }, { unit_id: 3 }],
  ['rtu-tcp', 'port', { port: '4011' }, { port: 4011, protocol: 'rtu-tcp' }],
  ['ascii-tcp', 'unit', { unit_id: '8' }, { unit_id: 8, protocol: 'ascii-tcp' }],
  ['tap-local', 'baud', { baudrate: '9600' }, { baudrate: 9600, protocol: 'rtu_tap' }],
  ['tap-bridge', 'unit', { unit_id: '10' }, { unit_id: 10, protocol: 'rtu_tap' }],
  ['http', 'url', { url: 'http://192.0.2.40/solar_api/v1/GetInverterRealtimeData.cgi?DeviceId=2' },
    { url: 'http://192.0.2.40/solar_api/v1/GetInverterRealtimeData.cgi?DeviceId=2', protocol: 'http' }],
  ['gx', 'a topic variable', { vars: 'portal=c0ffee000001, vebus=274, extra=1' },
    { vars: { portal: 'c0ffee000001', vebus: '274', extra: '1' }, protocol: 'mqtt' }],
  ['gx-plain', 'TLS on', { tls: true }, { tls: true, protocol: 'mqtt' }],
  ['mqtt-off', 'keepalive back to the template', { ka_mode: 'template' }, { keepalive: null, protocol: 'mqtt' }],
  ['umg512', 'the primary timeout', { timeout: '5' }, { protocol: 'tcp', host: '192.168.1.207', port: 502, unit_id: 1, timeout: 5 }],
];
for (const [id, label, changes, want] of cases) {
  const p = edit(rows[id], changes);
  check(`${id}: ${label} → only that`, eq(p, want), p);
  push(id, label, p);
}

// a custom keepalive stored without a first payload stays as stored while
// untouched (the form shows the payload in that field — showing is not saying)
check('gx: an untouched keepalive form keeps the stored object', eq(edit(rows.gx, {}), {}));
check('gx: a typed password is sent; blank keeps it',
      edit(rows.gx, { password: 'new' }).password === 'new' && !('password' in edit(rows.gx, { tls: false })));
check('vars: pairs by commas; a comma inside a value survives',
      eq(CM.text2vars('a=1, b=x,y'), { a: '1', b: 'x,y' }) && eq(CM.text2vars('a=1, b=2'), { a: '1', b: '2' })
      && eq(CM.text2vars('a=1\nb=2'), { a: '1', b: '2' }) && eq(CM.text2vars('q=x=y, r=1'), { q: 'x=y', r: '1' }));

// ── a change of how it is reached: the whole connection, and a bridge left is said ──
{
  const p = edit(rows['on-bridge'], { mode: 'rtu' });
  // leaving the bridge for direct serial: the form then shows the line; here
  // only the mode moved, so the serial port is the empty one the form shows
  check('on-bridge → direct serial says bridge: null', p.bridge === null && p.protocol === 'rtu', p);
}
{
  const row = rows['on-bridge'];
  const M = CM.model(row);
  const shown = CM.display(M);
  const M2 = CM.apply(Object.assign(CM.apply(M, Object.assign({}, shown, { mode: 'rtu' }), shown)), {}, null);
  Object.assign(M2, { serial_port: '/dev/ttyUSB7' });
  const p = CM.payload(M2, M, { context: 'edit' });
  check('on-bridge → direct serial on /dev/ttyUSB7: the whole line, no host',
        p.bridge === null && p.serial_port === '/dev/ttyUSB7' && !('host' in p) && p.protocol === 'rtu', p);
  push('on-bridge', 'off the bridge, onto direct serial', p,
       { stored_has: { protocol: 'rtu', serial_port: '/dev/ttyUSB7', unit_id: 2 }, stored_lacks: ['bridge', 'bridge_port', 'host'] });
}
{
  const p = edit(rows.udp, { udp: false });
  check('udp → tcp: a new protocol, the whole connection', p.protocol === 'tcp' && p.host === '192.0.2.81' && p.port === 502, p);
  push('udp', 'UDP off', p, { stored_has: { protocol: 'tcp', host: '192.0.2.81', port: 502, unit_id: 3, timeout: 2 }, stored_lacks: [] });
}
{
  const p = edit(rows.tcp, { via: 'gw::502' });
  check('tcp → onto a gateway bus: the bus, its own address cleared',
        eq(p, { bridge: 'gw', bridge_port: 502, unit_id: 1, timeout: 3, host: null, port: null,
                serial_port: null, baudrate: null, parity: null, stopbits: null, bytesize: null }), p);
  push('tcp', 'onto a gateway bus', p, { stored_has: { bridge: 'gw', unit_id: 1 }, stored_lacks: ['host', 'port', 'serial_port'] });
  const M = CM.model(rows.tcp), M2 = CM.apply(M, Object.assign(CM.display(M), { via: 'gw::502' }), CM.display(M));
  check('onto a bridge in one protocol is a move, not a new start', !CM.routeChanged(M2, M) && CM.bridgeMoved(M2, M));
}

// ── review 8b (MAJOR-1): off a gateway's bus, the port shown is the port saved ──
{
  const row = rows['via-gw'];
  const M = CM.model(row);
  const shown = CM.display(M);
  const M2 = CM.apply(M, Object.assign({}, shown, { via: '' }), shown);
  check('off a gateway bus: the model has the 502 the form shows', M2.port === 502 && CM.display(M2).port === '502', M2.port);
  // the form redraws (its shown values are M2's now); the host is typed
  const M3 = CM.apply(M2, Object.assign(CM.display(M2), { host: '192.0.2.90' }), CM.display(M2));
  const p = CM.payload(M3, M, { context: 'edit' });
  check('off a gateway bus with 502 left as shown: 502 saved, the bridge said left',
        p.protocol === 'tcp' && p.host === '192.0.2.90' && p.port === 502 && p.bridge === null, p);
  check('…and the only local error is gone', !Object.keys(CM.errors(M3)).length, CM.errors(M3));
  push('via-gw', 'off the gateway bus, own address', p,
       { stored_has: { host: '192.0.2.90', unit_id: 4 }, stored_lacks: ['bridge', 'bridge_port'] });   // tcp and 502: the defaults, not stored
}

// ── MAJOR-2: Test after taking it off a bridge says the bridge is left ──
{
  const M = CM.model(rows['on-bridge']);
  const M2 = Object.assign(JSON.parse(JSON.stringify(M)), { bridge: '', bridge_port: null, legacy: true, host: '192.0.2.70', port: 4001 });
  const c = CM.testConnection(M2, M);
  check('Test off a bridge: bridge null, the typed address', c.bridge === null && c.host === '192.0.2.70' && c.port === 4001, c);
  const p = CM.payload(M2, M, { context: 'edit' });
  check('Test and save agree on what they say of the bridge', p.bridge === null && p.host === c.host && p.port === c.port, p);
  check('Test of an untouched form is the whole stored connection', eq(CM.testConnection(M, M), CM.wire(M)));
  check('Test on a new device (no saved one) never says bridge: null', !('bridge' in CM.testConnection(M2, null)) || CM.testConnection(M2, null).bridge !== null);
}

// ── MAJOR-4: Test on an edited bridged device sends the edited bus and unit ──
{
  const M = CM.model(rows['on-bridge']);
  const M2 = CM.apply(M, Object.assign(CM.display(M), { unit_id: '7' }), CM.display(M));
  const c = CM.testConnection(M2, M);
  check('Test an edited bridged device: unit 7 on its bridge', c.unit_id === 7 && c.bridge === M.bridge, c);
}

// ── MAJOR-5: a typed RTU-over-network address is a choice the model keeps ──
check('rtu-tcp at an address opens as that choice', CM.model(rows['rtu-tcp']).legacy === true);
check('a bridged one does not', CM.model(rows['on-bridge']).legacy === false);
check('a prefilled address for RTU over the network is that choice',
      CM.blank({ protocol: 'rtu-tcp', host: '192.0.2.7', port: 4005 }).legacy === true);
{
  const M = CM.blank();
  M.mode = 'rtu-tcp'; M.legacy = true; M.host = '192.0.2.7'; M.port = 4005;
  const again = JSON.parse(JSON.stringify(CM.apply(M, CM.display(M), CM.display(M))));
  check('the choice survives a read and a copy (Next, Back)', again.legacy === true && again.host === '192.0.2.7');
}

// ── MINOR-4: a local tap moved onto a bridge clears its own line ──
{
  const p = edit(rows['tap-local'], { via: 'ws::4197' });
  check('local tap → a bridge: the line and the serial path cleared',
        p.protocol === 'rtu_tap' && p.bridge === 'ws' && p.bridge_port === 4197
        && p.serial_port === null && p.baudrate === null && p.host === null, p);
  push('tap-local', 'onto a bridge', p, { stored_has: { protocol: 'rtu_tap', bridge: 'ws', bridge_port: 4197, unit_id: 2 },
                                          stored_lacks: ['serial_port', 'baudrate', 'parity', 'stopbits', 'bytesize'] });
}

// ── MINOR-6: a stored value the form would judge differently never blocks ──
{
  const row = JSON.parse(JSON.stringify(rows.umg512));
  row.connection = Object.assign({}, row.connection, { timeout: 0.5 });
  const M = CM.model(row);
  const M2 = CM.apply(M, Object.assign(CM.display(M), { host: '192.0.2.207' }), CM.display(M));
  check('a sub-second stored timeout: an error on it, but not a changed key',
        'connection.timeout' in CM.errors(M2) && !CM.changed(M2, M).includes('timeout') && CM.changed(M2, M).includes('host'),
        CM.changed(M2, M));
}

// ── MINOR-7: vars round trip, a value holding ", name=" escaped ──
{
  const v = { url: 'a=1, b=2', p: 'x\\y', q: 'r,s' };
  check('vars: a value with ", name=" is written with \\, and read back whole',
        eq(CM.text2vars(CM.vars2text(v)), v) && /a=1\\, b=2/.test(CM.vars2text(v)), CM.vars2text(v));
  check('vars: a plain comma inside a value needs no escape', CM.vars2text({ q: 'r,s' }) === 'q=r,s');
}

// ── what the form tells before asking ──
{
  const M = CM.blank();
  check('a new device: host needed', 'connection.host' in CM.errors(M));
  M.host = '/dev/ttyUSB0';
  check('a serial path as a host is said at the field', /serial/.test(CM.errors(M)['connection.host'] || ''));
  M.host = 'tty-gateway.local';
  check('a host name that merely starts like one is a host', !('connection.host' in CM.errors(M)));
  M.unit_id = 256;
  check('unit 256 is refused', 'connection.unit_id' in CM.errors(M));
  M.unit_id = 0;
  check('unit 0 is a unit', !('connection.unit_id' in CM.errors(M)));
  const H = CM.blank({ protocol: 'http', url: 'ftp://x' });
  check('an http URL must be http(s)', 'connection.url' in CM.errors(H));
}
// ── the device page's PUT: its fields as the model reads them ──
check('pagePayload: an untouched page sends its fields and connection {}',
      eq(Object.keys(out.bodies.gx).sort(), ['connection', 'enabled', 'ha_discovery_enabled', 'id', 'influxdb', 'mqtt', 'name', 'template'])
      && eq(out.bodies.gx.connection, {}));
check('pagePayload: a device measured only by its meters sends its identity only',
      eq(CM.pagePayload('ups', { name: 'UPS', protocol: 'none', template: 'x', enabled: true }, { host: 'h' }).connection, { protocol: 'none' }));

check('a new device is added with its whole connection',
      eq(CM.payload(CM.blank({ protocol: 'tcp', host: '192.0.2.5' }), null, { context: 'add' }),
         { protocol: 'tcp', host: '192.0.2.5', port: 502, unit_id: 1, timeout: 3 }));

const path = new URL('connform_payloads.json', FIX);
const text = JSON.stringify(out, null, 1) + '\n';
if (process.argv.includes('--write')) writeFileSync(path, text);
let kept = '';
try { kept = readFileSync(path, 'utf8'); } catch (e) { /* first run */ }
check('the payload fixture the pytest sends is this model\'s', kept === text, 'run with --write after a deliberate change');

console.log(`${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
