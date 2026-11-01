/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// Add Device (ADR-0031 §2) — augments
// JanitzaMonitor.prototype. The only Add: every way in calls
// openDeviceWizard(null, prefill) or openAddDevice(o) — the Add button, a
// fresh install's first device, a bridge's + Device and a scan row (the
// connection step, the bus filled), discovery and Found (Check, pre-filled),
// a meter's "Add equipment it measures" (the measured path), ⋯ Duplicate
// (the connection step with the copy list).
//
// One frame (device-shell.js, steps mode), one state (adddev-model.js):
//   What is it?   search, type chips, model cards, three other ways, Found (n)
//   Connection    the connection step (conn-step.js, mountConnStep): port →
//                 reach → Master / Listen-only → protocol → line → address;
//                 Next asks the gateway (POST /api/devices?dry_run=true&
//                 only=connection) and places its refusals at their fields
//   Check         a read preview (app-preview.js, POST /api/previews) started
//                 on arrival: values with quality pills, "heard n of m
//                 groups", the identity (a mismatch names the model to use),
//                 the components found (ticked), the load, the setup-writes
//                 truth; a refusal said with what can be done (open anyway,
//                 try again). It never blocks: "Next — without a check".
//   Name & save   name (id derived), type (+ Add type), location, description,
//                 labels, nameplate (pre-filled from the check), recommended
//                 attributes, outputs as chips, the Writes card, Start reading
//                 now, the review strip from the full dry run's would-be entry
//                 (each layer with Change); Create is aria-disabled while the
//                 dry run refuses, its reason beside it. Then a result panel:
//                 Open it · Add another like this (unit N) · Done.
//   Measured      What is it (its type) · Meters · Name & save (byte-identical
//                 to the meter-only create of old).
//   Many          the connection with its bus scan panel, then Units — the
//                 batch (app-add-many.js, L3.7), each row built by
//                 AddDevModel.batchPayload over the one createPayload.
// Every answer is kept on Back and when another way is taken; Esc, ✕ and
// Cancel ask before dropping them. "Many of the same" (scan and batch) is
// L3.7's: it builds its creates with AddDevModel.draftOf/createPayload.
Object.assign(JanitzaMonitor.prototype, {

    // ── every way in ─────────────────────────────────────────────────────
    // the one entry callers have always used (editId: a saved device is
    // edited in its Edit modal); a prefill says how the device is reached
    // ({protocol, bridge, bridge_port, unit_id, host, port, url, …}) and may
    // name its model, id and name. `how`: 'bus' | 'found' (else told from it)
    openDeviceWizard(editId = null, prefill = null, how = '') {
        if (editId) return this.openDeviceEdit(editId);
        if (!prefill) return this.openAddDevice({});
        const source = how || (prefill.bridge ? 'bus' : 'found');
        return this.openAddDevice({ prefill, source });
    },

    // a meter's page: equipment that does not communicate, measured by it
    addMeasuredBy(meterId) {
        return this.openAddDevice({ source: 'measured', meter: meterId });
    },

    // o: {source, prefill, state, found, meter}
    async openAddDevice(o = {}) {
        if (this._ad?.open || this._adOpening) return false;
        this._adOpening = true;
        try {
            return await this._adOpenNow(o || {});
        } finally {
            this._adOpening = false;
        }
    },

    _adStepLabel(id) {
        const t = (k, d) => this.t(k, d);
        return ({ what: t('adddev.step.what', 'What is it?'), connection: t('adddev.step.connection', 'Connection'),
                  check: t('adddev.step.check', 'Check'), save: t('adddev.step.save', 'Name & save'),
                  meters: t('adddev.step.meters', 'Meters'), many: t('adddev.step.many', 'Many of the same'),
                  units: t('adddev.step.units', 'Units') })[id] || id;
    },

    _adSteps() {
        return window.AddDevModel.stepsOf(this._ad.s.path).map(id => ({ id, label: this._adStepLabel(id) }));
    },

    _adTpl() {
        const ad = this._ad;
        return ad ? (ad.templates || []).find(x => x.id === ad.s.template) || null : null;
    },

    async _adOpenNow(o) {
        const M = window.AddDevModel, SH = window.DeviceShell;
        const t = (k, d, p) => this.t(k, d, p);
        // the dialog opens at once, busy while what it lists is read
        const ad = this._ad = { open: true, done: false, busy: false, loading: true, s: M.blank(), templates: [], found: [],
                                gen: 0, result: null, dry: null };
        const root = document.getElementById('addDeviceContent');
        const modal = document.getElementById('addDeviceModal');
        ad.shell = SH.mount(root, {
            mode: 'steps', prefix: 'addDev', t, title: t('devices.wizard.titleAdd', 'Add Device'),
            steps: this._adSteps(),
            onRail: id => this._adGoto(id),
        });
        const sh = ad.shell;
        sh.el.next.addEventListener('click', () => this._adNext());
        sh.el.back.addEventListener('click', () => this._adMove(-1));
        sh.el.cancel.addEventListener('click', () => { if (!this._ad?.busy) this._dismissModal('addDeviceModal'); });
        sh.el.close.addEventListener('click', () => this.closeModal('addDeviceModal'));
        if (!modal._adWired) {
            modal._adWired = true;
            modal.addEventListener('click', ev => this._adOnClick(ev));
            modal.addEventListener('keydown', ev => this._adOnKey(ev));
            modal.addEventListener('input', ev => this._adOnInput(ev));
            modal.addEventListener('change', ev => this._adOnChange(ev));
        }
        modal._dirty = () => !!this._ad && !this._ad.done && !this._ad.result && !this._ad.loading && M.isDirty(this._adCollect());
        modal._keepOpen = () => !!this._ad?.busy;
        modal._onClose = () => {
            this._amClose?.();
            // the Connection step's marks observer goes with the dialog (R5)
            const h = document.getElementById('adConnHost');
            if (h && h._adMarkObs) { h._adMarkObs.disconnect(); h._adMarkObs = null; }
            this._adClosed(ad);
        };
        sh.el.main.setAttribute('aria-busy', 'true');
        sh.go('what', false);
        sh.panel('what').innerHTML = `<p class="field-hint"><span class="btn-spinner" aria-hidden="true"></span> ${this._esc(t('adddev.loading', 'Reading the models…'))}</p>`;
        sh.setNext(t('common.next', 'Next'), { disabled: true, why: t('adddev.loading', 'Reading the models…') });
        this.openModal('addDeviceModal', { focus: sh.heading('what') });
        // the reads at once; the models and the types are the ones that
        // matter (the rest have their defaults)
        const tplRead = fetch('/api/device-templates').then(async r => {
            if (!r.ok) throw new Error(this._respError(r, await r.json().catch(() => ({}))));
            return (await r.json()).templates || [];
        });
        const [tpls, types] = await Promise.allSettled([tplRead, this._loadDeviceTypes(), this._meterRoles(),
                                                         this._fetchDevices(true).catch(() => null), this._adLoadSettings(ad),
                                                         this._attrCatalogGet ? this._attrCatalogGet() : null]);
        if (this._ad !== ad || !ad.open) return true;
        ad.loading = false;
        sh.el.main.removeAttribute('aria-busy');
        ad.templates = tpls.status === 'fulfilled' ? tpls.value : [];
        this._adBegin(o);
        const failed = [];
        if (tpls.status !== 'fulfilled') failed.push(t('adddev.loadFailedModels', 'The models could not be read: {why}', { why: String(tpls.reason?.message || tpls.reason || '') }));
        if (types.status !== 'fulfilled' || !this._deviceTypes) failed.push(t('adddev.loadFailedTypes', 'The device types could not be read.'));
        if (failed.length) {
            ad.shell.showErrors(failed.map(text => ({ text })), t('adddev.loadFailed', 'Add Device could not read everything it lists — close it and try again, or use a way below:'));
        }
        this._adLoadFound();
        return true;
    },

    // the gateway's own words for a new device: its topic and bucket
    // patterns, its id, the InfluxDB heartbeat (defaults said, never hidden)
    async _adLoadSettings(ad) {
        const get = async url => { try { const r = await fetch(url); return r.ok ? await r.json() : null; } catch (e) { return null; } };
        const [idn, ix] = await Promise.all([get('/api/config/identity'), get('/api/config/influxdb')]);
        ad.cfg = {
            topicPattern: (idn || {}).default_topic_pattern || this.config?.mqtt?.default_topic_pattern || 'mbg/devices/{device}',
            bucketPattern: (idn || {}).default_bucket_pattern || '{device}',
            gatewayId: (idn || {}).gateway_id || '',
            heartbeat: (ix || {}).heartbeat_interval ?? null,
        };
    },

    _adPattern(pat, did) {
        return String(pat || '').replaceAll('{device_id}', did).replaceAll('{device}', did).replaceAll('{gateway}', this._ad?.cfg?.gatewayId || '');
    },

    // where the dialog starts: the way in decides the way and the step
    _adBegin(o) {
        const ad = this._ad, M = window.AddDevModel, CM = window.ConnformModel;
        const t = (k, d, p) => this.t(k, d, p);
        let s = M.blank();
        let note = '';
        if (o.state) {
            s = o.state;
        } else if (o.found) {
            s = this._adFromFound(o.found) || s;
        } else if (o.prefill) {
            const CONN = ['protocol', 'host', 'port', 'unit_id', 'timeout', 'url', 'bridge', 'bridge_port', 'serial_port',
                          'baudrate', 'parity', 'stopbits', 'bytesize', 'broker', 'mqtt_port', 'topic', 'mqtt_username',
                          'mqtt_password', 'mqtt_tls', 'udp', 'framing'];
            const conn = {};
            for (const [k, v] of Object.entries(o.prefill)) if (CONN.includes(k)) conn[k] = v;
            let tpl = o.prefill.template || '';
            if (tpl && !ad.templates.some(x => x.id === tpl)) {
                note = t('adddev.tplGone', 'The model {id} is not on this gateway: pick one.', { id: tpl });
                tpl = '';
            }
            const L = M.landing({ source: o.source, template: tpl });
            s.path = L.path; s.step = L.step; s.template = tpl;
            s.prefill = conn;
            // a bus of a Modbus TCP gateway is a Modbus TCP device
            if (conn.bridge && this._bridgeType && this._bridgeType(((this._bridges || []).find(b => b.id === conn.bridge) || {}).type).framing === 'modbus_tcp') conn.protocol = 'tcp';
            s.conn = { M: CM.blank(conn), cs: { axes: null, role: null, touched: { unit: conn.unit_id !== undefined, line: false }, busId: null, announce: '' }, prefilled: true };
            if (o.prefill.name) { s.name = String(o.prefill.name); s.nameTouched = true; }
            if (o.prefill.id) { s.id = String(o.prefill.id); s.idTouched = true; }
        } else if (o.source === 'measured') {
            s = M.setPath(s, 'measured');
            if (o.meter) s.meters = [{ role: '', device: o.meter, power: '', energy: '', sign: 'auto' }];
        }
        ad.s = s;
        ad.shell.setSteps(this._adSteps());
        this._adShow(s.step);
        if (note) ad.shell.say(note);
        if (note) ad.shell.showErrors([{ text: note }], t('adddev.noteTitle', 'Note'));
    },

    // a device announcing itself on MQTT: its model, its connection (the keys
    // the form does not show kept beside it), its name — then Check
    _adFromFound(f) {
        const M = window.AddDevModel, CM = window.ConnformModel;
        if (!f || !f.connection) return null;
        const s = M.blank();
        const L = M.landing({ source: 'found', template: this._ad.templates.some(x => x.id === f.template) ? f.template : '' });
        s.path = L.path; s.step = L.step;
        s.template = L.path === 'model' ? f.template : '';
        const Mx = CM.model({ protocol: f.connection.protocol || 'mqtt', connection: f.connection });
        s.conn = { M: Mx, cs: { axes: null, role: null, touched: { unit: true, line: false }, busId: null, announce: '' }, prefilled: true };
        const wire = CM.wire(Mx);
        const extra = Object.fromEntries(Object.entries(f.connection).filter(([k]) => !(k in wire)));
        s.connExtra = Object.keys(extra).length ? extra : null;
        if (f.name) { s.name = String(f.name).slice(0, 80); s.nameTouched = true; }
        if (f.device_id) { s.id = M.uniqueId(f.device_id, (this._devices || []).map(d => d.id)); s.idTouched = true; }
        return s;
    },

    _adClosed(ad) {
        if (ad) {
            ad.open = false;
            if (ad.pvRun) { ad.pvRun.stop = true; this._pvCancel(ad.pvRun.job); }
            clearTimeout(ad.dryTimer);
        }
    },

    // ── the steps ────────────────────────────────────────────────────────
    // a step drawn (again when what it shows changed), shown, and the footer
    // told what Next needs
    _adShow(step, focus) {
        const ad = this._ad, s = ad.s;
        const from = ad.shell.current;
        if (from && from !== step) this._adLeave(from);
        s.step = step;
        const host = ad.shell.panel(step);
        const key = this._adKey(step);
        if (host && host.dataset.adKey !== key) {
            host.dataset.adKey = key;
            host.innerHTML = this._adStepHtml(step);
            this._adWireStep(step, host);
        }
        ad.shell.showErrors([]);
        // a field's error belongs to the try that made it: gone on another step
        if (step !== 'connection') this._placeFieldErrors(document.getElementById('addDeviceContent'), {}, () => null, { focus: false });
        ad.shell.go(step, focus === undefined ? (step === 'what' && s.path !== 'measured' ? document.getElementById('adQ') : 'heading') : focus);
        this._adArrive(step);
        this._adFooter();
    },

    // what makes a step's panel be drawn again: the connection follows the
    // model (its questions narrow on it); Check and Name & save show what
    // the steps before them chose, every time
    _adKey(step) {
        const ad = this._ad, s = ad.s;
        if (step === 'connection') return `${s.path}:connection:${s.template}:${ad.gen}`;
        if (step === 'check' || step === 'save' || step === 'units') return `${s.path}:${step}:${Date.now()}`;
        if (step === 'what') return `${s.path}:what:${ad.gen}`;
        return `${s.path}:${step}:${ad.gen}`;
    },

    _adStepHtml(step) {
        const s = this._ad.s;
        if (step === 'what') return s.path === 'measured' ? this._adKindsHtml() : this._adWhatHtml();
        if (step === 'meters') return this._adMetersHtml();
        if (s.path === 'many') return this._amStepHtml(step);          // app-add-many.js (L3.7)
        if (step === 'connection') return this._adConnHtml();
        if (step === 'check') return `<div class="ad-check" id="adCheck"></div>`;
        if (step === 'save') return s.path === 'measured' ? this._adSaveHtml() : this._adSaveFullHtml();
        return this._adLaterHtml(step);
    },

    _adWireStep(step, host) {
        const s = this._ad.s;
        if (s.path === 'many') this._amWireStep(step, host);
        if (step === 'what' && s.path !== 'measured') {
            this._adDrawResults();
            if (this._ad.found.length) this._adFoundChip();
        }
        if (step === 'what' && s.path === 'measured') {
            // "+ Add type…": a type of one's own, created and picked in place
            this._typeFormWire('ad', ty => {
                this._ad.s.kind = ty.id;
                host.dataset.adKey = '';
                this._adShow('what', false);
                setTimeout(() => host.querySelector(`input[name="adKind"][value="${CSS.escape(ty.id)}"]`)?.focus(), 20);
            });
        }
        if (step === 'meters') this._metersRowsMount('adMtr', {});
        if (step === 'connection') this._adMountConn();
        if (step === 'save' && s.path === 'measured') {
            const name = document.getElementById('adName'), id = document.getElementById('adId');
            name?.addEventListener('input', () => {
                if (this._ad.s.idTouched) return;
                id.value = window.AddDevModel.idFromName(name.value);
            });
            id?.addEventListener('input', () => { this._ad.s.idTouched = true; });
        }
        if (step === 'save' && s.path !== 'measured') this._adWireSave(host);
    },

    // arriving: the check starts; Name & save asks the gateway
    _adArrive(step) {
        const s = this._ad.s;
        if (step === 'check') this._adCheckStart(false);
        if (step === 'save' && s.path !== 'measured') this._adDrySoon(0);
    },

    // leaving: what was answered is kept; a check still reading is stopped
    // (said as skipped: Next went on without it)
    _adLeave(step) {
        const ad = this._ad;
        if (step === 'connection') this._adConnKeep();
        if (step === 'check' && ad.s.check.state === 'running') {
            if (ad.pvRun) { ad.pvRun.stop = true; this._pvCancel(ad.pvRun.job); }
            ad.pvRun = null;
            ad.s.check.state = 'skipped';
        }
    },

    // what the steps on screen hold, into the state (on Next, Back, the rail
    // and the dirty test): nothing typed is lost
    _adCollect() {
        const ad = this._ad, s = ad.s;
        const g = id => document.getElementById(id);
        const q = g('adQ');
        if (q) s.q = q.value;
        const kind = document.querySelector('#addDeviceModal input[name="adKind"]:checked');
        if (kind) s.kind = kind.value;
        if (g('adMtrRows')?.dataset.ready) s.meters = this._metersRowsRead('adMtr');
        const name = g('adName'), id = g('adId');
        if (name) s.name = name.value.trim();
        if (id) s.id = id.value.trim();
        // words typed in + Add type… (not created yet) are something to lose
        s.typeDraft = ['adTnEn', 'adTnRo', 'adTnId', 'adsTnEn', 'adsTnRo', 'adsTnId'].map(x => g(x)?.value || '').join('').trim();
        if (ad.shell.current === 'connection' || ad.cs) this._adConnKeep();
        if (g('adSaveForm')) this._adSaveCollect();
        if (s.path === 'many') this._amCollect(s);
        return s;
    },

    _adMove(dir) {
        const ad = this._ad;
        if (!ad || ad.busy) return;
        if (ad.result) return;
        this._adCollect();
        const next = window.AddDevModel.go(ad.s, dir);
        this._adShow(next.step);
    },

    // the rail, a Change link: a step already passed
    _adGoto(id) {
        if (!this._ad || this._ad.busy || this._ad.result) return;
        this._adCollect();
        this._adShow(id);
    },

    // another way in: the steps change, every answer stays
    _adSetPath(path) {
        const ad = this._ad;
        this._adCollect();
        ad.s = window.AddDevModel.setPath(ad.s, path);
        ad.shell.setSteps(this._adSteps());
        this._adShow(ad.s.step);
        ad.shell.say(this.t('adddev.pathSaid', '{steps}: {list}', {
            steps: this.t('adddev.steps', 'Steps'), list: this._adSteps().map(x => x.label).join(' · ') }));
    },

    _adWhy(why) {
        const t = (k, d) => this.t(k, d);
        return ({
            pick: t('adddev.why.pick', 'Pick a model, or one of the three other ways above the list.'),
            kind: t('adddev.why.kind', 'Say what it is.'),
            meters: t('adddev.why.meters', 'Name at least one meter.'),
            later: t('adddev.why.later', 'Many of the same on one bus comes with the bus scan: add them one at a time with Create another like this.'),
            units: t('adddev.why.units', 'Scan the bus (or listen to it) and tick the units to add.'),
        })[why] || '';
    },

    // the footer: Next's words and state, the line beside it
    _adFooter() {
        const ad = this._ad, s = ad.s, M = window.AddDevModel;
        const t = (k, d, p) => this.t(k, d, p);
        const sh = ad.shell;
        sh.el.cancel.hidden = !!ad.result;
        // the result panel has its own Open it · Add another · Done: one Done
        // on the screen, not a second one in the footer (L3-UX-12)
        sh.el.next.hidden = !!ad.result;
        if (ad.result) {
            sh.el.back.hidden = true;
            sh.setSummary(ad.result.line);
            return;
        }
        const c = M.canNext(s);
        let disabled = !c.ok, why = c.ok ? '' : this._adWhy(c.why);
        if (c.ok && s.step === 'save' && s.path !== 'measured' && ad.dry && ad.dry.refused) {
            disabled = true;
            why = ad.dry.why;
        }
        const tally = c.batch ? M.batchTally((s.batch || {}).rows || []) : null;
        const label = c.batch ? (tally.total && !tally.left ? t('common.done', 'Done') : this.tn('am.createN', tally.left || ((s.batch || {}).units || []).length, 'Create {n} device(s)'))
            : M.nextLabel(s, t);
        sh.setNext(label, { disabled, why });
        let line = '';
        if (s.step === 'what' && s.path !== 'measured') {
            const tpl = this._adTpl();
            line = tpl ? t('adddev.sum.model', 'Model: {name}', { name: tpl.name || tpl.id }) : this._adWhy('pick');
        } else if (s.path === 'measured' && s.step === 'what') {
            line = s.kind ? t('adddev.sum.kind', 'Measured equipment: {kind}', { kind: this._kindLabel(s.kind) }) : this._adWhy('kind');
        } else if (s.step === 'check' && c.unchecked) {
            line = s.check.state === 'running' ? t('adddev.sum.checking', 'Reading… Next goes on without waiting.')
                : t('adddev.sum.unchecked', 'Not checked: it is created all the same, and the audit says so.');
        } else if (disabled) {
            line = why;
        }
        sh.setSummary(line);
    },

    async _adNext() {
        const ad = this._ad, M = window.AddDevModel;
        if (!ad || ad.busy) return;
        if (ad.loading) { ad.shell.say(this.t('adddev.loading', 'Reading the models…')); return; }
        if (ad.result) { this.closeModal('addDeviceModal'); return; }
        this._adCollect();
        const s = ad.s;
        const c = M.canNext(s);
        if (!c.ok) {
            ad.shell.say(this._adWhy(c.why));
            if (c.why === 'kind') document.querySelector('#addDeviceModal input[name="adKind"]')?.focus();
            return;
        }
        if (s.path === 'measured') {
            if (s.step === 'meters' && !await this._adCheckMeters()) return;
            if (c.create) return this._adCreateMeasured();
            return this._adShow(M.go(s, +1).step);
        }
        if (c.batch) return this._amCreate();                           // many of the same (L3.7)
        if (s.path === 'many' && s.step === 'many') { this._amLeaveConn(); return this._adShow(M.go(s, +1).step); }
        if (s.step === 'connection' && !await this._adCheckConn()) return;
        // the model chosen after the connection: the gateway judges the two together
        if (s.step === 'what' && s.path === 'connection' && !await this._adCheckConn()) return;
        if (c.create) return this._adCreate(false);
        this._adShow(M.go(s, +1).step);
    },

    // ── Step 1: What is it? ──────────────────────────────────────────────
    _adWhatHtml() {
        const ad = this._ad, s = ad.s, M = window.AddDevModel;
        const t = (k, d, p) => this.t(k, d, p);
        const { chips, more } = M.typeChips(ad.templates, this._deviceTypes);
        const chip = c => `<button type="button" role="radio" class="find-chip ad-chip" data-ad-type="${this._esc(c.id)}"
            aria-checked="${s.type === c.id}" tabindex="${s.type === c.id ? 0 : -1}">${c.id
                ? `<i aria-hidden="true" class="bi ${this._KIND_ICON[c.id] || this._KIND_ICON.other}"></i> ${this._esc(this._kindLabel(c.id))}`
                : this._esc(t('adddev.anyType', 'Any type'))}
            <span class="find-chip-n">${c.count}</span></button>`;
        const fromConn = s.path === 'connection';
        return `
        ${fromConn ? `<p class="field-hint ad-lead" id="adConnFit"></p>` : ''}
        <div class="ad-top">
            <p class="field-hint ad-lead" id="adWhatHint">${t('adddev.whatHint', 'Search by model, brand or type, or narrow by type. Pick the model to go on.')}</p>
            <button type="button" class="find-chip" id="adFoundChip" aria-expanded="false" aria-controls="adFoundPanel" hidden>
                <i aria-hidden="true" class="bi bi-inbox"></i> <span id="adFoundLabel"></span></button>
        </div>
        <div class="ad-found" id="adFoundPanel" hidden></div>
        <label class="sr-only" for="adQ">${t('adddev.searchLabel', 'Search the models')}</label>
        <input type="search" id="adQ" class="input ad-search" autocomplete="off" spellcheck="false" value="${this._esc(s.q)}"
            placeholder="${this._esc(t('adddev.searchPh', 'Model, brand or type — e.g. SDM630, Deye, Seplos, heat pump'))}"
            aria-describedby="adWhatHint" aria-controls="adResults">
        <div class="ad-chips" role="radiogroup" id="adChips" aria-label="${this._esc(t('adddev.typeGroup', 'Type'))}">${chips.map(chip).join('')}</div>
        ${more.length ? `<p class="field-hint ad-more">${this._esc(t('adddev.noModelTypes', 'No model yet for: {list}. Equipment that does not communicate is added with "It doesn\'t communicate" above.',
            { list: more.map(c => this._kindLabel(c.id)).join(', ') }))}</p>` : ''}
        ${fromConn ? '' : `<div class="ad-ways" role="group" aria-label="${this._esc(t('adddev.ways', 'Other ways'))}">
            <button type="button" class="ad-way" data-ad-way="connection"><i aria-hidden="true" class="bi bi-ethernet"></i>
                <span><b>${t('adddev.way.connection', 'I don\'t know the model — start from the connection')}</b>
                <span>${t('adddev.way.connectionHint', 'Port, bus and address first; the model after.')}</span></span></button>
            <button type="button" class="ad-way is-measured" data-ad-way="measured"><i aria-hidden="true" class="bi bi-speedometer2"></i>
                <span><b>${t('adddev.way.measured', 'It doesn\'t communicate — measured by other meters')}</b>
                <span>${t('adddev.way.measuredHint', 'A UPS, a charger, a boiler: one meter on its supply, or one on what goes in and one on what comes out.')}</span></span></button>
            <button type="button" class="ad-way" data-ad-way="many"><i aria-hidden="true" class="bi bi-layers"></i>
                <span><b>${t('adddev.way.many', 'Many of the same on one bus')}</b>
                <span>${t('adddev.way.manyHint', 'Scan the bus, tick the units, add them in one go.')}</span></span></button>
        </div>`}
        <p class="ad-count" id="adCount"></p>
        <div class="ad-results" role="listbox" id="adResults" aria-label="${this._esc(t('adddev.models', 'Models'))}" aria-describedby="adCount"></div>
        <div class="ad-tpl-acts" id="adTplActs">${this._adTplActsHtml()}</div>
        <div class="ad-tpl-notes" id="adTplNotes" aria-live="polite">${this._tplNotesHtml(((this._adTpl() || {}).notes) || [])}</div>
`;
    },

    _adPortWord(p) {
        const t = (k, d) => this.t(k, d);
        return ({ serial: t('adddev.port.serial', 'RS-485'), ethernet: t('adddev.port.ethernet', 'Ethernet'),
                  can: t('adddev.port.can', 'CAN'), http: t('adddev.port.http', 'Web API'), mqtt: t('adddev.port.mqtt', 'MQTT') })[p] || p;
    },

    _adBadgeHtml(b, tpl) {
        const t = (k, d) => this.t(k, d);
        const word = ({ listens: t('adddev.badge.listens', 'Only listens'), setup: t('adddev.badge.setup', 'Needs setup writes'),
                        parts: t('adddev.badge.parts', 'Has parts'), note: t('adddev.badge.note', 'Note') })[b] || b;
        const M = window.AddDevModel;
        const warn = b === 'note' && M.noteWarn(tpl);
        const tip = b === 'note' ? M.noteText(tpl)
            : b === 'setup' ? this.t('adddev.badge.setupTip', '{n} setup writes before every value is read; an admin unlocks them from its ⋯ → Unlock writes… after creation', { n: (tpl.setup || {}).count || 0 })
            : b === 'parts' ? (tpl.components || []).map(c => c.name || c.type).join(', ')
            : b === 'listens' ? t('adddev.badge.listensTip', 'A passive monitor: it never transmits on the bus') : '';
        return `<span class="ad-badge is-${b}${warn ? ' is-warn' : ''}" ${tip ? `title="${this._esc(tip)}"` : ''}>${warn
            ? `<i aria-hidden="true" class="bi bi-exclamation-triangle"></i> ` : ''}${this._esc(word)}${warn
            ? `<span class="sr-only"> (${this._esc(t('adddev.badge.warn', 'warning'))})</span>` : ''}${tip && b !== 'listens'
            ? `<span class="sr-only">: ${this._esc(tip)}</span>` : ''}</span>`;
    },

    _adCardHtml(x) {
        const s = this._ad.s, M = window.AddDevModel;
        const on = x.id === s.template;
        const kind = x.device_kind || x.kind || 'other';
        const ports = M.ports(x).map(p => `<span class="ad-port"><i aria-hidden="true" class="bi ${p.icon}"></i>${this._esc(this._adPortWord(p.id))}</span>`).join('');
        return `<div class="ad-card" role="option" id="adOpt-${this._esc(x.id)}" data-ad-tpl="${this._esc(x.id)}" aria-selected="${on}" tabindex="-1">
            <span class="ad-card-name">${this._esc(x.name || x.id)}</span>
            <span class="ad-card-sub">${this._esc([x.vendor, this._kindLabel(kind)].filter(Boolean).join(' · '))}</span>
            <span class="ad-card-row">${ports}${M.badges(x).map(b => this._adBadgeHtml(b, x)).join('')}</span></div>`;
    },

    // By connection: the models that speak what the connection chose
    _adConnClass() {
        const s = this._ad.s, CM = window.ConnformModel;
        const mode = s.conn && s.conn.M ? s.conn.M.mode : '';
        if (!mode) return '';
        const c = CM.classOf(mode);
        return c === 'none' ? '' : c;
    },

    // the list for the type and the words chosen; one option is the tab stop
    // (the chosen model, else the first)
    _adDrawResults(say) {
        const ad = this._ad, s = ad.s, M = window.AddDevModel;
        const box = document.getElementById('adResults');
        if (!box) return;
        // by connection: the models that speak what it chose — in the list and
        // in every chip's count (a chip and its list always agree, L3-UX-12)
        const cls = s.path === 'connection' ? this._adConnClass() : '';
        const mctx = { catalog: this._deviceTypes, kindWords: this._KIND_WORDS };
        const list = M.filter(ad.templates, { type: s.type, q: s.q, transport: cls || '' }, mctx);
        const fit = document.getElementById('adConnFit');
        if (cls && fit) fit.textContent = this.t('adddev.connFit', 'The models that speak {proto}, as the connection chose.', { proto: this._PROTO_LABEL?.[cls] || cls });
        const counts = M.chipCounts(ad.templates, { q: s.q, transport: cls || '' }, mctx);
        document.querySelectorAll('#adChips [role="radio"]').forEach(b => {
            const n = b.querySelector('.find-chip-n');
            if (n && counts[b.dataset.adType] !== undefined) n.textContent = String(counts[b.dataset.adType]);
        });
        ad.list = list;
        box.innerHTML = list.length ? list.map(x => this._adCardHtml(x)).join('')
            : `<p class="ad-empty">${this._esc(this.t('adddev.none', 'No model matches. Try another word, Any type, or one of the other ways above.'))}</p>`;
        const stop = box.querySelector('[aria-selected="true"]') || box.querySelector('[role="option"]');
        if (stop) stop.tabIndex = 0;
        const count = this.tn('adddev.count', list.length, '{n} models');
        document.getElementById('adCount').textContent = count;
        if (say) ad.shell.say(count);
    },

    _adPickType(id, focus) {
        const s = this._ad.s;
        s.type = id;
        document.querySelectorAll('#adChips [role="radio"]').forEach(b => {
            const on = b.dataset.adType === id;
            b.setAttribute('aria-checked', String(on));
            b.tabIndex = on ? 0 : -1;
            if (on && focus) b.focus();
        });
        this._adDrawResults(true);
    },

    _adPickTemplate(id, focus) {
        const s = this._ad.s;
        if (s.template !== id) this._adCheckReset();
        s.template = id;
        document.querySelectorAll('#adResults [role="option"]').forEach(o => {
            const on = o.dataset.adTpl === id;
            o.setAttribute('aria-selected', String(on));
            o.tabIndex = on ? 0 : -1;
            if (on && focus) o.focus();
        });
        // the model's notes, read before going on (a warning among them marked)
        const notes = document.getElementById('adTplNotes');
        if (notes) notes.innerHTML = this._tplNotesHtml(((this._adTpl() || {}).notes) || []);
        const acts = document.getElementById('adTplActs');
        if (acts) acts.innerHTML = this._adTplActsHtml();
        this._adFooter();
    },

    // the model library from here: a map of one's own (upload, new) and, for
    // the model picked, its editor (a built-in: duplicate to edit) and export
    _adTplActsHtml() {
        const t = (k, d) => this.t(k, d);
        const x = this._adTpl();
        const own = `<button type="button" class="btn btn-ghost btn-sm" data-admin data-action="tplUpload"><i aria-hidden="true" class="bi bi-upload"></i> ${t('devtpl.upload', 'Upload template')}</button>
            <button type="button" class="btn btn-ghost btn-sm" data-admin ${this._act('openTplEditor', [null])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('devtpl.new', 'New template')}</button>`;
        if (!x) return own;
        return `${own}<span class="field-hint ad-tpl-of">${this._esc(x.name || x.id)}:</span>
            ${x.builtin
                ? `<button type="button" class="btn btn-ghost btn-sm" data-admin ${this._act('openTplEditor', [x.id, true])}><i aria-hidden="true" class="bi bi-copy"></i> ${t('devtpl.duplicate', 'Duplicate to edit')}</button>`
                : `<button type="button" class="btn btn-ghost btn-sm" data-admin ${this._act('openTplEditor', [x.id])}><i aria-hidden="true" class="bi bi-pencil"></i> ${t('common.edit', 'Edit')}</button>`}
            <button type="button" class="btn btn-ghost btn-sm" ${this._act('tplExport', [x.id])}><i aria-hidden="true" class="bi bi-download"></i> ${t('common.export', 'Export')}</button>`;
    },

    // the template list changed (uploaded, created): drawn anew, maybe picked
    _adTemplatesChanged(templates, selectId) {
        const ad = this._ad;
        if (!ad || !ad.open) return;
        ad.templates = templates || ad.templates;
        if (ad.shell.current === 'what' && ad.s.path !== 'measured') this._adDrawResults(true);
        if (selectId && ad.templates.some(x => x.id === selectId)) this._adPickTemplate(selectId, false);
        this._adFooter();
    },

    // ── keyboard and pointer, delegated on the dialog ───────────────────
    _adOnClick(ev) {
        const ad = this._ad;
        if (!ad || !ad.open) return;
        const el = ev.target.closest ? ev.target : null;
        if (!el) return;
        if (this._amOnClick(ev)) return;                                 // the scan panel (L3.7)
        const chip = el.closest('#adChips [role="radio"]');
        if (chip) return this._adPickType(chip.dataset.adType, true);
        const card = el.closest('#adResults [role="option"]');
        if (card) return this._adPickTemplate(card.dataset.adTpl, true);
        const way = el.closest('[data-ad-way]');
        if (way) return this._adSetPath(way.dataset.adWay);
        const goto = el.closest('[data-ad-goto]');
        if (goto) return this._adGoto(goto.dataset.adGoto);
        if (el.closest('#adFoundChip')) return this._adToggleFound();
        const use = el.closest('[data-ad-use]');
        if (use) return this._adUseFound(use.dataset.adUse);
        if (el.closest('#adCheckAgain')) return this._adCheckStart(true);
        if (el.closest('#adCheckModel')) return this._adChangeModel();
        const sug = el.closest('[data-pv-use]');
        if (sug) return this._adUseSuggested(sug.dataset.pvUse);
        if (el.closest('#adCreateAnother')) return this._adCreate(true);
        if (el.closest('#adResOpen')) return this._adResultOpen();
        if (el.closest('#adResAnother')) return this._adAnother();
        if (el.closest('#adResDone')) return this.closeModal('addDeviceModal');
    },

    _adOnKey(ev) {
        const ad = this._ad;
        if (!ad || !ad.open) return;
        const el = ev.target;
        const roving = (sel, onPick) => {
            const items = [...document.querySelectorAll(sel)];
            const i = items.indexOf(el);
            if (i < 0) return false;
            const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[ev.key];
            const to = ev.key === 'Home' ? 0 : ev.key === 'End' ? items.length - 1 : step ? (i + step + items.length) % items.length : null;
            if (to === null) return false;
            ev.preventDefault();
            onPick(items[to]);
            return true;
        };
        // the type chips: a radiogroup, the choice follows the arrow
        if (el.closest && el.closest('#adChips')) {
            roving('#adChips [role="radio"]', b => this._adPickType(b.dataset.adType, true));
            return;
        }
        // the models: a listbox; the arrows move, Space picks, Enter picks and goes on
        if (el.closest && el.closest('#adResults')) {
            if (roving('#adResults [role="option"]', o => {
                document.querySelectorAll('#adResults [role="option"]').forEach(x => { x.tabIndex = x === o ? 0 : -1; });
                o.focus();
            })) return;
            const opt = el.closest('[role="option"]');
            if (opt && (ev.key === ' ' || ev.key === 'Enter')) {
                ev.preventDefault();
                this._adPickTemplate(opt.dataset.adTpl, true);
                if (ev.key === 'Enter') this._adNext();
            }
            return;
        }
        if (el.id === 'adQ') {
            if (ev.key === 'ArrowDown') {
                ev.preventDefault();
                document.querySelector('#adResults [role="option"][tabindex="0"]')?.focus();
            } else if (ev.key === 'Enter') {
                ev.preventDefault();
                const list = ad.list || [];
                if (list.length === 1) {
                    const was = this._adTpl();
                    if (was && was.id !== list[0].id) {
                        ad.shell.say(this.t('adddev.replaced', '{name} picked, instead of {was}', { name: list[0].name || list[0].id, was: was.name || was.id }));
                    }
                    this._adPickTemplate(list[0].id, false);
                    this._adNext();
                }
                else document.querySelector('#adResults [role="option"][tabindex="0"]')?.focus();
            }
            return;
        }
        // Enter in a text field of the connection or of Name & save moves on
        if (ev.key === 'Enter' && el.tagName === 'INPUT' && !['checkbox', 'radio', 'search', 'button'].includes(el.type)
            && (el.closest('#adConnHost') || el.closest('#adSaveForm') || el.id === 'adName' || el.id === 'adId')
            && !el.closest('.type-new-form')) {
            ev.preventDefault();
            this._adNext();
        }
    },

    _adOnInput(ev) {
        const ad = this._ad;
        if (!ad || !ad.open) return;
        if (ev.target.id === 'adQ') {
            ad.s.q = ev.target.value;
            clearTimeout(ad.qTimer);
            // the list at once; its count said once typing pauses
            this._adDrawResults(false);
            ad.qTimer = setTimeout(() => ad.shell.say(document.getElementById('adCount')?.textContent || ''), 600);
            return;
        }
        if (ev.target.closest && ev.target.closest('#adSaveForm')) this._adSaveChanged(ev.target);
    },

    _adOnChange(ev) {
        const ad = this._ad;
        if (!ad || !ad.open) return;
        const el = ev.target;
        if (el.name === 'adKind') {
            ad.s.kind = el.value;
            document.querySelectorAll('#adKinds .seg-btn').forEach(l => l.classList.toggle('on', l.querySelector('input').checked));
            this._adFooter();
        } else if (/^adMtr/.test(el.id || '')) {
            this._adCollect();             // a meter picked: Next may go on now
            this._adFooter();
        } else if (el.closest && el.closest('#adCheck') && el.matches('[data-ad-comp]')) {
            this._adCompChanged();
        } else if (el.closest && el.closest('#adSaveForm')) {
            this._adSaveChanged(el);
        }
    },

    // ── Found (n): what the gateway already heard of ─────────────────────
    async _adLoadFound() {
        const ad = this._ad;
        let mqtt = null;
        try { const r = await fetch('/api/mqtt-discovery'); if (r.ok) mqtt = await r.json(); } catch (e) { /* none */ }
        if (this._ad !== ad || !ad.open) return;
        ad.found = window.AddDevModel.foundItems(mqtt, this._brFound || []);
        this._adFoundChip();
    },

    _adFoundChip() {
        const ad = this._ad;
        const chip = document.getElementById('adFoundChip');
        if (!chip) return;
        chip.hidden = !ad.found.length;
        document.getElementById('adFoundLabel').textContent = this.tn('adddev.found.chip', ad.found.length, 'Found ({n})');
    },

    _adToggleFound() {
        const ad = this._ad, t = (k, d, p) => this.t(k, d, p);
        const chip = document.getElementById('adFoundChip'), box = document.getElementById('adFoundPanel');
        const open = chip.getAttribute('aria-expanded') !== 'true';
        chip.setAttribute('aria-expanded', String(open));
        box.hidden = !open;
        if (!open) return;
        const has = id => ad.templates.some(x => x.id === id);
        box.innerHTML = `<p class="field-hint" id="adFoundWhy">${this._esc(t('adddev.found.lead', 'Heard by the gateway without asking anything: devices announcing themselves on MQTT, and what "Find on the LAN" answered. Searching the network is never started from here.'))}</p>
            <ul>${ad.found.map((f, i) => {
                const missing = f.source === 'mqtt' && (f.state === 'template_missing' || !has(f.template));
                return `<li><b>${this._esc(f.label || f.key)}</b>
                <span class="field-hint" id="adFoundW-${i}">${this._esc(f.source === 'mqtt'
                    ? (missing ? t('adddev.found.noTpl', 'on MQTT · its model {tpl} is not on this gateway yet', { tpl: f.template || '—' })
                               : t('adddev.found.mqtt', 'on MQTT · template {tpl}', { tpl: f.template || '—' }))
                    : t('adddev.found.lan', 'on the LAN · {where} — add it as a bridge first', { where: f.key }))}</span>
                <button type="button" class="btn btn-ghost btn-sm" data-ad-use="${i}" ${missing ? 'aria-disabled="true"' : ''}
                    aria-describedby="adFoundW-${i}">${f.source === 'bridge' ? t('adddev.found.addBridge', 'Add bridge') : t('adddev.found.use', 'Use')}</button></li>`;
            }).join('')}</ul>`;
    },

    // Found → Use: an MQTT device goes to Check with its model and
    // connection; a bridge found on the LAN is added as a bridge first
    _adUseFound(i) {
        const ad = this._ad;
        const f = ad.found[+i];
        if (!f) return;
        const btn = document.querySelector(`[data-ad-use="${CSS.escape(String(i))}"]`);
        if (btn && btn.getAttribute('aria-disabled') === 'true') {
            ad.shell.say(document.getElementById(`adFoundW-${i}`)?.textContent || '');
            return;
        }
        if (f.source === 'bridge') {
            ad.done = true;
            this.closeModal('addDeviceModal');
            if (typeof this.addFoundBridge === 'function') this.addFoundBridge(f.idx);
            return;
        }
        const s = this._adFromFound(f);
        if (!s) return;
        ad.gen++;
        ad.s = s;
        ad.shell.setSteps(this._adSteps());
        this._adShow(s.step);
    },

    // ── Step 2: the connection (conn-step.js) ────────────────────────────
    _adConnHtml() {
        const s = this._ad.s, t = (k, d, p) => this.t(k, d, p);
        const first = window.AddDevModel.stepsOf(s.path)[0] === 'connection';
        return `${first ? `<p class="ad-back-link"><button type="button" class="btn btn-ghost btn-sm" data-ad-way="model">
                <i aria-hidden="true" class="bi bi-arrow-left"></i> ${t('adddev.backToModels', 'Choose a model instead')}</button></p>` : ''}
            <div id="adConnHost" class="cf-host"></div>`;
    },

    _adMountConn() {
        const ad = this._ad, s = ad.s, M = window.AddDevModel;
        const host = document.getElementById('adConnHost');
        if (!host) return;
        const opts = {
            context: 'add', px: 'csAdd',
            template: () => this._adTpl(),
            onChange: () => { if (this._ad === ad) { this._adConnKeep(); this._adFooter(); } },
        };
        if (s.path === 'model') opts.onChangeModel = () => this._adGoto('what');
        const st = s.conn;
        if (st && st.cs && st.cs.axes) opts.state = st;
        else if (st && st.M) { opts.M = st.M; opts.prefilled = true; opts.touchedUnit = !!((st.cs || {}).touched || {}).unit; }
        else {
            // a sensor bridge's documented topic, its <placeholder> to replace
            const pre = M.topicPreset(s.template);
            if (pre) opts.prefill = { topic: pre };
        }
        ad.cs = window.mountConnStep(host, opts);
        // the step is drawn again once the buses are read: the marks follow
        if (!host._adMarkObs) {
            host._adMarkObs = new MutationObserver(() => this._adMarkAdopted());
            host._adMarkObs.observe(host, { childList: true, subtree: true });
        }
        this._adMarkAdopted();
        if (st && st.pwCleared && !document.getElementById('adPwAgain')) {
            host.insertAdjacentHTML('beforebegin', `<p class="field-hint ad-pw-again" id="adPwAgain" role="note"><i aria-hidden="true" class="bi bi-key"></i> ${this._esc(this.t('adddev.pwAgain', 'The copied device had a password: it is never copied — type it again.'))}</p>`);
        }
    },

    // the connection step's answers, into the state (Back, the rail, Next)
    _adConnKeep() {
        const ad = this._ad;
        const live = ad && ad.cs && ad.cs.form && ad.cs.form.host && ad.cs.form.host.isConnected ? ad.cs : null;
        if (!live) return;
        const before = JSON.stringify(ad.s.conn ? ad.s.conn.M : null);
        ad.s.conn = live.state();
        if (JSON.stringify(ad.s.conn.M) !== before && before !== 'null') this._adCheckReset();
    },

    // the connection as it would be sent: the live step's, else the one kept
    _adConnection() {
        const ad = this._ad, s = ad.s, CM = window.ConnformModel;
        const live = ad.cs && ad.cs.form && ad.cs.form.host && ad.cs.form.host.isConnected ? ad.cs : null;
        if (live) {
            live.read();
            ad.s.conn = live.state();
            return this._cfWithAdapter ? this._cfWithAdapter(live.form, live.payload()) : live.payload();
        }
        if (s.conn && s.conn.M) return CM.wire(s.conn.M);
        if (s.prefill) return CM.wire(CM.blank(s.prefill));
        return null;
    },

    // Next on the connection: what the form can tell itself, then what the
    // gateway refuses (only=connection: its fields, the matrix, the bus
    // claims, the Seplos mode rule) — each at its field
    async _adCheckConn() {
        const ad = this._ad, t = (k, d, p) => this.t(k, d, p);
        const live = ad.cs && ad.cs.form && ad.cs.form.host && ad.cs.form.host.isConnected ? ad.cs : null;
        if (live) {
            const errs = live.errors();
            if (Object.keys(errs).length) {
                if (ad.shell.current !== 'connection') this._adShow('connection', false);
                const left = live.showErrors(errs);
                ad.shell.showErrors([...Object.entries(errs).map(([k, v]) => ({ section: 'connection', text: v, fieldId: live.fieldId(k) })),
                                     ...left.filter(x => !Object.keys(errs).some(k => x.startsWith(k + ': '))).map(text => ({ text }))]);
                ad.shell.say(t('devices.fixMarked', 'Fix the marked fields.'));
                (document.getElementById(live.fieldId(Object.keys(errs)[0]) || '') || ad.shell.el.errors.querySelector('a'))?.focus();
                return false;
            }
        }
        const conn = this._adConnection();
        if (!conn) {
            if (ad.shell.current !== 'connection') this._adShow('connection');
            ad.shell.say(t('cs.err.port', 'Choose the port it is plugged into.'));
            return false;
        }
        let r, res;
        ad.busy = true;
        ad.shell.el.next.setAttribute('aria-busy', 'true');
        try {
            const body = { connection: conn };
            if (ad.s.template) body.template = ad.s.template;
            r = await fetch('/api/devices?dry_run=true&only=connection', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            res = await r.json().catch(() => ({}));
        } catch (e) {
            return true;                                 // the gateway unreachable: Create will say
        } finally {
            ad.busy = false;
            ad.shell.el.next.removeAttribute('aria-busy');
        }
        if (r.ok) { ad.s.bus = res.bus ?? null; return true; }
        if (r.status !== 422 && r.status !== 409) return true;
        const det = res.detail || {};
        const fields = Object.fromEntries(Object.entries(det.fields || {}).filter(([k]) => k.startsWith('connection')));
        const keys = Object.keys(fields);
        const other = (det.errors || []).map(String).filter(e => !keys.some(k => e.startsWith(k + ': ')));
        if (!keys.length && !other.length) return true;
        this._adConnRefused(fields, other);
        return false;
    },

    // a refusal of the connection: on its step, each at its field
    _adConnRefused(fields, other) {
        const ad = this._ad;
        if (ad.shell.current !== 'connection') this._adShow('connection', false);
        const live = ad.cs;
        const left = live ? live.showErrors(fields) : [];
        const keys = Object.keys(fields || {});
        ad.shell.showErrors([...keys.map(k => ({ section: 'connection', text: fields[k], fieldId: live ? live.fieldId(k) : null })),
                             ...[...left.filter(x => !keys.some(k => x.startsWith(k + ': '))), ...(other || [])].map(text => ({ text }))]);
        const first = keys.map(k => document.getElementById((live && live.fieldId(k)) || '')).find(Boolean);
        (first || ad.shell.el.errors.querySelector('a') || ad.shell.heading('connection'))?.focus();
    },

    // MQTT presets (the connection step's chips): the documented topic, and
    // the model they stand for when none is chosen yet
    _adMqttPreset(tplId) {
        const ad = this._ad, M = window.AddDevModel;
        const inp = document.getElementById('csAddMqttTopic');
        if (inp && !inp.value.trim()) inp.value = M.topicPreset(tplId);
        if (ad && !ad.s.template && ad.templates.some(x => x.id === tplId)) { this._adCheckReset(); ad.s.template = tplId; }
        inp?.focus();
        inp?.select?.();
    },

    // ── Step 3: Check (a read preview, app-preview.js) ───────────────────
    // adopted: the line of the device that holds the bus, taken by the run
    // before this one (said in the answer)
    async _adCheckStart(force, adopted = null) {
        const ad = this._ad, s = ad.s, M = window.AddDevModel;
        const t = (k, d, p) => this.t(k, d, p);
        const conn = this._adConnection();
        const key = JSON.stringify([s.template, conn]);
        if (!force && s.check.key === key && ['running', 'done', 'refused'].includes(s.check.state)) {
            this._adCheckDraw();
            if (s.check.state === 'running' && !ad.pvRun) this._adCheckFollow(s.check);
            return;
        }
        const confirm = !!document.getElementById('adChkConfirmOpen')?.checked;
        // the run before this one is stopped, and its job cancelled first
        // (one preview per bus: a second would be refused while it runs)
        if (ad.pvRun) { const old = ad.pvRun; ad.pvRun = null; old.stop = true; await this._pvCancel(old.job); }
        if (this._ad !== ad || !ad.open) return;
        const chk = s.check = Object.assign(M.blankCheck(), { state: 'running', key, confirmOpen: confirm, adopted });
        if (!conn || !s.template) {
            chk.state = 'refused';
            chk.refusal = { status: 0, kind: 'refused', errors: [t('adddev.chk.needBoth', 'A model and a connection are needed first.')], detail: {} };
            this._adCheckDraw();
            this._adFooter();
            return;
        }
        this._adCheckDraw();
        this._adFooter();
        const body = { connection: conn, template: s.template };
        if (confirm) body.confirm_open = true;
        let got = await this._pvStart(body);
        // a job of ours still on this bus (a Run again before its cancel landed): once more
        if (got.refused && got.refused.status === 409 && !(got.refused.detail || {}).config_blocked && (got.refused.detail || {}).job) {
            await this._pvCancel(got.refused.detail.job);
            got = await this._pvStart(body);
        }
        // the dialog closed, the step left or the check reset meanwhile: the
        // job it started is cancelled at once, never left reading the bus
        if (this._ad !== ad || !ad.open || ad.s.check !== chk || ad.shell.current !== 'check') {
            if (got.job) this._pvCancel(got.job);
            return;
        }
        // a bus a running device reads: its line and timeout are the ones any
        // reader of that bus uses — taken (the device keeps them), said, and
        // asked again once; never the raw "connection.timeout: …" (L3-UX-12)
        const shared = M.sharedLine(got.refused);
        if (shared && !adopted) {
            this._adAdoptLine(shared.line, shared.holder);
            return this._adCheckStart(true, shared);
        }
        if (got.refused) {
            chk.state = 'refused';
            chk.refusal = got.refused;
            this._adCheckDraw();
            this._adFooter();
            ad.shell.say(this.t('adddev.chk.refused', 'The check could not run: {why}', { why: (got.refused.errors || [])[0] || '' }));
            return;
        }
        chk.job = got.job;
        chk.view = Object.assign({ state: 'running' }, got.head);
        this._adCheckDraw();
        this._adCheckFollow(chk);
    },

    // the holder's line / timeout into the connection the device is created
    // with: the live step's fields when it is still drawn, else the kept state
    // the change is said in the dialog's live region and the fields it changed
    // are marked on the Connection step (review m3: never silently)
    _adAdoptLine(line, holder = '') {
        const ad = this._ad, M = window.AddDevModel;
        ad.s.adopted = { holder, line: Object.assign({}, (ad.s.adopted || {}).line || {}, line || {}) };
        ad.shell.say(this._adAdoptedText(ad.s.adopted));
        const live = ad.cs && ad.cs.form && ad.cs.form.host && ad.cs.form.host.isConnected ? ad.cs : null;
        if (live) {
            for (const [k, v] of Object.entries(line || {})) {
                const el = document.getElementById(live.fieldId(`connection.${k}`) || '');
                if (el && v !== null && v !== undefined) el.value = String(v);
            }
            live.read();
            live.form.M = M.adoptLine(live.form.M, line);
            ad.s.conn = live.state();
            ad.s.conn.M = M.adoptLine(ad.s.conn.M, line);
        } else if (ad.s.conn && ad.s.conn.M) {
            ad.s.conn.M = M.adoptLine(ad.s.conn.M, line);
        }
        this._adMarkAdopted();
    },

    // the fields the bus's values went into: data-adopted and a note with
    // whose they are; drawn again whenever the step is (a typed value takes
    // the field back, unmarked)
    _adMarkAdopted() {
        const ad = this._ad;
        const a = ad && ad.s.adopted;
        const live = ad && ad.cs && ad.cs.form && ad.cs.form.host && ad.cs.form.host.isConnected ? ad.cs : null;
        if (!a || !live) return;
        const t = (k, d, p) => this.t(k, d, p);
        const who = ((this._devices || []).find(d => d.id === a.holder) || {}).name || a.holder;
        for (const k of Object.keys(a.line || {})) {
            const el = document.getElementById(live.fieldId(`connection.${k}`) || '');
            if (!el || String(el.value) !== String(a.line[k])) continue;
            if (!el.hasAttribute('data-adopted')) {
                el.setAttribute('data-adopted', '');
                el.addEventListener('input', () => {
                    delete (ad.s.adopted.line || {})[k];
                    el.removeAttribute('data-adopted');
                    document.getElementById(`${el.id}Adopted`)?.remove();
                }, { once: true });
            }
            if (!document.getElementById(`${el.id}Adopted`)) {
                const note = document.createElement('p');
                note.className = 'field-hint ad-adopted-note';
                note.id = `${el.id}Adopted`;
                note.setAttribute('data-adopted-note', '');
                note.textContent = t('adddev.adoptedField', 'Taken from {who}’s connection ({id}): this bus is read through it.', { who, id: a.holder });
                (el.closest('.form-group') || el.parentElement).appendChild(note);
                const d = el.getAttribute('aria-describedby') || '';
                if (!d.split(' ').includes(note.id)) el.setAttribute('aria-describedby', `${d} ${note.id}`.trim());
            }
        }
    },

    _adAdoptedText(a) {
        const t = (k, d, p) => this.t(k, d, p);
        const who = ((this._devices || []).find(d => d.id === a.holder) || {}).name || a.holder;
        const word = (k, v) => ({
            timeout: t('adddev.chk.lineTimeout', 'timeout {v} s', { v }), baudrate: t('adddev.chk.lineBaud', '{v} Bd', { v }),
            parity: t('adddev.chk.lineParity', 'parity {v}', { v }), bytesize: t('adddev.chk.lineBits', '{v} data bits', { v }),
            stopbits: t('adddev.chk.lineStop', '{v} stop bit(s)', { v }),
        })[k] || `${k} ${v}`;
        const what = Object.entries(a.line || {}).map(([k, v]) => word(k, v)).join(', ');
        return t('adddev.chk.adopted', 'This bus is read through {who}’s connection ({id}): its values are used — {what} — and the device takes them too.', { who, id: a.holder, what });
    },

    // "This bus is read through Cabinet meter’s connection (em1): its values
    // are used — timeout 1 s — and the device takes them too."
    _adAdoptedHtml(a) {
        if (!a) return '';
        return `<p class="field-hint ad-chk-adopted" id="adChkAdopted" role="note"><i aria-hidden="true" class="bi bi-diagram-3"></i> ${this._esc(this._adAdoptedText(a))}</p>`;
    },

    async _adCheckFollow(chk) {
        const ad = this._ad, s = ad.s, M = window.AddDevModel;
        const run = ad.pvRun = { job: chk.job, stop: false };
        if (!ad.open) { run.stop = true; this._pvCancel(run.job); return; }
        const view = await this._pvFollow(run, v => {
            if (this._ad !== ad || s.check !== chk) return;
            chk.view = v;
            if (ad.shell.current === 'check') this._adCheckDraw();
        });
        if (ad.pvRun === run) ad.pvRun = null;
        if (!view || this._ad !== ad || ad.s.check !== chk) return;
        chk.state = view.state === 'done' ? 'done' : 'failed';
        // the components it holds: ticked (read-only parts, ADR-0031 §2)
        const have = new Map((ad.s.components || []).map(c => [`${c.type}:${c.instance}`, c]));
        ad.s.components = (view.components || []).map(c => Object.assign({ type: c.type, instance: c.instance,
            name: c.name || c.product || '', product: c.product || '', on: true }, have.get(`${c.type}:${c.instance}`) || {}));
        // the nameplate pre-filled from what it said of itself (a field typed in stays)
        const np = M.applyNameplate(ad.s, view);
        ad.s.identity = np.identity;
        ad.s.plateFrom = np.plateFrom;
        if (ad.shell.current === 'check') this._adCheckDraw();
        this._adFooter();
        const v = M.checkVerdict(view);
        ad.shell.say(v === 'ok' ? this.t('adddev.chk.said.ok', 'Checked: it answers.') : this.t('adddev.chk.said.none', 'Checked: nothing came back. Next goes on without a check.'));
    },

    _adCheckDraw() {
        const ad = this._ad, s = ad.s;
        const host = document.getElementById('adCheck');
        if (!host) return;
        const t = (k, d, p) => this.t(k, d, p);
        const tpl = this._adTpl();
        const c = s.check;
        const focusId = host.contains(document.activeElement) ? document.activeElement.id : '';
        const tools = `<div class="ad-check-tools">
            <button type="button" class="btn btn-secondary btn-sm" id="adCheckAgain"><i aria-hidden="true" class="bi bi-arrow-repeat"></i> ${this._esc(t('pv.again', 'Run again'))}</button>
            <button type="button" class="btn btn-ghost btn-sm" id="adCheckModel"><i aria-hidden="true" class="bi bi-arrow-left"></i> ${this._esc(t('cs.changeModel', 'Change model'))}</button>
            <span class="field-hint">${this._esc(t('adddev.chk.lead', 'Reads only, through the bus\'s own connection, within the budget (Settings → Modbus → Read preview budget). It never blocks Create.'))}</span></div>`;
        let body = '';
        if (!host.querySelector('#adCheckAgain')) host.innerHTML = tools + '<div id="adChkBody" aria-live="off"></div>';
        const bodyHost = host.querySelector('#adChkBody');
        if (c.state === 'refused' && c.refusal) {
            body = this._pvRefusalHtml(c.refusal, 'adChk', { confirmed: c.confirmOpen });
        } else if (c.view) {
            body = this._pvHtml(c.view, { px: 'adChk', onUse: true, extra: v => this._adSetupTruthHtml(tpl) + this._adCompsHtml(v, tpl) });
        } else if (c.state === 'skipped') {
            body = `<p class="field-hint">${this._esc(t('adddev.chk.skipped', 'The check was left before its answer. Run it again, or go on without it.'))}</p>`;
        } else {
            body = `<p class="field-hint" role="status"><span class="btn-spinner" aria-hidden="true"></span> ${this._esc(t('pv.starting', 'Asking the gateway…'))}</p>`;
        }
        if (c.state !== 'running' && !c.view) body += this._adSetupTruthHtml(tpl);
        body = this._adAdoptedHtml(c.adopted) + body;
        // only the answer is drawn again (the tools keep their focus; a part's
        // name being typed is kept: the parts appear once, when the read ends)
        if (bodyHost.dataset.sig === body) return;
        bodyHost.dataset.sig = body;
        bodyHost.innerHTML = body;
        if (focusId && !document.getElementById(focusId)) host.querySelector('#adCheckAgain')?.focus();
        else if (focusId) document.getElementById(focusId)?.focus();
    },

    // setup writes: the truth, said once (ADR-0031 §2 — never unlocked here)
    _adSetupTruthHtml(tpl) {
        const su = (tpl || {}).setup || {};
        if (!su.count) return '';
        const t = (k, d, p) => this.t(k, d, p);
        const list = (su.writes || []).map(w => `FC${w.fc} ${t('adddev.chk.at', 'at')} ${w.address}`).join(', ');
        return `<p class="ad-setup-truth" id="adChkSetup"><i aria-hidden="true" class="bi bi-lock"></i> ${this._esc(this.tn('adddev.chk.setup', su.count,
            'This model maps some values only after its {n} setup write(s){list}. A new device is created write-locked: those values stay empty until an admin unlocks it from its ⋯ → Unlock writes….',
            { list: list ? ` (${list})` : '' }))}</p>`;
    },

    // Found inside it: the parts the preview heard, ticked, each renamable
    _adCompsHtml(view, tpl) {
        const s = this._ad.s, t = (k, d, p) => this.t(k, d, p);
        if (!((tpl || {}).components || []).length) return '';
        const comps = s.components || [];
        if (view.state === 'running') return '';
        if (!comps.length) {
            return `<p class="field-hint ad-comps-none">${this._esc(t('adddev.chk.compsNone', 'Nothing found inside it in this window. Its parts can be added later, on its page (Components).'))}</p>`;
        }
        return `<fieldset class="cf-fieldset ad-comps" id="adComps"><legend class="wiz-eyebrow">${this._esc(t('adddev.chk.comps', 'Found inside it'))}</legend>
            <p class="field-hint">${this._esc(t('adddev.chk.compsLead', 'Ticked ones are created with it, read-only, in the same request.'))}</p>
            ${comps.map((c, i) => `<div class="ad-comp"><label class="checkbox-label"><input type="checkbox" data-ad-comp="${i}" id="adComp-${i}" ${c.on !== false ? 'checked' : ''}>
                <span>${this._esc(`${c.type} ${c.instance}`)}${c.product ? ` · ${this._esc(c.product)}` : ''}</span></label>
                <label class="sr-only" for="adCompName-${i}">${this._esc(t('adddev.chk.compName', 'Name of {what}', { what: `${c.type} ${c.instance}` }))}</label>
                <input type="text" class="input input-sm" id="adCompName-${i}" data-ad-comp-name="${i}" data-ad-comp="${i}" maxlength="80" value="${this._esc(c.name || '')}"></div>`).join('')}
        </fieldset>`;
    },

    _adCompChanged() {
        const s = this._ad.s;
        document.querySelectorAll('#adComps input[type="checkbox"][data-ad-comp]').forEach(b => {
            const c = s.components[+b.dataset.adComp];
            if (c) c.on = b.checked;
        });
        document.querySelectorAll('#adComps [data-ad-comp-name]').forEach(b => {
            const c = s.components[+b.dataset.adCompName];
            if (c) c.name = b.value.trim();
        });
    },

    // the connection or the model changed: the check, the parts it found and
    // the nameplate values it filled are no longer true (a typed value stays);
    // a check still reading is stopped and its job cancelled (review M1, M2)
    _adCheckReset() {
        const ad = this._ad;
        if (!ad) return;
        if (ad.pvRun) { const old = ad.pvRun; ad.pvRun = null; old.stop = true; this._pvCancel(old.job); }
        const r = window.AddDevModel.checkReset(ad.s);
        ad.s.check = r.check;
        ad.s.components = r.components;
        ad.s.identity = r.identity;
        ad.s.plateFrom = r.plateFrom;
    },

    // Change model: back to What, the connection kept
    _adChangeModel() {
        const ad = this._ad;
        this._adCollect();
        if (ad.s.path === 'connection') return this._adShow('what');
        this._adShow('what');
    },

    // the device answers as another model: that one, back on What (the
    // connection kept), picked
    _adUseSuggested(id) {
        const ad = this._ad;
        if (!ad.templates.some(x => x.id === id)) return;
        this._adCollect();
        ad.s.template = id;
        this._adCheckReset();
        ad.gen++;
        this._adShow('what');
        this._adPickTemplate(id, true);
        ad.shell.say(this.t('adddev.chk.switched', '{name} picked: the connection is kept. Next checks again.', { name: (this._adTpl() || {}).name || id }));
    },

    // ── Step 4: Name & save ──────────────────────────────────────────────
    _adDefaultName() {
        const s = this._ad.s, tpl = this._adTpl();
        const base = (tpl && (tpl.name || tpl.id)) || '';
        const M = s.conn && s.conn.M;
        const unit = M && (['tcp', 'rtu', 'rtu-tcp', 'rtu_tap', 'seplos_v2'].includes(M.mode)) ? M.unit_id : null;
        if (!base) return '';
        return unit === null || unit === undefined ? base
            : `${base} · ${this.t(M.mode === 'seplos_v2' ? 'adddev.packWord' : 'adddev.unitWord', M.mode === 'seplos_v2' ? 'pack' : 'unit')} ${unit}`;
    },

    // the id a new device is proposed: from the name typed, else short —
    // the model and its address ("sdm630-3"), never cut where it differs
    _adDefaultId() {
        const s = this._ad.s, M = window.AddDevModel, tpl = this._adTpl() || {};
        const taken = (this._devices || []).map(d => d.id);
        if (s.nameTouched && String(s.name || '').trim()) return M.uniqueId(M.idFromName(s.name), taken);
        const Mx = s.conn && s.conn.M;
        const unit = Mx && ['tcp', 'rtu', 'rtu-tcp', 'rtu_tap', 'seplos_v2'].includes(Mx.mode) ? Mx.unit_id : null;
        const base = M.idFromName(tpl.model || tpl.name || tpl.id || s.name).slice(0, 24).replace(/-+$/, '');
        return M.uniqueId(M.idFromName(unit === null || unit === undefined ? base : `${base} ${unit}`) || 'device', taken);
    },

    _adTypeOptions(cur, tplKind) {
        const t = (k, d, p) => this.t(k, d, p);
        const cat = this._deviceTypes || {};
        const ids = [...(this._KIND_ORDER || []), ...((cat.types || []).filter(x => !x.builtin).map(x => x.id))];
        const uniq = [...new Set(ids)];
        return uniq.map(k => `<option value="${this._esc(k)}" ${k === cur ? 'selected' : ''}>${this._esc(this._kindLabel(k))}${k === tplKind
            ? ` — ${this._esc(t('adddev.fromModel', 'from the model'))}` : ''}</option>`).join('');
    },

    _adSaveFullHtml() {
        const ad = this._ad, s = ad.s, t = (k, d, p) => this.t(k, d, p);
        const tpl = this._adTpl() || {};
        const tplKind = tpl.device_kind || tpl.kind || 'other';
        if (!s.nameTouched) s.name = this._adDefaultName();
        if (!s.idTouched) s.id = this._adDefaultId();
        const typeId = s.typeId || tplKind;
        const id = s.identity;
        const pf = s.plateFrom || {};
        const fromWord = k => pf[k] ? ` <span class="field-hint ad-from">${this._esc(t('adddev.plate.from', 'read from the device ({src})', { src: pf[k] === 'sunspec' ? 'SunSpec' : 'FC43' }))}</span>` : '';
        const txt = (k, label, opts = {}) => `<div class="form-group ${opts.cls || ''}"><label class="form-label" for="adI-${k}">${this._esc(label)}${fromWord(k)}</label>
            ${opts.area ? `<textarea id="adI-${k}" class="input" rows="2" maxlength="500" data-ad-ident="${k}">${this._esc(id[k] || '')}</textarea>`
                : `<input type="${opts.type || 'text'}" id="adI-${k}" class="input" data-ad-ident="${k}" value="${this._esc(id[k] || '')}" ${opts.max ? `maxlength="${opts.max}"` : ''}
                    ${opts.hint ? `aria-describedby="adI-${k}-hint"` : ''} ${opts.ph ? `placeholder="${this._esc(opts.ph)}"` : ''} spellcheck="false">`}
            ${opts.hint ? `<div class="field-hint" id="adI-${k}-hint">${this._esc(opts.hint)}</div>` : ''}</div>`;
        const plateOpen = Object.keys(pf).length > 0 || ['manufacturer', 'model', 'serial_number', 'firmware', 'installed_on'].some(k => String(id[k] || '').trim());
        const recCats = this._adRecommended(typeId);
        const o = s.out;
        const prefixPh = this._adPattern(ad.cfg?.topicPattern, s.id || 'device-id');
        const bucketPh = this._adPattern(ad.cfg?.bucketPattern, s.id || 'device-id');
        const hb = ad.cfg?.heartbeat;
        const su = (tpl.setup || {});
        return `<div id="adSaveForm" class="ad-save">
            <div class="form-row">
                <div class="form-group flex-2">
                    <label class="form-label" for="adName">${t('devices.wizard.name', 'Device name')}</label>
                    <input type="text" id="adName" class="input" autocomplete="off" maxlength="80" value="${this._esc(s.name)}">
                </div>
                <div class="form-group">
                    <label class="form-label" for="adId">${t('devices.wizard.id', 'Device id')}</label>
                    <input type="text" id="adId" class="input" autocomplete="off" spellcheck="false" value="${this._esc(s.id)}"
                        pattern="[a-z0-9][a-z0-9_-]{1,63}" aria-describedby="adIdHint">
                    <div class="field-hint" id="adIdHint">${t('adddev.idHint', 'a-z 0-9 - _ · fixed after creation: it names topics and history')}</div>
                </div>
            </div>
            <div class="form-group">
                <label class="form-label" for="adType">${t('adddev.fact.type', 'Type')}</label>
                <select id="adType" class="input" aria-describedby="adTypeHint">${this._adTypeOptions(typeId, tplKind)}</select>
                <div class="field-hint" id="adTypeHint">${this._esc(t('kind.setMsg', 'How the device is filed and found. It changes no reading, topic or history.'))}</div>
                ${this._typeFormHtml('ads', {})}
            </div>
            <div class="form-row ad-where">
                ${txt('location', t('attr.identity.location', 'Where it is (e.g. garage, cabinet 2)'), { max: 120 })}
                ${txt('labels', t('attr.identity.labels', 'Labels'), { hint: t('attr.hint.labels', 'Separated by commas: a-z 0-9 - _, at most {n}', { n: 16 }), ph: 'hvac, floor-1' })}
            </div>
            ${txt('description', t('attr.identity.description', 'Description'), { area: true })}
            <details class="ad-fold" id="adPlate" ${plateOpen ? 'open' : ''}><summary>${this._esc(t('adddev.plate', 'Nameplate'))}${Object.keys(pf).length
                ? ` <span class="field-hint">${this._esc(t('adddev.plate.read', '— pre-filled from what the check read'))}</span>` : ''}</summary>
                <div class="form-row ad-plate">
                    ${txt('manufacturer', t('attr.identity.manufacturer', 'Manufacturer'), { max: 64, ph: tpl.vendor || '' })}
                    ${txt('model', t('attr.identity.model', 'Model'), { max: 64, ph: tpl.model || '' })}
                    ${txt('serial_number', t('attr.identity.serial_number', 'Serial number'), { max: 64, hint: t('attr.hint.private', 'Shown to signed-in users; published on MQTT only when the settings allow it') })}
                    ${txt('firmware', t('attr.identity.firmware', 'Firmware'), { max: 64 })}
                    ${txt('installed_on', t('attr.identity.installed_on', 'Installed on'), { type: 'date' })}
                </div></details>
            ${recCats.length ? `<details class="ad-fold" id="adRec"><summary>${this._esc(t('adddev.rec', 'Recommended for {type}', { type: this._kindLabel(typeId) }))}
                <span class="field-hint">${this._esc(t('adddev.rec.optional', '— optional'))}</span></summary>
                <p class="field-hint">${this._esc(t('adddev.rec.lead', 'Site totals and checks use them; never required. More on its About tab later.'))}</p>
                ${recCats.map(c => this._adRecHtml(c)).join('')}</details>` : ''}
            <fieldset class="cf-fieldset ad-outs" id="adOuts"><legend class="wiz-eyebrow">${this._esc(t('adddev.outs', 'Outputs'))}</legend>
                <div class="ad-out-chips">
                    <details class="ad-out" id="adOutMqtt"><summary><span class="ad-out-st ${o.mqtt ? 'is-on' : ''}" id="adOutMqttSt">MQTT · ${this._esc(o.mqtt ? (o.prefix || prefixPh) : t('adddev.off', 'off'))}</span></summary>
                        <label class="checkbox-label"><input type="checkbox" id="adOutMqttOn" ${o.mqtt ? 'checked' : ''}> <span>${this._esc(t('adddev.out.mqttOn', 'Publish its values on MQTT'))}</span></label>
                        <div class="form-group"><label class="form-label" for="adOutPrefix">${t('devices.wizard.topicPrefix', 'MQTT topic prefix')}</label>
                            <input type="text" id="adOutPrefix" class="input" spellcheck="false" value="${this._esc(o.prefix)}" placeholder="${this._esc(prefixPh)}" aria-describedby="adOutPrefixHint">
                            <div class="field-hint" id="adOutPrefixHint">${this._esc(t('adddev.out.prefixHint', 'Empty: the gateway\'s pattern (Settings → MQTT). Fixed after creation.'))}</div></div></details>
                    <details class="ad-out" id="adOutInflux"><summary><span class="ad-out-st ${o.influx ? 'is-on' : ''}" id="adOutInfluxSt">InfluxDB · ${this._esc(o.influx ? (o.bucket || bucketPh) : t('adddev.off', 'off'))}</span></summary>
                        <label class="checkbox-label"><input type="checkbox" id="adOutInfluxOn" ${o.influx ? 'checked' : ''}> <span>${this._esc(t('adddev.out.influxOn', 'Write its values to InfluxDB'))}</span></label>
                        <div class="form-row">
                            <div class="form-group"><label class="form-label" for="adOutBucket">${t('devices.wizard.bucket', 'InfluxDB bucket')}</label>
                                <input type="text" id="adOutBucket" class="input" spellcheck="false" value="${this._esc(o.bucket)}" placeholder="${this._esc(bucketPh)}"></div>
                            <div class="form-group"><label class="form-label" for="adOutTag">${t('devices.wizard.deviceTag', 'Influx device tag')}</label>
                                <input type="text" id="adOutTag" class="input" spellcheck="false" value="${this._esc(o.tag)}" placeholder="${this._esc(s.id || 'id')}"></div>
                        </div>
                        <div class="field-hint">${this._esc(t('adddev.out.influxHint', 'A missing bucket is created (90 days). Heartbeat — an unchanged value written again every: gateway {s}.',
                            { s: hb === null || hb === undefined ? '—' : (+hb ? `${hb} s` : t('adddev.off', 'off')) }))}</div></details>
                    <details class="ad-out" id="adOutHa"><summary><span class="ad-out-st ${o.mqtt && o.ha ? 'is-on' : ''}" id="adOutHaSt">Home Assistant · ${this._esc(o.mqtt && o.ha ? t('adddev.on', 'on') : t('adddev.off', 'off'))}</span></summary>
                        <label class="checkbox-label"><input type="checkbox" id="adOutHaOn" ${o.ha ? 'checked' : ''} ${o.mqtt ? '' : 'aria-disabled="true"'} aria-describedby="adOutHaWhy">
                            <span>${this._esc(t('devices.wizard.haDiscovery', 'Publish Home Assistant MQTT discovery for this device'))}</span></label>
                        <div class="field-hint" id="adOutHaWhy">${this._esc(o.mqtt ? '' : t('adddev.out.haNeedsMqtt', 'Home Assistant reads it over MQTT: turn MQTT on first.'))}</div></details>
                    <span class="ad-out ad-out-fixed" id="adOutOpcua"><span class="ad-out-st">OPC UA · ${this._esc(t('adddev.off', 'off'))}</span>
                        <span class="field-hint">${this._esc(t('adddev.out.opcua', 'A new device is not served on OPC UA until it is turned on from its ⋯ → Outputs….'))}</span></span>
                </div>
            </fieldset>
            <div class="ad-writes" id="adWrites"><i aria-hidden="true" class="bi bi-lock"></i>
                <div><b>${this._esc(t('adddev.writes', 'Writes'))}</b>
                <p>${this._esc(t('adddev.writes.locked', 'Created write-locked: nothing is written to the device until an admin unlocks it from its ⋯ → Unlock writes….'))}</p>
                ${su.count ? `<p>${this._esc(this.tn('adddev.writes.setup', su.count, 'Its {n} setup write(s) wait for that unlock too: the values they map stay empty until then.'))}</p>` : ''}</div></div>
            <label class="checkbox-label ad-enabled"><input type="checkbox" id="adEnabled" ${s.enabled ? 'checked' : ''}>
                <span>${this._esc(t('adddev.enabled', 'Start reading now'))}</span></label>
            <section class="ad-review" id="adReview" aria-labelledby="adReviewH" aria-busy="true">
                <h5 id="adReviewH">${this._esc(t('adddev.review', 'What will be created'))}</h5>
                <div id="adReviewBody"><p class="field-hint">${this._esc(t('adddev.review.asking', 'Asking the gateway…'))}</p></div>
                <p class="field-hint ad-refuse" id="adRefuse" role="status"></p>
            </section>
            <div class="ad-save-more"><button type="button" class="btn btn-secondary btn-sm" id="adCreateAnother" aria-describedby="adCopyList">
                <i aria-hidden="true" class="bi bi-plus-square"></i> ${this._esc(t('adddev.createAnother', 'Create and add another like this'))}</button>
                <details class="ad-copylist"><summary>${this._esc(t('adddev.copyWhat', 'What is copied'))}</summary>
                <p class="field-hint" id="adCopyList">${this._esc(t('adddev.copyList', 'Copied: the model, its type, the connection (the same bus, its line and role), the outputs, Start reading now, where it is, labels, manufacturer and model, recommended attributes. Not copied: name and id (the next ones are proposed), the address (the next free on the bus), description, serial, firmware, installed on, a meter\'s grid connection, components. Never unlocked.'))}</p></details></div>
        </div>`;
    },

    // the recommended categories for a type (its built-in's for a user type)
    _adRecommended(typeId) {
        const cat = this._deviceTypes || {};
        const ty = (cat.types || []).find(x => x.id === typeId);
        const cats = (ty && ty.recommended_attributes) || [];
        const known = ((this._attrCatalog || {}).categories) || {};
        return cats.filter(c => known[c]);
    },

    _adRecHtml(cat) {
        const s = this._ad.s;
        const spec = (((this._attrCatalog || {}).categories) || {})[cat] || {};
        const vals = (s.recommended || {})[cat] || {};
        const field = (fid, f) => {
            const id = `adRec-${cat}-${fid}`;
            const label = this._esc(this._attrLabel ? this._attrLabel(cat, fid, f.label || fid) : (f.label || fid));
            const v = vals[fid] ?? '';
            const input = f.type === 'choice'
                ? `<select class="input input-sm" id="${id}" data-ad-rec="${cat}" data-ad-recf="${fid}"><option value=""></option>${(f.choices || []).map(c =>
                    `<option value="${this._esc(c)}" ${String(v) === String(c) ? 'selected' : ''}>${this._esc(this.t(`attr.${cat}.${fid}.${c}`, String(c)))}</option>`).join('')}</select>`
                : `<input class="input input-sm" id="${id}" type="text" ${['number', 'integer'].includes(f.type) ? 'inputmode="decimal"' : ''} data-ad-rec="${cat}" data-ad-recf="${fid}" value="${this._esc(v)}">`;
            return `<div class="form-group"><label class="form-label" for="${id}">${label}${f.unit ? ` <span class="field-hint">(${this._esc(f.unit)})</span>` : ''}</label>${input}</div>`;
        };
        return `<fieldset class="cf-fieldset ad-rec" data-ad-reccat="${this._esc(cat)}"><legend class="form-label">${this._esc(this._attrLabel ? this._attrLabel(cat, '', spec.label || cat) : (spec.label || cat))}</legend>
            <div class="form-row ad-rec-row">${Object.entries(spec.fields || {}).map(([fid, f]) => field(fid, f)).join('')}</div></fieldset>`;
    },

    _adWireSave(host) {
        const ad = this._ad;
        this._typeFormWire('ads', ty => {
            ad.s.typeId = ty.id;
            ad.s.typeTouched = true;
            const sel = document.getElementById('adType');
            if (sel) {
                const tpl = this._adTpl() || {};
                sel.innerHTML = this._adTypeOptions(ty.id, tpl.device_kind || tpl.kind || 'other');
                sel.focus();
            }
            this._adDrySoon();
        });
        void host;
    },

    // what Name & save holds, into the state
    _adSaveCollect() {
        const s = this._ad.s;
        const g = id => document.getElementById(id);
        if (g('adType')) {
            const tpl = this._adTpl() || {};
            const tk = tpl.device_kind || tpl.kind || 'other';
            const v = g('adType').value;
            s.typeId = v === tk && !s.typeTouched ? '' : v;
        }
        document.querySelectorAll('#adSaveForm [data-ad-ident]').forEach(el => { s.identity[el.dataset.adIdent] = el.value; });
        const rec = {};
        document.querySelectorAll('#adSaveForm [data-ad-rec]').forEach(el => {
            (rec[el.dataset.adRec] = rec[el.dataset.adRec] || {})[el.dataset.adRecf] = el.value;
        });
        // another type's categories stay in the state (a type changed back
        // finds them); only the current type's are sent (_adPayload)
        for (const [c, v] of Object.entries(rec)) s.recommended[c] = v;
        const o = s.out;
        if (g('adOutMqttOn')) o.mqtt = g('adOutMqttOn').checked;
        if (g('adOutInfluxOn')) o.influx = g('adOutInfluxOn').checked;
        if (g('adOutHaOn')) o.ha = g('adOutHaOn').checked;
        if (g('adOutPrefix')) o.prefix = g('adOutPrefix').value.trim();
        if (g('adOutBucket')) o.bucket = g('adOutBucket').value.trim();
        if (g('adOutTag')) o.tag = g('adOutTag').value.trim();
        if (g('adEnabled')) s.enabled = g('adEnabled').checked;
    },

    // a field of Name & save changed: the id follows the name (until typed),
    // the chips say their state, the gateway is asked again (debounced)
    _adSaveChanged(el) {
        const ad = this._ad, s = ad.s, M = window.AddDevModel;
        const g = id => document.getElementById(id);
        const t = (k, d, p) => this.t(k, d, p);
        if (el.id === 'adName') {
            s.nameTouched = true;
            if (!s.idTouched) {
                s.name = el.value.trim();
                s.id = this._adDefaultId();
                if (g('adId')) g('adId').value = s.id;
            }
        }
        if (el.id === 'adId') { s.idTouched = true; s.id = el.value.trim(); }
        if (el.dataset && el.dataset.adIdent && (s.plateFrom || {})[el.dataset.adIdent]) {
            ad.s = Object.assign(s, { plateFrom: M.plateTyped(s, el.dataset.adIdent).plateFrom });
            el.closest('.form-group')?.querySelector('.ad-from')?.remove();
        }
        if (el.id === 'adType') {
            s.typeTouched = true;
            this._adSaveCollect();
            // the recommended attributes follow the type
            const box = g('adRec');
            const recCats = this._adRecommended(el.value);
            const html = recCats.length ? `<details class="ad-fold" id="adRec"><summary>${this._esc(t('adddev.rec', 'Recommended for {type}', { type: this._kindLabel(el.value) }))}
                <span class="field-hint">${this._esc(t('adddev.rec.optional', '— optional'))}</span></summary>
                <p class="field-hint">${this._esc(t('adddev.rec.lead', 'Site totals and checks use them; never required. More on its About tab later.'))}</p>
                ${recCats.map(c => this._adRecHtml(c)).join('')}</details>` : '';
            if (box) box.outerHTML = html || '<span id="adRec" hidden></span>';
            else g('adOuts')?.insertAdjacentHTML('beforebegin', html);
        }
        if (el.id === 'adOutHaOn' && !g('adOutMqttOn')?.checked) {
            el.checked = false;
            ad.shell.say(t('adddev.out.haNeedsMqtt', 'Home Assistant reads it over MQTT: turn MQTT on first.'));
        }
        this._adSaveCollect();
        this._adOutsDraw();
        this._adDrySoon();
    },

    _adOutsDraw() {
        const ad = this._ad, s = ad.s, o = s.out, t = (k, d, p) => this.t(k, d, p);
        const g = id => document.getElementById(id);
        const did = s.id || 'device-id';
        const set = (id, on, text) => { const e = g(id); if (e) { e.textContent = text; e.classList.toggle('is-on', !!on); } };
        set('adOutMqttSt', o.mqtt, `MQTT · ${o.mqtt ? (o.prefix || this._adPattern(ad.cfg?.topicPattern, did)) : t('adddev.off', 'off')}`);
        set('adOutInfluxSt', o.influx, `InfluxDB · ${o.influx ? (o.bucket || this._adPattern(ad.cfg?.bucketPattern, did)) : t('adddev.off', 'off')}`);
        set('adOutHaSt', o.mqtt && o.ha, `Home Assistant · ${o.mqtt && o.ha ? t('adddev.on', 'on') : t('adddev.off', 'off')}`);
        const ha = g('adOutHaOn');
        if (ha) {
            if (o.mqtt) ha.removeAttribute('aria-disabled'); else ha.setAttribute('aria-disabled', 'true');
            const why = g('adOutHaWhy');
            if (why) why.textContent = o.mqtt ? '' : t('adddev.out.haNeedsMqtt', 'Home Assistant reads it over MQTT: turn MQTT on first.');
        }
        if (g('adOutPrefix')) g('adOutPrefix').placeholder = this._adPattern(ad.cfg?.topicPattern, did);
        if (g('adOutBucket')) g('adOutBucket').placeholder = this._adPattern(ad.cfg?.bucketPattern, did);
        if (g('adOutTag')) g('adOutTag').placeholder = did;
    },

    // the create as it would be sent (the one builder, adddev-model.js)
    _adPayload() {
        const ad = this._ad, M = window.AddDevModel;
        const s = ad.s;
        if (!s.nameTouched && !s.name) s.name = this._adDefaultName();
        if (!s.id) s.id = this._adDefaultId();
        const tpl = this._adTpl() || {};
        const typeId = s.typeId || tpl.device_kind || tpl.kind || 'other';
        return M.createPayload(M.draftOf(s, { connection: this._adConnection(), tpl, catalog: this._attrCatalog,
                                               recommendedCats: this._adRecommended(typeId) }));
    },

    _adDrySoon(ms = 400) {
        const ad = this._ad;
        ad.dryDue = true;
        clearTimeout(ad.dryTimer);
        ad.dryTimer = setTimeout(() => this._adDryRun(), ms);
    },

    // the full dry run: the review strip from its would-be entry, or its
    // refusal at the fields (Create aria-disabled, the reason beside it)
    async _adDryRun() {
        const ad = this._ad, M = window.AddDevModel, t = (k, d, p) => this.t(k, d, p);
        if (!ad || !ad.open || ad.shell.current !== 'save' || ad.s.path === 'measured' || ad.result) return;
        ad.dryDue = false;
        this._adCollect();
        const payload = this._adPayload();
        const seq = (ad.drySeq = (ad.drySeq || 0) + 1);
        let r, res, done;
        ad.dryBusy = new Promise(res2 => { done = res2; });
        try {
            r = await fetch('/api/devices?dry_run=true', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
            res = await r.json().catch(() => ({}));
        } catch (e) {
            r = null;
        } finally {
            ad.dryBusy = null;
            setTimeout(done, 0);                              // after this answer is placed
        }
        if (this._ad !== ad || ad.drySeq !== seq || ad.shell.current !== 'save') return;
        const review = document.getElementById('adReview');
        const body = document.getElementById('adReviewBody');
        const say = document.getElementById('adRefuse');
        review?.removeAttribute('aria-busy');
        if (!r) {
            ad.dry = { refused: false, entry: null };
            if (body) body.innerHTML = `<p class="field-hint">${this._esc(t('adddev.review.unreachable', 'The gateway did not answer: Create will say what it thinks.'))}</p>`;
            this._adFooter();
            return;
        }
        if (r.ok) {
            ad.dry = { refused: false, entry: res.entry || null };
            if (say) say.textContent = '';
            this._placeFieldErrors(document.getElementById('adSaveForm'), {}, () => null, { focus: false });
            ad.shell.showErrors([]);
            if (body) body.innerHTML = this._adReviewHtml(res.entry || {});
            this._adFooter();
            return;
        }
        const det = res.detail && typeof res.detail === 'object' ? res.detail : { errors: [typeof res.detail === 'string' ? res.detail : this._respError(r, res)] };
        const fields = det.fields || {};
        const keys = Object.keys(fields);
        const errs = (det.errors || []).map(String);
        const other = errs.filter(e => !keys.some(k => e.startsWith(k + ': ')));
        const why = (keys.length ? `${keys[0]}: ${fields[keys[0]]}` : other[0]) || t('adddev.review.refused', 'The gateway refuses it as it is.');
        ad.dry = { refused: true, why: t('adddev.review.why', 'Create waits: {why}', { why }), fields, other };
        if (say) say.textContent = ad.dry.why;
        if (body) body.innerHTML = `<p class="field-hint">${this._esc(t('adddev.review.none', 'Nothing to review until the gateway accepts it.'))}</p>`;
        // each at its field on this page; the others listed with a link to their step
        const mine = Object.fromEntries(keys.filter(k => M.stepOfField(k, ad.s.path) === 'save').map(k => [k, fields[k]]));
        // a refused field is never left inside a closed chip or fold
        for (const k of Object.keys(mine)) {
            const d = document.getElementById(this._adFieldId(k) || '')?.closest('details');
            if (d) d.open = true;
        }
        const left = this._placeFieldErrors(document.getElementById('adSaveForm'), mine, p => this._adFieldId(p), { focus: false });
        ad.shell.showErrors([...keys.filter(k => M.stepOfField(k, ad.s.path) !== 'save').map(k => ({ section: M.stepOfField(k, ad.s.path), text: `${k}: ${fields[k]}`,
            fieldId: this._adFieldId(k) })), ...[...left.filter(x => !keys.some(k => x.startsWith(k + ': '))), ...other].map(text => ({ text }))]
            .concat(Object.keys(mine).map(k => ({ section: 'save', text: fields[k], fieldId: this._adFieldId(k) }))));
        this._adFooter();
    },

    _adReviewHtml(entry) {
        const ad = this._ad, t = (k, d, p) => this.t(k, d, p);
        const tpl = this._adTpl() || {};
        // the type as the list will file it: the one chosen, else the model's
        const shown = Object.assign({}, entry, { type: this._kindLabel(entry.type || tpl.device_kind || tpl.kind || 'other') });
        const groups = window.AddDevModel.reviewStrip(shown, t, { mqtt: ad.s.out.mqtt, influx: ad.s.out.influx, ha: ad.s.out.ha });
        return `<dl class="ad-strip">${groups.map(g => `<div class="ad-strip-g" data-layer="${this._esc(g.id)}">
            <dt>${this._esc(g.label)} <button type="button" class="btn btn-ghost btn-sm ad-change" data-ad-goto="${this._esc(g.step)}"
                aria-label="${this._esc(t('adddev.changeOf', 'Change: {what}', { what: g.label }))}">${this._esc(t('adddev.change', 'Change'))}</button></dt>
            ${g.rows.map(r => `<dd><span class="ad-strip-k">${this._esc(r.label)}</span> <span class="ad-strip-v" data-k="${this._esc(r.k)}">${this._esc(r.v || '—')}</span></dd>`).join('')}
        </div>`).join('')}</dl>`;
    },

    // a server path → its field in the dialog
    _adFieldId(p) {
        const ad = this._ad;
        const save = { id: 'adId', name: 'adName', type: 'adType', kind: 'adType', 'mqtt.topic_prefix': 'adOutPrefix',
                       'influxdb.bucket': 'adOutBucket', 'influxdb.device_tag': 'adOutTag', template: 'adQ' };
        if (ad.s.path === 'measured') {
            if (p === 'kind') return 'adKinds';
            if (p === 'id') return 'adId';
            if (p === 'name') return 'adName';
            if (p === 'meters') return 'adMtrDev-0';
            return this._mtrFieldId('adMtr', p);
        }
        if (save[p]) return save[p];
        if (String(p).startsWith('connection') && ad.cs) return ad.cs.fieldId(p);
        const m = /^attributes(?:\.identity)?\.?(\w+)?/.exec(String(p));
        if (m && m[1] && document.getElementById(`adI-${m[1]}`)) return `adI-${m[1]}`;
        const c = /^components\[(\d+)\]/.exec(String(p));
        if (c) return `adCompName-${c[1]}`;
        return null;
    },

    // a refusal of Create: each at its field, on the step that owns it
    _adRefused(fields, other) {
        const ad = this._ad, M = window.AddDevModel;
        const keys = Object.keys(fields || {});
        const own = keys.length ? M.stepOfField(keys[0], ad.s.path) : ad.s.step;
        if (own === 'connection' && ad.s.path !== 'measured') {
            const conn = Object.fromEntries(keys.filter(k => M.stepOfField(k, ad.s.path) === 'connection').map(k => [k, fields[k]]));
            return this._adConnRefused(conn, [...keys.filter(k => !(k in conn)).map(k => `${k}: ${fields[k]}`), ...(other || [])]);
        }
        if (own !== ad.s.step) this._adShow(own, false);
        const host = ad.shell.panel(own);
        const mine = Object.fromEntries(keys.filter(k => M.stepOfField(k, ad.s.path) === own).map(k => [k, fields[k]]));
        const left = this._placeFieldErrors(host, mine, p => this._adFieldId(p), { focus: false });
        const items = [
            ...keys.map(k => ({ section: M.stepOfField(k, ad.s.path), text: fields[k], fieldId: this._adFieldId(k) })),
            ...[...left.filter(x => !keys.some(k => x.startsWith(k + ': '))), ...(other || [])].map(x => ({ text: x })),
        ];
        ad.shell.showErrors(items);
        const first = keys.map(k => document.getElementById(this._adFieldId(k) || '')).find(el => el && host.contains(el));
        (first || ad.shell.el.errors.querySelector('a') || ad.shell.heading(own))?.focus();
    },

    async _adCreate(another) {
        const ad = this._ad, M = window.AddDevModel;
        const t = (k, d, p) => this.t(k, d, p);
        // one create at a time; none after the result (review m3)
        if (!ad || ad.busy || ad.result || ad.creating) return;
        ad.creating = true;
        try {
            return await this._adCreateNow(another);
        } finally {
            ad.creating = false;
        }
    },

    async _adCreateNow(another) {
        const ad = this._ad, M = window.AddDevModel;
        const t = (k, d, p) => this.t(k, d, p);
        this._adCollect();
        // a dry run still due (a field just typed) answers before Create is
        // judged; one already sent is waited for (review m4)
        clearTimeout(ad.dryTimer);
        if (ad.dryDue || ad.dryBusy) await (ad.dryBusy || this._adDryRun());
        const s = ad.s;
        if (ad.dry && ad.dry.refused) {
            ad.shell.say(ad.dry.why);
            const first = Object.keys(ad.dry.fields || {}).map(k => document.getElementById(this._adFieldId(k) || '')).find(Boolean);
            (first || ad.shell.el.errors.querySelector('a') || document.getElementById('adRefuse'))?.focus();
            return;
        }
        const payload = this._adPayload();
        if (!M.idValid(payload.id)) {
            this._adRefused({ id: t('devices.wizard.idInvalid', 'Use a-z 0-9 - _ (2-64 chars).') });
            return;
        }
        // how its Check ended, for the create's audit line (L3.9-F3)
        const c = s.check || {};
        payload.check = M.checkPassed(c) ? 'ok' : (['skipped', 'idle'].includes(c.state) ? 'skipped' : 'failed');
        if (c.job) payload.check_job = c.job;
        ad.drySeq = (ad.drySeq || 0) + 1;                 // a dry run's late answer is not this create's
        let r, res;
        ad.busy = true;
        ad.shell.el.next.setAttribute('aria-busy', 'true');
        try {
            r = await fetch('/api/devices', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
            res = await r.json().catch(() => ({}));
        } catch (e) {
            r = null;
            res = { detail: String(e.message || e) };
        } finally {
            ad.busy = false;
            ad.shell.el.next.removeAttribute('aria-busy');
        }
        // the answer lost (the network, a 5xx): the device may still have been
        // made — asked, and said, never created twice (review m3)
        if (!r || r.status >= 500) {
            let made = false;
            try { made = (await fetch(`/api/devices/${encodeURIComponent(payload.id)}`)).ok; } catch (e) { made = false; }
            if (!made) {
                ad.shell.showErrors([{ text: r ? this._respError(r, res) : String(res.detail) }]);
                return;
            }
            ad.shell.say(t('adddev.lostAnswer', 'The answer was lost, but {id} was created.', { id: payload.id }));
            r = { ok: true };
            res = res && res.device ? res : {};
        }
        if (!r.ok) {
            const det = res.detail && typeof res.detail === 'object' ? res.detail : {};
            const fields = det.fields || {};
            const keys = Object.keys(fields);
            const errs = det.errors || [typeof res.detail === 'string' ? res.detail : this._respError(r, res)];
            this._adRefused(fields, errs.filter(e => !keys.some(k => String(e).startsWith(k + ': '))));
            return;
        }
        const dev = res.device || {};
        const created = { id: payload.id, name: payload.name, enabled: payload.enabled, unit: (payload.connection || {}).unit_id,
                          components: (res.components || []).length };
        this._fetchDevices(true).catch(() => null);
        if (this.renderDevicesList) this.renderDevicesList().catch?.(() => {});
        this.showToast('success', t('devices.created', 'Device created'),
            `${payload.name} → MQTT ${dev.mqtt_topic_prefix || payload.mqtt.topic_prefix || this._adPattern(ad.cfg?.topicPattern, payload.id)}/…`);
        if (another) {
            ad.lastCreated = created;
            return this._adAnother(true);
        }
        this._adResult(created);
    },

    // after Create: what was made, and what comes next
    _adResult(created) {
        const ad = this._ad, M = window.AddDevModel, t = (k, d, p) => this.t(k, d, p);
        ad.lastCreated = created;
        const tpl = this._adTpl() || {};
        const used = ((((this._csCache || {}).buses || []).find(b => b.id === (ad.s.conn && ad.s.conn.cs && ad.s.conn.cs.busId)) || {}).units_in_use || [])
            .map(u => +u.unit);
        const unitful = created.unit !== undefined && created.unit !== null;
        const next = unitful ? M.nextFreeUnit([...used, +created.unit], Number.isInteger(tpl.default_unit_id) ? tpl.default_unit_id : +created.unit) : null;
        const state = [t('adddev.res.created', 'Created'), created.enabled ? t('adddev.res.reading', 'reading') : t('adddev.res.off', 'not reading (off)'),
                       t('adddev.res.locked', 'write-locked')].join(' · ');
        ad.result = { line: `${created.name} — ${state}`, created, next };
        ad.shell.setSteps(this._adSteps());
        const host = ad.shell.panel('save');
        host.dataset.adKey = 'result';
        host.innerHTML = `<div class="ad-result" id="adResult">
            <p class="ad-result-h"><i aria-hidden="true" class="bi bi-check-circle"></i> <b>${this._esc(created.name)}</b> <code>${this._esc(created.id)}</code></p>
            <p class="ad-result-st" id="adResultState">${this._esc(state)}${created.components ? ` · ${this._esc(this.tn('adddev.res.comps', created.components, 'with {n} part(s)'))}` : ''}</p>
            <div class="ad-result-acts">
                <button type="button" class="btn btn-primary btn-sm" id="adResOpen"><i aria-hidden="true" class="bi bi-box-arrow-up-right"></i> ${this._esc(t('adddev.res.open', 'Open it'))}</button>
                <button type="button" class="btn btn-secondary btn-sm" id="adResAnother"><i aria-hidden="true" class="bi bi-plus-square"></i> ${this._esc(next !== null
                    ? t('adddev.res.anotherUnit', 'Add another like this (unit {n})', { n: next }) : t('adddev.res.another', 'Add another like this'))}</button>
                <button type="button" class="btn btn-ghost btn-sm" id="adResDone">${this._esc(t('adddev.done', 'Done'))}</button>
            </div></div>`;
        ad.shell.showErrors([]);
        this._adFooter();
        ad.shell.say(ad.result.line);
        document.getElementById('adResOpen')?.focus();
        setTimeout(() => { if (this._ad === ad && ad.result) document.getElementById('adResOpen')?.focus(); }, 30);
    },

    async _adResultOpen() {
        const ad = this._ad;
        const id = ad.result?.created?.id;
        ad.done = true;
        this.closeModal('addDeviceModal');
        if (id) await this.openDeviceDetail(id);
    },

    // Create another like this: the copy list (adddev-model.js COPY_LIST), on
    // the connection step, the next free address taken there
    _adAnother(fromCreate) {
        const ad = this._ad, M = window.AddDevModel, t = (k, d, p) => this.t(k, d, p);
        const made = ad.lastCreated;
        const nx = M.another(ad.s);
        ad.s = nx;
        ad.result = null;
        ad.dry = null;
        ad.gen++;
        if (this._csCache) this._csCache.at = 0;           // the bus read again: the device just made is on it
        ad.shell.setSteps(this._adSteps());
        ['connection', 'check', 'save'].forEach(id => { const p = ad.shell.panel(id); if (p) p.dataset.adKey = ''; });
        this._adShow('connection');
        ad.shell.say(fromCreate && made
            ? t('adddev.anotherSaid', '{name} created. The next one: the same model and bus, the next free address.', { name: made.name })
            : t('adddev.anotherSaid2', 'The same model and bus, the next free address.'));
        // the address it took, focused once the bus is read
        setTimeout(() => document.getElementById('csAddUnit')?.focus(), 400);
    },

    // ⋯ Duplicate: a stored device through the same copy list
    async duplicateDevice(id) {
        const CM = window.ConnformModel, M = window.AddDevModel;
        let entry = this._devDetail?.id === id ? this._devDetail.entry : null;
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(id)}`);
            if (r.ok) { const j = await r.json(); entry = j.device || j; }
        } catch (e) { /* the page's copy */ }
        entry = entry || (this._devices || []).find(x => x.id === id);
        if (!entry) return;
        let attrs = {};
        try {
            const r = await fetch(`/api/attributes/devices/${encodeURIComponent(id)}`);
            if (r.ok) attrs = (await r.json()).attributes || {};
        } catch (e) { attrs = {}; }
        return this.openAddDevice({ state: M.fromEntry(entry, attrs, CM) });
    },

    // ── the steps of "many of the same" (L3.7) ───────────────────────────
    _adLaterHtml(step) {
        const s = this._ad.s, t = (k, d, p) => this.t(k, d, p);
        const first = window.AddDevModel.stepsOf(s.path)[0] === step && s.path !== 'model';
        return `${first ? `<p class="ad-back-link"><button type="button" class="btn btn-ghost btn-sm" data-ad-way="model">
                <i aria-hidden="true" class="bi bi-arrow-left"></i> ${t('adddev.backToModels', 'Choose a model instead')}</button></p>` : ''}
            <div class="ad-later"><p>${this._esc(t('adddev.later.many', 'Scanning one bus and adding several units in one go comes with the bus scan.'))}</p>
            <p class="field-hint">${this._esc(t('adddev.later.manyNow', 'Until then: add the first unit, then "Create and add another like this" — the next free address each time.'))}</p></div>`;
    },

    // ── the measured path ────────────────────────────────────────────────
    _adKindsHtml() {
        const s = this._ad.s, t = (k, d, p) => this.t(k, d, p);
        const k = window.AddDevModel.measuredKinds(this._meterRolesData, this._deviceTypes);
        const chip = kind => `<label class="seg-btn ${s.kind === kind ? 'on' : ''}"><input type="radio" name="adKind" value="${this._esc(kind)}" ${s.kind === kind ? 'checked' : ''}>
            <span class="s"></span> <i aria-hidden="true" class="bi ${this._KIND_ICON[kind] || this._KIND_ICON.other}"></i> ${this._esc(this._kindLabel(kind))}</label>`;
        return `<p class="ad-back-link"><button type="button" class="btn btn-ghost btn-sm" data-ad-way="model">
                <i aria-hidden="true" class="bi bi-arrow-left"></i> ${t('adddev.backToModels', 'Choose a model instead')}</button></p>
            <p class="field-hint" id="adKindsHint">${t('adddev.measuredLead', 'Equipment with no communication of its own: meters you already have measure it. Say what it is.')}</p>
            <div class="form-group" id="adKindGroup">
                <div class="seg wizmtr-kinds ad-kinds" role="radiogroup" id="adKinds" tabindex="-1"
                     aria-labelledby="addDevH-what" aria-describedby="adKindsHint">${[...k.first, ...k.more].map(chip).join('')}</div>
                ${this._typeFormHtml('ad', { exclude: ['meter'] })}
            </div>`;
    },

    _adMetersHtml() {
        const s = this._ad.s, t = (k, d, p) => this.t(k, d, p);
        return `<p class="field-hint">${t('wizMtr.metersHint', 'One meter on its supply (consumption), or one on what goes in and one on what comes out (input and output: its loss and efficiency follow).')}</p>
            <div data-mtr-wrap="adMtr">${this._metersRowsHtml('adMtr', s.meters || [], {})}</div>`;
    },

    // before Name & save: the meters said complete here, then the gateway
    // asked (a dry run stores nothing; a device of no connection reads no bus)
    async _adCheckMeters() {
        const ad = this._ad, M = window.AddDevModel;
        const t = (k, d) => this.t(k, d);
        const f = M.measuredErrors(ad.s, t);
        delete f.kind;
        if (Object.keys(f).length) { this._adRefused(f); return false; }
        let r, res;
        const body = M.measuredDryRunBody(ad.s);
        ad.busy = true;
        ad.shell.el.next.setAttribute('aria-busy', 'true');
        try {
            r = await fetch('/api/devices?dry_run=true', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            res = await r.json().catch(() => ({}));
        } catch (e) { return true; } finally {
            ad.busy = false;
            ad.shell.el.next.removeAttribute('aria-busy');
        }
        if (r.ok || r.status !== 422) return true;
        const det = res.detail || {};
        const fields = Object.fromEntries(Object.entries(det.fields || {}).filter(([k]) => k === 'kind' || k.startsWith('meters')));
        const keys = Object.keys(fields);
        // the id and the name are Name & save's to say
        const other = (det.errors || []).filter(e => !keys.some(k => String(e).startsWith(k + ': '))
            && !String(e).startsWith(`device '${body.id}'`) && !/^(id|name)\b/.test(String(e)));
        if (!keys.length && !other.length) return true;
        this._adRefused(fields, other);
        return false;
    },

    _adSaveHtml() {
        const s = this._ad.s, t = (k, d, p) => this.t(k, d, p);
        const dev = id => ((this._devices || []).find(x => x.id === id) || {}).name || id;
        const role = r => this._meterRoleLabel(((this._meterRolesData || {}).roles || []).find(x => x.id === r) || r);
        const meters = (s.meters || []).filter(m => m.device).map(m => `${dev(m.device)} (${role(m.role)})`).join(', ');
        return `<div class="form-row">
                <div class="form-group flex-2">
                    <label class="form-label" for="adName">${t('devices.wizard.name', 'Device name')}</label>
                    <input type="text" id="adName" class="input" autocomplete="off" value="${this._esc(s.name)}"
                        placeholder="${this._esc(t('adddev.namePh', 'Server rack UPS'))}">
                </div>
                <div class="form-group">
                    <label class="form-label" for="adId">${t('devices.wizard.id', 'Device id')}</label>
                    <input type="text" id="adId" class="input" autocomplete="off" spellcheck="false" value="${this._esc(s.id)}"
                        pattern="[a-z0-9][a-z0-9_-]{1,63}" aria-describedby="adIdHint" placeholder="ups-rack">
                    <div class="field-hint" id="adIdHint">${t('adddev.idHint', 'a-z 0-9 - _ · fixed after creation: it names topics and history')}</div>
                </div>
            </div>
            <ul class="ad-facts">
                <li>${t('adddev.fact.type', 'Type')}: <b>${this._esc(this._kindLabel(s.kind))}</b>
                    <button type="button" class="btn btn-ghost btn-sm" data-ad-goto="what">${t('adddev.changeType', 'Change the type')}</button></li>
                <li>${t('ddv.setup.measuredBy', 'Measured by')}: <b>${this._esc(meters || '—')}</b>
                    <button type="button" class="btn btn-ghost btn-sm" data-ad-goto="meters">${t('adddev.changeMeters', 'Change the meters')}</button></li>
            </ul>
            <p class="field-hint">${t('adddev.saveDefaults', 'Created with polling on and Home Assistant discovery on; topics and bucket from the gateway\'s patterns. Change them on the device page afterwards.')}</p>`;
    },

    async _adCreateMeasured() {
        const ad = this._ad, M = window.AddDevModel;
        const t = (k, d, p) => this.t(k, d, p);
        const s = this._adCollect();
        if (!s.id && s.name) s.id = M.idFromName(s.name);
        if (!M.idValid(s.id)) {
            this._adRefused({ id: t('devices.wizard.idInvalid', 'Use a-z 0-9 - _ (2-64 chars).') });
            return;
        }
        const payload = M.meterOnlyPayload(M.measuredDraft(s));
        let r, res;
        ad.busy = true;
        ad.shell.el.next.setAttribute('aria-busy', 'true');
        try {
            r = await fetch('/api/devices', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
            res = await r.json().catch(() => ({}));
        } catch (e) {
            ad.shell.showErrors([{ text: String(e.message || e) }]);
            return;
        } finally {
            ad.busy = false;
            ad.shell.el.next.removeAttribute('aria-busy');
        }
        if (!r.ok) {
            const det = res.detail || {};
            const fields = det.fields || {};
            const keys = Object.keys(fields);
            const errs = det.errors || [typeof det === 'string' ? det : this._respError(r, res)];
            this._adRefused(fields, errs.filter(e => !keys.some(k => String(e).startsWith(k + ': '))));
            return;
        }
        ad.done = true;
        this.closeModal('addDeviceModal');
        this.showToast('success', t('devices.created', 'Device created'),
            t('wizMtr.created', '{name}: measured by {list}', { name: payload.name,
                list: payload.meters.map(l => ((this._devices || []).find(x => x.id === l.device) || {}).name || l.device).join(', ') }));
        await this.openDeviceDetail(payload.id);
        this.ddvEdit('meters');
    },
});
