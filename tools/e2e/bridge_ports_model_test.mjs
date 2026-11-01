// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The serial ports a bridge lists (ui/js/bridge-ports.js), with no browser:
// which ones a picker may offer, and how a port the bridge cannot serve (a
// HAT channel the container may not open, the console, a busy port) is told
// apart — from a current bridge and from one that predates board UARTs.
//
//   node tools/e2e/bridge_ports_model_test.mjs
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const BP = require('../../ui/js/bridge-ports.js');

let failed = 0, n = 0;
const check = (name, cond, extra = '') => {
  n++; if (!cond) failed++;
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};

// what GET /api/bridge/adapters relays from a current bridge
const usb = { stable_id: '1a86:7523@1-1', dev: '/dev/ttyUSB0', tcp_port: 7001, available: true, excluded: false,
  kind: 'usb', label: 'USB Serial (USB 1-1)', status: 'ok', model: 'USB Serial', bridge_host: 'mbg-serial-bridge' };
const sc0 = { stable_id: 'spi0.0:0', dev: '/dev/ttySC0', tcp_port: 7003, available: true, kind: 'board',
  label: 'SC16IS752 ch0 (spi0.0)', status: 'ok' };
const sc1 = { stable_id: 'spi0.0:1', dev: '/dev/ttySC1', tcp_port: null, available: false, kind: 'board',
  label: 'SC16IS752 ch1 (spi0.0)', status: 'no-access', needs_major: 509,
  reason: 'the container may not open device major 509', fix: 'sh scripts/serial-access.sh' };
const con = { stable_id: '107d001000.serial', dev: '/dev/ttyAMA10', tcp_port: null, available: false,
  kind: 'board', label: 'pl011-axi (107d001000.serial)', status: 'reserved', reason: 'kernel console' };
const busy = { stable_id: 'B0045K08', dev: '/dev/ttyUSB1', tcp_port: null, available: false, kind: 'usb',
  status: 'busy', model: 'FT232R', serial: 'B0045K08' };
const excl = { stable_id: 'X1', dev: '/dev/ttyUSB2', available: false, excluded: true, status: 'excluded', kind: 'usb' };

check('a served USB adapter is offered', BP.selectable(usb) && BP.status(usb) === 'ok');
check('a served HAT channel is offered, as a board port', BP.selectable(sc0) && BP.kind(sc0) === 'board');
check('no-access is shown, never offered', !BP.selectable(sc1) && BP.status(sc1) === 'no-access');
check('the console is reserved, never offered', !BP.selectable(con) && BP.status(con) === 'reserved');
check('a busy port is never offered', !BP.selectable(busy) && BP.status(busy) === 'busy');
const soc = { stable_id: '00:05', dev: '/dev/ttyS0', tcp_port: null, available: false, kind: 'board',
  status: 'not-enabled', needs_enable: true, enabled: false };
check('a SoC UART not enabled is shown, never offered', !BP.selectable(soc) && BP.status(soc) === 'not-enabled');
check('an excluded adapter is never offered', !BP.selectable(excl) && BP.status(excl) === 'excluded');
check('status ok without a data port is not offered', !BP.selectable({ ...usb, tcp_port: null }));
check('an unknown status is not offered', !BP.selectable({ ...usb, status: 'later-kind' })
      && BP.status({ ...usb, status: 'later-kind' }) === 'unknown');

const { usable, blocked } = BP.split([usb, sc1, sc0, con, busy, excl]);
check('split keeps the bridge order, usable first list', usable.map(a => a.stable_id).join() === '1a86:7523@1-1,spi0.0:0');
check('split lists every port it cannot serve', blocked.map(a => a.stable_id).join() === 'spi0.0:1,107d001000.serial,B0045K08,X1');
check('split of nothing', BP.split(undefined).usable.length === 0 && BP.split(null).blocked.length === 0);

check('the label is the bridge\'s', BP.label(sc1) === 'SC16IS752 ch1 (spi0.0)');
check('without a label: model (serial)', BP.label(busy) === 'FT232R (B0045K08)');
check('without a label or model: vendor id', BP.label({ vendor_id: '0403' }) === '0403');

// a bridge before board UARTs: no status, no kind, no label
const old = { stable_id: 'B0045K08', dev: '/dev/ttyUSB1', tcp_port: 7002, available: true, excluded: false,
  model: 'FT232R', serial: 'B0045K08' };
const oldEx = { stable_id: 'X1', dev: '/dev/ttyUSB2', available: false, excluded: true };
check('older bridge: a served adapter is still offered', BP.selectable(old) && BP.status(old) === 'ok');
check('older bridge: kind defaults to USB', BP.kind(old) === 'usb');
check('older bridge: excluded is still not offered', !BP.selectable(oldEx) && BP.status(oldEx) === 'excluded');

// a bridge record's scan (GET /api/bridges/<id>/scan) names the data port `port`
check('a scan bus with a port is offered', BP.selectable({ port: 7001, status: 'ok', available: true }));
check('a scan bus without a port is not', !BP.selectable({ port: null, status: 'no-access', available: false }));

console.log(`\n${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
