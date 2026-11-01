// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The connection step's choices (ui/js/conn-step-model.js, ADR-0031 §2, L3.6),
// with no browser: every option it offers comes from the matrix the server
// serves (tests/fixtures/conn_step_protocols.json, kept equal to GET
// /api/protocols by tests/test_bus_line.py), /api/buses and the template
// summary — each unavailable one with its reason. And what it chooses is the
// connection the matrix says that cell is stored as (ConnformModel.wire).
//
//   node tools/e2e/conn_step_model_test.mjs
import { createRequire } from 'module';
import { readFileSync } from 'fs';
const require = createRequire(import.meta.url);
const CS = require('../../ui/js/conn-step-model.js');
const CM = require('../../ui/js/connform-model.js');
const SP = require('../../ui/js/serial-ports.js');
const PROTO = JSON.parse(readFileSync(new URL('../../tests/fixtures/conn_step_protocols.json', import.meta.url)));
const CELLS = PROTO.matrix.device;
const INVALID = PROTO.invalid.device;

let failed = 0, n = 0;
const check = (name, cond, extra = '') => {
  n++; if (!cond) failed++;
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const AX = ['port', 'reach', 'protocol', 'framing', 'udp', 'mode'];
const axes = c => Object.fromEntries(AX.map(k => [k, c[k] ?? null]));
const opt = (list, v) => (list || []).find(o => o.value === v);
const ctxOf = (ax, more = {}) => ({ protocols: PROTO, axes: ax, role: ax.mode, context: 'add', ...more });
const protoKey = ax => (ax.protocol === 'modbus' && ax.port === 'serial' && ax.reach !== 'bridge') ? `modbus:${ax.framing}` : ax.protocol === 'modbus' && ax.port === 'serial' ? 'modbus' : ax.protocol;

// ── every cell the matrix serves: offered, or refused with its own note ──
for (const c of CELLS) {
  const ax = axes(c);
  const o = CS.options(ctxOf(ax));
  const name = `${c.port}/${c.reach}/${c.protocol}/${c.framing}/${c.mode}${c.udp ? '/udp' : ''}`;
  check(`${name}: its port is offered`, opt(o.ports, c.port)?.state === 'ok', o.ports);
  if (c.reach) {
    const r = opt(o.reaches, c.reach);
    if (c.status === 'not_yet' && CELLS.filter(x => x.port === c.port && x.reach === c.reach && x.status === 'today').length === 0
        && c.protocol !== 'seplos_v2') {
      check(`${name}: its reach is shown, refused with the matrix's note`, r && r.state === 'off' && r.reason === c.note, r);
    } else {
      check(`${name}: its reach is offered`, r && r.state === 'ok', o.reaches);
    }
  }
  if (c.port === 'serial') {
    const p = opt(o.protocols, protoKey(ax));
    if (c.status === 'not_yet') check(`${name}: its protocol here is refused with the note`, p && p.state === 'off' && p.reason === c.note, o.protocols);
    else check(`${name}: its protocol is offered`, p && p.state === 'ok', o.protocols);
  }
  const role = opt(o.roles, c.mode);
  if (c.port === 'http' || c.port === 'mqtt') check(`${name}: no role is asked (no bus)`, o.roles.length === 0, o.roles);
  else if (c.status === 'today') check(`${name}: its role is offered`, role && role.state === 'ok', o.roles);
  else if (c.port === 'serial' && c.protocol === 'modbus') check(`${name}: its role refused with the note`, role && role.state === 'off' && role.reason === c.note, o.roles);
  // the M it makes is the connection the matrix stores for that cell
  if (c.status === 'today') {
    let M = CS.applyAxes(CM.blank(), ax);
    if (c.reach === 'bridge') M = Object.assign(M, { bridge: 'br1', bridge_port: 7001 });
    if (c.reach === 'here' || c.reach === 'address') M.serial_port = '/dev/ttyX';
    check(`${name}: axesOf(applyAxes) is the cell`, eq(CS.axesOf(M), ax), [CS.axesOf(M), ax]);
    const w = CM.wire(M);
    const want = c.connection;
    check(`${name}: the wire is the matrix's stored form`, Object.entries(want).every(([k, v]) => w[k] === v)
          && (c.reach !== 'bridge' || (w.bridge === 'br1' && !('host' in w))), [w, want]);
  }
}

// ── every combination no device may have: refused with the server's words ──
for (const inv of INVALID) {
  const ax = axes(inv);
  const o = CS.options(ctxOf(ax));
  const r = opt(o.roles, inv.mode);
  check(`invalid ${inv.code} (${inv.port}/${inv.reach}/${inv.framing}${inv.udp ? '/udp' : ''}): the role is shown, aria-disabled, with its reason`,
        r && r.state === 'off' && r.code === inv.code && r.reason === inv.text, o.roles);
}

// ── the template narrows: a port it has not is off, with why ──
{
  const tpl = { id: 'sdm', name: 'Eastron SDM120', speaks: 'modbus', ports: ['serial'], listen_only: false };
  const o = CS.options(ctxOf({ port: 'serial', reach: 'here', protocol: 'modbus', framing: 'rtu', udp: false, mode: null }, { tpl }));
  check('template: its own port offered', opt(o.ports, 'serial').state === 'ok');
  check('template: Ethernet refused, the model named', opt(o.ports, 'ethernet').state === 'off' && opt(o.ports, 'ethernet').code === 'tpl_port'
        && /Eastron SDM120/.test(opt(o.ports, 'ethernet').reason), opt(o.ports, 'ethernet'));
  check('template: every port is still shown', o.ports.map(x => x.value).join() === 'serial,ethernet,can,http,mqtt', o.ports.map(x => x.value));
  check('template: Seplos refused (it speaks Modbus)', opt(o.protocols, 'seplos_v2').state === 'off' && opt(o.protocols, 'seplos_v2').code === 'tpl_speaks');
  const sv = CS.options(ctxOf({ port: 'serial', reach: 'bridge', protocol: 'seplos_v2', framing: null, udp: false, mode: null },
                              { tpl: { name: 'Seplos', speaks: 'seplos_v2', ports: ['serial'] } }));
  check('Seplos: through a bridge record refused with the matrix note', opt(sv.reaches, 'bridge').state === 'off'
        && /bridge's host and port/.test(opt(sv.reaches, 'bridge').reason), sv.reaches);
  const can = CS.options(ctxOf({ port: 'can', reach: 'socketcan', protocol: 'can_signals', framing: null, udp: false, mode: 'listen' },
                               { tpl: { name: 'Pylontech', speaks: 'can_signals', ports: ['can'], listen_only: true } }));
  check('CAN: Master refused with the server words, Listen-only offered', opt(can.roles, 'poll').state === 'off'
        && opt(can.roles, 'poll').code === 'can_poll' && opt(can.roles, 'listen').state === 'ok', can.roles);
}

// ── a saved device keeps its class: the lock's own words ──
{
  const lock = { field: 'connection.protocol', code: 'protocol_fixed', text: 'connection.protocol: fixed after creation — this device speaks Modbus' };
  const o = CS.options(ctxOf({ port: 'ethernet', reach: 'address', protocol: 'modbus', framing: 'mbap', udp: false, mode: 'poll' },
                             { context: 'edit', locks: [lock], orig: { port: 'ethernet', reach: 'address', protocol: 'modbus' } }));
  check('edit: Modbus may move to serial', opt(o.ports, 'serial').state === 'ok');
  check('edit: HTTP refused with the lock\'s words', opt(o.ports, 'http').state === 'off' && opt(o.ports, 'http').code === 'protocol_fixed'
        && /fixed after creation/.test(opt(o.ports, 'http').reason), opt(o.ports, 'http'));
  const comp = CS.options(ctxOf({ port: 'mqtt', reach: null, protocol: 'json_mqtt', framing: null, udp: false, mode: 'listen' },
                                { context: 'edit', locks: [{ field: 'connection.protocol', code: 'components', text: "'gx' has 2 component(s)" }],
                                  orig: { port: 'mqtt', reach: null, protocol: 'json_mqtt' } }));
  check('edit: a device holding components keeps its port', comp.ports.filter(x => x.state === 'ok').map(x => x.value).join() === 'mqtt', comp.ports);
}

// ── role on the bus: no default on a bus nobody claims ──
const serialHere = { port: 'serial', reach: 'here', protocol: 'modbus', framing: 'rtu', udp: false, mode: null };
{
  const bus = { id: '/dev/ttySC1', kind: 'serial', devices: [], pollers: [], listeners: [], units_in_use: [], line: null };
  const o = CS.options(ctxOf(serialHere, { role: null, bus }));
  check('an unclaimed bus: no role pre-filled', o.prefill === null, o.prefill);
  check('…both roles offered', opt(o.roles, 'poll').state === 'ok' && opt(o.roles, 'listen').state === 'ok');
  check('…and the role is asked (an error until answered)', 'connection.role' in CS.errors(ctxOf(serialHere, { role: null, bus })));
}
{
  const bus = { id: '/dev/ttySC0', kind: 'serial', devices: [{ id: 'seplos-adr1', role: 'listen-only' }], pollers: [], listeners: ['seplos-adr1'], units_in_use: [] };
  const o = CS.options(ctxOf(serialHere, { role: null, bus }));
  check('a listened bus: Listen-only pre-filled, with its reason', o.prefill && o.prefill.value === 'listen' && /seplos-adr1/.test(o.prefill.reason), o.prefill);
  check('…Master refused, the listener named', opt(o.roles, 'poll').state === 'off' && opt(o.roles, 'poll').code === 'role_bus_listened'
        && /seplos-adr1/.test(opt(o.roles, 'poll').reason), o.roles);
  check('…Master chosen anyway is an error', /seplos-adr1/.test(CS.errors(ctxOf(serialHere, { role: 'poll', bus }))['connection.role'] || ''));
}
{
  const bus = { id: '/dev/ttySC1', kind: 'serial', devices: [{ id: 'deye' }], pollers: ['deye'], listeners: [], units_in_use: [{ unit: 1, device: 'deye' }] };
  const o = CS.options(ctxOf(serialHere, { role: null, bus }));
  check('a polled bus: Master pre-filled, with its reason', o.prefill && o.prefill.value === 'poll' && /deye/.test(o.prefill.reason), o.prefill);
  check('…Listen-only refused, the poller named', opt(o.roles, 'listen').state === 'off' && opt(o.roles, 'listen').code === 'role_bus_polled', o.roles);
  // the device being edited is not its own claim
  const self = CS.options(ctxOf(serialHere, { role: 'poll', bus, context: 'edit', selfId: 'deye' }));
  check('edit: a device is not its own claim', opt(self.roles, 'listen').state === 'ok', self.roles);
}
{
  const tpl = { name: 'Eastron (listen)', speaks: 'modbus', ports: ['serial'], listen_only: true };
  const o = CS.options(ctxOf(serialHere, { role: null, tpl }));
  check('a listen-only template: Listen-only pre-filled, with its reason', o.prefill && o.prefill.value === 'listen' && /Eastron \(listen\)/.test(o.prefill.reason), o.prefill);
  check('…Master refused', opt(o.roles, 'poll').state === 'off' && opt(o.roles, 'poll').code === 'template_listens');
  check('…the master_hint of a template is said beside the role',
        CS.options(ctxOf(serialHere, { role: null, tpl: { name: 'SDM', speaks: 'modbus', master_hint: 'often the inverter\'s meter' } })).hint === 'often the inverter\'s meter');
}
{
  // L3.11c (L3-UX-12): Ethernet — Master is the only role a TCP device can
  // have (the matrix refuses Listen-only there): pre-filled, and why said
  const invText = code => (INVALID.find(c => c.code === code) || {}).text;
  const eth = { port: 'ethernet', reach: 'address', protocol: 'modbus', framing: 'mbap', udp: false, mode: null };
  const o = CS.options(ctxOf(eth, { role: null }));
  check('Ethernet: Master pre-filled, its only possible role', o.prefill && o.prefill.value === 'poll', o.prefill);
  check('…with the matrix\'s reason', o.prefill && o.prefill.code === 'listen_mbap' && o.prefill.reason === invText('listen_mbap'), o.prefill);
  const udp = CS.options(ctxOf(Object.assign({}, eth, { udp: true }), { role: null }));
  check('Ethernet UDP: Master pre-filled too', udp.prefill && udp.prefill.value === 'poll', udp.prefill);
  const gw = CS.options(ctxOf({ port: 'ethernet', reach: 'bridge', protocol: 'modbus', framing: 'mbap', udp: false, mode: null }, { role: null }));
  check('Ethernet through a Modbus gateway: Master pre-filled', gw.prefill && gw.prefill.value === 'poll' && gw.prefill.code === 'listen_gateway_bridge', gw.prefill);
  check('…an answered role is never overwritten', CS.options(ctxOf(eth, { role: 'poll' })).prefill === null);
  check('…and once chosen it is said as pre-filled (forced stays)', CS.options(ctxOf(eth, { role: 'poll' })).forced?.value === 'poll');
  check('serial, nobody on the bus: still asked (both roles fit)', CS.options(ctxOf(serialHere, { role: null,
        bus: { id: 'x', devices: [], pollers: [], listeners: [], units_in_use: [] } })).prefill === null);
}
{
  // Seplos: never defaulted (M7), even where a claim decides
  const sv = { port: 'serial', reach: 'here', protocol: 'seplos_v2', framing: null, udp: false, mode: null };
  const bus = { id: '/dev/ttySC0', kind: 'serial', devices: [{ id: 'seplos-adr1' }], pollers: [], listeners: ['seplos-adr1'], units_in_use: [] };
  const o = CS.options(ctxOf(sv, { role: null, bus }));
  check('Seplos: no role pre-filled even on a listened bus (M7)', o.prefill === null, o.prefill);
  check('…Master still refused there, with why', opt(o.roles, 'poll').state === 'off', o.roles);
  check('…the only role that fits is said, not chosen', o.forced && o.forced.value === 'listen', o.forced);
  // a bus change clears a chosen Seplos role; a Modbus role is re-judged
  check('Seplos: a bus change clears the role', CS.roleAfterBusChange({ protocol: 'seplos_v2' }, 'listen', o) === null);
  const mb = CS.options(ctxOf(serialHere, { role: 'poll', bus }));
  check('Modbus: a bus change to a listened bus moves to its pre-fill', CS.roleAfterBusChange(serialHere, 'poll', mb) === 'listen');
  check('Modbus: a bus change to an unclaimed bus clears the role', CS.roleAfterBusChange(serialHere, 'poll',
        CS.options(ctxOf(serialHere, { role: 'poll', bus: { id: 'x', devices: [], pollers: [], listeners: [], units_in_use: [] } }))) === null);
}

// ── the serial picker never pre-judges an unanswered Seplos role (L3-F4) ──
{
  const sc0 = { name: 'ttySC0', path: '/dev/ttySC0', stable_path: '/dev/ttySC0', kind: 'hat', state: 'listened',
    listened_by: ['seplos-adr1'], polled_by: [], mapped: true };
  const sc1 = { name: 'ttySC1', path: '/dev/ttySC1', stable_path: '/dev/ttySC1', kind: 'hat', state: 'polled',
    listened_by: [], polled_by: ['deye'], mapped: true };
  check('L3-F4: an unanswered Seplos mode is unknown, not poll', SP.role('seplos_v2', '') === 'unknown' && SP.role('seplos_v2') === 'unknown');
  const a = SP.choice(sc0, { role: 'unknown' }), b = SP.choice(sc1, { role: 'unknown' });
  check('L3-F4: …a listened port is pickable, said listened (not refused as for a master)', a.pickable && a.why === 'listened', a);
  check('L3-F4: …a polled port too', b.pickable && b.why === 'polled', b);
  check('L3-F4: the connection step reads its role the same way', CS.pickerRole({ protocol: 'seplos_v2' }, null) === 'unknown'
        && CS.pickerRole(serialHere, null) === 'unknown' && CS.pickerRole(serialHere, 'listen') === 'listen' && CS.pickerRole(serialHere, 'poll') === 'poll');
}

// ── the line: inherited and locked on a bus with devices ──
{
  const bus = { id: '/dev/ttySC1', kind: 'serial', devices: [{ id: 'deye', line: { baud: 9600, parity: 'N', bytesize: 8, stopbits: 1 } }],
    pollers: ['deye'], listeners: [], line: { baud: 9600, parity: 'N', bytesize: 8, stopbits: 1 }, units_in_use: [{ unit: 1, device: 'deye' }] };
  const L = CS.lineInfo(ctxOf({ ...serialHere, mode: 'poll' }, { bus, tpl: { line: { baudrate: 19200, parity: 'E', bytesize: 8, stopbits: 1 } } }));
  check('line: inherited from the bus, locked', L.state === 'inherited' && L.locked === true
        && eq(L.line, { baudrate: 9600, parity: 'N', bytesize: 8, stopbits: 1 }), L);
  check('line: …said with who reads it at what', /deye/.test(L.reason) && /9600 8N1/.test(L.reason), L.reason);
  check('line: …its fix is Edit bus line', L.fix === 'bus');
  const self = CS.lineInfo(ctxOf({ ...serialHere, mode: 'poll' }, { bus, context: 'edit', selfId: 'deye' }));
  check('line: a device alone on its bus edits it in place', self.state === 'device' && !self.locked, self);
  const tl = CS.lineInfo(ctxOf({ ...serialHere, mode: 'poll' }, { tpl: { name: 'X', line: { baudrate: 19200, parity: 'E', bytesize: 8, stopbits: 1 } } }));
  check('line: an empty bus takes the template\'s line', tl.state === 'template' && tl.line.baudrate === 19200 && tl.line.parity === 'E' && !tl.locked, tl);
  const d = CS.lineInfo(ctxOf({ ...serialHere, mode: 'poll' }));
  check('line: else 9600 8N1, labelled a common default', d.state === 'default' && eq(d.line, { baudrate: 9600, parity: 'N', bytesize: 8, stopbits: 1 })
        && /common default — check the device/.test(d.reason), d);
  const conflict = CS.lineInfo(ctxOf({ ...serialHere, mode: 'poll' }, { bus: { ...bus, line: null, line_conflict: true,
    lines: [{ line: { baud: 9600, parity: 'N', bytesize: 8, stopbits: 1 }, devices: ['deye'] }, { line: { baud: 19200, parity: 'E', bytesize: 8, stopbits: 1 }, devices: ['m2'] }],
    devices: [...bus.devices, { id: 'm2', line: { baud: 19200, parity: 'E', bytesize: 8, stopbits: 1 } }] } }));
  check('line: devices that disagree: locked, no line taken, both said', conflict.locked && conflict.line === null
        && /9600 8N1/.test(conflict.reason) && /19200 8E1/.test(conflict.reason), conflict);
  // L3.10 review 2 N2: a standalone device on a bus an installation's unit is
  // on carries the bus-mate's lock (its own code): the bus says the line and
  // who sets it, in the page's words — never the unit's own early answer
  const mate = { id: 'pv-u1', installation: 'pv', line: { baud: 9600, parity: 'N', bytesize: 8, stopbits: 1 } };
  const shared = CS.lineInfo(ctxOf({ ...serialHere, mode: 'poll' }, { context: 'edit', selfId: 'solo',
    bus: { ...bus, devices: [mate, { id: 'solo', line: mate.line }] },
    locks: [{ field: 'connection.line', code: 'line_bus_installation', installation: 'pv', text: 'connection.line: english' }] }));
  check('line (N2): a bus-mate installation — its line shown, the way to the installation, the page\'s words',
        shared.state === 'installation' && shared.fix === 'installation' && eq(shared.line, { baudrate: 9600, parity: 'N', bytesize: 8, stopbits: 1 })
        && /pv/.test(shared.reason) && !/english/.test(shared.reason), shared);
  const unitOwn = CS.lineInfo(ctxOf({ ...serialHere, mode: 'poll' }, { context: 'edit', selfId: 'pv-u1',
    locks: [{ field: 'connection.line', code: 'line_installation', installation: 'pv', text: 'connection.line: set by the installation' }] }));
  check('line: the unit itself still answers early (its installation sets it)', unitOwn.state === 'installation' && unitOwn.readonly, unitOwn);
  const br = CS.lineInfo(ctxOf({ port: 'serial', reach: 'bridge', protocol: 'modbus', framing: 'rtu', udp: false, mode: 'poll' }, { bridge: { id: 'pi', name: 'Pi', api: true } }));
  check('line: through our bridge: the bridge\'s, read-only, Edit bus line', br.state === 'bridge' && br.readonly && br.fix === 'bus', br);
  const tr = CS.lineInfo(ctxOf({ port: 'serial', reach: 'bridge', protocol: 'modbus', framing: 'rtu', udp: false, mode: 'poll' }, { bridge: { id: 'ws', name: 'Waveshare', api: false } }));
  check('line: a converter bridge: set on the box', tr.readonly && tr.fix === 'box' && /set on the box/i.test(tr.reason), tr);
  const conv = CS.lineInfo(ctxOf({ port: 'serial', reach: 'address', protocol: 'modbus', framing: 'rtu', udp: false, mode: 'poll' }));
  check('line: at a converter\'s address: read-only, set on the box', conv.state === 'converter' && conv.readonly && conv.fix === 'box', conv);
  check('line: none for Ethernet', CS.lineInfo(ctxOf({ port: 'ethernet', reach: 'address', protocol: 'modbus', framing: 'mbap', udp: false, mode: 'poll' })).state === 'none');
}

// ── the address: the template's default if free, else the next free ──
{
  const bus = { units_in_use: [{ unit: 1, device: 'deye' }] };
  const a = CS.addressInfo(ctxOf(serialHere, { bus, tpl: { default_unit_id: 1 } }));
  check('address: 1 taken → 2, said "1 is deye; 2 is free"', a.value === 2 && /1 is deye/.test(a.text) && /2 is free/.test(a.text), a);
  check('address: the template\'s default when free', CS.addressInfo(ctxOf(serialHere, { bus, tpl: { default_unit_id: 3 } })).value === 3);
  check('address: no bus, no template → 1', CS.addressInfo(ctxOf(serialHere)).value === 1);
  check('address: several taken → the next free', CS.addressInfo(ctxOf(serialHere, { bus: { units_in_use: [1, 2, 3].map(u => ({ unit: u, device: `d${u}` })) } })).value === 4);
  check('address: the device edited is not its own neighbour', CS.addressInfo(ctxOf(serialHere, { bus, selfId: 'deye', context: 'edit' })).value === 1);
  check('address: wraps below the template default', CS.addressInfo(ctxOf(serialHere, { bus: { units_in_use: [{ unit: 247, device: 'x' }] }, tpl: { default_unit_id: 247 } })).value === 1);
}

// ── CAN: the listen-only gate, with the exact command ──
{
  const cmd = 'ip link set can0 down && ip link set can0 type can bitrate 500000 listen-only on && ip link set can0 up';
  const off = CS.canGate({ name: 'can0', kind: 'can', up: true, bitrate: 500000, listen_only: false });
  check('CAN gate: not listen-only blocks, with the exact ip link command', !off.ok && off.code === 'not_listen_only' && off.command === cmd, off);
  check('CAN gate: down blocks too', !CS.canGate({ name: 'can0', kind: 'can', up: false, bitrate: 500000, listen_only: true }).ok);
  check('CAN gate: up and listen-only passes', CS.canGate({ name: 'can0', kind: 'can', up: true, bitrate: 500000, listen_only: true }).ok);
  check('CAN gate: a vcan passes (nothing reaches a bus)', CS.canGate({ name: 'vcan0', kind: 'vcan', up: true, listen_only: null }).ok);
  const nf = CS.canGate(null, 'can1', 250000);
  check('CAN gate: an interface the host has not blocks', !nf.ok && nf.code === 'not_found' && /can1/.test(nf.text) && /250000/.test(nf.command), nf);
  const e = CS.errors(ctxOf({ port: 'can', reach: 'socketcan', protocol: 'can_signals', framing: null, udp: false, mode: 'listen' },
                            { can: { name: 'can0', kind: 'can', up: true, bitrate: 500000, listen_only: false } }));
  check('CAN gate: a hard gate (an error at the interface)', /ip link set can0/.test(e['connection.channel'] || ''), e);
}

// ── HTTP: host + device id with the template's path ──
{
  const path = '/solar_api/v1/GetMeterRealtimeData.cgi?Scope=Device&DeviceId=${unit_id}';
  const url = CS.httpUrl('192.168.1.5', '1', path);
  check('http: host + id make the URL', url === 'http://192.168.1.5/solar_api/v1/GetMeterRealtimeData.cgi?Scope=Device&DeviceId=1', url);
  check('http: …and are read back from it', eq(CS.httpParse(url, path), { host: '192.168.1.5', id: '1' }), CS.httpParse(url, path));
  check('http: a port in the host is kept', CS.httpParse('http://10.0.0.2:8080/solar_api/v1/GetMeterRealtimeData.cgi?Scope=Device&DeviceId=7', path).host === '10.0.0.2:8080');
  check('http: a URL of another shape is not forced into it', CS.httpParse('http://x/other', path) === null);
  check('http: without a template path the URL is typed', CS.httpUrl('h', '1', null) === '');
}

// ── which known bus the form is on ──
{
  const buses = [
    { id: 'chr:4:65', kind: 'serial', path: '/dev/ttySC1', devices: [{ id: 'deye' }] },
    { id: '10.0.0.5:7001', kind: 'bridge', bridge: { id: 'pi', port: 7001 }, port: 7001, devices: [] },
    { id: '10.0.0.9:502', kind: 'address', devices: [] },
    { id: 'can:can0', kind: 'can', devices: [] },
  ];
  const ports = [{ name: 'ttySC1', path: '/dev/ttySC1', stable_path: '/dev/serial/by-path/x', polled_by: ['deye'], listened_by: [] }];
  check('bus: a serial port by its path', CS.busFor({ mode: 'rtu', serial_port: '/dev/ttySC1' }, buses, ports)?.id === 'chr:4:65');
  check('bus: a serial port by another name of it (its claimants)', CS.busFor({ mode: 'rtu', serial_port: '/dev/serial/by-path/x' }, buses, ports)?.id === 'chr:4:65');
  check('bus: a bridge port', CS.busFor({ mode: 'rtu-tcp', bridge: 'pi', bridge_port: 7001 }, buses)?.id === '10.0.0.5:7001');
  check('bus: an address', CS.busFor({ mode: 'rtu-tcp', bridge: '', host: '10.0.0.9', port: 502 }, buses)?.id === '10.0.0.9:502');
  check('bus: a CAN interface', CS.busFor({ mode: 'can', channel: 'can0' }, buses)?.id === 'can:can0');
  check('bus: none for HTTP', CS.busFor({ mode: 'http' }, buses) === null);
}


// ── review (L3.6): m5, m6, names ──
{
  const L9 = { baud: 9600, parity: 'N', bytesize: 8, stopbits: 1 }, L19 = { baud: 19200, parity: 'E', bytesize: 8, stopbits: 1 };
  // m6: the edited device's own (different) line is not a disagreement
  const bus = { id: '/dev/ttyQA1', kind: 'serial', line: null, line_conflict: true,
    lines: [{ line: L9, devices: ['me'] }, { line: L19, devices: ['deye'] }],
    devices: [{ id: 'me', line: L9 }, { id: 'deye', name: 'Deye', line: L19 }], pollers: ['me', 'deye'], listeners: [], units_in_use: [] };
  const L = CS.lineInfo(ctxOf({ ...serialHere, mode: 'poll' }, { bus, context: 'edit', selfId: 'me' }));
  check('m6: the others agree → inherited from them (19200 8E1), not a conflict', L.state === 'inherited'
        && eq(L.line, { baudrate: 19200, parity: 'E', bytesize: 8, stopbits: 1 }) && /Deye/.test(L.reason), L);
  const bus3 = { ...bus, devices: [...bus.devices, { id: 'x3', line: L9 }] };
  const L3 = CS.lineInfo(ctxOf({ ...serialHere, mode: 'poll' }, { bus: bus3, context: 'edit', selfId: 'me' }));
  check('m6: only the OTHERS disagreeing is a conflict', L3.state === 'conflict' && L3.line === null, L3);
  // m5: an installation's unit on the bus: no Edit bus line, its installation named
  const busI = { id: '/dev/ttyQA3', kind: 'serial', line: L9, devices: [{ id: 'plant-1', name: 'Plant 1', line: L9, installation: 'plant' }],
    pollers: [], listeners: ['plant-1'], units_in_use: [] };
  const LI = CS.lineInfo(ctxOf({ ...serialHere, mode: 'listen' }, { bus: busI }));
  check('m5: a bus with an installation\'s unit: no Edit bus line, the installation named', LI.locked && LI.fix === 'installation'
        && /plant/.test(LI.reason), LI);
  const busB = { id: '1.2.3.4:7001', kind: 'bridge', devices: [{ id: 'c1', component_of: 'gx' }] };
  const LB = CS.lineInfo(ctxOf({ port: 'serial', reach: 'bridge', protocol: 'modbus', framing: 'rtu', udp: false, mode: 'poll' },
    { bus: busB, bridge: { id: 'pi', name: 'Pi', api: true } }));
  check('m5: a bridge bus with a component: no Edit bus line either', LB.fix !== 'bus' && /gx/.test(LB.reason), LB);
  // names in the role reasons
  const busN = { id: '/dev/ttySC1', kind: 'serial', devices: [{ id: 'deye', name: 'Deye 8 kW' }], pollers: ['deye'], listeners: [], units_in_use: [] };
  const oN = CS.options(ctxOf(serialHere, { role: null, bus: busN }));
  check('names: a role reason names the device by its name', /Deye 8 kW/.test(opt(oN.roles, 'listen').reason), oN.roles);
}

console.log(`\n${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
