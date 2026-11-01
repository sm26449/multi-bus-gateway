// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The device page as an operations view (ui/js/device-view-model.js,
// ADR-0031 §3, L3.3), with no browser: the one-line summary each kind of
// device shows (the header, a list row, a bridge card), the ⋯ menu each role
// gets for each kind (L3-F13), where each editing item opens, the tabs' four
// groups and the old tab names, and the read-only Setup sections (L3-F16).
//
//   node tools/e2e/device_view_model_test.mjs
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const M = require('../../ui/js/device-view-model.js');

let failed = 0, n = 0;
const check = (name, cond, extra = '') => {
  n++; if (!cond) failed++;
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const ids = groups => groups.flatMap(g => g.items.map(i => i.id));
const item = (groups, id) => groups.flatMap(g => g.items).find(i => i.id === id);
const it = (groups, id) => item(groups, id) || {};

// ── the devices ──────────────────────────────────────────────────────────
const rtu = {
  id: 'deye', name: 'Deye 8k', enabled: true, running: true, template: 'deye_sun', protocol: 'rtu',
  write_locked: true, commands_offered: ['set_limit'], commands: [],
  connection: { protocol: 'rtu', serial_port: '/dev/ttySC1', timeout: 3, retry_attempts: 3, retry_delay: 1 },
  connection_spec: { port: 'serial', reach: 'here', protocol: 'modbus', framing: 'rtu' },
  summary: { port: 'serial', reach: 'here', protocol: 'modbus', framing: 'rtu', role: 'master',
             where: '/dev/ttySC1', line: { baud: 9600, parity: 'E', bytesize: 8, stopbits: 1 },
             unit: 1, address_kind: 'unit', poll_s: [2, 10],
             text: 'Modbus RTU · /dev/ttySC1 · 9600 8E1 · unit 1 · Master · 2 s/10 s' },
  locks: [{ field: 'connection.line', code: 'line_shared', text: 'set by the bus: sdm reads it at 9600 8E1' }],
  sinks: { mqtt: { enabled: true, topic_prefix: 'mbg/devices/deye' }, influxdb: { enabled: true, bucket: 'mbg' },
           opcua: { enabled: false }, http: { enabled: false, path: '/api/meters/deye' }, rest: { enabled: false } },
  ha_discovery_enabled: true, mqtt_enabled: true, influxdb_enabled: true, opcua_enabled: false,
  meters: [{ role: 'consumption', device: 'sdm' }],
};
const bridged = { ...rtu, id: 'b1', bridge: 'wb-01', bridge_port: 2,
  summary: { ...rtu.summary, reach: 'bridge', bridge: { id: 'wb-01', port: 2, host: '192.168.1.50' }, line: null, where: null,
             text: 'Modbus RTU · bridge wb-01 port 2 · unit 3 · Master' } };
const tap = { ...rtu, id: 'tap', protocol: 'rtu_tap', commands_offered: [], commands: [],
  connection_spec: { port: 'serial', reach: 'here', protocol: 'modbus', framing: 'rtu', mode: 'listen', writable: false },
  summary: { ...rtu.summary, role: 'listen-only', poll_s: null,
  text: 'Modbus RTU · /dev/ttySC0 · 9600 8N1 · unit 2 · Listen-only' } };
const seplos = { id: 'bms', name: 'Seplos', enabled: true, protocol: 'seplos_v2', template: 'seplos_v2', write_locked: true,
  connection: { protocol: 'seplos_v2', serial_port: '/dev/ttySC0', mode: 'listen' },
  connection_spec: { port: 'serial', reach: 'here', protocol: 'seplos_v2', mode: 'listen', writable: false },
  summary: { port: 'serial', reach: 'here', protocol: 'seplos_v2', role: 'listen-only', text: 'Seplos V2 · /dev/ttySC0 · Listen-only' } };
const can = { id: 'pyl', name: 'Pylontech', enabled: true, protocol: 'can', template: 'pylontech_can', write_locked: true,
  connection: { protocol: 'can' }, connection_spec: { port: 'can', reach: 'socketcan', protocol: 'can_signals', mode: 'listen', writable: false },
  summary: { port: 'can', protocol: 'can_signals', text: 'CAN · can0 · 500 kbit/s' } };
const mqd = { id: 'gxm', name: 'GX', enabled: true, protocol: 'mqtt', template: 'victron_gx_mqtt', write_locked: true,
  commands_offered: [], commands: [], connection: { protocol: 'mqtt', broker: '127.0.0.1', port: 1883, topic: 'N/x/#' },
  connection_spec: { port: 'mqtt', protocol: 'json_mqtt', mode: 'listen', writable: true },
  summary: { protocol: 'json_mqtt', where: '127.0.0.1', text: 'MQTT/JSON · 127.0.0.1' } };
const http = { id: 'fro', name: 'Fronius', enabled: true, protocol: 'http', write_locked: true,
  connection_spec: { port: 'network', reach: 'address', protocol: 'json_http' },
  summary: { protocol: 'json_http', where: '192.168.1.240', endpoint_path: '/solar_api/v1/GetInverterRealtimeData.cgi',
             text: 'HTTP/JSON · 192.168.1.240/solar_api/v1/GetInverterRealtimeData.cgi · 10 s', poll_s: [10, 10] },
  sinks: { mqtt: { enabled: true }, influxdb: { enabled: false } } };
const unit = { id: 'sun-u1', name: 'Inverter 1', enabled: true, endpoint_id: 'sunfield', endpoint_name: 'Sunfield',
  protocol: 'tcp', write_locked: true, connection_spec: { protocol: 'modbus' },
  read_via: [{ id: 'sunspec', protocol: 'tcp', interval_s: 5 }, { id: 'solarapi', protocol: 'http', interval_s: 10 }],
  summary: { text: 'Modbus TCP · 127.0.0.1:9 · unit 1' } };
const comp = { id: 'gx-bat1', name: 'Battery 1', enabled: true, component_of: 'gx', component_type: 'battery',
  component_type_name: 'Battery', component_instance: 512, protocol: 'mqtt' };
const meterOnly = { id: 'heat', name: 'Heat pump', enabled: true, protocol: 'none', connection: { protocol: 'none' },
  measured_by: ['sdm-2'], summary: { port: null, protocol: null, text: 'Measured by its meters' },
  meters: [{ role: 'input', device: 'sdm-2' }] };
const primary = { ...rtu, id: 'umg512', primary: true, protocol: 'tcp',
  summary: { protocol: 'modbus', framing: 'mbap', role: 'master', where: '10.0.0.5:502', unit: 1,
             text: 'Modbus TCP · 10.0.0.5:502 · unit 1 · Master · 1 s' } };

// ── kinds and roles ──────────────────────────────────────────────────────
check('kind: a polled device', M.kindOf(rtu) === 'device');
check('kind: the primary', M.kindOf(primary) === 'primary');
check('kind: an installation unit', M.kindOf(unit) === 'unit');
check('kind: a component', M.kindOf(comp) === 'component');
check('kind: a device measured by its meters', M.kindOf(meterOnly) === 'meter-only');
check('role: no auth is admin', M.roleOf(false, undefined) === 'admin');
check('role: an unknown role is a viewer', M.roleOf(true, 'guest') === 'viewer');
check('role: operator may do live, not configure', M.allows('operator', 'operator') && !M.allows('operator', 'admin'));

// ── the summary line ─────────────────────────────────────────────────────
check('summary: the server\'s technical line, as it says it',
      M.summaryView(rtu).text === 'Modbus RTU · /dev/ttySC1 · 9600 8E1 · unit 1 · Master · 2 s/10 s');
check('summary: a tap says Listen-only', /Listen-only$/.test(M.summaryView(tap).text));
check('summary: a bridged device names its bridge', M.summaryView(bridged).text.includes('bridge wb-01 port 2'));
check('summary: HTTP', M.summaryView(http).text.startsWith('HTTP/JSON · 192.168.1.240'));
check('summary: a unit says how it is read', M.summaryView(unit).text === 'read via sunspec (Modbus TCP 5 s) + solarapi (HTTP 10 s)',
      M.summaryView(unit).text);
check('summary: a component, through its parent and the parent\'s line',
      M.summaryView(comp, null, { parent: { name: 'GX', summary: { text: 'MQTT/JSON · gateway broker' } } }).text
        === 'through GX · Battery 512 · MQTT/JSON · gateway broker',
      M.summaryView(comp, null, { parent: { name: 'GX', summary: { text: 'MQTT/JSON · gateway broker' } } }).text);
check('summary: measured by its meters', M.summaryView(meterOnly).text === 'Measured by its meters');
check('summary: no summary from the server (an older gateway) → the protocol, never empty',
      M.summaryView({ id: 'x', protocol: 'rtu' }).text === 'Modbus RTU');
check('summary: the translator is used', M.summaryView(unit, (k, d, p) => k === 'ddv.sum.readVia' ? `citit prin ${p.ways}` : d).text.startsWith('citit prin '));

check('lock pill: a Modbus device, locked, in words', eq(M.summaryView(rtu).lock, { locked: true, said: 'writes locked' }));
check('lock pill: unlocked says so', M.summaryView({ ...rtu, write_locked: false }).lock.said === 'writes allowed');
check('lock pill: HTTP without commands has nothing to lock', M.summaryView(http).lock === null);
check('lock pill: a unit shows its installation\'s lock', M.summaryView(unit).lock.locked === true);
check('lock pill: a meter-only device writes nothing', M.summaryView(meterOnly).lock === null);
// L3-UX-7: a listen-only device never says "writes allowed" (nothing is ever written to its bus)
check('lock pill: a listen-only device has none, locked or not', M.summaryView(tap).lock === null
      && M.summaryView({ ...tap, write_locked: false }).lock === null);
check('lock pill: a Seplos read-only map has none', M.summaryView(seplos).lock === null);
check('lock pill: a CAN device has none', M.summaryView(can).lock === null);

const outs = M.summaryView(rtu).outputs;
check('outputs: MQTT, InfluxDB, HA, OPC UA always said; HTTP/REST only when on',
      eq(outs.map(o => o.key), ['mqtt', 'influxdb', 'ha', 'opcua']), outs.map(o => o.key));
check('outputs: on/off in words, never colour alone', outs.find(o => o.key === 'opcua').said === 'OPC UA off'
      && outs.find(o => o.key === 'mqtt').said === 'MQTT on');
check('outputs: an HTTP output that is on is listed',
      M.summaryView({ ...rtu, sinks: { ...rtu.sinks, http: { enabled: true } } }).outputs.some(o => o.key === 'http' && o.on));
// L3-UX-7: a pill is green only when that output publishes now
const live = M.summaryView({ ...rtu, sinks: {
  mqtt: { enabled: true, available: true, connected: false, active: false },
  influxdb: { enabled: true, available: false, connected: false, active: false },
  opcua: { enabled: true, available: true, connected: true, active: true } } }).outputs;
const o = k => live.find(x => x.key === k);
check('outputs: on but its broker not connected is not green, and says so',
      o('mqtt').on === false && o('mqtt').state === 'down' && /not connected/.test(o('mqtt').said), o('mqtt'));
check('outputs: on here but off in Settings is not green, and says so',
      o('influxdb').on === false && o('influxdb').state === 'off-settings' && /off in Settings/.test(o('influxdb').said), o('influxdb'));
check('outputs: HA follows MQTT (not connected: not green)', o('ha').on === false && o('ha').state === 'down', o('ha'));
check('outputs: publishing is green', o('opcua').on === true && o('opcua').state === 'on');

check('via: a bridge', eq(M.summaryView(bridged).via, [{ kind: 'bridge', id: 'wb-01', port: 2 }]));
check('via: an installation', M.summaryView(unit).via[0].kind === 'installation' && M.summaryView(unit).via[0].id === 'sunfield');
check('via: a parent device', M.summaryView(comp).via[0].kind === 'parent');
check('via: a device of its own has none', M.summaryView(rtu).via.length === 0);

// ── the ⋯ menu (ADR-0032 §1–§3): one builder, fixed groups, every
// unavailable item says why ─────────────────────────────────────────────
const GW = { writes: true, credential: true };          // the gateway's own write facts
const act = (e, role, ctx = {}) => M.deviceActions(e, role, { gateway: GW, usedBy: 0, tplUpdate: false, ...ctx }, null);
const why = (groups, id) => (item(groups, id) || {}).reason || '';
const off = (groups, id) => !!(item(groups, id) || {}).disabled;
const GROUP_ORDER = ['operate', 'edit', 'data', 'device', 'safety', 'danger', 'restore'];

const admin = act(rtu, 'admin', { where: 'page', holdsComponents: false, can: { duplicate: true } });
const want = ['test', 'preview', 'reconnect', 'command', 'write',
              'edit', 'connection', 'polling', 'outputs', 'bus-line',
              'measurements', 'download-map', 'calculated', 'commands', 'meters', 'template-update',
              'type', 'about', 'duplicate', 'setup',
              'unlock', 'disable', 'delete', 'restore'];
check('menu admin: every item a polled serial device has, in their groups', eq(ids(admin), want), ids(admin));
check('menu admin: the groups, in order, the restore line last', eq(admin.map(g => g.group), GROUP_ORDER), admin.map(g => g.group));
check('menu: no item outside the fixed groups', admin.every(g => GROUP_ORDER.includes(g.group)));
// review NIT: a reason is said once — by the item itself, never a second id beside it
check('menu admin: Delete is the danger; its reason is its own (no second description)', it(admin, 'delete').danger && it(admin, 'delete').describedBy === '');
check('menu admin: the page\'s items keep their ids', it(admin, 'type').domId === 'ddvSetKind'
      && it(admin, 'disable').domId === 'ddvEnableToggle' && it(admin, 'delete').domId === 'ddvDeleteBtn'
      && it(admin, 'unlock').domId === 'ddvMenuLock' && it(admin, 'test').domId === 'ddvMenuTest');
check('menu admin: Type… says what it is now', it(act({ ...rtu, type: 'inverter', type_label: 'Inverter' }, 'admin'), 'type').label === 'Type: Inverter…'
      && it(M.deviceActions({ ...rtu, type: 'inverter' }, 'admin', { kindLabel: k => `<${k}>` }, null), 'type').label === 'Type: <inverter>…');
check('menu admin: an unlocked device offers Lock', ids(act({ ...rtu, write_locked: false }, 'admin')).includes('lock'));
check('menu admin: a disabled device offers Enable', ids(act({ ...rtu, enabled: false }, 'admin')).includes('enable'));
check('menu: no "Disable instead" item — Disable… asks what uses it, wherever it is chosen (L3-UX-3, L3-UX-11)',
      !ids(admin).includes('disable-instead') && it(admin, 'disable').label === 'Disable…');
check('menu: the restore line closes the menu, alone in its group',
      admin[admin.length - 1].group === 'restore' && eq(admin[admin.length - 1].items.map(i => i.id), ['restore'])
      && it(admin, 'restore').label === 'Deleted devices: restore…');
check('menu: Reconnect is always there for an admin; disabled with its reason while it runs',
      off(admin, 'reconnect') && /running/.test(why(admin, 'reconnect'))
      && !off(act({ ...rtu, running: false }, 'admin'), 'reconnect'));

// ── per role: role-gated items hidden (§13.4 Q1), never shown disabled ───
const oper = act(rtu, 'operator', { where: 'page' });
check('menu operator: the live actions and the way to read the settings', eq(ids(oper), ['test', 'preview', 'command', 'write', 'download-map', 'setup']), ids(oper));
const viewer = act(rtu, 'viewer', { where: 'page' });
check('menu viewer: what it may do, nothing else', eq(ids(viewer), ['download-map', 'setup']), ids(viewer));
check('menu: no item says "admin only" — a role-gated item is hidden',
      [...oper, ...viewer].flatMap(g => g.items).every(i => !/admin only|needs an admin/i.test(i.reason || '')));
check('menu: a reason never sends a role somewhere it may not go',
      [...oper, ...viewer].flatMap(g => g.items).every(i => !i.goto || ['open-installation', 'open-parent', 'setup', 'usedby'].includes(i.goto)),
      [...oper, ...viewer].flatMap(g => g.items).map(i => i.goto));
for (const [name, e] of Object.entries({ rtu, http, unit, comp, meterOnly, primary, tap, seplos, can, mqd })) {
  for (const role of ['viewer', 'operator', 'admin']) {
    const g = act(e, role, { where: 'list' });
    check(`menu: never empty — ${name} as ${role}`, ids(g).length > 0);
    check(`menu: every disabled item says why — ${name} as ${role}`,
          g.flatMap(x => x.items).every(i => !i.disabled || (i.reason && i.reason.length > 3)),
          g.flatMap(x => x.items).filter(i => i.disabled && !i.reason).map(i => i.id));
    check(`menu: the order is the fixed one — ${name} as ${role}`,
          eq(g.map(x => x.group), GROUP_ORDER.filter(x => g.some(y => y.group === x))), g.map(x => x.group));
  }
}

// ── stable: the tab and the async checks never change the set (§2) ───────
for (const [name, e] of Object.entries({ rtu, unit, comp, meterOnly, primary })) {
  const base = ids(M.deviceActions(e, 'admin', { where: 'page', gateway: GW }, null));
  const views = [{ tab: 'overview' }, { tab: 'measurements' }, { tab: 'calculated' }, { tab: 'commands' },
                 { tab: 'setup', section: 'outputs' }, { tab: 'setup', section: 'writes' }, { tab: 'logs' }];
  check(`stable: the same items on every tab — ${name}`,
        views.every(v => eq(ids(M.deviceActions(e, 'admin', { where: 'page', gateway: GW, view: v }, null)), base)));
  const states = [{ usedBy: undefined, tplUpdate: undefined }, { usedBy: 0, tplUpdate: false }, { usedBy: 3, tplUpdate: true },
                  { gateway: undefined }, { gateway: { writes: false, credential: false } }];
  check(`stable: the same items before and after the server answers — ${name}`,
        states.every(s => eq(ids(M.deviceActions(e, 'admin', { where: 'page', gateway: GW, ...s }, null)), base)));
  check(`stable: the same items on the page, a list row and a bridge line — ${name}`,
        ['list', 'bridge', 'installation'].every(w => eq(ids(M.deviceActions(e, 'admin', { where: w, gateway: GW }, null)), base)));
}
const checking = M.deviceActions(rtu, 'admin', { where: 'list', gateway: GW }, null);
check('async: Update from template… present while it is asked, saying "checking…"',
      it(checking, 'template-update').checking === true && off(checking, 'template-update') && /checking/.test(why(checking, 'template-update')));
check('async: Delete… present while what uses it is asked, saying "checking…"',
      it(checking, 'delete').checking === true && /checking/.test(why(checking, 'delete')));
check('async: a template that changed offers the update', !off(act(rtu, 'admin', { tplUpdate: true }), 'template-update'));
check('async: a template that did not change says so', off(admin, 'template-update') && /not changed/.test(why(admin, 'template-update')));
const used = act(rtu, 'admin', { usedBy: 3 });
check('used by: Delete… held, saying by how many, with the way to them', off(used, 'delete') && /3/.test(why(used, 'delete'))
      && it(used, 'delete').goto === 'usedby');
check('used by: Disable… stays (its dialog names what reacts)', !off(used, 'disable'));

// ── reasons: the server's facts (§3) ──────────────────────────────────────
// L3-UX-4: Write… is never offered where the server refuses it
const tapA = act(tap, 'admin');
check('write: a listen-only device keeps Write…, disabled: it listens only', item(tapA, 'write') && off(tapA, 'write')
      && /[Ll]isten-only/.test(why(tapA, 'write')) && it(tapA, 'write').code === 'listen_only', item(tapA, 'write'));
check('write: a locked device keeps Write…, disabled, the way to Unlock', off(admin, 'write') && /[Ll]ocked/.test(why(admin, 'write'))
      && it(admin, 'write').goto === 'unlock' && it(admin, 'write').code === 'locked');
check('write: locked, said to an operator: an admin unlocks it, no way it cannot use',
      off(oper, 'write') && /admin/.test(why(oper, 'write')) && !it(oper, 'write').goto);
const open = { ...rtu, write_locked: false, writable_registers: 4 };
check('write: unlocked, writes on for the gateway: Write… runs', !off(act(open, 'admin'), 'write') && !off(act(open, 'operator'), 'write'));
check('write: writes off for the whole gateway', off(act(open, 'admin', { gateway: { writes: false, credential: true } }), 'write')
      && /Settings/.test(why(act(open, 'admin', { gateway: { writes: false, credential: true } }), 'write'))
      && it(act(open, 'admin', { gateway: { writes: false, credential: true } }), 'write').code === 'writes_off');
check('write: no login and no API key — the server refuses every write',
      it(act(open, 'admin', { gateway: { writes: true, credential: false } }), 'write').code === 'no_credential');
check('write: the gateway\'s facts unknown (an older gateway) — not claimed', !off(act(open, 'admin', { gateway: undefined }), 'write'));
const rawOnly = { ...open, writable_registers: 0 };
check('write: an operator, nothing declared writable — only a raw write is left, and that is the admin\'s',
      off(act(rawOnly, 'operator'), 'write') && it(act(rawOnly, 'operator'), 'write').code === 'raw_admin'
      && !off(act(rawOnly, 'admin'), 'write'));
check('write: HTTP has no Write… (nothing to write)', !ids(act(http, 'admin')).includes('write'));
check('write: MQTT has no Write… (it is written through its commands)', !ids(act(mqd, 'admin')).includes('write'));
check('write: a Seplos map, a CAN device: no Write…', !ids(act(seplos, 'admin')).includes('write') && !ids(act(can, 'admin')).includes('write'));
// L3-UX-5: Lock / Unlock on a device that has nothing to write: disabled, saying so
for (const [name, e] of Object.entries({ tap, seplos, can, http })) {
  const g = act(e, 'admin');
  const lk = item(g, 'unlock') || item(g, 'lock');
  check(`lock: ${name} has nothing to write — the item stays, disabled, saying so`, !!lk && lk.disabled && /[Nn]othing to write|[Ll]isten-only/.test(lk.reason), lk);
}
check('lock: a device with commands can be locked', !off(act({ ...mqd, commands_offered: ['x'] }, 'admin'), 'unlock'));
// L3-UX-9: Polling… on a device measured by its meters polls nothing
const moA = act(meterOnly, 'admin');
check('meter-only: Polling… disabled, measured by its meters, the way to Meters…', off(moA, 'polling')
      && /meters/.test(why(moA, 'polling')) && it(moA, 'polling').goto === 'meters');
check('meter-only: Test disabled with the server\'s reason', off(moA, 'test') && it(moA, 'test').code === 'meter_only');
check('meter-only: Connection… disabled (it has none)', off(moA, 'connection'));
check('meter-only: Edit device…, Meters…, Calculated…, Type…, Delete… run', ['edit', 'meters', 'calculated', 'type', 'delete'].every(i => !off(moA, i)));
const uA = act(unit, 'admin');
check('unit: what its installation sets says so, with the way there',
      ['connection', 'polling', 'disable', 'delete'].every(i => off(uA, i) && /Sunfield/.test(why(uA, i)) && it(uA, i).goto === 'open-installation'),
      ['connection', 'polling', 'disable', 'delete'].map(i => item(uA, i)));
check('unit: Delete… names the server\'s refusal', it(uA, 'delete').code === 'unit');
check('unit: Test, Rename…, Measurements…, Outputs…, the lock run', ['test', 'rename', 'measurements', 'outputs', 'unlock'].every(i => !off(uA, i)), ids(uA));
const cA = act(comp, 'admin');
check('component: what its device sets says so, with the way there',
      ['test', 'connection', 'polling', 'outputs'].every(i => off(cA, i) && /gx/.test(why(cA, i)) && it(cA, i).goto === 'open-parent'),
      ['test', 'connection', 'polling', 'outputs'].map(i => item(cA, i)));
check('component: Rename…, Disable…, Remove… run (through its device)', !off(cA, 'rename') && !off(cA, 'disable') && !off(cA, 'delete')
      && it(cA, 'delete').label === 'Remove…');
const pA = act(primary, 'admin');
check('primary: Delete… disabled with the server\'s reason', off(pA, 'delete') && it(pA, 'delete').code === 'primary');
check('primary: Edit device…, Edit connection…, Polling… run (its page editor)', ['edit', 'connection', 'polling'].every(i => !off(pA, i)));
check('primary: Test, Preview and Write… (locked: disabled)', ['test', 'preview'].every(i => !off(pA, i)) && off(pA, 'write'));
check('preview: no template, nothing to read with', off(act({ ...rtu, template: '' }, 'admin'), 'preview'));
check('commands: Run a command… disabled while the device is locked (the server refuses a locked one)',
      off(admin, 'command') && it(admin, 'command').code === 'locked' && it(admin, 'command').goto === 'unlock');
const noneOn = act({ ...rtu, write_locked: false }, 'admin');
check('commands: unlocked, none enabled — said, with the way to enable one', off(noneOn, 'command')
      && /enabled/.test(why(noneOn, 'command')) && it(noneOn, 'command').goto === 'commands');
check('commands: none enabled, to an operator: an admin enables them', /admin/.test(why(act({ ...rtu, write_locked: false }, 'operator'), 'command')));
check('commands: an enabled command runs once unlocked', !off(act({ ...rtu, write_locked: false, commands: ['set_limit'] }, 'admin'), 'command'));
check('commands: none offered — no Run a command…, no Commands & bindings…', !ids(act(http, 'admin')).some(i => ['command', 'commands'].includes(i)));
check('components: Components… only when the template declares some',
      !ids(admin).includes('components') && ids(act(rtu, 'admin', { holdsComponents: true })).includes('components'));
check('duplicate: offered once it exists', ids(act(rtu, 'admin', { can: { duplicate: true } })).includes('duplicate')
      && !ids(act(rtu, 'admin', { can: { duplicate: false } })).includes('duplicate'));
check('export: not offered before it exists', !ids(admin).includes('export'));
// L3-UX-2: Edit bus line… lives in the menu, never as a dead button in the view
check('bus line: a serial device here has Edit bus line…', ids(admin).includes('bus-line') && !off(admin, 'bus-line'));
check('bus line: a network device has none', !ids(act(http, 'admin')).includes('bus-line') && !ids(act(primary, 'admin')).includes('bus-line'));
const owned = act({ ...rtu, locks: [{ field: 'connection.line', code: 'line_installation', installation: 'sunfield', text: 'connection.line: set by the installation sunfield' }] }, 'admin');
check('bus line: set by an installation — disabled, the server\'s lock said', off(owned, 'bus-line') && /installation sunfield/.test(why(owned, 'bus-line'))
      && !/^connection\.line:/.test(why(owned, 'bus-line')), why(owned, 'bus-line'));
check('bus line: a bridge that keeps its own line — disabled, said',
      off(act({ ...bridged, locks: [{ field: 'connection.line', code: 'line_bridge', text: 'set on the bridge' }] }, 'admin', { bridgeLineApi: () => false }), 'bus-line'));
// every reason that predicts a refusal names it (paired with multibus/api.py: tests/test_ui_device_actions.py)
const coded = [tapA, admin, act(open, 'admin', { gateway: { writes: false, credential: true } }), act(open, 'admin', { gateway: { writes: true, credential: false } }),
               act(rawOnly, 'operator'), moA, uA, cA, pA].flatMap(g => g.flatMap(x => x.items)).filter(i => i.code);
check('reasons: each code is one the server refuses with', coded.length > 5 && coded.every(i => i.code in (M.REFUSALS || {})),
      coded.filter(i => !(i.code in (M.REFUSALS || {}))).map(i => i.code));

check('menu list row: no page ids (many rows in one page)', act(rtu, 'admin', { where: 'list' }).flatMap(g => g.items).every(i => !i.domId && !i.describedBy));
check('menu: labels come from the translator', it(M.deviceActions(rtu, 'admin', {}, (k, d) => k === 'ddv.menu.edit' ? 'Editează…' : d), 'edit').label === 'Editează…');

// ── the header: Test and Preview always, Write… / Run command… where the kind has them (§5) ──
const H = (e, role = 'admin', ctx = {}) => (M.headerActions || (() => []))(e, role, { gateway: GW, usedBy: 0, tplUpdate: false, ...ctx }, null);
check('header: Test, Preview, Run command…, Write… for a polled device with commands', eq(H(rtu).map(i => i.id), ['test', 'preview', 'command', 'write']));
check('header: Write… shown, disabled, while locked (never removed)', (H(rtu).find(i => i.id === 'write') || {}).disabled === true);
check('header: a unit and a component keep Test and Preview, each said (a Modbus unit: Write…, its installation\'s lock)',
      eq(H(unit).map(i => i.id), ['test', 'preview', 'write']) && H(unit).find(i => i.id === 'write').code === 'locked'
      && eq(H(comp).map(i => i.id), ['test', 'preview']) && H(comp).every(i => i.disabled && i.reason));
check('header: a listen-only device keeps Write…, disabled, listen-only', (H(tap).find(i => i.id === 'write') || {}).code === 'listen_only');
check('header: the same items as the menu (one builder)', H(rtu).every(h => eq(h, item(act(rtu, 'admin'), h.id))));
check('header: a viewer gets none', H(rtu, 'viewer').length === 0);

// ── E: the editor of what is in view; the menu says which (§7) ────────────
const eOn = (e, view) => (M.deviceActions(e, 'admin', { where: 'page', gateway: GW, view }, null).flatMap(g => g.items).filter(i => i.shortcut === 'E')).map(i => i.id);
check('E: one item carries it, on every tab', [{ tab: 'overview' }, { tab: 'measurements' }, { tab: 'setup', section: 'polling' }].every(v => eOn(rtu, v).length === 1));
check('E label: on Measurements it is Measurements…', eq(eOn(rtu, { tab: 'measurements' }), ['measurements']));
check('E label: on Calculated it is Calculated…', eq(eOn(rtu, { tab: 'calculated' }), ['calculated']));
check('E label: on Commands it is Commands & bindings…', eq(eOn(rtu, { tab: 'commands' }), ['commands']));
check('E label: elsewhere it is Edit connection…', eq(eOn(rtu, { tab: 'history' }), ['connection']));
check('E label: on Setup › Polling it is Polling…', eq(eOn(rtu, { tab: 'setup', section: 'polling' }), ['polling']));
check('E label: on Setup › Identity it is Edit device… (it opens there)', eq(eOn(rtu, { tab: 'setup', section: 'identity' }), ['edit']));
check('E label: a unit on Measurements is Measurements… (no longer Rename)', eq(eOn(unit, { tab: 'measurements' }), ['measurements']));
check('E label: a unit elsewhere renames', eq(eOn(unit, { tab: 'overview' }), ['rename']));
check('E label: none in a list row (E works on the page only)', act(rtu, 'admin', { where: 'list' }).flatMap(g => g.items).every(i => !i.shortcut));
check('E label: none for an operator or a viewer', [...oper, ...viewer].flatMap(g => g.items).every(i => !i.shortcut));
check('E: Setup › Writes opens the commands editor when it offers commands', M.editKeyFor(rtu, { tab: 'setup', section: 'writes' }) === 'commands');
check('E: Setup › Components opens Components… when it holds some', M.editKeyFor({ ...rtu, components: 2 }, { tab: 'setup', section: 'components' }) === 'components');
// ── the L3.10 review ─────────────────────────────────────────────────────
// M1: a command run is refused without a credential, as a write is (_cmd_gate)
const noCred = act({ ...rtu, write_locked: false, commands: ['set_limit'] }, 'admin', { gateway: { writes: true, credential: false } });
check('review M1: Run a command… is held without a login or an API key', off(noCred, 'command') && it(noCred, 'command').code === 'no_credential',
      it(noCred, 'command'));
// m2: the model says the reasons in the order the server refuses them (listen-only before the lock)
check('review m2: a locked listen-only device: Write… says listen-only (the server\'s first refusal)',
      it(act({ ...tap, write_locked: true }, 'admin'), 'write').code === 'listen_only');
// m3: what uses it, unknown (the gateway could not be asked): never "free", never held
const unknown = act(rtu, 'admin', { usedBy: null });
check('review m3: used-by unknown — Delete… runs (the gateway asks again), saying it is not known',
      !off(unknown, 'delete') && /not known/.test(why(unknown, 'delete')) && !it(unknown, 'delete').checking);
// m4: the item set never waits for something to load
check('review m4: a template that declares components, said on the row: Components… without any template list',
      M.holdsComponents({ components_declared: true }, null)
      && ids(M.deviceActions({ ...rtu, components_declared: true }, 'admin', { gateway: GW }, null)).includes('components'));
check('review m4: a bridge that keeps its own line — said by the row\'s lock, with no bridge list',
      off(act({ ...bridged, locks: [{ field: 'connection.line', code: 'line_bridge', settable: false, text: 'set on the bridge' }] }, 'admin'), 'bus-line'));
check('review m4: a bridge whose line is set from here — Edit bus line… runs',
      !off(act({ ...bridged, locks: [{ field: 'connection.line', code: 'line_bridge', settable: true, text: 'set on the bridge' }] }, 'admin'), 'bus-line'));
const tr = (k, d, p) => `[${k}]${p && p.name ? p.name : ''}`;
const busOwned = M.deviceActions({ ...rtu, locks: [{ field: 'connection.line', code: 'line_installation', installation: 'pv', text: 'english' }] },
                                 'admin', { gateway: GW }, tr);
const mateOwned = M.deviceActions({ ...rtu, locks: [{ field: 'connection.line', code: 'line_bus_installation', installation: 'pv', text: 'english' }] },
                                  'admin', { gateway: GW }, tr);
check('review 2 N2: a bus-mate installation\'s lock (its own code) — held, said', off(mateOwned, 'bus-line') && why(mateOwned, 'bus-line') === '[ddv.why.lineInstallation]pv');
check('review m4/NIT: a line set by an installation on the bus — held, in the page\'s words (translated)',
      off(busOwned, 'bus-line') && why(busOwned, 'bus-line') === '[ddv.why.lineInstallation]pv', why(busOwned, 'bus-line'));
const compOwned = M.deviceActions({ ...rtu, locks: [{ field: 'connection.line', code: 'line_bus_component', parent: 'gx', text: 'english' }] },
                                  'admin', { gateway: GW }, tr);
check('review m4: a line set by a component\'s device on the bus — held, said', off(compOwned, 'bus-line') && why(compOwned, 'bus-line') === '[ddv.why.lineComponent]gx');
// L3.10-F9: a component of a device that is off is off whatever its own switch says
const compOff = act({ ...comp, enabled: false, parent_enabled: false, parent_name: 'Victron GX' }, 'admin');
check('review F9: a component under a device that is off — Enable held, saying its device is off',
      off(compOff, 'enable') && /Victron GX/.test(why(compOff, 'enable')) && it(compOff, 'enable').goto === 'open-parent', it(compOff, 'enable'));
// NIT: the credential is set under Settings › Security › Access, not Writes
check('review NIT: no credential goes where the login is switched on', it(act({ ...rtu, write_locked: false }, 'admin', { gateway: { writes: true, credential: false } }), 'write').goto === 'security-login');

check('E: Edit device… opens the modal at the section in view, else at Connection',
      !!M.editSectionFor && M.editSectionFor(rtu, { tab: 'setup', section: 'identity' }) === 'identity' && M.editSectionFor(rtu, { tab: 'measurements' }) === 'connection'
      && M.editSectionFor(meterOnly, { tab: 'overview' }) === 'identity');

// ── L3.11b (ADR-0032 §6): one way to edit ──────────────────────────────────
check('L3.11b targets: no kind opens elsewhere (no byKind, no page form for the primary)',
      Object.values(M.EDIT_TARGETS).every(tg => !('byKind' in tg)), Object.keys(M.EDIT_TARGETS).filter(k => 'byKind' in M.EDIT_TARGETS[k]));
for (const [item, kind, sec] of [['connection', 'primary', 'connection'], ['polling', 'primary', 'polling'], ['outputs', 'primary', 'outputs'],
                                 ['outputs', 'unit', 'outputs'], ['rename', 'unit', 'identity'], ['rename', 'component', 'identity'],
                                 ['connection', 'device', 'connection'], ['type', 'device', 'identity']]) {
  const tg = M.editTarget(item, kind) || {};
  check(`L3.11b targets: ${item}… of a ${kind} opens the Edit device modal at ${sec}`, tg.modal === sec && !tg.panel && !tg.tab, tg);
}
check('L3.11b targets: Rename… focuses the name', (M.editTarget('rename', 'unit') || {}).focus === 'name');
check('L3.11b targets: Measurements…, Calculated… — one full-screen editor, at its part',
      (M.editTarget('measurements') || {}).full === 'measurements' && (M.editTarget('measurements') || {}).part === 'selected'
      && (M.editTarget('calculated') || {}).full === 'measurements' && (M.editTarget('calculated') || {}).part === 'calculated',
      [M.editTarget('measurements'), M.editTarget('calculated')]);
check('L3.11b targets: Commands & bindings… — its full-screen editor', (M.editTarget('commands') || {}).full === 'commands', M.editTarget('commands'));
const pDis = it(act(primary, 'admin'), 'disable');
check('L3.11b primary: Disable… held — it always polls (the server\'s lock), no way to a section that cannot switch it',
      pDis.disabled && /always polls/.test(pDis.reason) && pDis.goto !== 'connection' && pDis.code === 'primary_polls', pDis);
check('L3.11b E: a unit or a component on Overview — Rename… (now the modal at Identity) carries E',
      eq(eOn(unit, { tab: 'overview' }), ['rename']) && eq(eOn(comp, { tab: 'overview' }), ['rename']));
check('L3.11b E: the primary on Overview — the same item as any device (Edit connection…, the modal at Connection)',
      eq(eOn(primary, { tab: 'overview' }), eOn(rtu, { tab: 'overview' })) && eq(eOn(primary, { tab: 'overview' }), ['connection']), eOn(primary, { tab: 'overview' }));

// ── tabs ─────────────────────────────────────────────────────────────────
check('tabs: four groups', eq(M.GROUPS, ['live', 'data', 'setup', 'diagnose']));
check('tabs: Setup is one tab of its own', Object.entries(M.TAB_GROUP).filter(([, g]) => g === 'setup').map(([k]) => k).join() === 'setup');
check('tabs: no tab only edits (Connection, Outputs, Attributes, Meters left)',
      !['connection', 'outputs', 'attributes', 'meters'].some(x => x in M.TAB_GROUP));
for (const old of ['edit', 'connection', 'outputs', 'attributes', 'meters']) {
  check(`deep link: #/…/${old} opens Setup at its section`, M.TAB_ALIAS[old] === 'setup' && !!M.TAB_SECTION[old]);
}
check('deep link: every alias lands on a tab that exists', Object.values(M.TAB_ALIAS).every(x => x in M.TAB_GROUP));

// ── Setup (read-only) ────────────────────────────────────────────────────
const defaults = { modbus: { timeout: { value: 3, source: 'code' }, retry_attempts: { value: 3 }, retry_delay: { value: 1 } } };
const S = M.setupSections(rtu, defaults, null);
const sec = id => S.find(x => x.id === id);
check('setup: the sections the Edit modal will hold', eq(S.map(x => x.id), ['connection', 'polling', 'identity', 'outputs', 'writes', 'meters']),
      S.map(x => x.id));
for (const id of new Set(Object.values(M.TAB_SECTION))) {
  check(`setup: an old tab's section "${id}" exists`, S.some(x => x.id === id));
}
const row = (s, id) => s.rows.find(r => r.id === id);
check('setup: the connection line', row(sec('connection'), 'summary').value === rtu.summary.text);
check('setup: the role in the operator\'s words', row(sec('connection'), 'role').value === 'Master');
check('setup: the line', row(sec('connection'), 'line').value === '9600 8E1');
check('setup: a value equal to its default says "default"', row(sec('connection'), 'timeout').value === '3 s' && row(sec('connection'), 'timeout').note === 'default');
const S2 = M.setupSections({ ...rtu, connection: { ...rtu.connection, timeout: 5 } }, defaults, null);
check('setup: a value of its own says nothing more', row(S2[0], 'timeout').value === '5 s' && row(S2[0], 'timeout').note === '');
check('setup: the locks with their reasons', sec('connection').locks.length === 1 && /set by the bus/.test(sec('connection').locks[0].text));
check('setup: only the connection\'s locks under Connection (a primary\'s identity / polling / outputs locks are not)',
      eq(M.setupSections({ ...primary, locks: [
        { field: 'connection.host', code: 'primary', text: 'connection.host: the primary' },
        { field: 'identity.name', code: 'primary', text: 'fixed' }, { field: 'polling.enabled', code: 'primary', text: 'fixed' },
        { field: 'outputs.mqtt', code: 'primary', text: 'fixed' }, { field: 'template', code: 'primary', text: 'fixed' }] }, defaults, null)[0]
        .locks.map(l => l.field), ['connection.host']));
check('setup: polling interval', row(sec('polling'), 'poll_s').value === '2–10 s');
check('setup: a listened bus is never asked', /never asked/.test(row(M.setupSections(tap, defaults, null)[1], 'poll_s').value));
check('setup: write protection', row(sec('writes'), 'lock').value === 'locked' && row(sec('writes'), 'commands').value === '0 of 1 enabled');
check('setup: outputs, with the topic and bucket', row(sec('outputs'), 'mqtt').value.startsWith('on · mbg/devices/deye')
      && row(sec('outputs'), 'opcua').value.startsWith('off'));
check('setup: the meters', row(sec('meters'), 'meter-consumption').value === 'sdm');
const SU = M.setupSections(unit, defaults, null);
check('setup unit: how it is read, and where that is set', /read via/.test(SU[0].rows[0].value) && /Sunfield/.test(SU[0].rows[1].value));
check('setup unit: outputs said to be the installation\'s', /installation/.test(SU.find(x => x.id === 'outputs').note));
const mq = { id: 'gx', protocol: 'mqtt', connection: { protocol: 'mqtt', broker: 'none.invalid', port: 8883, tls: true, topic: 'N/x/#' },
  connection_spec: { protocol: 'json_mqtt' }, summary: { protocol: 'json_mqtt', where: 'none.invalid', endpoint_path: 'N/x/#',
  text: 'MQTT/JSON · none.invalid · N/x/#' } };
check('summary MQTT: the broker with its port and TLS, never the topic', M.summaryView(mq).text === 'MQTT/JSON · none.invalid:8883 · TLS · topic ••••',
      M.summaryView(mq).text);
const SQ = M.setupSections(mq, defaults, null)[0];
check('setup MQTT: the broker with its port and TLS, the topic masked (it can carry a portal id)', row(SQ, 'where').value === 'none.invalid:8883 · TLS'
      && row(SQ, 'topic').value === '••••', SQ.rows);
const SM = M.setupSections(meterOnly, defaults, null);
check('setup meter-only: measured by', SM.find(x => x.id === 'connection').rows.some(r => r.value === 'sdm-2')
      && SM.find(x => x.id === 'meters').title === 'Measured by');

// ── review L3.3: URLs without their secrets, the PQ recorder, one components rule ──
check('maskUrl: the userinfo and every query value masked, the rest kept',
      M.maskUrl('http://user:pw@10.0.0.9:8080/solar_api/v1/x.cgi?Scope=Device&token=abc')
        === 'http://••••@10.0.0.9:8080/solar_api/v1/x.cgi?Scope=••••&token=••••',
      M.maskUrl('http://user:pw@10.0.0.9:8080/solar_api/v1/x.cgi?Scope=Device&token=abc'));
check('maskUrl: a plain URL is unchanged; not a URL is said as it is', M.maskUrl('https://h/p') === 'https://h/p' && M.maskUrl('') === '');
const web = { id: 'web', protocol: 'http', connection: { protocol: 'http', url: 'http://u:secret@10.0.0.9/x?key=abc' },
  connection_spec: { protocol: 'json_http' }, summary: { protocol: 'json_http', where: '10.0.0.9', endpoint_path: '/x', text: 'HTTP/JSON · 10.0.0.9/x' },
  sinks: { rest: { enabled: true, url: 'https://cloud/ingest?apikey=zzz' } } };
const SW = M.setupSections(web, defaults, null);
const urlRow = row(SW[0], 'where').value, restRow = row(SW.find(x => x.id === 'outputs'), 'rest').value;
check('setup: the HTTP URL without its secrets (for an admin too)', !/secret|abc/.test(urlRow) && /••••@10\.0\.0\.9/.test(urlRow), urlRow);
check('setup: the REST push URL without its query values', !/zzz/.test(restRow) && /apikey=••••/.test(restRow), restRow);
const pq = { ...rtu, id: 'pq', pq_supported: true, connection: { ...rtu.connection, host: '10.0.0.5' },
  pq_recorder: { enabled: true, poll_s: 300, base_url: '', archive_waveforms: true } };
const SP = M.setupSections(pq, defaults, null).find(x => x.id === 'pq');
check('setup: a meter that records PQ shows its recorder, defaults marked', !!SP && row(SP, 'enabled').value === 'on'
      && row(SP, 'poll_s').value === '300 s' && row(SP, 'poll_s').note === 'default'
      && /http:\/\/10\.0\.0\.5/.test(row(SP, 'base_url').value) && row(SP, 'base_url').note === 'default', SP && SP.rows);
check('setup: no recorder section without PQ', !M.setupSections(rtu, defaults, null).some(x => x.id === 'pq'));
check('components: one rule — the template declares them, or the device holds some',
      M.holdsComponents({ components: 2 }, null) && M.holdsComponents({}, { components: [{ type: 'battery' }] })
      && !M.holdsComponents({}, { components: [] }) && !M.holdsComponents({}, null));

console.log(`\n${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
