/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// The new Add Device's model (ADR-0031 §2, L3.5): pure, no DOM; Node tests
// it (tools/e2e/adddev_model_test.mjs). The page is ui/js/app-add-device.js,
// its frame the device shell in steps mode (ui/js/device-shell.js).
//
//   stepsOf(path)               the steps of a way in: model (default) ·
//                               connection (the first two swapped) ·
//                               measured · many
//   blank() / setPath(s, p) / go(s, ±1) / position(s) / isDirty(s)
//                               the state: one object, every answer kept on
//                               Back and when another way is taken
//   canNext(s)                  {ok, why}: what the step still needs
//   typeChips(templates, cat)   {chips: Any, built-ins, user types (each
//                               with its count), more: the types with none}
//   filter(templates, {type, q}, ctx)
//                               the models offered: by type (a user type =
//                               its built-in's models and the ones listing
//                               it), by search (model, brand, type words in
//                               every language, synonyms; case and
//                               diacritics never matter); a part of another
//                               device is never offered alone
//   badges(tpl) / ports(tpl) / noteText / noteWarn
//                               what a card wears
//   measuredKinds / measuredErrors / measuredDraft / meterOnlyPayload /
//   measuredDryRunBody / stepOfField / idFromName / idValid
//                               the measured path (a device that IS its
//                               meters); the create is byte-identical to
//                               the old wizard's (app-meters.js at ab27652)
//   foundItems(mqtt, bridges)   the passive inbox behind the Found chip
//
// L3.9 — the connection, the check, Name & save:
//   landing({source, template}) where a way in lands: the button (What),
//                               a bus's + Device / a scan row (Connection),
//                               a found device (Check, or What when its
//                               model is unknown), Duplicate (Connection),
//                               the measured path
//   blankCheck / checkVerdict(view) / checkPassed / heard / checkRows /
//   identityVerdict / nameplateFrom / applyNameplate / nextLabel
//                               the Check step over GET /api/previews/{job}:
//                               it never blocks; Next goes on "without a check"
//   draftOf(s, {connection, tpl, catalog}) / createPayload(d)
//                               THE create payload builder (Add, Create
//                               another, the batch of L3.7): byte-identical
//                               to the old wizard's for the same choices;
//                               type, attributes, components and an output
//                               off are added only when chosen
//   COPY_LIST / another(s) / fromEntry(entry, attrs, CM)
//                               Create another like this and Duplicate: a
//                               fixed copy list, the next free address, a
//                               Seplos mode only on the same bus (M7: the
//                               connection step clears it on another), never
//                               an unlock
//   checkReset(s) / plateTyped(s, k)
//                               a connection or a model changed: the check,
//                               the parts it found and the nameplate values
//                               it filled go (a value the user typed stays)
//   nextName / uniqueId / nextFreeUnit / topicPreset
//   reviewStrip(entry, t, outs) the dry run's would-be entry, grouped by layer
//   many of the same (L3.7):
//   scanSays(f) / scanModel(f, tpls)
//                               what a scanned unit says (FC43, SunSpec, the
//                               fingerprint) and the model suggested for it
//   scanPickable(f)             a unit a device of this gateway has already: never ticked
//   batchSlug(tpl) / batchId(tpl, unit, taken)
//                               the id pattern {model}-u{unit}, made unique
//                               against the devices and the other rows
//   batchRows(units, found, o)  the Units table: one row per ticked unit,
//                               {unit, template, name, id}
//   batchErrors(rows, taken, t) {row index: {id|name|template: why}} before anything is sent
//   batchPayload(row, shared, connection)
//                               one row's create — the old wizard's body (key
//                               order too), the unit its own
//   batchTally(rows)            {created, failed, left, total}: nothing half-done hidden
(function (root) {
    'use strict';

    const PATHS = {
        model: ['what', 'connection', 'check', 'save'],
        connection: ['connection', 'what', 'check', 'save'],
        measured: ['what', 'meters', 'save'],
        // many of the same (L3.7): the connection with its scan panel, then
        // the units picked from it, each created on its own
        many: ['many', 'units'],
    };
    // the steps this build takes a device through (many of the same: the
    // bus scan and the Units step, L3.7; then the batch)
    const READY = { model: ['what', 'connection', 'check', 'save'], connection: ['connection', 'what', 'check', 'save'],
                    measured: ['what', 'meters', 'save'], many: ['many', 'units'] };

    const copy = v => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
    const fold = s => String(s ?? '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

    function stepsOf(path) {
        return (PATHS[path] || PATHS.model).slice();
    }

    const IDENTITY = ['description', 'location', 'labels', 'manufacturer', 'model', 'serial_number', 'firmware', 'installed_on'];

    function blankCheck() {
        return { state: 'idle', job: '', view: null, refusal: null, confirmOpen: false, key: '' };
    }

    function blank() {
        return { path: 'model', step: 'what', q: '', type: '', template: '',
                 kind: '', meters: [], name: '', id: '', idTouched: false, typeDraft: '',
                 // the connection step's state() (kept on Back), what a way in
                 // brought, and a found connection's keys its form does not show
                 conn: null, prefill: null, connExtra: null, bus: null,
                 check: blankCheck(),
                 nameTouched: false, typeId: '', typeTouched: false,
                 identity: Object.fromEntries(IDENTITY.map(k => [k, ''])), plateFrom: {},
                 recommended: {},
                 out: { mqtt: true, influx: true, ha: true, prefix: '', bucket: '', tag: '' },
                 enabled: true, components: [] };
    }

    function setPath(s, path) {
        const out = copy(s);
        out.path = PATHS[path] ? path : 'model';
        out.step = stepsOf(out.path)[0];
        return out;
    }

    function go(s, dir) {
        const out = copy(s);
        const steps = stepsOf(out.path);
        const i = Math.max(0, steps.indexOf(out.step));
        out.step = steps[Math.min(steps.length - 1, Math.max(0, i + dir))];
        return out;
    }

    function position(s) {
        const steps = stepsOf(s.path);
        return { n: steps.indexOf(s.step) + 1, of: steps.length };
    }

    // what closing would lose: a choice, a meter, a name, words typed in
    // + Add type…; a search alone is nothing (typed again in a second)
    function isDirty(s) {
        s = s || {};
        return !!(s.type || s.template || s.kind || (s.meters || []).length || s.conn || ((s.batch || {}).units || []).length
                  || String(s.name || '').trim() || String(s.id || '').trim() || String(s.typeDraft || '').trim()
                  || Object.values(s.identity || {}).some(v => String(v || '').trim()));
    }

    // what keeps Next from moving on, as a word the page translates; the
    // connection step's own answers are judged by its form and the gateway
    function canNext(s) {
        if (!(READY[s.path] || []).includes(s.step)) return { ok: false, why: 'later' };
        const last = stepsOf(s.path).slice(-1)[0] === s.step;
        if (s.path === 'measured') {
            if (s.step === 'what' && !s.kind) return { ok: false, why: 'kind' };
            if (s.step === 'meters' && !(s.meters || []).some(m => m && m.device)) return { ok: false, why: 'meters' };
            return { ok: true, create: last };
        }
        if (s.path === 'many') {
            if (s.step === 'many' && !((s.batch || {}).units || []).length) return { ok: false, why: 'units' };
            return { ok: true, batch: s.step === 'units' };
        }
        if (s.step === 'what' && !s.template) return { ok: false, why: 'pick' };
        // the check never blocks (ADR-0031 §2): Next says it goes on without one
        if (s.step === 'check') return { ok: true, unchecked: !checkPassed(s.check) };
        return { ok: true, create: last };
    }

    function nextLabel(s, t) {
        const tr = typeof t === 'function' ? t : (k, d) => d;
        const c = canNext(s);
        if (c.create) return tr('devices.wizard.create', 'Create device');
        if (c.unchecked) return tr('adddev.nextUnchecked', 'Next — without a check');
        return tr('common.next', 'Next');
    }

    // ── types ─────────────────────────────────────────────────────────────
    function typeOf(cat, id) {
        return ((cat || {}).types || []).find(x => x.id === id) || null;
    }

    function builtinOf(id, cat) {
        const ty = typeOf(cat, id);
        if (!ty) return id;
        return ty.builtin ? ty.id : (ty.behaves_like || 'other');
    }

    function matchesType(id, tpl, cat) {
        if (!id) return true;
        return (tpl.device_kind || tpl.kind || 'other') === builtinOf(id, cat) || (tpl.types || []).includes(id);
    }

    const listable = templates => (templates || []).filter(x => x && !x.component_of);

    function typeChips(templates, cat) {
        const list = listable(templates);
        const chips = [{ id: '', builtin: true, count: list.length }];
        const more = [];
        const types = ((cat || {}).types || []);
        // built-ins first (the catalog's order), then the user's
        for (const ty of [...types.filter(x => x.builtin), ...types.filter(x => !x.builtin)]) {
            const c = { id: ty.id, builtin: !!ty.builtin, count: list.filter(x => matchesType(ty.id, x, cat)).length };
            (c.count ? chips : more).push(c);
        }
        return { chips, more };
    }

    // ── search ────────────────────────────────────────────────────────────
    function searchText(tpl, ctx) {
        const cat = (ctx || {}).catalog || {};
        const words = (ctx || {}).kindWords || {};
        const kind = tpl.device_kind || tpl.kind || 'other';
        const parts = [tpl.id, tpl.name, tpl.vendor, tpl.model, kind, tpl.transport, tpl.speaks, words[kind]];
        // the type's words in every language, and those of each type that
        // takes this model (a user type behaving like its kind, or listed)
        for (const ty of (cat.types || [])) {
            if (ty.id === kind || (tpl.types || []).includes(ty.id) || (!ty.builtin && ty.behaves_like === kind)) {
                parts.push(...Object.values(ty.labels || {}), ty.id);
            }
        }
        return fold(parts.filter(Boolean).join(' ').replace(/[_-]/g, ' ') + ' ' + parts.filter(Boolean).join(' '));
    }

    function filter(templates, f, ctx) {
        f = f || {};
        const cat = (ctx || {}).catalog || {};
        const words = fold(f.q).split(/\s+/).filter(Boolean);
        const key = x => `${fold(x.vendor)}\u0000${fold(x.name || x.id)}`;
        return listable(templates)
            .filter(x => !f.transport || (x.transport || 'modbus') === f.transport)
            .filter(x => matchesType(f.type, x, cat))
            .filter(x => { if (!words.length) return true; const h = searchText(x, ctx); return words.every(w => h.includes(w)); })
            .sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0));
    }

    // what each chip's list would show for the search and the connection in
    // force (L3-UX-12: a chip and its list always agree): {typeId: n, '': all}
    function chipCounts(templates, f, ctx) {
        f = f || {};
        const cat = (ctx || {}).catalog || {};
        const out = { '': filter(templates, { q: f.q, transport: f.transport }, ctx).length };
        for (const ty of (cat.types || [])) out[ty.id] = filter(templates, { q: f.q, transport: f.transport, type: ty.id }, ctx).length;
        return out;
    }

    // ── the Check through a running device's connection (L3-UX-12) ────────
    // a preview refused because the request names another line or timeout
    // than the device that holds the bus: {holder, line: {key: its value}}
    function sharedLine(ref) {
        if (!ref || ref.status !== 422) return null;
        const d = ref.detail || {};
        if (!d.holder || !d.line || typeof d.line !== 'object' || !Object.keys(d.line).length) return null;
        return { holder: String(d.holder), line: Object.assign({}, d.line) };
    }
    // the connection with the holder's values taken (only the ones it has)
    function adoptLine(M, line) {
        const out = Object.assign({}, M || {});
        for (const [k, v] of Object.entries(line || {})) if (v !== null && v !== undefined) out[k] = v;
        return out;
    }

    // ── a card ────────────────────────────────────────────────────────────
    function badges(tpl) {
        const out = [];
        if (tpl.listen_only) out.push('listens');
        if (((tpl.setup || {}).count || 0) > 0) out.push('setup');
        if ((tpl.components || []).length) out.push('parts');
        if (notesOf(tpl).length) out.push('note');
        return out;
    }

    // a template's notes: [{level: info|warning, text, …}] (device_template.py)
    const notesOf = tpl => (Array.isArray((tpl || {}).notes) ? tpl.notes : []).filter(n => n && String(n.text || '').trim());
    const noteText = tpl => notesOf(tpl).map(n => String(n.text).trim()).join(' · ');
    const noteWarn = tpl => notesOf(tpl).some(n => n.level === 'warning');

    const PORT_ICON = { serial: 'bi-usb-symbol', ethernet: 'bi-ethernet', can: 'bi-bezier2', http: 'bi-globe', mqtt: 'bi-broadcast' };

    function ports(tpl) {
        return ((tpl || {}).ports || []).filter(p => PORT_ICON[p]).map(p => ({ id: p, icon: PORT_ICON[p] }));
    }

    // ── the measured path ─────────────────────────────────────────────────
    function measuredKinds(roles, cat) {
        const k = (roles || {}).meter_only_kinds || { first: ['ups', 'charger', 'heat_pump', 'drive'], more: [] };
        const users = ((cat || {}).types || []).filter(x => !x.builtin && x.behaves_like !== 'meter').map(x => x.id);
        const more = (k.more || []).slice();
        for (const u of users) if (!more.includes(u) && !(k.first || []).includes(u)) more.push(u);
        return { first: (k.first || []).slice(), more };
    }

    // the same checks the old wizard made before its last step
    function measuredErrors(s, t) {
        const tr = typeof t === 'function' ? t : (k, d) => d;
        const f = {};
        if (!s.kind) f.kind = tr('wizMtr.needKind', 'Say what it is');
        if (!(s.meters || []).length) f['meters[0].device'] = tr('wizMtr.needMeter', 'Name at least one meter');
        (s.meters || []).forEach((l, i) => {
            if (!l.role) f[`meters[${i}].role`] = tr('meters.needRole', 'Choose its role');
            if (!l.device) f[`meters[${i}].device`] = tr('meters.needMeter', 'Choose the meter');
            else if (!l.power && !l.energy) f[`meters[${i}].power`] = tr('meters.needField', 'Choose what it reads: its power or its energy counter');
        });
        return f;
    }

    function stepOfField(path, way) {
        const p = String(path || '');
        if (way === 'measured') {
            if (p === 'kind' || p === 'type') return 'what';
            if (p.startsWith('meters')) return 'meters';
            return 'save';
        }
        if (p.startsWith('connection') || p.startsWith('sources') || p === 'bus') return 'connection';
        if (p === 'template') return 'what';
        if (p.startsWith('components')) return 'check';
        return 'save';
    }

    function idFromName(name) {
        return fold(name).replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32).replace(/-+$/, '');
    }

    const idValid = id => /^[a-z0-9][a-z0-9_-]{1,63}$/.test(String(id || ''));

    // what the old wizard's data held for this device: name & save asks only
    // the name (and its id); polling on, Home Assistant on and the routing
    // left to the gateway's patterns, as the old wizard opened them
    function measuredDraft(s) {
        return { id: s.id, name: s.name, kind: s.kind, enabled: true, meters: copy(s.meters || []),
                 topic_prefix: '', bucket: '', device_tag: '', ha_discovery_enabled: true };
    }

    // byte-identical to the meter-only create of old (app-meters.js at ab27652, the
    // key order too): one create, two writers, one shape
    function meterOnlyPayload(d) {
        return {
            id: d.id, name: d.name || d.id, kind: d.kind, enabled: d.enabled,
            connection: { protocol: 'none' }, meters: d.meters || [],
            mqtt: d.topic_prefix ? { topic_prefix: d.topic_prefix } : {},
            influxdb: { bucket: d.bucket || undefined, device_tag: d.device_tag || undefined },
            ha_discovery_enabled: d.ha_discovery_enabled,
        };
    }

    // what the gateway is asked before Name & save: the type and the meters
    // (a dry run stores nothing; a device of no connection reads no bus).
    // Its id is the one typed, else one from the name or the type — a real
    // id, never a probe device (L3-F3); a clash with a device is not this
    // step's to say (the page leaves `id` refusals to Name & save)
    function measuredDryRunBody(s) {
        const id = [s.id, idFromName(s.name), idFromName(`${s.kind || 'measured'}-new`), 'measured-new'].find(idValid);
        return { id, name: s.name || id, kind: s.kind, enabled: false,
                 connection: { protocol: 'none' }, meters: copy(s.meters || []) };
    }

    // ── Found: what the gateway already heard of, nothing asked anew ──────
    function foundItems(mqtt, bridges) {
        const out = [];
        for (const x of ((mqtt || {}).items || [])) {
            if (x.state !== 'new' && x.state !== 'template_missing') continue;
            out.push({ source: 'mqtt', key: x.key, label: x.name || x.device_id || x.key,
                       template: x.template || '', state: x.state, name: x.name || '', device_id: x.device_id || '',
                       connection: x.connection ? copy(x.connection) : null });
        }
        (bridges || []).forEach((b, idx) => {
            if (!b || b.bridge) return;                 // added already
            out.push({ source: 'bridge', key: `${b.host}${b.port ? ':' + b.port : ''}`, idx,
                       label: b.type_name || b.type || '', host: b.host, port: b.port || null });
        });
        return out;
    }

    // ── L3.9: where a way in lands ────────────────────────────────────────
    // the button: What; a bus's + Device or a scan row: Connection (the bus
    // filled; By connection when the model is not known); a found device:
    // Check (its model known) or What (the connection kept); Duplicate:
    // Connection with the copy list; the Meters' way: the measured path
    function landing(o) {
        o = o || {};
        if (o.source === 'measured') return { path: 'measured', step: 'what' };
        if (o.source === 'bus') return o.template ? { path: 'model', step: 'connection' } : { path: 'connection', step: 'connection' };
        if (o.source === 'found') return o.template ? { path: 'model', step: 'check' } : { path: 'connection', step: 'what' };
        if (o.source === 'duplicate') return { path: 'model', step: 'connection' };
        return { path: 'model', step: 'what' };
    }

    // a sensor bridge's documented topic, the <placeholder> to replace
    const TOPIC_PRESETS = { zigbee2mqtt_sensor: 'zigbee2mqtt/<friendly_name>',
                            ble_theengs_sensor: 'home/TheengsGateway/BTtoMQTT/<MAC>' };
    const topicPreset = id => TOPIC_PRESETS[id] || '';

    // ── L3.9: the Check step (GET /api/previews/{job}) ────────────────────
    // idle · running · ok (a value read, or groups heard) · none (asked,
    // nothing answered) · failed (failed, cancelled)
    function checkVerdict(view) {
        if (!view) return 'idle';
        if (view.state === 'running') return 'running';
        if (view.state !== 'done') return 'failed';
        const vals = view.values || [];
        const any = vals.some(v => v && v.quality === 'ok');
        if (view.mode === 'listen') return (any || ((view.groups || {}).heard || 0) > 0) ? 'ok' : 'none';
        return any ? 'ok' : 'none';
    }

    const checkPassed = c => !!c && c.state === 'done' && checkVerdict(c.view) === 'ok';

    function heard(view) {
        if (!view || view.mode !== 'listen') return null;
        const g = view.groups || {};
        return { n: g.heard || 0, of: g.total || 0 };
    }

    // ok first, then by poll group (the server's order kept within)
    function checkRows(view) {
        const vals = ((view || {}).values || []).map((v, i) => ({ v, i }));
        vals.sort((a, b) => ((a.v.quality === 'ok' ? 0 : 1) - (b.v.quality === 'ok' ? 0 : 1))
            || String(a.v.group || '').localeCompare(String(b.v.group || '')) || a.i - b.i);
        return vals.map(x => x.v);
    }

    function identityVerdict(view) {
        const id = (view || {}).identity;
        if (!id || !id.fingerprint || id.fingerprint === 'none') return { state: 'none' };
        if (id.fingerprint === 'mismatch') return { state: 'mismatch', suggested: id.suggested || '', name: id.suggested_name || id.suggested || '' };
        return { state: id.fingerprint };
    }

    // what the device said of itself, as a nameplate: SunSpec's common model
    // first, FC43 next; each value with where it came from
    function nameplateFrom(view) {
        const id = (view || {}).identity || {};
        const out = {};
        const put = (k, v, from) => { if (!out[k] && String(v || '').trim()) out[k] = { value: String(v).trim(), from }; };
        const ss = id.sunspec || {}, f = id.fc43 || {};
        put('manufacturer', ss.manufacturer, 'sunspec'); put('model', ss.model, 'sunspec');
        put('firmware', ss.version, 'sunspec'); put('serial_number', ss.serial, 'sunspec');
        put('manufacturer', f.vendor, 'fc43'); put('model', f.product, 'fc43'); put('firmware', f.version, 'fc43');
        return out;
    }

    // the nameplate pre-filled from the check; a field typed in stays
    function applyNameplate(s, view) {
        const out = copy(s);
        for (const [k, x] of Object.entries(nameplateFrom(view))) {
            if (String(out.identity[k] || '').trim() && !(out.plateFrom || {})[k]) continue;
            out.identity[k] = x.value;
            out.plateFrom = Object.assign({}, out.plateFrom, { [k]: x.from });
        }
        return out;
    }

    // the connection or the model changed: what the check said is no longer
    // true — its answer, the parts it found and the nameplate values it
    // filled go; a value the user typed stays
    function checkReset(s) {
        const out = copy(s);
        out.check = blankCheck();
        out.components = [];
        for (const k of Object.keys(out.plateFrom || {})) out.identity[k] = '';
        out.plateFrom = {};
        return out;
    }

    // a nameplate field the user edits is theirs: never "read from the device"
    function plateTyped(s, k) {
        const out = copy(s);
        if (out.plateFrom) delete out.plateFrom[k];
        return out;
    }

    // ── L3.9: THE create payload (Add, Create another, the batch) ─────────
    const nonEmpty = o => Object.keys(o || {}).length > 0;
    function attrValue(f, raw) {
        const v = String(raw ?? '').trim();
        if (!v) return undefined;
        const type = (f || {}).type;
        if (type === 'labels') return v.split(',').map(x => x.trim()).filter(Boolean);
        if (type === 'choice') { const c = (f.choices || []).find(x => String(x) === v); return c !== undefined ? c : v; }
        if (type === 'number' || type === 'integer') { const n = Number(v.replace(',', '.')); return Number.isFinite(n) ? n : v; }
        return v;
    }

    function attributesOf(s, catalog, only) {
        const cats = (catalog || {}).categories || {};
        const out = {};
        const idf = (cats.identity || {}).fields || { labels: { type: 'labels' } };
        const ident = {};
        for (const k of IDENTITY) {
            const v = attrValue(idf[k] || (k === 'labels' ? { type: 'labels' } : { type: 'text' }), (s.identity || {})[k]);
            if (v !== undefined) ident[k] = v;
        }
        if (nonEmpty(ident)) out.identity = ident;
        for (const [cat, vals] of Object.entries(s.recommended || {})) {
            if (only && !only.includes(cat)) continue;           // another type's, kept in the state
            const spec = cats[cat] || {};
            const row = {};
            for (const [fid, raw] of Object.entries(vals || {})) {
                const v = attrValue((spec.fields || {})[fid] || { type: 'number' }, raw);
                if (v !== undefined) row[fid] = v;
            }
            if (nonEmpty(row)) out[cat] = spec.repeatable ? [row] : row;
        }
        return out;
    }

    // what Name & save holds, as the create's draft: the connection the
    // step sends (with a found connection's own keys), routing left to the
    // gateway's patterns unless typed, HA only with MQTT, the type only when
    // it is not the template's own
    function draftOf(s, ctx) {
        ctx = ctx || {};
        const tpl = ctx.tpl || {};
        let conn = copy(ctx.connection || {});
        // a found device's own keys (an MQTT availability) go with its own
        // kind of connection only, never over what the form says
        const extra = s.connExtra || null;
        if (extra && conn.protocol === 'mqtt') {
            for (const [k, v] of Object.entries(extra)) if (!(k in conn)) conn[k] = copy(v);
        }
        const out = s.out || {};
        const tplKind = tpl.device_kind || tpl.kind || '';
        return {
            id: s.id || idFromName(s.name), name: s.name || '', template: s.template, enabled: s.enabled !== false,
            connection: conn,
            topic_prefix: out.prefix || '', bucket: out.bucket || '', device_tag: out.tag || '',
            ha_discovery_enabled: out.mqtt !== false && out.ha !== false,
            mqtt_enabled: out.mqtt !== false, influxdb_enabled: out.influx !== false,
            type: s.typeId && s.typeId !== tplKind ? s.typeId : '',
            attributes: attributesOf(s, ctx.catalog, ctx.recommendedCats || null),
            components: (s.components || []).filter(c => c && c.on !== false)
                .map(c => Object.assign({ type: c.type, instance: c.instance }, c.name ? { name: c.name } : {})),
        };
    }

    // byte-identical to the old wizard's create (app-wizard.js, its
    // Next on step 3, at ab27652; key order too) for the same choices
    function createPayload(d) {
        const p = {
            id: d.id, name: d.name || d.id, template: d.template, enabled: d.enabled,
            connection: d.connection,
            mqtt: Object.assign(d.topic_prefix ? { topic_prefix: d.topic_prefix } : {}, d.mqtt_enabled === false ? { enabled: false } : {}),
            influxdb: Object.assign({ bucket: d.bucket || undefined, device_tag: d.device_tag || undefined },
                                    d.influxdb_enabled === false ? { enabled: false } : {}),
            ha_discovery_enabled: d.ha_discovery_enabled,
        };
        if (d.type) p.type = d.type;
        if (nonEmpty(d.attributes)) p.attributes = d.attributes;
        if ((d.components || []).length) p.components = d.components;
        return p;
    }

    // ── L3.9: Create another like this · Duplicate ───────────────────────
    const COPY_LIST = {
        copied: ['path', 'template', 'typeId', 'typeTouched', 'conn', 'connExtra', 'enabled', 'out.mqtt', 'out.influx', 'out.ha',
                 'identity.location', 'identity.labels', 'identity.manufacturer', 'identity.model', 'recommended (but grid_connection)'],
        cleared: ['id', 'name (proposed: the next one)', 'unit (the next free on the bus)', 'identity.description',
                  'identity.serial_number', 'identity.firmware', 'identity.installed_on', 'recommended.grid_connection',
                  'components', 'check', 'out.prefix', 'out.bucket', 'out.tag'],
    };
    const KEEP_IDENTITY = ['location', 'labels', 'manufacturer', 'model'];

    function nextName(name) {
        const s = String(name || '');
        if (!s.trim()) return '';
        const m = s.match(/^(.*?)(\d+)(\D*)$/);
        return m ? `${m[1]}${+m[2] + 1}${m[3]}` : `${s} 2`;
    }

    function uniqueId(id, taken) {
        const set = new Set(taken || []);
        if (!set.has(id)) return id;
        let n = 2;
        while (set.has(`${id}-${n}`)) n++;
        return `${id}-${n}`;
    }

    // the connection step's own rule (ConnStepModel.addressInfo): the wanted
    // address when free, else the next free one after it, 1..247 around
    function nextFreeUnit(used, want) {
        const taken = new Set((used || []).map(Number));
        const w = Number.isInteger(want) ? want : 1;
        if (!taken.has(w)) return w;
        for (let i = 0; i < 247; i++) {
            const u = ((w - 1 + i) % 247) + 1;
            if (!taken.has(u)) return u;
        }
        return null;
    }

    // the connection kept, its address taken afresh where it is mounted (the
    // bus read again holds the device just made: the next free one follows)
    function connAgain(conn) {
        if (!conn) return null;
        const c = copy(conn);
        c.M = Object.assign(c.M || {}, {});
        if (conn.M && conn.M.keepalive !== undefined) c.M.keepalive = conn.M.keepalive;
        c.cs = Object.assign({ axes: null, role: null, touched: {}, busId: null, announce: '' }, c.cs || {});
        c.fromBus = c.cs.busId !== undefined && c.cs.busId !== null ? c.cs.busId : (conn.fromBus ?? null);
        c.cs.touched = Object.assign({}, c.cs.touched, { unit: false });
        c.cs.busId = null;
        return c;
    }

    function another(s) {
        const out = blank();
        out.path = s.path === 'measured' || s.path === 'many' ? 'model' : s.path;
        out.step = 'connection';
        out.template = s.template;
        out.type = s.type || '';
        out.typeId = s.typeId || '';
        out.typeTouched = !!s.typeTouched;
        out.conn = connAgain(s.conn);
        out.connExtra = s.connExtra ? copy(s.connExtra) : null;
        out.enabled = s.enabled !== false;
        out.out = Object.assign(out.out, { mqtt: s.out.mqtt !== false, influx: s.out.influx !== false, ha: s.out.ha !== false });
        for (const k of KEEP_IDENTITY) out.identity[k] = (s.identity || {})[k] || '';
        out.recommended = Object.fromEntries(Object.entries(copy(s.recommended || {})).filter(([c]) => c !== 'grid_connection'));
        if (s.nameTouched && String(s.name || '').trim()) { out.name = nextName(s.name); out.nameTouched = true; }
        return out;
    }

    // ⋯ Duplicate: a stored device through the same copy list. Its
    // connection opens as a prefilled one (the role the bus's claims give,
    // with their reason), its address the next free on that bus
    function fromEntry(e, attrs, CM) {
        e = e || {};
        attrs = attrs || {};
        const s = blank();
        const L = landing({ source: 'duplicate', template: e.template });
        s.path = L.path; s.step = L.step;
        s.template = e.template || '';
        if (e.kind_source === 'override' && (e.type || e.kind)) { s.typeId = e.type || e.kind; s.typeTouched = true; }
        const M = CM.model(e);
        const pwCleared = !!M.has_password;          // never copied: typed again, and said so
        M.password = '';
        M.has_password = false;
        s.conn = { M, cs: { axes: null, role: null, touched: { unit: false, line: false }, busId: null, announce: '' },
                   prefilled: true, fromBus: null, tplId: s.template || null, pwCleared };
        s.enabled = e.enabled !== false;
        s.out = Object.assign(s.out, { mqtt: e.mqtt_enabled !== false, influx: e.influxdb_enabled !== false,
                                       ha: e.ha_discovery_enabled !== false });
        const ident = attrs.identity || {};
        for (const k of KEEP_IDENTITY) {
            const v = ident[k];
            s.identity[k] = Array.isArray(v) ? v.join(', ') : (v || '');
        }
        for (const [cat, v] of Object.entries(attrs)) {
            if (cat === 'identity' || cat === 'grid_connection' || cat === 'location') continue;
            const row = Array.isArray(v) ? v[0] : v;
            if (row && typeof row === 'object') s.recommended[cat] = Object.fromEntries(Object.entries(row).map(([k, x]) => [k, String(x)]));
        }
        if (String(e.name || '').trim()) { s.name = nextName(e.name); s.nameTouched = true; }
        return s;
    }

    // ── L3.9: the review strip, from the dry run's would-be entry ─────────
    function reviewStrip(e, t, outs) {
        const tr = typeof t === 'function' ? t : (k, d) => d;
        const o = Object.assign({ mqtt: true, influx: true, ha: true }, outs || {});
        e = e || {};
        const w = e.wire || {};
        const word = (k, v) => (v === undefined || v === null || v === '' || v === false ? '' : tr(`adddev.wire.${k}.${v}`, String(v)));
        const row = (k, label, v) => ({ k, label, v });
        const groups = [];
        groups.push({ id: 'identity', step: 'save', label: tr('adddev.rv.identity', 'Identity'), rows: [
            row('name', tr('devices.wizard.name', 'Device name'), e.name || ''),
            row('id', tr('devices.wizard.id', 'Device id'), e.id || ''),
            row('type', tr('adddev.fact.type', 'Type'), e.type || ''),
            row('enabled', tr('adddev.rv.reading', 'Reading'), e.enabled === false ? tr('adddev.off', 'off') : tr('adddev.on', 'on')),
        ] });
        const wire = [word('port', w.port), word('reach', w.reach),
                      [word('protocol', w.protocol), word('framing', w.framing)].filter(Boolean).join(' '),
                      word('mode', w.mode)].filter(Boolean).join(' · ');
        const crows = [row('wire', tr('adddev.rv.wire', 'Connection'), wire || e.protocol || '')];
        if (e.bus && typeof e.bus === 'object') {
            const who = [];
            if ((e.bus.pollers || []).length) who.push(tr('adddev.rv.polledBy', 'polled by {list}').replace('{list}', e.bus.pollers.join(', ')));
            if ((e.bus.listeners || []).length) who.push(tr('adddev.rv.listenedBy', 'listened to by {list}').replace('{list}', e.bus.listeners.join(', ')));
            if (!who.length) who.push(tr('adddev.rv.alone', 'nobody else on it'));
            crows.push(row('bus', tr('adddev.rv.bus', 'Bus it joins'),
                [e.bus.where, tr(`adddev.wire.role.${e.bus.role}`, String(e.bus.role || '')), ...who].filter(Boolean).join(' · ')));
        }
        groups.push({ id: 'connection', step: 'connection', label: tr('adddev.step.connection', 'Connection'), rows: crows });
        groups.push({ id: 'map', step: 'what', label: tr('adddev.rv.map', 'Model'), rows: [row('template', tr('adddev.rv.template', 'Template'), e.template || '')] });
        if ((e.components || []).length) {
            groups.push({ id: 'inside', step: 'check', label: tr('adddev.rv.inside', 'Inside it'),
                rows: e.components.map(c => row(c.id, c.id, `${c.name || c.id} (${c.type} ${c.instance})`)) });
        }
        const off = tr('adddev.off', 'off');
        groups.push({ id: 'outputs', step: 'save', label: tr('adddev.rv.outputs', 'Outputs'), rows: [
            row('mqtt', 'MQTT', o.mqtt ? (e.topic_prefix || '') : off),
            row('influxdb', 'InfluxDB', o.influx ? [e.bucket, e.device_tag].filter(Boolean).join(' · ') : off),
            row('ha', 'Home Assistant', o.mqtt && o.ha ? (e.ha_node || '') : off),
            row('opcua', 'OPC UA', e.opcua_enabled ? tr('adddev.on', 'on') : off),
        ] });
        const wrows = [];
        if (w.writable === false) wrows.push(row('lock', tr('adddev.rv.writes', 'Writes'), tr('adddev.rv.writesNone', 'none')));
        else {
            wrows.push(row('lock', tr('adddev.rv.writes', 'Writes'), e.write_locked === false ? tr('adddev.rv.unlocked', 'unlocked') : tr('adddev.rv.locked', 'locked — an admin unlocks them from its ⋯ → Unlock writes…')));
            if (e.setup === 'pending') wrows.push(row('setup', tr('adddev.rv.setup', 'Setup writes'), tr('adddev.rv.pending', 'pending')));
        }
        groups.push({ id: 'writes', step: 'save', label: tr('adddev.rv.writes', 'Writes'), rows: wrows });
        return groups;
    }


    // ── many of the same on one bus (L3.7) ───────────────────────────────
    function scanSays(f) {
        f = f || {};
        const out = [];
        const id = f.fc43 || {};
        if (id.vendor || id.product) out.push(`${id.vendor || ''} ${id.product || ''}${id.version ? ' ' + id.version : ''}`.trim());
        if (f.sunspec) out.push('SunSpec');
        for (const m of (f.matches || [])) out.push(m.name || m.id);
        if (f.answer === 'exception') out.push('exception');
        return out;
    }

    // the model a unit gets: the one chosen in Step 1, else what its
    // fingerprint names (one match only), else none (the row asks)
    function scanModel(f, chosen) {
        if (chosen) return chosen;
        return (f && f.suggested) || '';
    }

    const scanPickable = f => !!f && !f.device;

    // the model's own word: "SDM120 Modbus" → sdm120 (how it is reached is
    // not the model)
    const NOT_MODEL = new Set(['modbus', 'rtu', 'tcp', 'ascii', 'can', 'mqtt', 'http', 'sunspec', 'tap', 'listen']);
    function batchSlug(tpl) {
        const base = tpl ? (tpl.model || tpl.name || tpl.id) : '';
        const words = idFromName(String(base || '')).split('-').filter(w => w && !NOT_MODEL.has(w));
        return words.join('-').slice(0, 24).replace(/-+$/, '') || 'device';
    }

    // {model}-u{unit}; -2, -3 … when a device or another row has it
    function batchId(tpl, unit, taken) {
        const used = taken instanceof Set ? taken : new Set(taken || []);
        const base = `${batchSlug(tpl)}-u${unit}`;
        if (!used.has(base)) return base;
        let n = 2;
        while (used.has(`${base}-${n}`)) n++;
        return `${base}-${n}`;
    }

    function batchRows(units, found, o) {
        o = o || {};
        const tpls = o.templates || [];
        const taken = new Set(o.taken || []);
        const byUnit = new Map((found || []).map(f => [+f.unit_id, f]));
        const rows = [];
        for (const u of [...new Set((units || []).map(Number))].sort((a, b) => a - b)) {
            const f = byUnit.get(u) || { unit_id: u };
            const tid = scanModel(f, o.template || '');
            const tpl = tpls.find(x => x.id === tid) || null;
            const id = batchId(tpl, u, taken);
            taken.add(id);
            const label = tpl ? (tpl.model || tpl.name || tpl.id) : '';
            rows.push({ unit: u, template: tid, id, name: label ? `${label} · ${o.unitWord || 'unit'} ${u}` : `${o.unitWord || 'unit'} ${u}`,
                        status: 'new', why: '' });
        }
        return rows;
    }

    function batchErrors(rows, taken, t) {
        const tr = typeof t === 'function' ? t : (k, d) => d;
        const used = new Set(taken || []);
        const seen = new Map();
        const out = {};
        (rows || []).forEach((r, i) => {
            if (r.status === 'created') return;
            const e = {};
            if (!idValid(r.id)) e.id = tr('devices.wizard.idInvalid', 'Use a-z 0-9 - _ (2-64 chars).');
            else if (used.has(r.id)) e.id = tr('batch.idTaken', 'A device has this id already.');
            else if (seen.has(r.id)) e.id = tr('batch.idTwice', 'Another row has this id.');
            if (!String(r.name || '').trim()) e.name = tr('batch.needName', 'Give it a name.');
            if (!r.template) e.template = tr('batch.needModel', 'Choose its model.');
            if (Object.keys(e).length) out[i] = e;
            seen.set(r.id, i);
        });
        return out;
    }

    // one row's create: THE builder (createPayload, the one Add uses), the
    // unit its own — the old wizard's body, key order too
    function batchPayload(row, shared, connection) {
        shared = shared || {};
        const conn = Object.assign({}, copy(connection || {}), { unit_id: +row.unit });
        return createPayload({
            id: row.id, name: String(row.name || '').trim() || row.id, template: row.template,
            enabled: shared.enabled !== false, connection: conn,
            topic_prefix: shared.topic_prefix || '', bucket: shared.bucket || '', device_tag: '',
            ha_discovery_enabled: shared.ha_discovery_enabled !== false,
            mqtt_enabled: true, influxdb_enabled: true,
        });
    }

    function batchTally(rows) {
        const all = rows || [];
        const created = all.filter(r => r.status === 'created').length;
        const failed = all.filter(r => r.status === 'failed' || r.status === 'refused').length;
        return { created, failed, left: all.length - created, total: all.length };
    }

    const api = { PATHS, stepsOf, blank, setPath, go, position, isDirty, canNext, nextLabel, builtinOf, matchesType,
                  typeChips, chipCounts, sharedLine, adoptLine, searchText, filter, badges, ports, PORT_ICON, measuredKinds, measuredErrors,
                  stepOfField, idFromName, idValid, measuredDraft, meterOnlyPayload, measuredDryRunBody,
                  foundItems, fold, noteText, noteWarn, landing, topicPreset, IDENTITY,
                  blankCheck, checkVerdict, checkPassed, heard, checkRows, identityVerdict, nameplateFrom, applyNameplate,
                  attributesOf, draftOf, createPayload, COPY_LIST, another, fromEntry, checkReset, plateTyped,
                  nextName, uniqueId, nextFreeUnit, reviewStrip,
                  scanSays, scanModel, scanPickable, batchSlug, batchId, batchRows, batchErrors, batchPayload,
                  batchTally };
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root && root.document) root.AddDevModel = api;
})(typeof window !== 'undefined' ? window : globalThis);
