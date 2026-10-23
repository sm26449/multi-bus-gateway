/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
/* Asset attributes (docs/attributes.md): what a piece of equipment is, where,
 * and how it is built — picked from the gateway's catalog with "+", never
 * invented. One renderer serves the plant (Settings → Plant), an installation
 * and a device. Every category is optional; the server checks each one.
 */
Object.assign(JanitzaMonitor.prototype, {

    _ATTR_COMPASS: [['N', 0], ['NE', 45], ['E', 90], ['SE', 135], ['S', 180], ['SW', 225], ['W', 270], ['NW', 315]],

    async _attrCatalogGet() {
        if (!this._attrCatalog) {
            try { this._attrCatalog = await (await fetch('/api/attributes/catalog')).json(); }
            catch (e) { this._attrCatalog = null; }
        }
        return this._attrCatalog;
    },

    // everything of one attributes section is looked up inside ITS host: the
    // plant's and a device's sections can both be in the page at once
    _attrQ(sel) { return this._attrState?.host?.querySelector(sel) || null; },

    _attrLabel(cat, field, fallback) {
        return field ? this.t(`attr.${cat}.${field}`, fallback) : this.t(`attr.cat.${cat}`, fallback);
    },

    // kind: 'site' | 'installations' | 'devices'
    async _loadAttributes(kind, id, host) {
        if (!host) return;
        const t = (k, d) => this.t(k, d);
        const catalog = await this._attrCatalogGet();
        let data;
        try {
            const r = await fetch(kind === 'site' ? '/api/attributes/site' : `/api/attributes/${kind}/${encodeURIComponent(id)}`);
            data = await r.json();
            if (!r.ok) throw new Error((data.detail?.errors || [data.detail || r.status]).join('; '));
        } catch (e) {
            host.innerHTML = `<p class="field-error">${this._esc(e.message)}</p>`;
            return;
        }
        this._attrState = { kind, id, host, data, catalog, editing: null };
        let extra = '';
        if (kind === 'site') {
            try { this._attrAll = await (await fetch('/api/attributes')).json(); } catch (e) { this._attrAll = null; }
            extra = this._attrSiteExtrasHtml(this._attrAll);
        }
        host.innerHTML = this._attrHtml(data, catalog) + extra;
    },

    _attrHtml(data, catalog) {
        const t = (k, d) => this.t(k, d);
        const cats = catalog?.categories || {};
        const held = data.attributes || {};
        const present = Object.keys(held);
        const offered = (data.offered || []).filter(o => !present.includes(o.category));
        const eff = data.effective_location;
        const effLine = (data.level !== 'site' && eff && !held.location)
            ? `<div class="field-hint" style="margin:0 0 10px;"><i aria-hidden="true" class="bi bi-geo-alt"></i> ${t('attr.locationFrom', 'Location')}: ${this._esc(eff.value?.latitude_deg ?? '—')}, ${this._esc(eff.value?.longitude_deg ?? '—')} · ${this._esc(t('attr.inheritedFrom', 'from'))} ${this._esc(eff.from)}</div>` : '';
        const add = offered.length ? `
            <div class="attr-add" data-admin style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:8px;">
                <select class="input input-sm attr-add-sel" aria-label="${this._esc(t('attr.add', 'Add an attribute'))}" style="max-width:280px;">
                    ${offered.map(o => `<option value="${this._esc(o.category)}">${this._esc(this._attrLabel(o.category, '', cats[o.category]?.label || o.category))}${o.fits ? '' : ' ·'}</option>`).join('')}
                </select>
                <button class="btn btn-secondary btn-sm" ${this._act('attrAdd')}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('attr.add', 'Add an attribute')}</button>
                <span class="field-hint" style="margin:0;">${t('attr.addHint', 'From the gateway’s catalog. Everything is optional; “·” marks a category that does not usually fit this equipment.')}</span>
            </div>` : '';
        return `
        <div class="settings-card attr-card">
            <div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-tags"></i> ${t('attr.title', 'Attributes')}</h3></div>
            <div class="settings-card-body">
                <p class="field-hint" style="margin:0 0 10px;">${t('attr.intro', 'What this equipment is, where, and how it is built: facts other systems (an EMS, an HMI, a SCADA) read from here. Nothing is required.')}</p>
                ${data.orphan ? `<div class="field-error" style="margin-bottom:8px;">${t('attr.orphanHere', 'Kept for an id that no longer exists (a deleted device can be restored).')}</div>` : ''}
                ${effLine}
                <div class="attr-cats">${present.map(c => this._attrCatHtml(c, held[c], cats[c])).join('') || `<div class="field-hint">${t('attr.none', 'No attributes yet.')}</div>`}</div>
                <div class="attr-editor-host"></div>
                ${data.orphan ? '' : add}
            </div>
        </div>`;
    },

    _attrFmt(cat, fid, v, spec) {
        if (v === undefined || v === null || v === '') return '—';
        if (fid === 'azimuth_deg') {
            const c = this._ATTR_COMPASS.find(([, d]) => Math.abs(d - v) < 1e-9);
            return `${this._esc(v)}°${c ? ` (${c[0]})` : ''}`;
        }
        if (spec?.type === 'choice') return this._esc(this.t(`attr.${cat}.${fid}.${v}`, String(v)));
        return `${this._esc(v)}${spec?.unit ? ' ' + this._esc(spec.unit) : ''}`;
    },

    _attrCatHtml(cat, value, spec) {
        const t = (k, d) => this.t(k, d);
        const fields = spec?.fields || {};
        const rows = Array.isArray(value) ? value : [value];
        const head = Object.keys(fields).filter(f => rows.some(r => r && r[f] !== undefined));
        const body = spec?.repeatable
            ? `<div class="table-container"><table class="data-table"><thead><tr>${head.map(f => `<th>${this._esc(this._attrLabel(cat, f, fields[f].label || f))}</th>`).join('')}</tr></thead>
                <tbody>${rows.map(r => `<tr>${head.map(f => `<td>${this._attrFmt(cat, f, r[f], fields[f])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`
            : `<dl class="attr-dl" style="display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:0;">${head.map(f =>
                `<dt class="field-hint" style="margin:0;">${this._esc(this._attrLabel(cat, f, fields[f].label || f))}</dt><dd style="margin:0;">${this._attrFmt(cat, f, rows[0][f], fields[f])}</dd>`).join('')}</dl>`;
        const st = this._attrState || {};
        const copy = (spec?.repeatable && st.kind === 'devices')
            ? `<button class="btn btn-ghost btn-sm" data-admin ${this._act('attrCopyOpen', [cat])} title="${this._esc(t('attr.copyHint', 'Copy to other units'))}"><i aria-hidden="true" class="bi bi-copy"></i> ${t('attr.copy', 'Copy to…')}</button>` : '';
        return `
            <div class="attr-cat" data-attr-cat="${this._esc(cat)}" style="border:1px solid var(--border,#e5e7eb);border-radius:8px;padding:10px 12px;margin-bottom:10px;">
                <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px;">
                    <b>${this._esc(this._attrLabel(cat, '', spec?.label || cat))}</b>
                    <span style="margin-left:auto;display:flex;gap:4px;">
                        ${copy}
                        <button class="btn btn-ghost btn-sm" data-admin ${this._act('attrEdit', [cat])}><i aria-hidden="true" class="bi bi-pencil"></i> ${t('common.edit', 'Edit')}</button>
                        <button class="btn btn-ghost btn-sm" data-admin ${this._act('attrDelete', [cat])} aria-label="${this._esc(t('common.delete', 'Delete'))}"><i aria-hidden="true" class="bi bi-trash"></i></button>
                    </span>
                </div>
                ${body}
            </div>`;
    },

    attrAdd() {
        const cat = this._attrQ('.attr-add-sel')?.value;
        if (cat) this.attrEdit(cat, true);
    },

    // ── the editor: a form built from the catalog ───────────────────────────
    attrEdit(cat, isNew = false) {
        const st = this._attrState;
        if (!st) return;
        const spec = st.catalog?.categories?.[cat];
        if (!spec) return;
        let cur = st.data.attributes?.[cat];
        // coordinates shown rounded are not the stored ones: start empty
        // rather than offer the rounded values for saving
        if (cat === 'location' && st.data.exact === false) cur = {};
        const rows = spec.repeatable ? (Array.isArray(cur) && cur.length ? cur : [{}]) : [cur || {}];
        st.editing = { cat, rows: rows.map(r => ({ ...r })) };
        this._attrRenderEditor();
    },

    _attrFieldInput(cat, fid, f, v, i) {
        const id = `attrF_${i}_${fid}`;
        const lbl = this._esc(this._attrLabel(cat, fid, f.label || fid));
        const req = f.required ? ' *' : '';
        let input;
        if (f.type === 'text') {
            input = `<input class="input input-sm" id="${id}" type="text" maxlength="${f.max_len || 64}" value="${this._esc(v ?? '')}" data-attr-f="${fid}" data-attr-row="${i}" spellcheck="false">`;
        } else if (f.type === 'choice') {
            input = `<select class="input input-sm" id="${id}" data-attr-f="${fid}" data-attr-row="${i}">
                <option value=""></option>${f.choices.map(c => `<option value="${this._esc(c)}" ${String(v) === String(c) ? 'selected' : ''}>${this._esc(this.t(`attr.${cat}.${fid}.${c}`, String(c)))}</option>`).join('')}</select>`;
        } else if (fid === 'azimuth_deg') {
            input = `<div style="display:flex;gap:4px;">
                <select class="input input-sm" data-attr-compass="${i}" aria-label="${lbl}" style="max-width:70px;">
                    <option value=""></option>${this._ATTR_COMPASS.map(([n, d]) => `<option value="${d}" ${Number(v) === d ? 'selected' : ''}>${n}</option>`).join('')}</select>
                <input class="input input-sm" id="${id}" type="number" step="any" min="0" max="360" value="${this._esc(v ?? '')}" data-attr-f="${fid}" data-attr-row="${i}" style="max-width:90px;"></div>`;
        } else if (cat === 'location' && (fid === 'latitude_deg' || fid === 'longitude_deg')) {
            // coordinates are identity: masked, with the eye; a pasted "48.86, 2.35" fills both
            input = this._secretInput(id, v ?? '', `data-attr-f="${fid}" data-attr-row="${i}" inputmode="decimal" placeholder="${fid === 'latitude_deg' ? '48.86' : '2.35'}"`);
        } else {
            input = `<input class="input input-sm" id="${id}" type="number" step="${f.type === 'integer' ? 1 : 'any'}" ${f.min != null ? `min="${f.min}"` : ''} ${f.max != null ? `max="${f.max}"` : ''} value="${this._esc(v ?? '')}" data-attr-f="${fid}" data-attr-row="${i}">`;
        }
        return `<div class="form-group" style="min-width:120px;"><label class="form-label" for="${id}">${lbl}${req}${f.unit ? ` <span class="field-hint" style="margin:0;">(${this._esc(f.unit)})</span>` : ''}</label>${input}</div>`;
    },

    _attrRenderEditor() {
        const st = this._attrState;
        const host = this._attrQ('.attr-editor-host');
        if (!st?.editing || !host) return;
        const t = (k, d) => this.t(k, d);
        const { cat, rows } = st.editing;
        const spec = st.catalog.categories[cat];
        const fields = spec.fields;
        host.innerHTML = `
            <div class="attr-editor" style="border:1px solid var(--accent,#3b82f6);border-radius:8px;padding:12px;margin:10px 0;">
                <b>${this._esc(this._attrLabel(cat, '', spec.label))}</b>
                ${cat === 'location' && st.data.exact === false && st.data.attributes?.location ? `<div class="field-hint">${t('attr.locationHidden', 'The stored coordinates are shown rounded here. Type the new ones to replace them; leaving the form keeps them.')}</div>` : ''}
                ${rows.map((r, i) => `
                <div class="form-row align-end" data-attr-rowbox="${i}" style="flex-wrap:wrap;margin-top:8px;">
                    ${Object.entries(fields).map(([fid, f]) => this._attrFieldInput(cat, fid, f, r[fid], i)).join('')}
                    ${spec.repeatable && rows.length > 1 ? `<button class="btn btn-ghost btn-sm" ${this._act('attrRowDel', [i])} aria-label="${this._esc(t('attr.rowDel', 'Remove this row'))}"><i aria-hidden="true" class="bi bi-x-lg"></i></button>` : ''}
                </div>`).join('')}
                ${spec.repeatable ? `<button class="btn btn-ghost btn-sm" ${this._act('attrRowAdd')}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('attr.rowAdd', 'Another row (another orientation)')}</button>` : ''}
                <div class="field-error attr-err" role="alert" style="white-space:pre-line;"></div>
                <div style="display:flex;gap:6px;margin-top:8px;">
                    <button data-admin class="btn btn-primary btn-sm" ${this._act('attrSave')}><i aria-hidden="true" class="bi bi-check-lg"></i> ${t('common.save', 'Save')}</button>
                    <button class="btn btn-ghost btn-sm" ${this._act('attrCancel')}>${t('common.cancel', 'Cancel')}</button>
                </div>
            </div>`;
        host.querySelectorAll('[data-attr-compass]').forEach(sel => sel.addEventListener('change', () => {
            const inp = host.querySelector(`input[data-attr-f="azimuth_deg"][data-attr-row="${sel.dataset.attrCompass}"]`);
            if (inp && sel.value !== '') inp.value = sel.value;
        }));
        // one paste of "lat, lon" into either field fills both
        host.querySelectorAll('input[data-attr-f="latitude_deg"], input[data-attr-f="longitude_deg"]').forEach(inp =>
            inp.addEventListener('paste', ev => {
                const txt = (ev.clipboardData || window.clipboardData)?.getData('text') || '';
                const m = txt.trim().match(/^(-?\d+(?:\.\d+)?)\s*[,;\s]\s*(-?\d+(?:\.\d+)?)$/);
                if (!m) return;
                ev.preventDefault();
                const row = inp.dataset.attrRow;
                host.querySelector(`input[data-attr-f="latitude_deg"][data-attr-row="${row}"]`).value = m[1];
                host.querySelector(`input[data-attr-f="longitude_deg"][data-attr-row="${row}"]`).value = m[2];
            }));
    },

    _attrCollect() {
        const st = this._attrState;
        const host = this._attrQ('.attr-editor-host');
        const spec = st.catalog.categories[st.editing.cat];
        const rows = st.editing.rows.map((_r, i) => {
            const row = {};
            host.querySelectorAll(`[data-attr-row="${i}"][data-attr-f]`).forEach(el => {
                const fid = el.dataset.attrF;
                const f = spec.fields[fid];
                const raw = String(el.value ?? '').trim();
                if (raw === '') return;
                if (f.type === 'text') {
                    row[fid] = raw;
                } else if (f.type === 'choice') {
                    const c = f.choices.find(x => String(x) === raw);
                    row[fid] = c !== undefined ? c : raw;
                } else {
                    // a comma decimal from a localized keyboard becomes a dot;
                    // anything else non-numeric goes as text so the server names it
                    const n = Number(raw.replace(',', '.'));
                    row[fid] = Number.isFinite(n) ? n : raw;
                }
            });
            return row;
        });
        st.editing.rows = rows;
        return spec.repeatable ? rows : rows[0];
    },

    attrRowAdd() { this._attrCollect(); this._attrState.editing.rows.push({}); this._attrRenderEditor(); },
    attrRowDel(i) { this._attrCollect(); this._attrState.editing.rows.splice(i, 1); this._attrRenderEditor(); },
    attrCancel() { const st = this._attrState; if (st) { st.editing = null; } const h = this._attrQ('.attr-editor-host'); if (h) h.innerHTML = ''; },

    _attrUrl(cat) {
        const st = this._attrState;
        return st.kind === 'site' ? `/api/attributes/site/${cat}` : `/api/attributes/${st.kind}/${encodeURIComponent(st.id)}/${cat}`;
    },

    async attrSave() {
        const st = this._attrState;
        if (!st?.editing) return;
        const value = this._attrCollect();
        const err = this._attrQ('.attr-err');
        try {
            const r = await fetch(this._attrUrl(st.editing.cat), {
                method: 'PUT', headers: { 'Content-Type': 'application/json', 'If-Match': st.data.rev || '' },
                body: JSON.stringify(value) });
            const body = await r.json().catch(() => ({}));
            if (r.status === 412) {
                this.showToast('warning', this.t('attr.changedMeanwhile', 'Changed meanwhile'),
                               this.t('attr.changedMeanwhileHint', 'Someone else saved these attributes. Here is what is stored now; make your change again.'));
                await this._loadAttributes(st.kind, st.id, st.host);
                return;
            }
            if (!r.ok) {
                if (err) err.textContent = (body.detail?.errors || [body.detail || r.status]).join('\n');
                return;
            }
            (body.warnings || []).forEach(w => this.showToast('warning', this.t('attr.warning', 'Check'), w));
            this.showToast('success', this.t('attr.saved', 'Saved'), this._attrLabel(st.editing.cat, '', st.editing.cat));
            await this._loadAttributes(st.kind, st.id, st.host);
        } catch (e) { if (err) err.textContent = e.message; }
    },

    async attrDelete(cat) {
        const st = this._attrState;
        if (!st || !await this.confirmAction({
            title: this.t('attr.deleteTitle', 'Delete attribute {name}', { name: this._attrLabel(cat, '', cat) }),
            confirmLabel: this.t('common.delete', 'Delete'), danger: true,
        })) return;
        const r = await fetch(this._attrUrl(cat), { method: 'DELETE', headers: { 'If-Match': st.data.rev || '' } });
        if (!r.ok) {
            const body = await r.json().catch(() => ({}));
            this.showToast('error', this.t('attr.notDeleted', 'Not deleted'), (body.detail?.errors || [r.status]).join('; '));
        }
        await this._loadAttributes(st.kind, st.id, st.host);
    },

    // ── copy a device's rows to other units ─────────────────────────────────
    async attrCopyOpen(cat) {
        const st = this._attrState;
        const t = (k, d) => this.t(k, d);
        let devs = [];
        try { devs = (await (await fetch('/api/devices')).json()).devices || []; } catch (e) { /* none */ }
        const self = devs.find(d => d.id === st.id);
        const same = devs.filter(d => d.id !== st.id && (self?.endpoint_id ? d.endpoint_id === self.endpoint_id : true));
        const host = this._attrQ('.attr-editor-host');
        host.innerHTML = `
            <div class="attr-editor" style="border:1px solid var(--accent,#3b82f6);border-radius:8px;padding:12px;margin:10px 0;">
                <b>${t('attr.copyTitle', 'Copy to other units')}</b>
                <p class="field-hint">${t('attr.copyIntro', 'The values are copied, not linked: a later change on one unit stays on that unit.')}</p>
                ${same.map(d => `<label class="form-label" style="display:flex;gap:8px;align-items:center;font-weight:normal;">
                    <input type="checkbox" data-attr-copy="${this._esc(d.id)}"> ${this._esc(d.name || d.id)} <span class="dev-chip">${this._esc(d.id)}</span></label>`).join('') || `<div class="field-hint">${t('attr.copyNone', 'No other units.')}</div>`}
                <div class="field-error attr-err" role="alert" style="white-space:pre-line;"></div>
                <div style="display:flex;gap:6px;margin-top:8px;">
                    <button data-admin class="btn btn-primary btn-sm" ${this._act('attrCopyRun', [cat])}><i aria-hidden="true" class="bi bi-copy"></i> ${t('attr.copy', 'Copy to…')}</button>
                    <button class="btn btn-ghost btn-sm" ${this._act('attrCancel')}>${t('common.cancel', 'Cancel')}</button>
                </div>
            </div>`;
    },

    async attrCopyRun(cat) {
        const st = this._attrState;
        const to = [...(this._attrState?.host || document).querySelectorAll('[data-attr-copy]:checked')].map(c => c.dataset.attrCopy);
        const err = this._attrQ('.attr-err');
        if (!to.length) { if (err) err.textContent = this.t('attr.copyPick', 'Pick at least one unit.'); return; }
        const r = await fetch(`/api/attributes/devices/${encodeURIComponent(st.id)}/${cat}/copy`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ to }) });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) { if (err) err.textContent = (body.detail?.errors || [r.status]).join('\n'); return; }
        (body.warnings || []).forEach(w => this.showToast('warning', this.t('attr.warning', 'Check'), w));
        this.showToast('success', this.t('attr.copied', 'Copied'), to.join(', '));
        await this._loadAttributes(st.kind, st.id, st.host);      // the document's revision moved
    },

    // ── the plant's own extras: publishing coordinates, orphans, warnings ──
    _attrSiteExtrasHtml(all) {
        const t = (k, d) => this.t(k, d);
        if (!all) return '';
        const mode = all.settings?.location_publish || 'coarse';
        const orphans = [...(all.orphans?.device || []).map(id => ['devices', id]),
                         ...(all.orphans?.installation || []).map(id => ['installations', id])];
        return `
        <div class="settings-card">
            <div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-shield-lock"></i> ${t('attr.privacy', 'Coordinates')}</h3></div>
            <div class="settings-card-body">
                <div class="form-group"><label class="form-label" for="attrPublish">${t('attr.publish', 'Shown to viewers and published as')}</label>
                    <select class="input" id="attrPublish" data-admin ${this._act('attrPublishSet', [], { value: true, on: 'change' })} style="max-width:320px;">
                        <option value="coarse" ${mode === 'coarse' ? 'selected' : ''}>${t('attr.publishCoarse', 'Coarse (2 decimals, about 1 km)')}</option>
                        <option value="exact" ${mode === 'exact' ? 'selected' : ''}>${t('attr.publishExact', 'Exact')}</option>
                        <option value="off" ${mode === 'off' ? 'selected' : ''}>${t('attr.publishOff', 'Not at all')}</option></select>
                    <div class="field-hint">${t('attr.publishHint', 'Coordinates locate a home. An admin always sees them exactly; logs and the backup diff show them coarse; a sanitised backup leaves them out.')}</div></div>
                ${(all.warnings || []).map(w => `<div class="field-error">${this._esc(w)}</div>`).join('')}
                ${orphans.length ? `<h4 style="margin:12px 0 6px;">${t('attr.orphans', 'Kept for ids that no longer exist')}</h4>
                    <p class="field-hint">${t('attr.orphansHint', 'A deleted device can be restored with them. Delete them when the equipment is gone for good.')}</p>
                    ${orphans.map(([k, id]) => `<div style="display:flex;gap:8px;align-items:center;margin-bottom:4px;"><span class="dev-chip">${this._esc(id)}</span>
                        <button class="btn btn-ghost btn-sm" data-admin ${this._act('attrDropOrphan', [k, id])}><i aria-hidden="true" class="bi bi-trash"></i> ${t('common.delete', 'Delete')}</button></div>`).join('')}` : ''}
            </div>
        </div>`;
    },

    async attrPublishSet(mode) {
        const r = await fetch('/api/attributes/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
                                                            body: JSON.stringify({ location_publish: mode }) });
        if (!r.ok) this.showToast('error', this.t('attr.notSaved', 'Not saved'), String(r.status));
        else this.showToast('success', this.t('attr.saved', 'Saved'), mode);
        const st = this._attrState;
        if (st) await this._loadAttributes(st.kind, st.id, st.host);
    },

    async attrDropOrphan(kind, id) {
        if (!await this.confirmAction({
            title: this.t('attr.orphanTitle', 'Delete what is kept for {id}', { id }),
            message: this.t('attr.orphanConfirm', 'Every attribute kept for this id is deleted.'),
            confirmLabel: this.t('common.delete', 'Delete'), danger: true,
        })) return;
        const r = await fetch(`/api/attributes/${kind}/${encodeURIComponent(id)}`, { method: 'DELETE' });
        if (!r.ok) this.showToast('error', this.t('attr.notDeleted', 'Not deleted'), String(r.status));
        const st = this._attrState;
        if (st) await this._loadAttributes(st.kind, st.id, st.host);
    },
});
