/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// meters — augments JanitzaMonitor.prototype
//
// The meters that measure a device, each in a role (docs/API.md "Meters"):
// one meter on its supply (consumption), or one on what goes in and one on
// what comes out (input / output → loss, efficiency, load). The same rows
// serve the device page's Meters tab (prefix `mtr`) and the Add Device
// path for a device that IS its meters (prefix `wizMtr`).
//
//   _metersRowsHtml(px, links, opts)   the rows, as markup
//   _metersRowsMount(px, opts)         their pickers and field lists
//   _metersRowsRead(px)                the links, as the server takes them
//   _renderMetersTab()                 the whole tab: rows, thresholds,
//                                      what the pair gives, what it measures
//   _renderPairLine()                  the Overview's one line about a pair

Object.assign(JanitzaMonitor.prototype, {

    async _meterRoles() {
        if (this._meterRolesData) return this._meterRolesData;
        try { this._meterRolesData = await (await fetch('/api/meter-roles')).json(); }
        catch (e) { this._meterRolesData = { roles: [], options: {}, meter_only_kinds: { first: [], more: [] } }; }
        return this._meterRolesData;
    },

    _meterRoleLabel(r) {
        return this.t(`meters.role.${r.id || r}`, r.label || r.id || r);
    },

    // why a value is not published, in words (never only a colour)
    _meterReasonWords(reason, opts = {}) {
        const t = (k, d, p) => this.t(k, d, p);
        const min = opts.min_load_w != null ? opts.min_load_w : 100;
        return ({
            no_reading: t('meters.why.no_reading', 'a meter has not reported this value'),
            stale: t('meters.why.stale', 'a meter’s reading is too old'),
            skew: t('meters.why.skew', 'the two readings were taken too far apart'),
            unsteady: t('meters.why.unsteady', 'the load was changing between the two readings'),
            no_time: t('meters.why.no_time', 'a meter’s reading carries no time'),
            below_min_load: t('meters.why.below_min_load', 'below {w} W: not meaningful', { w: min }),
            out_exceeds_in: t('meters.why.out_exceeds_in', 'output reads above input beyond the meters’ tolerance — check the two meters'),
            negative: t('meters.why.negative', 'the result would be negative'),
            no_rating: t('meters.why.no_rating', 'no rated power set for this device'),
            not_evaluated: t('meters.why.not_evaluated', 'waiting for the first reading'),
        })[reason] || reason || '';
    },

    // what a meter reads that a link may use: power (W/kW/MW) and energy
    // (Wh/kWh/MWh) — the server refuses any other unit
    async _meterFields(meterId) {
        if (!meterId) return [];
        const c = this._meterFieldCache || (this._meterFieldCache = new Map());
        if (c.has(meterId)) return c.get(meterId);
        let rows = [];
        try { rows = (await (await fetch(`/api/registers/selected?device=${encodeURIComponent(meterId)}`)).json()).registers || []; }
        catch (e) { rows = []; }
        rows = rows.filter(r => !r.calculated);
        c.set(meterId, rows);
        return rows;
    },

    // ── the rows ────────────────────────────────────────────────────────────
    _metersRowsHtml(px, links, opts = {}) {
        const t = (k, d, p) => this.t(k, d, p);
        const rows = links.length ? links : [{ role: opts.firstRole || '', device: '', power: '', energy: '', sign: 'auto' }];
        const ro = opts.readOnly ? 'disabled data-ro' : '';
        return `
        <div class="mtr-table" role="table" aria-label="${this._esc(t('meters.tableLabel', 'Meters that measure this device'))}" id="${px}Rows">
            <div class="mtr-tr mtr-th" role="row">
                <span role="columnheader">${t('meters.col.role', 'Role')}</span>
                <span role="columnheader">${t('meters.col.meter', 'Meter')}</span>
                <span role="columnheader">${t('meters.col.power', 'Its power')}</span>
                <span role="columnheader">${t('meters.col.energy', 'Its energy counter')}</span>
                <span role="columnheader"><span class="sr-only">${t('meters.col.remove', 'Remove')}</span></span>
            </div>
            ${rows.map((ln, i) => `
            <div class="mtr-tr" role="row" data-mtr-row="${i}" data-sign="${this._esc(ln.sign || 'auto')}">
                <div role="cell" class="form-group"><label class="mtr-lbl" for="${px}Role-${i}">${t('meters.col.role', 'Role')}</label>
                    <select id="${px}Role-${i}" class="input" ${ro} aria-label="${this._esc(t('meters.roleOfRow', 'Role, row {n}', { n: i + 1 }))}"
                        data-cur="${this._esc(ln.role || '')}"></select></div>
                <div role="cell" class="form-group"><label class="mtr-lbl" for="${px}Dev-${i}">${t('meters.col.meter', 'Meter')}</label>
                    <select id="${px}Dev-${i}" class="input" ${ro} aria-label="${this._esc(t('meters.meterOfRow', 'Meter, row {n}', { n: i + 1 }))}"
                        data-cur="${this._esc(ln.device || '')}"></select></div>
                <div role="cell" class="form-group"><label class="mtr-lbl" for="${px}Power-${i}">${t('meters.col.power', 'Its power')}</label>
                    <select id="${px}Power-${i}" class="input" ${ro} aria-label="${this._esc(t('meters.powerOfRow', 'Its power, row {n}', { n: i + 1 }))}"
                        data-cur="${this._esc(ln.power || '')}"></select></div>
                <div role="cell" class="form-group"><label class="mtr-lbl" for="${px}Energy-${i}">${t('meters.col.energy', 'Its energy counter')}</label>
                    <select id="${px}Energy-${i}" class="input" ${ro} aria-label="${this._esc(t('meters.energyOfRow', 'Its energy counter, row {n}', { n: i + 1 }))}"
                        data-cur="${this._esc(ln.energy || '')}"></select></div>
                <div role="cell" class="mtr-rm">${opts.readOnly ? '' : `<button type="button" class="btn btn-ghost btn-sm" data-admin
                    ${this._act('_metersRowRemove', [px, i])} aria-label="${this._esc(t('meters.removeRow', 'Remove meter, row {n}', { n: i + 1 }))}"><i aria-hidden="true" class="bi bi-x-lg"></i></button>`}</div>
            </div>`).join('')}
        </div>
        ${opts.readOnly ? '' : `<button type="button" class="btn btn-secondary btn-sm" data-admin id="${px}Add" ${this._act('_metersRowAdd', [px])}>
            <i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('meters.add', 'Add meter')}</button>`}`;
    },

    // the role lists (a role taken by another row is not offered; consumption
    // does not go with input / output), the meter pickers, the field lists
    async _metersRowsMount(px, opts = {}) {
        const cat = await this._meterRoles();
        const roles = (cat.roles || []).filter(r => r.available);
        const devices = (await this._fetchDevices().catch(() => [])) || [];
        const self = opts.selfId || '';
        // a meter is a device of its own (never an installation unit, never
        // the device itself, never one measured only by meters)
        const meters = devices.filter(x => x.id !== self && !x.endpoint_id && x.protocol !== 'none');
        const rows = [...document.querySelectorAll(`#${px}Rows [data-mtr-row]`)];
        for (const row of rows) {
            const i = row.dataset.mtrRow;
            const dev = document.getElementById(`${px}Dev-${i}`);
            const cur = dev.dataset.cur;
            dev.innerHTML = `<option value="">${this._esc(this.t('meters.pickMeter', 'Choose a meter…'))}</option>` +
                meters.map(m => `<option value="${this._esc(m.id)}" ${m.id === cur ? 'selected' : ''}>${this._esc(m.name || m.id)}</option>`).join('') +
                (cur && !meters.some(m => m.id === cur) ? `<option value="${this._esc(cur)}" selected>${this._esc(cur)}</option>` : '');
            if (!dev._wired) {
                dev._wired = true;
                dev.addEventListener('change', () => {
                    this._metersAutoRole(px, i, roles);
                    this._metersFieldsFill(px, i, dev.value);
                    this._metersChanged(px, (this._metersOptsBy || {})[px] || opts);
                });
            }
            if (this._pickDevice && !dev.disabled) this._pickDevice(dev, { minWidth: 260 });
            await this._metersFieldsFill(px, i, cur, true);
            for (const k of ['Power', 'Energy']) {
                const el = document.getElementById(`${px}${k}-${i}`);
                if (el && !el._wired) { el._wired = true; el.addEventListener('change', () => this._metersChanged(px, opts)); }
            }
        }
        this._metersRolesFill(px, roles);
        rows.forEach(row => {
            const el = document.getElementById(`${px}Role-${row.dataset.mtrRow}`);
            if (el && !el._wired) {
                el._wired = true;
                el.addEventListener('change', () => { el.dataset.cur = el.value; this._metersRolesFill(px, roles); this._metersChanged(px, opts); });
            }
        });
        this._metersHoldLast(px, opts);
        (this._metersOptsBy || (this._metersOptsBy = {}))[px] = opts;
        const box = document.getElementById(`${px}Rows`);
        if (box) box.dataset.ready = '1';            // every list filled
    },

    _metersAutoRole(px, i, roles) {
        const el = document.getElementById(`${px}Role-${i}`);
        if (!el || el.value) return;
        const opt = [...el.options].find(o => o.value && !o.disabled);
        if (opt) { el.value = opt.value; el.dataset.cur = opt.value; this._metersRolesFill(px, roles); }
    },

    _metersRolesFill(px, roles) {
        const els = [...document.querySelectorAll(`#${px}Rows select[id^="${px}Role-"]`)];
        const taken = els.map(e => e.dataset.cur || e.value).filter(Boolean);
        const pair = taken.some(r => r === 'input' || r === 'output');
        const cons = taken.includes('consumption');
        els.forEach((el, n) => {
            const mine = el.dataset.cur || el.value || '';
            const free = r => r.id === mine || (!taken.includes(r.id) && !(r.id === 'consumption' && pair)
                && !((r.id === 'input' || r.id === 'output') && cons));
            el.innerHTML = (mine ? '' : `<option value="">${this._esc(this.t('meters.pickRole', 'Choose a role…'))}</option>`) +
                roles.map(r => `<option value="${this._esc(r.id)}" ${r.id === mine ? 'selected' : ''} ${free(r) ? '' : 'disabled'}
                    title="${this._esc(this.t(`meters.roleHint.${r.id}`, r.hint || ''))}">${this._esc(this._meterRoleLabel(r))}</option>`).join('');
            // a row's role is chosen with its meter: a meter picked into a row
            // with none gets the first free one (_metersAutoRole)
            void n;
        });
    },

    async _metersFieldsFill(px, i, meterId, keep = false) {
        const rows = await this._meterFields(meterId);
        const fill = (k, units, guess) => {
            const el = document.getElementById(`${px}${k}-${i}`);
            if (!el) return;
            const want = keep ? el.dataset.cur : (el.value || '');
            const ok = rows.filter(r => units.includes(String(r.unit || '').toLowerCase()));
            // a stored link shows exactly what it has (a counter it does not
            // use stays "not used"); a meter just picked gets the usual one
            const pick = keep ? want : want && ok.some(r => r.name === want) ? want : (ok.find(r => r.name === guess) || {}).name || '';
            el.innerHTML = `<option value="">${this._esc(this.t('meter.notUsed', '— not used —'))}</option>` +
                ok.map(r => `<option value="${this._esc(r.name)}" ${r.name === pick ? 'selected' : ''}>${this._esc(r.label || r.name)} · ${this._esc(r.unit)}</option>`).join('') +
                (keep && want && !ok.some(r => r.name === want) ? `<option value="${this._esc(want)}" selected>${this._esc(want)}</option>` : '');
            el.disabled = !meterId || el.hasAttribute('data-ro');
        };
        fill('Power', ['w', 'kw', 'mw'], 'power_active_total');
        fill('Energy', ['wh', 'kwh', 'mwh'], 'energy_active_import');
    },

    // a device measured only by its meters keeps at least one: its last
    // row's ✕ is held, with the reason (the server refuses it too)
    _metersHoldLast(px, opts = {}) {
        const btns = [...document.querySelectorAll(`#${px}Rows .mtr-rm button`)];
        btns.forEach(b => {
            const hold = !!opts.meterOnly && btns.length === 1;
            if (hold) {
                b.setAttribute('aria-disabled', 'true');
                b.title = this.t('meters.keepOne', 'A device measured only by its meters keeps at least one — delete the device instead.');
            } else { b.removeAttribute('aria-disabled'); b.removeAttribute('title'); }
        });
    },

    // every row with anything chosen; a row left wholly empty is no link (one
    // row is always shown) — a half-filled one is kept, so Save says what is
    // missing instead of dropping it
    _metersRowsRead(px) {
        const kept = [];
        const out = [...document.querySelectorAll(`#${px}Rows [data-mtr-row]`)].map(row => {
            const i = row.dataset.mtrRow;
            const v = k => document.getElementById(`${px}${k}-${i}`)?.value || '';
            const ln = { role: v('Role'), device: v('Dev') };
            if (v('Power')) ln.power = v('Power');
            if (v('Energy')) ln.energy = v('Energy');
            ln.sign = row.dataset.sign || 'auto';
            return { ln, i };
        }).filter(x => x.ln.device || x.ln.role);
        // a link's index as sent → its row on screen (an empty row skipped)
        (this._mtrRowMap || (this._mtrRowMap = {}))[px] = out.map(x => x.i);
        return out.map(x => x.ln);
    },

    // a server path `meters[i].field` → the input of that row on screen
    _mtrFieldId(px, p) {
        const m = /^meters\[(\d+)\]\.(\w+)$/.exec(p);
        if (!m) return null;
        const row = ((this._mtrRowMap || {})[px] || [])[+m[1]] ?? m[1];
        return `${px}${({ role: 'Role', device: 'Dev', power: 'Power', energy: 'Energy' })[m[2]] || 'Role'}-${row}`;
    },

    async _metersRowAdd(px) {
        const links = this._metersRowsRead(px);
        links.push({ role: '', device: '', sign: 'auto' });
        await this._metersRedraw(px, links);
        document.getElementById(`${px}Role-${links.length - 1}`)?.focus();
    },

    async _metersRowRemove(px, i) {
        const btn = document.querySelector(`#${px}Rows [data-mtr-row="${i}"] .mtr-rm button`);
        if (btn?.getAttribute('aria-disabled') === 'true') {
            this.showToast('warning', this.t('meters.tab', 'Meters'), btn.title);
            return;
        }
        const all = [...document.querySelectorAll(`#${px}Rows [data-mtr-row]`)].map(r => r.dataset.mtrRow);
        const links = all.filter(x => String(x) !== String(i)).map(x => {
            const v = k => document.getElementById(`${px}${k}-${x}`)?.value || '';
            const row = document.querySelector(`#${px}Rows [data-mtr-row="${x}"]`);
            return { role: v('Role'), device: v('Dev'), power: v('Power'), energy: v('Energy'), sign: row?.dataset.sign || 'auto' };
        });
        await this._metersRedraw(px, links);
        (document.getElementById(`${px}Add`) || document.getElementById(`${px}Role-0`))?.focus();
        this._metersChanged(px, (this._metersOptsBy || {})[px] || {});
    },

    async _metersRedraw(px, links) {
        const host = document.getElementById(`${px}Rows`)?.parentElement;
        if (!host) return;
        const opts = (this._metersOptsBy || {})[px] || {};
        const wrap = host.querySelector(`[data-mtr-wrap="${px}"]`) || host;
        wrap.innerHTML = this._metersRowsHtml(px, links, opts);
        await this._metersRowsMount(px, opts);
        this._metersChanged(px, opts);
    },

    _metersChanged(px, opts = {}) {
        if (px !== 'mtr') return;
        const roles = this._metersRowsRead(px).map(l => l.role);
        const box = document.getElementById('mtrOptsBox');
        if (box) box.hidden = !(roles.includes('input') && roles.includes('output'));
        const fb = document.getElementById('mtrFeedback');
        if (fb && !fb.dataset.keep) { fb.textContent = ''; fb.className = 'save-feedback'; }
    },

    // ── the Meters tab ──────────────────────────────────────────────────────
    async _renderMetersTab() {
        const s = this._devDetail;
        const host = document.querySelector('#deviceDetailView [data-dpanel="meters"] .dev-meters-host');
        if (!s || !host) return;
        const t = (k, d, p) => this.t(k, d, p);
        const id = s.id;
        const devices = (await this._fetchDevices(true).catch(() => [])) || [];
        if (this._devDetail?.id !== id) return;
        const me = devices.find(x => x.id === id) || s.entry || {};
        const cat = await this._meterRoles();
        const links = me.meters || [];
        const meterOnly = me.protocol === 'none';
        const admin = !this._isAdminView || this._isAdminView();
        const own = me.meter_options_own || {};
        const eff = { ...(Object.fromEntries(Object.entries(cat.options || {}).map(([k, v]) => [k, v.default]))), ...(me.meter_options || {}) };
        const thr = (me.meters_status || {}).thresholds || {};
        this._mtrState = { touched: false, reset: false };
        const measures = devices.filter(x => (x.meters || []).some(l => l.device === id))
            .map(x => ({ dev: x, links: (x.meters || []).filter(l => l.device === id) }));
        // the meter's own field names, said by their labels
        const myFields = measures.length ? await this._meterFields(id) : [];
        const fieldLabel = n => ((myFields.find(r => r.name === n) || {}).label) || n;
        const measuresHtml = `
        <div class="settings-card" id="mtrMeasures">
            <div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-diagram-2"></i> ${t('meters.measures', 'Measures')}</h3></div>
            <div class="settings-card-body">
                ${measures.length ? `<p class="field-hint" style="margin:0 0 8px;">${this._esc(this.tn('meters.measuresLead', measures.length, 'This meter measures {n} device(s).'))}</p>
                <ul class="mtr-measures">${measures.map(({ dev, links: ls }) => `<li>
                    <a href="#/devices/${encodeURIComponent(dev.id)}/meters" ${this._act('usedByOpen', ['device', dev.id, 'meters'])}>${this._kindIconHtml ? this._kindIconHtml(dev.kind) : ''}${this._esc(dev.name || dev.id)}</a>
                    <span class="dev-chip">${this._esc(dev.id)}</span>
                    ${ls.map(l => `<span class="mtr-role-pill">${this._esc(this._meterRoleLabel({ id: l.role }))}</span> <span title="${this._esc(l.power || l.energy || '')}">${this._esc(fieldLabel(l.power || l.energy || ''))}</span>`).join(' ')}
                </li>`).join('')}</ul>` : `<p class="field-hint" style="margin:0;">${t('meters.measuresNone', 'This meter measures no other device.')}</p>`}
            </div>
        </div>`;
        if (s.primary) {
            // the primary meter has no meters of its own: what it measures only
            host.innerHTML = measuresHtml;
            return;
        }
        const num = (k, label, unit, ph) => `
            <div class="form-group"><label class="form-label" for="mtrOpt-${k}">${label}</label>
                <div class="input-unit"><input type="text" inputmode="decimal" id="mtrOpt-${k}" class="input mtr-opt" data-opt="${k}"
                    value="${own[k] != null ? this._esc(String(own[k])) : ''}" placeholder="${this._esc(ph)}" ${admin ? '' : 'disabled'}>
                <span class="input-unit-suffix">${unit}</span></div></div>`;
        const skewPh = eff.max_skew_s != null ? String(eff.max_skew_s)
            : thr.max_skew_s != null ? t('meters.opt.autoIs', 'auto ({v})', { v: thr.max_skew_s })
            : t('meters.opt.auto', 'auto, from the poll intervals');
        host.innerHTML = `
        <div class="settings-card">
            <div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-lightning"></i> ${t('meters.title', 'Meters that measure it')}</h3></div>
            <div class="settings-card-body">
                <p class="field-hint" style="margin:0 0 10px;">${meterOnly
                    ? t('meters.leadOnly', 'This device has no connection of its own: these meters are how it is measured.')
                    : t('meters.lead', 'For a device with no meter of its own, name the meter on its supply (consumption). For one with a meter on what goes in and one on what comes out (a UPS, a charger), name both: its loss, efficiency and load follow.')}</p>
                <div data-mtr-wrap="mtr">${this._metersRowsHtml('mtr', links, { readOnly: !admin, meterOnly })}</div>
                <div id="mtrOptsBox" hidden>
                    <details class="mtr-opts" id="mtrOpts" ${Object.keys(own).length ? 'open' : ''}>
                        <summary>${t('meters.opt.title', 'When loss and efficiency are published')}</summary>
                        <p class="field-hint">${t('meters.opt.hint', 'Empty uses the default. A value is published only when it is honest; otherwise it is left out and the reason is shown below.')}</p>
                        <div class="form-row">
                            ${num('min_load_w', t('meters.opt.min', 'Minimum load'), 'W', String(eff.min_load_w ?? 100))}
                            ${num('tolerance_pct', t('meters.opt.tol', 'Meters’ tolerance'), '%', String(eff.tolerance_pct ?? 2))}
                            ${num('max_skew_s', t('meters.opt.skew', 'Readings at most apart'), 's', skewPh)}
                        </div>
                        ${admin ? `<button type="button" class="btn btn-ghost btn-sm" data-admin id="mtrOptReset" ${this._act('_metersOptsReset')}>
                            <i aria-hidden="true" class="bi bi-arrow-counterclockwise"></i> ${t('meters.opt.reset', 'Reset to defaults')}</button>` : ''}
                    </details>
                </div>
            </div>
            ${admin ? `<div class="settings-card-footer">
                <span class="save-feedback" id="mtrFeedback" role="status"></span>
                <button type="button" class="btn btn-primary btn-sm" data-admin id="mtrSave" ${this._act('saveDeviceMeters', [], { el: true })}><i aria-hidden="true" class="bi bi-check-lg"></i> ${t('common.save', 'Save')}</button>
            </div>` : ''}
        </div>
        <div class="settings-card" id="mtrGives">${this._metersGivesHtml(me, {})}</div>
        ${measures.length ? measuresHtml : ''}`;
        await this._metersRowsMount('mtr', { selfId: id, meterOnly, readOnly: !admin });
        this._metersChanged('mtr');
        host.querySelectorAll('.mtr-opt').forEach(inp => inp.addEventListener('input', () => { this._mtrState.touched = true; this._mtrState.reset = false; }));
        this._metersGivesLive(me);
    },

    // what an input / output pair gives — the verdict of its last evaluation
    // (meters_status), the two powers from the device's live values
    _metersGivesHtml(me, live) {
        const t = (k, d, p) => this.t(k, d, p);
        const st = me.meters_status || {};
        const roles = (me.meters || []).map(l => l.role);
        const head = `<div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-activity"></i> ${t('meters.gives', 'What it gives')}</h3>
            ${st.age_s != null ? `<span class="field-hint">${this._esc(t('meters.evaluatedAgo', 'evaluated {s} s ago', { s: st.age_s }))}</span>` : ''}</div>`;
        if (me.enabled === false) {
            return `${head}<div class="settings-card-body"><p class="field-hint">${t('meters.disabled', 'The device is disabled: nothing is computed for it.')}</p></div>`;
        }
        if (!(roles.includes('input') && roles.includes('output'))) {
            return `${head}<div class="settings-card-body"><p class="field-hint">${roles.includes('consumption')
                ? t('meters.givesConsumption', 'Its electric power and energy, from the meter on its supply (Overview, Energy).')
                : t('meters.noPair', 'Name an input and an output meter to see its loss, efficiency and load.')}</p></div>`;
        }
        const fmt = (v, unit) => v == null ? '—' : `${Number(v).toLocaleString(this._loc(), { maximumFractionDigits: unit === '%' ? 1 : 0 })} ${unit}`;
        const opts = me.meter_options || {};
        // a meter that does not run: said by name, never "waiting" forever
        const off = (me.meters || []).map(l => (this._devices || []).find(x => x.id === l.device))
            .filter(m => m && (m.enabled === false || m.running === false));
        const offWord = off.length ? t('meters.why.meterOff', '{list}: not reading (disabled or stopped)',
            { list: off.map(m => m.name || m.id).join(', ') }) : '';
        const row = (name, label, unit) => {
            const v = st[name] || {};
            const ok = v.state === 'published';
            const word = v.state === 'waiting' ? (offWord || this._meterReasonWords('not_evaluated'))
                : ok ? '' : this._meterReasonWords(v.reason, opts);
            return `<tr data-mtr-value="${name}"><th scope="row" title="${this._esc(name)}">${label}</th>
                <td class="mtr-v">${ok ? fmt(v.value ?? live[name], unit) : '—'}</td>
                <td class="mtr-why">${word ? this._esc(word) : `<span class="sr-only">${t('meters.published', 'published')}</span>`}
                ${!ok && v.reason === 'no_rating' ? (['inverter', 'ups', 'charger'].includes(me.kind)
                    ? ` <a href="#/devices/${encodeURIComponent(me.id)}/attributes" ${this._act('_switchDeviceTab', ['attributes'])}>${t('meters.setRating', 'Set rated_ac_kw in Attributes')}</a>`
                    : ` ${this._esc(t('meters.noRatingKind', '(this kind of device has no rating)'))}`) : ''}</td></tr>`;
        };
        return `${head}<div class="settings-card-body">
            <p class="mtr-flow" id="mtrFlow">${this._esc(t('meters.in', 'In'))} <b data-live="power_in">${fmt(live.power_in, 'W')}</b>
                → ${this._esc(t('meters.out', 'Out'))} <b data-live="power_out">${fmt(live.power_out, 'W')}</b></p>
            <table class="mtr-gives"><tbody>
                ${row('power_loss', t('meters.loss', 'Loss'), 'W')}
                ${row('efficiency_pct', t('meters.eff', 'Efficiency'), '%')}
                ${row('load_pct', t('meters.load', 'Load'), '%')}
            </tbody></table></div>`;
    },

    async _metersGivesLive(me) {
        if (!me || !(me.meters || []).some(l => l.role === 'input')) return;
        let live = {};
        try {
            const d = await (await fetch('/api/values?device=' + encodeURIComponent(me.id))).json();
            Object.values(d.values || {}).forEach(v => { live[v.name] = v.value; });
        } catch (e) { live = {}; }
        if (this._devDetail?.id !== me.id) return;
        const box = document.getElementById('mtrGives');
        if (box) box.innerHTML = this._metersGivesHtml(me, live);
        this._pairLive = { id: me.id, live };
        this._renderPairLine();
    },

    _metersOptsReset() {
        document.querySelectorAll('#mtrOpts .mtr-opt').forEach(i => { i.value = ''; });
        this._mtrState = { touched: true, reset: true };
        const fb = document.getElementById('mtrFeedback');
        if (fb) { fb.textContent = this.t('meters.opt.resetSaid', 'Defaults on Save.'); fb.className = 'save-feedback'; }
    },

    // the thresholds a save sends: none untouched (absent keeps them); the
    // device's own plus what was typed, an emptied one left out (its
    // default); {} after "Reset to defaults" — never null
    _metersOptsRead() {
        const st = this._mtrState || {};
        if (st.reset) return { options: {}, errors: {} };
        if (!st.touched) return { options: undefined, errors: {} };
        const out = {}, errors = {};
        const cat = this._meterRolesData?.options || {};
        document.querySelectorAll('#mtrOpts .mtr-opt').forEach(inp => {
            const k = inp.dataset.opt, raw = inp.value.trim().replace(',', '.');
            if (raw === '') return;
            const v = /^\d+(\.\d*)?$|^\.\d+$/.test(raw) ? Number(raw) : NaN;
            const b = cat[k] || {};
            if (!Number.isFinite(v) || (b.min != null && v < b.min) || (b.max != null && v > b.max)) {
                errors[`options.${k}`] = this.t('meters.opt.range', 'A number from {min} to {max}', { min: b.min ?? 0, max: b.max ?? '∞' });
            } else out[k] = v;
        });
        return { options: out, errors };
    },

    async saveDeviceMeters(btn) {
        const s = this._devDetail;
        if (!s) return;
        const fb = document.getElementById('mtrFeedback');
        const panel = document.querySelector('#deviceDetailView [data-dpanel="meters"]');
        const say = (text, ok) => { if (fb) { fb.textContent = text; fb.className = `save-feedback ${ok ? 'ok' : 'err'}`; } };
        const links = this._metersRowsRead('mtr');
        const idOf = p => {
            const mid = this._mtrFieldId('mtr', p);
            if (mid) return mid;
            const o = /^options\.(\w+)$/.exec(p);
            return o ? `mtrOpt-${o[1]}` : null;
        };
        const local = {};
        links.forEach((l, i) => {
            if (!l.role) local[`meters[${i}].role`] = this.t('meters.needRole', 'Choose its role');
            if (!l.device) local[`meters[${i}].device`] = this.t('meters.needMeter', 'Choose the meter');
            else if (!l.power && !l.energy) local[`meters[${i}].power`] = this.t('meters.needField', 'Choose what it reads: its power or its energy counter');
        });
        const pair = links.some(l => l.role === 'input') && links.some(l => l.role === 'output');
        const o = pair ? this._metersOptsRead() : { options: undefined, errors: {} };
        Object.assign(local, o.errors);
        if (Object.keys(local).length) {
            const left = this._placeFieldErrors(panel, local, idOf);
            say(left.length ? left.join(' · ') : this.t('devices.fixMarked', 'Fix the marked fields.'), false);
            return;
        }
        this._placeFieldErrors(panel, {}, () => null);
        const body = { meters: links };
        if (o.options !== undefined) body.options = o.options;
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<i aria-hidden="true" class="bi bi-arrow-repeat spin"></i>'; }
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(s.id)}/meters`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            const res = await r.json().catch(() => ({}));
            if (!r.ok) {
                const det = res.detail || {};
                const left = det.fields ? this._placeFieldErrors(panel, det.fields, idOf) : [];
                const keys = Object.keys(det.fields || {});
                const rest = [...left, ...(det.errors || [typeof det === 'string' ? det : this.t('toast.saveFailed', 'Save Failed')])
                    .filter(e => !keys.some(k => String(e).startsWith(k + ': ')))];
                say(rest.length ? rest.join(' · ') : this.t('devices.fixMarked', 'Fix the marked fields.'), false);
                return;
            }
            this._meterFieldCache = null;
            const words = res.status === 'unchanged' ? this.t('devices.unchanged', 'Nothing changed — nothing was restarted.')
                : this.t('meters.saved', 'Saved — no restart: the values follow at the meters’ next reading.');
            await this._renderMetersTab();
            const fb2 = document.getElementById('mtrFeedback');
            if (fb2) { fb2.textContent = '✓ ' + words; fb2.className = 'save-feedback ok'; }
            document.getElementById('mtrSave')?.focus();
        } catch (e) {
            say(e.message || String(e), false);
        } finally {
            if (btn && document.contains(btn)) { btn.disabled = false; btn.innerHTML = orig; }
        }
    },

    // ── the Overview's line about a pair ───────────────────────────────────
    _renderPairLine() {
        const box = document.getElementById('ddvPairLine');
        const s = this._devDetail;
        if (!box || !s) return;
        const e = (this._devices || []).find(x => x.id === s.id) || s.entry || {};
        const roles = (e.meters || []).map(l => l.role);
        if (!(roles.includes('input') && roles.includes('output')) || e.enabled === false) { box.innerHTML = ''; return; }
        const t = (k, d, p) => this.t(k, d, p);
        const st = e.meters_status || {};
        const live = this._pairLive?.id === s.id ? this._pairLive.live : {};
        const fmt = (v, u) => v == null ? '—' : `${Number(v).toLocaleString(this._loc(), { maximumFractionDigits: u === '%' ? 1 : 0 })} ${u}`;
        const val = (k, u) => {
            const v = st[k] || {};
            return v.state === 'published' ? fmt(v.value ?? live[k], u)
                : `— <span class="field-hint">(${this._esc(this._meterReasonWords(v.state === 'waiting' ? 'not_evaluated' : v.reason, e.meter_options || {}))})</span>`;
        };
        box.innerHTML = `<p class="ddv-pair" id="ddvPair"><i aria-hidden="true" class="bi bi-lightning"></i>
            ${this._esc(t('meters.in', 'In'))} ${fmt(live.power_in, 'W')} → ${this._esc(t('meters.out', 'Out'))} ${fmt(live.power_out, 'W')}
            · ${this._esc(t('meters.loss', 'Loss'))} ${val('power_loss', 'W')} · ${this._esc(t('meters.eff', 'Efficiency'))} ${val('efficiency_pct', '%')}
            <a href="#/devices/${encodeURIComponent(s.id)}/meters" ${this._act('_switchDeviceTab', ['meters'])}>${t('meters.tab', 'Meters')}</a></p>`;
        if (!this._pairLive || this._pairLive.id !== s.id) this._metersGivesLive(e);
    },

    // ── Add Device: a device that IS its meters ────────────────────────────
    // Step 2 asks what it is (the kinds the gateway allows for it) and which
    // meters measure it — the same rows as the Meters tab.
    _devWizMetersStepHtml() {
        const w = this._devWiz, d = w.data;
        const t = (k, def, p) => this.t(k, def, p);
        const k = this._meterRolesData?.meter_only_kinds || { first: ['ups', 'charger', 'heat_pump', 'drive'], more: [] };
        const chip = kind => `<label class="seg-btn ${d.kind === kind ? 'on' : ''}"><input type="radio" name="wizMtrKind" value="${this._esc(kind)}" ${d.kind === kind ? 'checked' : ''}>
            <span class="s"></span> <i aria-hidden="true" class="bi ${this._KIND_ICON[kind] || this._KIND_ICON.other}"></i> ${this._esc(this._kindLabel(kind))}</label>`;
        const moreOpen = (k.more || []).includes(d.kind);
        return `
        <div class="wiz-eyebrow" id="wizMtrKindQ">${t('wizMtr.kindQ', 'What is it?')}</div>
        <div class="form-group" id="wizMtrKindGroup">
            <div class="seg wizmtr-kinds" role="radiogroup" aria-labelledby="wizMtrKindQ" id="wizMtrKinds" tabindex="-1">
                ${(k.first || []).map(chip).join('')}
            </div>
            ${(k.more || []).length ? `<details class="wizmtr-more" ${moreOpen ? 'open' : ''}><summary>${t('wizMtr.more', 'More kinds…')}</summary>
                <div class="seg wizmtr-kinds" role="radiogroup" aria-labelledby="wizMtrKindQ">${k.more.map(chip).join('')}</div></details>` : ''}
        </div>
        <div class="wiz-eyebrow">${t('wizMtr.metersQ', 'Which meters measure it?')}</div>
        <p class="field-hint" style="margin:0 0 8px;">${t('wizMtr.metersHint', 'One meter on its supply (consumption), or one on what goes in and one on what comes out (input and output: its loss and efficiency follow).')}</p>
        <div data-mtr-wrap="wizMtr">${this._metersRowsHtml('wizMtr', d.meters || [], {})}</div>`;
    },

    // a server path → the input on the wizard's meters step
    _wizMtrFieldId(p) {
        if (p === 'kind') return 'wizMtrKinds';
        const mid = this._mtrFieldId('wizMtr', p);
        if (mid) return mid;
        if (p === 'meters') return 'wizMtrDev-0';
        if (p === 'id') return 'devWizId';
        if (p === 'name') return 'devWizName';
        return null;
    },

    // the gateway asked before step 3: the kind and the meters it would
    // refuse are said here, at their fields
    async _devWizCheckMeters() {
        const d = this._devWiz.data;
        let r, res;
        try {
            r = await fetch('/api/devices?dry_run=true', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: 'x-dry-run', name: 'x', kind: d.kind, enabled: false,
                                       connection: { protocol: 'none' }, meters: d.meters || [] }) });
            res = await r.json().catch(() => ({}));
        } catch (e) { return true; }
        if (r.ok) {
            if (res.status === 'valid' && res.would) return true;
            if (res.status === 'created' || res.device) {
                await fetch('/api/devices/x-dry-run', { method: 'DELETE' }).catch(() => {});
            }
            return true;
        }
        if (r.status !== 422) return true;
        const det = res.detail || {};
        const fields = Object.fromEntries(Object.entries(det.fields || {}).filter(([k]) => k === 'kind' || k.startsWith('meters')));
        const keys = Object.keys(fields);
        const other = (det.errors || []).filter(e => !keys.some(k => String(e).startsWith(k + ': '))
            && !/^device 'x-dry-run'/.test(String(e)) && !/^id\b/.test(String(e)));
        if (!keys.length && !other.length) return true;
        const left = this._placeFieldErrors(document.getElementById('devWizBody'), fields, p => this._wizMtrFieldId(p));
        this._wizSay([...left, ...other].join(' · '));
        return false;
    },

    async _devWizCreateMeterOnly() {
        const w = this._devWiz, d = w.data;
        const payload = {
            id: d.id, name: d.name || d.id, kind: d.kind, enabled: d.enabled,
            connection: { protocol: 'none' }, meters: d.meters || [],
            mqtt: d.topic_prefix ? { topic_prefix: d.topic_prefix } : {},
            influxdb: { bucket: d.bucket || undefined, device_tag: d.device_tag || undefined },
            ha_discovery_enabled: d.ha_discovery_enabled,
        };
        let r, res;
        try {
            r = await fetch('/api/devices', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
            res = await r.json().catch(() => ({}));
        } catch (e) { this._wizSay(e.message || String(e)); return; }
        if (!r.ok) {
            const det = res.detail || {};
            const fields = det.fields || {};
            const errs = det.errors || [typeof det === 'string' ? det : this.t('toast.saveFailed', 'Save Failed')];
            // a refusal goes back to the step that owns its field
            const step = Object.keys(fields).some(k => k === 'kind' || k.startsWith('meters')) ? 2 : 3;
            if (step !== w.step) { w.step = step; this._devWizRender(); await new Promise(res2 => setTimeout(res2, 50)); }
            const keys = Object.keys(fields);
            const left = this._placeFieldErrors(document.getElementById('devWizBody'), fields, p => this._wizMtrFieldId(p));
            this._wizSay([...left, ...errs.filter(e => !keys.some(k => String(e).startsWith(k + ': ')))].join(' · '));
            return;
        }
        this.closeModal('deviceWizardModal');
        this.showToast('success', this.t('devices.created', 'Device created'),
            this.t('wizMtr.created', '{name}: measured by {list}', { name: d.name || d.id,
                list: (d.meters || []).map(l => ((this._devices || []).find(x => x.id === l.device) || {}).name || l.device).join(', ') }));
        // a device that reads nothing has no measurements to pick: its Meters
        await this.openDeviceDetail(d.id);
        this._switchDeviceTab('meters');
    },

    // ── a meter-only device's Connection tab: its identity, no address ───
    _meterOnlyIdentityHtml(s, d) {
        const t = (k, def, p) => this.t(k, def, p);
        const e = s.entry || {};
        return `
        <div class="settings-card">
            <div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-person-vcard"></i> ${t('ddv.identity', 'Identity')}</h3></div>
            <div class="settings-card-body">
                <div class="form-group" style="margin-bottom:10px;">
                    <span class="form-label">${t('devices.wizard.protocol', 'Protocol')}</span>
                    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
                        <span class="dev-chip" id="ddvProtoShown" data-proto="none"><i aria-hidden="true" class="bi bi-lock"></i> ${t('ddv.meterOnlyProto', 'Measured by its meters')}</span>
                        <span class="field-hint">${this._esc(t('ddv.measuredByList', 'Measured by {list} — change them on the Meters tab.', { list: (e.measured_by || []).join(', ') || '—' }))}
                            <a href="#/devices/${encodeURIComponent(s.id)}/meters" ${this._act('_switchDeviceTab', ['meters'])}>${t('meters.tab', 'Meters')}</a></span>
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-group flex-2"><label class="form-label" for="ddvName">${t('devices.wizard.name', 'Device name')}</label>
                        <input type="text" id="ddvName" class="input" value="${this._esc(d.name)}"></div>
                    <div class="form-group"><span class="form-label">${t('find.facet.kind', 'Kind')}</span>
                        <div class="ddv-kind">${this._kindCellHtml ? this._kindCellHtml(e.kind) : this._esc(e.kind || '')}
                            <button type="button" class="btn btn-ghost btn-sm" data-admin ${this._act('setDeviceKind', [s.id])}>${t('ddv.change', 'Change')}</button></div></div>
                </div>
                <label style="display:flex;align-items:center;gap:8px;">
                    <input type="checkbox" id="ddvEnabled" ${d.enabled ? 'checked' : ''}> ${t('ddv.meterOnlyEnabled', 'Enabled — computed from its meters')}</label>
            </div>
            <div class="settings-card-footer">
                <span class="save-feedback" id="ddvFeedback"></span>
                <button data-admin class="btn btn-primary btn-sm" data-action="saveDeviceDetail" data-with-el><i aria-hidden="true" class="bi bi-check-lg"></i> ${t('common.save', 'Save')}</button>
            </div>
        </div>`;
    },
});
