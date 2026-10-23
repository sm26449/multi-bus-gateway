// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
//
// components — augments JanitzaMonitor.prototype
//
// The things a device holds, added by instance: a Victron GX's batteries and
// temperature sensors. Each is an ordinary
// device everywhere else; here it is listed under its parent, added, renamed,
// switched and removed. Which kinds exist comes from the parent's template.

Object.assign(JanitzaMonitor.prototype, {

    _componentOpenState(id) {
        try { return localStorage.getItem(`mbg-components-open-${id}`) !== '0'; }
        catch (e) { return true; }
    },

    // the nested rows under a parent on the Devices list
    _componentChildrenHtml(parent, kids, healthColor) {
        if (!kids.length) return '';
        const open = this._componentOpenState(parent.id);
        const online = kids.filter(k => k.connected).length;
        const word = kids.length === 1 ? this.t('components.one', 'component') : this.t('components.many', 'components');
        return `
        <div class="component-toggle" role="button" tabindex="0" data-component-toggle="${this._esc(parent.id)}" data-key-enter
             aria-expanded="${open}" aria-controls="compUnits-${this._esc(parent.id)}" style="margin:-4px 0 4px 34px;font-size:12px;color:var(--text-secondary);cursor:pointer;display:inline-flex;gap:6px;align-items:center;">
            <i aria-hidden="true" class="bi bi-chevron-right component-chevron" style="transition:transform .15s;${open ? 'transform:rotate(90deg);' : ''}"></i>
            <i aria-hidden="true" class="bi bi-boxes"></i> ${kids.length} ${word} · ${online}/${kids.length} ${this.t('endpoints.answeringShort', 'answering')}
        </div>
        <div data-component-units="${this._esc(parent.id)}" id="compUnits-${this._esc(parent.id)}" style="display:${open ? '' : 'none'};
             margin-left:22px;border-left:2px solid var(--border,#e5e7eb);padding-left:8px;">
            ${kids.map(k => this._deviceRowHtml(k, healthColor, true)).join('')}
        </div>`;
    },

    _wireComponentToggles(el) {
        el.querySelectorAll('[data-component-toggle]').forEach(h =>
            h.addEventListener('click', () => {
                const id = h.dataset.componentToggle;
                const box = el.querySelector(`[data-component-units="${CSS.escape(id)}"]`);
                // what is shown decides (a search may have opened it)
                const open = box ? box.style.display === 'none' : !this._componentOpenState(id);
                try { localStorage.setItem(`mbg-components-open-${id}`, open ? '1' : '0'); } catch (e) { /* private mode */ }
                if (this._findDisclose) this._findDisclose(h, box, open);
                else if (box) box.style.display = open ? '' : 'none';
            }));
    },

    // a component's own page: what it is part of, in one line (the header
    // carries the one "Open device" button)
    _componentOfHtml(s) {
        const t = this.t.bind(this);
        return `
        <div class="field-hint" style="margin:0 0 12px;" data-component-of>
            <i aria-hidden="true" class="bi bi-boxes"></i> ${t('components.partOf', 'Part of')}
            <a href="#" ${this._act('openDeviceDetail', [s.component_of])}><b>${this._esc(s.parent_name || s.component_of)}</b></a>
            · ${this._esc(s.component_type_name || s.component_type)} ${this._esc(s.component_instance)}.
            ${t('components.readThrough', 'It is read through that device’s connection, set there.')}
        </div>`;
    },

    // a component's "Read via" tab: its parent's connection (read-only),
    // its instance, and what can be done to it from here
    _componentReadViaHtml(s) {
        const t = this.t.bind(this);
        const parent = (this._devices || []).find(x => x.id === s.component_of) || { id: s.component_of };
        const reach = this._reachWords(parent);
        const d = s.data || {};
        const fact = (label, val) => `<div><div style="color:var(--text-secondary);font-size:11.5px;">${label}</div><div style="font-weight:600;font-size:13.5px;word-break:break-all;">${val}</div></div>`;
        return `
        <div class="settings-card">
            <div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-diagram-3"></i> ${t('devices.tab.readVia', 'Read via')}</h3></div>
            <div class="settings-card-body">
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:16px 24px;margin-bottom:14px;">
                    ${fact(t('components.device', 'Device'), `<a href="#" ${this._act('openDeviceDetail', [s.component_of])}>${this._esc(s.parent_name || s.component_of)}</a>`)}
                    ${fact(t('devices.overview.source', 'Source'), `${this._esc(reach.proto)}<br><span style="font-weight:400;font-size:12px;color:var(--text-secondary);">${this._esc(reach.address)}</span>`)}
                    ${fact(t('components.type', 'Type'), this._esc(s.component_type_name || s.component_type))}
                    ${fact(t('components.instance', 'Instance'), this._esc(s.component_instance))}
                </div>
                <p class="field-hint" style="margin:0 0 12px;">${t('components.readThroughEdit', 'Its connection is its device’s: change it on that device’s Edit tab.')}</p>
                <div style="display:flex;gap:8px;flex-wrap:wrap;">
                    <button data-admin class="btn btn-secondary btn-sm" ${this._act('renameComponentUi', [s.id, d.name, s.component_of])}><i aria-hidden="true" class="bi bi-pencil"></i> ${t('components.rename', 'Rename')}</button>
                    <button data-admin class="btn btn-secondary btn-sm" ${this._act('switchComponentUi', [s.id, !d.enabled, s.component_of])}><i aria-hidden="true" class="bi bi-${d.enabled ? 'pause' : 'play'}-circle"></i> ${d.enabled ? t('components.disable', 'Disable') : t('components.enable', 'Enable')}</button>
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('removeComponentUi', [s.id, s.component_of])}><i aria-hidden="true" class="bi bi-trash"></i> ${t('components.remove', 'Remove')}</button>
                </div>
            </div>
        </div>`;
    },

    // after a change made on a component's own page, that page shows it
    // (renamed, switched); on its parent's page, the Components card does
    async _componentChanged(s) {
        if (s.component_of) {
            await this.openDeviceDetail(s.id);
            this._switchDeviceTab('connection');
        } else {
            await this.renderComponentsCard();
        }
    },

    // ── the Components card on a parent's Overview ──────────────────────────
    async renderComponentsCard() {
        const s = this._devDetail;
        const box = document.getElementById('devComponentsCard');
        if (!box || !s) return;
        let d;
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(s.id)}/components`);
            if (!r.ok) { box.innerHTML = ''; return; }
            d = await r.json();
        } catch (e) { box.innerHTML = ''; return; }
        if (this._devDetail?.id !== s.id) return;          // another device opened meanwhile
        this._compTypes = d.types || [];
        if (!this._compTypes.length && !(d.components || []).length) { box.innerHTML = ''; return; }
        const t = this.t.bind(this);
        const typeName = k => (this._compTypes.find(x => x.type === k) || {}).name || k;
        const dot = h => ({ ok: 'var(--success,#22c55e)', degraded: 'var(--warning,#f59e0b)',
                            down: 'var(--danger,#ef4444)' })[h] || 'var(--text-secondary,#8a94a0)';
        const rows = (d.components || []).map(c => `
            <tr data-comp="${this._esc(c.id)}" data-comp-key="${this._esc(c.type + '|' + c.instance)}">
                <td><span class="status-dot" style="--dot:${c.enabled ? dot(c.data_health) : 'var(--text-secondary,#8a94a0)'}" title="${this._esc(c.enabled ? (c.data_health || 'idle') : t('devices.disabled', 'disabled'))}"></span>
                    <a href="#" ${this._act('openDeviceDetail', [c.id])}>${this._esc(c.name)}</a> <span class="dev-chip">${this._esc(c.id)}</span></td>
                <td>${this._esc(typeName(c.type))}</td>
                <td style="font-variant-numeric:tabular-nums;">${this._esc(c.instance)}</td>
                <td>${c.enabled ? this._esc(c.data_health || 'idle') : t('devices.disabled', 'disabled')}</td>
                <td style="white-space:nowrap;text-align:right;">
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('renameComponentUi', [c.id, c.name])} title="${t('components.rename', 'Rename')}" aria-label="${t('components.rename', 'Rename')}"><i aria-hidden="true" class="bi bi-pencil"></i></button>
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('switchComponentUi', [c.id, !c.enabled])} title="${c.enabled ? t('components.disable', 'Disable') : t('components.enable', 'Enable')}" aria-label="${c.enabled ? t('components.disable', 'Disable') : t('components.enable', 'Enable')}"><i aria-hidden="true" class="bi bi-${c.enabled ? 'pause' : 'play'}-circle"></i></button>
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('removeComponentUi', [c.id])} title="${t('components.remove', 'Remove')}" aria-label="${t('components.remove', 'Remove')}"><i aria-hidden="true" class="bi bi-trash"></i></button>
                </td>
            </tr>`).join('');
        const canFind = this._compTypes.some(x => x.discover);
        const first = this._compTypes[0] || {};
        box.innerHTML = `
        <div class="settings-card">
            <div class="settings-card-header" style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
                <h3><i aria-hidden="true" class="bi bi-boxes"></i> ${t('components.title', 'Components')} <span class="dev-chip">${(d.components || []).length}</span></h3>
                ${canFind ? `<button data-admin class="btn btn-secondary btn-sm" id="compFindBtn" data-action="findComponentsUi"><i aria-hidden="true" class="bi bi-search"></i> ${t('components.find', 'Find on the device')}</button>` : ''}
            </div>
            <div class="settings-card-body">
                <p class="field-hint" style="margin-top:0;">${t('components.intro', 'What this device holds — batteries, sensors — each added by its instance and read through this device’s connection. Each one is a device of its own for values, history, rules and attributes.')}</p>
                ${rows ? `<div style="overflow-x:auto;"><table class="data-table" style="width:100%;">
                    <thead><tr><th scope="col">${t('components.name', 'Name')}</th><th scope="col">${t('components.type', 'Type')}</th>
                        <th scope="col">${t('components.instance', 'Instance')}</th><th scope="col">${t('components.status', 'Status')}</th><th scope="col"><span class="sr-only">${t('common.actions', 'Actions')}</span></th></tr></thead>
                    <tbody>${rows}</tbody></table></div>`
                : `<div class="field-hint">${t('components.none', 'No components yet.')}</div>`}
                <div id="compFound" aria-live="polite"></div>
                ${this._compTypes.length ? `
                <div data-admin id="compAddForm" style="margin-top:12px;">
                    <div class="form-row align-end" style="flex-wrap:wrap;">
                        <div class="form-group"><label class="form-label" for="compType">${t('components.type', 'Type')}</label>
                            <select id="compType" class="input" data-action="_compTypeChanged" data-on="change">${this._compTypes.map(x => `<option value="${this._esc(x.type)}">${this._esc(x.name)}</option>`).join('')}</select></div>
                        <div class="form-group"><label class="form-label" for="compInstance" id="compInstanceLabel">${this._esc(first.instance_label || t('components.instance', 'Instance'))}</label>
                            <input id="compInstance" class="input" required maxlength="32" pattern="[A-Za-z0-9_.\\-]{1,32}" autocomplete="off" aria-describedby="compInstanceHint"></div>
                        <div class="form-group flex-2"><label class="form-label" for="compName">${t('components.nameOptional', 'Name (optional)')}</label>
                            <input id="compName" class="input" maxlength="80" autocomplete="off"></div>
                        <div class="form-group"><button data-admin type="button" class="btn btn-primary btn-sm" data-action="addComponentUi"><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('components.add', 'Add component')}</button></div>
                    </div>
                    <div class="field-hint" id="compInstanceHint">${this._esc(first.instance_hint || '')}</div>
                </div>` : ''}
            </div>
        </div>`;
    },

    _compTypeChanged() {
        const x = (this._compTypes || []).find(y => y.type === document.getElementById('compType')?.value) || {};
        const lab = document.getElementById('compInstanceLabel');
        const hint = document.getElementById('compInstanceHint');
        if (lab) lab.textContent = x.instance_label || this.t('components.instance', 'Instance');
        if (hint) hint.textContent = x.instance_hint || '';
    },

    async _componentCall(url, method, body) {
        const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' },
                                     body: body === undefined ? undefined : JSON.stringify(body) });
        const res = await r.json().catch(() => ({}));
        if (!r.ok) {
            const det = res.detail;
            throw new Error((det && det.errors) ? det.errors.join('; ') : (typeof det === 'string' ? det : r.statusText));
        }
        return res;
    },

    async addComponentUi(type, instance, name) {
        const s = this._devDetail;
        if (!s) return;
        const body = {
            type: type || document.getElementById('compType')?.value,
            instance: String(instance ?? document.getElementById('compInstance')?.value ?? '').trim(),
        };
        const nm = (name ?? document.getElementById('compName')?.value ?? '').trim();
        if (nm) body.name = nm;
        try {
            const res = await this._componentCall(`/api/devices/${encodeURIComponent(s.id)}/components`, 'POST', body);
            this.showToast('success', this.t('components.added', 'Component added'), res.component?.name || '');
            await this.renderComponentsCard();
            if (this._compFoundLast) this._paintFoundComponents(this._compFoundLast);
        } catch (e) {
            this.showToast('error', this.t('components.addFail', 'Could not add the component'), e.message);
        }
    },

    async renameComponentUi(id, current, parentId) {
        const s = this._devDetail;
        if (!s) return;
        const parent = parentId || s.id;
        const name = await this.askValue({
            title: this.t('components.renameTitle', 'Rename {id}', { id }),
            kind: 'text', initial: current || '',
            label: this.t('components.renamePrompt', 'New name'),
            confirmLabel: this.t('common.save', 'Save'),
        });
        if (name === null || this._devDetail !== s) return;
        try {
            await this._componentCall(`/api/devices/${encodeURIComponent(parent)}/components/${encodeURIComponent(id)}`, 'PATCH', { name: name.trim() });
            await this._componentChanged(s);
        } catch (e) { this.showToast('error', this.t('components.saveFail', 'Could not save'), e.message); }
    },

    async switchComponentUi(id, enabled, parentId) {
        const s = this._devDetail;
        if (!s) return;
        const parent = parentId || s.id;
        try {
            await this._componentCall(`/api/devices/${encodeURIComponent(parent)}/components/${encodeURIComponent(id)}`, 'PATCH', { enabled: !!enabled });
            await this._componentChanged(s);
        } catch (e) { this.showToast('error', this.t('components.saveFail', 'Could not save'), e.message); }
    },

    async removeComponentUi(id, parentId) {
        const s = this._devDetail;
        const parent = parentId || s?.id;
        const name = s?.component_of ? s.data?.name : ((this._devices || []).find(x => x.id === id) || {}).name;
        if (!s) return;
        const own = s.id === id;            // removed from its own page
        if (own && this._ubCount(s.usedBy)
            && !await this._ubRecheck('device', id, document.getElementById('devUsedBy'), deps => this._applyDeviceUsedBy(id, deps))) return;
        // asks the server first: a component something uses says what, on its page
        const ask = {
            kind: 'device', id,
            url: `/api/devices/${encodeURIComponent(parent)}/components/${encodeURIComponent(id)}`,
            host: own ? document.getElementById('devUsedBy') : null,
            title: this.t('components.removeTitle', 'Remove component {id}', { id: name ? `${name} (${id})` : id }),
            message: this.t('components.removeConfirm',
                'Its values stop and its topics are cleared. Its measurement selection stays on disk: adding the same instance again resumes it.'),
            confirmLabel: this.t('components.remove', 'Remove'),
        };
        if (!await this._ubDelete(ask)) {
            if (own && ask.refused && this._applyDeviceUsedBy) this._applyDeviceUsedBy(id, ask.refused);
            return;
        }
        try {
            this.showToast('success', this.t('components.removed', 'Component removed'), id);
            // removed from its own page: back to its device
            if (s.component_of) { this.closeDeviceDetail(); return; }
            await this.renderComponentsCard();
            if (this._compFoundLast) this._paintFoundComponents(this._compFoundLast);
        } catch (e) { this.showToast('error', this.t('components.removeFail', 'Could not remove'), e.message); }
    },

    async findComponentsUi() {
        const s = this._devDetail;
        const box = document.getElementById('compFound');
        const btn = document.getElementById('compFindBtn');
        if (!s || !box) return;
        box.innerHTML = `<div class="field-hint" style="margin-top:10px;"><span class="spinner-border spinner-border-sm" aria-hidden="true"></span> ${this.t('components.listening', 'Asking the device what it holds… (a few seconds)')}</div>`;
        if (btn) btn.disabled = true;
        try {
            const res = await this._componentCall(`/api/devices/${encodeURIComponent(s.id)}/components/discover`, 'POST');
            if (this._devDetail?.id !== s.id) return;
            this._compFoundLast = res.found || [];
            await this.renderComponentsCard();
            this._paintFoundComponents(this._compFoundLast);
        } catch (e) {
            const b = document.getElementById('compFound');
            if (b) b.innerHTML = `<div class="field-hint" style="margin-top:10px;color:var(--danger,#ef4444);">${this._esc(e.message)}</div>`;
        } finally {
            const b2 = document.getElementById('compFindBtn');
            if (b2) b2.disabled = false;
        }
    },

    async _paintFoundComponents(found) {
        const box = document.getElementById('compFound');
        if (!box) return;
        const t = this.t.bind(this);
        // what is already added, from the card just painted
        const have = new Set([...document.querySelectorAll('#devComponentsCard tr[data-comp-key]')].map(r => r.dataset.compKey));
        const typeName = k => ((this._compTypes || []).find(x => x.type === k) || {}).name || k;
        if (!found.length) {
            box.innerHTML = `<div class="field-hint" style="margin-top:10px;">${t('components.foundNone', 'The device named nothing it holds.')}</div>`;
            return;
        }
        box.innerHTML = `
        <div style="margin-top:12px;"><b>${t('components.foundTitle', 'Found on the device')}</b></div>
        <div style="overflow-x:auto;"><table class="data-table" style="width:100%;">
            <thead><tr><th scope="col">${t('components.type', 'Type')}</th><th scope="col">${t('components.instance', 'Instance')}</th>
                <th scope="col">${t('components.product', 'Product')}</th><th scope="col">${t('components.nameOnDevice', 'Name on the device')}</th><th scope="col"><span class="sr-only">${t('common.actions', 'Actions')}</span></th></tr></thead>
            <tbody>${found.map(f => {
                const added = have.has(`${f.type}|${f.instance}`);
                return `<tr>
                    <td>${this._esc(typeName(f.type))}</td>
                    <td style="font-variant-numeric:tabular-nums;">${this._esc(f.instance)}</td>
                    <td>${this._esc(f.product || '—')}</td>
                    <td>${this._esc(f.name || '—')}</td>
                    <td style="text-align:right;white-space:nowrap;">${added
                        ? `<span class="sink-pill ok">${t('components.alreadyAdded', 'added')}</span>`
                        : `<button data-admin class="btn btn-secondary btn-sm" ${this._act('addComponentUi', [f.type, f.instance, f.name || f.product || ''])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('components.addThis', 'Add')}</button>`}</td>
                </tr>`; }).join('')}</tbody></table></div>`;
    },
});
