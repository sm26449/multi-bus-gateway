/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// The read preview, in the page (ADR-0031 §2 Check, §3/§4 Preview; the server
// side is multibus/preview.py, docs/API.md "POST /api/previews") — augments
// JanitzaMonitor.prototype. One runner and one drawing serve Add's Check step
// (app-add-device.js) and the device page's Preview (its header, and the Edit
// modal's Connection section with the edited connection).
//
//   _pvStart(body, deviceId)         POST it: {job, head} | {refused: {status,
//                                    errors, detail, retryAfter, kind}}
//                                    kind: confirm (nobody can say who holds
//                                    the port: confirm_open opens it anyway),
//                                    busy (409 busy / another preview, 429:
//                                    try again), listened (a bus another master
//                                    polls — never transmitted on), refused
//   _pvFollow(run, onView)           GET /api/previews/{job} until it ends
//   _pvCancel(job)                   DELETE it (a step left, a dialog closed)
//   _pvHtml(view, o)                 the answer: state and budget, "heard n of
//                                    m groups", the identity, the values with
//                                    their quality pills, the load, warnings
//   _pvRefusalHtml(ref, px)          a refusal in words, with what can be done
//   ddvPreview(id, {connection, template})
//                                    the device page's Preview (operator)
Object.assign(JanitzaMonitor.prototype, {

    async _pvStart(body, deviceId) {
        let r, d;
        try {
            r = deviceId
                ? await window.DeviceStore.devWrite(deviceId, 'POST', `/api/devices/${encodeURIComponent(deviceId)}/preview`, body)
                : await fetch('/api/previews', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            d = await r.json().catch(() => ({}));
        } catch (e) {
            return { refused: { status: 0, errors: [String(e.message || e)], detail: {}, kind: 'refused' } };
        }
        if (r.status === 202 || r.ok) return { job: d.job, head: d };
        const det = d && typeof d.detail === 'object' && d.detail ? d.detail : { errors: [typeof d.detail === 'string' ? d.detail : this._respError(r, d)] };
        const errors = (det.errors || []).map(String);
        let kind = 'refused';
        if (r.status === 429) kind = 'busy';
        else if (r.status === 409 && !det.config_blocked && det.client === 'unknown') kind = 'confirm';
        else if (r.status === 409 && !det.config_blocked && (det.job || /busy starting|already running/.test(errors.join(' ')))) kind = 'busy';
        else if (r.status === 409 && !det.config_blocked && (det.listeners || []).length) kind = 'listened';
        const ra = Number(r.headers && r.headers.get ? r.headers.get('Retry-After') : 0) || 0;
        return { refused: { status: r.status, errors, detail: det, retryAfter: ra, kind } };
    },

    // follows a job until it ends; run.stop ends the following (the job is
    // the caller's to cancel)
    async _pvFollow(run, onView) {
        let wait = 400;
        while (!run.stop) {
            let view = null;
            try {
                const r = await fetch(`/api/previews/${encodeURIComponent(run.job)}`);
                if (r.status === 404) { view = { state: 'failed', error: this.t('pv.gone', 'The answer is no longer kept (10 minutes): run it again.') }; }
                else if (r.ok) view = await r.json();
            } catch (e) { view = null; }
            if (run.stop) return null;
            if (view) {
                onView(view);
                if (view.state !== 'running') return view;
            }
            await new Promise(res => setTimeout(res, wait));
            wait = Math.min(1200, wait + 200);
        }
        return null;
    },

    // a promise: Run again waits for it (one preview per bus)
    _pvCancel(job) {
        if (!job) return Promise.resolve();
        return fetch(`/api/previews/${encodeURIComponent(job)}`, { method: 'DELETE' }).catch(() => {});
    },

    _pvQualityWord(q) {
        const t = (k, d) => this.t(k, d);
        return ({ ok: t('pv.q.ok', 'ok'), no_answer: t('pv.q.no_answer', 'no answer'), illegal_address: t('pv.q.illegal_address', 'illegal address'),
                  out_of_range: t('pv.q.out_of_range', 'out of range'), byte_swap_suspect: t('pv.q.byte_swap_suspect', 'looks byte-swapped'),
                  sentinel: t('pv.q.sentinel', 'no value (sentinel)') })[q] || String(q || '');
    },

    _pvValue(v) {
        if (v === null || v === undefined || v === '') return '—';
        if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toLocaleString(this._loc ? this._loc() : undefined, { maximumFractionDigits: 3 });
        return String(v);
    },

    _pvWhere(v) {
        if (v.address !== undefined && v.address !== null) return `${v.register_type || ''} ${v.address}`.trim();
        return v.path || v.can_id || v.command || '';
    },

    _pvValuesHtml(view, px) {
        const t = (k, d, p) => this.t(k, d, p);
        const rows = window.AddDevModel.checkRows(view);
        if (!rows.length) return '';
        return `<div class="pv-values-wrap"><table class="pv-values" id="${px}Values">
            <caption class="sr-only">${this._esc(t('pv.valuesCap', 'Values read'))}</caption>
            <thead><tr><th scope="col">${this._esc(t('pv.col.name', 'Name'))}</th><th scope="col">${this._esc(t('pv.col.value', 'Value'))}</th>
                <th scope="col">${this._esc(t('pv.col.unit', 'Unit'))}</th><th scope="col">${this._esc(t('pv.col.quality', 'Quality'))}</th>
                <th scope="col">${this._esc(t('pv.col.group', 'Group'))}</th></tr></thead>
            <tbody>${rows.map(v => `<tr data-pv-row="${this._esc(v.name)}" data-q="${this._esc(v.quality)}">
                <td><span class="pv-name">${this._esc(v.label || v.name)}</span><span class="field-hint pv-where">${this._esc(this._pvWhere(v))}</span></td>
                <td class="pv-val">${this._esc(this._pvValue(v.value))}</td>
                <td>${this._esc(v.unit || '')}</td>
                <td><span class="pv-q is-${this._esc(v.quality)}">${this._esc(this._pvQualityWord(v.quality))}</span>${v.note ? `<span class="field-hint"> ${this._esc(v.note)}</span>` : ''}</td>
                <td>${this._esc(v.group || '')}</td></tr>`).join('')}</tbody></table></div>`;
    },

    // what the device says it is, against the model chosen
    _pvIdentityHtml(view, o = {}) {
        const t = (k, d, p) => this.t(k, d, p);
        const M = window.AddDevModel;
        const iv = M.identityVerdict(view);
        const id = (view || {}).identity || {};
        const said = [];
        const ss = id.sunspec || {}, f = id.fc43 || {};
        if (ss.manufacturer || ss.model) said.push(`SunSpec: ${[ss.manufacturer, ss.model, ss.version, ss.serial].filter(Boolean).join(' · ')}`);
        if (f.vendor || f.product) said.push(`FC43: ${[f.vendor, f.product, f.version].filter(Boolean).join(' · ')}`);
        if (iv.state === 'none' && !said.length) return '';
        let line = '';
        if (iv.state === 'match') line = `<i aria-hidden="true" class="bi bi-check-circle"></i> ${this._esc(t('pv.id.match', 'It answers as the model chosen.'))}`;
        else if (iv.state === 'mismatch') {
            line = `<i aria-hidden="true" class="bi bi-exclamation-triangle"></i> ${this._esc(iv.suggested
                ? t('pv.id.mismatchNamed', 'It does not answer as the model chosen; it answers as {name}.', { name: iv.name })
                : t('pv.id.mismatch', 'It does not answer as the model chosen.'))}`
                + (iv.suggested && o.onUse ? ` <button type="button" class="btn btn-secondary btn-sm" id="${o.px}UseSuggested" data-pv-use="${this._esc(iv.suggested)}">${this._esc(t('pv.id.use', 'Use {name}', { name: iv.name }))}</button>` : '');
        } else if (iv.state === 'unknown') line = this._esc(t('pv.id.unknown', 'Its identity could not be told from what it answered.'));
        return `<div class="pv-identity is-${this._esc(iv.state)}" id="${o.px}Identity"><p>${line}</p>${said.length
            ? `<p class="field-hint">${this._esc(t('pv.id.said', 'The device says'))}: ${said.map(x => this._esc(x)).join(' — ')}</p>` : ''}</div>`;
    },

    _pvPct(x) { return `${Math.round(Number(x) * 100)} %`; },

    _pvLoadHtml(view) {
        const t = (k, d, p) => this.t(k, d, p);
        const l = (view || {}).load || {};
        if (l.share_added === null || l.share_added === undefined) {
            return l.share_now !== null && l.share_now !== undefined
                ? `<p class="field-hint pv-load">${this._esc(t('pv.load.now', 'The bus is about {now} busy now.', { now: this._pvPct(l.share_now) }))}</p>` : '';
        }
        const total = (Number(l.share_now) || 0) + Number(l.share_added);
        const high = total >= 0.8;
        const words = l.share_now !== null && l.share_now !== undefined
            ? t('pv.load.with', 'This bus would be about {total} busy at this model\'s poll rate ({now} now).', { total: this._pvPct(total), now: this._pvPct(l.share_now) })
            : t('pv.load.added', 'This model\'s poll rate takes about {added} of the bus.', { added: this._pvPct(l.share_added) });
        return `<p class="pv-load ${high ? 'is-warn' : 'field-hint'}">${high ? '<i aria-hidden="true" class="bi bi-exclamation-triangle"></i> ' : ''}${this._esc(words)}${high
            ? ' ' + this._esc(this._roleHint('pv.load.lower', 'Lower its poll rate later, from its ⋯ → Polling….', 'pv.load.lowerOther', 'An admin can lower its poll rate later.')) : ''}${l.basis ? `<span class="field-hint"> (${this._esc(l.basis)})</span>` : ''}</p>`;
    },

    _pvStatusHtml(view) {
        const t = (k, d, p) => this.t(k, d, p);
        const M = window.AddDevModel;
        const b = (view || {}).budget || {};
        const v = M.checkVerdict(view);
        const where = [view.where, view.transport === 'shared' && view.shared_with ? t('pv.sharedWith', 'through {id}\'s connection', { id: view.shared_with }) : '',
                       view.mode === 'listen' ? t('pv.listening', 'listening, transmits nothing') : ''].filter(Boolean).join(' · ');
        let line;
        if (v === 'running') line = `<span class="btn-spinner" aria-hidden="true"></span> ${this._esc(view.mode === 'listen'
            ? t('pv.runListen', 'Listening for {s} s…', { s: view.seconds || '' }) : t('pv.run', 'Reading…'))}`;
        else if (v === 'ok') line = `<i aria-hidden="true" class="bi bi-check-circle"></i> ${this._esc(t('pv.ok', 'Read.'))}`;
        else if (v === 'none') line = `<i aria-hidden="true" class="bi bi-exclamation-triangle"></i> ${this._esc(view.mode === 'listen'
            ? t('pv.noneListen', 'Nothing heard — is the master polling? Is the address right?') : t('pv.none', 'Nothing answered.'))}`;
        else line = `<i aria-hidden="true" class="bi bi-x-circle"></i> ${this._esc(view.error || t('pv.failed', 'The check failed.'))}`;
        const h = M.heard(view);
        const budget = b.max_requests ? t('pv.budget', '{n} of {max} requests, {ms} ms', { n: b.requests || 0, max: b.max_requests, ms: b.ms ?? '—' }) : '';
        return `<p class="pv-status is-${v}">${line}${h ? ` <b class="pv-heard">${this._esc(t('pv.heard', 'heard {n} of {m} groups', { n: h.n, m: h.of }))}</b>` : ''}</p>
            ${where || budget ? `<p class="field-hint pv-where-line">${this._esc([where, budget].filter(Boolean).join(' · '))}</p>` : ''}`;
    },

    _pvWarningsHtml(view) {
        const w = (view || {}).warnings || [];
        if (!w.length) return '';
        return `<ul class="pv-warnings">${w.map(x => `<li class="field-hint"><i aria-hidden="true" class="bi bi-info-circle"></i> ${this._esc(x)}</li>`).join('')}</ul>`;
    },

    // the whole answer; o.px names its ids, o.extra(view) adds the caller's
    // parts (Add: the components found, the setup writes)
    _pvHtml(view, o = {}) {
        if (!view) return '';
        return `${this._pvStatusHtml(view)}
            ${this._pvIdentityHtml(view, o)}
            ${o.extra ? o.extra(view) : ''}
            ${this._pvValuesHtml(view, o.px || 'pv')}
            ${this._pvLoadHtml(view)}
            ${this._pvWarningsHtml(view)}`;
    },

    // a refusal: its reason, and what can be done about it
    _pvRefusalHtml(ref, px, o = {}) {
        const t = (k, d, p) => this.t(k, d, p);
        const det = ref.detail || {};
        const head = ({
            confirm: t('pv.ref.confirm', 'Nobody can say whether another program holds this connection.'),
            busy: t('pv.ref.busy', 'The bus is busy: try again in a moment.'),
            listened: t('pv.ref.listened', 'Another master polls this bus: the gateway never transmits on it. Choose Listen-only, or another bus.'),
        })[ref.kind] || t('pv.ref.refused', 'The check could not run.');
        // the gateway's words; the line refusal (detail.line: a holder's line
        // or timeout) without its field path — its words say the field
        // (review m5: any other refusal keeps its subject)
        const strip = e => (det.line ? String(e).replace(/^connection\.(timeout|baudrate|parity|bytesize|stopbits):\s*/, '') : String(e));
        const why = (ref.errors || []).map(e => `<li>${this._esc(strip(e))}</li>`).join('');
        const confirm = ref.kind === 'confirm' ? `<label class="checkbox-label pv-confirm"><input type="checkbox" id="${px}ConfirmOpen" ${o.confirmed ? 'checked' : ''}>
            <span>${this._esc(t('pv.ref.openAnyway', 'Open the connection anyway (audited): {why}', { why: det.unknown || '' }))}</span></label>` : '';
        const retry = ref.kind === 'busy' && ref.retryAfter ? `<p class="field-hint">${this._esc(t('pv.ref.retryIn', 'Try again in {s} s.', { s: ref.retryAfter }))}</p>` : '';
        return `<div class="pv-refused is-${this._esc(ref.kind)}" role="status"><p><i aria-hidden="true" class="bi bi-slash-circle"></i> <b>${this._esc(head)}</b></p>
            ${why ? `<ul class="pv-why">${why}</ul>` : ''}${confirm}${retry}</div>`;
    },

    // ── the device page's Preview (operator): the stored connection, or
    // the Edit modal's edited one, read before Save ───────────────────────
    async ddvPreview(id, opts = {}) {
        const t = (k, d, p) => this.t(k, d, p);
        if (!id) return;
        const body = document.getElementById('previewModalBody');
        const again = document.getElementById('previewModalAgain');
        const modal = document.getElementById('previewModal');
        if (!body || !modal) return;
        // the run before this one stopped and its job cancelled first (review M2)
        if (this._pvRun) { const old = this._pvRun; this._pvRun = null; old.stop = true; await this._pvCancel(old.job); }
        const run = this._pvRun = { stop: false, job: '', id, opts, confirmed: !!opts.confirmed };
        const e = (this._devices || []).find(x => x.id === id) || {};
        document.querySelector('#previewModalTitle span').textContent = t('pv.titleOf', 'Preview — {name}', { name: e.name || id });
        body.innerHTML = `<p class="field-hint" role="status"><span class="btn-spinner" aria-hidden="true"></span> ${this._esc(t('pv.starting', 'Asking the gateway…'))}</p>`;
        if (!modal.classList.contains('active')) {
            modal._onClose = () => { const r = this._pvRun; this._pvRun = null; if (r) { r.stop = true; this._pvCancel(r.job); } };
            again.onclick = () => this.ddvPreview(id, Object.assign({}, opts, {
                confirmed: !!document.getElementById('pvDevConfirmOpen')?.checked }));
            this.openModal('previewModal', { focus: document.getElementById('previewModalTitle') });
        }
        const req = {};
        if (opts.connection && Object.keys(opts.connection).length) req.connection = opts.connection;
        if (opts.template) req.template = opts.template;
        if (run.confirmed) req.confirm_open = true;
        let got = await this._pvStart(req, id);
        if (got.refused && got.refused.status === 409 && !(got.refused.detail || {}).config_blocked && (got.refused.detail || {}).job && this._pvRun === run) {
            await this._pvCancel(got.refused.detail.job);
            got = await this._pvStart(req, id);
        }
        // closed or replaced while it started: its job cancelled at once
        if (this._pvRun !== run || run.stop || !modal.classList.contains('active')) {
            if (got.job) this._pvCancel(got.job);
            return;
        }
        if (got.refused) {
            body.innerHTML = this._pvRefusalHtml(got.refused, 'pvDev', { confirmed: run.confirmed });
            return;
        }
        run.job = got.job;
        const draw = v => { if (this._pvRun === run) body.innerHTML = this._pvHtml(v, { px: 'pvDev' }); };
        draw(Object.assign({ state: 'running' }, got.head));
        await this._pvFollow(run, draw);
    },
});
