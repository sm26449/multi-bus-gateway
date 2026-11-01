// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// A lock in the page's words (ui/js/lock-text.js, L3.10-F16 / review 2 N3),
// with no browser: the lock's code (and, for the primary, its field) picks a
// dictionary key, filled with the lock's params; the server's English is the
// fallback — an unknown code, or a key whose words need a value the lock
// does not carry (an older gateway). Every code has a key: tests/
// test_l311c_lock_text.py reads the codes from the server's code.
//
//   node tools/e2e/lock_text_test.mjs
import { createRequire } from 'module';
import { readFileSync } from 'fs';
const require = createRequire(import.meta.url);
const LT = require('../../ui/js/lock-text.js');
const lang = c => JSON.parse(readFileSync(new URL(`../../ui/languages/${c}.json`, import.meta.url)));
const EN = lang('en'), RO = lang('ro');

let failed = 0, n = 0;
const check = (name, cond, extra = '') => {
  n++; if (!cond) failed++;
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};
// the page's t(): the language over English, params filled
const tOf = d => (k, fb, p) => {
  let s = d[k] || EN[k] || fb || k;
  if (p) s = s.replace(/\{(\w+)\}/g, (m, x) => (x in p ? String(p[x]) : m));
  return s;
};
const tEn = tOf(EN), tRo = tOf(RO);

const polled = { field: 'connection.role', code: 'role_bus_polled',
  text: 'polled by deye, em2: a bus is either polled or listened to, never both, so the role is Master',
  params: { devices: 'deye, em2' } };
check('ro: a role lock in Romanian, the devices named', /deye, em2/.test(LT.text(polled, tRo)) && LT.text(polled, tRo) !== LT.text(polled, tEn), LT.text(polled, tRo));
check('en: the dictionary\'s words', /deye, em2/.test(LT.text(polled, tEn)), LT.text(polled, tEn));

const shared = { field: 'connection.line', code: 'line_shared', text: 'connection.line: set by the bus: meter2 reads it at 9600 8N1. Edit bus line… changes it for every device on /dev/ttySC1',
  params: { devices: 'meter2', line: '9600 8N1', where: '/dev/ttySC1', disagree: false } };
check('ro: the shared line, its devices, line and place', /meter2/.test(LT.text(shared, tRo)) && /9600 8N1/.test(LT.text(shared, tRo)) && /ttySC1/.test(LT.text(shared, tRo)), LT.text(shared, tRo));
const dis = Object.assign({}, shared, { params: Object.assign({}, shared.params, { disagree: true, line: '19200 8N1', own: '9600 8N1' }) });
check('ro: a bus whose devices disagree says so', LT.text(dis, tRo) !== LT.text(shared, tRo) && /9600 8N1/.test(LT.text(dis, tRo)), LT.text(dis, tRo));

const prim = { field: 'identity.name', code: 'primary', text: "the primary device's name is fixed: it is the gateway's own meter", params: {} };
check('primary: a key per field', LT.key(prim) === 'lock.primary.identity.name');
check('ro: the primary\'s name lock in Romanian', LT.text(prim, tRo) === RO['lock.primary.identity.name'], LT.text(prim, tRo));

const unknown = { field: 'connection.x', code: 'from_a_newer_gateway', text: 'connection.x: something new' };
check('an unknown code: the server\'s text, without the field before it', LT.text(unknown, tRo) === 'something new', LT.text(unknown, tRo));
const old = { field: 'connection.role', code: 'role_bus_polled', text: 'polled by deye: …' };
check('a lock without the params its words need (an older gateway): the server\'s text, never "{devices}"',
      LT.text(old, tRo) === 'polled by deye: …', LT.text(old, tRo));
check('a lock with no code: its text', LT.text({ field: 'f', text: 'f: plain' }, tRo) === 'plain');
check('no lock: empty', LT.text(null, tRo) === '');

// the connection step says a lock that holds an option in the page's words
// too (conn-step-model.js, when lock-text.js is on the page)
{
  globalThis.LockText = LT;
  const CS = require('../../ui/js/conn-step-model.js');
  const PROTO = JSON.parse(readFileSync(new URL('../../tests/fixtures/conn_step_protocols.json', import.meta.url)));
  const ax = { port: 'serial', reach: 'here', protocol: 'modbus', framing: 'rtu', udp: false, mode: 'poll' };
  const roles = CS.roleOptions({ protocols: PROTO, axes: ax, role: 'poll', context: 'edit', locks: [polled] }, tRo);
  const listen = roles.find(r => r.value === 'listen');
  check('connection step (ro): a role held by a lock says it in Romanian', listen && listen.state === 'off'
        && listen.reason === LT.text(polled, tRo) && /Interogată/.test(listen.reason), listen);
  const en0 = CS.roleOptions({ protocols: PROTO, axes: ax, role: 'poll', context: 'edit', locks: [polled] });
  check('connection step, no dictionary: the server\'s words', en0.find(r => r.value === 'listen').reason === polled.text, en0);
  delete globalThis.LockText;
}

console.log(`\n${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
