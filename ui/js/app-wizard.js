/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// wizard domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    // ── Add/Edit Device wizard ──────────────────────────────────────────────
    _wizTopicFor(did) {
        const pat = this._devWiz?.topicPattern || 'mbg/devices/{device}';
        return pat.replaceAll('{device_id}', did).replaceAll('{device}', did);
    },

    // Add Device. A saved device is edited on its page (Configure →
    // Connection): the wizard only adds — one connection form, two places.
    async openDeviceWizard(editId = null, prefill = null) {
        if (editId) return this.openDeviceEdit(editId);
        await this._fetchDevices(true);
        let templates = [];
        try { templates = (await (await fetch('/api/device-templates')).json()).templates || []; }
        catch (e) { console.error(e); }
        if (this._loadBridges) await this._loadBridges();
        if (this._meterRoles) await this._meterRoles();     // the kinds a meter-only device may be
        // where a new device publishes when the field is left empty: the
        // gateway's pattern (Settings → MQTT), resolved by the server too
        // (loaded with the config at boot; asked again only when missing)
        let topicPattern = this.config?.mqtt?.default_topic_pattern || '';
        if (!topicPattern) {
            try { topicPattern = (await (await fetch('/api/config/mqtt')).json()).default_topic_pattern || ''; }
            catch (e) { /* the server applies its own pattern anyway */ }
        }
        topicPattern = topicPattern || 'mbg/devices/{device}';
        // a prefill (discovery, a bridge's "+ Device") says both how the device
        // is reached (the connection form's model) and what it is (the rest)
        const CONN = ['protocol', 'host', 'port', 'unit_id', 'timeout', 'url', 'bridge', 'bridge_port', 'serial_port',
                      'baudrate', 'parity', 'stopbits', 'bytesize', 'broker', 'mqtt_port', 'topic', 'mqtt_username',
                      'mqtt_password', 'mqtt_tls', 'udp', 'framing'];
        const conn = {}, rest = {};
        for (const [k, v] of Object.entries(prefill || {})) (CONN.includes(k) ? conn : rest)[k] = v;
        const cf = window.ConnformModel.blank(conn);
        // a device on a bus of a Modbus TCP gateway is a Modbus TCP device
        if (cf.bridge && this._bridgeType && this._bridgeType(
                (this._bridges || []).find(b => b.id === cf.bridge)?.type).framing === 'modbus_tcp') cf.mode = 'tcp';
        this._devWiz = {
            step: 1, templates, topicPattern, cf,
            data: Object.assign({
                id: '', name: '', template: '', enabled: true, protocol: cf.mode,
                topic_prefix: '', bucket: '', device_tag: '', ha_discovery_enabled: true,
            }, rest),
        };
        document.getElementById('devWizTitle').textContent = this.t('devices.wizard.titleAdd', 'Add Device');
        this._devWizRender();
        this.openModal('deviceWizardModal');
        // Clickable step rail — jump back to an already-seen step (no re-validation
        // needed going backward). onclick (not addEventListener) so re-opening the
        // wizard never stacks duplicate handlers.
        document.querySelectorAll('#devWizSteps .wizard-step').forEach(li => {
            li.tabIndex = 0;
            li.onclick = () => {
                const n = parseInt(li.dataset.step, 10);
                if (n < this._devWiz.step) { this._devWizCollect(); this._devWiz.step = n; this._devWizRender(); }
            };
            li.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); li.click(); } };
        });
    },

    _devWizRender() {
        const w = this._devWiz;
        document.querySelectorAll('#devWizSteps .wizard-step').forEach(s => {
            const n = parseInt(s.dataset.step, 10);
            s.classList.toggle('active', n === w.step);
            s.classList.toggle('done', n < w.step);
            if (n === w.step) s.setAttribute('aria-current', 'step');
            else s.removeAttribute('aria-current');       // SR announces the active step
        });
        document.getElementById('devWizBack').style.visibility = w.step === 1 ? 'hidden' : 'visible';
        document.getElementById('devWizNextLabel').textContent = w.step === 3
            ? this.t('devices.wizard.create', 'Create device')
            : this.t('common.next', 'Next');
        const fb = document.getElementById('devWizFeedback');
        if (fb) { fb.textContent = ''; fb.className = 'save-feedback'; }
        const body = document.getElementById('devWizBody');
        body.innerHTML = w.step === 1 ? this._devWizStep1Html()
                       : w.step === 2 ? (w.data.protocol === 'none' ? this._devWizMetersStepHtml() : this._devWizStep2Html())
                       : this._devWizStep3Html();
        // step 2's name follows what it asks
        const s2 = document.querySelector('#devWizSteps [data-step="2"] [data-i18n]');
        if (s2) {
            const k = w.data.protocol === 'none' ? 'wizMtr.step2' : 'devices.wizard.step2';
            s2.setAttribute('data-i18n', k);           // a language switch keeps it
            s2.textContent = this.t(k, w.data.protocol === 'none' ? 'Meters & kind' : 'Template');
        }
        this._devWizWire();
        // Move focus to the first field of the step (keyboard + screen-reader
        // users land where they can act; setTimeout lets the modal become visible
        // on the initial open before we focus).
        setTimeout(() => body.querySelector(
            'input:not([disabled]), select:not([disabled]), textarea')?.focus(), 0);
    },

    // step 1 is the connection form (app-connform.js), mounted by _devWizWire
    _devWizStep1Html() {
        return '<div id="devWizConn" class="cf-host"></div>';
    },

    // Sensor preset chips (step 1, MQTT): fill the subscribe-topic pattern for
    // the bridge and preselect the matching template for step 2. The user
    // replaces the <placeholder> with their device's friendly name / MAC.
    _devWizMqttPreset(templateId, topicPattern) {
        const inp = document.getElementById('devWizMqttTopic');
        if (inp && !inp.value.trim()) inp.value = topicPattern;
        if (this._devWiz) this._devWiz.data.template = templateId;
        if (this._devWiz && inp) this._devWiz.cf.topic = inp.value;
        inp?.focus();
        inp?.select?.();
    },

    _devWizStep2Html() {
        const w = this._devWiz;
        const t = (k, d, p) => this.t(k, d, p);
        // Only templates whose transport matches the chosen protocol — a Modbus
        // map can't be read over HTTP (and vice versa). Drop a now-incompatible
        // selection so the picker + validation stay honest.
        const cls = this._devWizTplClass();
        const compatible = w.templates.filter(x => (x.transport || 'modbus') === cls);
        if (w.data.template && !compatible.some(x => x.id === w.data.template)) w.data.template = '';
        // what kind of equipment: the chips that put its templates first
        const kinds = [...new Set(compatible.map(x => x.device_kind || x.kind || 'other'))]
            .sort((a, b) => this._KIND_ORDER.indexOf(a) - this._KIND_ORDER.indexOf(b));
        const kindChips = kinds.length > 1 ? `
            <div class="tplp-kinds" role="group" aria-label="${this._esc(t('devices.wizard.tplKindQ', 'What kind of equipment'))}">
                <span class="cbx-facets-label">${this._esc(t('find.facet.kind', 'Kind'))}</span>
                ${kinds.map(k => `<button type="button" class="find-chip" data-tplp-kind="${this._esc(k)}" aria-pressed="${w.tplKind === k}">
                    <i aria-hidden="true" class="bi ${this._KIND_ICON[k] || this._KIND_ICON.other}"></i> ${this._esc(this._kindLabel(k))}</button>`).join('')}
            </div>` : '';
        const empty = `<span class="field-hint">${this._esc(t('devices.wizard.noTplForProto', 'No {proto} templates yet — upload or create one for this protocol.').replace('{proto}', cls === 'http' ? 'HTTP/JSON' : cls === 'mqtt' ? 'MQTT' : 'Modbus'))}</span>`;
        return `
        <div class="wiz-eyebrow">${t('devices.wizard.tplQ', 'What is it?')}</div>
        <div class="tplp-toolbar">
            <div class="tplp-search"><i aria-hidden="true" class="bi bi-search"></i>
                <input type="text" id="devWizTplSearch" class="input" aria-label="${this._esc(t('devices.wizard.tplSearch', 'Search templates by name, vendor or model'))}"
                       placeholder="${this._esc(t('devices.wizard.tplSearchPh', 'Search name, vendor, model…'))}"></div>
            <button class="btn btn-secondary btn-sm" data-action="tplUpload">
                <i aria-hidden="true" class="bi bi-upload"></i> ${t('devtpl.upload', 'Upload template')}</button>
            <button class="btn btn-secondary btn-sm" ${this._act('openTplEditor', [null])}>
                <i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('devtpl.new', 'New template')}</button>
        </div>
        ${kindChips}
        <p class="field-hint" style="margin:6px 0;">${t('devices.wizard.tplIntro2', 'The template is the measurement map of the equipment: pick one, upload a file, or create your own.')}</p>
        <div class="tpl-pick-list tplp" id="devWizTplList">${compatible.length ? '' : empty}</div>
        <div class="tplp-actions" id="devWizTplActions"></div>
        <div id="devWizTplNotes">${this._tplNotesHtml((compatible.find(x => x.id === w.data.template) || {}).notes)}</div>
        <div class="field-error" id="devWizTplError" style="display:none;">${t('devices.wizard.tplRequired', 'Choose a template to continue.')}</div>`;
    },

    _devWizTplClass() {
        const p = this._devWiz.data.protocol;
        return p === 'http' ? 'http' : p === 'mqtt' ? 'mqtt' : 'modbus';
    },

    // the template list: the shared picker, always open — search, the kind
    // chips (its templates first, the others folded), the vendor chips;
    // the actions under it act on the template chosen
    _devWizTplPanel() {
        const w = this._devWiz;
        const host = document.getElementById('devWizTplList');
        const search = document.getElementById('devWizTplSearch');
        if (!host || !search) return;
        const t = (k, d, p) => this.t(k, d, p);
        const cls = this._devWizTplClass();
        const compatible = w.templates.filter(x => (x.transport || 'modbus') === cls);
        if (!compatible.length) return;
        // Sensor presets: picking a bridge-backed sensor template prefills
        // the (still empty) subscribe topic with the bridge's documented
        // pattern — the user replaces the <placeholder> with their device.
        const TOPIC_PRESETS = {
            zigbee2mqtt_sensor: 'zigbee2mqtt/<friendly_name>',
            ble_theengs_sensor: 'home/TheengsGateway/BTtoMQTT/<MAC>',
        };
        const items = () => compatible.map(x => {
            const k = x.device_kind || x.kind || 'other';
            const fits = !w.tplKind || k === w.tplKind;
            return {
                value: x.id, label: x.name || x.id, chip: x.id, kind: k, facet: x.vendor || t('pick.noVendor', 'No vendor'),
                group: w.tplKind ? t('pick.recommended', 'For {kind}', { kind: this._kindLabel(w.tplKind) }) : this._kindLabel(k),
                more: !fits,
                sub: [[x.vendor, x.model].filter(Boolean).join(' '),
                      (x.used_by || []).length ? `${t('devtpl.usedBy', 'used by')} ${x.used_by.join(', ')}` : ''].filter(Boolean).join(' · '),
                aside: `${x.builtin ? t('devices.builtin', 'built-in') : t('devices.community', 'user')} · ${this.tn('pick.nMeasurements', x.registers || 0, '{n} measurements')}${x.version ? ` · v${x.version}` : ''}`,
                warn: (x.notes || []).some(n => n.level === 'warning') ? t('tplNotes.hasWarning', 'This template has a note to read') : '',
                search: `${x.vendor || ''} ${x.model || ''} ${(this._KIND_WORDS || {})[k] || ''}`,
                attrs: { 'data-tpl': x.id },
            };
        }).sort((a, b) => (a.more - b.more) || (this._KIND_ORDER.indexOf(a.kind) - this._KIND_ORDER.indexOf(b.kind))
            || a.label.localeCompare(b.label));
        const c = this._cbxNew(search, {
            inline: true, optClass: 'tpl-pick',
            listLabel: t('devices.wizard.tplList', 'Templates'),
            current: () => w.data.template,
            facet: { label: t('find.facet.vendor', 'Vendor') },
            moreGroup: t('pick.otherKinds', 'Other kinds'),
            moreLabel: n => this.tn('pick.showOtherKinds', n, 'Show {n} templates of other kinds…'),
            onPick: it => {
                w.data.template = it.value;
                if (w.cf.mode === 'mqtt' && !(w.cf.topic || '').trim() && TOPIC_PRESETS[it.value]) {
                    w.cf.topic = TOPIC_PRESETS[it.value];
                }
                const err = document.getElementById('devWizTplError');
                if (err) err.style.display = 'none';
                this._cbxRender(c);
                this._devWizTplAfterPick(compatible);
            },
        });
        host.append(c.pop, c.live);
        c.items = items();
        this._cbxRender(c);
        search.addEventListener('input', () => { c.q = search.value; c.activeValue = null; this._cbxRender(c); });
        document.querySelectorAll('[data-tplp-kind]').forEach(b => b.addEventListener('click', () => {
            w.tplKind = w.tplKind === b.dataset.tplpKind ? '' : b.dataset.tplpKind;
            document.querySelectorAll('[data-tplp-kind]').forEach(x => x.setAttribute('aria-pressed', String(x.dataset.tplpKind === w.tplKind)));
            c.more = false;
            c.items = items();
            this._cbxRender(c);
        }));
        this._devWizTplAfterPick(compatible);
    },

    // what the chosen template's toolbar and notes say
    _devWizTplAfterPick(compatible) {
        const w = this._devWiz;
        const t = (k, d) => this.t(k, d);
        const x = compatible.find(y => y.id === w.data.template);
        const bar = document.getElementById('devWizTplActions');
        if (bar) {
            bar.innerHTML = x ? `<span class="field-hint">${this._esc(x.name || x.id)}:</span>
                ${x.builtin
                    ? `<button type="button" class="btn btn-ghost btn-sm" ${this._act('openTplEditor', [x.id, true])}><i aria-hidden="true" class="bi bi-copy"></i> ${t('devtpl.duplicate', 'Duplicate to edit')}</button>`
                    : `<button type="button" class="btn btn-ghost btn-sm" ${this._act('openTplEditor', [x.id])}><i aria-hidden="true" class="bi bi-pencil"></i> ${t('common.edit', 'Edit')}</button>`
                      + ((x.used_by || []).length ? '' : `<button type="button" data-admin class="btn btn-ghost btn-sm" ${this._act('tplDelete', [x.id])}><i aria-hidden="true" class="bi bi-trash"></i> ${t('common.delete', 'Delete')}</button>`)}
                <button type="button" class="btn btn-ghost btn-sm" ${this._act('tplExport', [x.id])}><i aria-hidden="true" class="bi bi-download"></i> ${t('common.export', 'Export')}</button>` : '';
        }
        const notes = document.getElementById('devWizTplNotes');
        if (notes) notes.innerHTML = this._tplNotesHtml((x || {}).notes);
    },

    _devWizStep3Html() {
        const d = this._devWiz.data;
        // a new device: its id and routing are chosen here, and fixed once
        // it exists (they name its topics and its history)
        const idLocked = false, routeLocked = false, routeNote = '';
        return `
        <div class="wiz-eyebrow">${this.t('devices.wizard.routeQ', 'Where does its data go?')}</div>
        <div class="form-row">
            <div class="form-group flex-2">
                <label class="form-label" for="devWizName">${this.t('devices.wizard.name', 'Device name')}</label>
                <input type="text" id="devWizName" class="input" value="${this._esc(d.name)}" placeholder="Warehouse EM24">
            </div>
            <div class="form-group">
                <label class="form-label" for="devWizId">${this.t('devices.wizard.id', 'Device id')}</label>
                <input type="text" id="devWizId" class="input" value="${this._esc(d.id)}" ${idLocked ? 'disabled' : ''}
                       pattern="[a-z0-9][a-z0-9_-]{1,63}" placeholder="em24-hala">
                <div class="field-hint">${idLocked ? this.t('devices.wizard.idLocked', 'Fixed after creation (used in topics).')
                                                   : this.t('devices.wizard.idHint', 'a-z 0-9 - _ · used in MQTT/InfluxDB routing')}</div>
                <div class="field-error" id="devWizIdError" style="display:none;"></div>
            </div>
        </div>
        <div class="form-row">
            <div class="form-group flex-2">
                <label class="form-label" for="devWizTopic">${this.t('devices.wizard.topicPrefix', 'MQTT topic prefix')}</label>
                <input type="text" id="devWizTopic" class="input" value="${this._esc(d.topic_prefix)}" placeholder="${this._esc(this._wizTopicFor(d.id || 'em24-hala'))}" ${routeLocked ? 'disabled' : ''}>
                ${routeNote}
            </div>
            <div class="form-group">
                <label class="form-label" for="devWizBucket">${this.t('devices.wizard.bucket', 'InfluxDB bucket')}</label>
                <input type="text" id="devWizBucket" class="input" value="${this._esc(d.bucket)}" placeholder="meters" ${routeLocked ? 'disabled' : ''}>
                <div class="field-hint">${this.t('devices.wizard.bucketHint', 'Auto-created (90d retention) if missing.')}</div>
            </div>
            <div class="form-group">
                <label class="form-label" for="devWizTag">${this.t('devices.wizard.deviceTag', 'Influx device tag')}</label>
                <input type="text" id="devWizTag" class="input" value="${this._esc(d.device_tag)}" placeholder="(= id)" ${routeLocked ? 'disabled' : ''}>
            </div>
        </div>
        <div class="topic-preview" id="devWizTopicPreview"></div>
        <div style="display:flex;flex-direction:column;gap:2px;margin-top:15px;">
        <label class="wiz-tog">
            <input type="checkbox" id="devWizHaDisc" ${d.ha_discovery_enabled ? 'checked' : ''}><span class="wiz-switch"></span>
            <span>${this.t('devices.wizard.haDiscovery', 'Publish Home Assistant MQTT discovery for this device')}</span>
        </label>
        <label class="wiz-tog">
            <input type="checkbox" id="devWizEnabled" ${d.enabled ? 'checked' : ''}><span class="wiz-switch"></span>
            <span>${this.t('devices.wizard.enabled', 'Start polling immediately after saving')}</span>
        </label></div>`;
    },

    _devWizWire() {
        const w = this._devWiz;
        // Inline validation feedback: clear a field's error state the moment the
        // user starts fixing it, and let Enter advance the wizard (novice-friendly).
        // Attached to the freshly-rendered inputs each render, so no listener leak.
        document.getElementById('devWizBody')?.querySelectorAll('.input').forEach(inp => {
            inp.addEventListener('input', () => {
                inp.classList.remove('invalid');
                inp.removeAttribute('aria-invalid');
                document.getElementById(inp.id + 'Error')?.style.setProperty('display', 'none');
            });
            if (w.step !== 2) inp.addEventListener('keydown', e => {   // step 2 = template pick
                if (e.key === 'Enter') { e.preventDefault(); this.devWizardNext(); }
            });
        });
        if (w.step === 1) {
            this._cfMount(document.getElementById('devWizConn'), 'devWiz', w.cf, {
                context: 'add', allowNone: true,
                template: () => w.data.template,
                onChange: f => this._devWizConnChanged(f),
            });
        } else if (w.step === 2 && w.data.protocol === 'none') {
            this._metersRowsMount('wizMtr', {});
            document.querySelectorAll('input[name="wizMtrKind"]').forEach(r => r.addEventListener('change', () => {
                document.querySelectorAll('#wizMtrKinds .seg-btn').forEach(l => l.classList.toggle('on', l.querySelector('input').checked));
            }));
        } else if (w.step === 2) {
            this._devWizTplPanel();
        } else if (w.step === 3) {
            const name = document.getElementById('devWizName');
            const id = document.getElementById('devWizId');
            const topic = document.getElementById('devWizTopic');
            const preview = () => {
                const did = id.value.trim() || 'device-id';
                const p = topic.value.trim() || this._wizTopicFor(did);
                const tag = document.getElementById('devWizTag')?.value.trim() || did;
                document.getElementById('devWizTopicPreview').innerHTML =
                    `<div class="tp-row"><span class="tp-node">${this._esc(did)}</span>` +
                    `<span class="tp-arrow">→</span>` +
                    `<span class="tp-topic">${this._esc(p)}/voltage/l1_n</span></div>` +
                    `<div class="tp-row tp-sub"><span class="tp-dim">${this.t('devices.wizard.influxTag', 'Influx tag')}</span> ` +
                    `<span class="tp-topic">${this._esc(tag)}</span></div>`;
            };
            name?.addEventListener('input', () => {
                if (!id.dataset.touched) {
                    id.value = name.value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32);
                    preview();
                }
            });
            id?.addEventListener('input', () => { id.dataset.touched = '1'; preview(); });
            topic?.addEventListener('input', preview);
            document.getElementById('devWizTag')?.addEventListener('input', preview);
            preview();
        }
    },

    // the connection form redrew (a mode, a bridge): step 2 is named after
    // what it will ask, and Enter on any of its inputs moves the wizard on
    _devWizConnChanged(f) {
        const w = this._devWiz;
        w.cf = f.M;
        w.data.protocol = f.M.mode;
        const s2 = document.querySelector('#devWizSteps [data-step="2"] [data-i18n]');
        if (s2) {
            const k = f.M.mode === 'none' ? 'wizMtr.step2' : 'devices.wizard.step2';
            s2.setAttribute('data-i18n', k);
            s2.textContent = this.t(k, f.M.mode === 'none' ? 'Meters & kind' : 'Template');
        }
        f.host.querySelectorAll('.input').forEach(inp => inp.addEventListener('keydown', e => {
            if (e.key === 'Enter') { e.preventDefault(); this.devWizardNext(); }
        }));
    },

    _devWizCollect() {
        const w = this._devWiz, d = w.data;
        const g = id => document.getElementById(id);
        if (w.step === 2 && d.protocol === 'none') {
            d.kind = document.querySelector('input[name="wizMtrKind"]:checked')?.value || d.kind || '';
            d.meters = this._metersRowsRead('wizMtr');
            return;
        }
        if (w.step === 1) {
            const f = (this._cf || {}).devWiz;
            if (f) w.cf = f.read().model;
            d.protocol = w.cf.mode;
            if (d.protocol === 'none') d.template = '';
        } else if (w.step === 3) {
            d.name = g('devWizName')?.value.trim() ?? d.name;
            d.id = g('devWizId')?.value.trim() ?? d.id;
            d.topic_prefix = g('devWizTopic')?.value.trim() ?? d.topic_prefix;
            d.bucket = g('devWizBucket')?.value.trim() ?? d.bucket;
            d.device_tag = g('devWizTag')?.value.trim() ?? d.device_tag;
            d.ha_discovery_enabled = !!g('devWizHaDisc')?.checked;
            d.enabled = !!g('devWizEnabled')?.checked;
        }
    },

    _wizInvalid(id) {
        const el = document.getElementById(id);
        if (el) {
            el.classList.add('invalid');
            el.setAttribute('aria-invalid', 'true');
            el.focus();                    // take the user straight to the problem field
        }
        return false;
    },

    _devWizValidate() {
        const w = this._devWiz, d = w.data;
        if (w.step === 1 && d.protocol !== 'none') {
            const f = (this._cf || {}).devWiz;
            const errs = f ? window.ConnformModel.errors(w.cf, (k, x) => this.t(k, x)) : {};
            if (Object.keys(errs).length) {
                const left = f.showErrors(errs);
                this._wizSay(left.length ? left.join(' · ') : this.t('devices.fixMarked', 'Fix the marked fields.'));
                return false;
            }
        }
        if (w.step === 2 && d.protocol === 'none') {
            const f = {};
            if (!d.kind) f.kind = this.t('wizMtr.needKind', 'Say what it is');
            if (!(d.meters || []).length) f['meters[0].device'] = this.t('wizMtr.needMeter', 'Name at least one meter');
            (d.meters || []).forEach((l, i) => {
                if (!l.role) f[`meters[${i}].role`] = this.t('meters.needRole', 'Choose its role');
                if (!l.device) f[`meters[${i}].device`] = this.t('meters.needMeter', 'Choose the meter');
                else if (!l.power && !l.energy) f[`meters[${i}].power`] = this.t('meters.needField', 'Choose what it reads: its power or its energy counter');
            });
            if (Object.keys(f).length) {
                const left = this._placeFieldErrors(document.getElementById('devWizBody'), f, p => this._wizMtrFieldId(p));
                this._wizSay(left.length ? left.join(' · ') : this.t('devices.fixMarked', 'Fix the marked fields.'));
                return false;
            }
            return true;
        }
        if (w.step === 2 && !d.template) {
            const err = document.getElementById('devWizTplError');
            if (err) err.style.display = '';
            return false;
        }
        if (w.step === 3 && !/^[a-z0-9][a-z0-9_-]{1,63}$/.test(d.id)) {
            const err = document.getElementById('devWizIdError');
            if (err) {
                err.textContent = this.t('devices.wizard.idInvalid', 'Use a-z 0-9 - _ (2-64 chars).');
                err.style.display = '';
            }
            document.getElementById('devWizId')?.setAttribute('aria-describedby', 'devWizIdError');
            return this._wizInvalid('devWizId');
        }
        return true;
    },

    // Test: the connection form's own (the one builder for Test, the step-1
    // check and Create)
    devWizardTest(btn) {
        return this._cfTest('devWiz', btn);
    },

    _devWizConnection() {
        return window.ConnformModel.wire(this._devWiz.cf);
    },

    // a server path → the wizard input that holds it
    _wizFieldId(path) {
        if (path.startsWith('connection.')) return (this._cf || {}).devWiz?.fieldId(path) || null;
        return ({ id: 'devWizId', name: 'devWizName' })[path] || null;
    },

    _wizSay(text) {
        const fb = document.getElementById('devWizFeedback');
        if (fb) { fb.textContent = text; fb.className = text ? 'save-feedback err' : 'save-feedback'; }
    },

    // Step 1 asks the gateway before moving on: what it would refuse about
    // this connection (a URL it may not reach, a bus already taken, a line
    // set differently) is said here, at its field — not on step 3 as a
    // joined path. Only the connection is judged; the rest comes later.
    async _devWizCheckConnection() {
        const d = this._devWiz.data;
        let r, res;
        try {
            r = await fetch('/api/devices?dry_run=true', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: 'x-dry-run', name: 'x', template: '', enabled: false,
                                       connection: this._devWizConnection() }),
            });
            res = await r.json().catch(() => ({}));
        } catch (e) { return true; }            // the gateway unreachable: Create will say
        if (r.ok) {
            if (res.status === 'valid' && res.would) return true;
            // a gateway that does not know the check stored the probe for
            // real: it is taken away again, and the wizard goes on without
            if (res.status === 'created' || res.device) {
                await fetch('/api/devices/x-dry-run', { method: 'DELETE' }).catch(() => {});
                this.showToast('warning', this.t('devices.wizard.noCheckTitle', 'Connection not checked'),
                    this.t('devices.wizard.noCheck', 'This gateway is too old to check a connection before saving; Create will say what it refuses.'));
            }
            return true;
        }
        if (r.status !== 422) return true;
        const det = res.detail || {};
        const conn = Object.fromEntries(Object.entries(det.fields || {}).filter(([k]) => k.startsWith('connection.')));
        const keys = Object.keys(conn);
        // what the check cannot judge without a template or an id is not for
        // step 1 (the probe's own build); everything else is said
        const other = (det.errors || []).filter(e => !keys.some(k => String(e).startsWith(k + ': '))
            && !/^device 'x-dry-run' could not be built/.test(String(e)));
        if (!Object.keys(conn).length && !other.length) return true;
        const left = this._placeFieldErrors(document.getElementById('devWizBody'), conn, p => this._wizFieldId(p));
        this._wizSay([...left, ...other].join(' · '));
        return false;
    },

    devWizardBack() {
        this._devWizCollect();
        if (this._devWiz.step > 1) { this._devWiz.step--; this._devWizRender(); }
    },

    async devWizardNext() {
        this._devWizCollect();
        if (!this._devWizValidate()) return;
        const w = this._devWiz;
        if (w.step === 1 && w.data.protocol !== 'none') {
            this._wizSay('');
            if (!await this._devWizCheckConnection()) return;
        }
        if (w.step === 2 && w.data.protocol === 'none') {
            this._wizSay('');
            if (!await this._devWizCheckMeters()) return;
        }
        if (w.step < 3) { w.step++; this._devWizRender(); return; }

        // step 3 → submit
        const d = w.data;
        if (d.protocol === 'none') return this._devWizCreateMeterOnly();
        const payload = {
            id: d.id, name: d.name || d.id, template: d.template, enabled: d.enabled,
            connection: this._devWizConnection(),
            // empty = the gateway's pattern, resolved by the server
            mqtt: d.topic_prefix ? { topic_prefix: d.topic_prefix } : {},
            influxdb: { bucket: d.bucket || undefined, device_tag: d.device_tag || undefined },
            ha_discovery_enabled: d.ha_discovery_enabled,
        };
        const fb = document.getElementById('devWizFeedback');
        try {
            const r = await fetch('/api/devices', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const res = await r.json().catch(() => ({}));
            if (!r.ok) {
                const det = res.detail;
                const fields = det?.fields || {};
                const errs = det?.errors || [typeof det === 'string' ? det : this.t('toast.saveFailed', 'Save Failed')];
                // a refusal goes back to the step that owns its field
                const step = Object.keys(fields).some(k => k.startsWith('connection.')) ? 1
                    : ('template' in fields) ? 2 : 3;
                if (step !== w.step) { w.step = step; this._devWizRender(); }
                const keys = Object.keys(fields);
                const left = this._placeFieldErrors(document.getElementById('devWizBody'), fields, p => this._wizFieldId(p));
                this._wizSay([...left, ...errs.filter(e => !keys.some(k => String(e).startsWith(k + ': ')))].join(' · '));
                return;
            }
            this.closeModal('deviceWizardModal');
            // where it publishes, as the server decided it
            const where = res.device?.mqtt_topic_prefix || payload.mqtt.topic_prefix || this._wizTopicFor(d.id);
            this.showToast('success',
                this.t('devices.created', 'Device created'),
                `${d.name || d.id} → MQTT ${where}/…`);
            await this.renderDevicesList();
            if (d.enabled) this.jumpToDeviceRegisters(d.id);
        } catch (e) {
            fb.textContent = e.message;
            fb.className = 'save-feedback err';
        }
    },

});
