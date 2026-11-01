/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// "Many of the same on one bus" in the new Add (ADR-0031 §2, design §3 2f,
// L3.7) — augments JanitzaMonitor.prototype; the Add shell (app-add-device.js)
// hands its two steps here.
//
//   many   the connection step (conn-step.js, mounted as it is) with the bus
//          scan panel in its address question (its addressExtra hook):
//          [Scan this bus] when the gateway is the master, [Listen for
//          addresses] when it only listens — POST /api/bus-scan, polled, a
//          row per unit: unit · what it says (FC43, SunSpec, fingerprint) ·
//          suggested model · "already: <device>"; the free ones ticked
//          by choice (a multi-select), "Add N selected" goes on.
//   units  the batch: a row per unit (unit · name · id {model}-u{unit},
//          editable · model), one shared block (start reading, Home
//          Assistant, InfluxDB bucket), then a dry run for every row before
//          anything is created, then the creates one by one, each with its
//          status. A failed row stays, with its reason; created rows are not
//          sent again; nothing half-done is hidden.
// The rows, the ids and the create body are AddDevModel's (adddev-model.js:
// batchRows, batchErrors, batchPayload — every row through createPayload, the one Add uses).
Object.assign(JanitzaMonitor.prototype, {

    _amBatch() {
        const s = this._ad.s;
        if (!s.batch) s.batch = { units: [], found: [], key: '', mode: '', rows: [], heard: null,
                                  shared: { enabled: true, ha_discovery_enabled: true, bucket: '' },
                                  range: { from: 1, to: 32 }, conn: null, state: '', note: '' };
        return s.batch;
    },

    _amTpl() {
        const ad = this._ad;
        return ad.templates.find(x => x.id === ad.s.template) || null;
    },

    // ── the steps ────────────────────────────────────────────────────────
    _amStepHtml(step) {
        const t = (k, d, p) => this.t(k, d, p);
        if (step === 'many') {
            const tpl = this._amTpl();
            return `<p class="ad-back-link"><button type="button" class="btn btn-ghost btn-sm" data-ad-way="model">
                    <i aria-hidden="true" class="bi bi-arrow-left"></i> ${this._esc(t('adddev.backToModels', 'Choose a model instead'))}</button></p>
                <p class="field-hint" id="adManyLead">${this._esc(tpl
                    ? t('am.leadModel', 'Several {model} on one bus: say how the bus is reached, then scan it (or listen to it) and tick the units to add.', { model: tpl.name || tpl.id })
                    : t('am.lead', 'Several devices on one bus: say how the bus is reached, then scan it (or listen to it) and tick the units to add. Each unit gets the model its answer suggests, or the one you choose.'))}</p>
                <div id="adManyConn"></div>`;
        }
        if (step === 'units') return this._amUnitsHtml();
        return '';
    },

    _amWireStep(step, host) {
        if (step !== 'many') return;
        const ad = this._ad, b = this._amBatch();
        const box = host.querySelector('#adManyConn');
        ad.cs = window.mountConnStep(box, {
            context: 'add', px: 'adMany', template: () => this._amTpl(), state: ad.s.csState || null,
            onChange: () => { this._amSyncKey(); this._adFooter(); },
            addressExtra: (form, st, how) => this._amPanelHtml(form, st, how),
        });
        b.state = b.state === 'running' ? '' : b.state;
    },

    // what the steps hold, into the state (Next, Back, the rail, the dirty test)
    _amCollect(s) {
        const ad = this._ad;
        if (s.step === 'many' && ad.cs && document.getElementById('adManyConn')) {
            try { s.csState = ad.cs.state(); } catch (e) { /* not drawn yet */ }
            const b = this._amBatch();
            const from = document.getElementById('adManyFrom'), to = document.getElementById('adManyTo');
            if (from && to) b.range = { from: +from.value || 1, to: +to.value || 32 };
        }
        if (s.step === 'units' && document.getElementById('adUnitsTable')) {
            const b = this._amBatch();
            document.querySelectorAll('#adUnitsTable [data-am-row]').forEach(el => {
                const r = b.rows[+el.dataset.amRow];
                if (r && r.status !== 'created') r[el.dataset.amField] = el.value.trim();
            });
            b.shared = { enabled: !!document.getElementById('adManyEnabled')?.checked,
                         ha_discovery_enabled: !!document.getElementById('adManyHa')?.checked,
                         bucket: (document.getElementById('adManyBucket')?.value || '').trim() };
        }
        return s;
    },

    // the bus the answers point at, as a key: a scan's rows are kept only
    // while the answers still point there
    _amConnKey() {
        const ad = this._ad;
        if (!ad.cs) return '';
        let w = {};
        try { w = ad.cs.wire() || {}; } catch (e) { return ''; }
        const keep = ['protocol', 'serial_port', 'host', 'port', 'bridge', 'bridge_port', 'mode', 'bus_mode'];
        return JSON.stringify(keep.map(k => w[k] ?? null));
    },

    _amSyncKey() {
        const b = this._amBatch();
        const key = this._amConnKey();
        if (b.key && key && b.key !== key && b.state !== 'running') {
            b.found = []; b.units = []; b.key = ''; b.note = ''; b.heard = null;
        }
    },

    // ── the panel in the address question ────────────────────────────────
    _amPanelHtml(form, st, how) {
        const t = (k, d, p) => this.t(k, d, p);
        const b = this._amBatch();
        const role = form.cs.role;
        const listen = role === 'listen' || (how.seplos && form.M.bus_mode === 'listen');
        const asks = !listen && role === 'poll' && !how.seplos;
        const port = form.cs.axes.port;
        let why = '';
        if (!port || !form.cs.axes.protocol) why = t('am.why.conn', 'Say first how the bus is reached.');
        else if (!['serial', 'ethernet'].includes(port)) why = t('am.why.port', 'Units are scanned on an RS-485 bus or a Modbus TCP gateway.');
        else if (how.seplos && form.M.bus_mode === 'poll') why = t('am.why.seplosPoll', 'Seplos packs are not scanned with questions: listen while the master pack polls, or type each pack\'s DIP-switch address.');
        else if (!role && !how.seplos) why = t('am.why.role', 'Say first whether anything else polls this bus (Master or Listen-only).');
        else if (how.seplos && !form.M.bus_mode) why = t('am.why.role', 'Say first whether anything else polls this bus (Master or Listen-only).');
        const running = b.state === 'running';
        const listenOnly = listen && !asks;
        const btn = (mode, label, icon, desc) => {
            const off = !!why || (mode === 'poll' ? !asks : !listen) || running;
            const reason = why || (mode === 'poll' && listenOnly ? t('am.why.listened', 'This bus is only listened to: the gateway never asks on it. Listen for addresses instead.')
                : mode === 'listen' && asks ? t('am.why.polled', 'The gateway is the master here: it asks — Scan this bus.') : running ? t('am.why.running', 'A scan of this bus is running.') : '');
            return `<button type="button" class="btn btn-secondary btn-sm" id="adMany-${mode}" data-am="${mode}"
                ${off ? `aria-disabled="true" data-am-why="${this._esc(reason)}" aria-describedby="adManyWhy"` : ''}>
                <i aria-hidden="true" class="bi ${icon}"></i> ${this._esc(label)}</button>
                <span class="sr-only">${this._esc(desc)}</span>`;
        };
        const range = `<details class="am-range"${b.range.to > 32 || b.range.from > 1 ? ' open' : ''}><summary>${this._esc(t('am.range', 'Addresses {from}–{to}', b.range))}</summary>
            <div class="form-row">
              <div class="form-group"><label class="form-label" for="adManyFrom">${this._esc(t('am.from', 'From'))}</label>
                <input type="number" id="adManyFrom" class="input" min="1" max="247" value="${b.range.from}"></div>
              <div class="form-group"><label class="form-label" for="adManyTo">${this._esc(t('am.to', 'To'))}</label>
                <input type="number" id="adManyTo" class="input" min="1" max="247" value="${b.range.to}"></div>
            </div><p class="field-hint">${this._esc(t('am.rangeHint', '1–32 by default; up to 1–247. A silent address costs half a second.'))}</p></details>`;
        return `<div class="am-panel" id="adManyPanel" role="group" aria-labelledby="adManyTitle">
            <p class="am-title" id="adManyTitle"><i aria-hidden="true" class="bi bi-search"></i> ${this._esc(t('am.title', 'Find the units on this bus'))}</p>
            <div class="am-actions">
              ${btn('poll', t('am.scan', 'Scan this bus'), 'bi-broadcast-pin', t('am.scanDesc', 'asks each address, reads only'))}
              ${btn('listen', t('am.listen', 'Listen for addresses'), 'bi-ear', t('am.listenDesc', 'transmits nothing'))}
              ${running ? `<button type="button" class="btn btn-ghost btn-sm" data-am="stop">${this._esc(t('am.stop', 'Stop'))}</button>` : ''}
            </div>
            ${asks ? range : ''}
            <p class="field-hint" id="adManyWhy">${this._esc(why || (asks ? t('am.scanHint', 'Reads only (FC3/FC4, the device identification FC43), one address at a time through the bus\'s own connection: the devices on it keep reading in between.')
                : listen ? t('am.listenHint', 'Listens to what the master asks and the units answer, for up to {s} s. Nothing is transmitted.', { s: 15 }) : ''))}</p>
            <p class="am-state" id="adManyState" role="status" aria-live="polite">${this._esc(b.note || '')}</p>
            ${b.warn ? `<p class="field-hint am-warn" id="adManyWarn"><i aria-hidden="true" class="bi bi-hourglass-split"></i> ${this._esc(b.warn)}</p>` : ''}
            <div id="adManyRows">${this._amRowsHtml()}</div>
        </div>`;
    },

    _amRowsHtml() {
        const t = (k, d, p) => this.t(k, d, p);
        const M = window.AddDevModel, b = this._amBatch(), ad = this._ad;
        if (!b.found.length) return '';
        const listen = b.mode === 'listen';
        const name = id => ((ad.templates || []).find(x => x.id === id) || {}).name || id;
        const pick = b.found.filter(M.scanPickable);
        const rows = b.found.map(f => {
            const u = f.unit_id;
            const free = M.scanPickable(f);
            const on = b.units.includes(u);
            const says = listen
                ? [t('am.heard', 'asked {a} · answered {n}', { a: f.asked, n: f.answered }), ...(f.pattern || []).slice(0, 3),
                   f.every_s ? t('am.every', 'every {s} s', { s: f.every_s }) : ''].filter(Boolean).join(' · ')
                : f.answer === 'known' ? t('am.notAsked', 'not asked — a device of this gateway has this address')
                : (M.scanSays(f).join(' · ') || t('am.says.nothing', 'answers; does not say what it is'));
            const sugg = f.suggested ? name(f.suggested) : '';
            return `<tr class="${on ? 'on' : ''}">
                <td><input type="checkbox" id="adManyU-${u}" data-am-unit="${u}" ${on ? 'checked' : ''}
                    ${free ? '' : `aria-disabled="true" data-am-why="${this._esc(t('am.already', 'already: {id}', { id: f.device }))}"`}
                    aria-label="${this._esc(t('am.tick', 'Add unit {u}', { u }))}"></td>
                <td><label for="adManyU-${u}"><code>${u}</code></label></td>
                <td>${this._esc(says)}</td>
                <td>${sugg ? this._esc(sugg) : '<span class="field-hint">—</span>'}</td>
                <td>${f.device ? `<span class="sink-pill info">${this._esc(t('am.already', 'already: {id}', { id: f.device }))}</span>` : ''}</td></tr>`;
        }).join('');
        const n = b.units.length;
        return `<div class="am-table-wrap"><table class="tplu-table am-table" id="adManyTable">
            <caption class="sr-only">${this._esc(t('am.caption', 'Units found on the bus'))}</caption>
            <thead><tr><th scope="col"><input type="checkbox" id="adManyAll" data-am="all" ${pick.length && pick.every(f => b.units.includes(f.unit_id)) ? 'checked' : ''}
                aria-label="${this._esc(t('am.all', 'Tick every free unit'))}"></th>
                <th scope="col">${this._esc(t('bridges.colUnit', 'Unit'))}</th>
                <th scope="col">${this._esc(listen ? t('am.colHeard', 'What was heard') : t('am.colSays', 'What it says'))}</th>
                <th scope="col">${this._esc(t('am.colModel', 'Suggested model'))}</th><th scope="col"></th></tr></thead>
            <tbody>${rows}</tbody></table></div>
            <p class="am-go"><button type="button" class="btn btn-primary btn-sm" data-am="go" id="adManyGo"
                ${n ? '' : `aria-disabled="true" data-am-why="${this._esc(t('am.why.none', 'Tick the units to add.'))}"`}>
                ${this._esc(this.tn('am.addN', n, 'Add {n} selected'))} <i aria-hidden="true" class="bi bi-arrow-right"></i></button></p>`;
    },

    _amRedraw() {
        const box = document.getElementById('adManyRows');
        const had = document.activeElement && box && box.contains(document.activeElement) ? document.activeElement.id : null;
        if (box) box.innerHTML = this._amRowsHtml();
        if (had) document.getElementById(had)?.focus();
        const st = document.getElementById('adManyState');
        if (st) st.textContent = this._amBatch().note || '';
    },

    // ── run a scan / listen ──────────────────────────────────────────────
    async _amStart(mode, confirm = {}) {
        const t = (k, d, p) => this.t(k, d, p);
        const ad = this._ad, b = this._amBatch();
        if (!ad.cs || b.state === 'running') return;
        this._amCollect(ad.s);
        let conn;
        try { conn = Object.assign({}, ad.cs.wire() || {}); } catch (e) { conn = {}; }
        delete conn.unit_id;
        delete conn.timeout;
        const body = { connection: conn, mode };
        if (mode === 'poll') Object.assign(body, { from: b.range.from, to: b.range.to });
        else body.seconds = 15;
        if (confirm.open) body.confirm_open = true;
        if (confirm.slow) body.confirm_slow = true;
        b.note = mode === 'poll' ? t('am.starting', 'Starting the scan…') : t('am.listening', 'Listening…');
        this._amRedraw();
        let r, d;
        try {
            r = await fetch('/api/bus-scan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            d = await r.json().catch(() => ({}));
        } catch (e) { r = { ok: false, status: 0 }; d = { detail: String(e.message || e) }; }
        if (!r.ok) {
            const det = (d && d.detail) || {};
            if (r.status === 409 && !det.config_blocked && det.client === 'unknown' && !confirm.open) {
                const ok = await this.confirmAction({
                    title: t('am.confirmTitle', 'Open a connection to this bus?'),
                    message: (det.errors || [])[0] || '', confirmLabel: t('am.confirmOpen', 'Open it and scan'), danger: true });
                if (ok) return this._amStart(mode, Object.assign({}, confirm, { open: true }));
                b.note = t('am.notSent', 'Nothing was sent.');
                return this._amRedraw();
            }
            // an armed rule reads or drives a device on this bus: the scan
            // would slow its polling — said with the delay, and asked
            if (r.status === 409 && !det.config_blocked && det.slow && (det.rules || []).length && !confirm.slow) {
                const sl = det.slow;
                const ok = await this.confirmAction({
                    title: t('am.slowTitle', 'Slow down a bus that armed rules depend on?'),
                    message: t('am.slowMsg', 'Armed rule(s) {rules} read or drive {devices} on this bus. While the scan runs each of their reads may wait up to {s} s — a poll cycle of {k} read(s) up to {w} s slower.',
                        { rules: det.rules.join(', '), devices: (sl.devices || []).join(', '), s: sl.per_read_s, k: sl.reads_per_cycle, w: sl.worst_per_cycle_s }),
                    confirmLabel: t('am.slowOk', 'Scan anyway'), danger: true });
                if (ok) return this._amStart(mode, Object.assign({}, confirm, { slow: true }));
                b.note = t('am.notSent', 'Nothing was sent.');
                return this._amRedraw();
            }
            b.note = this._respError(r, d);
            ad.shell.say(b.note);
            return this._amRedraw();
        }
        Object.assign(b, { state: 'running', mode, job: d.job, found: [], units: [], key: this._amConnKey(), heard: null,
                           warn: (d.warnings || []).join(' ') });
        this._amRefreshPanel();
        this._amTick(d.job);
    },

    // the panel drawn again from the state (its buttons depend on running)
    _amRefreshPanel() {
        const ad = this._ad;
        const form = ad.cs && ad.cs.form;
        const q = document.getElementById('adManyPanel');
        if (!form || !q) return;
        const tmp = document.createElement('div');
        tmp.innerHTML = this._amPanelHtml(form, ad.cs.stateNow(), { asks: form.cs.role !== 'listen', seplos: form.cs.axes.protocol === 'seplos_v2' });
        const had = document.activeElement && q.contains(document.activeElement) ? document.activeElement.id : null;
        q.replaceWith(tmp.firstElementChild);
        if (had) document.getElementById(had)?.focus();
    },

    async _amTick(job) {
        const t = (k, d, p) => this.t(k, d, p);
        const ad = this._ad, b = this._amBatch();
        if (!ad || !ad.open || b.job !== job) {
            fetch(`/api/bus-scan/${job}`, { method: 'DELETE' }).catch(() => {});
            return;
        }
        let st = null;
        try { const r = await fetch(`/api/bus-scan/${job}`); st = r.ok ? await r.json() : null; } catch (e) { st = null; }
        if (this._ad !== ad || b.job !== job) return;
        if (!st) { b.state = 'failed'; b.note = t('am.lost', 'The scan could not be read.'); this._amRefreshPanel(); return; }
        b.found = st.found || [];
        b.units = b.units.filter(u => b.found.some(f => f.unit_id === u && !f.device));
        // a scan counts the units that answered (an address a device has is listed, never asked)
        const n = b.found.filter(f => b.mode !== 'poll' || f.answer !== 'known').length;
        if (st.state === 'running' || st.state === 'cancelling') {
            b.note = b.mode === 'poll'
                ? t('am.progress', '{done}/{total} asked · {n} answered', { done: st.done, total: st.total, n })
                : t('am.progressListen', 'Listening… {n} address(es) heard', { n });
            this._amRedraw();
            ad.scanTimer = setTimeout(() => this._amTick(job), 600);
            return;
        }
        b.state = st.state;
        b.heard = st.heard || null;
        const warn = (st.warnings || []).join(' ');
        b.note = st.state === 'failed' ? `${t('bridges.scanFailed', 'Scan failed')}: ${st.error || warn}`
            : [b.mode === 'poll' ? this.tn('am.done', n, 'Done — {n} unit(s) answered.') : this.tn('am.doneListen', n, 'Done — {n} address(es) heard.'),
               st.state === 'cancelled' ? t('am.stopped', '(stopped)') : '', warn].filter(Boolean).join(' ');
        this._amRefreshPanel();
        ad.shell.say(b.note);
        this._adFooter();
    },

    _amStop() {
        const b = this._amBatch();
        if (b.job && b.state === 'running') fetch(`/api/bus-scan/${b.job}`, { method: 'DELETE' }).catch(() => {});
    },

    // closing the dialog stops a scan still asking the bus
    _amClose() {
        const ad = this._ad;
        if (!ad) return;
        clearTimeout(ad.scanTimer);
        if (ad.s && ad.s.batch) this._amStop();
    },

    // ── pointer and keyboard (delegated from the dialog) ────────────────
    _amOnClick(ev) {
        const ad = this._ad;
        if (!ad || ad.s.path !== 'many') return false;
        const el = ev.target.closest ? ev.target.closest('[data-am], [data-am-unit]') : null;
        if (!el) return false;
        const b = this._amBatch();
        if (el.getAttribute('aria-disabled') === 'true') {
            ev.preventDefault();
            ad.shell.say(el.dataset.amWhy || '');
            return true;
        }
        const what = el.dataset.am;
        if (what === 'poll' || what === 'listen') { this._amStart(what); return true; }
        if (what === 'stop') { this._amStop(); return true; }
        if (what === 'go') { this._adNext(); return true; }
        if (what === 'all') {
            const free = b.found.filter(window.AddDevModel.scanPickable).map(f => f.unit_id);
            b.units = el.checked ? free : [];
            this._amRedraw();
            document.getElementById('adManyAll')?.focus();
            this._adFooter();
            return true;
        }
        if (el.dataset.amUnit) {
            const u = +el.dataset.amUnit;
            b.units = el.checked ? [...new Set([...b.units, u])].sort((x, y) => x - y) : b.units.filter(x => x !== u);
            el.closest('tr')?.classList.toggle('on', el.checked);
            this._amRedraw();
            this._adFooter();
            return true;
        }
        return false;
    },

    // ── Units: the batch ─────────────────────────────────────────────────
    // the rows, made again when the ticked units changed (what was typed in
    // a row that stays is kept)
    _amRows() {
        const ad = this._ad, b = this._amBatch(), M = window.AddDevModel;
        const want = b.units.slice().sort((x, y) => x - y);
        const have = b.rows.map(r => r.unit);
        if (JSON.stringify(want) === JSON.stringify(have)) return b.rows;
        const taken = new Set((this._devices || []).map(d => d.id));
        const kept = b.rows.filter(r => want.includes(r.unit));
        kept.forEach(r => taken.add(r.id));
        const fresh = M.batchRows(want.filter(u => !have.includes(u)), b.found, {
            template: ad.s.template, templates: ad.templates, taken, unitWord: this.t('am.unitWord', 'unit') });
        b.rows = [...kept, ...fresh].sort((x, y) => x.unit - y.unit);
        return b.rows;
    },

    _amUnitsHtml() {
        const t = (k, d, p) => this.t(k, d, p);
        const ad = this._ad, b = this._amBatch();
        const rows = this._amRows();
        const tpls = (ad.templates || []).filter(x => !x.component_of);
        const opts = sel => `<option value="">${this._esc(t('am.pickModel', '— choose —'))}</option>` + tpls.map(x =>
            `<option value="${this._esc(x.id)}" ${x.id === sel ? 'selected' : ''}>${this._esc(x.name || x.id)}</option>`).join('');
        const word = r => ({ new: '', checked: t('am.st.checked', 'checked'), refused: t('am.st.refused', 'refused'),
                             creating: t('am.st.creating', 'creating…'), created: t('am.st.created', 'created'),
                             failed: t('am.st.failed', 'failed') })[r.status] || '';
        const conn = this._amWhere();
        const body = rows.map((r, i) => {
            const done = r.status === 'created';
            const ro = done ? 'readonly aria-readonly="true"' : '';
            return `<tr data-am-status="${r.status}">
                <td><code>${r.unit}</code></td>
                <td><label class="sr-only" for="adUName-${i}">${this._esc(t('am.nameOf', 'Name of unit {u}', { u: r.unit }))}</label>
                    <input type="text" class="input" id="adUName-${i}" data-am-row="${i}" data-am-field="name" value="${this._esc(r.name)}" ${ro}></td>
                <td><label class="sr-only" for="adUId-${i}">${this._esc(t('am.idOf', 'Id of unit {u}', { u: r.unit }))}</label>
                    <input type="text" class="input" id="adUId-${i}" data-am-row="${i}" data-am-field="id" value="${this._esc(r.id)}" spellcheck="false" ${ro}
                        pattern="[a-z0-9][a-z0-9_-]{1,63}"></td>
                <td><label class="sr-only" for="adUTpl-${i}">${this._esc(t('am.modelOf', 'Model of unit {u}', { u: r.unit }))}</label>
                    <select class="input" id="adUTpl-${i}" data-am-row="${i}" data-am-field="template" ${done ? 'disabled' : ''}>${opts(r.template)}</select></td>
                <td class="am-status" id="adUSt-${i}">${r.status !== 'new' ? `<span class="sink-pill ${done ? 'ok' : (r.status === 'failed' || r.status === 'refused') ? 'warn' : 'info'}">${this._esc(word(r))}</span>` : ''}
                    ${r.why ? `<span class="field-error" id="adUWhy-${i}">${this._esc(r.why)}</span>` : ''}</td></tr>`;
        }).join('');
        const sh = b.shared;
        return `<p class="field-hint">${this._esc(t('am.unitsLead', '{n} unit(s) on {where}. Each becomes a device of its own; the id names its topics and history and is fixed after creation.', { n: rows.length, where: conn }))}</p>
            <div class="am-table-wrap"><table class="tplu-table am-units" id="adUnitsTable">
              <caption class="sr-only">${this._esc(t('am.unitsCaption', 'The units to add'))}</caption>
              <thead><tr><th scope="col">${this._esc(t('bridges.colUnit', 'Unit'))}</th><th scope="col">${this._esc(t('devices.wizard.name', 'Device name'))}</th>
                <th scope="col">${this._esc(t('devices.wizard.id', 'Device id'))}</th><th scope="col">${this._esc(t('am.colModelShort', 'Model'))}</th>
                <th scope="col">${this._esc(t('am.colStatus', 'Status'))}</th></tr></thead>
              <tbody>${body}</tbody></table></div>
            <fieldset class="cf-fieldset am-shared"><legend class="wiz-eyebrow">${this._esc(t('am.shared', 'For every unit'))}</legend>
              <label class="checkbox-label"><input type="checkbox" id="adManyEnabled" ${sh.enabled ? 'checked' : ''}> ${this._esc(t('am.startNow', 'Start reading now'))}</label>
              <label class="checkbox-label"><input type="checkbox" id="adManyHa" ${sh.ha_discovery_enabled ? 'checked' : ''}> ${this._esc(t('am.ha', 'Home Assistant discovery'))}</label>
              <div class="form-group"><label class="form-label" for="adManyBucket">${this._esc(t('am.bucket', 'InfluxDB bucket'))}</label>
                <input type="text" id="adManyBucket" class="input" value="${this._esc(sh.bucket)}" placeholder="${this._esc(t('am.bucketPh', 'the gateway\'s'))}" spellcheck="false"></div>
              <p class="field-hint"><i aria-hidden="true" class="bi bi-lock"></i> ${this._esc(t('am.locked', 'Each is created write-locked; an admin unlocks it from its ⋯ → Unlock writes…. Topics follow the gateway\'s pattern.'))}</p>
            </fieldset>
            <p class="am-state" id="adUnitsState" role="status" aria-live="polite">${this._esc(this._amTallyText())}</p>`;
    },

    _amWhere() {
        const b = this._amBatch();
        const c = b.conn || {};
        if (c.bridge) return `${c.bridge} :${c.bridge_port}`;
        return c.serial_port || (c.host ? `${c.host}:${c.port}` : '—');
    },

    _amTallyText() {
        const b = this._amBatch();
        const tl = window.AddDevModel.batchTally(b.rows);
        if (!tl.created && !tl.failed) return '';
        return this.t('am.tally', '{created} of {total} created · {failed} failed · {left} left', tl);
    },

    _amSetRow(i) {
        const b = this._amBatch();
        const r = b.rows[i];
        const tr = document.querySelector(`#adUnitsTable tbody tr:nth-child(${i + 1})`);
        if (!r || !tr) return;
        const tmp = document.createElement('tbody');
        tmp.innerHTML = this._amUnitsHtml().match(/<tbody>([\s\S]*)<\/tbody>/)[1];
        const fresh = tmp.children[i];
        if (fresh) tr.replaceWith(fresh);
        const st = document.getElementById('adUnitsState');
        if (st) st.textContent = this._amTallyText();
    },

    // leaving the connection: what the rows will be created on
    _amLeaveConn() {
        const ad = this._ad, b = this._amBatch();
        try { b.conn = ad.cs.wire(); } catch (e) { b.conn = null; }
        if (b.state === 'running') this._amStop();
    },

    // Create: every row checked here, then a dry run for each, then the
    // creates one by one; a row that fails stays with its reason
    async _amCreate() {
        const t = (k, d, p) => this.t(k, d, p);
        const ad = this._ad, b = this._amBatch(), M = window.AddDevModel;
        if (ad.busy) return;
        this._amCollect(ad.s);
        if (M.batchTally(b.rows).left === 0) {
            ad.done = true;
            this.closeModal('addDeviceModal');
            return;
        }
        const taken = new Set((this._devices || []).map(d => d.id));
        b.rows.filter(r => r.status === 'created').forEach(r => taken.delete(r.id));
        const errs = M.batchErrors(b.rows, taken, t);
        const keys = Object.keys(errs);
        if (keys.length) {
            const items = [];
            for (const i of keys) {
                for (const [f, why] of Object.entries(errs[i])) {
                    const id = { name: `adUName-${i}`, id: `adUId-${i}`, template: `adUTpl-${i}` }[f];
                    items.push({ text: `${t('bridges.colUnit', 'Unit')} ${b.rows[i].unit}: ${why}`, fieldId: id });
                }
            }
            ad.shell.showErrors(items, t('am.fix', 'Fix these rows first — nothing was sent:'));
            document.getElementById(items[0].fieldId)?.focus();
            return;
        }
        ad.shell.showErrors([]);
        ad.busy = true;
        ad.shell.el.next.setAttribute('aria-busy', 'true');
        // a create names no device yet: the old wizard's own POST, dry or not
        const post = async (dry, payload) => {
            try {
                const r = await fetch(dry ? '/api/devices?dry_run=true' : '/api/devices', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
                return { r, d: await r.json().catch(() => ({})) };
            } catch (e) { return { r: { ok: false, status: 0 }, d: { detail: String(e.message || e) } }; }
        };
        try {
            // one dry run per row before anything is created
            let refused = 0;
            for (let i = 0; i < b.rows.length; i++) {
                const row = b.rows[i];
                if (row.status === 'created') continue;
                const { r, d } = await post(true, M.batchPayload(row, b.shared, b.conn));
                Object.assign(row, r.ok ? { status: 'checked', why: '' } : { status: 'refused', why: this._respError(r, d) });
                if (!r.ok) refused++;
                this._amSetRow(i);
            }
            if (refused) {
                ad.shell.say(this.tn('am.refusedN', refused, '{n} row(s) refused by the dry run — nothing was created. Fix them and Create again.'));
                return;
            }
            for (let i = 0; i < b.rows.length; i++) {
                const row = b.rows[i];
                if (row.status === 'created') continue;
                row.status = 'creating';
                this._amSetRow(i);
                const { r, d } = await post(false, M.batchPayload(row, b.shared, b.conn));
                Object.assign(row, r.ok ? { status: 'created', why: '' } : { status: 'failed', why: this._respError(r, d) });
                this._amSetRow(i);
            }
        } finally {
            ad.busy = false;
            ad.shell.el.next.removeAttribute('aria-busy');
        }
        const tl = M.batchTally(b.rows);
        ad.shell.say(this._amTallyText());
        this._fetchDevices?.(true)?.catch?.(() => {});
        if (tl.left === 0) {
            this.showToast('success', t('devices.created', 'Device created'), this.tn('am.createdN', tl.created, '{n} device(s) created, write-locked.'));
        }
        this._adFooter();
    },
});
