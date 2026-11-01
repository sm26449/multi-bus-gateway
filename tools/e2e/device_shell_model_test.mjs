// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The Edit device modal's draft (ui/js/device-shell.js, ADR-0031 §4, L3.4),
// with no browser: which sections a device has, what the draft holds, what
// changed, the PATCH it sends (only what changed), the edits re-applied on
// the version stored after a 412 (conflicts marked), the changes that need a
// review before Save, and the Save label and footer line the dry run decides.
//
//   node tools/e2e/device_shell_model_test.mjs
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const S = require('../../ui/js/device-shell.js');

let failed = 0, n = 0;
const check = (name, cond, extra = '') => {
  n++; if (!cond) failed++;
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const fill = (s, p) => String(s).replace(/\{(\w+)\}/g, (m, k) => (p && p[k] !== undefined ? String(p[k]) : m));
const tn = (k, num, d, p) => fill(d, { ...(p || {}), n: num });
const t = (k, d, p) => fill(d, p);

// ── the devices (as GET /api/devices/{id} answers them) ──────────────────
const rtu = {
  id: 'deye', name: 'Deye 8k', enabled: true, template: 'deye_sun', protocol: 'rtu', kind: 'inverter', type: 'inverter',
  kind_source: 'template', connection: { protocol: 'rtu', serial_port: '/dev/ttySC1', baudrate: 9600, unit_id: 1 },
  mqtt_enabled: true, influxdb_enabled: true, ha_discovery_enabled: true, http_output_enabled: false,
  opcua_enabled: false, opcua_fields: [],
  publishing: { own: { mqtt: {}, influxdb: { write_interval: 10 } }, settings: {}, effective: {}, editable: true },
  rest_push: { enabled: false, url: '', interval_s: 30, format: 'native', verify_tls: true, headers: {}, timeout: 10 },
};
const groups = { fast: { interval: 2, description: 'power' }, slow: { interval: 60, description: 'energy' } };
const meterOnly = { id: 'heat', name: 'Heat pump', enabled: true, protocol: 'none', connection: { protocol: 'none' },
  type: 'heat_pump', kind: 'heat_pump', kind_source: 'override', mqtt_enabled: true, influxdb_enabled: true };

// ── sections ─────────────────────────────────────────────────────────────
check('sections: a device has Model · Connection · Polling · Identity · Outputs, in that order',
      eq(S.sectionsFor(rtu), ['model', 'connection', 'polling', 'identity', 'outputs']), S.sectionsFor(rtu));
check('sections: a meter-only device has no Model nor Connection (absent, not disabled)',
      eq(S.sectionsFor(meterOnly), ['polling', 'identity', 'outputs']), S.sectionsFor(meterOnly));
// (L3.11b: the primary, a unit and a component are edited in the modal too — below)
check('sections: a path names its section', S.sectionOf('outputs.mqtt.enabled') === 'outputs'
      && S.sectionOf('polling.poll_groups.fast') === 'polling' && S.sectionOf('connection.host') === 'connection'
      && S.sectionOf('identity.name') === 'identity' && S.sectionOf('model.template') === 'model');

// ── the draft ────────────────────────────────────────────────────────────
const f = S.fieldsOf(rtu, groups);
check('fields: name, type (derived = blank), template, enabled',
      f['identity.name'] === 'Deye 8k' && f['identity.type'] === '' && f['model.template'] === 'deye_sun'
      && f['polling.enabled'] === true, f);
check('fields: an own type is the type', S.fieldsOf({ ...rtu, kind_source: 'override', type: 'ups' }, {})['identity.type'] === 'ups');
check('fields: a meter-only device always says its type', S.fieldsOf(meterOnly, {})['identity.type'] === 'heat_pump');
check('fields: each poll group\'s interval', f['polling.poll_groups.fast'] === 2 && f['polling.poll_groups.slow'] === 60);
check('fields: outputs, with the device\'s own publishing (blank = as in Settings)',
      f['outputs.mqtt.enabled'] === true && f['outputs.ha_discovery'] === true && f['outputs.mqtt.publish_mode'] === ''
      && f['outputs.mqtt.heartbeat_interval'] === null && f['outputs.influxdb.write_interval'] === 10
      && f['outputs.http_output'] === false && f['outputs.opcua.enabled'] === false && eq(f['outputs.opcua.fields'], [])
      && f['outputs.rest_push.enabled'] === false && f['outputs.rest_push.interval_s'] === 30, f);

let d = S.draft(f);
check('draft: nothing changed at first', eq(S.changed(d), []) && !S.isDirty(d));
S.set(d, 'identity.name', 'Deye 8k');
check('draft: a value set back to what it was is no change', !S.isDirty(d));
S.set(d, 'identity.name', 'Deye inverter');
S.set(d, 'outputs.mqtt.enabled', false);
check('draft: what changed, in a stable order', eq(S.changed(d), ['identity.name', 'outputs.mqtt.enabled']), S.changed(d));
check('draft: the sections changed', eq([...S.changedSections(d)].sort(), ['identity', 'outputs']));
check('draft: a section changed elsewhere (the connection form) counts too',
      eq([...S.changedSections(d, ['connection'])].sort(), ['connection', 'identity', 'outputs']));

// ── the PATCH (only what changed) ────────────────────────────────────────
check('payload: nothing changed → nothing to send', S.payload(S.draft(f)) === null);
check('payload: a name and an output switch',
      eq(S.payload(d), { sections: { identity: { name: 'Deye inverter' }, outputs: { mqtt: { enabled: false } } } }), S.payload(d));
d = S.draft(f);
S.set(d, 'outputs.mqtt.publish_mode', 'all');
S.set(d, 'outputs.influxdb.write_interval', null);
check('payload: publishing — the keys changed; blank goes back to Settings (null)',
      eq(S.payload(d).sections.outputs, { mqtt: { publish_mode: 'all' }, influxdb: { write_interval: null } }), S.payload(d));
S.set(d, 'outputs.mqtt.publish_mode', '');
S.set(d, 'outputs.influxdb.write_interval', 10);
check('payload: changed back → nothing', S.payload(d) === null);
d = S.draft(f);
S.set(d, 'identity.type', '');
S.set(d, 'identity.type', 'ups');
S.set(d, 'model.template', 'deye_sun_v2');
S.set(d, 'polling.poll_groups.fast', 5);
S.set(d, 'polling.enabled', false);
check('payload: type, template, the one interval changed, acquisition off',
      eq(S.payload(d).sections, { model: { template: 'deye_sun_v2' }, polling: { enabled: false, poll_groups: { fast: { interval: 5 } } },
                                  identity: { type: 'ups' } }), S.payload(d));
d = S.draft(S.fieldsOf({ ...rtu, kind_source: 'override', type: 'ups' }, {}));
S.set(d, 'identity.type', '');
check('payload: a type cleared is derived again (null)', S.payload(d).sections.identity.type === null);
d = S.draft(f);
S.set(d, 'polling.poll_groups.slow', null);
check('payload: a cleared interval goes back to the gateway\'s (null, not {interval: null})',
      eq(S.payload(d).sections.polling.poll_groups, { slow: null }), S.payload(d));
check('fields: the OPC UA values are a set (sorted): another order is no change',
      eq(S.fieldsOf({ ...rtu, opcua_fields: ['b', 'a'] }, {})['outputs.opcua.fields'], ['a', 'b']));
d = S.draft(f);
S.set(d, 'outputs.opcua.enabled', true);
check('payload: OPC UA as one block {enabled, fields}', eq(S.payload(d).sections.outputs.opcua, { enabled: true, fields: [] }));
d = S.draft(f);
S.set(d, 'outputs.rest_push.url', 'https://cloud/ingest');
S.set(d, 'outputs.rest_push.enabled', true);
check('payload: REST push as the whole /rest-push body (its timeout kept)',
      eq(S.payload(d).sections.outputs.rest_push, { enabled: true, url: 'https://cloud/ingest', interval_s: 30, format: 'native',
                                                    verify_tls: true, headers: {}, timeout: 10 }), S.payload(d));
d = S.draft(f);
check('payload: the connection form\'s own change rides along',
      eq(S.payload(d, { connection: { unit_id: 2, protocol: 'rtu' } }), { sections: { connection: { unit_id: 2, protocol: 'rtu' } } }));
check('payload: an empty connection change is nothing', S.payload(d, { connection: {} }) === null);

// ── 412: the edits re-applied on the version stored now ──────────────────
d = S.draft(f);
S.set(d, 'identity.name', 'Mine');
S.set(d, 'polling.poll_groups.fast', 4);
S.set(d, 'outputs.mqtt.enabled', false);
const theirs = S.fieldsOf({ ...rtu, name: 'Theirs', influxdb_enabled: false }, { ...groups, fast: { interval: 4 } });
const rb = S.rebase(d, theirs);
check('rebase: my edits kept on the new version', rb.draft.cur['identity.name'] === 'Mine' && rb.draft.cur['outputs.mqtt.enabled'] === false);
check('rebase: what only they changed is taken', rb.draft.cur['outputs.influxdb.enabled'] === false
      && rb.draft.orig['outputs.influxdb.enabled'] === false);
check('rebase: the new version is the base (nothing saved yet: still changed)',
      eq(S.changed(rb.draft), ['identity.name', 'outputs.mqtt.enabled']), S.changed(rb.draft));
check('rebase: a field both changed is a conflict, with what is stored now',
      eq(rb.conflicts, [{ path: 'identity.name', mine: 'Mine', stored: 'Theirs', was: 'Deye 8k' }]), rb.conflicts);
check('rebase: the same edit made there too is no conflict (and no longer a change)',
      !rb.conflicts.some(c => c.path === 'polling.poll_groups.fast') && !S.changed(rb.draft).includes('polling.poll_groups.fast'));
check('rebase: never a save (the PATCH is built again from the new base)', eq(S.payload(rb.draft).sections.identity, { name: 'Mine' }));

// the connection form's model, re-applied the same way
const was = { mode: 'rtu', serial_port: '/dev/ttySC1', baudrate: 9600, unit_id: 1, timeout: 3 };
const now = { mode: 'rtu', serial_port: '/dev/ttySC1', baudrate: 19200, unit_id: 1, timeout: 5 };
const mine = { ...was, baudrate: 38400, unit_id: 7 };
const rm = S.rebaseModel(mine, was, now);
check('rebase (connection): my keys over the stored model', eq(rm.M, { ...now, baudrate: 38400, unit_id: 7 }), rm.M);
check('rebase (connection): a key both changed is a conflict', eq(rm.conflicts, [{ key: 'baudrate', mine: 38400, stored: 19200, was: 9600 }]), rm.conflicts);

// ── the review a Save needs (safety) ─────────────────────────────────────
const kinds = changes => S.safety(changes).map(x => x.kind);
check('safety: a rename or a timeout needs none',
      eq(kinds([{ section: 'identity', field: 'name', from: 'a', to: 'b' }, { section: 'connection', field: 'timeout', from: 3, to: 5 }]), []));
check('safety: Master → Listen-only', eq(kinds([{ section: 'connection', field: 'protocol', from: 'rtu', to: 'rtu_tap' }]), ['role']));
check('safety: Listen-only → Master', eq(kinds([{ section: 'connection', field: 'protocol', from: 'rtu_tap', to: 'rtu' }]), ['role']));
check('safety: RTU → ASCII is not a role change', eq(kinds([{ section: 'connection', field: 'protocol', from: 'rtu', to: 'ascii' }]), []));
check('safety: a Seplos bus polled → listened', eq(kinds([{ section: 'connection', field: 'mode', from: 'poll', to: 'listen' }]), ['role']));
check('safety: a bus move (serial port, bridge, bridge port)',
      eq(kinds([{ section: 'connection', field: 'serial_port', from: '/dev/ttySC1', to: '/dev/ttySC0' }]), ['bus'])
      && eq(kinds([{ section: 'connection', field: 'bridge', from: 'wb-1', to: 'wb-2' }]), ['bus'])
      && eq(kinds([{ section: 'connection', field: 'bridge_port', from: 1, to: 2 }]), ['bus']));
check('safety: a converter\'s address is its bus (RTU over the network); an HTTP source\'s is not',
      eq(S.safety([{ section: 'connection', field: 'host', from: '10.0.0.5', to: '10.0.0.6' }], { protocol: 'rtu-tcp' }).map(x => x.kind), ['bus'])
      && eq(S.safety([{ section: 'connection', field: 'host', from: '10.0.0.5', to: '10.0.0.6' }], { protocol: 'http' }).map(x => x.kind), []));
check('safety: a template change', eq(kinds([{ section: 'model', field: 'template', from: 'a', to: 'b' }]), ['template']));
check('safety: an output going off (MQTT, InfluxDB, HA, OPC UA)',
      eq(kinds([{ section: 'outputs', field: 'mqtt.enabled', from: null, to: false },
                { section: 'outputs', field: 'influxdb.enabled', from: true, to: false },
                { section: 'outputs', field: 'ha_discovery', from: null, to: false },
                { section: 'outputs', field: 'opcua.enabled', from: true, to: false }]), ['output-off', 'output-off', 'output-off', 'output-off']));
check('safety: HTTP or REST off only when they were on',
      eq(kinds([{ section: 'outputs', field: 'rest_push.enabled', from: null, to: false },
                { section: 'outputs', field: 'http_output.enabled', from: null, to: false }]), [])
      && eq(kinds([{ section: 'outputs', field: 'rest_push.enabled', from: true, to: false }]), ['output-off']));
check('safety: a CAN link moved (channel, carrier) is a bus move',
      eq(kinds([{ section: 'connection', field: 'channel', from: 'can0', to: 'can1' }]), ['bus'])
      && eq(kinds([{ section: 'connection', field: 'carrier', from: 'socketcan', to: 'bridge' }]), ['bus']));
check('safety: an address of a Modbus TCP/UDP device or a tap over the network is its bus',
      ['tcp', 'udp', 'rtu_tap'].every(p => eq(S.safety([{ section: 'connection', field: 'host', from: 'a', to: 'b' }], { protocol: p }).map(x => x.kind), ['bus'])
                                       && eq(S.safety([{ section: 'connection', field: 'port', from: 502, to: 503 }], { protocol: p }).map(x => x.kind), ['bus'])));
check('safety: an MQTT or HTTP address is not a bus', eq(S.safety([{ section: 'connection', field: 'host', from: 'a', to: 'b' }], { protocol: 'mqtt' }), []));
check('safety: the adapter a bus is bound to', eq(kinds([{ section: 'connection', field: 'adapter', from: 'usb-1', to: null }]), ['bus']));
check('safety: acquisition switched off stops every consumer',
      eq(kinds([{ section: 'polling', field: 'enabled', from: null, to: false }]), ['acquisition-off'])
      && eq(kinds([{ section: 'polling', field: 'enabled', from: false, to: true }]), []));
check('safety: OPC UA off only when it was on (it is off by default)',
      eq(kinds([{ section: 'outputs', field: 'opcua.enabled', from: null, to: false }]), [])
      && eq(kinds([{ section: 'outputs', field: 'opcua.enabled', from: true, to: false }]), ['output-off']));
check('safety: OPC UA served without a block of its own (a device from before the choice) going off is reviewed',
      eq(S.safety([{ section: 'outputs', field: 'opcua.enabled', from: null, to: false }], { opcuaOn: true }).map(x => x.kind), ['output-off']));
check('safety: an output going on needs none', eq(kinds([{ section: 'outputs', field: 'mqtt.enabled', from: false, to: true }]), []));

// ── the dry run's verdict: the footer line and the Save label ────────────
const live = { changes: [{ section: 'identity', field: 'name', from: 'a', to: 'b' }], restart_required: false };
const restart = { changes: [{ section: 'connection', field: 'unit_id', from: 1, to: 2 }, { section: 'identity', field: 'name' }],
                  restart_required: true };
check('verdict: none / live / restart / error / checking',
      S.verdict(null) === 'none' && S.verdict(live) === 'live' && S.verdict(restart) === 'restart'
      && S.verdict({ error: { errors: ['x'] } }) === 'error' && S.verdict({ pending: true }) === 'checking'
      && S.verdict({ changes: [], restart_required: false }) === 'none');
check('label: a live change saves as "Save"', S.saveLabel(live, t) === 'Save');
check('label: a restart says so on the button', S.saveLabel(restart, t) === 'Save & restart acquisition');
check('label: nothing changed, an error, a check running: "Save"', S.saveLabel(null, t) === 'Save' && S.saveLabel({ pending: true }, t) === 'Save');
check('summary: N changes · applies live', S.summaryText(live, tn) === '1 changes · applies live', S.summaryText(live, tn));
check('summary: N changes · restarts acquisition', S.summaryText(restart, tn) === '2 changes · restarts acquisition');
check('summary: no changes', S.summaryText(null, tn) === 'No changes');
check('summary: an error says how many problems', S.summaryText({ error: { errors: ['a', 'b'] } }, tn) === '2 problems to fix before saving');
check('summary: a stalled device reconnects even with no change',
      S.verdict({ changes: [], restart_required: true }) === 'restart'
      && S.summaryText({ changes: [], restart_required: true }, tn) === 'No changes · it reconnects (enabled, not running)');

// ── where a refusal goes ─────────────────────────────────────────────────
check('errors: a field path → its section (server and client words alike)',
      S.sectionOf('outputs.rest_push.url') === 'outputs' && S.sectionOf('connection') === 'connection'
      && S.sectionOf('name') === 'identity' && S.sectionOf('template') === 'model' && S.sectionOf('poll_groups.fast') === 'polling');

// ── steps mode (Add, L3.5): the rail's items ─────────────────────────────
const steps = [{ id: 'what', label: 'What is it?' }, { id: 'meters', label: 'Meters' }, { id: 'save', label: 'Name & save' }];
const rail = S.railItems(steps, 'meters');
check('steps: each step numbered, in order', eq(rail.map(r => [r.id, r.n]), [['what', 1], ['meters', 2], ['save', 3]]));
check('steps: before the current one done (reachable), the current one current, the rest to come',
      eq(rail.map(r => r.state), ['done', 'current', 'todo']), rail);
check('steps: an unknown current = the first', S.railItems(steps, 'nope')[0].state === 'current');
check('steps: the line a phone shows', S.stepLine(steps, 'meters', (k, d, p) => fill(d, p)) === 'Step 2 of 3 · Meters');

// ── L3.11b (ADR-0032 §6): one way to edit — every kind in the modal ──────
const EDIT_SECTIONS_PRIMARY = { connection: ['protocol', 'host', 'port', 'unit_id', 'timeout', 'retry_attempts', 'retry_delay'],
                                polling: ['poll_groups'], identity: [], outputs: ['http_output', 'opcua', 'rest_push'], model: [] };
const primary = { ...rtu, id: 'umg512', primary: true, protocol: 'tcp', template: 'janitza_umg512_pro',
  connection: { protocol: 'tcp', host: '10.0.0.5', port: 502, unit_id: 1, timeout: 3 },
  edit_sections: EDIT_SECTIONS_PRIMARY,
  locks: [{ field: 'connection.protocol', code: 'primary', text: 'connection.protocol: the primary is Modbus TCP' },
          { field: 'identity.name', code: 'primary', text: 'fixed' }, { field: 'identity.type', code: 'primary', text: 'fixed' },
          { field: 'template', code: 'primary', text: 'fixed' }, { field: 'polling.enabled', code: 'primary', text: 'always polls' },
          { field: 'outputs.mqtt', code: 'primary', text: 'always MQTT' }, { field: 'outputs.influxdb', code: 'primary', text: 'always InfluxDB' },
          { field: 'outputs.ha_discovery', code: 'primary', text: 'Settings → MQTT' }] };
const unit = { ...rtu, id: 'sun-u1', name: 'Inverter 1', endpoint_id: 'sunfield', endpoint_name: 'Sunfield', edit_sections: {},
  locks: [{ field: 'device', code: 'installation_unit', installation: 'sunfield', text: 'set on the installation' }] };
const comp = { ...rtu, id: 'gx-bat-1', name: 'Battery 1', component_of: 'gx', parent_name: 'Victron GX', parent_enabled: true,
  edit_sections: {}, locks: [{ field: 'device', code: 'component', parent: 'gx', text: 'set on its device' }] };
const attrs = { identity: { description: 'Roof string A', labels: ['pv', 'roof'], manufacturer: 'Deye' } };
for (const fn of ['scopeOf', 'attrPayload', 'unitPayload', 'componentPayload', 'unitOutputWrites', 'pqPayload', 'savePlan', 'localChanges', 'saveReport']) {
  check(`L3.11b model: ${fn}() exists`, typeof S[fn] === 'function');
  // the checks below still run, and fail
  if (typeof S[fn] !== 'function') S[fn] = ['unitOutputWrites', 'savePlan', 'localChanges'].includes(fn) ? () => [] : () => ({});
}

check('L3.11b sections: the primary — only the sections its edit_sections hold, Identity (its attributes) too, no Model',
      eq(S.sectionsFor(primary), ['connection', 'polling', 'identity', 'outputs']), S.sectionsFor(primary));
check('L3.11b sections: the primary from an older gateway (no edit_sections) — the same',
      eq(S.sectionsFor({ ...primary, edit_sections: undefined }), ['connection', 'polling', 'identity', 'outputs']));
check('L3.11b sections: a unit opens in the modal', eq(S.sectionsFor(unit), ['connection', 'polling', 'identity', 'outputs']), S.sectionsFor(unit));
check('L3.11b sections: a component opens in the modal', eq(S.sectionsFor(comp), ['connection', 'polling', 'identity', 'outputs']), S.sectionsFor(comp));
check('L3.11b scope: a device edits every section of its own', ['model', 'connection', 'polling', 'identity', 'outputs'].every(s => S.scopeOf(rtu, s).own));
check('L3.11b scope: a unit — its name and attributes its own; connection and polling set on its installation, with the way there',
      S.scopeOf(unit, 'identity').own && S.scopeOf(unit, 'connection').held === 'installation' && S.scopeOf(unit, 'connection').id === 'sunfield'
      && S.scopeOf(unit, 'connection').name === 'Sunfield' && S.scopeOf(unit, 'polling').held === 'installation', S.scopeOf(unit, 'connection'));
check('L3.11b scope: a unit\'s outputs are its installation\'s, shared by every unit (edited here, said so)',
      S.scopeOf(unit, 'outputs').shared === 'installation' && S.scopeOf(unit, 'outputs').name === 'Sunfield', S.scopeOf(unit, 'outputs'));
check('L3.11b scope: a component — name, on/off and attributes its own; connection and outputs set on its device',
      S.scopeOf(comp, 'identity').own && S.scopeOf(comp, 'polling').own && S.scopeOf(comp, 'connection').held === 'parent'
      && S.scopeOf(comp, 'connection').id === 'gx' && S.scopeOf(comp, 'connection').name === 'Victron GX' && S.scopeOf(comp, 'outputs').held === 'parent');

// identity: name, type and the attributes' identity category (one Save)
const fa = S.fieldsOf(rtu, groups, attrs);
check('L3.11b identity: the attributes\' identity fields are in the draft (blank where none)',
      fa['about.description'] === 'Roof string A' && eq(fa['about.labels'], ['pv', 'roof']) && fa['about.manufacturer'] === 'Deye'
      && fa['about.location'] === '' && fa['about.serial_number'] === '' && fa['about.installed_on'] === '', fa);
check('L3.11b identity: an attribute path belongs to Identity', S.sectionOf('about.labels') === 'identity');
const da = S.set(S.draft(fa), 'about.description', 'Roof string B');
check('L3.11b identity: an attribute change is not part of the PATCH', S.payload(da) === null, S.payload(da));
check('L3.11b identity: an attribute change marks Identity', S.changedSections(da).has('identity'));
check('L3.11b attributes: one changed field — the whole category PUT, the others kept',
      eq(S.attrPayload(da), { method: 'PUT', value: { description: 'Roof string B', labels: ['pv', 'roof'], manufacturer: 'Deye' } }),
      S.attrPayload(da));
check('L3.11b attributes: nothing changed — nothing sent', S.attrPayload(S.draft(fa)) === null);
let dclr = S.draft(fa);
for (const p of ['about.description', 'about.manufacturer']) dclr = S.set(dclr, p, '');
dclr = S.set(dclr, 'about.labels', []);
check('L3.11b attributes: every field cleared — the category deleted (the server refuses it empty)',
      eq(S.attrPayload(dclr), { method: 'DELETE' }), S.attrPayload(dclr));
check('L3.11b attributes: a new category from nothing — PUT',
      eq(S.attrPayload(S.set(S.draft(S.fieldsOf(rtu, {}, {})), 'about.model', 'SUN-8K')), { method: 'PUT', value: { model: 'SUN-8K' } }));
check('L3.11b attributes: text is trimmed; a blank one is left out',
      eq(S.attrPayload(S.set(S.set(S.draft(S.fieldsOf(rtu, {}, {})), 'about.model', '  SUN-8K '), 'about.firmware', '  ')),
         { method: 'PUT', value: { model: 'SUN-8K' } }));

// the primary: the same PATCH (the server's adapter maps it)
const fp = S.fieldsOf(primary, { fast: { interval: 1 } }, {});
const dp = S.set(S.draft(fp), 'polling.poll_groups.fast', 2);
check('L3.11b primary: an interval goes in the PATCH, as for any device',
      eq(S.payload(dp, { connection: { protocol: 'tcp', host: '10.0.0.6', port: 502, unit_id: 1, timeout: 3 } }),
         { sections: { connection: { protocol: 'tcp', host: '10.0.0.6', port: 502, unit_id: 1, timeout: 3 }, polling: { poll_groups: { fast: { interval: 2 } } } } }));

// a unit: its name by its own route; a component: name and on/off by its device's
const du = S.set(S.draft(S.fieldsOf(unit, {}, {})), 'identity.name', 'Inverter one');
check('L3.11b unit: its name, by the unit route', eq(S.unitPayload(du), { name: 'Inverter one' }));
check('L3.11b unit: nothing changed — nothing', S.unitPayload(S.draft(S.fieldsOf(unit, {}, {}))) === null);
const dc = S.set(S.set(S.draft(S.fieldsOf(comp, {}, {})), 'identity.name', 'Bat 1'), 'polling.enabled', false);
check('L3.11b component: name and on/off, by its device\'s route', eq(S.componentPayload(dc), { name: 'Bat 1', enabled: false }));
check('L3.11b component: nothing changed — nothing', S.componentPayload(S.draft(S.fieldsOf(comp, {}, {}))) === null);

// a unit's outputs: its installation's, by the routes that set them for every unit
let duo = S.draft(S.fieldsOf(unit, {}, {}));
duo = S.set(duo, 'outputs.http_output', true);
duo = S.set(duo, 'outputs.rest_push.url', 'https://x/y');
duo = S.set(duo, 'outputs.influxdb.write_interval', 30);
const uw = S.unitOutputWrites(duo);
check('L3.11b unit outputs: HTTP, REST push and publishing, each by its route, in a fixed order',
      eq(uw.map(w => w.route), ['http-output', 'rest-push', 'publishing']), uw);
check('L3.11b unit outputs: the bodies those routes take',
      eq(uw[0]?.body, { enabled: true }) && uw[1]?.body?.url === 'https://x/y' && uw[1]?.body?.enabled === false
      && eq(uw[2]?.body, { influxdb: { write_interval: 30 } }), uw);
check('L3.11b unit outputs: nothing changed — no write', S.unitOutputWrites(S.draft(S.fieldsOf(unit, {}, {}))).length === 0);

// the PQ recorder: staged with the rest (L3-F21), by its own route
const pqd = { enabled: false, poll_s: 300, archive_waveforms: true, base_url: '' };
const fq = S.fieldsOf(primary, {}, {}, pqd);
check('L3.11b PQ: its settings are in the draft (Outputs)', fq['pq.enabled'] === false && fq['pq.poll_s'] === 300
      && S.sectionOf('pq.poll_s') === 'outputs');
const dq = S.set(S.draft(fq), 'pq.enabled', true);
check('L3.11b PQ: a change — its whole settings for its route; not in the PATCH',
      eq(S.pqPayload(dq), { enabled: true, poll_s: 300, archive_waveforms: true, base_url: '' }) && S.payload(dq) === null);
check('L3.11b PQ: no recorder (not offered) — no field, nothing', !('pq.enabled' in S.fieldsOf(rtu, {}, {})) && S.pqPayload(S.draft(fa)) === null);

// one Save: the writes it takes, in order
let dall = S.set(S.set(S.draft(S.fieldsOf(rtu, groups, attrs, pqd)), 'identity.name', 'X'), 'about.model', 'M');
dall = S.set(dall, 'pq.poll_s', 600);
check('L3.11b save: a device — the PATCH first, then the attributes, then the PQ recorder',
      eq(S.savePlan(dall, rtu).map(s => s.id), ['device', 'attributes', 'pq']), S.savePlan(dall, rtu));
check('L3.11b save: only attributes changed — only their write', eq(S.savePlan(da, rtu).map(s => s.id), ['attributes']));
let duall = S.set(S.set(du, 'about.model', 'M'), 'outputs.http_output', true);
check('L3.11b save: a unit — its name, its installation\'s outputs, its attributes',
      eq(S.savePlan(duall, unit).map(s => s.id), ['unit', 'outputs', 'attributes']), S.savePlan(duall, unit));
check('L3.11b save: a component — its device\'s route, then its attributes',
      eq(S.savePlan(S.set(dc, 'about.model', 'M'), comp).map(s => s.id), ['component', 'attributes']));
check('L3.11b save: each write names the fields it carries',
      eq(S.savePlan(dall, rtu).find(s => s.id === 'attributes')?.paths, ['about.model']));
check('L3.11b save: nothing changed — nothing to write', S.savePlan(S.draft(fa), rtu).length === 0);

// what the server's dry run does not see: said by the page (the footer, the diff)
check('L3.11b changes: a device — the attributes and the PQ recorder are said by the page',
      eq(S.localChanges(dall, rtu).map(c => `${c.section}:${c.field}`), ['identity:about.model', 'outputs:pq.poll_s']), S.localChanges(dall, rtu));
check('L3.11b changes: a unit — every change is said by the page (no dry run for it)',
      eq(S.localChanges(duall, unit).map(c => c.field), ['about.model', 'identity.name', 'outputs.http_output']), S.localChanges(duall, unit));

// what was saved and what was not, when a later write fails
const rep = S.saveReport(S.savePlan(dall, rtu), { device: { ok: true }, attributes: { ok: false, error: 'HTTP 412' } });
check('L3.11b partial: saved, failed and not tried, each named',
      eq(rep.saved, ['device']) && rep.failed && rep.failed.id === 'attributes' && rep.failed.error === 'HTTP 412' && eq(rep.skipped, ['pq']), rep);
check('L3.11b partial: the edits left are the failed and the skipped writes\' fields',
      eq(rep.left, ['about.model', 'pq.poll_s']), rep);
check('L3.11b partial: all saved', S.saveReport(S.savePlan(da, rtu), { attributes: { ok: true } }).failed === null);

// ── L3.11b review ────────────────────────────────────────────────────────
for (const fn of ['localErrors', 'rebaseRows']) {
  check(`review: ${fn}() exists`, typeof S[fn] === 'function');
  if (typeof S[fn] !== 'function') S[fn] = fn === 'rebaseRows' ? () => ({ rows: [], conflicts: [] }) : () => ({});
}
// m5: the PQ recorder's interval is judged before it is sent (the old form: a number, at least 30 s)
const pqd2 = S.draft(S.fieldsOf(primary, {}, {}, { enabled: true, poll_s: 300, archive_waveforms: true, base_url: '' }));
for (const [v, bad] of [[null, true], ['abc', true], [10, true], [0, true], [30, false], [600, false]]) {
  const e = S.localErrors(S.set(pqd2, 'pq.poll_s', v), primary);
  check(`review m5: PQ interval ${JSON.stringify(v)} ${bad ? 'refused at its field' : 'taken'}`, bad ? !!e['pq.poll_s'] : !e['pq.poll_s'], e);
}
check('review m5: a refused interval is never sent as a number it is not', (() => {
  const d = S.set(pqd2, 'pq.poll_s', null);
  return !!S.localErrors(d, primary)['pq.poll_s'];
})());
// m9: a unit and a component have no dry run: what their routes refuse is judged here first
const du9 = S.set(S.draft(S.fieldsOf(unit, {}, {})), 'outputs.rest_push.enabled', true);
check('review m9: a unit\'s REST push on without a URL — refused at the URL, before any write',
      !!S.localErrors(du9, unit)['outputs.rest_push.url'], S.localErrors(du9, unit));
check('review m9: …with a URL that is not http(s) — refused',
      !!S.localErrors(S.set(du9, 'outputs.rest_push.url', 'ftp://x'), unit)['outputs.rest_push.url']);
check('review m9: …with a URL — taken', !S.localErrors(S.set(du9, 'outputs.rest_push.url', 'https://x/y'), unit)['outputs.rest_push.url']);
check('review m9: an interval under 5 s — refused', !!S.localErrors(S.set(S.set(du9, 'outputs.rest_push.url', 'https://x/y'), 'outputs.rest_push.interval_s', 2), unit)['outputs.rest_push.interval_s']);
check('review m9: a name over 80 characters — refused (unit, component)',
      !!S.localErrors(S.set(S.draft(S.fieldsOf(unit, {}, {})), 'identity.name', 'x'.repeat(81)), unit)['identity.name']
      && !!S.localErrors(S.set(S.draft(S.fieldsOf(comp, {}, {})), 'identity.name', 'x'.repeat(81)), comp)['identity.name']);
check('review m9: nothing changed — nothing refused', !Object.keys(S.localErrors(S.draft(S.fieldsOf(unit, {}, {})), unit)).length);
// R2-6: the page's REST push check is the server's (one table for both:
// tests/fixtures/rest_push_rule_cases.json, tests/test_rest_push_rule.py)
{
  const fs = require('fs');
  const cases = JSON.parse(fs.readFileSync(new URL('../../tests/fixtures/rest_push_rule_cases.json', import.meta.url), 'utf8')).cases;
  for (const c of cases) {
    let d = S.draft(S.fieldsOf(unit, {}, {}));
    d = S.set(S.set(S.set(d, 'outputs.rest_push.enabled', c.enabled), 'outputs.rest_push.url', c.url), 'outputs.rest_push.interval_s', c.interval_s);
    const e = S.localErrors(d, unit);
    check(`review R2-6: ${JSON.stringify([c.enabled, c.url, c.interval_s])} — the server's verdict (url ${c.url_ok}, interval ${c.interval_ok})`,
          !e['outputs.rest_push.url'] === c.url_ok && !e['outputs.rest_push.interval_s'] === c.interval_ok, e);
  }
}
// M2: the full Measurements editor's rows, re-applied over the selection stored now after a 412
const rkey = r => String(r.address);
const rbase = [{ address: 1, name: 'a', label: 'A' }, { address: 2, name: 'b', label: 'B' }, { address: 3, name: 'c', label: 'C' }];
const rmine = [{ address: 1, name: 'a', label: 'A mine' }, { address: 3, name: 'c', label: 'C' }, { address: 4, name: 'd', label: 'D' }];
const rstored = [{ address: 1, name: 'a', label: 'A theirs' }, { address: 2, name: 'b', label: 'B' }, { address: 3, name: 'c', label: 'C theirs' }, { address: 5, name: 'e', label: 'E' }];
const rr = S.rebaseRows(rbase, rmine, rstored, rkey);
check('review M2: my untick (2), my tick (4) and my edit (1) over the stored rows; theirs (3 changed, 5 added) taken',
      eq(rr.rows.map(rkey).sort(), ['1', '3', '4', '5']) && rr.rows.find(r => r.address === 1).label === 'A mine'
      && rr.rows.find(r => r.address === 3).label === 'C theirs', rr.rows);
check('review M2: a row both changed is a conflict (mine kept, theirs said)',
      rr.conflicts.length === 1 && rr.conflicts[0].key === '1' && rr.conflicts[0].stored.label === 'A theirs', rr.conflicts);
check('review M2: a row I unticked that they changed — a conflict, my untick kept',
      (() => { const r2 = S.rebaseRows(rbase, rmine, [{ ...rbase[0] }, { address: 2, name: 'b', label: 'B theirs' }, rbase[2]], rkey);
               return !r2.rows.some(r => r.address === 2) && r2.conflicts.some(c => c.key === '2'); })());

// a 412 on the attributes or the device: the edits kept over the version stored now
const rb2 = S.rebase(da, S.fieldsOf(rtu, groups, { identity: { description: 'Changed elsewhere', manufacturer: 'Deye' } }));
check('L3.11b 412: an attribute both changed — mine kept, theirs said',
      rb2.draft.cur['about.description'] === 'Roof string B' && rb2.conflicts.length === 1 && rb2.conflicts[0].stored === 'Changed elsewhere'
      && eq(rb2.draft.cur['about.labels'], []), rb2);

console.log(`\n${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
