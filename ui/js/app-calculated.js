// calculated registers domain — augments JanitzaMonitor.prototype
// A "calculated register" is a formula-derived measurement (e.g. PF = P / S)
// that flows to every sink like a real one. The builder lets the user compose an
// expression from the device's own measurement fields + a whitelist of functions,
// with a live preview, plus one-click parameterized presets.
Object.assign(JanitzaMonitor.prototype, {

    async _initCalculated(deviceId) {
        this._calc = { deviceId, items: [], fields: [], presets: [], userTemplates: [],
                       functions: [], operators: [], editing: null, fieldFilter: '' };
        const host = document.querySelector('#deviceDetailView [data-dpanel="calculated"]');
        if (host) host.innerHTML = `<div class="calc-loading">${this.t('common.loading', 'Loading…')}</div>`;
        try {
            const [calc, regs, presets, fns, tpls] = await Promise.all([
                fetch(`/api/devices/${encodeURIComponent(deviceId)}/calculated`).then(r => r.json()),
                fetch(`/api/registers/selected?device=${encodeURIComponent(deviceId)}`).then(r => r.json()),
                fetch('/api/calculated/presets').then(r => r.json()),
                fetch('/api/calculated/functions').then(r => r.json()),
                fetch('/api/calculated/templates').then(r => r.json()),
            ]);
            this._calc.items = calc.calculated || [];
            this._calc.userTemplates = tpls.templates || [];
            this._calc.fields = (regs.registers || []).map(r => ({
                name: r.name, label: r.label || r.description || r.name,
                unit: r.unit || '', poll_group: r.poll_group || 'normal',
            })).filter(f => f.name);
            this._calc.presets = presets.presets || [];
            this._calc.functions = fns.functions || [];
            this._calc.operators = fns.operators || [];
        } catch (e) { console.error(e); }
        this._calcRender();
    },

    _calcPollGroups() {
        const s = new Set(this._calc.fields.map(f => f.poll_group));
        ['normal', 'realtime', 'slow'].forEach(g => s.add(g));
        return [...s];
    },

    _calcFmt(v) {
        if (typeof v !== 'number') return String(v);
        return Number.isInteger(v) ? String(v) : v.toFixed(Math.min(4, 3));
    },

    _calcRender() {
        const host = document.querySelector('#deviceDetailView [data-dpanel="calculated"]');
        if (!host) return;
        const t = this.t.bind(this);
        const builtinOpts = this._calc.presets.map(p =>
            `<option value="${this._esc(p.id)}">${this._esc(p.label)}</option>`).join('');
        const userOpts = this._calc.userTemplates.map(p =>
            `<option value="user:${this._esc(p.id)}">${this._esc(p.label || p.name)}</option>`).join('');
        const presetOpts = `<optgroup label="${t('calc.presets', 'Presets')}">${builtinOpts}</optgroup>`
            + (userOpts ? `<optgroup label="${t('calc.myTemplates', 'My templates')}">${userOpts}</optgroup>` : '');
        host.innerHTML = `
        <div class="settings-card">
            <div class="settings-card-header">
                <h3><i class="bi bi-calculator"></i> ${t('calc.title', 'Calculated measurements')}</h3>
            </div>
            <div class="settings-card-body">
                <p class="field-hint" style="margin-top:0"><i class="bi bi-info-circle"></i>
                    ${t('calc.help', 'Derive new measurements from existing ones with a formula (e.g. power factor = P / S). Calculated values flow to MQTT, InfluxDB and the JSON output like any real measurement.')}</p>
                <div class="calc-toolbar">
                    <button class="btn btn-primary btn-sm" onclick="app.calcAdd()"><i class="bi bi-plus-lg"></i> ${t('calc.add', 'Add')}</button>
                    <div class="calc-preset">
                        <label class="form-label" style="margin:0">${t('calc.fromPreset', 'From preset')}</label>
                        <select id="calcPresetSel" class="input input-sm"><option value="">—</option>${presetOpts}</select>
                        <button class="btn btn-secondary btn-sm" onclick="app.calcApplyPreset(document.getElementById('calcPresetSel').value)">${t('calc.use', 'Use')}</button>
                        <button class="btn btn-ghost btn-sm" title="${t('calc.deletePreset', 'Delete selected preset')}" onclick="app.calcDeleteSelectedPreset(document.getElementById('calcPresetSel').value)"><i class="bi bi-trash"></i></button>
                    </div>
                </div>
                ${this._calc.editing ? this._calcEditorHtml() : ''}
                <div class="calc-saved">
                    <div class="calc-saved-head">${t('calc.yourList', 'Your calculated measurements')} (${this._calc.items.length})</div>
                    ${this._calcListHtml()}
                    ${this._calc.items.length ? `<p class="field-hint"><i class="bi bi-broadcast"></i> ${t('calc.alsoIn', 'These also appear in Monitor, MQTT, InfluxDB and the JSON output — like any real measurement.')}</p>` : ''}
                </div>
            </div>
        </div>`;
        if (this._calc.editing) { this.calcPreview(); }
        this._calcRefreshValues();
    },

    _calcListHtml() {
        const t = this.t.bind(this);
        if (!this._calc.items.length) {
            // Empty state that TEACHES: the first three presets as clickable
            // cards (same flow as the dropdown's Use), not a bare line of text.
            const cards = (this._calc.presets || []).slice(0, 3).map(pr => `
                <button type="button" class="calc-preset-card" ${this._act('calcApplyPreset', [pr.id])}>
                    <span class="calc-preset-name">${this._esc(pr.label || pr.name)}</span>
                    <code class="calc-preset-expr">${this._esc(pr.template || pr.expr || '')}</code>
                    <span class="calc-preset-hint">${this._esc(pr.hint || '')}</span>
                </button>`).join('');
            return `<div class="calc-empty">
                <div class="calc-empty-title">${t('calc.emptyTitle', 'No calculated measurements yet')}</div>
                <div class="calc-empty-sub">${t('calc.emptySub', 'Derive new values with a formula — start from a preset:')}</div>
                <div class="calc-preset-cards">${cards}</div>
                <div class="calc-empty-sub" style="margin-top:10px;">${t('calc.emptyOr', '…or use + Add above for a blank formula.')}</div>
            </div>`;
        }
        return `<div class="calc-list">` + this._calc.items.map((c, i) => `
            <div class="calc-row" data-i="${i}">
                <div class="calc-row-main">
                    <span class="reg-name-mono">${this._esc(c.name)}</span>
                    <span class="calc-row-val" id="calcVal${i}">—</span>
                    <span class="calc-unit">${this._esc(c.unit || '')}</span>
                </div>
                <code class="calc-expr">${this._esc(c.expr)}</code>
                <div class="calc-row-actions">
                    <button class="btn-action edit" title="${t('common.edit', 'Edit')}" ${this._act('calcEditIdx', [i])}>&#9998;</button>
                    <button class="btn-action remove" title="${t('common.delete', 'Delete')}" ${this._act('calcDelete', [i])}>&#10005;</button>
                </div>
            </div>`).join('') + `</div>`;
    },

    async _calcRefreshValues() {
        // Show each calc register's REAL running value from the live store (by name)
        // — works for stateful (prev/dt) formulas too, which a one-shot test can't.
        if (!this._calc.items.length) return;
        try {
            const d = await fetch(`/api/values?device=${encodeURIComponent(this._calc.deviceId)}`).then(r => r.json());
            const byName = {};
            for (const v of Object.values(d.values || {})) if (v && v.name) byName[v.name] = v.value;
            this._calc.items.forEach((c, i) => {
                const cell = document.getElementById('calcVal' + i);
                if (!cell) return;
                const val = byName[c.name];
                const has = typeof val === 'number';
                cell.textContent = has ? '= ' + this._calcFmt(val) : '—';
                cell.className = 'calc-row-val' + (has ? ' ok' : '');
            });
        } catch (e) { /* leave dashes */ }
    },

    _calcEditorHtml() {
        const t = this.t.bind(this);
        const d = this._calc.editing.draft;
        const groups = this._calcPollGroups().map(g =>
            `<option value="${g}" ${g === d.poll_group ? 'selected' : ''}>${g}</option>`).join('');
        const flt = this._calc.fieldFilter.toLowerCase();
        const chips = this._calc.fields
            .filter(f => !flt || f.name.toLowerCase().includes(flt) || (f.label || '').toLowerCase().includes(flt))
            .slice(0, 200)
            .map(f => `<button class="calc-chip" title="${this._esc(f.label)}${f.unit ? ' · ' + this._esc(f.unit) : ''}" ${this._act('calcInsert', [f.name])}>${this._esc(f.name)}</button>`).join('');
        const fnChips = this._calc.functions.map(f =>
            `<button class="calc-chip fn" title="${this._esc(f.desc)}" ${this._act('calcInsert', [f.name + '()'])}>${this._esc(f.sig)}</button>`).join('');
        const opChips = this._calc.operators.map(o =>
            `<button class="calc-chip op" ${this._act('calcInsert', [' ' + o.replace(' ', '') + ' '])}>${this._esc(o)}</button>`).join('');
        const bind = this._calcBindHtml();
        return `
        <div class="calc-editor">
            <div class="calc-editor-head">${this._calc.editing.index == null ? t('calc.newTitle', 'New calculated measurement') : t('calc.editTitle', 'Edit calculated measurement')}</div>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="calcName">${t('calc.name', 'Name')}</label>
                    <input id="calcName" class="input" value="${this._esc(d.name)}" placeholder="PF_TOTAL" oninput="app._calc.editing.draft.name=this.value"></div>
                <div class="form-group"><label class="form-label" for="calcLabel">${t('calc.label', 'Label')}</label>
                    <input id="calcLabel" class="input" value="${this._esc(d.label)}" oninput="app._calc.editing.draft.label=this.value"></div>
                <div class="form-group" style="max-width:90px"><label class="form-label" for="calcUnit">${t('calc.unit', 'Unit')}</label>
                    <input id="calcUnit" class="input" value="${this._esc(d.unit)}" oninput="app._calc.editing.draft.unit=this.value"></div>
                <div class="form-group" style="max-width:110px"><label class="form-label" for="calcPoll">${t('calc.pollGroup', 'Poll group')}</label>
                    <select id="calcPoll" class="input">${groups}</select></div>
                <div class="form-group" style="max-width:90px"><label class="form-label" for="calcDec">${t('calc.decimals', 'Decimals')}</label>
                    <input id="calcDec" class="input" type="number" min="0" max="6" value="${d.decimals ?? ''}"></div>
            </div>
            ${bind}
            <div class="form-group">
                <label class="form-label" for="calcExpr">${t('calc.expr', 'Expression')}</label>
                <textarea id="calcExpr" class="input calc-expr-input" rows="2" spellcheck="false"
                    oninput="app._calc.editing.draft.expr=this.value; app.calcPreview()">${this._esc(d.expr)}</textarea>
                <div id="calcPreview" class="calc-preview"></div>
            </div>
            <div class="calc-palette">
                <div class="calc-palette-label">${t('calc.fields', 'Measurement fields')} <input class="input input-sm calc-fieldfilter" placeholder="${t('common.search', 'Search')}" value="${this._esc(this._calc.fieldFilter)}" oninput="app._calc.fieldFilter=this.value; app._calcRerenderChips()"></div>
                <div class="calc-chips" id="calcFieldChips">${chips || `<span class="calc-empty">${t('calc.noFields', 'No measurements selected on this device yet.')}</span>`}</div>
            </div>
            <div class="calc-palette">
                <div class="calc-palette-label">${t('calc.functions', 'Functions & operators')}</div>
                <div class="calc-chips">${fnChips} ${opChips}</div>
            </div>
            <div class="calc-editor-actions">
                <span class="save-feedback" id="calcFeedback"></span>
                <button class="btn btn-ghost btn-sm" onclick="app.calcSaveAsTemplate()" title="${t('calc.saveAsPresetHint', 'Save this formula as a reusable preset')}"><i class="bi bi-bookmark-plus"></i> ${t('calc.saveAsPreset', 'Save as preset')}</button>
                <button class="btn btn-ghost btn-sm" onclick="app.calcCancel()">${t('common.cancel', 'Cancel')}</button>
                <button class="btn btn-primary btn-sm" onclick="app.calcSave()"><i class="bi bi-check-lg"></i> ${t('common.save', 'Save')}</button>
            </div>
        </div>`;
    },

    _calcBindHtml() {
        // When a preset is being applied, show a <select> per input to bind it to
        // one of the device's measurement fields (substitutes {key} in the expr).
        const ed = this._calc.editing;
        if (!ed || !ed.preset) return '';
        const fieldOpts = ['<option value="">—</option>'].concat(this._calc.fields.map(f =>
            `<option value="${this._esc(f.name)}">${this._esc(f.name)}${f.unit ? ' (' + this._esc(f.unit) + ')' : ''}</option>`)).join('');
        const rows = ed.preset.inputs.map(inp => `
            <div class="form-group">
                <label class="form-label">${this._esc(inp.label)} <code>{${this._esc(inp.key)}}</code></label>
                <select class="input" data-bindkey="${this._esc(inp.key)}" onchange="app.calcBindPreset()">${fieldOpts}</select>
            </div>`).join('');
        return `<div class="calc-bind"><div class="calc-palette-label">${this.t('calc.bindInputs', 'Bind preset inputs to your fields')}</div>
            <div class="form-row">${rows}</div></div>`;
    },

    _calcRerenderChips() {
        const box = document.getElementById('calcFieldChips');
        if (!box) return;
        const flt = this._calc.fieldFilter.toLowerCase();
        box.innerHTML = this._calc.fields
            .filter(f => !flt || f.name.toLowerCase().includes(flt) || (f.label || '').toLowerCase().includes(flt))
            .slice(0, 200)
            .map(f => `<button class="calc-chip" title="${this._esc(f.label)}${f.unit ? ' · ' + this._esc(f.unit) : ''}" ${this._act('calcInsert', [f.name])}>${this._esc(f.name)}</button>`).join('')
            || `<span class="calc-empty">${this.t('calc.noMatch', 'No match')}</span>`;
    },

    calcAdd() {
        this._calc.editing = { index: null, preset: null,
            draft: { name: '', label: '', unit: '', expr: '', poll_group: 'normal', decimals: null } };
        this._calcRender();
    },

    calcEditIdx(i) {
        const c = this._calc.items[i];
        this._calc.editing = { index: i, preset: null, draft: { ...c } };
        this._calcRender();
    },

    calcApplyPreset(id) {
        if (!id) return;
        if (id.startsWith('user:')) {           // a user-saved template: concrete expr
            const u = this._calc.userTemplates.find(x => x.id === id.slice(5));
            if (!u) return;
            this._calc.editing = { index: null, preset: null,
                draft: { name: u.name, label: u.label || u.name, unit: u.unit || '',
                         expr: u.expr, poll_group: 'normal', decimals: u.decimals ?? null } };
            this._calcRender();
            return;
        }
        const p = this._calc.presets.find(x => x.id === id);   // built-in parameterized preset
        if (!p) return;
        this._calc.editing = { index: null, preset: p,
            draft: { name: p.name, label: p.label, unit: p.unit || '',
                     expr: p.template, poll_group: 'normal', decimals: p.decimals ?? null } };
        this._calcRender();
    },

    async calcSaveAsTemplate() {
        const ed = this._calc.editing;
        if (!ed) return;
        const d = ed.draft;
        d.name = (document.getElementById('calcName')?.value || '').trim();
        d.label = (document.getElementById('calcLabel')?.value || '').trim();
        d.unit = (document.getElementById('calcUnit')?.value || '').trim();
        d.expr = (document.getElementById('calcExpr')?.value || '').trim();
        const dec = document.getElementById('calcDec')?.value;
        if (!d.name || !d.expr) {
            this.showToast('error', this.t('calc.title', 'Calculated measurements'),
                this.t('calc.needNameExpr', 'Name and expression are required to save a template'));
            return;
        }
        try {
            const r = await fetch('/api/calculated/templates', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: d.name, label: d.label, unit: d.unit,
                    expr: d.expr, decimals: (dec === '' || dec == null) ? null : parseInt(dec) }),
            });
            if (!r.ok) throw new Error((await r.json()).detail?.errors?.join(' · ') || 'failed');
            this._calc.userTemplates = (await r.json()).templates;
            this.showToast('success', this.t('calc.myTemplates', 'My templates'),
                this.t('calc.templateSaved', 'Saved as reusable preset'));
            this._calcRender();
        } catch (e) {
            this.showToast('error', this.t('common.error', 'Error'), e.message);
        }
    },

    async calcDeleteSelectedPreset(value) {
        if (!value || !value.startsWith('user:')) {
            this.showToast('info', this.t('calc.myTemplates', 'My templates'),
                this.t('calc.onlyUserDeletable', 'Only your own saved presets can be deleted'));
            return;
        }
        try {
            const r = await fetch(`/api/calculated/templates/${encodeURIComponent(value.slice(5))}`, { method: 'DELETE' });
            if (!r.ok) throw new Error('failed');
            this._calc.userTemplates = (await r.json()).templates;
            this._calcRender();
        } catch (e) { this.showToast('error', this.t('common.error', 'Error'), e.message); }
    },

    calcBindPreset() {
        const ed = this._calc.editing;
        if (!ed || !ed.preset) return;
        let expr = ed.preset.template;
        document.querySelectorAll('.calc-bind select[data-bindkey]').forEach(sel => {
            if (sel.value) expr = expr.split('{' + sel.dataset.bindkey + '}').join(sel.value);
        });
        ed.draft.expr = expr;
        const ta = document.getElementById('calcExpr');
        if (ta) ta.value = expr;
        this.calcPreview();
    },

    calcInsert(txt) {
        const ta = document.getElementById('calcExpr');
        if (!ta) return;
        const s = ta.selectionStart ?? ta.value.length, e = ta.selectionEnd ?? ta.value.length;
        ta.value = ta.value.slice(0, s) + txt + ta.value.slice(e);
        const pos = s + txt.length;
        ta.selectionStart = ta.selectionEnd = pos;
        ta.focus();
        this._calc.editing.draft.expr = ta.value;
        this.calcPreview();
    },

    calcPreview() {
        clearTimeout(this._calc._pvT);
        this._calc._pvT = setTimeout(async () => {
            const expr = (document.getElementById('calcExpr')?.value || '').trim();
            const box = document.getElementById('calcPreview');
            if (!box) return;
            if (!expr) { box.textContent = ''; box.className = 'calc-preview'; return; }
            try {
                const r = await fetch(`/api/devices/${encodeURIComponent(this._calc.deviceId)}/calculated/test`,
                    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expr }) });
                const d = await r.json();
                if (d.ok && d.stateful) {
                    box.className = 'calc-preview';
                    box.textContent = '↻ ' + this.t('calc.statefulPreview', 'stateful — value appears once running (needs two polls)');
                }
                else if (d.ok) { box.className = 'calc-preview ok'; box.textContent = '= ' + this._calcFmt(d.value); }
                else {
                    box.className = 'calc-preview err';
                    box.textContent = d.error + (d.missing?.length ? ` (${this.t('calc.missing', 'missing')}: ${d.missing.join(', ')})` : '');
                }
            } catch (e) { box.className = 'calc-preview err'; box.textContent = e.message; }
        }, 400);
    },

    calcCancel() { this._calc.editing = null; this._calcRender(); },

    async calcSave() {
        const d = this._calc.editing.draft;
        d.expr = (document.getElementById('calcExpr')?.value || '').trim();
        d.name = (document.getElementById('calcName')?.value || '').trim();
        d.label = (document.getElementById('calcLabel')?.value || '').trim();
        d.unit = (document.getElementById('calcUnit')?.value || '').trim();
        d.poll_group = document.getElementById('calcPoll')?.value || 'normal';
        const dec = document.getElementById('calcDec')?.value;
        d.decimals = (dec === '' || dec == null) ? null : parseInt(dec);
        const items = this._calc.items.slice();
        if (this._calc.editing.index == null) items.push(d); else items[this._calc.editing.index] = d;
        const fb = document.getElementById('calcFeedback');
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(this._calc.deviceId)}/calculated`,
                { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ calculated: items }) });
            if (!r.ok) {
                const errs = (await r.json()).detail?.errors || ['save failed'];
                if (fb) { fb.textContent = errs.join(' · '); fb.className = 'save-feedback err'; }
                return;
            }
            this._calc.items = (await r.json()).calculated;
            this._calc.editing = null;
            this._calcRender();
            this.showToast('success', this.t('calc.title', 'Calculated measurements'), this.t('calc.saved', 'Saved'));
        } catch (e) {
            if (fb) { fb.textContent = e.message; fb.className = 'save-feedback err'; }
        }
    },

    async calcDelete(i) {
        const items = this._calc.items.slice();
        items.splice(i, 1);
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(this._calc.deviceId)}/calculated`,
                { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ calculated: items }) });
            if (!r.ok) throw new Error('delete failed');
            this._calc.items = (await r.json()).calculated;
            this._calcRender();
        } catch (e) { this.showToast('error', this.t('common.error', 'Error'), e.message); }
    },
});
