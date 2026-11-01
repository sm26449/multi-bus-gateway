/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// The device shell (ADR-0031 §4, L3.4): one modal frame for editing a device
// in sections — and, from L3.5, for adding one in steps — and the draft it
// edits.
//
// The draft (pure; Node tests it: tools/e2e/device_shell_model_test.mjs)
//   sectionsFor(entry)         the sections a device has, in the rail's order
//                              (Model · Connection · Polling · Identity ·
//                              Outputs) — every kind (ADR-0032 §6, L3.11b):
//                              the primary by its `edit_sections`, a unit and
//                              a component the four of them
//   scopeOf(entry, section)    whose a section is: {own}, {held: installation |
//                              parent, id, name} (said, with the way there),
//                              {shared: installation} (a unit's outputs)
//   fieldsOf(entry, groups, attrs, pq)
//                              the stored values as flat paths
//                              ('identity.name', 'outputs.mqtt.enabled',
//                              'polling.poll_groups.fast', 'about.labels',
//                              'pq.poll_s', …)
//   attrPayload / unitPayload / componentPayload / unitOutputWrites / pqPayload
//                              the writes that are not the device's PATCH
//   savePlan(d, entry, opts)   one Save's writes, in order; saveReport(plan,
//                              results): saved, failed, not tried, what is left
//   localChanges(d, entry)     the changes the server's dry run does not see
//   draft(fields) / set(d, path, v) / changed(d) / isDirty(d)
//   changedSections(d, extra)  the sections with a change (extra: the ones a
//                              form outside the draft says, e.g. the connection)
//   payload(d, {connection})   the PATCH body, only what changed, or null
//   rebase(d, fields)          after a 412: my changes over the version stored
//                              now; {draft, conflicts:[{path, mine, stored, was}]}
//   rebaseModel(M, orig, now)  the same for the connection form's model
//   safety(changes, ctx)       the dry run's changes that need a review before
//                              Save: role, bus move, template, an output off
//   verdict / saveLabel / summaryText
//                              what the dry run decides: the footer line and
//                              the Save button's words
//   sectionOf(path)            where a refusal belongs
//
// The frame (DOM; the page builds it once per open)
//   mount(root, {mode: 'sections', prefix, sections: [{id, label, icon}], t, onSelect})
//     → a shell: select(id, focus), markChanged(ids), setSummary(text, verdict),
//       setSave(label, {disabled, why}), setDiff(rows), showErrors(items),
//       showConflict(text)
//   The rail is a vertical tablist (arrows, Home, End, activation follows
//   focus); under 600 px it is a <select>. The footer is sticky: one line said
//   to screen readers only when the verdict changes, "Show changes", Cancel,
//   Save.
//
//   mount(root, {mode: 'steps', prefix, title, steps: [{id, label}], t, onGo, onRail})
//     → a shell for Add (ADR-0031 §2, L3.5; app-add-device.js). The API the
//     steps of Add are built on (L3.6 connection, L3.7 many, L3.8 check, L3.9
//     name & save each draw into their own panel):
//       setSteps(steps)        the way changed (By connection swaps the first
//                              two, Measured has three): the rail is drawn
//                              again; a panel already drawn is kept, with
//                              whatever was typed in it. Panels are never
//                              dropped (no drop()): a step that must start
//                              afresh empties its own panel(id) and draws
//                              it again (Add keys each panel by way:step)
//       go(id, focus)          the step shown; the rail marks it
//                              aria-current="step"; focus: 'heading' (the
//                              default — the step's question is read first),
//                              an element, or false; Back hidden on the first
//       panel(id) / heading(id) the step's body / its <h4> (tabindex -1)
//       current                the step shown
//       setNext(label, {disabled, why})
//                              Next's words; disabled = aria-disabled (still
//                              focusable, el.next.dataset.why holds the
//                              reason the page says when it is pressed)
//       setSummary(text)       the footer's line (shown, not announced)
//       say(text)              the live region (role=status, polite)
//       showErrors(items, title)
//                              the error summary (role=alert) over the step,
//                              each item {section: step id, text, fieldId} a
//                              link to its field (its step is shown first)
//       el.{title, close, back, next, cancel, summary, live, errors, main, rail}
//     The page wires Back, Next, Cancel and ✕ itself. The rail is an <ol>
//     (aria-label "Steps"); a step before the current one is a button back to
//     it (onRail(id) decides; without it, go(id)), the others are text. Under
//     600 px the dialog is full screen and the rail one line ("Step 2 of 3 ·
//     Meters"). The footer is sticky: the line, Back, Cancel, Next.
//   railItems(steps, current) / stepLine(steps, current, t)
//     the rail's items {id, label, n, state: done|current|todo} and the
//     phone's line (pure; Node tests them)
(function (root) {
    'use strict';

    const SECTIONS = ['model', 'connection', 'polling', 'identity', 'outputs'];
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const copy = v => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
    const isObj = v => v !== null && typeof v === 'object' && !Array.isArray(v);

    function kindOf(e) {
        e = e || {};
        if (e.primary) return 'primary';
        if (e.endpoint_id) return 'unit';
        if (e.component_of) return 'component';
        return (e.protocol || (e.connection || {}).protocol) === 'none' ? 'meter-only' : 'device';
    }

    // the modal edits every kind (ADR-0032 §6, L3.11b). A device measured by
    // its meters has no model and no connection (absent, not disabled). The
    // primary meter shows what its PATCH takes (GET's `edit_sections`: a
    // section with keys), and Identity — its attributes are its own. A unit
    // and a component show the four sections: what is their own is edited,
    // the rest says where it is set (scopeOf)
    const FOUR = ['connection', 'polling', 'identity', 'outputs'];
    function sectionsFor(e) {
        e = e || {};
        const k = kindOf(e);
        if (k === 'meter-only') return ['polling', 'identity', 'outputs'];
        if (k === 'device') return SECTIONS.slice();
        if (k === 'primary') {
            const es = e.edit_sections;
            if (!isObj(es) || !Object.keys(es).length) return FOUR.slice();
            return SECTIONS.filter(s => s === 'identity' || es[s] === null || (Array.isArray(es[s]) && es[s].length > 0));
        }
        return FOUR.slice();
    }

    // whose a section is: {own: true}; {held: 'installation'|'parent', id,
    // name} — set there, said with the way there; {shared: 'installation',
    // id, name} — the installation's, edited here for every unit of it
    function scopeOf(e, sec) {
        e = e || {};
        const k = kindOf(e);
        if (k === 'unit') {
            const inst = { id: e.endpoint_id, name: e.endpoint_name || e.endpoint_id };
            if (sec === 'identity') return { own: true };
            if (sec === 'outputs') return { shared: 'installation', ...inst };
            return { held: 'installation', ...inst };
        }
        if (k === 'component') {
            // its name and its on/off are its own; it is read through its device
            if (sec === 'identity' || sec === 'polling') return { own: true };
            return { held: 'parent', id: e.component_of, name: e.parent_name || e.component_of };
        }
        return { own: true };
    }

    // a path → its section; a server's word (`name`, `template`,
    // `poll_groups.x`) as well as the draft's
    function sectionOf(path) {
        const p = String(path || '');
        const head = p.split('.')[0];
        if (SECTIONS.includes(head)) return head;
        if (head === 'about') return 'identity';
        if (head === 'pq') return 'outputs';
        if (head === 'name' || head === 'type' || head === 'kind') return 'identity';
        if (head === 'template') return 'model';
        if (head === 'enabled' || head === 'poll_groups') return 'polling';
        if (['mqtt', 'influxdb', 'ha_discovery', 'http_output', 'opcua', 'rest_push'].includes(head)) return 'outputs';
        return 'connection';
    }

    // ── the draft ─────────────────────────────────────────────────────────
    const REST_KEYS = ['enabled', 'url', 'interval_s', 'format', 'verify_tls', 'headers'];
    // the attributes' identity category as Identity edits it (ADR-0027):
    // words, where it is mounted, labels to find it by, its nameplate. They
    // live in attributes.yaml under their own revision (ADR-0030) — one Save
    // sends them after the device's own write (savePlan)
    const ABOUT = ['description', 'location', 'labels', 'manufacturer', 'model', 'serial_number', 'firmware', 'installed_on'];
    // the PQ recorder's settings (its own route, POST /api/pq/config): staged
    // with Outputs, sent by the same Save (FINDINGS L3-F21)
    const PQ_KEYS = ['enabled', 'poll_s', 'archive_waveforms', 'base_url'];

    // groups: the poll groups (GET …/poll-groups); attrs: the device's
    // attributes ({identity: {…}}; undefined = not read: no about.* field);
    // pq: its PQ recorder's settings (undefined = it has none)
    function fieldsOf(e, groups, attrs, pq) {
        e = e || {};
        const own = ((e.publishing || {}).own) || {};
        const mq = own.mqtt || {}, ix = own.influxdb || {};
        const rp = e.rest_push || {};
        const meterOnly = kindOf(e) === 'meter-only';
        const f = {
            'identity.name': e.name || e.id || '',
            // blank = derived from its template; a device measured only by its
            // meters always has one (nothing to derive it from)
            'identity.type': (e.kind_source === 'override' || meterOnly) ? (e.type || e.kind || '') : '',
            'model.template': e.template || '',
            'polling.enabled': e.enabled !== false,
            'outputs.mqtt.enabled': e.mqtt_enabled !== false,
            'outputs.ha_discovery': e.ha_discovery_enabled !== false,
            'outputs.mqtt.publish_mode': mq.publish_mode || '',
            'outputs.mqtt.heartbeat_interval': mq.heartbeat_interval ?? null,
            'outputs.influxdb.enabled': e.influxdb_enabled !== false,
            'outputs.influxdb.publish_mode': ix.publish_mode || '',
            'outputs.influxdb.write_interval': ix.write_interval ?? null,
            'outputs.influxdb.heartbeat_interval': ix.heartbeat_interval ?? null,
            'outputs.http_output': e.http_output_enabled === true,
            'outputs.opcua.enabled': e.opcua_enabled === true,
            // a set, kept sorted (a list read again in another order is no change)
            'outputs.opcua.fields': Array.isArray(e.opcua_fields) ? e.opcua_fields.slice().sort() : [],
            'outputs.rest_push.enabled': !!rp.enabled,
            'outputs.rest_push.url': rp.url || '',
            'outputs.rest_push.interval_s': rp.interval_s ?? 30,
            'outputs.rest_push.format': rp.format || 'native',
            'outputs.rest_push.verify_tls': rp.verify_tls !== false,
            'outputs.rest_push.headers': isObj(rp.headers) ? Object.assign({}, rp.headers) : {},
        };
        // what the save keeps of the REST push that the form does not show
        f['outputs.rest_push.timeout'] = rp.timeout ?? null;
        for (const [name, g] of Object.entries(groups || {})) {
            f[`polling.poll_groups.${name}`] = isObj(g) ? (g.interval ?? null) : g;
        }
        if (attrs !== undefined && attrs !== null) {
            const about = isObj(attrs.identity) ? attrs.identity : {};
            for (const k of ABOUT) {
                f[`about.${k}`] = k === 'labels' ? (Array.isArray(about.labels) ? about.labels.slice() : [])
                    : (about[k] === undefined || about[k] === null ? '' : about[k]);
            }
        }
        if (isObj(pq)) {
            f['pq.enabled'] = !!pq.enabled;
            f['pq.poll_s'] = pq.poll_s ?? 300;
            f['pq.archive_waveforms'] = pq.archive_waveforms !== false;
            f['pq.base_url'] = pq.base_url || '';
        }
        return f;
    }

    function draft(fields) {
        return { orig: copy(fields || {}), cur: copy(fields || {}) };
    }

    function set(d, path, v) {
        d.cur[path] = copy(v);
        return d;
    }

    function changed(d) {
        return Object.keys(d.cur).filter(p => !same(d.cur[p], d.orig[p])).sort();
    }

    const isDirty = d => changed(d).length > 0;

    function changedSections(d, extra) {
        const out = new Set(changed(d).map(sectionOf));
        for (const s of extra || []) out.add(s);
        return out;
    }

    // the PATCH: a section only when something in it changed, and in it only
    // what changed — the server keeps the rest (publishing: per key; REST
    // push and OPC UA: their whole block, as their routes take it)
    function payload(d, opts) {
        opts = opts || {};
        const ch = new Set(changed(d));
        const v = p => d.cur[p];
        const secs = {};
        const put = (sec, key, val) => { (secs[sec] = secs[sec] || {})[key] = val; };
        if (ch.has('identity.name')) put('identity', 'name', v('identity.name'));
        if (ch.has('identity.type')) put('identity', 'type', v('identity.type') || null);
        if (ch.has('model.template')) put('model', 'template', v('model.template'));
        if (ch.has('polling.enabled')) put('polling', 'enabled', v('polling.enabled'));
        const pg = {};
        for (const p of ch) {
            // a cleared interval (null): back to the gateway's rhythm for that group
            if (p.startsWith('polling.poll_groups.')) pg[p.slice('polling.poll_groups.'.length)] = v(p) == null ? null : { interval: v(p) };
        }
        if (Object.keys(pg).length) put('polling', 'poll_groups', pg);
        for (const svc of ['mqtt', 'influxdb']) {
            const block = {};
            for (const k of ['enabled', 'publish_mode', 'heartbeat_interval', 'write_interval']) {
                const p = `outputs.${svc}.${k}`;
                if (!ch.has(p)) continue;
                block[k] = k === 'publish_mode' ? (v(p) || null) : v(p);
            }
            if (Object.keys(block).length) put('outputs', svc, block);
        }
        if (ch.has('outputs.ha_discovery')) put('outputs', 'ha_discovery', v('outputs.ha_discovery'));
        if (ch.has('outputs.http_output')) put('outputs', 'http_output', v('outputs.http_output'));
        if (ch.has('outputs.opcua.enabled') || ch.has('outputs.opcua.fields')) {
            put('outputs', 'opcua', { enabled: v('outputs.opcua.enabled'), fields: copy(v('outputs.opcua.fields')) || [] });
        }
        if (REST_KEYS.some(k => ch.has(`outputs.rest_push.${k}`))) {
            const rp = {};
            for (const k of REST_KEYS) rp[k] = copy(v(`outputs.rest_push.${k}`));
            if (v('outputs.rest_push.timeout') != null) rp.timeout = v('outputs.rest_push.timeout');
            put('outputs', 'rest_push', rp);
        }
        if (isObj(opts.connection) && Object.keys(opts.connection).length) secs.connection = copy(opts.connection);
        if (!Object.keys(secs).length) return null;
        const ordered = {};                 // in the rail's order (a diff reads the same)
        for (const s of SECTIONS) if (secs[s]) ordered[s] = secs[s];
        return { sections: ordered };
    }

    // ── the writes that are not the device's PATCH (L3.11b) ──────────────
    const changedUnder = (d, pfx) => changed(d).filter(p => p.startsWith(pfx));

    // the attributes' identity category: the whole category PUT (what is
    // stored and not shown here kept: `stored`), or DELETE when every field
    // is blank (the server refuses it empty); null: nothing changed
    function attrPayload(d, stored) {
        if (!changedUnder(d, 'about.').length) return null;
        const value = {};
        for (const [k, v] of Object.entries(isObj(stored) ? stored : {})) if (!ABOUT.includes(k)) value[k] = copy(v);
        for (const k of ABOUT) {
            const v = d.cur[`about.${k}`];
            if (Array.isArray(v)) {
                const xs = v.map(x => String(x).trim()).filter(Boolean);
                if (xs.length) value[k] = xs;
            } else if (typeof v === 'string') {
                if (v.trim()) value[k] = v.trim();
            } else if (v !== undefined && v !== null) {
                value[k] = v;
            }
        }
        return Object.keys(value).length ? { method: 'PUT', value } : { method: 'DELETE' };
    }

    // a unit: its name, by the unit route (PATCH /api/endpoints/{ep}/units/{id})
    function unitPayload(d) {
        if (!changed(d).includes('identity.name')) return null;
        return { name: String(d.cur['identity.name'] ?? '').trim() };
    }

    // a component: its name and on/off, by its device's route
    // (PATCH /api/devices/{parent}/components/{id})
    function componentPayload(d) {
        const ch = changed(d);
        const out = {};
        if (ch.includes('identity.name')) out.name = String(d.cur['identity.name'] ?? '').trim();
        if (ch.includes('polling.enabled')) out.enabled = !!d.cur['polling.enabled'];
        return Object.keys(out).length ? out : null;
    }

    // a unit's outputs are its installation's: each by the route that sets
    // it for every unit of it, in a fixed order — [{route, paths, body}]
    function unitOutputWrites(d) {
        const ch = new Set(changed(d));
        const v = p => copy(d.cur[p]);
        const out = [];
        if (ch.has('outputs.http_output')) out.push({ route: 'http-output', paths: ['outputs.http_output'], body: { enabled: !!v('outputs.http_output') } });
        const op = ['outputs.opcua.enabled', 'outputs.opcua.fields'].filter(p => ch.has(p));
        if (op.length) out.push({ route: 'opcua-output', paths: op, body: { enabled: !!v('outputs.opcua.enabled'), fields: v('outputs.opcua.fields') || [] } });
        const rp = REST_KEYS.map(k => `outputs.rest_push.${k}`).filter(p => ch.has(p));
        if (rp.length) {
            const body = {};
            for (const k of REST_KEYS) body[k] = v(`outputs.rest_push.${k}`);
            if (v('outputs.rest_push.timeout') != null) body.timeout = v('outputs.rest_push.timeout');
            out.push({ route: 'rest-push', paths: rp, body });
        }
        const pub = {}, pubPaths = [];
        for (const svc of ['mqtt', 'influxdb']) {
            for (const k of ['publish_mode', 'heartbeat_interval', 'write_interval']) {
                const p = `outputs.${svc}.${k}`;
                if (!ch.has(p)) continue;
                (pub[svc] = pub[svc] || {})[k] = k === 'publish_mode' ? (v(p) || null) : v(p);
                pubPaths.push(p);
            }
        }
        if (pubPaths.length) out.push({ route: 'publishing', paths: pubPaths, body: pub });
        return out;
    }

    // the PQ recorder: its whole settings for its route; null when unchanged
    function pqPayload(d) {
        if (!('pq.enabled' in d.cur) || !changedUnder(d, 'pq.').length) return null;
        // as typed (never a blank read as 0): localErrors refuses what is not seconds
        return { enabled: !!d.cur['pq.enabled'], poll_s: d.cur['pq.poll_s'],
                 archive_waveforms: !!d.cur['pq.archive_waveforms'], base_url: String(d.cur['pq.base_url'] || '').trim() };
    }

    // a value as Python's int() reads it (NaN where it raises): a number
    // truncated, a whole number's text, a boolean as 1/0
    function pyInt(x) {
        if (x === null || x === undefined) return NaN;
        if (typeof x === 'boolean') return x ? 1 : 0;
        if (typeof x === 'number') return Number.isFinite(x) ? Math.trunc(x) : NaN;
        const t = String(x).trim();
        return /^[-+]?\d+$/.test(t) ? parseInt(t, 10) : NaN;
    }

    // what a route that has no dry run would refuse, judged before Save (the
    // review of L3.11b, m5 and m9): the PQ recorder's interval (a number of
    // seconds, at least 30 — as the old form took it); a unit's or a
    // component's name (at most 80 characters); a unit's REST push (a URL,
    // http or https, when it is on; at least 5 s). {path: words}; t: the
    // page's translator (words in English without it)
    function localErrors(d, e, t) {
        const tr = typeof t === 'function' ? t : (k, dflt) => dflt;
        const k = kindOf(e);
        const ch = new Set(changed(d));
        const out = {};
        const v = p => d.cur[p];
        if ('pq.enabled' in d.cur && [...ch].some(p => p.startsWith('pq.'))) {
            const n = v('pq.poll_s');
            if (typeof n !== 'number' || !Number.isFinite(n) || n < 30) out['pq.poll_s'] = tr('devedit.err.pqPoll', 'Seconds, at least 30');
        }
        if ((k === 'unit' || k === 'component') && ch.has('identity.name') && String(v('identity.name') || '').trim().length > 80) {
            out['identity.name'] = tr('devedit.err.name80', 'At most 80 characters');
        }
        if (k === 'unit' && REST_KEYS.some(x => ch.has(`outputs.rest_push.${x}`))) {
            // the server's rule exactly (device_patch.rest_push_clean; one table
            // of cases holds both: tests/fixtures/rest_push_rule_cases.json):
            // a URL that starts with http:// or https:// (lower case) only
            // while the push is on; an interval Python's int() reads as ≥ 5
            const url = String(v('outputs.rest_push.url') ?? '').trim();
            if (v('outputs.rest_push.enabled') && !(url.startsWith('http://') || url.startsWith('https://'))) {
                out['outputs.rest_push.url'] = tr('devedit.err.restUrl', 'A URL (http:// or https://) while the push is on');
            }
            const iv = pyInt(v('outputs.rest_push.interval_s'));
            if (!Number.isFinite(iv) || iv < 5) out['outputs.rest_push.interval_s'] = tr('devedit.err.restInterval', 'Seconds, at least 5');
        }
        return out;
    }

    // rows re-applied over the rows stored now (a 412 of a list with its own
    // revision: the full Measurements editor's selection, ADR-0030): what I
    // added, removed or changed since `base` goes over `stored`; what only
    // they changed is taken; a row we both changed is a conflict (mine kept,
    // theirs said). {rows, conflicts: [{key, mine, stored, was}]}
    function rebaseRows(base, mine, stored, keyOf) {
        const idx = list => new Map((list || []).map(r => [String(keyOf(r)), r]));
        const B = idx(base), M = idx(mine), S = idx(stored);
        const rows = [];
        const conflicts = [];
        const theirs = k => (B.has(k) !== S.has(k)) || (B.has(k) && !same(B.get(k), S.get(k)));
        for (const [k, s] of S) {
            const inB = B.has(k), inM = M.has(k);
            if (inB && !inM) {                       // I removed it
                if (!same(B.get(k), s)) conflicts.push({ key: k, mine: null, stored: copy(s), was: copy(B.get(k)) });
                continue;
            }
            if (inM && inB && !same(M.get(k), B.get(k))) {   // I changed it
                rows.push(copy(M.get(k)));
                if (theirs(k) && !same(M.get(k), s)) conflicts.push({ key: k, mine: copy(M.get(k)), stored: copy(s), was: copy(B.get(k)) });
                continue;
            }
            if (inM && !inB && !same(M.get(k), s)) {         // we both added it
                rows.push(copy(M.get(k)));
                conflicts.push({ key: k, mine: copy(M.get(k)), stored: copy(s), was: null });
                continue;
            }
            rows.push(copy(s));                      // as stored now
        }
        for (const [k, m] of M) {
            if (S.has(k)) continue;
            if (!B.has(k)) { rows.push(copy(m)); continue; }  // I added it
            // they removed a row I kept: changed by me → kept (a conflict); untouched → their removal
            if (!same(m, B.get(k))) { rows.push(copy(m)); conflicts.push({ key: k, mine: copy(m), stored: null, was: copy(B.get(k)) }); }
        }
        return { rows, conflicts };
    }

    // one Save, its writes in order: the device's own (PATCH; a unit's
    // name; a component's name and on/off), a unit's installation outputs,
    // the attributes, the PQ recorder — [{id, paths, body?|writes?}]. A
    // device's PATCH is sent as its dry run checked it (opts.connection: the
    // connection form's part)
    function savePlan(d, e, opts) {
        const k = kindOf(e);
        const ch = changed(d);
        const own = ch.filter(p => !p.startsWith('about.') && !p.startsWith('pq.'));
        const steps = [];
        if (k === 'unit') {
            const body = unitPayload(d);
            if (body) steps.push({ id: 'unit', paths: ['identity.name'], body });
            const ws = unitOutputWrites(d);
            if (ws.length) steps.push({ id: 'outputs', paths: ws.flatMap(w => w.paths), writes: ws });
        } else if (k === 'component') {
            const body = componentPayload(d);
            if (body) steps.push({ id: 'component', paths: own.filter(p => p === 'identity.name' || p === 'polling.enabled'), body });
        } else {
            const body = payload(d, opts);
            if (body) steps.push({ id: 'device', paths: own.concat(body.sections.connection ? ['connection'] : []), body });
        }
        const about = ch.filter(p => p.startsWith('about.'));
        if (about.length) steps.push({ id: 'attributes', paths: about });
        const pq = pqPayload(d);
        if (pq) steps.push({ id: 'pq', paths: ch.filter(p => p.startsWith('pq.')), body: pq });
        return steps;
    }

    // the changes the server's dry run does not see, said by the page (the
    // footer, the diff): the attributes and the PQ recorder of a device; every
    // change of a unit or a component (no dry run for them)
    function localChanges(d, e) {
        const k = kindOf(e);
        const all = k === 'unit' || k === 'component';
        return changed(d).filter(p => all || p.startsWith('about.') || p.startsWith('pq.'))
            .map(p => ({ section: sectionOf(p), field: p, from: copy(d.orig[p]), to: copy(d.cur[p]) }));
    }

    // after a Save: what was saved, the write that failed, the writes not
    // tried; `left` — the fields still to save (their edits stay on screen)
    function saveReport(plan, results) {
        results = results || {};
        const saved = [], skipped = [];
        let failed = null;
        for (const s of plan || []) {
            const r = results[s.id];
            if (failed) { skipped.push(s.id); continue; }
            if (r && r.ok) saved.push(s.id);
            else if (r) failed = { id: s.id, error: r.error || '' };
            else skipped.push(s.id);
        }
        const notSaved = new Set([...(failed ? [failed.id] : []), ...skipped]);
        const left = (plan || []).filter(s => notSaved.has(s.id)).flatMap(s => s.paths || []);
        return { saved, failed, skipped, left };
    }

    // after a 412: the version stored now is the base; my changes go over
    // it; what only they changed is taken; a field we both changed is a
    // conflict (mine kept, theirs said). Nothing is saved.
    function rebase(d, fields) {
        const mine = changed(d);
        const nd = draft(fields);
        const conflicts = [];
        for (const p of mine) {
            nd.cur[p] = copy(d.cur[p]);
            if (!same(fields[p], d.orig[p]) && !same(fields[p], d.cur[p])) {
                conflicts.push({ path: p, mine: copy(d.cur[p]), stored: copy(fields[p]), was: copy(d.orig[p]) });
            }
        }
        return { draft: nd, conflicts };
    }

    // the same for a form's model (the connection form): its keys I changed
    // over the model of the version stored now
    function rebaseModel(M, orig, now) {
        const out = Object.assign(copy(now) || {}, {});
        const conflicts = [];
        for (const k of Object.keys(M || {})) {
            if (same(M[k], (orig || {})[k])) continue;
            out[k] = copy(M[k]);
            if (!same((now || {})[k], (orig || {})[k]) && !same((now || {})[k], M[k])) {
                conflicts.push({ key: k, mine: copy(M[k]), stored: copy((now || {})[k]), was: copy((orig || {})[k]) });
            }
        }
        return { M: out, conflicts };
    }

    // ── the changes that need a review before Save ────────────────────────
    // role (Master ↔ Listen-only): two masters on a bus, or none; a bus move:
    // the device leaves one bus and joins another; a template: the map and
    // the selection change; an output going off: a consumer stops getting
    // values. The server's own changes are judged (never the UI's guess).
    const LISTEN_PROTOCOLS = ['rtu_tap'];
    // where a host:port is the bus a device sits on (Modbus over the
    // network, a converter, a tap heard over the network)
    const NET_BUS_PROTOCOLS = ['tcp', 'udp', 'rtu_tap', 'rtu-tcp', 'ascii-tcp', 'seplos_v2-tcp'];
    // what names a bus whatever the protocol: a serial port, a bridge and
    // its port, the adapter a bus is bound to, a CAN link
    const BUS_FIELDS = ['serial_port', 'bridge', 'bridge_port', 'adapter', 'channel', 'carrier'];
    const OFF_DEFAULT_ON = ['mqtt.enabled', 'influxdb.enabled', 'ha_discovery'];
    const OFF_DEFAULT_OFF = ['http_output.enabled', 'http_output', 'rest_push.enabled', 'opcua.enabled'];

    function safety(changes, ctx) {
        ctx = ctx || {};
        const out = [];
        const listens = p => LISTEN_PROTOCOLS.includes(String(p || ''));
        for (const c of changes || []) {
            const sec = c.section, f = String(c.field || '');
            let kind = null;
            if (sec === 'connection') {
                if (f === 'protocol' && listens(c.from) !== listens(c.to)) kind = 'role';
                else if (f === 'mode' && (c.from === 'listen') !== (c.to === 'listen')) kind = 'role';
                else if (BUS_FIELDS.includes(f)) kind = 'bus';
                else if ((f === 'host' || f === 'port') && NET_BUS_PROTOCOLS.includes(String(ctx.protocol || ''))) kind = 'bus';
            } else if (sec === 'model' && f === 'template') {
                kind = 'template';
            } else if (sec === 'polling' && f === 'enabled' && c.to === false && c.from !== false) {
                kind = 'acquisition-off';           // nothing is read: every consumer stops
            } else if (sec === 'outputs' && c.to === false) {
                if (OFF_DEFAULT_ON.includes(f) && c.from !== false) kind = 'output-off';
                else if (OFF_DEFAULT_OFF.includes(f) && c.from === true) kind = 'output-off';
                // OPC UA with no block of its own (a device from before the
                // choice) is served: the device's state says so (ctx.opcuaOn)
                else if (f === 'opcua.enabled' && c.from == null && ctx.opcuaOn === true) kind = 'output-off';
            }
            if (kind) out.push({ kind, section: sec, field: f, from: c.from, to: c.to });
        }
        return out;
    }

    // ── what the dry run decides ──────────────────────────────────────────
    // dry: null (nothing to check) | {pending} | {error: {errors, fields}}
    //      | the dry run's answer {changes, restart_required, effects, locks}
    function verdict(dry) {
        if (!dry) return 'none';
        if (dry.pending) return 'checking';
        if (dry.error) return 'error';
        if (dry.restart_required) return 'restart';
        return (dry.changes || []).length ? 'live' : 'none';
    }

    function saveLabel(dry, t) {
        const tr = typeof t === 'function' ? t : (k, d) => d;
        return verdict(dry) === 'restart' ? tr('devedit.saveRestart', 'Save & restart acquisition') : tr('common.save', 'Save');
    }

    // tn(key, n, fallback, params): the page's plural translator
    function summaryText(dry, tn) {
        const tr = typeof tn === 'function' ? tn : (k, n, d) => String(d).replace('{n}', n);
        const v = verdict(dry);
        const n = ((dry || {}).changes || []).length;
        if (v === 'checking') return tr('devedit.sum.checking', 0, 'Checking…');
        if (v === 'error') {
            const e = dry.error || {};
            const m = Math.max(1, (e.errors || []).length, Object.keys(e.fields || {}).length);
            return tr('devedit.sum.error', m, '{n} problems to fix before saving');
        }
        if (v === 'restart' && !n) return tr('devedit.sum.reconnect', 0, 'No changes · it reconnects (enabled, not running)');
        if (v === 'restart') return tr('devedit.sum.restart', n, '{n} changes · restarts acquisition');
        if (v === 'live') return tr('devedit.sum.live', n, '{n} changes · applies live');
        return tr('devedit.sum.none', 0, 'No changes');
    }

    // ── steps (Add) ───────────────────────────────────────────────────────
    function railItems(steps, current) {
        const list = steps || [];
        let i = list.findIndex(s => s.id === current);
        if (i < 0) i = 0;
        return list.map((s, k) => ({ id: s.id, label: s.label, n: k + 1, state: k < i ? 'done' : k === i ? 'current' : 'todo' }));
    }

    function stepLine(steps, current, t) {
        const tr = typeof t === 'function' ? t
            : (k, d, p) => String(d).replace(/\{(\w+)\}/g, (m, x) => (p && x in p ? p[x] : m));
        const items = railItems(steps, current);
        const cur = items.find(x => x.state === 'current') || { n: 0, label: '' };
        return tr('adddev.stepLine', 'Step {n} of {of} · {label}', { n: cur.n, of: items.length, label: cur.label });
    }

    // ── the frame ─────────────────────────────────────────────────────────
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

    function mount(rootEl, o) {
        const doc = rootEl.ownerDocument;
        const px = o.prefix || 'devShell';
        const t = typeof o.t === 'function' ? o.t : (k, d) => d;
        const secs = o.sections || [];
        if (o.mode === 'steps') return mountSteps(rootEl, o);
        if ((o.mode || 'sections') !== 'sections') throw new Error(`device-shell: no mode ${o.mode}`);
        const tab = s => `${px}Tab-${s.id}`;
        const panel = s => `${px}Panel-${s.id}`;
        rootEl.innerHTML = `
            <div class="modal-header dev-shell-head">
                <h3 id="${px}Title">${esc(o.title || '')}</h3>
                <button type="button" class="modal-close" id="${px}Close" aria-label="${esc(t('common.close', 'Close'))}">&times;</button>
            </div>
            <div class="dev-shell-body">
                <div class="dev-shell-railsel form-group">
                    <label class="form-label" for="${px}RailSel">${esc(t('devedit.section', 'Section'))}</label>
                    <select class="input" id="${px}RailSel">${secs.map(s => `<option value="${esc(s.id)}">${esc(s.label)}</option>`).join('')}</select>
                </div>
                <div class="dev-shell-rail" role="tablist" aria-orientation="vertical" id="${px}Rail"
                     aria-label="${esc(t('devedit.sections', 'Sections'))}">
                    ${secs.map((s, i) => `<button type="button" role="tab" class="dev-shell-tab" id="${tab(s)}" data-shell-sec="${esc(s.id)}"
                        aria-controls="${panel(s)}" aria-selected="${i === 0}" tabindex="${i === 0 ? 0 : -1}">
                        ${s.icon ? `<i aria-hidden="true" class="bi bi-${esc(s.icon)}"></i>` : ''}<span class="dev-shell-tab-label">${esc(s.label)}</span>
                        <span class="dev-shell-dot" aria-hidden="true" hidden></span>
                        <span class="sr-only" id="${tab(s)}-changed" hidden>${esc(t('devedit.changed', 'changed'))}</span></button>`).join('')}
                </div>
                <div class="dev-shell-main" id="${px}Main">
                    <div class="dev-shell-conflict" id="${px}Conflict" role="alert"></div>
                    <div class="dev-shell-errors" id="${px}Errors" role="alert"></div>
                    ${secs.map((s, i) => `<section class="dev-shell-panel" role="tabpanel" id="${panel(s)}" aria-labelledby="${tab(s)}"
                        data-shell-panel="${esc(s.id)}" ${i === 0 ? '' : 'hidden'}>
                        <h4 class="dev-shell-h" id="${px}H-${esc(s.id)}" tabindex="-1">${esc(s.label)}</h4>
                        <div class="dev-shell-sec"></div></section>`).join('')}
                </div>
            </div>
            <div class="modal-footer dev-shell-foot">
                <div class="dev-shell-diff" id="${px}Diff" hidden></div>
                <div class="dev-shell-foot-row">
                    <div class="dev-shell-status">
                        <span class="dev-shell-sum" id="${px}Summary">${esc(summaryText(null, (k, n, d) => t(k, d, { n })))}</span>
                        <span class="sr-only" id="${px}Live" aria-live="polite"></span>
                        <button type="button" class="btn btn-ghost btn-sm dev-shell-show" id="${px}ShowChanges" aria-expanded="false"
                            aria-controls="${px}Diff" hidden>${esc(t('devedit.showChanges', 'Show changes'))}</button>
                    </div>
                    <div class="dev-shell-actions">
                        <button type="button" class="btn btn-ghost btn-sm" id="${px}Cancel">${esc(t('common.cancel', 'Cancel'))}</button>
                        <button type="button" class="btn btn-primary btn-sm" id="${px}Save" aria-describedby="${px}Summary">${esc(t('common.save', 'Save'))}</button>
                    </div>
                </div>
            </div>`;
        const $ = id => doc.getElementById(px + id);
        const tabs = [...rootEl.querySelectorAll('[role="tab"][data-shell-sec]')];
        const shell = {
            prefix: px, current: secs[0] ? secs[0].id : null, verdict: 'none',
            panel: id => rootEl.querySelector(`[data-shell-panel="${id}"] .dev-shell-sec`),
            heading: id => doc.getElementById(`${px}H-${id}`),
            el: { title: $('Title'), close: $('Close'), save: $('Save'), cancel: $('Cancel'), show: $('ShowChanges'),
                  diff: $('Diff'), summary: $('Summary'), live: $('Live'), errors: $('Errors'), conflict: $('Conflict'),
                  railSel: $('RailSel'), main: $('Main') },

            // a section shown (focus: 'heading', 'tab', an element, or nothing)
            select(id, focus) {
                if (!secs.some(s => s.id === id)) id = secs[0] && secs[0].id;
                if (!id) return;
                shell.current = id;
                for (const b of tabs) {
                    const on = b.dataset.shellSec === id;
                    b.setAttribute('aria-selected', String(on));
                    b.tabIndex = on ? 0 : -1;
                    b.classList.toggle('active', on);
                }
                rootEl.querySelectorAll('[data-shell-panel]').forEach(p => { p.hidden = p.dataset.shellPanel !== id; });
                shell.el.railSel.value = id;
                if (focus === 'heading') shell.heading(id)?.focus();
                else if (focus === 'tab') tabs.find(b => b.dataset.shellSec === id)?.focus();
                else if (focus && typeof focus.focus === 'function') focus.focus();
                if (typeof o.onSelect === 'function') o.onSelect(id);
            },

            // a dot on each changed section; "changed" said once, as its
            // description (the text stays hidden: never in the tab's name too)
            markChanged(ids) {
                const on = new Set(ids || []);
                for (const b of tabs) {
                    const c = on.has(b.dataset.shellSec);
                    b.querySelector('.dev-shell-dot').hidden = !c;
                    if (c) b.setAttribute('aria-describedby', `${b.id}-changed`);
                    else b.removeAttribute('aria-describedby');
                }
                for (const op of shell.el.railSel.options) {
                    const s = secs.find(x => x.id === op.value);
                    op.textContent = s ? `${s.label}${on.has(s.id) ? ' •' : ''}` : op.textContent;
                }
            },

            // the footer line; said aloud only when the verdict changes
            setSummary(text, v) {
                shell.el.summary.textContent = text;
                if (v !== shell.verdict && v !== 'checking') shell.el.live.textContent = text;
                if (v !== 'checking') shell.verdict = v;
            },

            setSave(label, opts) {
                opts = opts || {};
                shell.el.save.textContent = label;
                if (opts.disabled) shell.el.save.setAttribute('aria-disabled', 'true');
                else shell.el.save.removeAttribute('aria-disabled');
            },

            // rows: [{section, field, from, to}] (as the server shows them: masked)
            setDiff(html, has) {
                shell.el.diff.innerHTML = html || '';
                shell.el.show.hidden = !has;
                if (!has) shell.toggleDiff(false);
            },

            toggleDiff(open) {
                const now = open === undefined ? shell.el.diff.hidden : !!open;
                shell.el.diff.hidden = !now;
                shell.el.show.setAttribute('aria-expanded', String(now));
                shell.el.show.textContent = now ? t('devedit.hideChanges', 'Hide changes') : t('devedit.showChanges', 'Show changes');
            },

            // items: [{section, text, fieldId}] — each a link to its field
            showErrors(items, title) {
                showErrorsIn(shell.el.errors, items, title || t('devedit.errorsTitle', 'The gateway refused this change:'),
                             (sec, el) => shell.select(sec, el || 'heading'));
            },

            showConflict(html) {
                shell.el.conflict.innerHTML = html || '';
            },
        };
        // the rail: a vertical tablist, activation follows focus
        rootEl.querySelector('.dev-shell-rail').addEventListener('keydown', ev => {
            const i = tabs.indexOf(doc.activeElement);
            if (i < 0) return;
            const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[ev.key];
            const to = ev.key === 'Home' ? 0 : ev.key === 'End' ? tabs.length - 1
                : step ? (i + step + tabs.length) % tabs.length : null;
            if (to === null) return;
            ev.preventDefault();
            shell.select(tabs[to].dataset.shellSec, 'tab');
        });
        tabs.forEach(b => b.addEventListener('click', () => shell.select(b.dataset.shellSec, 'tab')));
        // the phone's list: the section shown, the focus stays on the list
        shell.el.railSel.addEventListener('change', () => shell.select(shell.el.railSel.value));
        shell.el.show.addEventListener('click', () => shell.toggleDiff());
        return shell;
    }

    // the error summary of both modes: written (and so said) again only when
    // it says something else — a role=alert region re-announces every
    // rewrite, and a dry run runs often; each item a link that shows its field
    function showErrorsIn(box, items, title, show) {
        const doc = box.ownerDocument;
        const sig = JSON.stringify([title || '', (items || []).map(it => [it.section, it.text, it.fieldId])]);
        if (box.dataset.sig === sig) return;
        box.dataset.sig = sig;
        if (!items || !items.length) { box.innerHTML = ''; return; }
        box.innerHTML = `<div class="dev-shell-errbox"><p><b>${esc(title)}</b></p>
            <ul>${items.map((it, i) => `<li>${it.section ? `<a href="#" data-shell-err="${i}">${esc(it.text)}</a>` : esc(it.text)}</li>`).join('')}</ul></div>`;
        box.querySelectorAll('[data-shell-err]').forEach(a => a.addEventListener('click', ev => {
            ev.preventDefault();
            const it = items[+a.dataset.shellErr];
            show(it.section, (it.fieldId && doc.getElementById(it.fieldId)) || null);
        }));
    }

    // ── the frame in steps (Add) ──────────────────────────────────────────
    function mountSteps(rootEl, o) {
        const doc = rootEl.ownerDocument;
        const px = o.prefix || 'devSteps';
        const t = typeof o.t === 'function' ? o.t : (k, d) => d;
        let steps = (o.steps || []).slice();
        rootEl.innerHTML = `
            <div class="modal-header dev-shell-head">
                <h3 id="${px}Title">${esc(o.title || '')}</h3>
                <button type="button" class="modal-close" id="${px}Close" aria-label="${esc(t('common.close', 'Close'))}">&times;</button>
            </div>
            <div class="dev-shell-body dev-shell-stepsbody">
                <nav class="dev-shell-steps" aria-label="${esc(t('adddev.steps', 'Steps'))}">
                    <ol class="dev-shell-stepol" id="${px}Rail"></ol>
                    <p class="dev-shell-stepline" id="${px}StepLine"></p>
                </nav>
                <div class="dev-shell-main" id="${px}Main">
                    <div class="dev-shell-errors" id="${px}Errors" role="alert"></div>
                    <div id="${px}Panels"></div>
                </div>
            </div>
            <div class="modal-footer dev-shell-foot">
                <div class="dev-shell-foot-row">
                    <div class="dev-shell-status">
                        <span class="dev-shell-sum" id="${px}Summary"></span>
                        <span class="sr-only" id="${px}Live" role="status" aria-live="polite"></span>
                    </div>
                    <div class="dev-shell-actions">
                        <button type="button" class="btn btn-ghost btn-sm" id="${px}Back"><i aria-hidden="true" class="bi bi-arrow-left"></i> ${esc(t('common.back', 'Back'))}</button>
                        <button type="button" class="btn btn-ghost btn-sm" id="${px}Cancel">${esc(t('common.cancel', 'Cancel'))}</button>
                        <button type="button" class="btn btn-primary btn-sm" id="${px}Next" aria-describedby="${px}Summary">${esc(t('common.next', 'Next'))}</button>
                    </div>
                </div>
            </div>`;
        const $ = id => doc.getElementById(px + id);
        const panels = $('Panels');
        const shell = {
            prefix: px, mode: 'steps', current: null,
            el: { title: $('Title'), close: $('Close'), back: $('Back'), next: $('Next'), cancel: $('Cancel'),
                  summary: $('Summary'), live: $('Live'), errors: $('Errors'), main: $('Main'), rail: $('Rail') },
            panel: id => panels.querySelector(`[data-shell-panel="${id}"] .dev-shell-sec`),
            heading: id => doc.getElementById(`${px}H-${id}`),

            setSteps(list) {
                steps = (list || []).slice();
                for (const s of steps) {
                    if (panels.querySelector(`[data-shell-panel="${s.id}"]`)) {
                        const h = shell.heading(s.id);
                        if (h) h.textContent = s.label;
                        continue;
                    }
                    panels.insertAdjacentHTML('beforeend', `<section class="dev-shell-panel" data-shell-panel="${esc(s.id)}"
                        aria-labelledby="${px}H-${esc(s.id)}" hidden>
                        <h4 class="dev-shell-h" id="${px}H-${esc(s.id)}" tabindex="-1">${esc(s.label)}</h4>
                        <div class="dev-shell-sec"></div></section>`);
                }
                drawRail();
            },

            go(id, focus) {
                if (!steps.some(s => s.id === id)) id = steps[0] && steps[0].id;
                if (!id) return;
                shell.current = id;
                panels.querySelectorAll('[data-shell-panel]').forEach(p => { p.hidden = p.dataset.shellPanel !== id; });
                drawRail();
                shell.el.back.hidden = steps[0].id === id;
                shell.el.main.scrollTop = 0;
                if (focus === undefined || focus === 'heading') shell.heading(id)?.focus();
                else if (focus && typeof focus.focus === 'function') focus.focus();
                if (typeof o.onGo === 'function') o.onGo(id);
            },
            // the sections mode's word, for what is shared (the error links)
            select(id, focus) { shell.go(id, focus); },

            setNext(label, opts) {
                opts = opts || {};
                shell.el.next.textContent = label;
                if (opts.disabled) shell.el.next.setAttribute('aria-disabled', 'true');
                else shell.el.next.removeAttribute('aria-disabled');
                shell.el.next.dataset.why = opts.disabled ? (opts.why || '') : '';
            },

            setSummary(text) { shell.el.summary.textContent = text || ''; },

            // the same words twice in a row are said again (cleared first)
            say(text) {
                shell.el.live.textContent = '';
                if (text) setTimeout(() => { shell.el.live.textContent = text; }, 40);
            },

            showErrors(items, title) {
                showErrorsIn(shell.el.errors, items, title || t('adddev.errorsTitle', 'Fix these to go on:'),
                             (sec, el) => shell.go(sec, el || 'heading'));
            },
        };
        function drawRail() {
            const done = t('adddev.stepDone', 'done');
            $('Rail').innerHTML = railItems(steps, shell.current).map(it => `<li class="dev-shell-step is-${it.state}"
                data-shell-step="${esc(it.id)}" ${it.state === 'current' ? 'aria-current="step"' : ''}>${it.state === 'done'
                ? `<button type="button" class="dev-shell-stepbtn" data-shell-goto="${esc(it.id)}"><span class="dev-shell-stepn" aria-hidden="true">${it.n}</span><span class="dev-shell-steplbl">${esc(it.label)}</span><span class="sr-only"> (${esc(done)})</span></button>`
                : `<span class="dev-shell-stepn" aria-hidden="true">${it.n}</span><span class="dev-shell-steplbl">${esc(it.label)}</span>`}</li>`).join('');
            $('StepLine').textContent = stepLine(steps, shell.current, t);
        }
        $('Rail').addEventListener('click', ev => {
            const b = ev.target.closest && ev.target.closest('[data-shell-goto]');
            if (!b) return;
            if (typeof o.onRail === 'function') o.onRail(b.dataset.shellGoto);
            else shell.go(b.dataset.shellGoto);
        });
        shell.setSteps(steps);
        return shell;
    }

    const api = { SECTIONS, ABOUT, sectionsFor, scopeOf, sectionOf, fieldsOf, draft, set, changed, isDirty, changedSections, payload,
                  attrPayload, unitPayload, componentPayload, unitOutputWrites, pqPayload, savePlan, localChanges, saveReport,
                  localErrors, rebaseRows,
                  rebase, rebaseModel, safety, verdict, saveLabel, summaryText, mount, kindOf, railItems, stepLine };
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root && root.document) root.DeviceShell = api;
})(typeof window !== 'undefined' ? window : globalThis);
