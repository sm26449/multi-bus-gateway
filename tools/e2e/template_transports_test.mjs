// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The template editor's "Ways to reach it" boxes (ui/js/app-templates.js),
// with no browser: a toggle keeps every declared transport that has no box
// (udp, rtu-tcp, ascii, ascii-tcp…) instead of dropping it (review SV2-R8).
//
//   node tools/e2e/template_transports_test.mjs
import { readFileSync } from 'fs';
import vm from 'vm';

const src = readFileSync(new URL('../../ui/js/app-templates.js', import.meta.url), 'utf8');
const ctx = { JanitzaMonitor: function () {} };
vm.createContext(ctx);
vm.runInContext(src, ctx);
const T = ctx.JanitzaMonitor.prototype;

let failed = 0, n = 0;
const check = (name, cond, extra = '') => {
  n++; if (!cond) failed++;
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};
const eq = (a, b) => JSON.stringify([...a].sort()) === JSON.stringify([...b].sort());
const BOXES = ['tcp', 'rtu', 'rtu_tap', 'http', 'mqtt', 'seplos_v2', 'seplos_v2-tcp'];

check('the helper exists', typeof T._tplTransportsKept === 'function');
if (typeof T._tplTransportsKept === 'function') {
  const keep = (prev, checked) => T._tplTransportsKept(prev, BOXES, checked);
  check('udp and rtu-tcp survive ticking rtu_tap',
        eq(keep(['tcp', 'udp', 'rtu-tcp'], ['tcp', 'rtu_tap']), ['tcp', 'rtu_tap', 'udp', 'rtu-tcp']));
  check('ascii and ascii-tcp survive unticking tcp',
        eq(keep(['tcp', 'ascii', 'ascii-tcp'], []), ['ascii', 'ascii-tcp']));
  check('a shown box unticked is removed', eq(keep(['tcp', 'rtu'], ['rtu']), ['rtu']));
  check('nothing declared: the boxes say it all', eq(keep(undefined, ['http']), ['http']));
  check('no duplicate', eq(keep(['udp'], ['udp']), ['udp']));
}
console.log(`${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
