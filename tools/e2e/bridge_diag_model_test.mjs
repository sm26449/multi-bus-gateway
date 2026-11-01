// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// A bus's line health (ui/js/bridge-diag.js), with no browser: the chip's
// colour from the diagnosis, the line counters with null kept null (an
// adapter that counts nothing is never shown as a clean 0), the activity.
//
//   node tools/e2e/bridge_diag_model_test.mjs
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const BD = require('../../ui/js/bridge-diag.js');

let failed = 0, n = 0;
const check = (name, cond, extra = '') => {
  n++; if (!cond) failed++;
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};

const dx = code => ({ diagnosis: { code, severity: 'x', confidence: 'high' } });

// the chip: one colour per diagnosis
check('ok is green', BD.level(dx('ok')) === 'ok');
for (const c of ['wiring', 'network', 'device_silent', 'host_overrun', 'timeouts'])
  check(`${c} is a warning`, BD.level(dx(c)) === 'warn');
check('line noise is information', BD.level(dx('line_noise')) === 'info');
for (const c of ['no_data', 'collecting', 'unsupported'])
  check(`${c} is unknown`, BD.level(dx(c)) === 'unknown');
check('no diagnosis at all (an older bridge) is unknown', BD.level(null) === 'unknown' && BD.code(undefined) === 'none');
check('an unknown code from a newer gateway is not trusted', BD.code(dx('later_kind')) === 'none');
check('every code of the server is known here',
  ['no_data', 'collecting', 'host_overrun', 'wiring', 'device_silent', 'network', 'timeouts', 'line_noise', 'ok']
    .every(c => BD.CODES.includes(c)));

// the line: null stays null
const ftdi = { line: { frame: 3, parity: 2, overrun: 0, brk: 0, buf_overrun: 0, rx: 900, tx: 800 } };
const rates = { frame_per_min: 0.6, parity_per_min: 0.4, overrun_per_min: 0, brk_per_min: 0, buf_overrun_per_min: 0 };
const rows = BD.lineRows(ftdi, rates);
check('five error counters, in order', rows.map(r => r.field).join() === 'frame,parity,overrun,brk,buf_overrun');
check('totals and rates carried', rows[0].total === 3 && rows[0].perMin === 0.6 && rows[0].known);
check('a zero is a real zero', rows[2].total === 0 && rows[2].known === true);
check('total line errors', BD.lineErrors(ftdi) === 5);

const ch340 = { line: null, line_note: 'the CH340/CH341 driver keeps no line counters' };
const crow = BD.lineRows(ch340, {});
check('CH340: every counter unknown, never 0', crow.every(r => r.total === null && !r.known));
check('CH340: no line-error total', BD.lineErrors(ch340) === null);

const cp = { line: { frame: null, parity: null, overrun: null, brk: null, buf_overrun: null, rx: null, tx: null } };
check('CP210x without parity checking: unknown, not 0', BD.lineErrors(cp) === null && BD.lineRows(cp).every(r => !r.known));
check('rates absent: per minute unknown', BD.lineRows(ftdi, null)[0].perMin === null);

// activity
const st = { client: { address: '172.20.0.5:40000', since: 900 }, silent_s: 42.4, last_rx_ts: 958, last_tx_ts: 999.6 };
const a = BD.activity(st, 1000);
check('client address and since', a.client === '172.20.0.5:40000' && a.since === 900);
check('silent and last activity per direction', a.silentS === 42.4 && a.lastRxAgoS === 42 && a.lastTxAgoS === 0);
const brief = BD.activity({ client: '10.0.0.2:1' }, 1000);
check('the card\'s brief form (client as a string)', brief.client === '10.0.0.2:1' && brief.since === null);
const none = BD.activity(null, 1000);
check('nothing known', none.client === null && none.silentS === null && none.lastRxAgoS === null);
check('a timestamp in the future is 0 s ago, never negative', BD.ago(1010, 1000) === 0);
check('event time: seconds → ms', BD.eventTime({ ts: 1000 }) === 1e6);
check('event time: a bad value from a bridge is no time, not a crash',
  [{ ts: 'x' }, { ts: null }, { ts: NaN }, { ts: Infinity }, {}, null, 'str'].every(e => BD.eventTime(e) === null));

console.log(`\n${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
