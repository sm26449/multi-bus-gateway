// Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
// Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
//
// The new Add Device's model (ui/js/adddev-model.js, ADR-0031 §2, L3.5), with
// no browser: the steps of each way in, the type chips and their counts, the
// search (model, brand, type, synonyms; case and diacritics never matter),
// the badges a card wears, what Next needs, the dirty test, the Found inbox,
// and the measured path's create — byte-identical to today's meter-only
// create (app-meters.js _devWizCreateMeterOnly at e16386f).
//
//   node tools/e2e/adddev_model_test.mjs
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const M = require('../../ui/js/adddev-model.js');

let failed = 0, n = 0;
const check = (name, cond, extra = '') => {
  n++; if (!cond) failed++;
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${!cond && extra !== '' ? ' — ' + JSON.stringify(extra) : ''}`);
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const t = (k, d) => d;

// ── the catalog (as GET /api/device-types answers it) ────────────────────
const catalog = { types: [
  { id: 'meter', builtin: true, labels: { en: 'Meter', ro: 'Contor' }, behaves_like: 'meter', templates: 3 },
  { id: 'inverter', builtin: true, labels: { en: 'Inverter', ro: 'Invertor' }, behaves_like: 'inverter', templates: 1 },
  { id: 'battery', builtin: true, labels: { en: 'Battery', ro: 'Baterie' }, behaves_like: 'battery', templates: 1 },
  { id: 'heat_pump', builtin: true, labels: { en: 'Heat pump', ro: 'Pompă de căldură' }, behaves_like: 'heat_pump', templates: 1 },
  { id: 'ups', builtin: true, labels: { en: 'UPS', ro: 'UPS' }, behaves_like: 'ups', templates: 0 },
  { id: 'other', builtin: true, labels: { en: 'Other', ro: 'Altele' }, behaves_like: 'other', templates: 0 },
  { id: 'chiller', builtin: false, labels: { en: 'Chiller', ro: 'Răcitor' }, behaves_like: 'heat_pump', templates: 2 },
  { id: 'boiler_room', builtin: false, labels: { en: 'Boiler room' }, behaves_like: 'other', templates: 0 },
] };
const kindWords = { meter: 'meter contor', inverter: 'inverter invertor', battery: 'battery bms baterie acumulator',
  heat_pump: 'heat pump pompa caldura', ups: 'ups', other: 'other altele' };

// ── the templates (as GET /api/device-templates lists them) ─────────────
const tpl = (o) => Object.assign({ builtin: true, transport: 'modbus', notes: [], ports: ['serial', 'ethernet'],
  listen_only: false, setup: { count: 0, writes: [] } }, o);
const templates = [
  tpl({ id: 'eastron_sdm630', name: 'Eastron SDM630', vendor: 'Eastron', model: 'SDM630', device_kind: 'meter' }),
  tpl({ id: 'eastron_sdm120', name: 'Eastron SDM120', vendor: 'Eastron', model: 'SDM120', device_kind: 'meter' }),
  tpl({ id: 'janitza_umg512', name: 'Janitza UMG 512-PRO', vendor: 'Janitza', model: 'UMG 512', device_kind: 'meter',
        ports: ['ethernet'], notes: [{ level: 'info', text: 'Read over Modbus TCP.' }, { level: 'warning', text: 'Firmware 5 or later.' }] }),
  tpl({ id: 'deye_sun', name: 'Deye SUN 8K', vendor: 'Deye', model: 'SUN-8K-SG05LP1', device_kind: 'inverter', ports: ['serial'] }),
  tpl({ id: 'seplos_v2_tap', name: 'Seplos V2 — listen on the inter-pack bus', vendor: 'Seplos', device_kind: 'battery',
        ports: ['serial'], listen_only: true }),
  tpl({ id: 'samsung_mim_b19n', name: 'Samsung MIM-B19N (DVM/EHS)', vendor: 'Samsung', device_kind: 'heat_pump',
        ports: ['serial'], setup: { count: 2, writes: [{ fc: 16, address: 52 }, { fc: 16, address: 53 }] } }),
  tpl({ id: 'carrier_chiller', name: 'Carrier 30RB', vendor: 'Carrier', device_kind: 'other', types: ['chiller'] }),
  tpl({ id: 'victron_gx', name: 'Victron GX', vendor: 'Victron', device_kind: 'site', transport: 'mqtt', ports: ['mqtt'],
        components: [{ type: 'battery', name: 'Battery', kind: 'battery' }] }),
  // a part of another device: never added alone
  tpl({ id: 'victron_gx_battery', name: 'Victron GX battery', vendor: 'Victron', device_kind: 'battery',
        component_of: 'victron_gx', component_key: 'battery' }),
];
const ctx = { catalog, kindWords };

// ── the ways in, and their steps ─────────────────────────────────────────
check('paths: pick a model = What · Connection · Check · Name & save',
      eq(M.stepsOf('model'), ['what', 'connection', 'check', 'save']), M.stepsOf('model'));
check('paths: by connection swaps the first two steps',
      eq(M.stepsOf('connection'), ['connection', 'what', 'check', 'save']), M.stepsOf('connection'));
check('paths: measured = What (its type) · Meters · Name & save',
      eq(M.stepsOf('measured'), ['what', 'meters', 'save']), M.stepsOf('measured'));
check('paths: many of the same = the connection with its scan, then the units (L3.7)', eq(M.stepsOf('many'), ['many', 'units']), M.stepsOf('many'));
check('paths: an unknown way is the default one', eq(M.stepsOf('nope'), M.stepsOf('model')));
check('paths: a copy (a caller changing it changes nothing here)', (() => { const a = M.stepsOf('model'); a.pop(); return M.stepsOf('model').length === 4; })());

// the state on Back: nothing lost
let s = M.blank();
check('blank: the default way, on its first step, nothing chosen', s.path === 'model' && s.step === 'what' && !M.isDirty(s));
s.q = 'sdm';
check('dirty: a search alone is nothing to lose', !M.isDirty(s));
check('dirty: a type chosen is', M.isDirty(Object.assign(M.blank(), { type: 'meter' })));
check('dirty: words typed in the + Add type… form are', M.isDirty(Object.assign(M.blank(), { typeDraft: 'Chil' })));
s.type = 'meter'; s.template = 'eastron_sdm630';
check('dirty: a model chosen is something to lose', M.isDirty(s));
s = M.setPath(s, 'measured');
check('setPath: another way keeps what was chosen', s.path === 'measured' && s.step === 'what' && s.q === 'sdm' && s.template === 'eastron_sdm630');
s.kind = 'ups';
s = M.go(s, +1);
check('go: Next moves along the way', s.step === 'meters');
s.meters = [{ role: 'input', device: 'em1', power: 'power_active_total', sign: 'auto' }];
s = M.go(s, -1);
check('go: Back keeps the meters chosen on the step left', s.step === 'what' && s.meters.length === 1 && s.kind === 'ups');
s = M.go(s, -1);
check('go: Back from the first step stays there', s.step === 'what');
s = M.go(M.go(M.go(s, +1), +1), +1);
check('go: Next from the last step stays there', s.step === 'save');
s = M.setPath(s, 'model');
check('setPath: back to the models keeps the measured answers too', s.kind === 'ups' && s.meters.length === 1 && s.step === 'what');
check('index: the step\'s place and the count', eq(M.position(M.setPath(M.blank(), 'measured')), { n: 1, of: 3 }));

// ── type chips: Any first, built-ins then the user's, the counts ────────
const chips = M.typeChips(templates, catalog);
check('chips: Any first, counting every model that can be added alone', chips.chips[0].id === '' && chips.chips[0].count === 8, chips.chips[0]);
check('chips: built-ins in the catalog order, then the user\'s types',
      eq(chips.chips.map(c => c.id), ['', 'meter', 'inverter', 'battery', 'heat_pump', 'other', 'chiller', 'boiler_room']), chips.chips.map(c => c.id));
check('chips: counts from the list itself (a part is not counted)',
      eq(chips.chips.map(c => c.count), [8, 3, 1, 1, 1, 1, 2, 1]), chips.chips.map(c => c.count));
check('chips: a user type behaving like a built-in counts that built-in\'s models and its own',
      chips.chips.find(c => c.id === 'chiller').count === 2);
check('chips: a type with no model sits behind "More types"', eq(chips.more.map(c => c.id), ['ups']), chips.more);
check('chips: a user type behaving like Other counts Other\'s models (as the server counts)',
      chips.chips.find(c => c.id === 'boiler_room').count === 1);
check('chips: user types are marked', chips.chips.find(c => c.id === 'chiller').builtin === false
      && chips.chips.find(c => c.id === 'meter').builtin === true);

// ── the list: by type, by search ─────────────────────────────────────────
const ids = list => list.map(x => x.id);
check('list: a part of another device is never offered alone', !ids(M.filter(templates, {}, ctx)).includes('victron_gx_battery'));
check('list: Any lists every model, by vendor then name',
      eq(ids(M.filter(templates, {}, ctx)), ['carrier_chiller', 'deye_sun', 'eastron_sdm120', 'eastron_sdm630',
        'janitza_umg512', 'samsung_mim_b19n', 'seplos_v2_tap', 'victron_gx']), ids(M.filter(templates, {}, ctx)));
check('list: a type narrows to its models', eq(ids(M.filter(templates, { type: 'meter' }, ctx)), ['eastron_sdm120', 'eastron_sdm630', 'janitza_umg512']));
check('list: a user type = its built-in\'s models and the ones that list it',
      eq(ids(M.filter(templates, { type: 'chiller' }, ctx)), ['carrier_chiller', 'samsung_mim_b19n']), ids(M.filter(templates, { type: 'chiller' }, ctx)));
check('search: a model', eq(ids(M.filter(templates, { q: 'sdm630' }, ctx)), ['eastron_sdm630']));
check('search: a brand, any case', eq(ids(M.filter(templates, { q: 'EASTRON' }, ctx)), ['eastron_sdm120', 'eastron_sdm630']));
check('search: every word must match', eq(ids(M.filter(templates, { q: 'eastron 120' }, ctx)), ['eastron_sdm120']));
check('search: the type in English', eq(ids(M.filter(templates, { q: 'heat pump' }, ctx)), ['samsung_mim_b19n']));
check('search: the type in Romanian, without diacritics', eq(ids(M.filter(templates, { q: 'pompa' }, ctx)), ['samsung_mim_b19n']));
check('search: the type in Romanian, with diacritics', eq(ids(M.filter(templates, { q: 'căldură' }, ctx)), ['samsung_mim_b19n']));
check('search: a synonym (bms → a battery)', eq(ids(M.filter(templates, { q: 'bms' }, ctx)), ['seplos_v2_tap']));
check('search: a user type\'s words find the models that list it', ids(M.filter(templates, { q: 'răcitor' }, ctx)).includes('carrier_chiller'));
check('search: within a type', eq(ids(M.filter(templates, { type: 'meter', q: 'janitza' }, ctx)), ['janitza_umg512']));
check('search: nothing found is an empty list', M.filter(templates, { q: 'zzz' }, ctx).length === 0);
check('search: only blanks = no search', M.filter(templates, { q: '   ' }, ctx).length === 8);

// ── what a card wears ────────────────────────────────────────────────────
const by = id => templates.find(x => x.id === id);
check('badges: none on a plain meter', eq(M.badges(by('eastron_sdm630')), []));
check('badges: Only listens', eq(M.badges(by('seplos_v2_tap')), ['listens']));
check('badges: Needs setup writes', eq(M.badges(by('samsung_mim_b19n')), ['setup']));
check('badges: Has parts', eq(M.badges(by('victron_gx')), ['parts']));
check('badges: Note', eq(M.badges(by('janitza_umg512')), ['note']));
check('ports: the summary\'s, in its order, each with an icon',
      eq(M.ports(by('eastron_sdm630')).map(p => p.id), ['serial', 'ethernet']) && M.ports(by('eastron_sdm630')).every(p => /^bi-/.test(p.icon)));
check('ports: none declared = none drawn', eq(M.ports({}), []));
check('notes: their texts, joined', M.noteText(by('janitza_umg512')) === 'Read over Modbus TCP. · Firmware 5 or later.');
check('notes: a warning among them is marked', M.noteWarn(by('janitza_umg512')) && !M.noteWarn(tpl({ notes: [{ level: 'info', text: 'x' }] })));
check('notes: none = no Note badge, no text', eq(M.badges(tpl({ notes: [] })), []) && M.noteText(tpl({})) === '');

// ── Next ─────────────────────────────────────────────────────────────────
check('next: step 1 with nothing picked says why', eq(M.canNext(M.blank()), { ok: false, why: 'pick' }));
check('next: a model picked', M.canNext(Object.assign(M.blank(), { template: 'eastron_sdm630' })).ok);
const ms = M.setPath(M.blank(), 'measured');
check('next: measured, no type yet', eq(M.canNext(ms), { ok: false, why: 'kind' }));
check('next: measured, a type', M.canNext(Object.assign(ms, { kind: 'ups' })).ok);
check('next: the connection step is the page\'s to judge (its form, then the gateway)', M.canNext(M.go(Object.assign(M.blank(), { template: 'x' }), +1)).ok);

// ── the measured path ────────────────────────────────────────────────────
check('kinds: the gateway\'s first, then more, then the user\'s that are not meters',
      eq(M.measuredKinds({ meter_only_kinds: { first: ['ups', 'charger'], more: ['drive'] } }, catalog),
         { first: ['ups', 'charger'], more: ['drive', 'chiller', 'boiler_room'] }));
const errs = M.measuredErrors({ kind: '', meters: [] }, t);
check('measured: no type, no meter: both said at their fields', 'kind' in errs && 'meters[0].device' in errs, errs);
const errs2 = M.measuredErrors({ kind: 'ups', meters: [{ role: '', device: 'em1' }, { role: 'output', device: '' }, { role: 'input', device: 'em2' }] }, t);
check('measured: a row without a role, a meter, or anything it reads',
      eq(Object.keys(errs2).sort(), ['meters[0].power', 'meters[0].role', 'meters[1].device', 'meters[2].power']), errs2);
check('measured: two complete rows are fine', eq(M.measuredErrors({ kind: 'ups', meters: [
  { role: 'input', device: 'em1', power: 'p' }, { role: 'output', device: 'em2', energy: 'e' }] }, t), {}));
check('stepOf: a refusal goes back to the step that owns its field',
      M.stepOfField('kind', 'measured') === 'what' && M.stepOfField('meters[1].role', 'measured') === 'meters'
      && M.stepOfField('id', 'measured') === 'save' && M.stepOfField('name', 'measured') === 'save');
check('id: from the name, a-z 0-9 -, diacritics folded', M.idFromName('Pompă căldură — Hală 2') === 'pompa-caldura-hala-2');
check('id: at most 32, never ending in -', M.idFromName('a'.repeat(31) + ' b c') === 'a'.repeat(31));
check('id: valid', M.idValid('ups-2') && !M.idValid('U') && !M.idValid('-x') && !M.idValid(''));

// byte-identical: today's meter-only create, as app-meters.js builds it
function todayPayload(d) {
  const payload = {
    id: d.id, name: d.name || d.id, kind: d.kind, enabled: d.enabled,
    connection: { protocol: 'none' }, meters: d.meters || [],
    mqtt: d.topic_prefix ? { topic_prefix: d.topic_prefix } : {},
    influxdb: { bucket: d.bucket || undefined, device_tag: d.device_tag || undefined },
    ha_discovery_enabled: d.ha_discovery_enabled,
  };
  return JSON.stringify(payload);
}
const two = [{ role: 'input', device: 'em1', power: 'power_active_total', energy: 'energy_active_import', sign: 'auto' },
             { role: 'output', device: 'em2', power: 'power_active_total', sign: 'auto' }];
// what the old wizard's data held for a device named on its step 3 with
// everything else left as it opened (enabled, HA on, routing blank)
const oldData = { id: 'ups-rack', name: 'UPS rack', template: '', enabled: true, protocol: 'none',
                  topic_prefix: '', bucket: '', device_tag: '', ha_discovery_enabled: true, kind: 'ups', meters: two };
const st = Object.assign(M.setPath(M.blank(), 'measured'), { kind: 'ups', meters: two, name: 'UPS rack', id: 'ups-rack' });
check('payload: byte-identical to today\'s meter-only create', JSON.stringify(M.meterOnlyPayload(M.measuredDraft(st))) === todayPayload(oldData),
      [JSON.stringify(M.meterOnlyPayload(M.measuredDraft(st))), todayPayload(oldData)]);
for (const d of [
  { ...oldData, topic_prefix: 'site/ups', bucket: 'ups', device_tag: 'u1', ha_discovery_enabled: false, enabled: false },
  { ...oldData, name: '', meters: undefined },
  { ...oldData, kind: 'chiller', meters: [two[0]] },
]) {
  check(`payload: byte-identical for ${JSON.stringify({ name: d.name, tp: d.topic_prefix, m: (d.meters || []).length })}`,
        JSON.stringify(M.meterOnlyPayload(d)) === todayPayload(d), [JSON.stringify(M.meterOnlyPayload(d)), todayPayload(d)]);
}
check('draft: the name is the id when none is given', M.measuredDraft(Object.assign(M.blank(), { id: 'x1', name: '' })).name === '');
const mdr = M.measuredDryRunBody({ kind: 'ups', meters: two });
check('dry run: the meters and the type checked, nothing stored, nothing on the bus',
      mdr.kind === 'ups' && mdr.enabled === false && eq(mdr.connection, { protocol: 'none' }) && eq(mdr.meters, two), mdr);
check('dry run: no probe id of old (L3-F3: x-dry-run gone); a name the id is derived from instead',
      !JSON.stringify(mdr).includes('x-dry-run') && M.idValid(mdr.id), mdr);
check('dry run: the id the user typed is the one judged', M.measuredDryRunBody({ kind: 'ups', meters: two, id: 'ups-rack' }).id === 'ups-rack');

// ── Found: what the gateway already heard of, passively ─────────────────
const mqtt = { items: [
  { key: 'n1', state: 'new', name: 'Shelly 3EM', template: 'shelly_3em', device_id: 'shelly-3em-1' },
  { key: 'n2', state: 'in_mbg', name: 'Ruuvi', template: 'ruuvi' },
  { key: 'n3', state: 'ignored', name: 'Old', template: 'x' },
  { key: 'n4', state: 'template_missing', name: 'ESP node', template: 'mbg_node_x' },
] };
// what "Find on the LAN" (Add bridge) answered earlier in this session
const bridges = [{ host: '192.168.1.50', port: 4196, type: 'rtu_transparent', type_name: 'RS-485 converter' },
                 { host: '192.168.1.51', port: 8091, type: 'mbg_bridge', bridge: 'b1' }];
const found = M.foundItems(mqtt, bridges);
check('found: new and template-missing MQTT devices, and bridges not added yet',
      eq(found.map(f => [f.source, f.key]), [['mqtt', 'n1'], ['mqtt', 'n4'], ['bridge', '192.168.1.50:4196']]), found);
check('found: nothing heard = an empty inbox (the chip is not shown)', M.foundItems(null, null).length === 0);
const mqtt2 = { items: [{ key: 'n1', state: 'new', name: 'Shelly 3EM', template: 'shelly_3em', device_id: 'shelly-3em-1',
  connection: { protocol: 'mqtt', broker: '@gateway', port: 1883, topic: 'tele/shelly', vars: { tele: 'tele/shelly' },
                availability: { topic: '{tele}/LWT', online: 'Online', offline: 'Offline' } } }] };
const f2 = M.foundItems(mqtt2, bridges);
check('found: an MQTT item carries its connection and template, a bridge its place in the LAN list',
      eq(f2[0].connection, mqtt2.items[0].connection) && f2[0].template === 'shelly_3em' && f2[1].idx === 0, f2);


// ══ L3.9: Connection → Check → Name & save, Create another, the ways in ══
const CM = require('../../ui/js/connform-model.js');
const CS = require('../../ui/js/conn-step-model.js');

// ── the steps each way takes, now every one built ───────────────────────
for (const [way, steps] of [['model', ['what', 'connection', 'check', 'save']], ['connection', ['connection', 'what', 'check', 'save']],
                            ['measured', ['what', 'meters', 'save']]]) {
  let st2 = Object.assign(M.setPath(M.blank(), way), { template: 'eastron_sdm630', kind: 'ups', meters: [{ device: 'em1' }] });
  const seen = [st2.step];
  for (let i = 0; i < 5; i++) { if (!M.canNext(st2).ok || M.canNext(st2).create) break; st2 = M.go(st2, +1); seen.push(st2.step); }
  check(`steps: ${way} walks ${steps.join(' → ')} with Next, Create on the last`, eq(seen, steps) && M.canNext(st2).create === true, seen);
}
check('next: the connection path asks the model second', eq(M.canNext(Object.assign(M.setPath(M.blank(), 'connection'), { step: 'what' })), { ok: false, why: 'pick' }));

// ── Check: never blocks; Next says when it goes on without one ──────────
const chk = (o) => Object.assign(M.blank(), { template: 'x', step: 'check' }, { check: Object.assign(M.blankCheck(), o) });
const okView = { state: 'done', mode: 'poll', values: [{ name: 'v', quality: 'ok' }, { name: 'p', quality: 'no_answer' }], groups: { total: 2, asked: 2, answered: 1, heard: 0 } };
const deadView = { state: 'done', mode: 'poll', values: [{ name: 'v', quality: 'no_answer' }], groups: { total: 1, asked: 1, answered: 0, heard: 0 } };
const heardView = { state: 'done', mode: 'listen', values: [{ name: 'soc', quality: 'ok' }], groups: { total: 5, asked: 0, answered: 0, heard: 3 } };
const deafView = { state: 'done', mode: 'listen', values: [], groups: { total: 5, asked: 0, answered: 0, heard: 0 } };
check('check: a value read = checked', M.checkVerdict(okView) === 'ok');
check('check: nothing answered = not checked', M.checkVerdict(deadView) === 'none');
check('check: listening, groups heard = checked', M.checkVerdict(heardView) === 'ok');
check('check: listening, nothing heard = not checked', M.checkVerdict(deafView) === 'none');
check('check: failed / cancelled / running', M.checkVerdict({ state: 'failed' }) === 'failed' && M.checkVerdict({ state: 'cancelled' }) === 'failed'
      && M.checkVerdict({ state: 'running' }) === 'running' && M.checkVerdict(null) === 'idle');
check('check: Next always possible — a failed check is "without a check"',
      M.canNext(chk({ state: 'refused' })).ok && M.canNext(chk({ state: 'refused' })).unchecked === true);
check('check: a skipped one too (the step left before its answer)', M.canNext(chk({ state: 'skipped' })).unchecked === true);
check('check: a good one is a plain Next', M.canNext(chk({ state: 'done', view: okView })).ok && !M.canNext(chk({ state: 'done', view: okView })).unchecked);
check('check: Next\'s words', M.nextLabel(chk({ state: 'refused' }), t) === 'Next — without a check'
      && M.nextLabel(chk({ state: 'done', view: okView }), t) === 'Next' && M.nextLabel(Object.assign(M.blank(), { template: 'x' }), t) === 'Next');
check('check: "heard n of m groups"', eq(M.heard(heardView), { n: 3, of: 5 }) && M.heard(okView) === null);
check('check: the values, ok first then by group', eq(M.checkRows({ values: [
  { name: 'a', quality: 'no_answer', group: 'slow' }, { name: 'b', quality: 'ok', group: 'slow' }, { name: 'c', quality: 'ok', group: 'fast' }] }).map(r => r.name), ['c', 'b', 'a']));

// ── identity: the nameplate from what the check read ─────────────────────
const idv = { identity: { template: 'x', fingerprint: 'match', sunspec: { manufacturer: 'Fronius', model: 'Symo 8.2', version: '1.2.3', serial: 'SN42' } } };
check('nameplate: SunSpec fills manufacturer, model, firmware, serial, each said from where',
      eq(M.nameplateFrom(idv), { manufacturer: { value: 'Fronius', from: 'sunspec' }, model: { value: 'Symo 8.2', from: 'sunspec' },
         firmware: { value: '1.2.3', from: 'sunspec' }, serial_number: { value: 'SN42', from: 'sunspec' } }), M.nameplateFrom(idv));
check('nameplate: FC43 when there is no SunSpec', eq(M.nameplateFrom({ identity: { fc43: { vendor: 'Eastron', product: 'SDM630', version: '2.1' } } }),
  { manufacturer: { value: 'Eastron', from: 'fc43' }, model: { value: 'SDM630', from: 'fc43' }, firmware: { value: '2.1', from: 'fc43' } }));
check('nameplate: nothing read = nothing pre-filled', eq(M.nameplateFrom({}), {}) && eq(M.nameplateFrom(null), {}));
const st3 = Object.assign(M.blank(), { identity: Object.assign(M.blank().identity, { manufacturer: 'typed' }) });
check('nameplate: a field typed in is never overwritten', M.applyNameplate(st3, idv).identity.manufacturer === 'typed'
      && M.applyNameplate(st3, idv).identity.serial_number === 'SN42');
check('identity: a mismatch names the model to use', eq(M.identityVerdict({ identity: { fingerprint: 'mismatch', suggested: 'eastron_sdm120', suggested_name: 'SDM120' } }),
  { state: 'mismatch', suggested: 'eastron_sdm120', name: 'SDM120' }));
check('identity: a match / nothing to tell', M.identityVerdict({ identity: { fingerprint: 'match' } }).state === 'match'
      && M.identityVerdict({ identity: null }).state === 'none');

// ── the create: today's payload, byte for byte, for the same choices ────
// the old wizard's (app-wizard.js devWizardNext at ab27652)
function todayCreate(d, conn) {
  return JSON.stringify({
    id: d.id, name: d.name || d.id, template: d.template, enabled: d.enabled,
    connection: conn,
    mqtt: d.topic_prefix ? { topic_prefix: d.topic_prefix } : {},
    influxdb: { bucket: d.bucket || undefined, device_tag: d.device_tag || undefined },
    ha_discovery_enabled: d.ha_discovery_enabled,
  });
}
const conns = {
  rtu: CM.wire(CM.blank({ protocol: 'rtu', serial_port: '/dev/ttySC1', unit_id: 3, baudrate: 9600 })),
  tcp: CM.wire(CM.blank({ protocol: 'tcp', host: '192.168.1.20', port: 502, unit_id: 1 })),
  bridge: CM.wire(CM.blank({ protocol: 'rtu-tcp', bridge: 'ws1', bridge_port: 4196, unit_id: 2 })),
  http: CM.wire(CM.blank({ protocol: 'http', url: 'http://192.168.1.30/solar_api/v1/GetMeterRealtimeData.cgi?Scope=Device&DeviceId=0' })),
  mqtt: CM.wire(CM.blank({ protocol: 'mqtt', broker: '@gateway', topic: 'zigbee2mqtt/hall' })),
};
for (const [k, conn] of Object.entries(conns)) {
  for (const d of [
    { id: 'm1', name: 'Hall meter', template: 'eastron_sdm630', enabled: true, topic_prefix: '', bucket: '', device_tag: '', ha_discovery_enabled: true },
    { id: 'm2', name: '', template: 'eastron_sdm630', enabled: false, topic_prefix: 'site/m2', bucket: 'b2', device_tag: 'tag2', ha_discovery_enabled: false },
  ]) {
    const got = JSON.stringify(M.createPayload(Object.assign({ connection: conn, mqtt_enabled: true, influxdb_enabled: true }, d)));
    check(`create: byte-identical to today's (${k}, ${d.id})`, got === todayCreate(d, conn), [got, todayCreate(d, conn)]);
  }
}
const s9 = Object.assign(M.blank(), { template: 'eastron_sdm630', name: 'Hall meter', id: 'hall-meter' });
const dft = M.draftOf(s9, { connection: conns.rtu, tpl: by('eastron_sdm630') });
check('draft: a fresh Name & save is today\'s defaults (on, HA on, routing from the gateway)',
      JSON.stringify(M.createPayload(dft)) === todayCreate({ id: 'hall-meter', name: 'Hall meter', template: 'eastron_sdm630', enabled: true,
        topic_prefix: '', bucket: '', device_tag: '', ha_discovery_enabled: true }, conns.rtu), M.createPayload(dft));
const s10 = Object.assign(M.blank(), { template: 'eastron_sdm630', name: 'Hall', id: 'hall', typeId: 'chiller',
  identity: Object.assign(M.blank().identity, { location: 'garage', labels: 'hvac, floor-1', manufacturer: 'Eastron' }),
  recommended: { grid_connection: { role: 'sub_meter', max_import_kw: '12,5' }, pv_input: { mppt: '1' } },
  out: Object.assign(M.blank().out, { mqtt: false, influx: false, ha: false }),
  components: [{ type: 'battery', instance: 512, name: 'Battery 512', on: true }, { type: 'ruuvi', instance: 3, name: 'R', on: false }] });
const p10 = M.createPayload(M.draftOf(s10, { connection: conns.mqtt, tpl: by('eastron_sdm630'),
  catalog: { categories: { grid_connection: { repeatable: false, fields: { role: { type: 'choice', choices: ['sub_meter'] }, max_import_kw: { type: 'number' } } },
                            pv_input: { repeatable: true, fields: { mppt: { type: 'integer' } } } } } }));
check('create: a type of one\'s own is sent', p10.type === 'chiller');
check('create: the template\'s own type is not sent (the server derives it)',
      !('type' in M.createPayload(M.draftOf(Object.assign(M.blank(), { template: 'eastron_sdm630', typeId: 'meter', name: 'a', id: 'a' }),
        { connection: conns.rtu, tpl: by('eastron_sdm630') }))));
check('create: identity attributes, labels split, numbers read (a comma decimal too), a repeatable category a list',
      eq(p10.attributes, { identity: { location: 'garage', labels: ['hvac', 'floor-1'], manufacturer: 'Eastron' },
                           grid_connection: { role: 'sub_meter', max_import_kw: 12.5 }, pv_input: [{ mppt: 1 }] }), p10.attributes);
check('create: outputs off are said; HA off with MQTT', p10.mqtt.enabled === false && p10.influxdb.enabled === false && p10.ha_discovery_enabled === false, p10);
check('create: the components ticked go with it, the others not', eq(p10.components, [{ type: 'battery', instance: 512, name: 'Battery 512' }]));
check('create: the parent\'s write lock is never in it (the server locks every new device)', !('write_locked' in p10) && !JSON.stringify(p10).includes('write_lock'));
const s11 = Object.assign(M.blank(), { template: 't', name: 'n', id: 'n', connExtra: { availability: { topic: '{tele}/LWT' } } });
check('create: a found connection\'s keys the form does not show are kept (an MQTT availability)',
      eq(M.draftOf(s11, { connection: conns.mqtt }).connection.availability, { topic: '{tele}/LWT' })
      && !('availability' in M.draftOf(s11, { connection: conns.rtu }).connection));

// ── Create another like this: the copy list, exactly ────────────────────
check('copy list: what is copied', eq(M.COPY_LIST.copied, ['path', 'template', 'typeId', 'typeTouched', 'conn', 'connExtra',
  'enabled', 'out.mqtt', 'out.influx', 'out.ha', 'identity.location', 'identity.labels', 'identity.manufacturer',
  'identity.model', 'recommended (but grid_connection)']), M.COPY_LIST.copied);
check('copy list: what is not', eq(M.COPY_LIST.cleared, ['id', 'name (proposed: the next one)', 'unit (the next free on the bus)',
  'identity.description', 'identity.serial_number', 'identity.firmware', 'identity.installed_on', 'recommended.grid_connection',
  'components', 'check', 'out.prefix', 'out.bucket', 'out.tag']), M.COPY_LIST.cleared);
const seplosConn = { M: Object.assign(CM.blank({ protocol: 'seplos_v2', serial_port: '/dev/ttySC0', unit_id: 2 }), { bus_mode: 'listen' }),
  cs: { axes: { port: 'serial', reach: 'here', protocol: 'seplos_v2', framing: null, udp: false, mode: 'listen' }, role: 'listen',
        touched: { unit: true, line: false }, busId: '/dev/ttySC0', announce: '' }, tplId: 'seplos_v2_tap' };
const done = Object.assign(M.blank(), { path: 'model', step: 'save', template: 'seplos_v2_tap', typeId: 'battery', typeTouched: true,
  conn: seplosConn, name: 'Pack 2', nameTouched: true, id: 'pack-2', enabled: false,
  identity: Object.assign(M.blank().identity, { location: 'rack', labels: 'bank-a', manufacturer: 'Seplos', model: 'V2',
    description: 'second pack', serial_number: 'SP2', firmware: '16.5', installed_on: '2026-10-01' }),
  recommended: { battery: { usable_kwh: '14' }, grid_connection: { role: 'storage' } },
  out: { mqtt: true, influx: false, ha: true, prefix: 'x/pack-2', bucket: 'b', tag: 't' },
  components: [{ type: 'b', instance: 1, on: true }], check: Object.assign(M.blankCheck(), { state: 'done', view: okView }) });
const nx = M.another(done);
check('another: back on the connection step, the model kept', nx.step === 'connection' && nx.path === 'model' && nx.template === 'seplos_v2_tap');
check('another: the type, the outputs, start reading now', nx.typeId === 'battery' && nx.typeTouched && nx.enabled === false
      && nx.out.mqtt && !nx.out.influx && nx.out.ha && nx.out.prefix === '' && nx.out.bucket === '' && nx.out.tag === '');
check('another: location, labels, manufacturer, model kept; description, serial, firmware, installed on cleared',
      eq(nx.identity, { description: '', location: 'rack', labels: 'bank-a', manufacturer: 'Seplos', model: 'V2', serial_number: '', firmware: '', installed_on: '' }), nx.identity);
check('another: recommended kept but where a meter sits on the grid', eq(nx.recommended, { battery: { usable_kwh: '14' } }));
check('another: never the components, the check, the id', eq(nx.components, []) && nx.check.state === 'idle' && nx.id === '' && !nx.idTouched);
check('another: the name proposed is the next one', nx.name === 'Pack 3' && nx.nameTouched);
check('another: a name not typed is made again from the model and the address', M.another(Object.assign({}, done, { nameTouched: false })).name === '');
check('another: the role on the bus kept — the same bus (a Seplos mode too)', nx.conn.cs.role === 'listen' && nx.conn.M.bus_mode === 'listen');
check('another: the address is taken afresh (the next free on the bus)', nx.conn.cs.touched.unit === false && nx.conn.cs.busId === null);
check('another: a copy — the device just made is untouched', done.conn.cs.touched.unit === true && done.identity.serial_number === 'SP2');
check('another: the Seplos mode on another bus is the connection step\'s to clear (M7) — no second rule here', M.anotherOnBus === undefined);

check('next name: a trailing number goes up', M.nextName('SDM630 · unit 3') === 'SDM630 · unit 4' && M.nextName('Pack 9') === 'Pack 10');
check('next name: none = " 2"', M.nextName('Hall meter') === 'Hall meter 2' && M.nextName('') === '');
check('unique id: a taken one gets -2, -3', M.uniqueId('m', ['m', 'm-2']) === 'm-3' && M.uniqueId('m', []) === 'm');
// the next free address, the connection step's own rule (ConnStepModel.addressInfo)
for (const [used, want] of [[[1, 2, 3], 1], [[1, 3], 1], [[], 7], [[7, 8], 7], [Array.from({ length: 247 }, (_, i) => i + 1), 1]]) {
  const a = CS.addressInfo({ bus: { units_in_use: used.map(u => ({ unit: u, device: `d${u}` })) }, tpl: { default_unit_id: want } });
  check(`next free: ${JSON.stringify(used).slice(0, 20)} from ${want} = the connection step's ${a.value}`, M.nextFreeUnit(used, want) === a.value, [M.nextFreeUnit(used, want), a.value]);
}
check('next free: after unit 3 is made, "Add another (unit 4)"', M.nextFreeUnit([1, 2, 3], 3) === 4);

// ── Duplicate: the same copy list, from a stored device ─────────────────
const entry = { id: 'sdm-3', name: 'SDM630 · unit 3', template: 'eastron_sdm630', enabled: true, kind: 'meter', kind_source: 'template',
  protocol: 'rtu', connection: { protocol: 'rtu', serial_port: '/dev/ttySC1', unit_id: 3, baudrate: 9600, parity: 'N', stopbits: 1, bytesize: 8 },
  mqtt_enabled: true, influxdb_enabled: false, ha_discovery_enabled: true, mqtt_topic_prefix: 'mbg/devices/sdm-3', influxdb_bucket: 'sdm-3' };
const dup = M.fromEntry(entry, { identity: { location: 'cab 2', serial_number: 'S1', labels: ['a', 'b'] }, grid_connection: { role: 'load' } }, CM);
check('duplicate: Connection step, model and line copied, unit taken afresh, never its id or serial',
      dup.step === 'connection' && dup.template === 'eastron_sdm630' && dup.conn.M.serial_port === '/dev/ttySC1'
      && dup.conn.cs.touched.unit === false && dup.id === '' && dup.identity.serial_number === '' && dup.identity.location === 'cab 2'
      && dup.identity.labels === 'a, b' && !dup.out.influx && eq(dup.recommended, {}) && dup.name === 'SDM630 · unit 4', dup);
check('duplicate: a type set by hand is copied; one derived is not', M.fromEntry(Object.assign({}, entry, { kind: 'chiller', kind_source: 'override' }), {}, CM).typeId === 'chiller'
      && dup.typeId === '');

// ── where each way in lands ─────────────────────────────────────────────
check('land: a bus\'s + Device without a model = By connection, its first step', eq(M.landing({ source: 'bus' }), { path: 'connection', step: 'connection' }));
check('land: a scan row with a model = Connection, the model chosen', eq(M.landing({ source: 'bus', template: 'x' }), { path: 'model', step: 'connection' }));
check('land: found with a model = Check, pre-filled', eq(M.landing({ source: 'found', template: 'x' }), { path: 'model', step: 'check' }));
check('land: found without one = the model asked, the connection kept', eq(M.landing({ source: 'found' }), { path: 'connection', step: 'what' }));
check('land: duplicate = Connection', eq(M.landing({ source: 'duplicate', template: 'x' }), { path: 'model', step: 'connection' }));
check('land: measured', eq(M.landing({ source: 'measured' }), { path: 'measured', step: 'what' }));
check('land: the button', eq(M.landing({}), { path: 'model', step: 'what' }));
check('stepOf: the connection\'s refusals on Connection, the template on What, components on Check, the rest on Name & save',
      M.stepOfField('connection.unit_id', 'model') === 'connection' && M.stepOfField('template', 'model') === 'what'
      && M.stepOfField('components[0].name', 'model') === 'check' && M.stepOfField('mqtt.topic_prefix', 'model') === 'save'
      && M.stepOfField('attributes', 'model') === 'save' && M.stepOfField('type', 'model') === 'save');
check('topic preset: a sensor bridge\'s documented topic', M.topicPreset('zigbee2mqtt_sensor') === 'zigbee2mqtt/<friendly_name>' && M.topicPreset('x') === '');

// ── the review strip, from the dry run's would-be entry ─────────────────
const would = { id: 'hall', name: 'Hall', template: 'eastron_sdm630', enabled: true, type: 'meter', protocol: 'rtu',
  wire: { port: 'serial', reach: 'here', protocol: 'modbus', framing: 'rtu', udp: false, mode: 'poll', writable: true },
  topic_prefix: 'mbg/devices/hall', bucket: 'hall', device_tag: 'hall', ha_node: 'mbg_gw_dev_hall', write_locked: true,
  opcua_enabled: false, setup: 'pending', bus: { key: 'serial:/dev/ttySC1', where: '/dev/ttySC1', role: 'master', pollers: ['deye'], listeners: [] },
  components: [{ id: 'hall-battery-1', type: 'battery', instance: 1, name: 'B1' }] };
const strip = M.reviewStrip(would, t);
check('review: grouped by layer, each with the step its Change opens',
      eq(strip.map(g => [g.id, g.step]), [['identity', 'save'], ['connection', 'connection'], ['map', 'what'], ['inside', 'check'], ['outputs', 'save'], ['writes', 'save']]), strip.map(g => [g.id, g.step]));
const rowsOf = id => Object.fromEntries(strip.find(g => g.id === id).rows.map(r => [r.k, r.v]));
check('review: identity', eq(rowsOf('identity'), { name: 'Hall', id: 'hall', type: 'meter', enabled: 'on' }), rowsOf('identity'));
check('review: the wire and the bus it joins (who is on it already)', rowsOf('connection').wire === 'serial · here · modbus rtu · poll'
      && rowsOf('connection').bus === '/dev/ttySC1 · master · polled by deye', rowsOf('connection'));
check('review: outputs resolved by the server', eq(rowsOf('outputs'), { mqtt: 'mbg/devices/hall', influxdb: 'hall · hall', ha: 'mbg_gw_dev_hall', opcua: 'off' }), rowsOf('outputs'));
check('review: writes locked, setup pending', eq(rowsOf('writes'), { lock: 'locked — an admin unlocks them from its ⋯ → Unlock writes…', setup: 'pending' }), rowsOf('writes'));
check('review: the components inside', eq(rowsOf('inside'), { 'hall-battery-1': 'B1 (battery 1)' }));
const strip2 = M.reviewStrip(Object.assign({}, would, { bus: null, components: [], setup: 'none',
  wire: Object.assign({}, would.wire, { writable: false, mode: 'listen' }) }), t);
check('review: no bus, nothing inside, nothing to set up, a listener writes nothing',
      !strip2.some(g => g.id === 'inside') && !Object.fromEntries(strip2.find(g => g.id === 'connection').rows.map(r => [r.k, 1])).bus
      && eq(Object.fromEntries(strip2.find(g => g.id === 'writes').rows.map(r => [r.k, r.v])), { lock: 'none' }));
check('review: outputs off said off', (() => {
  const g = M.reviewStrip(Object.assign({}, would), t, { mqtt: false, influx: false });
  const r = Object.fromEntries(g.find(x => x.id === 'outputs').rows.map(x => [x.k, x.v]));
  return r.mqtt === 'off' && r.influxdb === 'off' && r.ha === 'off';
})());
check('dirty: a connection answered is something to lose', M.isDirty(Object.assign(M.blank(), { conn: seplosConn })));

// ── many of the same on one bus (L3.7) ─────────────────────────────────────
const sdm = { id: 'eastron_sdm120', name: 'Eastron SDM120', model: 'SDM120' };
const deye = { id: 'deye_hybrid_1p', name: 'Deye hybrid 1P' };
const scanned = [
  { unit_id: 1, fc43: { vendor: 'Eastron', product: 'SDM120', version: '1.2' }, sunspec: false, matches: [], suggested: null, device: 'grid' },
  { unit_id: 2, fc43: {}, sunspec: false, matches: [{ id: 'deye_hybrid_1p', name: 'Deye hybrid 1P' }], suggested: 'deye_hybrid_1p', device: '' },
  { unit_id: 3, fc43: {}, sunspec: true, matches: [], suggested: null, device: '', answer: 'exception' },
  { unit_id: 4, fc43: {}, sunspec: false, matches: [], suggested: null, device: '' },
];
check('scan: what a unit says — FC43, SunSpec, the fingerprint, an exception',
      eq(scanned.map(M.scanSays), [['Eastron SDM120 1.2'], ['Deye hybrid 1P'], ['SunSpec', 'exception'], []]), scanned.map(M.scanSays));
check('scan: a unit a device has already is never ticked', eq(scanned.map(M.scanPickable), [false, true, true, true]));
check('scan: the model chosen in Step 1 wins, else the suggestion, else none',
      eq([M.scanModel(scanned[1], 'eastron_sdm120'), M.scanModel(scanned[1], ''), M.scanModel(scanned[3], '')],
         ['eastron_sdm120', 'deye_hybrid_1p', '']));
check('batch id: {model}-u{unit}', M.batchId(sdm, 7, []) === 'sdm120-u7');
check('batch id: the next free when a device has it', M.batchId(sdm, 7, ['sdm120-u7', 'sdm120-u7-2']) === 'sdm120-u7-3');
check('batch id: no model = device-u{unit}', M.batchId(null, 9, new Set()) === 'device-u9');
check('batch id: from the name when there is no model word', M.batchId(deye, 2, []) === 'deye-hybrid-1p-u2');
check('batch id: how it is reached is not the model ("SDM120 Modbus")', M.batchId({ model: 'SDM120 Modbus' }, 1, []) === 'sdm120-u1');
const rows = M.batchRows([4, 2, 2, 3], scanned, { template: 'eastron_sdm120', templates: [sdm, deye], taken: ['sdm120-u3'] });
check('batch rows: one per unit, in order, duplicates dropped', eq(rows.map(r => r.unit), [2, 3, 4]));
check('batch rows: ids unique against the devices and each other',
      eq(rows.map(r => r.id), ['sdm120-u2', 'sdm120-u3-2', 'sdm120-u4']), rows.map(r => r.id));
check('batch rows: the name says the model and the unit', rows[0].name === 'SDM120 · unit 2' && rows[0].status === 'new');
const free = M.batchRows([2, 4], scanned, { templates: [sdm, deye] });
check('batch rows: no model in Step 1 — the suggestion, else empty (asked on the row)',
      eq(free.map(r => r.template), ['deye_hybrid_1p', '']) && free[1].id === 'device-u4', free);
const bErrs = M.batchErrors([{ id: 'ok-1', name: 'a', template: 'x' }, { id: 'ok-1', name: 'b', template: 'x' },
                            { id: 'B@d', name: '', template: '' }, { id: 'grid', name: 'c', template: 'x' },
                            { id: 'grid', name: '', template: '', status: 'created' }], ['grid'], t);
check('batch errors: a second row with an id, a bad id, no name, no model, an id taken; a created row is not judged',
      eq(Object.keys(bErrs), ["1", "2", "3"]) && bErrs[1].id && bErrs[2].id && bErrs[2].name && bErrs[2].template && bErrs[3].id, bErrs);
const conn = { protocol: 'rtu', serial_port: '/dev/ttySC1', baudrate: 9600, parity: 'N', stopbits: 1, bytesize: 8, unit_id: 1, timeout: 1 };
const body = M.batchPayload(rows[0], { enabled: true, ha_discovery_enabled: false, bucket: '' }, conn);
check('batch payload: the old wizard\'s create body, key order too',
      eq(Object.keys(body), ['id', 'name', 'template', 'enabled', 'connection', 'mqtt', 'influxdb', 'ha_discovery_enabled']), Object.keys(body));
check('batch payload: the unit is the row\'s, the rest of the connection shared',
      body.connection.unit_id === 2 && body.connection.serial_port === '/dev/ttySC1' && conn.unit_id === 1);
check('batch payload: the shared block applies to every row', body.enabled === true && body.ha_discovery_enabled === false
      && eq(body.mqtt, {}) && body.influxdb.bucket === undefined);
check('batch tally: nothing half-done hidden', eq(M.batchTally([{ status: 'created' }, { status: 'failed' }, { status: 'new' }]),
      { created: 1, failed: 1, left: 2, total: 3 }));
const sm = Object.assign(M.setPath(M.blank(), 'many'), {});
check('next: many — nothing ticked says why', eq(M.canNext(sm), { ok: false, why: 'units' }));
check('next: many — ticked units go on to Units', M.canNext(Object.assign(sm, { batch: { units: [2] } })).ok);
check('next: many — on Units it creates the batch', M.canNext(Object.assign(M.go(sm, +1), {})).batch === true);
check('dirty: ticked units are something to lose', M.isDirty({ batch: { units: [1] } }) && !M.isDirty({ batch: { units: [] } }));

// ══ L3.9 review: the check reset, recommended values kept, Duplicate's password ══
const gxView = { state: 'done', mode: 'listen', values: [{ name: 'soc', quality: 'ok' }], groups: { heard: 1, total: 2 },
  identity: { fc43: { vendor: 'Victron', product: 'Cerbo GX', version: '3.1' } }, components: [{ type: 'battery', instance: 512 }] };
let r1 = Object.assign(M.blank(), { template: 'victron_gx_mqtt', check: Object.assign(M.blankCheck(), { state: 'done', view: gxView, key: 'k1' }),
  components: [{ type: 'battery', instance: 512, name: 'House bank', on: true }] });
r1 = M.applyNameplate(r1, gxView);
r1.identity.serial_number = 'typed by hand';
r1.identity.location = 'garage';
const r1b = M.checkReset(r1);
check('reset: the check, the parts it found and the nameplate it filled are gone',
      r1b.check.state === 'idle' && r1b.components.length === 0 && r1b.identity.manufacturer === '' && r1b.identity.model === ''
      && r1b.identity.firmware === '' && eq(r1b.plateFrom, {}), r1b);
check('reset: what the user typed stays (a nameplate field, where it is)', r1b.identity.serial_number === 'typed by hand' && r1b.identity.location === 'garage');
check('reset: a copy — the state given is untouched', r1.components.length === 1 && r1.identity.manufacturer === 'Victron');
const r1c = M.plateTyped(r1, 'manufacturer');
check('plate: a field the user edits is theirs (no longer "read from the device")', !('manufacturer' in r1c.plateFrom)
      && M.checkReset(Object.assign({}, r1c, { identity: Object.assign({}, r1c.identity, { manufacturer: 'Mine' }) })).identity.manufacturer === 'Mine');
check('plate: Run again never overwrites a correction', M.applyNameplate(Object.assign({}, r1c, { identity: Object.assign({}, r1c.identity, { manufacturer: 'Mine' }) }), gxView).identity.manufacturer === 'Mine');
const r1d = M.createPayload(M.draftOf(r1b, { connection: CM.wire(CM.blank({ protocol: 'mqtt', broker: '10.0.0.9', topic: 'N/x/#' })), tpl: {} }));
check('reset: a create after it posts no parts and no nameplate of the check before', !('components' in r1d)
      && !(r1d.attributes && r1d.attributes.identity && r1d.attributes.identity.manufacturer), r1d);

const rr = Object.assign(M.blank(), { template: 'x', name: 'n', id: 'n', recommended: { rating: { rated_ac_kw: '8' }, grid_connection: { role: 'load' } } });
const cat2 = { categories: { rating: { fields: { rated_ac_kw: { type: 'number' } } }, grid_connection: { fields: { role: { type: 'choice', choices: ['load'] } } } } };
const pr = M.createPayload(M.draftOf(rr, { connection: {}, tpl: {}, catalog: cat2, recommendedCats: ['rating'] }));
check('recommended: kept in the state for every type; only the current type\'s go out',
      eq(pr.attributes, { rating: { rated_ac_kw: 8 } }) && rr.recommended.grid_connection.role === 'load', pr.attributes);
check('recommended: no list given = every category held (Duplicate, the batch)', !!M.createPayload(M.draftOf(rr, { connection: {}, tpl: {}, catalog: cat2 })).attributes.grid_connection);

const withPw = Object.assign({}, entry, { protocol: 'mqtt', connection: { protocol: 'mqtt', broker: 'b', topic: 't', username: 'u', password: '******' } });
const dpw = M.fromEntry(withPw, {}, CM);
check('duplicate: a stored password is not copied — said, to type again', dpw.conn.M.password === '' && dpw.conn.pwCleared === true);
check('duplicate: no password, nothing said', !M.fromEntry(entry, {}, CM).conn.pwCleared);


// ── L3.11c (L3-UX-12): a chip counts what its list would show ───────────
{
  const filters = [{}, { q: 'eastron' }, { transport: 'mqtt' }, { q: 'deye' }, { transport: 'modbus', q: 'samsung' }];
  for (const f of filters) {
    const counts = M.chipCounts(templates, f, ctx);
    for (const c of M.typeChips(templates, catalog).chips) {
      const shown = M.filter(templates, Object.assign({}, f, { type: c.id }), ctx).length;
      check(`chip counts (${JSON.stringify(f)}): ${c.id || 'Any'} counts the ${shown} model(s) its list shows`, counts[c.id] === shown, counts[c.id]);
    }
  }
  check('filter: by what the connection speaks', eq(ids(M.filter(templates, { transport: 'mqtt' }, ctx)), ['victron_gx']), ids(M.filter(templates, { transport: 'mqtt' }, ctx)));
  check('filter: a template without a transport speaks Modbus', M.filter([{ id: 'x', name: 'X', device_kind: 'meter' }], { transport: 'modbus' }, ctx).length === 1);
  const cc = M.chipCounts(templates, { q: 'eastron' }, ctx);
  check('chip counts: a search narrows them (Any 2, Meter 2, Inverter 0)', cc[''] === 2 && cc.meter === 2 && cc.inverter === 0, cc);
  const none = M.chipCounts(templates, { transport: 'mqtt' }, ctx);
  check('chip counts: by connection (MQTT): Any 1, Meter 0', none[''] === 1 && none.meter === 0, none);
}

// ── L3.11c (L3-UX-12): Check through a running device's connection ───────
{
  const ref = { status: 422, kind: 'refused', errors: ["connection.timeout: 127.0.0.1:15502 is read through 'em1's connection, whose timeout is 1 — a preview cannot change it; save the device to change the line"],
                detail: { holder: 'em1', line: { timeout: 1 } } };
  const sl = M.sharedLine(ref);
  check('shared line: a 422 that names the holder\'s line is one', sl && sl.holder === 'em1' && eq(sl.line, { timeout: 1 }), sl);
  check('shared line: any other refusal is not', M.sharedLine({ status: 409, detail: { holder: 'em1' } }) === null
        && M.sharedLine({ status: 422, detail: { errors: ['x'] } }) === null && M.sharedLine(null) === null);
  const Mc = { protocol: 'tcp', host: '127.0.0.1', port: 15502, unit_id: 7, timeout: 3 };
  const a = M.adoptLine(Mc, sl.line);
  check('shared line: the holder\'s values are taken, nothing else changes', eq(a, Object.assign({}, Mc, { timeout: 1 })) && Mc.timeout === 3, a);
  check('shared line: a value the holder has not is not taken', eq(M.adoptLine(Mc, { timeout: null, baudrate: undefined }), Mc));
}

console.log(`\n${n - failed}/${n} passed`);
process.exit(failed ? 1 : 0);
