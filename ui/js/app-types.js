/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// device types — augments JanitzaMonitor.prototype
//
// Templates → Device types: the one catalog every list, filter and menu
// reads (GET /api/device-types, ADR-0027). The built-in kinds carry
// behaviour and are shown read-only; an administrator adds types of their
// own — words per language, an icon from a fixed set, and the built-in kind
// it behaves like (what goes out on MQTT). A type something uses is not
// deleted: the gateway names what holds it.

Object.assign(JanitzaMonitor.prototype, {

    // ── "+ Add type…" where a type is picked (the device's type, a device
    // measured only by its meters): a small form in place, the new type
    // created and picked without leaving the page ───────────────────────
    _typeSlug(words) {
        let s = this._fold ? this._fold(words) : String(words || '').toLowerCase();
        s = s.replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
        if (s && !/^[a-z]/.test(s)) s = `t_${s}`;
        return s.slice(0, 32).replace(/_+$/, '');
    },

    // p: an id prefix unique on the page; exclude: built-ins it may not
    // behave like here (a meter-only device is never a meter)
    _typeFormHtml(p, { exclude = [] } = {}) {
        const t = (k, d, x) => this.t(k, d, x);
        const cat = this._deviceTypes || {};
        const icons = Object.keys(cat.icons || { other: 'bi-box' });
        const kinds = (cat.behaves_like || ['other']).filter(k => !exclude.includes(k));
        return `
        <div class="type-new" data-type-new="${this._esc(p)}">
            <button data-admin type="button" class="btn btn-ghost btn-sm" id="${p}TypeAddBtn" aria-expanded="false" aria-controls="${p}TypeForm">
                <i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('types.addInline', 'Add type…')}</button>
            <fieldset id="${p}TypeForm" class="type-new-form" hidden style="border:1px solid var(--border,#e5e7eb);border-radius:8px;padding:10px 12px;margin:8px 0;">
                <legend class="form-label" style="padding:0 4px;">${t('types.addTitle', 'Add a device type')}</legend>
                <div class="form-row" style="flex-wrap:wrap;">
                    <div class="form-group"><label class="form-label" for="${p}TnEn">${t('types.f.en', 'Name in English')} *</label>
                        <input class="input input-sm" id="${p}TnEn" maxlength="40" autocomplete="off" aria-describedby="${p}TnEnHint">
                        <div class="field-hint" id="${p}TnEnHint">${t('types.f.enHint', 'Shown in every language that has no words of its own')}</div></div>
                    <div class="form-group"><label class="form-label" for="${p}TnRo">${t('types.f.ro', 'Name in Romanian')}</label>
                        <input class="input input-sm" id="${p}TnRo" maxlength="40" autocomplete="off" lang="ro"></div>
                    <div class="form-group"><label class="form-label" for="${p}TnId">${t('types.f.id', 'Id')} *</label>
                        <input class="input input-sm" id="${p}TnId" maxlength="32" autocomplete="off" spellcheck="false" aria-describedby="${p}TnIdHint">
                        <div class="field-hint" id="${p}TnIdHint">${t('types.f.idAuto', 'Made from the English name; a-z 0-9 _, it cannot be changed later')}</div></div>
                    <div class="form-group"><label class="form-label" for="${p}TnIcon">${t('types.f.icon', 'Icon')}</label>
                        <select class="input input-sm" id="${p}TnIcon">${icons.map(i => `<option value="${this._esc(i)}" ${i === 'other' ? 'selected' : ''}>${this._esc(t(`types.icon.${i}`, i))}</option>`).join('')}</select></div>
                    <div class="form-group"><label class="form-label" for="${p}TnLike">${t('types.f.behaves', 'Behaves like')}</label>
                        <select class="input input-sm" id="${p}TnLike" aria-describedby="${p}TnLikeHint">${kinds.map(k => `<option value="${this._esc(k)}" ${k === 'other' ? 'selected' : ''}>${this._esc(this._kindLabel(k))}</option>`).join('')}</select>
                        <div class="field-hint" id="${p}TnLikeHint">${t('types.f.behavesHint', 'What it counts as for site totals and on MQTT')}</div></div>
                </div>
                <div class="field-error" id="${p}TnErr" role="alert" hidden></div>
                <div style="display:flex;gap:6px;">
                    <button type="button" class="btn btn-primary btn-sm" id="${p}TnCreate">${t('types.create', 'Create and pick')}</button>
                    <button type="button" class="btn btn-ghost btn-sm" id="${p}TnCancel">${t('common.cancel', 'Cancel')}</button>
                </div>
            </fieldset>
        </div>`;
    },

    // onCreated(type) runs once the type exists (the catalog is reloaded)
    _typeFormWire(p, onCreated) {
        const $ = s => document.getElementById(p + s);
        const btn = $('TypeAddBtn'), form = $('TypeForm');
        if (!btn || !form) return;
        let idTouched = false;
        const toggle = open => {
            form.hidden = !open;
            btn.setAttribute('aria-expanded', String(open));
            if (open) $('TnEn').focus(); else btn.focus();
        };
        btn.addEventListener('click', () => toggle(form.hidden));
        $('TnEn').addEventListener('input', () => { if (!idTouched) $('TnId').value = this._typeSlug($('TnEn').value); });
        $('TnId').addEventListener('input', () => { idTouched = true; });
        $('TnCancel').addEventListener('click', () => toggle(false));
        form.addEventListener('keydown', e => {
            if (e.key === 'Escape') { e.stopPropagation(); e.preventDefault(); toggle(false); }
            if (e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); $('TnCreate').click(); }
        });
        $('TnCreate').addEventListener('click', async () => {
            const err = $('TnErr');
            err.hidden = true;
            form.querySelectorAll('[aria-invalid]').forEach(x => x.removeAttribute('aria-invalid'));
            const labels = { en: $('TnEn').value.trim() };
            if ($('TnRo').value.trim()) labels.ro = $('TnRo').value.trim();
            const body = { id: $('TnId').value.trim(), labels, icon: $('TnIcon').value, behaves_like: $('TnLike').value };
            let r, d;
            try {
                r = await fetch('/api/device-types', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
                d = await r.json().catch(() => ({}));
            } catch (e) { r = { ok: false, status: 0 }; d = {}; }
            if (!r.ok) {
                const fields = (d.detail && d.detail.fields) || {};
                const map = { id: 'TnId', 'labels.en': 'TnEn', labels: 'TnEn', 'labels.ro': 'TnRo', icon: 'TnIcon', behaves_like: 'TnLike' };
                let first = null;
                for (const k of Object.keys(fields)) {
                    const el = $(map[k] || 'TnEn');
                    el?.setAttribute('aria-invalid', 'true');
                    first = first || el;
                }
                err.textContent = this._respError(r, d);
                err.hidden = false;
                (first || $('TnEn')).focus();
                return;
            }
            await this._loadDeviceTypes();
            form.hidden = true;
            btn.setAttribute('aria-expanded', 'false');
            ['TnEn', 'TnRo', 'TnId'].forEach(s => { $(s).value = ''; });
            idTouched = false;
            this.showToast('success', this.t('types.saved', 'Type saved'), this._kindLabel(d.type.id));
            onCreated?.(d.type);
        });
    },

    // ── a device's type, picked by an administrator (⋯ → Type…: the Edit
    // device modal's Identity, L3.4) — the choices it offers ─────────────
    // [{value, label}]: "derive it" first, then every type (built-in and the
    // administrator's); a device measured only by its meters: the kinds the
    // gateway allows for it and a user type that is not a meter — nothing to
    // derive it from
    async _typeChoices(dev) {
        const t = (k, d, p) => this.t(k, d, p);
        await this._loadDeviceTypes();                 // a type added meanwhile is offered
        const derived = dev.kind_source === 'override' ? '' : (dev.type || dev.kind || 'other');
        if (dev.protocol !== 'none') {
            return [{ value: '', label: t('kind.derive', 'Not set — derive it ({kind})', { kind: this._kindLabel(derived || 'other') }) }]
                .concat(this._KIND_ORDER.filter(k => k !== 'other').map(k => ({ value: k, label: this._kindLabel(k) })));
        }
        const mo = ((await (this._meterRoles ? this._meterRoles() : null)) || {}).meter_only_kinds || {};
        const us = (this._deviceTypes?.types || []).filter(x => !x.builtin);
        return [...(mo.first || []), ...(mo.more || []), ...us.filter(x => x.behaves_like !== 'meter').map(x => x.id)]
            .map(k => ({ value: k, label: this._kindLabel(k) }));
    },

    async renderDeviceTypesPanel() {
        const host = document.getElementById('tmTypesPanel');
        if (!host) return;
        const t = (k, d, p) => this.t(k, d, p);
        const cat = await this._loadDeviceTypes();
        if (!cat) {
            host.innerHTML = `<p class="field-hint" role="alert">${t('types.loadFailed', 'The device types could not be read.')}</p>`;
            return;
        }
        const row = x => {
            const labels = Object.entries(x.labels || {}).map(([l, w]) => `<span class="dev-chip" lang="${this._esc(l)}">${this._esc(l)}: ${this._esc(w)}</span>`).join(' ');
            const name = this._kindLabel(x.id);
            return `<tr data-type-row="${this._esc(x.id)}">
                <td><i aria-hidden="true" class="bi ${this._esc(x.icon_class || 'bi-box')}"></i> <b>${this._esc(name)}</b><br><code>${this._esc(x.id)}</code></td>
                <td data-label="${this._esc(t('types.col.labels', 'Words'))}">${labels}</td>
                <td data-label="${this._esc(t('types.col.behaves', 'Behaves like'))}">${x.builtin ? `<span class="field-hint">${t('types.builtin', 'built-in')}</span>` : this._esc(this._kindLabel(x.behaves_like))}</td>
                <td class="types-tpl"><button type="button" class="btn btn-ghost btn-sm" aria-expanded="false" aria-controls="typeTpl_${this._esc(x.id)}"
                        ${this._act('typeTemplatesToggle', [x.id], { el: true })}>${this.tn('types.templatesN', x.templates, '{n} templates')}</button>
                    <div id="typeTpl_${this._esc(x.id)}" class="field-hint" hidden></div></td>
                <td class="types-acts" style="white-space:nowrap;">${x.builtin ? '' : `
                    <button data-admin type="button" class="btn btn-ghost btn-sm" ${this._act('typeEdit', [x.id])} ${this._ib(t('common.edit', 'Edit'), name)}><i aria-hidden="true" class="bi bi-pencil"></i></button>
                    <button data-admin type="button" class="btn btn-ghost btn-sm" ${this._act('typeDelete', [x.id])} ${this._ib(t('common.delete', 'Delete'), name)}><i aria-hidden="true" class="bi bi-trash"></i></button>`}</td>
            </tr>`;
        };
        const users = cat.types.filter(x => !x.builtin);
        host.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap;">
                <p class="field-hint" style="margin:0;max-width:640px;">${t('types.lead', 'What a device is. The built-in kinds carry behaviour (site totals, meter-only devices, dashboards). A type of your own behaves like one of them: that is what other systems see on MQTT, while the gateway shows your words.')}</p>
                <button data-admin type="button" class="btn btn-primary btn-sm" id="typeAddBtn" ${this._act('typeEdit', [''])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('types.add', 'Add type')}</button>
            </div>
            <div class="table-container"><table class="data-table types-table" aria-label="${this._esc(t('types.tab', 'Device types'))}">
                <thead><tr><th scope="col">${t('types.col.type', 'Type')}</th><th scope="col">${t('types.col.labels', 'Words')}</th>
                    <th scope="col">${t('types.col.behaves', 'Behaves like')}</th><th scope="col">${t('types.col.templates', 'Templates')}</th><th scope="col"><span class="sr-only">${t('common.actions', 'Actions')}</span></th></tr></thead>
                <tbody>${users.map(row).join('')}${cat.types.filter(x => x.builtin).map(row).join('')}</tbody>
            </table></div>
            ${users.length ? '' : `<p class="field-hint">${t('types.noneYet', 'No types of your own yet.')}</p>`}`;
        this._applyRoleGates?.(host);
    },

    async typeTemplatesToggle(id, el) {
        const box = document.getElementById(`typeTpl_${id}`);
        if (!box || !el) return;
        const open = el.getAttribute('aria-expanded') === 'true';
        el.setAttribute('aria-expanded', String(!open));
        box.hidden = open;
        if (open || box.dataset.loaded) return;
        try {
            const r = await fetch(`/api/device-templates?type=${encodeURIComponent(id)}&sort=name`);
            const d = await r.json();
            const list = (d.templates || []);
            box.innerHTML = list.length
                ? `<ul style="margin:4px 0 0;padding-left:18px;">${list.map(x => `<li>${this._esc(x.name || x.id)}</li>`).join('')}</ul>`
                : this._esc(this.t('types.noTemplates', 'No template for this type yet.'));
            box.dataset.loaded = '1';
        } catch (e) {
            box.textContent = this.t('types.loadFailed', 'The device types could not be read.');
        }
    },

    async typeEdit(id) {
        const t = (k, d, p) => this.t(k, d, p);
        const cat = this._deviceTypes || await this._loadDeviceTypes();
        const cur = id ? (cat?.types || []).find(x => x.id === id) : null;
        if (id && !cur) return;
        const icons = Object.keys(cat?.icons || { other: 'bi-box' });
        const kinds = (cat?.behaves_like || ['other']);
        const fields = [
            ...(cur ? [] : [{ name: 'id', kind: 'text', label: t('types.f.id', 'Id'), required: true,
                              placeholder: 'chiller', hint: t('types.f.idHint', 'a-z 0-9 _, starts with a letter; it cannot be changed later') }]),
            { name: 'en', kind: 'text', label: t('types.f.en', 'Name in English'), required: true,
              initial: cur?.labels?.en || '', hint: t('types.f.enHint', 'Shown in every language that has no words of its own') },
            { name: 'ro', kind: 'text', label: t('types.f.ro', 'Name in Romanian'), required: false, initial: cur?.labels?.ro || '' },
            { name: 'icon', kind: 'select', label: t('types.f.icon', 'Icon'), required: true, initial: cur?.icon || 'other',
              options: icons.map(i => ({ value: i, label: t(`types.icon.${i}`, i) })) },
            { name: 'behaves_like', kind: 'select', label: t('types.f.behaves', 'Behaves like'), required: true,
              initial: cur?.behaves_like || 'other', hint: t('types.f.behavesHint', 'What it counts as for site totals and on MQTT'),
              options: kinds.map(k => ({ value: k, label: this._kindLabel(k) })) },
        ];
        let saved = null;
        const got = await this.askValues({
            title: cur ? t('types.editTitle', 'Edit the type {name}', { name: this._kindLabel(id) }) : t('types.addTitle', 'Add a device type'),
            fields, confirmLabel: t('common.save', 'Save'),
            submit: async v => {
                const labels = { en: (v.en || '').trim() };
                if ((v.ro || '').trim()) labels.ro = v.ro.trim();
                const body = { labels, icon: v.icon, behaves_like: v.behaves_like };
                if (!cur) body.id = (v.id || '').trim();
                const r = await fetch(cur ? `/api/device-types/${encodeURIComponent(id)}` : '/api/device-types', {
                    method: cur ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
                const d = await r.json().catch(() => ({}));
                if (!r.ok) return this._respError(r, d);
                saved = d.type;
                return null;
            },
        });
        if (!got || !saved) return;
        this.showToast('success', t('types.saved', 'Type saved'), this._kindLabel(saved.id));
        await this._loadDeviceTypes();
        await this.renderDeviceTypesPanel();
        document.querySelector(`[data-type-row="${CSS.escape(saved.id)}"] button`)?.focus();
    },

    async typeDelete(id) {
        const t = (k, d, p) => this.t(k, d, p);
        const name = this._kindLabel(id);
        const ok = await this.confirmAction({
            title: t('types.deleteTitle', 'Delete the type {name}?', { name }), danger: true,
            message: t('types.deleteMsg', 'Only a type nothing uses can be deleted.'),
            confirmLabel: t('common.delete', 'Delete'),
            submit: async () => {
                const r = await fetch(`/api/device-types/${encodeURIComponent(id)}`, { method: 'DELETE' });
                const d = await r.json().catch(() => ({}));
                // a 409 that says config.yaml is blocked is not "in use": its words are shown
                if (r.status === 409 && !d.detail?.config_blocked) {
                    const det = d.detail || {};
                    const parts = [
                        det.devices?.length ? `${t('types.usedDevices', 'devices')}: ${det.devices.join(', ')}` : '',
                        det.templates?.length ? `${t('types.usedTemplates', 'templates')}: ${det.templates.join(', ')}` : '',
                        det.groups?.length ? `${t('types.usedGroups', 'installation groups')}: ${det.groups.join(', ')}` : '',
                    ].filter(Boolean).join('; ');
                    return t('types.inUse', 'In use — {what}. Change their type first.', { what: parts });
                }
                if (!r.ok) return this._respError(r, d);
                return null;
            },
        });
        if (!ok) return;
        this.showToast('success', t('types.deleted', 'Type deleted'), name);
        await this._loadDeviceTypes();
        await this.renderDeviceTypesPanel();
        document.getElementById('typeAddBtn')?.focus();
    },
});
