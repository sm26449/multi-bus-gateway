// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// A CAN link in the connection form's model (ui/js/connform-model.js), with
// no browser: a new CAN device's defaults and what Add sends, a saved one's
// row reopened and one field edited, what the form refuses before asking
// the server, and the template family it offers (CAN maps only).
//
//   node tools/e2e/connform_can_model_test.mjs
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const CM = require('../../ui/js/connform-model.js');

let failed = 0, n = 0;
const check = (name, cond, extra = '') => {
  n++; if (!cond) failed++;
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ── a new CAN device ──
const M0 = CM.blank({ protocol: 'can' });
check('CAN is a mode of its own', CM.MODES.includes('can') && M0.mode === 'can' && CM.classOf('can') === 'can');
check('a new CAN link opens on socketcan / can0 / 500 kbit/s / 5 s',
  eq(CM.wire(M0), { protocol: 'can', carrier: 'socketcan', channel: 'can0', bitrate: 500000, stale_after_s: 5 }),
  CM.wire(M0));
check('a new CAN link has nothing to fix', eq(CM.errors(M0), {}), CM.errors(M0));
check('Add sends the whole CAN connection',
  eq(CM.payload(M0, null, { context: 'add' }), CM.wire(M0)));
check('Test sends the whole CAN connection', eq(CM.testConnection(M0, null), CM.wire(M0)));

// switching to CAN from the default Modbus TCP form keeps nothing of Modbus
const M1 = CM.apply(CM.blank(), { mode: 'can' }, null);
check('choosing CAN on the form sends only CAN fields',
  eq(Object.keys(CM.wire(M1)).sort(), ['bitrate', 'carrier', 'channel', 'protocol', 'stale_after_s']), CM.wire(M1));

// typed values
const typed = CM.apply(M0, { carrier: 'socketcan', channel: ' can1 ', bitrate: '250000', stale_after_s: '7' }, null);
check('typed fields are read back', eq(CM.wire(typed),
  { protocol: 'can', carrier: 'socketcan', channel: 'can1', bitrate: 250000, stale_after_s: 7 }), CM.wire(typed));

// ── what the form refuses ──
const bad = (inputs) => CM.errors(CM.apply(M0, inputs, null));
check('a missing interface is refused at its field', 'connection.channel' in bad({ channel: '' }));
check('an interface name with a shell in it is refused', 'connection.channel' in bad({ channel: 'can0; rm' }));
check('the bridge carrier is not available yet', 'connection.carrier' in bad({ carrier: 'bridge' }));
check('a bitrate out of range is refused', 'connection.bitrate' in bad({ bitrate: '7' }));
check('stale after 0 s is refused', 'connection.stale_after_s' in bad({ stale_after_s: '0' }));
check('stale after 2.5 s is refused (whole seconds)', 'connection.stale_after_s' in bad({ stale_after_s: '2.5' }));
check('server errors land at the CAN fields',
  ['carrier', 'channel', 'bitrate', 'stale_after_s'].every(k => CM.fieldOf(`connection.${k}`, M0) === k));

// ── a saved CAN device, reopened ──
const row = { id: 'bms-can', protocol: 'can',
  connection: { protocol: 'can', carrier: 'socketcan', channel: 'can0', bitrate: 500000, stale_after_s: 5,
                host: '', port: 502, unit_id: 1, timeout: 3 } };
const M = CM.model(row);
const shown = CM.display(M);
check('the saved row shows its fields', shown.channel === 'can0' && shown.bitrate === '500000'
  && shown.stale_after_s === '5' && shown.carrier === 'socketcan', shown);
check('an untouched CAN form sends nothing', eq(CM.payload(CM.apply(M, shown, shown), M, { context: 'edit' }), {}));
const one = CM.payload(CM.apply(M, Object.assign({}, shown, { channel: 'can1' }), shown), M, { context: 'edit' });
check('one edited field sends that field and its protocol', eq(one, { channel: 'can1', protocol: 'can' }), one);
check('a stored row with no stale_after_s opens at the default 5',
  CM.model({ protocol: 'can', connection: { protocol: 'can', channel: 'can0' } }).stale_after_s === 5);

// ── a non-CAN device is not touched by the CAN fields ──
const mq = CM.model({ protocol: 'mqtt', connection: { protocol: 'mqtt', broker: 'b', topic: 't', stale_after_s: 99 } });
check('an MQTT device’s wire has no CAN field', !('channel' in CM.wire(mq)) && !('bitrate' in CM.wire(mq)));
const tcp = CM.model({ protocol: 'tcp', connection: { protocol: 'tcp', host: '10.0.0.5', port: 502, unit_id: 1, timeout: 3 } });
check('a Modbus TCP form untouched still sends nothing',
  eq(CM.payload(CM.apply(tcp, CM.display(tcp), CM.display(tcp)), tcp, { context: 'edit' }), {}));

// ── the template family a CAN form offers (app-wizard.js filters by it) ──
const tpls = [{ id: 'pylontech_can_inverter', transport: 'can' }, { id: 'em24', transport: 'modbus' },
              { id: 'shelly', transport: 'http' }, { id: 'legacy' }];
const offered = cls => tpls.filter(x => (x.transport || 'modbus') === cls).map(x => x.id);
check('a CAN device is offered the CAN templates only', eq(offered(CM.classOf('can')), ['pylontech_can_inverter']));
check('a Modbus device is never offered a CAN template', !offered(CM.classOf('tcp')).includes('pylontech_can_inverter'));

console.log(`\n${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
