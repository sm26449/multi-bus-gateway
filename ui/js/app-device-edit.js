/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// The Edit device modal (ADR-0031 §4, L3.4; ADR-0032 §6, L3.11b: one way to
// edit, every kind) — augments JanitzaMonitor.prototype.
//
// Opened from the device page's ⋯ menu (Edit…, Edit connection…, Polling…,
// Type…, Outputs…, Rename…) or the E key, at the section in view; never from
// the page body. One frame (ui/js/device-shell.js, sections mode), the
// sections the kind has (DeviceShell.sectionsFor), each drawn by the editor
// the page already had:
//   Model       the template (the page's template choice, its lock and why)
//   Connection  the connection step (conn-step.js, context 'edit' — the one
//               Add uses: port, reach, role on the bus, protocol, line,
//               address; the serial picker in it), the device's locks shown
//               with their reasons and, for a line the bus sets, Edit bus line…
//               The primary meter: Modbus TCP, its address only (no Advanced
//               keys: its PATCH refuses them, FINDINGS L3.11-F3)
//   Polling     acquisition on/off and the poll-group intervals
//   Identity    name, type (with "+ Add type…", app-types.js), the fixed id,
//               and the attributes' identity category (description, where it
//               is, labels, nameplate) — saved by the same Save
//   Outputs     MQTT, InfluxDB, HA, HTTP, OPC UA, REST push (app-outputs.js,
//               staged) and the PQ recorder (staged, its own route)
// Every kind is edited here (DeviceShell.scopeOf):
//   - a device of its own, a device measured by its meters, the primary
//     (PATCH /api/devices/{id}, the primary through its adapter — L3.11a);
//   - an installation's unit: its name (the unit route) and its attributes;
//     its outputs are its installation's (edited here, for every unit, by
//     the unit's output routes); connection and polling say "set on the
//     installation X", with the way there;
//   - a component: its name and on/off (its device's route), its attributes;
//     the rest says "set on its device Y".
// Every change is staged in one draft. One Save sends the writes it takes in
// order (DeviceShell.savePlan): the device's own (ONE PATCH with If-Match,
// DeviceStore.devWrite), a unit's installation outputs, the attributes
// (their own revision, If-Match), the PQ recorder. A write that fails after
// others were saved is said as that: what was saved, what was not and why;
// the edits not saved stay, over the version stored now. A dry run 400 ms
// after the last change says what the Save does — "N changes · applies live"
// or "· restarts acquisition" — and labels the button by it (a unit's and a
// component's changes are said by the page: no dry run for them). A change
// that needs a look (role, a bus move, the template, an output going off) is
// listed in the page's own dialog first. A 412 (saved elsewhere meanwhile)
// keeps the edits: the version stored now is read, the edits re-applied over
// it, the fields both changed marked ("Stored now: …"); nothing is saved
// until Save is pressed again. The write lock is never here: it has its own
// dialog (⋯ → Unlock writes…).
Object.assign(JanitzaMonitor.prototype, {

    _DE_ICONS: { model: 'file-earmark-code', connection: 'ethernet', polling: 'arrow-repeat',
                 identity: 'person-vcard', outputs: 'signpost-split' },

    _deLabel(sec) {
        const t = (k, d) => this.t(k, d);
        return ({ model: t('devedit.sec.model', 'Model'), connection: t('devedit.sec.connection', 'Connection'),
                  polling: t('devedit.sec.polling', 'Polling'), identity: t('devedit.sec.identity', 'Identity'),
                  outputs: t('devedit.sec.outputs', 'Outputs') })[sec] || sec;
    },

    // ── open ─────────────────────────────────────────────────────────────
    // section: where it opens; opts.focus: a field named by the menu item
    // (Type… → the type)
    async devEditOpen(section, opts = {}) {
        const s = this._devDetail;
        if (!s || !window.DeviceViewModel.allows(this._effRole(), 'admin')) return false;
        if (this._devEdit?.open || this._devEditOpening) return false;      // E pressed twice: one dialog
        this._devEditOpening = true;
        try {
            return await this._devEditOpenNow(s, section, opts);
        } finally {
            this._devEditOpening = false;
        }
    },

    async _devEditOpenNow(s, section, opts) {
        const SH = window.DeviceShell;
        const opener = document.activeElement;
        // the version the modal edits: read now, with its tag (ADR-0030)
        const store = window.DeviceStore;
        const heldTag = store.etag;
        const { entry, groups, attrs, pq } = await this._deRead(s.id, s.entry);
        if (this._devDetail?.id !== s.id) return false;
        const sections = SH.sectionsFor(entry);
        if (!sections.length) return false;
        await this._loadDeviceTypes();
        const typeChoices = this._deTyped(entry) ? await this._typeChoices(entry) : [];
        // another device opened meanwhile: this one is not edited over it
        if (this._devDetail?.id !== s.id || this._devEdit?.open) return false;
        const kind = SH.kindOf(entry);
        const ed = this._devEdit = {
            id: s.id, entry, kind, sections, groups, attrs, pq, open: true, closing: false,
            // the connection form is mounted where the connection is its own
            hasConn: sections.includes('connection') && !!SH.scopeOf(entry, 'connection').own,
            draft: SH.draft(SH.fieldsOf(entry, groups, attrs ? attrs.attributes : undefined, pq)), typeChoices,
            dry: null, seq: 0, timer: null, conflicts: [], connConflicts: [], rebased: false, saving: false, opener,
            // the page drew an older version than the one read now (its held
            // tag moved): closed without saving, the page is drawn again, so
            // its own next write never carries the new tag with old data
            pageStale: store.etag !== heldTag,
            rebases: 0,
        };
        this._outOpcuaChosen = false;
        const t = (k, d, p) => this.t(k, d, p);
        const root = document.getElementById('devEditContent');
        const modal = document.getElementById('devEditModal');
        ed.shell = SH.mount(root, {
            mode: 'sections', prefix: 'devEdit', t,
            title: t('devedit.title', 'Edit {name}', { name: entry.name || s.id }),
            sections: sections.map(id => ({ id, label: this._deLabel(id), icon: this._DE_ICONS[id] })),
            onSelect: id => {
                ed.section = id;
                if (id === 'outputs') this._outNavInView(document.getElementById('devEditOutputsHost'));
            },
        });
        ed.shell.el.title.insertAdjacentHTML('beforeend', ` <span class="dev-chip">${this._esc(s.id)}</span>`);
        this._deDrawSections();
        // the modal's own wiring (its frame is drawn anew at every open)
        ed.shell.el.save.addEventListener('click', () => this.devEditSave());
        ed.shell.el.cancel.addEventListener('click', () => { if (!this._devEdit?.busy) this._dismissModal('devEditModal'); });
        ed.shell.el.close.addEventListener('click', () => this.closeModal('devEditModal'));
        if (!modal._deWired) {
            modal._deWired = true;
            // leaving the page (reload, close, another site) with unsaved
            // edits: the browser's own question (L3-F20)
            window.addEventListener('beforeunload', ev => {
                if (this._devEdit?.open && this._deDirty()) { ev.preventDefault(); ev.returnValue = ''; }
            });
            const onEdit = ev => this._deOnInput(ev);
            modal.addEventListener('input', onEdit);
            modal.addEventListener('change', onEdit);
            // a locked control keeps its value: aria-disabled, still focusable
            modal.addEventListener('click', ev => {
                const el = ev.target.closest && ev.target.closest('[data-de-locked]');
                if (el && /^(radio|checkbox)$/.test(el.type || '')) ev.preventDefault();
            }, true);
            modal.addEventListener('change', ev => {
                const el = ev.target;
                if (el && el.hasAttribute && el.hasAttribute('data-de-locked') && el.tagName === 'SELECT') {
                    el.value = el.dataset.deLocked;
                    ev.stopImmediatePropagation();
                }
            }, true);
        }
        modal._dirty = () => this._deDirty();
        const target = sections.includes(section) ? section : sections[0];
        const focusEl = opts.focus === 'type' ? document.getElementById('devEditType')
            : opts.focus === 'name' ? document.getElementById('ddvName') : null;
        ed.shell.select(target);
        ed.section = target;
        this.openModal('devEditModal', { focus: focusEl || ed.shell.heading(target) });
        // closed, the focus goes back to ⋯ (the menu item that opened it is gone)
        const back = document.getElementById('ddvActions');
        if (back) modal._returnFocus = back;
        if (target === 'outputs') this._outNavInView(document.getElementById('devEditOutputsHost'));   // laid out now
        // closing, however it closes (Save, Cancel, ✕, Esc): what it started stops
        modal._onClose = () => this._deClosed();
        this._deRender();
        return true;
    },

    async _deGroups(id) {
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(id)}/poll-groups`);
            return r.ok ? ((await r.json()).poll_groups || {}) : {};
        } catch (e) { return {}; }
    },

    // the device's attributes and their revision (attributes.yaml, ADR-0030):
    // null when they cannot be read — Identity then does not edit them
    async _deAttrs(id) {
        try {
            const r = await fetch(`/api/attributes/devices/${encodeURIComponent(id)}`);
            if (!r.ok) return null;
            const d = await r.json();
            return { attributes: d.attributes || {}, rev: d.rev || '' };
        } catch (e) { return null; }
    },

    // what the modal edits, read now: the entry with its tag (the store), its
    // poll groups, its attributes, its PQ recorder's settings
    async _deRead(id, fallback) {
        const SH = window.DeviceShell;
        const got = await window.DeviceStore.load(id);
        const entry = this._ddvWithParent({ ...(got || fallback || {}), id });
        const k = SH.kindOf(entry);
        const secs = SH.sectionsFor(entry);
        const pollOwn = secs.includes('polling') && SH.scopeOf(entry, 'polling').own && k !== 'component';
        const [groups, attrs] = await Promise.all([pollOwn ? this._deGroups(id) : {}, this._deAttrs(id)]);
        const dd = this._devDetail?.id === id ? this._devDetail : null;
        const pqOn = !!dd?.data?.pq_supported && ['device', 'primary', 'meter-only'].includes(k);
        const pq = pqOn ? { ...(dd.data.pq_recorder || {}), ...(dd.entry?.pq_recorder || {}), ...(entry.pq_recorder || {}) } : undefined;
        return { entry, groups, attrs, pq, read: !!got };
    },

    // a type is chosen here for a device of its own (the primary's is its
    // template's, a unit's its group's, a component's its kind's)
    _deTyped(e) {
        return ['device', 'meter-only'].includes(window.DeviceShell.kindOf(e));
    },

    _deDirty() {
        const ed = this._devEdit;
        if (!ed || ed.closing || ed.saving || ed.busy) return false;   // busy: it cannot close anyway
        if (window.DeviceShell.isDirty(ed.draft)) return true;
        const form = (this._cf || {}).ddv;
        if (!form || !ed.hasConn) return false;
        form.read();                     // what is typed now (the dry run may not have read it yet)
        return form.dirty();
    },

    // ── the sections ─────────────────────────────────────────────────────
    _deDrawSections() {
        const ed = this._devEdit;
        const SH = window.DeviceShell;
        const draw = { model: this._deModelHtml, connection: this._deConnHtml, polling: this._dePollingHtml,
                       identity: this._deIdentityHtml, outputs: this._deOutputsHtml };
        for (const sec of ed.sections) {
            const sc = SH.scopeOf(ed.entry, sec);
            ed.shell.panel(sec).innerHTML = sc.held ? this._deHeldHtml(ed, sec, sc) : draw[sec].call(this, ed);
        }
        if (ed.hasConn) this._deMountConn();
        if (ed.sections.includes('identity') && this._deTyped(ed.entry)) {
            this._typeFormWire('de', ty => {
                ed.typeChoices = [...ed.typeChoices.filter(o => o.value !== ty.id), { value: ty.id, label: this._kindLabel(ty.id) }];
                const sel = document.getElementById('devEditType');
                if (sel) {
                    sel.innerHTML = this._deTypeOptions(ed, ty.id);
                    sel.value = ty.id;
                    this._deSet('identity.type', ty.id);
                    sel.focus();
                }
            });
        }
        if (ed.sections.includes('outputs') && !SH.scopeOf(ed.entry, 'outputs').held) {
            // staged: the draft, and the locks the server says (the primary's)
            this._outStage = { host: 'devEditOutputsHost', draft: ed.draft, set: (p, v) => this._deSet(p, v),
                               locks: this._deLocks(ed).filter(l => String(l.field).startsWith('outputs.')) };
            this._renderOutputs();
            if (ed.pq) this._loadPqSinkStatus(ed.id);
        }
        // a field that is not editable here says why, beside it
        this._deApplyLocks();
    },

    // a section set elsewhere (a unit's installation, a component's device):
    // said, with what it is now and the way there
    _deHeldHtml(ed, sec, sc) {
        const t = (k, d, p) => this.t(k, d, p);
        const inst = sc.held === 'installation';
        const where = inst ? t('devedit.held.installation', 'the installation {name}', { name: sc.name })
                           : t('devedit.held.parent', 'its device {name}', { name: sc.name });
        const text = ({
            connection: t('devedit.held.connection', 'How it is read is set on {where}.', { where }),
            polling: t('devedit.held.polling', 'Acquisition and the poll intervals are set on {where}.', { where }),
            outputs: t('devedit.held.outputs', 'Where its values go is set on {where}.', { where }),
        })[sec] || t('devedit.held.other', 'Set on {where}.', { where });
        const s = this._devDetail || {};
        const facts = sec === 'connection'
            ? (inst ? this._unitReadViaTableHtml(s) : this._componentReadViaFactsHtml(s)) : '';
        return `<div class="dev-shell-held" data-held="${this._esc(sc.held)}">
            <p class="field-hint"><i aria-hidden="true" class="bi bi-${inst ? 'diagram-3' : 'cpu'}"></i> ${this._esc(text)}</p>
            ${this._deWayHtml(sc.held, `devEditWay-${sec}`)}
            ${facts}</div>`;
    },

    // the way out of the modal to where a field is set (the modal closes
    // first, asking about unsaved edits)
    _deWayHtml(where, id) {
        const t = (k, d) => this.t(k, d);
        const inst = where === 'installation';
        const label = inst ? t('devices.openInstallation', 'Open installation') : t('components.openParent', 'Open device');
        return `<button type="button" class="btn btn-secondary btn-sm dev-shell-way"${id ? ` id="${this._esc(id)}"` : ''} data-de-way="${this._esc(where)}"
            ${this._act('devEditGoTo', [where])}><i aria-hidden="true" class="bi bi-${inst ? 'diagram-3' : 'cpu'}"></i> ${this._esc(label)}</button>`;
    },

    async devEditGoTo(where) {
        const ed = this._devEdit;
        const e = ed ? ed.entry : (this._devDetail?.entry || {});
        if (!await this._deLeave()) return;
        if (where === 'installation' && e.endpoint_id) return this.openEndpointDetail(e.endpoint_id);
        if (where === 'parent' && e.component_of) return this.openDeviceDetail(e.component_of);
    },

    // an input bound to a draft path
    _deIn(path, kind) {
        return ` data-draft-path="${this._esc(path)}" data-draft-kind="${this._esc(kind)}"`;
    },

    _deIntro(text) {
        return `<p class="field-hint dev-shell-intro">${this._esc(text)}</p>`;
    },

    _deModelHtml(ed) {
        const t = (k, d, p) => this.t(k, d, p);
        const e = ed.entry;
        const cur = ed.draft.cur['model.template'];
        const tpls = (this._devDetail?.templates || []).filter(x => this._tplFitsDevice(x, { template: e.template, protocol: e.protocol }));
        const stored = tpls.find(x => x.id === e.template);
        const opts = [`<option value="">${this._esc(t('devices.detail.noTemplate', '(no template)'))}</option>`]
            .concat(tpls.map(x => `<option value="${this._esc(x.id)}" ${x.id === cur ? 'selected' : ''}>${this._esc(x.name)} · ${this._esc(t('devedit.tplRegs', '{n} measurements', { n: x.registers }))}</option>`)).join('');
        const lock = (e.locks || []).find(l => l.field === 'template');
        return `${this._deIntro(t('devedit.intro.model', 'The template is the measurement map: what is read, how, and how it is decoded.'))}
            <div class="form-group">
                <label class="form-label" for="ddvTemplate">${this._esc(t('devices.wizard.template', 'Template'))}</label>
                <select id="ddvTemplate" class="input"${this._deIn('model.template', 'text')} aria-describedby="devEditTplHint${lock ? ' devEditTplLock' : ''}"
                    ${lock ? `data-de-lock="${this._esc(lock.text)}"` : ''}>${opts}</select>
                <p class="field-hint" id="devEditTplHint">${this._esc(t('devedit.tplNow', 'Stored: {tpl}', { tpl: stored ? `${stored.name} (${stored.id})` : (e.template || t('ddv.setup.noTemplate', 'none')) }))}
                    · ${this._esc(t('devedit.tplHint', 'A change restarts acquisition; the new template\'s registers are selected where the device has none selected.'))}</p>
                ${lock ? `<p class="field-hint dev-lock" id="devEditTplLock"><i aria-hidden="true" class="bi bi-lock"></i> ${this._esc(this._deLockText(lock))}</p>` : ''}
            </div>`;
    },

    // a lock in the page's words (ui/js/lock-text.js; L3.10-F16b)
    _deLockText(l) {
        return this._lockText(l);
    },

    _deConnHtml(ed) {
        const t = (k, d, p) => this.t(k, d, p);
        const locks = (ed.entry.locks || []).filter(l => String(l.field).startsWith('connection.'));
        return `${this._deIntro(t('devedit.intro.connection', 'How the gateway reaches it. A field the bus or the template decides says why.'))}
            ${locks.length ? `<div class="setup-locks dev-shell-locks"><h5>${this._esc(t('ddv.setup.locks', 'Fixed, and why'))}</h5>
                <ul>${locks.map((l, i) => `<li id="devEditLock-${i}" data-lock="${this._esc(l.code)}"><code>${this._esc(l.field)}</code> — ${this._esc(this._deLockText(l))}${this._deLineFix(ed, l)}</li>`).join('')}</ul></div>` : ''}
            <div id="ddvConn" class="cf-host"></div>
            ${ed.entry.template ? `<div class="dev-shell-own dev-edit-preview"><button type="button" data-live class="btn btn-secondary btn-sm" id="devEditPreview" ${this._act('devEditPreview')} aria-describedby="devEditPreviewHint">
                <i aria-hidden="true" class="bi bi-eye"></i> ${this._esc(t('pv.button', 'Preview'))}</button>
                <span class="field-hint" id="devEditPreviewHint">${this._esc(t('pv.editHint', 'Reads the connection as edited here, once, before Save: nothing is stored, nothing written.'))}</span></div>` : ''}`;
    },

    // the edited connection (and model), read once before Save
    devEditPreview() {
        const ed = this._devEdit;
        const form = (this._cf || {}).ddv;
        if (!ed) return;
        let connection = {};
        if (form && form.host && form.host.isConnected) {
            form.read();
            connection = form.payload() || {};
        }
        const tpl = ed.draft.cur['model.template'];
        return this.ddvPreview(ed.id, { connection, template: tpl && tpl !== ed.entry.template ? tpl : '' });
    },

    // a line the bus sets: its fix is Edit bus line… (every device on the bus)
    // — on a serial port here, or a port of our serial bridge
    _deLineFix(ed, l) {
        if (l.field !== 'connection.line') return '';
        const br = l.code === 'line_bridge' ? (this._bridges || []).find(b => b.id === ed.entry.bridge) : null;
        if (!(l.code === 'line_shared' || (br && this._bridgeType(br.type).serial_config === 'api'))) return '';
        return ` <button type="button" class="btn btn-ghost btn-sm" data-admin id="devEditLineFix" ${this._act('busLineOpen', ['', ed.id])}><i aria-hidden="true" class="bi bi-sliders"></i> ${this._esc(this.t('cs.line.edit', 'Edit bus line…'))}</button>`;
    },

    _deMountConn() {
        const ed = this._devEdit;
        const host = document.getElementById('ddvConn');
        if (!host) return;
        const CM = window.ConnformModel;
        const M = ed.connM || CM.model(ed.entry);
        // the connection step (conn-step.js), the one Add uses: the matrix,
        // the buses, the template and this device's locks decide its options
        const form = this._csMount(host, 'ddv', M, {
            context: 'edit', deviceId: ed.id, primary: ed.kind === 'primary', device: ed.entry,
            template: () => ed.draft.cur['model.template'] || ed.entry.template || '',
            // a redraw of the step (its buses and matrix read, a choice made)
            // draws its fields anew: the fields both changed are marked again
            // (review of L3.11b, m4 — FINDINGS L3.11b-F6)
            onChange: () => {
                if (this._devEdit !== ed) return;
                this._deApplyLocks();
                if ((ed.connConflicts || []).length) this._deMarkConflicts();
                this._deSchedule();
            },
        });
        // after a 412 the form edits the version stored now, my edits over it
        if (ed.connOrig) {
            form.orig = JSON.parse(JSON.stringify(ed.connOrig));
            form.orig.keepalive = ed.connOrig.keepalive;
            form.origAxes = window.ConnStepModel.axesOf(form.orig);
        }
        ed.connM = null;
        ed.connOrig = null;
    },

    _dePollingHtml(ed) {
        const t = (k, d, p) => this.t(k, d, p);
        const meterOnly = ed.kind === 'meter-only';
        const comp = ed.kind === 'component';
        const groups = Object.entries(ed.groups || {});
        const on = ed.draft.cur['polling.enabled'];
        const label = meterOnly ? t('ddv.meterOnlyEnabled', 'Enabled — computed from its meters')
            : comp ? t('devedit.compEnabled', 'On: read through its device')
            : t('devedit.enabled', 'Acquisition on: the gateway reads it');
        // a component's intervals are its device's: said, with the way there
        const sc = { name: ed.entry.parent_name || ed.entry.component_of };
        const groupsHtml = meterOnly ? '' : comp
            ? `<p class="field-hint dev-shell-held"><i aria-hidden="true" class="bi bi-cpu"></i> ${this._esc(t('devedit.held.intervals',
                'Its poll intervals are set on {where}.', { where: t('devedit.held.parent', 'its device {name}', sc) }))}</p>${this._deWayHtml('parent', 'devEditWay-polling')}`
            : groups.length ? `<fieldset class="dev-shell-groups"><legend class="form-label">${this._esc(t('devices.pollGroups', 'Poll-group intervals (seconds)'))}</legend>
                ${groups.map(([name, g]) => `<div class="form-group dev-shell-pg">
                    <label class="form-label" for="ddvPg-${this._esc(name)}">${this._esc(name)}</label>
                    <input type="text" inputmode="decimal" class="input input-sm ddv-pg" id="ddvPg-${this._esc(name)}" data-group="${this._esc(name)}"
                        value="${this._esc(ed.draft.cur[`polling.poll_groups.${name}`] ?? '')}"${this._deIn(`polling.poll_groups.${name}`, 'num')}
                        aria-describedby="ddvPgHint-${this._esc(name)}">
                    <span class="field-hint" id="ddvPgHint-${this._esc(name)}">${this._esc([g.description, t('devedit.pgStored', 'stored: {s} s', { s: g.interval })].filter(Boolean).join(' · '))}</span>
                </div>`).join('')}</fieldset>`
            : `<p class="field-hint">${this._esc(t('devices.noPollGroups', 'No poll groups.'))}</p>`;
        return `${this._deIntro(comp ? t('devedit.intro.pollingComp', 'Whether it is read. It is read through its device\'s connection, at its device\'s rhythm.')
                : t('devedit.intro.polling', 'Acquisition on or off, and how often each poll group is read. An interval applies at once, without a restart.'))}
            <div class="form-group"><label class="checkbox-label dev-shell-check"><input type="checkbox" id="ddvEnabled" ${on ? 'checked' : ''}${this._deIn('polling.enabled', 'bool')}>
                <span>${this._esc(label)}</span></label></div>
            ${groupsHtml}`;
    },

    _deTypeOptions(ed, cur) {
        // the stored type is always shown, even one the choices leave out
        const opts = cur && !ed.typeChoices.some(o => o.value === cur)
            ? [{ value: cur, label: this._kindLabel(cur) }, ...ed.typeChoices] : ed.typeChoices;
        return opts.map(o => `<option value="${this._esc(o.value)}" ${o.value === cur ? 'selected' : ''}>${this._esc(o.label)}</option>`).join('');
    },

    // the attributes' identity fields (ADR-0027) as Identity edits them —
    // DeviceShell.ABOUT, in that order
    _DE_ABOUT: {
        description: { kind: 'text', multiline: true, max: 500, label: ['attr.identity.description', 'Description'] },
        location: { kind: 'text', max: 120, label: ['attr.identity.location', 'Where it is (e.g. garage, cabinet 2)'] },
        labels: { kind: 'labels', label: ['attr.identity.labels', 'Labels'] },
        manufacturer: { kind: 'text', max: 64, label: ['attr.identity.manufacturer', 'Manufacturer'] },
        model: { kind: 'text', max: 64, label: ['attr.identity.model', 'Model'] },
        serial_number: { kind: 'text', max: 64, label: ['attr.identity.serial_number', 'Serial number'] },
        firmware: { kind: 'text', max: 64, label: ['attr.identity.firmware', 'Firmware'] },
        installed_on: { kind: 'date', label: ['attr.identity.installed_on', 'Installed on'] },
    },

    _deAboutHtml(ed) {
        const t = (k, d, p) => this.t(k, d, p);
        if (!ed.attrs) {
            return `<p class="field-hint dev-shell-about" id="devEditAboutUnread"><i aria-hidden="true" class="bi bi-exclamation-circle"></i>
                ${this._esc(t('devedit.aboutUnread', 'Its attributes could not be read: the description, labels and nameplate are not edited here now.'))}</p>`;
        }
        const input = (k, f) => {
            const id = `devEditAbout-${k}`;
            const path = `about.${k}`;
            const v = ed.draft.cur[path];
            const lbl = `<label class="form-label" for="${id}">${this._esc(t(f.label[0], f.label[1]))}</label>`;
            if (f.multiline) {
                return `<div class="form-group dev-shell-about-wide">${lbl}<textarea id="${id}" class="input" rows="2" maxlength="${f.max}"${this._deIn(path, 'text')}>${this._esc(v ?? '')}</textarea></div>`;
            }
            if (f.kind === 'labels') {
                return `<div class="form-group">${lbl}<input type="text" id="${id}" class="input" spellcheck="false" autocapitalize="none" placeholder="hvac, floor-1"
                    value="${this._esc((v || []).join(', '))}"${this._deIn(path, 'labels')} aria-describedby="${id}-hint">
                    <p class="field-hint" id="${id}-hint">${this._esc(t('attr.hint.labels', 'Separated by commas: a-z 0-9 - _, at most {n}', { n: 16 }))}</p></div>`;
            }
            return `<div class="form-group">${lbl}<input type="${f.kind === 'date' ? 'date' : 'text'}" id="${id}" class="input"${f.max ? ` maxlength="${f.max}"` : ''}
                value="${this._esc(v ?? '')}"${this._deIn(path, 'text')}></div>`;
        };
        return `<fieldset class="dev-shell-about-set"><legend class="form-label">${this._esc(t('devedit.aboutTitle', 'Description, place, labels and nameplate'))}</legend>
            <div class="dev-shell-about-grid">${Object.entries(this._DE_ABOUT).map(([k, f]) => input(k, f)).join('')}</div>
            <p class="field-hint dev-shell-about"><i aria-hidden="true" class="bi bi-tags"></i>
                ${this._esc(t('devedit.aboutSaved', 'Saved with Save, as its attributes (their own revision). Coordinates, ratings and the other attributes:'))}
                <button type="button" class="btn btn-ghost btn-sm" id="devEditAbout" ${this._act('devEditGoAbout')}>${this._esc(t('ddv.menu.about', 'About (attributes)…'))}</button></p>
        </fieldset>`;
    },

    _deIdentityHtml(ed) {
        const t = (k, d, p) => this.t(k, d, p);
        const e = ed.entry;
        const meterOnly = ed.kind === 'meter-only';
        // the type: chosen here for a device of its own; the primary's is
        // locked (its lock says why); a unit's is its group's, a component's
        // its kind's — said, not offered
        let typeHtml = '';
        if (this._deTyped(e) || ed.kind === 'primary') {
            typeHtml = `<div class="form-group"><label class="form-label" for="devEditType">${this._esc(t('kind.set', 'Type'))}</label>
                <select id="devEditType" class="input"${this._deIn('identity.type', 'text')} aria-describedby="devEditTypeHint">${
                    ed.kind === 'primary' ? `<option value="${this._esc(ed.draft.cur['identity.type'])}" selected>${this._esc(this._kindLabel(e.type || e.kind) || '—')}</option>`
                        : this._deTypeOptions(ed, ed.draft.cur['identity.type'])}</select>
                <p class="field-hint" id="devEditTypeHint">${this._esc(t('kind.setMsg', 'How the device is filed and found. It changes no reading, topic or history.'))}</p>
                ${ed.kind === 'primary' ? '' : this._typeFormHtml('de', { exclude: meterOnly ? ['meter'] : [] })}</div>`;
        } else {
            const word = ed.kind === 'unit'
                ? t('devedit.typeUnit', 'Type: {type} — set on the installation {name} (its group\'s role).', { type: this._kindLabel(e.type || e.kind) || '—', name: e.endpoint_name || e.endpoint_id })
                : t('devedit.typeComp', 'Type: {type} — a component of {name}.', { type: e.component_type_name || e.component_type || this._kindLabel(e.type || e.kind) || '—', name: e.parent_name || e.component_of });
            typeHtml = `<p class="field-hint" id="devEditTypeSaid"><i aria-hidden="true" class="bi bi-tag"></i> ${this._esc(word)}</p>`;
        }
        const nameHint = ed.kind === 'unit' ? t('devedit.nameHintUnit', 'Blank: the installation\'s naming. Up to 80 characters.')
            : t('devedit.nameHint', 'Blank: the id. Up to 80 characters.');
        return `${this._deIntro(t('devedit.intro.identity', 'How it is named and filed. Neither changes a reading, a topic or its history.'))}
            <div class="form-row">
                <div class="form-group flex-2"><label class="form-label" for="ddvName">${this._esc(t('devices.wizard.name', 'Device name'))}</label>
                    <input type="text" id="ddvName" class="input" maxlength="80" value="${this._esc(ed.draft.cur['identity.name'])}"${this._deIn('identity.name', 'text')}
                        aria-describedby="devEditNameHint">
                    <p class="field-hint" id="devEditNameHint">${this._esc(nameHint)}</p></div>
                <div class="form-group"><label class="form-label" for="devEditId">ID</label>
                    <input type="text" id="devEditId" class="input" value="${this._esc(ed.id)}" readonly aria-describedby="devEditIdHint">
                    <p class="field-hint" id="devEditIdHint">${this._esc(t('devedit.idHint', 'Fixed after creation: it names the topics and the history.'))}</p></div>
            </div>
            ${typeHtml}
            ${this._deAboutHtml(ed)}`;
    },

    _deOutputsHtml(ed) {
        const t = (k, d, p) => this.t(k, d, p);
        const sc = window.DeviceShell.scopeOf(ed.entry, 'outputs');
        // a unit's outputs are its installation's: edited here, for every unit
        const shared = sc.shared ? `<div class="dev-shell-shared" id="devEditOutputsShared"><p class="field-hint"><i aria-hidden="true" class="bi bi-diagram-3"></i>
                ${this._esc(t('devedit.outputsShared', 'These outputs are shared: HTTP / JSON and REST push are set on the installation {name} (every unit of it); OPC UA and the publishing rhythm on its group {group} (every unit of the group).',
                    { name: sc.name, group: ed.entry.group_name || ed.entry.group_id || '—' }))}</p>
                ${this._deWayHtml('installation', 'devEditWay-outputs')}</div>` : '';
        return `${this._deIntro(t('devedit.intro.outputs', 'Where its values go. Switches and settings apply on Save, with everything else.'))}
            ${shared}
            <div id="devEditOutputsHost"></div>
            ${ed.pq ? this._dePqHtml(ed) : ''}`;
    },

    // the PQ event recorder: staged with the rest, sent by Save to its own
    // route (FINDINGS L3-F21)
    _dePqHtml(ed) {
        const t = (k, d, p) => this.t(k, d, p);
        const c = ed.draft.cur;
        const d = this._devDetail?.data || {};
        return `<div class="settings-card dev-shell-pq" id="devEditPq">
            <div class="settings-card-header">
                <h3><i aria-hidden="true" class="bi bi-activity"></i> ${this._esc(t('pq.sinkTitle', 'PQ event recorder'))}</h3>
                <label class="switch-label"><input type="checkbox" id="devEditPq-enabled" ${c['pq.enabled'] ? 'checked' : ''}${this._deIn('pq.enabled', 'bool')}><span>${this._esc(t('devices.sink.enable', 'Enable'))}</span></label>
            </div>
            <div class="settings-card-body">
                <div class="form-row">
                    <div class="form-group" style="max-width:130px"><label class="form-label" for="devEditPq-poll_s">${this._esc(t('pq.pollS', 'Poll (s)'))}</label>
                        <input type="number" id="devEditPq-poll_s" class="input" min="30" value="${this._esc(c['pq.poll_s'] ?? '')}"${this._deIn('pq.poll_s', 'num')}></div>
                    <div class="form-group flex-2"><label class="form-label" for="devEditPq-base_url">${this._esc(t('pq.baseUrl', 'Base URL'))}</label>
                        <input type="text" id="devEditPq-base_url" class="input" value="${this._esc(c['pq.base_url'] || '')}" placeholder="${this._esc(this._pqDefaultUrl(d))}"${this._deIn('pq.base_url', 'text')}
                            aria-describedby="devEditPq-base_url-hint">
                        <div class="field-hint" id="devEditPq-base_url-hint">${this._esc(t('pq.baseUrlBlank', 'Blank: {url}', { url: this._pqDefaultUrl(d) }))}</div></div>
                </div>
                <label class="checkbox-label"><input type="checkbox" id="devEditPq-archive_waveforms" ${c['pq.archive_waveforms'] ? 'checked' : ''}${this._deIn('pq.archive_waveforms', 'bool')}>
                    <span>${this._esc(t('pq.archiveWf', 'Archive event waveforms (RMS traces of the implicated channels)'))}</span></label>
                <div class="rest-status" data-pq-status></div>
                <p class="field-hint"><i aria-hidden="true" class="bi bi-info-circle"></i> ${this._esc(t('devedit.pqStaged', 'Saved with Save, with everything else (its own route: the device\'s other settings are saved first).'))}</p>
            </div></div>`;
    },

    // ── locks: aria-disabled, focusable, the reason beside ───────────────
    // the locks the modal says: the server's (a primary's fields, a line, a
    // template), and a component's on/off while its device is off (L3.10-F9)
    _deLocks(ed) {
        const out = (ed.entry.locks || []).filter(l => l.field !== 'device');
        if (ed.kind === 'component' && ed.entry.parent_enabled === false) {
            out.push({ field: 'polling.enabled', code: 'parent_off', way: 'parent',
                       text: this.t('ddv.why.parentOff', 'Its device {name} is off: enable it there', { name: ed.entry.parent_name || ed.entry.component_of }) });
        }
        return out;
    },

    // where a lock is changed instead, when there is such a place: a
    // Settings tab (the gateway's own switch or rhythm)
    _deLockWay(l) {
        const SETTINGS = { 'outputs.ha_discovery': 'mqtt', 'outputs.mqtt': 'mqtt', 'outputs.influxdb': 'influxdb' };
        return SETTINGS[l.field] || '';
    },

    // a lock's reason, with its way (a Settings tab, a device), as a hint
    _deLockHtml(l, id) {
        const t = (k, d) => this.t(k, d);
        const tab = this._deLockWay(l);
        const way = tab ? ` <button type="button" class="btn btn-ghost btn-sm dev-lock-way" data-lock-way="${this._esc(tab)}" ${this._act('openSettingsTab', [tab])}>${
                this._esc(`${t('devices.pub.settingsLink', 'Settings')} → ${tab === 'mqtt' ? 'MQTT' : 'InfluxDB'}`)}</button>`
            : l.way ? ` ${this._deWayHtml(l.way)}` : '';
        return `<p class="field-hint dev-lock" id="${this._esc(id)}"><i aria-hidden="true" class="bi bi-lock"></i> ${this._esc(l.text && l.code !== 'parent_off' ? this._deLockText(l) : l.text || '')}${way}</p>`;
    },

    _deApplyLocks() {
        const ed = this._devEdit;
        if (!ed) return;
        const locks = (ed.entry.locks || []).filter(l => String(l.field).startsWith('connection.'));
        const lockEl = (el, reasonId) => {
            if (!el) return;
            el.setAttribute('aria-disabled', 'true');
            el.setAttribute('data-de-locked', el.tagName === 'SELECT' ? el.value : '1');
            if (el.tagName === 'INPUT' && !/^(radio|checkbox)$/.test(el.type)) el.readOnly = true;
            if (el.tagName === 'TEXTAREA') el.readOnly = true;
            const ids = (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
            if (reasonId && !ids.includes(reasonId)) el.setAttribute('aria-describedby', [...ids, reasonId].join(' '));
        };
        const tpl = document.getElementById('ddvTemplate');
        if (tpl && tpl.hasAttribute('data-de-lock')) lockEl(tpl, 'devEditTplLock');
        // the connection step marks its own refused options (role, port, …)
        // with their reasons; a line the bus still sets is also tied to the
        // server's words listed above it
        locks.forEach((l, i) => {
            const why = `devEditLock-${i}`;
            if (l.field === 'connection.line') {
                ['Baud', 'Parity', 'Stop', 'Bits'].map(x => document.getElementById(`ddv${x}`))
                    .filter(el => el && el.hasAttribute('data-cs-locked')).forEach(el => lockEl(el, why));
            }
        });
        // a field a lock names (the primary's name, type and acquisition; a
        // component's on/off under a device that is off): held, its reason
        // and its way beside it
        const FIELD_EL = { 'identity.name': 'ddvName', 'identity.type': 'devEditType', 'polling.enabled': 'ddvEnabled' };
        for (const l of this._deLocks(ed)) {
            const el = FIELD_EL[l.field] && document.getElementById(FIELD_EL[l.field]);
            if (!el) continue;
            const why = `${el.id}-lock`;
            if (!document.getElementById(why)) (el.closest('.form-group') || el.parentElement).insertAdjacentHTML('beforeend', this._deLockHtml(l, why));
            lockEl(el, why);
        }
    },

    // ── what was typed ───────────────────────────────────────────────────
    _deOnInput(ev) {
        const ed = this._devEdit;
        if (!ed || !ed.open) return;
        const el = ev.target && ev.target.closest ? ev.target.closest('[data-draft-path]') : null;
        if (el && !el.hasAttribute('data-de-locked') && !el.closest('.type-new-form')) {
            ed.draft = window.DeviceShell.set(ed.draft, el.dataset.draftPath, this._draftReadIn(el));
        }
        if (ev.target && ev.target.closest && ev.target.closest('.type-new-form, #ddvOpcuaFilter')) return;
        this._deSchedule();
    },

    // the value an input bound to a draft path holds, as the draft keeps it
    // (bool | num | mode | text | headers | fields)
    _draftReadIn(el) {
        const kind = el.dataset.draftKind;
        if (kind === 'bool') return !!el.checked;
        if (kind === 'mode') return el.value || '';
        if (kind === 'num') {
            const raw = String(el.value ?? '').trim();
            if (raw === '') return null;
            const n = Number(raw.replace(',', '.'));
            return Number.isFinite(n) ? n : raw;          // the gateway says why a word is not seconds
        }
        // labels as typed, split at the commas (the server says which one is not a label)
        if (kind === 'labels') return String(el.value || '').split(',').map(x => x.trim()).filter(Boolean);
        if (kind === 'headers') {
            const headers = {};
            String(el.value || '').split('\n').forEach(line => {
                const i = line.indexOf(':');
                const k = i > 0 ? line.slice(0, i).trim() : '';
                if (k) headers[k] = line.slice(i + 1).trim();
            });
            return headers;
        }
        if (kind === 'fields') return [...el.querySelectorAll('input[type=checkbox]:checked')].map(x => x.value).sort();
        return String(el.value ?? '');
    },

    // a value set by a control that is not an input (an output's switch)
    _deSet(path, v) {
        const ed = this._devEdit;
        if (!ed || !ed.open) return;
        ed.draft = window.DeviceShell.set(ed.draft, path, v);
        if (this._outStage) this._outStage.draft = ed.draft;
        this._deSchedule();
    },

    _deSchedule() {
        const ed = this._devEdit;
        if (!ed) return;
        clearTimeout(ed.timer);
        this._deMarks();
        ed.timer = setTimeout(() => this._deDryRun(), 400);
    },

    // the sections with a change: a dot on the rail
    _deMarks() {
        const ed = this._devEdit;
        const form = (this._cf || {}).ddv;
        const extra = form && ed.hasConn && form.dirty() ? ['connection'] : [];
        ed.shell.markChanged([...window.DeviceShell.changedSections(ed.draft, extra)]);
    },

    // the PATCH the modal would send now, and what the page itself refuses
    _deBody() {
        const ed = this._devEdit;
        const form = (this._cf || {}).ddv;
        let conn = {}, local = {};
        if (form && ed.hasConn) {
            const got = form.read();
            local = got.errors || {};
            // the primary: its address only (it binds no adapter: its PATCH
            // refuses a key it has not — L3.11b-F2)
            conn = (ed.kind === 'primary' ? form.payload() : this._cfWithAdapter(form, form.payload())) || {};
        }
        // "only the values I choose" with none ticked: refused here, in the
        // words of the page's own OPC UA editor
        const c = ed.draft.cur;
        if (this._outOpcuaChosen && c['outputs.opcua.enabled'] && !(c['outputs.opcua.fields'] || []).length) {
            local = { ...local, 'outputs.opcua.fields': this.t('devices.sink.opcuaNone', 'Choose at least one value, or serve every value.') };
        }
        // what a route with no dry run would refuse (the PQ interval; a unit's
        // and a component's own checks): refused here, before any write
        // (review of L3.11b, m5 and m9)
        local = { ...local, ...window.DeviceShell.localErrors(ed.draft, ed.entry, (k, d, p) => this.t(k, d, p)) };
        // a write of the last Save refused these fields: said at them until they change
        const se = ed.stepErr;
        if (se && JSON.stringify(se.paths.map(p => c[p])) === se.was) local = { ...local, ...se.fields };
        else if (se) ed.stepErr = null;
        // a unit and a component have no PATCH of their own (their routes: savePlan)
        const own = ['device', 'primary', 'meter-only'].includes(ed.kind);
        return { body: own ? window.DeviceShell.payload(ed.draft, { connection: conn }) : null, conn, local };
    },

    // ── the dry run: what Save would do ──────────────────────────────────
    async _deDryRun() {
        const ed = this._devEdit;
        if (!ed || !ed.open) return null;
        clearTimeout(ed.timer);
        const { body, conn, local } = this._deBody();
        ed.conn = conn;
        if (Object.keys(local).length) {
            ed.dry = { error: { fields: local, errors: Object.entries(local).map(([k, v]) => `${k}: ${v}`) } };
            ed.body = null;
            this._deRender();
            return ed.dry;
        }
        ed.body = body;
        // what the server's dry run does not see (the attributes, the PQ
        // recorder; all of a unit's or a component's changes): said here
        const mine = window.DeviceShell.localChanges(ed.draft, ed.entry);
        // a device that should run and does not reconnects when its
        // connection is sent: the server says so even with nothing changed
        if (!body) {
            ed.dry = mine.length ? { changes: mine, restart_required: false, effects: this._deSharedEffects(ed), local: true } : null;
            this._deRender();
            return ed.dry;
        }
        const seq = ++ed.seq;
        ed.dry = { pending: true };
        this._deRender();
        let r, d = {};
        try {
            r = await window.DeviceStore.devWrite(ed.id, 'PATCH', `/api/devices/${encodeURIComponent(ed.id)}?dry_run=true`, body);
            d = await r.json().catch(() => ({}));
        } catch (e) {
            r = { ok: false, status: 0 };
            d = { detail: { errors: [String(e.message || e)] } };
        }
        if (seq !== ed.seq || !ed.open || this._devEdit !== ed) return undefined;     // a newer check runs
        if (r.status === 412) { await this._deRebase(); return undefined; }
        ed.dry = r.ok ? { ...d, changes: [...(d.changes || []), ...mine] } : { error: this._deErr(r, d) };
        this._deRender();
        return ed.dry;
    },

    // a unit's shared outputs: the units a change reaches, named (the
    // installation's for HTTP / JSON and REST push, its group's for OPC UA
    // and the publishing rhythm — review of L3.11b, m2)
    _deSharedEffects(ed) {
        if (ed.kind !== 'unit') return [];
        const t = (k, d, p) => this.t(k, d, p);
        const e = ed.entry;
        const ch = window.DeviceShell.changed(ed.draft);
        const inst = (this._devices || []).filter(d => d.endpoint_id === e.endpoint_id);
        const grp = inst.filter(d => d.group_id === e.group_id);
        const names = list => list.map(d => d.name || d.id).join(', ') || e.id;
        const out = [];
        if (ch.some(p => p === 'outputs.http_output' || p.startsWith('outputs.rest_push.'))) {
            out.push(t('devedit.effInstallation', 'HTTP / JSON, REST push: every unit of the installation — {units}', { units: names(inst) }));
        }
        if (ch.some(p => p.startsWith('outputs.opcua.') || /^outputs\.(mqtt|influxdb)\.(publish_mode|heartbeat_interval|write_interval)$/.test(p))) {
            out.push(t('devedit.effGroup', 'OPC UA, publishing: every unit of its group — {units}', { units: names(grp) }));
        }
        return out;
    },

    _deErr(r, d) {
        const det = d && d.detail;
        if (det && typeof det === 'object') return { errors: det.errors || [], fields: det.fields || {} };
        return { errors: [typeof det === 'string' && det ? det : `HTTP ${r.status}`], fields: {} };
    },

    // the footer, the Save button, the diff, the errors — from ed.dry
    _deRender() {
        const ed = this._devEdit;
        const SH = window.DeviceShell;
        if (!ed || !ed.shell) return;
        const tn = (k, n, d, p) => this.tn(k, n, d, p);
        const t = (k, d, p) => this.t(k, d, p);
        const v = SH.verdict(ed.dry);
        ed.shell.setSummary(SH.summaryText(ed.dry, tn), v);
        ed.shell.setSave(SH.saveLabel(ed.dry, t), { disabled: v === 'none' || v === 'error' || ed.saving || ed.busy });
        if (v === 'checking') return;              // the list stays as it was until the answer
        const rows = (ed.dry && !ed.dry.error) ? (ed.dry.changes || []) : [];
        const shown = x => (x === undefined || x === null || x === '' ? '—' : typeof x === 'object' ? JSON.stringify(x) : String(x));
        ed.shell.setDiff(rows.length ? `<table class="dev-shell-difftab"><caption class="sr-only">${this._esc(t('devedit.diffCaption', 'What Save changes'))}</caption>
            <thead><tr><th scope="col">${this._esc(t('devedit.colSection', 'Section'))}</th><th scope="col">${this._esc(t('ddv.conflict.colField', 'Setting'))}</th>
                <th scope="col">${this._esc(t('devedit.colStored', 'Stored'))}</th><th scope="col">${this._esc(t('devedit.colNew', 'After Save'))}</th></tr></thead>
            <tbody>${rows.map(c => `<tr><td>${this._esc(this._deLabel(c.section))}</td><th scope="row"><code>${this._esc(c.field)}</code></th>
                <td>${this._esc(shown(c.from))}</td><td>${this._esc(shown(c.to))}</td></tr>`).join('')}</tbody></table>
            ${(ed.dry.effects || []).length ? `<ul class="dev-shell-effects">${ed.dry.effects.map(x => `<li>${this._esc(x)}</li>`).join('')}</ul>` : ''}` : '', rows.length > 0);
        this._deShowErrors(v === 'error' ? ed.dry.error : null);
        this._deMarks();
    },

    // a refusal: at its field (in whichever section), and listed at the top
    // of the modal, each item a link to its field
    _deShowErrors(err) {
        const ed = this._devEdit;
        const SH = window.DeviceShell;
        const scope = document.getElementById('devEditContent');
        const form = (this._cf || {}).ddv;
        const idOf = path => {
            if (SH.sectionOf(path) === 'connection') return form ? form.fieldId(path.startsWith('connection') ? path : `connection.${path}`) : null;
            return this._deFieldId(path);
        };
        if (!err) {
            this._placeFieldErrors(scope, {}, () => null, { anySection: true, focus: false });
            ed.shell.showErrors([]);
            return;
        }
        const fields = { ...(err.fields || {}) };
        // the intervals are judged as one field; its message names the group
        // ("normal: interval must be a number"): said at that group's input
        const pgMsg = fields['polling.poll_groups'];
        const pgName = /^([\w.-]+): /.exec(pgMsg || '');
        if (pgName && document.getElementById(`ddvPg-${pgName[1]}`)) {
            delete fields['polling.poll_groups'];
            fields[`polling.poll_groups.${pgName[1]}`] = pgMsg.slice(pgName[0].length);
        }
        // an Advanced connection field with an error is never hidden in its fold
        for (const p of Object.keys(fields)) {
            const id = idOf(p);
            const adv = id ? document.getElementById(id)?.closest('details') : null;
            if (adv) adv.open = true;
        }
        this._placeFieldErrors(scope, fields, idOf, { anySection: true, focus: false });
        const keys = [...new Set([...Object.keys(err.fields || {}), ...Object.keys(fields)])];
        const items = Object.entries(fields).map(([p, m]) => ({ section: SH.sectionOf(p), text: `${p}: ${m}`, fieldId: idOf(p) }))
            .concat((err.errors || []).filter(e => !keys.some(k => String(e).startsWith(k + ': ')))
                .map(e => ({ section: SH.sectionOf(String(e).split(':')[0]), text: String(e), fieldId: null })));
        ed.shell.showErrors(items.map(it => ({ ...it, section: ed.sections.includes(it.section) ? it.section : null })));
    },

    // a draft path (or a server field) → the input that shows it
    _deFieldId(path) {
        const p = String(path);
        if (p.startsWith('about.')) return `devEditAbout-${p.slice(6).split('.')[0]}`;
        if (p.startsWith('pq.')) return `devEditPq-${p.slice(3).split('.')[0]}`;
        const SH = window.DeviceShell;
        const sec = SH.sectionOf(p);
        const rel = p.startsWith(sec + '.') ? p.slice(sec.length + 1) : p;
        if (sec === 'identity') return rel.startsWith('type') || rel.startsWith('kind') ? 'devEditType' : 'ddvName';
        if (sec === 'model') return 'ddvTemplate';
        if (sec === 'polling') {
            if (rel.startsWith('poll_groups.')) return `ddvPg-${rel.slice('poll_groups.'.length).split('.')[0]}`;
            if (rel.startsWith('poll_groups')) return document.querySelector('#devEditContent .ddv-pg')?.id || null;
            return 'ddvEnabled';
        }
        if (sec === 'outputs') {
            const el = document.querySelector(`#devEditContent [data-draft-path="outputs.${CSS.escape(rel)}"]`)
                || document.querySelector(`#devEditContent [data-draft-path^="outputs.${CSS.escape(rel.split('.').slice(0, 2).join('.'))}"]`);
            return el ? el.id || null : null;
        }
        return null;
    },

    // ── 412: the edits kept, re-applied over the version stored now ──────
    async _deRebase(opts = {}) {
        const ed = this._devEdit;
        const SH = window.DeviceShell;
        const CM = window.ConnformModel;
        if (!ed || !ed.open) return;
        const t = (k, d, p) => this.t(k, d, p);
        const live = () => ed.open && this._devEdit === ed;
        const read = await this._deRead(ed.id, null);
        if (!live()) return;
        if (!read.read) {
            ed.shell.showConflict(`<div class="dev-shell-conflictbox"><i aria-hidden="true" class="bi bi-exclamation-triangle"></i>
                <p>${this._esc(t('devedit.conflictUnread', 'This device was changed elsewhere, and it could not be read again. Nothing was saved; your edits are still here.'))}</p></div>`);
            return;
        }
        const { entry, groups } = read;
        const typeChoices = this._deTyped(entry) ? await this._typeChoices(entry) : ed.typeChoices;
        if (!live()) return;
        // what is typed is read now, after every wait (nothing typed meanwhile is lost)
        const form = (this._cf || {}).ddv;
        let connMine = null, connWas = null;
        if (form && ed.hasConn) {
            form.read();
            connMine = JSON.parse(JSON.stringify(form.M));
            connMine.keepalive = form.M.keepalive;
            connWas = JSON.parse(JSON.stringify(form.orig));
            connWas.keepalive = form.orig.keepalive;
        }
        ed.rebases++;
        ed.entry = entry;
        ed.groups = groups;
        // attributes that could not be read again: the ones read before stay
        // (their edits are never dropped)
        ed.attrs = read.attrs || ed.attrs;
        ed.pq = read.pq;
        const rb = SH.rebase(ed.draft, SH.fieldsOf(ed.entry, groups, ed.attrs ? ed.attrs.attributes : undefined, ed.pq));
        ed.draft = rb.draft;
        ed.conflicts = rb.conflicts;
        ed.connConflicts = [];
        if (connMine) {
            const now = CM.model(ed.entry);
            const rm = SH.rebaseModel(connMine, connWas, now);
            rm.M.keepalive = 'keepalive' in rm.M ? rm.M.keepalive : now.keepalive;
            ed.connM = rm.M;
            ed.connOrig = now;
            ed.connConflicts = rm.conflicts;
        }
        // typed after the save, or the bus line set from here: not a conflict
        // (a Save that stopped half-way: a conflict only where it was a 412)
        if (opts.saved || opts.busLine || (opts.partial && !opts.keepConflicts)) { ed.conflicts = []; ed.connConflicts = []; }
        ed.rebased = true;
        ed.typeChoices = typeChoices;
        const at = ed.section;
        // drawn again over the stored version: the focus stays where it was
        // (a 412 often answers the dry run of what is being typed)
        const focusId = document.getElementById('devEditContent')?.contains(document.activeElement) ? document.activeElement.id : '';
        this._deDrawSections();
        ed.shell.select(at);
        if (focusId) document.getElementById(focusId)?.focus();
        const { marked, unplaced } = this._deMarkConflicts();
        const said = v => (typeof v === 'boolean' ? (v ? '✓' : '✕') : this._ddvValText(v));
        ed.shell.showConflict(opts.partial
            ? this._dePartialHtml(opts.partial, marked)
            : opts.busLine
            ? `<div class="dev-shell-conflictbox"><i aria-hidden="true" class="bi bi-info-circle"></i>
                <p>${this._esc(t('devedit.busLineRead', 'The bus line was changed: this device was read again, with your other edits kept.'))}</p></div>`
            : opts.saved
            ? `<div class="dev-shell-conflictbox"><i aria-hidden="true" class="bi bi-info-circle"></i>
                <p><b>${this._esc(t('devedit.savedMoreTitle', 'Saved.'))}</b> ${this._esc(t('devedit.savedMore',
                    'A value changed while it was saving is kept here, not saved yet — review, then Save again.'))}</p></div>`
            : `<div class="dev-shell-conflictbox"><i aria-hidden="true" class="bi bi-exclamation-triangle"></i>
            <p><b>${this._esc(t('ddv.conflict.title', 'This device was changed elsewhere since you opened it.'))}</b>
            ${this._esc(marked
                ? t('devedit.conflictMarked', 'Your edits are kept and re-applied to the version stored now; {n} of them differ from it (marked “Stored now”). Nothing was saved — review, then Save again.', { n: marked })
                : t('devedit.conflictKept', 'Your edits are kept and re-applied to the version stored now. Nothing was saved — review, then Save again.'))}</p>
            ${unplaced.length ? `<ul class="dev-shell-unplaced">${unplaced.map(u => `<li><code>${this._esc(u.path)}</code> — ${this._esc(
                t('ddv.conflict.keptField', 'Your edit, kept. Stored now: {value}', { value: said(u.stored) }))}</li>`).join('')}</ul>` : ''}</div>`);
        await this._deDryRun();
    },

    // each field both changed: "Your edit, kept. Stored now: X" beside it
    _deMarkConflicts() {
        const ed = this._devEdit;
        const t = (k, d, p) => this.t(k, d, p);
        const form = (this._cf || {}).ddv;
        const said = v => (typeof v === 'boolean' ? (v ? '✓' : '✕') : this._ddvValText(v));
        const CONN_PATH = { mode: 'connection.protocol', bus_mode: 'connection.mode', mqtt_port: 'connection.port' };
        const marks = ed.conflicts.map(c => ({ path: c.path, id: this._deFieldId(c.path), stored: c.stored }))
            .concat(ed.connConflicts.map(c => ({ path: `connection.${c.key}`, stored: c.stored,
                id: form ? form.fieldId(CONN_PATH[c.key] || `connection.${c.key}`) : null })));
        // counted only where marked; what has no field on screen is listed in the banner
        let n = 0;
        const unplaced = [];
        for (const m of marks) {
            const el = m.id && document.getElementById(m.id);
            if (!el) { unplaced.push(m); continue; }
            n++;
            if (document.getElementById(`${m.id}-kept`)) continue;      // marked already (a redraw marks again)
            el.classList.add('ddv-kept');
            const note = document.createElement('span');
            note.className = 'field-hint ddv-kept-note';
            note.id = `${m.id}-kept`;
            note.textContent = t('ddv.conflict.keptField', 'Your edit, kept. Stored now: {value}', { value: said(m.stored) });
            (el.closest('.form-group') || el.parentElement).appendChild(note);
            el.setAttribute('aria-describedby', [el.getAttribute('aria-describedby'), note.id].filter(Boolean).join(' '));
        }
        return { marked: n, unplaced };
    },

    // ── Save ─────────────────────────────────────────────────────────────
    async devEditSave() {
        const ed = this._devEdit;
        const SH = window.DeviceShell;
        if (!ed || !ed.open || ed.saving || ed.busy) return;
        const t = (k, d, p) => this.t(k, d, p);
        // from the final check to the gateway's answer nothing can be typed
        // (what is sent is what is on screen) and the dialog cannot close
        this._deBusy(true);
        const go = await this._deSaveCheck();
        if (!go) { if (this._devEdit === ed) this._deBusy(false); return; }
        const { dry, body } = go;
        // a change that needs a look first: listed, in the page's own dialog
        // (a change the page says itself names its field by its draft path)
        const judged = (dry.changes || []).map(c => (String(c.field).startsWith(`${c.section}.`) ? { ...c, field: String(c.field).slice(c.section.length + 1) } : c));
        const risks = SH.safety(judged, { protocol: (ed.conn || {}).protocol || ed.entry.protocol,
                                          opcuaOn: ed.entry.opcua_enabled === true });
        if (risks.length) {
            const ok = await this.confirmAction({
                title: t('devedit.reviewTitle', 'Review before saving'),
                message: t('devedit.reviewMsg', 'These changes affect what this gateway does on a bus, or who gets its values:'),
                details: risks.map(r => this._deRiskText(r)),
                confirmLabel: SH.saveLabel(dry, t), danger: true,
            });
            if (!ok || !ed.open) { if (this._devEdit === ed) this._deBusy(false); return; }
        }
        ed.saving = true;
        const modal = document.getElementById('devEditModal');
        this._deRender();
        // one Save, its writes in order (DeviceShell.savePlan): each runs only
        // when the one before it was saved
        const sent = this._deSnapshot();
        const plan = SH.savePlan(ed.draft, ed.entry, { connection: ed.conn });
        const results = {};
        for (const step of plan) {
            results[step.id] = await this._deSaveStep(ed, step, body);
            if (!results[step.id].ok) break;
        }
        const rep = SH.saveReport(plan, results);
        const devAns = (results.device || {}).data || {};
        ed.saving = false;
        if (!ed.open) {                       // left meanwhile (the page went): what happened is said
            if (rep.saved.length && this._devDetail?.id === ed.id) await this._ddvRedraw(ed.id);
            if (rep.failed) {
                const f = results[rep.failed.id];
                this.showToast('error', t('toast.saveFailed', 'Save Failed'),
                               `${ed.entry.name || ed.id}: ${f.status === 412 ? t('ddv.conflict.title', 'This device was changed elsewhere since you opened it.') : rep.failed.error}`);
            }
            return;
        }
        this._deBusy(false);
        modal._keepOpen = null;
        if (rep.failed && !rep.saved.length) {
            // the first write refused: nothing saved — said where it shows
            const f = results[rep.failed.id];
            if (f.status === 412) { await this._deRebase(); return; }
            const err = this._deErr({ status: f.status }, f.data || {});
            if (f.status === 428) {
                err.errors = [t('devedit.noTag', 'The device could not be read with its version: close this, open the device again, and save.')];
            } else if (rep.failed.id !== 'device' && !err.errors.length && !Object.keys(err.fields).length) {
                err.errors = [`${this._deStepLabel(rep.failed.id)}: ${rep.failed.error}`];
            }
            ed.dry = { error: this._deStepFields(rep.failed.id, err) };
            this._deRender();
            this._deFocusErrors();
            return;
        }
        if (rep.failed) {
            // half-way: what was saved is said as saved, what was not stays
            // here over the version stored now (a 412 marks the fields)
            const f = results[rep.failed.id];
            this.showToast('warning', t('devedit.partialTitle', 'Saved in part'), this._dePartialText(rep));
            await this._deRebase({ partial: rep, keepConflicts: f.status === 412 });
            // the refusal at its field, as a refusal of the first write is
            // (review of L3.11b, m6); a 412 marks its fields instead
            if (f.status !== 412 && this._devEdit === ed && ed.open) {
                const err = this._deStepFields(rep.failed.id, this._deErr({ status: f.status }, f.data || {}));
                if (Object.keys(err.fields || {}).length) {
                    // kept while those fields hold what was refused (a later
                    // check of what is typed says it again, never drops it)
                    const paths = Object.keys(err.fields);
                    ed.stepErr = { fields: err.fields, was: JSON.stringify(paths.map(p => ed.draft.cur[p])), paths };
                    await this._deDryRun();
                }
            }
            return;
        }
        const said = devAns.status === 'unchanged' ? t('devices.unchanged', 'Nothing changed — nothing was restarted.')
            : devAns.restarted ? t('devices.savedRestarted', 'Saved — restarted') : t('devices.savedKept', 'Saved — kept running');
        // a guard: what the dialog holds now is what was sent — else (a
        // value set while saving) it stays open, over the version just stored
        if (this._deSnapshot() !== sent) {
            this.showToast('success', t('devices.updated', 'Device updated'), said);
            await this._deRebase({ saved: true });
            return;
        }
        ed.closing = true;
        this.closeModal('devEditModal');
        this.showToast('success', t('devices.updated', 'Device updated'), said);
        await this._ddvRedraw(ed.id);
    },

    // what the dialog holds: the draft and the connection form's part
    _deSnapshot() {
        const ed = this._devEdit;
        if (!ed) return '';
        const b = this._deBody();
        return JSON.stringify([ed.draft.cur, b.body, b.conn]);
    },

    // the words for one write of a Save
    _deStepLabel(id) {
        const t = (k, d) => this.t(k, d);
        return ({
            device: t('devedit.step.device', 'its settings'),
            unit: t('devedit.step.unit', 'its name'),
            component: t('devedit.step.component', 'its name and on/off'),
            outputs: t('devedit.step.outputs', 'the installation\'s outputs'),
            attributes: t('devedit.step.attributes', 'its description, labels and nameplate'),
            pq: t('devedit.step.pq', 'the PQ event recorder'),
        })[id] || id;
    },

    _dePartialText(rep) {
        const t = (k, d, p) => this.t(k, d, p);
        const L = ids => ids.map(id => this._deStepLabel(id)).join(', ');
        return t('devedit.partial', 'Saved: {saved}. Not saved: {failed} — {error}. What was not saved is still here: Save again.',
                 { saved: L(rep.saved), failed: L([rep.failed.id, ...rep.skipped]), error: rep.failed.error });
    },

    _dePartialHtml(rep, marked) {
        const t = (k, d, p) => this.t(k, d, p);
        return `<div class="dev-shell-conflictbox" id="devEditPartial"><i aria-hidden="true" class="bi bi-exclamation-triangle"></i>
            <p><b>${this._esc(t('devedit.partialTitle', 'Saved in part'))}</b> ${this._esc(this._dePartialText(rep))}
            ${marked ? this._esc(t('devedit.partialMarked', '{n} of the fields not saved were changed elsewhere meanwhile (marked “Stored now”).', { n: marked })) : ''}</p></div>`;
    },

    // a refusal of a write that is not the PATCH, keyed by the draft's paths
    // (at its field, listed in the summary)
    _deStepFields(id, err) {
        const pfx = { attributes: 'about', pq: 'pq', unit: 'identity', component: 'identity' }[id];
        if (!pfx) return err;
        const fields = {};
        for (const [k, v] of Object.entries(err.fields || {})) {
            const key = String(k).replace(/^identity\./, '');
            fields[id === 'unit' || id === 'component' ? (key === 'enabled' ? 'polling.enabled' : `identity.${key}`) : `${pfx}.${key}`] = v;
        }
        // an attribute's error names `identity.<field>` (the category)
        const errors = (err.errors || []).map(e => {
            const m = id === 'attributes' && /^identity\.(\w+):\s*(.*)$/.exec(String(e));
            if (m) { fields[`about.${m[1]}`] = m[2]; return null; }
            return e;
        }).filter(Boolean);
        return { errors, fields };
    },

    // one write of a Save: {ok, status, data, error}
    async _deSaveStep(ed, step, body) {
        const run = {
            device: () => this._deSaveDevice(ed, body),
            unit: () => this._deSaveUnit(ed, step),
            component: () => this._deSaveComponent(ed, step),
            outputs: () => this._deSaveOutputs(ed, step),
            attributes: () => this._deSaveAttributes(ed, step),
            pq: () => this._deSavePq(ed, step),
        }[step.id];
        if (!run) return { ok: false, status: 0, data: {}, error: step.id };
        try {
            return await run();
        } catch (e) {
            return { ok: false, status: 0, data: { detail: { errors: [String(e.message || e)] } }, error: String(e.message || e) };
        }
    },

    async _deAnswer(r) {
        const data = await r.json().catch(() => ({}));
        return { ok: r.ok, status: r.status, data, error: r.ok ? '' : this._respError(r, data) };
    },

    // the device's own: one PATCH, as its dry run checked it (ADR-0030)
    async _deSaveDevice(ed, body) {
        return this._deAnswer(await window.DeviceStore.devWrite(ed.id, 'PATCH', `/api/devices/${encodeURIComponent(ed.id)}`, body));
    },

    // a unit: its name, by the unit route, checked against the unit's tag
    async _deSaveUnit(ed, step) {
        const ep = ed.entry.endpoint_id;
        return this._deAnswer(await window.DeviceStore.devWrite(ed.id, 'PATCH',
            `/api/endpoints/${encodeURIComponent(ep)}/units/${encodeURIComponent(ed.id)}`, step.body));
    },

    // a component: its name and on/off, by its device's route (the device's
    // entry holds it)
    async _deSaveComponent(ed, step) {
        const parent = ed.entry.component_of;
        return this._deAnswer(await window.DeviceStore.devWrite(parent, 'PATCH',
            `/api/devices/${encodeURIComponent(parent)}/components/${encodeURIComponent(ed.id)}`, step.body));
    },

    // a unit's installation outputs: each by its route, in order (for every
    // unit of the installation); a refusal stops there and says which
    async _deSaveOutputs(ed, step) {
        let last = { ok: true, status: 200, data: {}, error: '' };
        for (const w of step.writes || []) {
            last = await this._deAnswer(await window.DeviceStore.devWrite(ed.id, 'POST',
                `/api/devices/${encodeURIComponent(ed.id)}/${w.route}`, w.body));
            if (!last.ok) return { ...last, error: `${w.route}: ${last.error}` };
        }
        return last;
    },

    // the attributes' identity category, under the attributes' own revision
    async _deSaveAttributes(ed) {
        const stored = ((ed.attrs || {}).attributes || {}).identity;
        const pay = window.DeviceShell.attrPayload(ed.draft, stored);
        if (!pay) return { ok: true, status: 200, data: {}, error: '' };
        const headers = { 'If-Match': (ed.attrs || {}).rev || '' };
        if (pay.method === 'PUT') headers['Content-Type'] = 'application/json';
        const r = await fetch(`/api/attributes/devices/${encodeURIComponent(ed.id)}/identity`, {
            method: pay.method, headers, body: pay.method === 'PUT' ? JSON.stringify(pay.value) : undefined });
        const ans = await this._deAnswer(r);
        if (ans.ok && ed.attrs) ed.attrs.rev = ans.data.rev || ed.attrs.rev;
        (ans.data.warnings || []).forEach(w => this.showToast('warning', this.t('attr.warning', 'Check'), w));
        return ans;
    },

    // the PQ recorder: its whole settings, by its route (the device's tag)
    async _deSavePq(ed, step) {
        const ans = await this._deAnswer(await window.DeviceStore.devWrite(ed.id, 'POST',
            `/api/pq/config?device=${encodeURIComponent(ed.id)}`, step.body));
        if (ans.ok && this._devDetail?.id === ed.id && this._devDetail.entry) this._devDetail.entry.pq_recorder = ans.data.pq_recorder;
        return ans;
    },

    // Save's final check: the dry run of exactly what is on screen now
    // (never a stale one); {dry, body} to send, or null (nothing to save, a
    // refusal, a 412 rebased — each said where it shows)
    async _deSaveCheck() {
        const ed = this._devEdit;
        const SH = window.DeviceShell;
        let dry, tries = 0;
        do {
            const rebases = ed.rebases;
            dry = await this._deDryRun();
            if (ed.rebases !== rebases || !ed.open) return null;   // a 412: rebased, said
        } while (dry === undefined && ++tries < 3);
        if (dry === undefined) {                                   // superseded every time: said, not dropped
            ed.shell.setSummary(this.t('devedit.sum.superseded', 'Changed while it was being checked — press Save again.'), 'superseded');
            return null;
        }
        if (!dry || dry.pending) return null;                      // nothing to save (the footer says so)
        if (dry.error) { this._deFocusErrors(); return null; }
        if (SH.verdict(dry) === 'none') return null;
        return { dry, body: ed.body };
    },

    // busy (Save's final check through the gateway's answer): the sections
    // and the rail inert, the footer's controls held back, the dialog kept
    _deBusy(on) {
        const ed = this._devEdit;
        if (!ed || !ed.shell) return;
        ed.busy = !!on;
        for (const el of [ed.shell.el.main, document.getElementById('devEditRail'), ed.shell.el.railSel]) {
            if (el) el.inert = !!on;
        }
        for (const b of [ed.shell.el.save, ed.shell.el.cancel, ed.shell.el.close, ed.shell.el.show]) {
            if (!b) continue;
            if (on) b.setAttribute('aria-disabled', 'true');
            else if (b !== ed.shell.el.save) b.removeAttribute('aria-disabled');
        }
        const modal = document.getElementById('devEditModal');
        if (modal) modal._keepOpen = on ? () => !!(this._devEdit && this._devEdit.busy) : null;
        if (!on) this._deRender();
    },

    // the error summary: its first link focused (else the summary itself)
    _deFocusErrors() {
        const box = this._devEdit?.shell.el.errors;
        if (!box) return;
        const a = box.querySelector('a');
        if (a) { a.focus(); return; }
        box.tabIndex = -1;
        box.focus();
    },

    _deRiskText(r) {
        const t = (k, d, p) => this.t(k, d, p);
        const val = x => (x === undefined || x === null || x === '' ? '—' : String(x));
        if (r.kind === 'role') {
            const role = x => (x === 'rtu_tap' || x === 'listen' ? 'Listen-only' : 'Master');
            return t('devedit.risk.role', 'Role on the bus: {from} → {to}. Two masters on one bus collide; with none, nothing reads it.',
                     { from: role(r.from), to: role(r.to) });
        }
        if (r.kind === 'bus') return t('devedit.risk.bus', 'Moves to another bus ({field}: {from} → {to}): it leaves its bus and joins that one.',
                                       { field: r.field, from: val(r.from), to: val(r.to) });
        if (r.kind === 'template') return t('devedit.risk.template', 'Template {from} → {to}: the measurement map changes.',
                                            { from: val(r.from), to: val(r.to) });
        if (r.kind === 'acquisition-off') return t('devedit.risk.acqOff', 'Acquisition off: nothing is read, so every output and every rule that uses this device stops getting its values.');
        return t('devedit.risk.off', 'Output off ({field}): what reads it stops getting this device\'s values.', { field: r.field });
    },

    // ── closing ──────────────────────────────────────────────────────────
    _deClosed() {
        const ed = this._devEdit;
        if (!ed) return;
        ed.open = false;
        clearTimeout(ed.timer);
        this._outStage = null;
        this._outOpcuaChosen = false;
        if (this._cf) delete this._cf.ddv;
        const root = document.getElementById('devEditContent');
        if (root) root.innerHTML = '';
        this._devEdit = null;
        // after a 412 the stored version moved: the page shows it (closed
        // without saving too)
        if ((ed.rebased || ed.pageStale) && !ed.closing) this._ddvRedraw(ed.id);
    },

    // About (attributes): their own editor and revision — the modal closes
    // first (asking about unsaved edits)
    async devEditGoAbout() {
        if (await this._deLeave()) this.ddvEdit('about');
    },

    // a link out of the modal (About, Settings): it closes first, asking
    // about unsaved edits; true when it is closed (or was not open)
    async _deLeave() {
        const ed = this._devEdit;
        if (!ed || !ed.open) return true;
        const modal = document.getElementById('devEditModal');
        if (this._modalDirty(modal) && !await this._confirmDiscard()) return false;
        const redraw = ed.rebased || ed.pageStale;
        ed.closing = true;
        this.closeModal('devEditModal');
        // after a 412 (or a newer version read at open) the page is drawn
        // again from what is stored — before the link goes on, never over it
        if (redraw) await this._ddvRedraw(ed.id);
        return true;
    },
});
