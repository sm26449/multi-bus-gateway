// Multi-Bus Gateway — Virtual BMS page (ADR-0037).
//
// Lists the configured Virtual BMS instances and lets an operator ARM / DISARM each one.
// Arming lets the instance transmit its aggregated battery state to an inverter over CAN, so
// it is a write-gated, confirmed action; disarming is the safe direction (the inverter falls to
// its BMS-loss safe state) and needs no confirm. Reads GET /api/vbms; acts via
// POST /api/vbms/<id>/arm | /disarm. A short self-cancelling poll keeps "transmitting" honest
// while the page is open (the executor thread can stop on a CAN fault).
Object.assign(JanitzaMonitor.prototype, {

    async renderVbmsPage() {
        const b = document.getElementById('vbmsRefreshBtn');
        if (b && !b._wired) { b._wired = true; b.addEventListener('click', () => this.renderVbmsPage()); }
        await this._loadVbms();
    },

    async _loadVbms(quiet = false) {
        const el = document.getElementById('vbmsContent');
        if (!el) return;
        let data;
        try {
            const r = await fetch('/api/vbms');
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            data = await r.json();
        } catch (e) {
            if (!quiet) el.innerHTML = this._emptyState({
                icon: 'bi-exclamation-triangle', title: this.t('vbms.loadFail', 'Could not load the Virtual BMS list'),
                body: this._esc(this._errMsg(e)),
            });
            // a quiet refresh that failed leaves the last cards on screen: say they are stale,
            // never a "transmitting" badge that nobody is checking any more — and keep polling,
            // one failed request must not end the refresh for good (WK7-UI-2)
            else this._vbmsMarkStale(el, true);
            if (this.currentPage === 'vbms') this._vbmsSchedulePoll();
            return;
        }
        this._vbmsMarkStale(el, false);
        const ids = Object.keys(data || {}).sort();
        if (!ids.length) {
            el.innerHTML = this._emptyState({
                icon: 'bi-battery-charging',
                title: this.t('vbms.noneTitle', 'No Virtual BMS configured'),
                body: this.t('vbms.noneBody', 'A Virtual BMS aggregates battery packs and speaks a battery CAN protocol to an inverter. It is set up in the gateway config (the virtual_bms block); its source packs are devices.'),
                actionHtml: `<a class="btn btn-secondary btn-sm" href="#/devices"><i aria-hidden="true" class="bi bi-cpu"></i> ${this._esc(this.t('vbms.goDevices', 'Go to Devices'))}</a>`,
            });
        } else {
            el.innerHTML = ids.map(id => this._vbmsCardHtml(id, data[id] || {})).join('');
            for (const id of [...(this._vbmsOpen || [])]) {
                if (ids.includes(id)) this._vbmsFillPreview(id); else this._vbmsOpen.delete(id);
            }
        }
        this._vbmsSchedulePoll();
    },

    // armed + running → transmitting (ok); armed + !running → listen-only / transmitter refused
    // (warn); disarmed → silent (neutral). An honest badge, never a reassuring green when idle.
    _vbmsState(s) {
        const t = (k, d) => this.t(k, d);
        if (!s.armed) return { dot: 'var(--muted,#8a94a6)', pill: 'off', text: t('vbms.status.disarmed', 'Disarmed'), tip: t('vbms.status.disarmedTip', 'Silent — it is not transmitting, so the inverter sees no BMS on this bus.') };
        // armed and running, but the bank cannot be vouched for (too few live packs): it is
        // sending zero limits, or nothing at all — never the green "transmitting"
        if (s.running && s.data_state === 'silent') return { dot: 'var(--warning,#b7791f)', pill: 'warn', text: t('vbms.status.armedSilent', 'Armed · silent — source packs missing'), tip: t('vbms.status.armedSilentTip', 'Too few source packs are live, so nothing is sent: the inverter sees a BMS loss. It resumes when the packs are back.') };
        if (s.running && s.data_state === 'hold') return { dot: 'var(--warning,#b7791f)', pill: 'warn', text: t('vbms.status.armedHold', 'Armed · sending zero limits'), tip: t('vbms.status.armedHoldTip', 'Too few source packs are live: charge and discharge limits are sent as 0. It goes silent if they do not come back.') };
        if (s.running) return { dot: 'var(--success,#2e7d32)', pill: 'ok', text: t('vbms.status.armedTx', 'Armed · transmitting'), tip: t('vbms.status.armedTxTip', 'Transmitting its limits and readings to the inverter over CAN.') };
        return { dot: 'var(--warning,#b7791f)', pill: 'warn', text: t('vbms.status.armedIdle', 'Armed · not transmitting'), tip: t('vbms.status.armedIdleTip', 'Armed, but the transmitter is not running — the CAN interface is in listen-only. Take it out of listen-only on the host, then disarm and arm again.') };
    },

    _vbmsCardHtml(id, s) {
        const t = (k, d) => this.t(k, d);
        const st = this._vbmsState(s);
        const packs = (s.packs || []);
        const packsHtml = packs.length
            ? packs.map(p => `<span class="dev-chip">${this._esc(p)}</span>`).join(' ')
            : `<span class="field-hint">${this._esc(t('vbms.packsNone', 'none'))}</span>`;
        return `
        <div class="settings-card" data-vbms="${this._esc(id)}">
            <div class="settings-card-header" style="flex-wrap:wrap;gap:8px;">
                <h3 style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                    <span class="status-dot" style="--dot:${st.dot}" aria-hidden="true"></span>
                    ${this._esc(id)}
                    <span class="sink-pill ${st.pill}" title="${this._esc(st.tip)}">${this._esc(st.text)}</span>
                </h3>
                <div class="header-actions" style="display:flex;gap:6px;flex-wrap:wrap;">
                    ${s.armed
                        ? `<button data-admin class="btn btn-secondary btn-sm" ${this._act('setVbmsArmed', [id, false])}><i aria-hidden="true" class="bi bi-shield-slash"></i> ${this._esc(t('vbms.disarm', 'Disarm'))}</button>`
                        : `<button data-admin class="btn btn-primary btn-sm" ${this._act('setVbmsArmed', [id, true])}><i aria-hidden="true" class="bi bi-shield-check"></i> ${this._esc(t('vbms.arm', 'Arm…'))}</button>`}
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('openVbmsModal', [id])} ${s.armed ? `disabled title="${this._esc(t('vbms.armedEditTip', 'Disarm first to edit'))}"` : ''}><i aria-hidden="true" class="bi bi-pencil"></i> ${this._esc(t('common.edit', 'Edit'))}</button>
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('deleteVbms', [id])} ${s.armed ? 'disabled' : ''}><i aria-hidden="true" class="bi bi-trash"></i> ${this._esc(t('common.delete', 'Delete'))}</button>
                </div>
            </div>
            <div class="settings-card-body">
                <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px;">
                    <span class="field-hint" style="min-width:110px;">${this._esc(t('vbms.channel', 'CAN interface'))}</span> <code>${this._esc(s.channel || '—')}</code></div>
                <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                    <span class="field-hint" style="min-width:110px;">${this._esc(t('vbms.packs', 'Source packs'))}</span> ${packsHtml}</div>
                <div style="margin-top:8px;">
                    <button class="btn btn-ghost btn-sm" data-vbms-pvbtn="${this._esc(id)}" aria-expanded="false" ${this._act('toggleVbmsPreview', [id])}><i aria-hidden="true" class="bi bi-table"></i> <span>${this._esc(t('vbms.showAgg', 'Show aggregation'))}</span></button>
                    <div class="vbms-preview" data-vbms-pv="${this._esc(id)}" hidden style="margin-top:8px;"></div>
                </div>
            </div>
        </div>`;
    },

    _vbmsPvEl(id) {
        return document.querySelector(`.vbms-preview[data-vbms-pv="${(window.CSS && CSS.escape) ? CSS.escape(id) : id}"]`);
    },

    _vbmsPvButton(id, open) {
        const btn = document.querySelector(`[data-vbms-pvbtn="${(window.CSS && CSS.escape) ? CSS.escape(id) : id}"]`);
        if (!btn) return;
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        const span = btn.querySelector('span');
        if (span) span.textContent = open ? this.t('vbms.hideAgg', 'Hide aggregation') : this.t('vbms.showAgg', 'Show aggregation');
    },

    async toggleVbmsPreview(id) {
        const el = this._vbmsPvEl(id);
        if (!el) return;
        this._vbmsOpen = this._vbmsOpen || new Set();
        if (!el.hidden) { el.hidden = true; this._vbmsOpen.delete(id); this._vbmsPvButton(id, false); return; }   // a second click collapses it
        this._vbmsOpen.add(id);
        el.hidden = false;
        el.innerHTML = `<p class="field-hint">${this._esc(this.t('common.loading', 'Loading…'))}</p>`;
        await this._vbmsFillPreview(id);
    },

    // the page's 5 s refresh re-renders every card: an aggregation the user opened stays open
    // and is filled again with fresh values, never collapsed under them (VBMS-UI-1)
    async _vbmsFillPreview(id) {
        const el = this._vbmsPvEl(id);
        if (!el) return;
        this._vbmsPvHtml = this._vbmsPvHtml || {};
        if (!el.innerHTML && this._vbmsPvHtml[id]) el.innerHTML = this._vbmsPvHtml[id];   // no blank flash
        el.hidden = false;
        this._vbmsPvButton(id, true);
        try {
            const pv = await fetch(`/api/vbms/${encodeURIComponent(id)}/preview`).then(r => r.json());
            const now = this._vbmsPvEl(id);                 // the list may have been re-rendered meanwhile
            if (now && this._vbmsOpen && this._vbmsOpen.has(id)) {
                now.innerHTML = this._vbmsPvHtml[id] = this._vbmsPreviewHtml(pv);
            }
        } catch (e) {
            const now = this._vbmsPvEl(id);
            if (now) now.innerHTML = `<p style="color:var(--danger,#c0392b);">${this._esc(this._errMsg(e))}</p>`;
        }
    },

    // what the Virtual BMS sends, and from which packs (ADR-0045 L2 transparency)
    _vbmsPreviewHtml(pv) {
        const t = (k, d, p) => this.t(k, d, p);
        const packs = pv.packs || [];
        // the union of canonical keys across packs, in a stable order
        const keys = [...new Set(packs.flatMap(p => Object.keys(p.values || {})))];
        // a pack contributes `voltage`; it is SENT as `pack_voltage` — look the sent value up
        // under the name the gateway says it is sent as, not under the pack's own key
        const sent = k => (pv.sends || {})[(pv.sent_as || {})[k] || k];
        const head = `<tr><th style="text-align:left;">${this._esc(t('vbms.pvField', 'Field'))}</th>`
            + packs.map(p => `<th style="text-align:right;">${this._esc(p.id)}${p.eligible ? '' : ' ⚠'}</th>`).join('')
            + `<th style="text-align:right;">${this._esc(t('vbms.pvSends', 'Sends'))}</th></tr>`;
        const rows = keys.map(k => `<tr><td><code>${this._esc(k)}</code></td>`
            + packs.map(p => `<td style="text-align:right;">${p.values && p.values[k] != null ? this._esc(String(p.values[k])) : '—'}</td>`).join('')
            + `<td style="text-align:right;"><strong>${sent(k) != null ? this._esc(String(sent(k))) : '—'}</strong></td></tr>`).join('')
            // the control fields have no pack column: they come from the EMS / the floor, and
            // they are what the inverter obeys — shown, not left out
            + Object.keys(pv.control || {}).map(k => `<tr><td><code>${this._esc(k)}</code></td>`
                + packs.map(() => '<td style="text-align:right;">·</td>').join('')
                + `<td style="text-align:right;"><strong>${pv.control[k] != null ? this._esc(String(pv.control[k])) : '—'}</strong></td></tr>`).join('');
        const packNote = t('vbms.pvLive', '{live} of {total} packs live', { live: pv.packs_live, total: pv.packs_total });
        let cross = '';
        const xc = pv.inverter_check;
        if (xc) {
            const cell = (c, unit) => `${c.sent != null ? this._esc(String(c.sent)) : '—'}${unit} → ${c.received != null ? this._esc(String(c.received)) : '—'}${unit} ${c.received == null ? '' : (c.match ? '✓' : '⚠')}`;
            cross = `<p class="field-hint" style="margin-bottom:4px;">${this._esc(t('vbms.feeds', 'Feeds'))} <code>${this._esc(xc.inverter)}</code> — ${this._esc(t('vbms.pvSent', 'we send → it reads'))}: SOC ${cell(xc.soc, '%')} · V ${cell(xc.voltage, '')}</p>`;
        }
        return cross + `<p class="field-hint">${this._esc(packNote)} · ${this._esc(t('vbms.pvFrames', 'frames'))}: ${this._esc((pv.frames || []).join(' '))}${pv.send_optional === false ? ` (${this._esc(t('vbms.pvCoreOnly', 'core only'))})` : ''}</p>
            <table class="data-table" style="font-size:12.5px;"><thead>${head}</thead><tbody>${rows || `<tr><td colspan="99" class="field-hint">${this._esc(t('vbms.pvNoData', 'no live pack data'))}</td></tr>`}</tbody></table>`;
    },

    async setVbmsArmed(id, arm) {
        const t = (k, d, p) => this.t(k, d, p);
        if (this._vbmsBusy) return;                     // one change at a time: a second click is not a second request
        const cur = this._vbmsCardEl(id);
        const channel = (cur && cur.querySelector('code')) ? cur.querySelector('code').textContent : 'CAN';
        if (arm && !await this.confirmAction({
            title: t('vbms.armTitle', 'Arm {name}?', { name: id }),
            message: t('vbms.armConfirm', 'The Virtual BMS starts transmitting to the inverter over {channel}. The inverter will follow its limits and readings — make sure the CAN interface is out of listen-only.', { channel }),
            confirmLabel: t('vbms.armGo', 'Arm'),
            danger: true,
        })) return;
        this._vbmsBusy = true;
        const doIt = () => fetch(`/api/vbms/${encodeURIComponent(id)}/${arm ? 'arm' : 'disarm'}`, { method: 'POST' });
        try {
            let r = await doIt();
            let d = await r.json().catch(() => ({}));
            // a 409 carrying prerequisites is the guided case (ADR-0046); any other 409 falls
            // through to the error toast below (a config_blocked config.yaml is shown as-is)
            if (r.status === 409 && d.detail?.prerequisites) {   // not config_blocked → the guided path
                if (!await this._resolvePrerequisites(d.detail.prerequisites)) return;   // later / fix failed
                r = await doIt();                           // retry once the prerequisite is enabled
                d = await r.json().catch(() => ({}));
            }
            if (!r.ok) {
                const why = d.detail?.errors ? d.detail.errors.join(' ') : this._errMsg(d);
                this.showToast('error', arm ? t('vbms.armFail', 'Could not arm') : t('vbms.disarmFail', 'Could not disarm'), why);
                return;
            }
            this.showToast('success', arm ? t('vbms.armedToast', '{name} armed', { name: id }) : t('vbms.disarmedToast', '{name} disarmed', { name: id }), '');
        } catch (e) {
            this.showToast('error', arm ? t('vbms.armFail', 'Could not arm') : t('vbms.disarmFail', 'Could not disarm'), this._errMsg(e));
        } finally {
            this._vbmsBusy = false;
        }
        this._loadVbms(true);
    },

    // the list could not be refreshed: a visible notice above the (now unverified) cards
    _vbmsMarkStale(el, stale) {
        if (!el) return;
        const old = document.getElementById('vbmsStaleNote');
        if (old && old.remove) old.remove();
        if (!stale || !el.insertAdjacentHTML) return;
        el.insertAdjacentHTML('afterbegin', `<p id="vbmsStaleNote" role="status" class="field-hint" style="color:var(--warning,#b7791f);"><i aria-hidden="true" class="bi bi-exclamation-triangle"></i> ${this._esc(this.t('vbms.stale', 'The gateway did not answer — what is shown below may be out of date. Retrying…'))}</p>`);
    },

    _vbmsCardEl(id) {
        return document.querySelector(`.settings-card[data-vbms="${(window.CSS && CSS.escape) ? CSS.escape(id) : id}"]`);
    },

    // a self-cancelling poll: keeps "transmitting" honest (the executor can stop on a CAN
    // fault) without a leaked interval — it stops itself the moment we leave the page, and
    // never re-renders over an open confirm / an in-flight action.
    _vbmsSchedulePoll() {
        clearTimeout(this._vbmsPoll);
        this._vbmsPoll = setTimeout(() => {
            if (this.currentPage !== 'vbms') return;        // left the page → stop (renderVbmsPage re-arms it)
            if (this._vbmsBusy) { this._vbmsSchedulePoll(); return; }   // an action is in flight → try again later
            this._loadVbms(true);                           // reloads, which reschedules the next tick
        }, 5000);
    },

    // ── add / edit modal (ADR-0045 L1) ───────────────────────────────────
    async openVbmsModal(id) {
        const t = (k, d) => this.t(k, d);
        this._vbmsEditing = id || null;
        const body = document.getElementById('vbmsModalBody');
        const fb = document.getElementById('vbmsModalFeedback');
        if (fb) fb.textContent = '';
        document.getElementById('vbmsModalTitle').textContent = id ? t('vbms.editTitle', 'Edit Virtual BMS') : t('vbms.add', 'Add Virtual BMS');
        body.innerHTML = `<p class="field-hint">${this._esc(t('common.loading', 'Loading…'))}</p>`;
        this.openModal('vbmsModal');
        let ports, sources, profiles, inst = {};
        try {
            [ports, sources, profiles] = await Promise.all([
                fetch(`/api/vbms/ports${id ? `?editing=${encodeURIComponent(id)}` : ''}`).then(r => r.json()).then(d => d.ports || []),
                fetch('/api/vbms/sources').then(r => r.json()).then(d => d.sources || []),
                fetch('/api/vbms/profiles').then(r => r.json()).then(d => d.profiles || []),
            ]);
            if (id) inst = await fetch(`/api/vbms/${encodeURIComponent(id)}`).then(r => r.json());
        } catch (e) {
            body.innerHTML = `<p style="color:var(--danger,#c0392b);">${this._esc(this._errMsg(e))}</p>`;
            return;
        }
        body.innerHTML = this._vbmsFormHtml(inst, ports, sources, profiles);
    },

    _vbmsFormHtml(inst, ports, sources, profiles) {
        const t = (k, d) => this.t(k, d);
        const editing = !!this._vbmsEditing;
        const bounds = inst.bounds || {}, floor = inst.floor || {};
        const ccl = (bounds.charge_current_limit || [])[1] ?? 80;
        const dcl = (bounds.discharge_current_limit || [])[1] ?? 180;
        const cvl = floor.charge_voltage_limit ?? 57.6;
        const dvl = floor.discharge_voltage_limit ?? 46.4;
        const chosenPacks = new Set(inst.packs || []);
        const num = (id, label, val, step) => `<div class="form-group"><label for="${id}">${this._esc(label)}</label>
            <input class="input" type="number" id="${id}" value="${val}" step="${step || 'any'}" min="0"></div>`;
        const portOpt = p => {
            const reserved = p.availability === 'reserved';
            const note = reserved ? ` — ${t('vbms.portReserved', 'reserved by')} ${p.publisher}`
                : p.availability === 'available_shared' ? ` — ${t('vbms.portShared', 'also used by')} ${(p.readers || []).join(', ')}` : '';
            const lo = p.listen_only ? ' ⚠ listen-only' : '';
            const sel = p.name === inst.channel ? 'selected' : '';
            return `<option value="${this._esc(p.name)}" ${reserved ? 'disabled' : ''} ${sel}>${this._esc(p.name + note + lo)}</option>`;
        };
        const warnLO = ports.some(p => p.listen_only && (p.name === inst.channel || p.availability !== 'reserved'));
        return `
        <div class="form-group"><label for="vbfId">${this._esc(t('vbms.fId', 'Id'))}</label>
            <input class="input" id="vbfId" value="${this._esc(inst.id || '')}" ${editing ? 'disabled' : ''} placeholder="deye-vbms"></div>
        <div class="form-group"><label for="vbfName">${this._esc(t('vbms.fName', 'Name (optional)'))}</label>
            <input class="input" id="vbfName" value="${this._esc(inst.name || '')}"></div>
        <div class="form-group"><label for="vbfProfile">${this._esc(t('vbms.fProfile', 'Protocol profile'))}</label>
            <select class="input" id="vbfProfile">${profiles.map(p => `<option value="${this._esc(p.id)}" ${p.id === inst.profile ? 'selected' : ''}>${this._esc(p.name || p.id)}</option>`).join('')}</select></div>
        <div class="form-group"><label for="vbfPort">${this._esc(t('vbms.channel', 'CAN interface'))}</label>
            <select class="input" id="vbfPort">${ports.map(portOpt).join('') || `<option value="">${this._esc(t('vbms.noPorts', 'no CAN interfaces found'))}</option>`}</select>
            ${warnLO ? `<p class="field-hint" style="color:var(--warning,#b7791f);">${this._esc(t('vbms.loWarn', 'A listen-only port can be saved, but it will not transmit until it is taken out of listen-only on the host.'))}</p>` : ''}</div>
        <div class="form-group"><label>${this._esc(t('vbms.packs', 'Source packs'))}</label>
            <div id="vbfPacks">${sources.length ? sources.map(s => `<label style="display:block;font-weight:normal;"><input type="checkbox" value="${this._esc(s.id)}" ${chosenPacks.has(s.id) ? 'checked' : ''}> ${this._esc(s.name || s.id)}</label>`).join('')
                : `<span class="field-hint">${this._esc(t('vbms.noSources', 'no battery devices configured'))}</span>`}</div></div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;">
            ${num('vbfCcl', t('vbms.fCcl', 'Charge limit (A)'), ccl)}
            ${num('vbfDcl', t('vbms.fDcl', 'Discharge limit (A)'), dcl)}
            ${num('vbfCvl', t('vbms.fCvl', 'Charge voltage (V)'), cvl, '0.1')}
            ${num('vbfDvl', t('vbms.fDvl', 'Discharge voltage (V)'), dvl, '0.1')}
        </div>
        <div class="form-group"><label for="vbfManuf">${this._esc(t('vbms.fManuf', 'Manufacturer'))}</label>
            <input class="input" id="vbfManuf" value="${this._esc(inst.manufacturer || 'SHEnergy')}"></div>
        <div class="form-group"><label for="vbfInverter">${this._esc(t('vbms.fInverter', 'Inverter it feeds (optional)'))}</label>
            <input class="input" id="vbfInverter" value="${this._esc(inst.inverter || '')}" placeholder="deye">
            <p class="field-hint">${this._esc(t('vbms.inverterHint', 'A device id — the preview then cross-checks what we send against what this inverter reports receiving.'))}</p></div>
        <div class="form-group"><label style="font-weight:normal;"><input type="checkbox" id="vbfSendOptional" ${((inst.fields || {}).send_optional !== false) ? 'checked' : ''}> ${this._esc(t('vbms.fSendOptional', 'Send optional detail frames (cell extremes, alarms, manufacturer)'))}</label>
            <p class="field-hint">${this._esc(t('vbms.sendOptionalHint', 'Off sends only the frames the inverter needs (limits, SOC, V/I/T, enables).'))}</p></div>`;
    },

    async saveVbmsModal() {
        const t = (k, d) => this.t(k, d);
        const fb = document.getElementById('vbmsModalFeedback');
        const val = id => (document.getElementById(id) || {}).value;
        const numv = id => parseFloat(val(id));
        const packs = [...document.querySelectorAll('#vbfPacks input:checked')].map(c => c.value);
        const editing = this._vbmsEditing;
        const body = {
            id: editing || (val('vbfId') || '').trim(),
            name: (val('vbfName') || '').trim() || undefined,
            profile: val('vbfProfile'),
            channel: val('vbfPort'),
            packs,
            bounds: { charge_current_limit: [0, numv('vbfCcl')], discharge_current_limit: [0, numv('vbfDcl')] },
            floor: { charge_current_limit: 0, discharge_current_limit: 0,
                     charge_voltage_limit: numv('vbfCvl'), discharge_voltage_limit: numv('vbfDvl'),
                     charge_enable: 0, discharge_enable: 0 },
            manufacturer: (val('vbfManuf') || '').trim() || undefined,
            inverter: (val('vbfInverter') || '').trim() || undefined,
            fields: { send_optional: !!(document.getElementById('vbfSendOptional') || {}).checked },
        };
        if (fb) fb.textContent = '';
        try {
            const url = editing ? `/api/vbms/${encodeURIComponent(editing)}` : '/api/vbms';
            const r = await fetch(url, { method: editing ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) {
                const why = d.detail?.errors ? d.detail.errors.join('\n') : this._errMsg(d);
                if (fb) fb.textContent = why; else this.showToast('error', t('vbms.saveFail', 'Could not save'), why);
                return;
            }
            this.closeModal('vbmsModal');
            this.showToast('success', editing ? t('vbms.savedEdit', 'Virtual BMS updated') : t('vbms.savedNew', 'Virtual BMS created'), body.id);
            this._loadVbms(true);
        } catch (e) {
            if (fb) fb.textContent = this._errMsg(e);
        }
    },

    async deleteVbms(id) {
        const t = (k, d, p) => this.t(k, d, p);
        if (!await this.confirmAction({
            title: t('vbms.deleteTitle', 'Delete {name}?', { name: id }),
            message: t('vbms.deleteConfirm', 'The Virtual BMS and its configuration are removed. This cannot be undone.'),
            confirmLabel: t('common.delete', 'Delete'), danger: true,
        })) return;
        try {
            const r = await fetch(`/api/vbms/${encodeURIComponent(id)}`, { method: 'DELETE' });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) {
                this.showToast('error', t('vbms.deleteFail', 'Could not delete'), d.detail?.errors ? d.detail.errors.join(' ') : this._errMsg(d));
                return;
            }
            this.showToast('success', t('vbms.deleted', 'Virtual BMS deleted'), id);
            this._loadVbms(true);
        } catch (e) {
            this.showToast('error', t('vbms.deleteFail', 'Could not delete'), this._errMsg(e));
        }
    },
});
