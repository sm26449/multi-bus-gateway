/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// Template editor, Advanced: everything a template can carry beyond its
// register rows, edited here instead of in an external editor — calculated
// fields, commands, how a unit is shown, how a bus scan recognises it, the
// template's own details, a per-row details dialog and the raw JSON.
// Every field edits the working copy (this._tplEdit.data) in place; the
// server checks the whole template after each change (nothing is saved
// until Save). Each tab explains what the block is for, shows an example,
// and links the guide (docs/templates-advanced.md).
Object.assign(JanitzaMonitor.prototype, {

    _ADV_DOC: 'https://github.com/sm26449/multi-bus-gateway/blob/main/docs/templates-advanced.md',

    // ── shell ──────────────────────────────────────────────────────────────
    _tplAdvancedHtml(d) {
        const e = this._tplEdit;
        e.advTab = e.advTab || 'calculated';
        const n = this._advCounts(d);
        const tabs = [
            ['calculated', 'bi-calculator', this.t('adv.tab.calculated', 'Calculated'), n.calculated],
            ['commands', 'bi-send', this.t('adv.tab.commands', 'Commands'), n.commands],
            ['display', 'bi-layout-text-window', this.t('adv.tab.display', 'Display'), n.display],
            ['identify', 'bi-fingerprint', this.t('adv.tab.identify', 'Scan recognition'), n.identify],
            ['details', 'bi-info-circle', this.t('adv.tab.details', 'Details & categories'), null],
            ['json', 'bi-braces', this.t('adv.tab.json', 'Raw JSON'), null],
        ];
        const total = n.calculated + n.commands + n.display + n.identify;
        return `
        <details class="tpl-proto tpl-adv" id="tplAdv" ${e.advOpen ? 'open' : ''}>
            <summary><i aria-hidden="true" class="bi bi-sliders"></i> ${this.t('adv.title', 'Advanced')}
                <span class="field-hint">— ${this.t('adv.summary', 'calculated fields, commands, display, scan recognition, details, raw JSON')}${total ? ` · ${total} ${this.t('adv.declared', 'declared')}` : ''}</span></summary>
            <div class="tpl-adv-tabs" role="tablist">${tabs.map(([id, ic, lbl, c]) => `
                <button type="button" role="tab" class="tpl-adv-tab ${e.advTab === id ? 'on' : ''}" aria-selected="${e.advTab === id}" ${this._act('_advTab', [id])}>
                    <i aria-hidden="true" class="bi ${ic}"></i> ${lbl}${c ? ` <span class="tpl-adv-n">${c}</span>` : ''}</button>`).join('')}
            </div>
            <div id="tplAdvPanel">${this._advPanelHtml()}</div>
            <div id="tplAdvCheck" class="tpl-adv-check" role="status" aria-live="polite"></div>
        </details>`;
    },

    // a row that carries something set in its details dialog
    _advRowHasExtras(r) {
        return ['scale_from', 'nan', 'monotonic', 'daily', 'aggregates', 'device_class', 'state_class',
                'entity_category', 'icon', 'suggested_display_precision', 'write_allowed']
            .some(k => r[k] != null && r[k] !== '' && r[k] !== false) || r.enabled_by_default != null;
    },

    _tplWireAdvanced() {
        const det = document.getElementById('tplAdv');
        if (det) det.addEventListener('toggle', () => { this._tplEdit.advOpen = det.open; });
        if (this._tplEdit.advOpen) this._advChanged();
    },

    _advCounts(d) {
        const disp = d.display || {};
        return {
            calculated: (d.calculated || []).length,
            commands: Object.keys(d.commands || {}).length,
            display: Object.keys(disp).length,
            identify: ((d.identify || {}).registers || []).length + ((d.identify || {}).fc43 ? 1 : 0),
        };
    },

    _advTab(tab) {
        this._tplCollectMeta();
        this._tplEdit.advTab = tab;
        this._tplEdit.advEdit = null;
        this._advRender();
    },

    _advRender() {
        const p = document.getElementById('tplAdvPanel');
        if (p) p.innerHTML = this._advPanelHtml();
        // the row dialog draws from the same builders: redraw it too while open
        const rb = document.getElementById('tplRowBody');
        if (rb && this._tplRowIdx != null && document.getElementById('tplRowModal')?.classList.contains('active'))
            rb.innerHTML = this._advRowHtml(this._tplRowIdx);
        {
            const det = document.getElementById('tplAdv');
            const d = this._tplEdit.data;
            const n = this._advCounts(d);
            det?.querySelectorAll('.tpl-adv-tab').forEach(b => {
                const id = (JSON.parse(b.dataset.args || '[]'))[0];
                b.classList.toggle('on', id === this._tplEdit.advTab);
                b.setAttribute('aria-selected', id === this._tplEdit.advTab);
                const badge = b.querySelector('.tpl-adv-n');
                const c = n[id];
                if (c && !badge) b.insertAdjacentHTML('beforeend', ` <span class="tpl-adv-n">${c}</span>`);
                else if (badge) { if (c) badge.textContent = c; else badge.remove(); }
            });
        }
        if (this._tplEdit.advTab === 'calculated' && this._tplEdit.advEdit?.kind === 'calc') this._advExprCheck();
    },

    _advPanelHtml() {
        const tab = this._tplEdit.advTab;
        const fn = { calculated: '_advCalcHtml', commands: '_advCmdHtml', display: '_advDisplayHtml',
                     identify: '_advIdentifyHtml', details: '_advDetailsHtml', json: '_advJsonHtml' }[tab];
        return fn ? this[fn]() : '';
    },

    // what a block is for, an example, and the guide
    _advIntro(lead, example, anchor) {
        return `<p class="tpl-adv-lead">${lead}
            <a href="${this._ADV_DOC}#${anchor}" target="_blank" rel="noopener">${this.t('adv.learnMore', 'Learn more')} <i aria-hidden="true" class="bi bi-box-arrow-up-right"></i></a></p>
            ${example ? `<details class="tpl-adv-example"><summary>${this.t('adv.example', 'Example')}</summary>${example}</details>` : ''}`;
    },

    // ── the working copy: get / set / delete by path ─────────────────────
    _advGet(path) {
        let o = this._tplEdit.data;
        for (const k of path) { if (o == null) return undefined; o = o[k]; }
        return o;
    },

    _advPut(path, v) {
        let o = this._tplEdit.data;
        for (let i = 0; i < path.length - 1; i++) {
            const k = path[i];
            if (o[k] == null || typeof o[k] !== 'object') o[k] = typeof path[i + 1] === 'number' ? [] : {};
            o = o[k];
        }
        o[path[path.length - 1]] = v;
    },

    // delete, then drop the containers it leaves empty (an empty `identify`
    // or `display` is not "nothing declared" to the validator — it is invalid)
    _advDelPath(path) {
        const parent = this._advGet(path.slice(0, -1));
        if (parent == null) return;
        const k = path[path.length - 1];
        if (Array.isArray(parent) && typeof k === 'number') parent.splice(k, 1);
        else delete parent[k];
        for (let i = path.length - 1; i > 0; i--) {
            const c = this._advGet(path.slice(0, i));
            const empty = c != null && typeof c === 'object' && (Array.isArray(c) ? !c.length : !Object.keys(c).length);
            if (!empty || (i === 1 && path[0] === 'registers')) break;
            if (i === 2 && path[0] === 'registers') break;     // never drop a register row
            const pp = this._advGet(path.slice(0, i - 1));
            const pk = path[i - 1];
            if (Array.isArray(pp) && typeof pk === 'number') break;   // keep list items
            delete pp[pk];
        }
    },

    // one field changed: parse by kind, write or delete, check the template
    _advSet(path, kind, el) {
        let v;
        const raw = el.type === 'checkbox' ? el.checked : el.value;
        switch (kind) {
            case 'bool': v = !!raw; break;
            case 'flag': v = raw ? true : undefined; break;              // only `true` is written
            case 'tri': v = raw === '' ? undefined : raw === 'true'; break;
            case 'int': v = raw === '' ? undefined : parseInt(raw, 10); if (Number.isNaN(v)) v = undefined; break;
            case 'num': v = raw === '' ? undefined : Number(raw); if (Number.isNaN(v)) v = undefined; break;
            case 'numlist': v = String(raw).split(',').map(s => s.trim()).filter(Boolean).map(Number);
                if (!v.length || v.some(Number.isNaN)) v = v.length ? v.filter(x => !Number.isNaN(x)) : undefined; break;
            case 'strlist': v = String(raw).split(',').map(s => s.trim()).filter(Boolean); if (!v.length) v = undefined; break;
            case 'value': {                                               // a command value: number, ${param}, or JSON
                const s = String(raw).trim();
                if (s === '') { v = undefined; break; }
                try { v = JSON.parse(s); } catch (_) { v = s; }
                break;
            }
            case 'nan': v = raw === '' ? undefined : raw === 'true' ? true
                : (String(raw).includes(',') ? String(raw).split(',').map(Number) : Number(raw)); break;
            case 'map': {                                                 // "code = text" per line
                const m = {};
                String(raw).split('\n').forEach(line => {
                    const i = line.indexOf('=');
                    if (i > 0) m[line.slice(0, i).trim()] = line.slice(i + 1).trim();
                });
                v = Object.keys(m).length ? m : undefined;
                break;
            }
            case 'text': v = raw; break;                                 // kept as typed (an expression)
            default: v = String(raw).trim() || undefined;
        }
        if (v === undefined) this._advDelPath(path); else this._advPut(path, v);
        if (el.dataset.rerender !== undefined) this._advRender();
        if (path[0] === 'calculated' && path[2] === 'expr') this._advExprCheck();
        this._advChanged();
    },

    _advAdd(path, proto) {
        this._tplCollectMeta();
        const cur = this._advGet(path);
        const item = JSON.parse(JSON.stringify(proto));
        if (Array.isArray(cur)) cur.push(item);
        else this._advPut(path, [item]);
        this._advRender();
        this._advChanged();
    },

    // add a key to an object map (commands, params, bitmasks…) under a free name
    _advAddKey(path, base, proto) {
        this._tplCollectMeta();
        const cur = this._advGet(path) || {};
        let k = base, i = 2;
        while (k in cur) k = `${base}_${i++}`;
        this._advPut([...path, k], JSON.parse(JSON.stringify(proto)));
        if (path[0] === 'commands' && path.length === 1) this._tplEdit.advEdit = { kind: 'cmd', key: k };
        this._advRender();
        this._advChanged();
    },

    _advRemove(path) {
        this._tplCollectMeta();
        this._advDelPath(path);
        const ed = this._tplEdit.advEdit;
        if (ed && ((ed.kind === 'calc' && path[0] === 'calculated' && path.length === 2 && path[1] === ed.i)
                || (ed.kind === 'cmd' && path[0] === 'commands' && path.length === 2 && path[1] === ed.key)))
            this._tplEdit.advEdit = null;
        this._advRender();
        this._advChanged();
    },

    // rename a key of an object map, keeping its place
    _advRenameKey(path, oldKey, el) {
        const nk = String(el.value).trim();
        const obj = this._advGet(path);
        if (!obj || !nk || nk === oldKey) return;
        if (nk in obj) { el.value = oldKey; this.showToast('warning', this.t('adv.exists', 'Already used'), nk); return; }
        const out = {};
        for (const [k, v] of Object.entries(obj)) out[k === oldKey ? nk : k] = v;
        this._advPut(path, out);
        const ed = this._tplEdit.advEdit;
        if (ed && ed.kind === 'cmd' && path.length === 1 && path[0] === 'commands' && ed.key === oldKey) ed.key = nk;
        this._advRender();
        this._advChanged();
    },

    _advEdit(kind, id) {
        this._tplCollectMeta();
        const cur = this._tplEdit.advEdit;
        const same = cur && cur.kind === kind && (kind === 'calc' ? cur.i === id : cur.key === id);
        this._tplEdit.advEdit = same ? null : (kind === 'calc' ? { kind, i: id } : { kind, key: id });
        this._advRender();
    },

    // the server checks the whole template a moment after the last change
    _advChanged() {
        clearTimeout(this._advCheckTimer);
        this._advCheckTimer = setTimeout(() => this._advCheckNow(), 600);
    },

    async _advCheckNow() {
        const box = document.getElementById('tplAdvCheck');
        if (!box || !this._tplEdit) return;
        try {
            const r = await fetch('/api/device-templates/check', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ device_template: this._tplEdit.data }) });
            const d = await r.json();
            const errs = (d.errors || []).filter(x => !/^id: |^name: /.test(x));
            box.className = 'tpl-adv-check ' + (errs.length ? 'bad' : 'ok');
            box.innerHTML = errs.length
                ? `<i aria-hidden="true" class="bi bi-exclamation-triangle"></i> ${this.t('adv.problems', 'Save would refuse this:')}<ul>${errs.slice(0, 8).map(x => `<li>${this._esc(x)}</li>`).join('')}${errs.length > 8 ? `<li>… ${errs.length - 8} ${this.t('adv.more', 'more')}</li>` : ''}</ul>`
                : `<i aria-hidden="true" class="bi bi-check-circle"></i> ${this.t('adv.noProblems', 'No problems — Save to keep it.')}`;
        } catch (_) { /* offline: Save reports it */ }
    },

    // ── small field builders (each writes straight into the working copy) ──
    _advIn(path, kind, value, opts = {}) {
        const v = value === undefined || value === null ? '' : (Array.isArray(value) ? value.join(', ') : (typeof value === 'object' ? JSON.stringify(value) : value));
        return `<input class="input ${opts.cls || ''}" ${opts.type ? `type="${opts.type}"` : ''} ${opts.step ? `step="${opts.step}"` : ''}
            value="${this._esc(String(v))}" ${opts.ph ? `placeholder="${this._esc(opts.ph)}"` : ''} ${opts.list ? `list="${opts.list}"` : ''}
            aria-label="${this._esc(opts.label || path.join('.'))}" ${opts.style ? `style="${opts.style}"` : ''} ${opts.rerender ? 'data-rerender' : ''}
            ${this._act('_advSet', [path, kind], { el: true, on: 'change' })}>`;
    },

    _advSel(path, kind, value, options, opts = {}) {
        return `<select class="input ${opts.cls || ''}" aria-label="${this._esc(opts.label || path.join('.'))}" ${opts.style ? `style="${opts.style}"` : ''} ${opts.rerender ? 'data-rerender' : ''}
            ${this._act('_advSet', [path, kind], { el: true, on: 'change' })}>
            ${options.map(o => { const [val, lbl] = Array.isArray(o) ? o : [o, o];
                return `<option value="${this._esc(String(val))}" ${String(val) === String(value ?? '') ? 'selected' : ''}>${this._esc(String(lbl))}</option>`; }).join('')}
        </select>`;
    },

    _advChk(path, value, label, kind = 'bool') {
        return `<label class="checkbox-label"><input type="checkbox" ${value ? 'checked' : ''}
            ${this._act('_advSet', [path, kind], { el: true, on: 'change' })}> <span>${label}</span></label>`;
    },

    _advField(label, html, hint = '', cls = '') {
        return `<div class="form-group ${cls}"><label class="form-label">${label}</label>${html}${hint ? `<div class="field-hint">${hint}</div>` : ''}</div>`;
    },

    _advDelBtn(path) {
        return `<button type="button" class="btn btn-ghost btn-sm" ${this._act('_advRemove', [path])} aria-label="${this._esc(this.t('common.delete', 'Delete'))}" title="${this._esc(this.t('common.delete', 'Delete'))}"><i aria-hidden="true" class="bi bi-trash"></i></button>`;
    },

    // names a block can refer to: the registers, and the calculated fields
    _advNames(withCalc = true) {
        const d = this._tplEdit.data;
        const regs = (d.registers || []).map(r => r.name).filter(Boolean);
        const calc = withCalc ? (d.calculated || []).map(c => c.name).filter(Boolean) : [];
        return [...new Set([...regs, ...calc])];
    },

    _advNameOptions(withCalc = true, blank = true) {
        return [...(blank ? [['', '—']] : []), ...this._advNames(withCalc).map(n => [n, n])];
    },

    // ═══ Calculated fields ═════════════════════════════════════════════════
    _advCalcHtml() {
        const d = this._tplEdit.data;
        const list = d.calculated || [];
        const ed = this._tplEdit.advEdit;
        const t = (k, def) => this.t(k, def);
        const intro = this._advIntro(
            t('adv.calc.lead', 'A value the device does not report, computed from the ones it does — total power from the three phases, a power factor, a status decoded to text. Every device made from this template gets it; it publishes to MQTT and InfluxDB like a register.'),
            `<pre class="tpl-adv-code">name:  power_active_total
expr:  power_l1 + power_l2 + power_l3
unit:  W

name:  battery_state
expr:  1 if current > 0.5 else (2 if current &lt; -0.5 else 0)
enum:  0 = Idle · 1 = Charging · 2 = Discharging</pre>
            <p class="field-hint">${t('adv.calc.exHint', 'A formula reads the registers of the same device by their name. Functions: min, max, avg, abs, round, sqrt, pow, floor, ceil, clamp, popcount; prev(x) and dt for rates.')}</p>`,
            'calculated-fields');
        const rows = list.map((c, i) => `
            <tr class="${ed?.kind === 'calc' && ed.i === i ? 'tpl-adv-sel' : ''}">
                <td><code>${this._esc(c.name || '—')}</code></td>
                <td class="tpl-adv-expr"><code>${this._esc(c.expr || '')}</code></td>
                <td>${this._esc(c.unit || '')}</td>
                <td style="white-space:nowrap;">
                    <button type="button" class="btn btn-ghost btn-sm" ${this._act('_advEdit', ['calc', i])} aria-label="${this._esc(t('common.edit', 'Edit'))}"><i aria-hidden="true" class="bi bi-pencil"></i></button>
                    ${this._advDelBtn(['calculated', i])}</td>
            </tr>`).join('');
        return `${intro}
            ${list.length ? `<table class="data-table tpl-adv-table"><thead><tr><th>${t('calc.name', 'Name')}</th><th>${t('calc.expr', 'Expression')}</th><th>${t('calc.unit', 'Unit')}</th><th></th></tr></thead><tbody>${rows}</tbody></table>` : `<p class="field-hint">${t('adv.calc.none', 'None yet.')}</p>`}
            ${ed?.kind === 'calc' && list[ed.i] ? this._advCalcForm(ed.i) : ''}
            <button type="button" class="btn btn-secondary btn-sm" ${this._act('_advAddCalc', [])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('adv.calc.add', 'Add a calculated field')}</button>`;
    },

    _advAddCalc() {
        this._tplCollectMeta();
        const d = this._tplEdit.data;
        (d.calculated = d.calculated || []).push({ name: '', expr: '' });
        this._tplEdit.advEdit = { kind: 'calc', i: d.calculated.length - 1 };
        this._advRender();
    },

    _advCalcForm(i) {
        const c = this._tplEdit.data.calculated[i];
        const P = k => ['calculated', i, k];
        const t = (k, def) => this.t(k, def);
        const groups = [['', t('adv.calc.anyGroup', '— with its inputs —')], ...Object.keys(this._tplEdit.data.poll_groups || {}).map(g => [g, g])];
        const names = this._advNames(false);
        const fns = (this._calcFns || { functions: [], operators: [] });
        if (!this._calcFns) this._advLoadFns();
        const enumText = c.enum ? Object.entries(c.enum).map(([k, v]) => `${k} = ${v}`).join('\n') : '';
        const aggs = Object.entries(c.aggregates || {});
        return `<div class="tpl-adv-form">
            <div class="form-row">
                ${this._advField(t('calc.name', 'Name'), this._advIn(P('name'), 'str', c.name, { ph: 'power_active_total', label: 'name' }), t('adv.calc.nameHint', 'Letters, digits and _ — not the name of a register.'))}
                ${this._advField(t('calc.label', 'Label'), this._advIn(P('label'), 'str', c.label, { label: 'label' }))}
                ${this._advField(t('calc.unit', 'Unit'), this._advIn(P('unit'), 'str', c.unit, { label: 'unit', style: 'max-width:90px' }))}
                ${this._advField(t('calc.decimals', 'Decimals'), this._advIn(P('decimals'), 'int', c.decimals, { type: 'number', label: 'decimals', style: 'max-width:80px' }))}
                ${this._advField(t('calc.pollGroup', 'Poll group'), this._advSel(P('poll_group'), 'str', c.poll_group, groups, { label: 'poll group' }))}
            </div>
            <div class="form-group">
                <label class="form-label" for="advExpr">${t('calc.expr', 'Expression')}</label>
                <textarea id="advExpr" class="input calc-expr-input" rows="2" spellcheck="false" aria-label="expression"
                    ${this._act('_advSet', [P('expr'), 'text'], { el: true, on: 'input' })}>${this._esc(c.expr || '')}</textarea>
                <div id="advExprCheck" class="calc-preview"></div>
            </div>
            <div class="calc-palette"><div class="calc-palette-label">${t('adv.calc.regs', 'Registers of this template')}</div>
                <div class="calc-chips">${names.slice(0, 300).map(n => `<button type="button" class="calc-chip" ${this._act('_advInsert', [n])}>${this._esc(n)}</button>`).join('') || `<span class="calc-empty">${t('adv.calc.noRegs', 'Name the registers first.')}</span>`}</div></div>
            <div class="calc-palette"><div class="calc-palette-label">${t('calc.functions', 'Functions & operators')}</div>
                <div class="calc-chips">${(fns.functions || []).map(f => `<button type="button" class="calc-chip fn" title="${this._esc(f.desc)}" ${this._act('_advInsert', [f.name === 'dt' ? 'dt' : f.name + '()'])}>${this._esc(f.sig)}</button>`).join('')}
                ${(fns.operators || []).map(o => `<button type="button" class="calc-chip op" ${this._act('_advInsert', [' ' + o.replace(' ', '') + ' '])}>${this._esc(o)}</button>`).join('')}</div></div>
            <details class="tpl-adv-more"><summary>${t('adv.more', 'More')} — ${t('adv.calc.moreSum', 'text states, MQTT topic, totals of an installation, dashboard')}</summary>
                <div class="form-row">
                    ${this._advField(t('adv.calc.enum', 'Text for codes'), `<textarea class="input" rows="3" spellcheck="false" placeholder="0 = Idle&#10;1 = Charging" aria-label="enum" ${this._act('_advSet', [P('enum'), 'map'], { el: true, on: 'change' })}>${this._esc(enumText)}</textarea>`,
                        t('adv.calc.enumHint', 'One “code = text” per line: the computed number is published as the text.'))}
                    ${this._advField(t('adv.calc.topic', 'MQTT topic'), this._advIn(P('topic'), 'str', c.topic, { ph: 'status/text', label: 'topic' }),
                        t('adv.calc.topicHint', 'Relative to the device topic. Empty: the name.'))}
                    ${this._advField(t('adv.calc.measurement', 'InfluxDB measurement'), this._advIn(P('measurement'), 'str', c.measurement, { label: 'measurement' }))}
                </div>
                <div class="form-row">
                    ${this._advChk(P('mqtt'), c.mqtt !== false, t('adv.calc.mqtt', 'Publish to MQTT'), 'bool')}
                    ${this._advChk(P('influxdb'), c.influxdb !== false, t('adv.calc.influx', 'Write to InfluxDB'), 'bool')}
                    ${this._advChk(['calculated', i, 'ui', 'show_on_dashboard'], (c.ui || {}).show_on_dashboard !== false, t('adv.calc.dash', 'On the dashboard'), 'bool')}
                    ${this._advField(t('lbl.widget', 'Widget'), this._advSel(['calculated', i, 'ui', 'widget'], 'str', (c.ui || {}).widget || 'value', [['value', t('lbl.value', 'Value')], ['gauge', t('lbl.gauge', 'Gauge')], ['chart', t('lbl.chart', 'Chart')]]))}
                </div>
                ${this._advAggHtml(['calculated', i, 'aggregates'], aggs)}
            </details>
            <div style="text-align:right;"><button type="button" class="btn btn-primary btn-sm" ${this._act('_advEdit', ['calc', i])}><i aria-hidden="true" class="bi bi-check-lg"></i> ${t('adv.done', 'Done')}</button></div>
        </div>`;
    },

    async _advLoadFns() {
        try { this._calcFns = await (await fetch('/api/calculated/functions')).json(); }
        catch (_) { this._calcFns = { functions: [], operators: [] }; }
        if (this._tplEdit?.advEdit?.kind === 'calc') this._advRender();
    },

    _advInsert(text) {
        const ta = document.getElementById('advExpr');
        if (!ta) return;
        const s = ta.selectionStart ?? ta.value.length, e = ta.selectionEnd ?? s;
        ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
        const at = s + (text.endsWith('()') ? text.length - 1 : text.length);
        ta.focus(); ta.setSelectionRange(at, at);
        ta.dispatchEvent(new Event('input', { bubbles: true }));
    },

    // the formula, checked against THIS template's names (no device needed)
    _advExprCheck() {
        clearTimeout(this._advExprTimer);
        this._advExprTimer = setTimeout(async () => {
            const box = document.getElementById('advExprCheck');
            const ed = this._tplEdit?.advEdit;
            if (!box || !ed || ed.kind !== 'calc') return;
            const c = this._tplEdit.data.calculated[ed.i] || {};
            if (!(c.expr || '').trim()) { box.innerHTML = ''; return; }
            try {
                const r = await (await fetch('/api/device-templates/check-expression', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ template: this._tplEdit.data, expr: c.expr, name: c.name }) })).json();
                box.className = 'calc-preview ' + (r.ok ? 'ok' : 'err');
                box.innerHTML = r.ok
                    ? `<i aria-hidden="true" class="bi bi-check-circle"></i> ${this.t('adv.calc.ok', 'Valid')} · ${this.t('adv.calc.reads', 'reads')} ${(r.refs || []).map(x => `<code>${this._esc(x)}</code>`).join(', ') || '—'}${r.warning ? `<br><i aria-hidden="true" class="bi bi-exclamation-triangle"></i> ${this._esc(r.warning)}` : ''}`
                    : `<i aria-hidden="true" class="bi bi-x-circle"></i> ${this._esc(r.error || '')}`;
            } catch (_) { /* offline */ }
        }, 400);
    },

    // {output_name: op} — how an installation of many units totals this field
    _advAggHtml(path, entries) {
        const t = (k, def) => this.t(k, def);
        const OPS = [['sum', t('adv.agg.sum', 'sum')], ['avg', t('adv.agg.avg', 'average')], ['min', t('adv.agg.min', 'minimum')],
                     ['max', t('adv.agg.max', 'maximum')], ['spread', t('adv.agg.spread', 'spread (max − min)')], ['mode', t('adv.agg.mode', 'most common')]];
        return `<div class="form-group"><label class="form-label">${t('adv.agg.title', 'Totals of an installation')}</label>
            <div class="field-hint">${t('adv.agg.hint', 'When several units of this template form an installation (a battery bank, an inverter farm), each line publishes one total: its name and how the units’ values combine. pack_average_soc = average of soc.')}
                <a href="${this._ADV_DOC}#totals-of-an-installation" target="_blank" rel="noopener">${t('adv.learnMore', 'Learn more')}</a></div>
            ${entries.map(([name, op]) => `<div class="form-row tpl-adv-line">
                <input class="input" value="${this._esc(name)}" aria-label="output name" ${this._act('_advRenameKey', [path, name], { el: true, on: 'change' })}>
                ${this._advSel([...path, name], 'str', op, OPS, { label: 'op' })}
                ${this._advDelBtn([...path, name])}</div>`).join('')}
            <button type="button" class="btn btn-ghost btn-sm" ${this._act('_advAddKey', [path, 'total', 'sum'])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('adv.agg.add', 'Add a total')}</button>
        </div>`;
    },

    // ═══ Commands ══════════════════════════════════════════════════════════
    _advCmdHtml() {
        const d = this._tplEdit.data;
        const cmds = d.commands || {};
        const ed = this._tplEdit.advEdit;
        const t = (k, def) => this.t(k, def);
        const intro = this._advIntro(
            t('adv.cmd.lead', 'A named, guarded write — “power_limit = 60 %” — that a controller, a rule, Home Assistant or the UI can ask for, without knowing the registers. The template says how: which registers, in which order, what must be true first, and how to check it took.'),
            `<pre class="tpl-adv-code">power_limit
  param   value: 0 … 100 %, required
  guard   controls_model_id = 123          ← refuse on the wrong device
  writes  power_limit_pct    = \${value}
          power_limit_enabled = {"if": "\${value} &lt; 100", "then": 1, "else": 0}
  verify  power_limit_pct = \${value} (± 1)
  safe    value = 100                       ← what an expiring lease restores</pre>
            <p class="field-hint">${this._esc(t('adv.cmd.exHint', 'A value is a number, ${param}, or a small JSON rule: {"*": ["${value}", 10]} multiplies, {"if": "${value} < 100", "then": 1, "else": 0} chooses.'))}</p>`,
            'commands');
        const rows = Object.entries(cmds).map(([k, c]) => `
            <tr class="${ed?.kind === 'cmd' && ed.key === k ? 'tpl-adv-sel' : ''}">
                <td><code>${this._esc(k)}</code></td><td>${this._esc(c.label || '')}</td>
                <td class="field-hint">${c.alias ? `→ ${this._esc(c.alias.command || '')}` : `${(c.writes || []).length} ${t('adv.cmd.writesN', 'write(s)')}`}</td>
                <td style="white-space:nowrap;"><button type="button" class="btn btn-ghost btn-sm" ${this._act('_advEdit', ['cmd', k])} aria-label="${this._esc(t('common.edit', 'Edit'))}"><i aria-hidden="true" class="bi bi-pencil"></i></button>${this._advDelBtn(['commands', k])}</td>
            </tr>`).join('');
        return `${intro}
            ${rows ? `<table class="data-table tpl-adv-table"><tbody>${rows}</tbody></table>` : `<p class="field-hint">${t('adv.cmd.none', 'None yet.')}</p>`}
            ${ed?.kind === 'cmd' && cmds[ed.key] ? this._advCmdForm(ed.key) : ''}
            <button type="button" class="btn btn-secondary btn-sm" ${this._act('_advAddKey', [['commands'], 'command', { label: '', params: { value: { label: 'Value', required: true } }, writes: [{ register: '', value: '${value}' }], confirm: true }])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('adv.cmd.add', 'Add a command')}</button>`;
    },

    _advCmdForm(key) {
        const c = this._tplEdit.data.commands[key];
        const P = (...k) => ['commands', key, ...k];
        const t = (k, def) => this.t(k, def);
        const regs = this._advNameOptions(false);
        const params = Object.entries(c.params || {});
        const paramOpts = [['', '—'], ...params.map(([n]) => [n, n])];
        const groups = [['', '—'], ...Object.keys(this._tplEdit.data.poll_groups || {}).map(g => [g, g])];
        const isAlias = !!c.alias;
        const otherCmds = [['', '—'], ...Object.keys(this._tplEdit.data.commands || {}).filter(k => k !== key).map(k => [k, k])];
        const list = (title, hint, path, items, cells, proto) => `
            <div class="form-group"><label class="form-label">${title}</label>${hint ? `<div class="field-hint">${hint}</div>` : ''}
                ${(items || []).map((it, j) => `<div class="form-row tpl-adv-line">${cells(it, [...path, j])}${this._advDelBtn([...path, j])}</div>`).join('')}
                <button type="button" class="btn btn-ghost btn-sm" ${this._act('_advAdd', [path, proto])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('adv.add', 'Add')}</button></div>`;
        return `<div class="tpl-adv-form">
            <div class="form-row">
                ${this._advField(t('adv.cmd.name', 'Name'), `<input class="input" value="${this._esc(key)}" aria-label="command name" ${this._act('_advRenameKey', [['commands'], key], { el: true, on: 'change' })}>`, t('adv.cmd.nameHint', 'a-z 0-9 _ - · it becomes an MQTT topic and an HA entity'))}
                ${this._advField(t('calc.label', 'Label'), this._advIn(P('label'), 'str', c.label, { label: 'label' }))}
                ${this._advChk(P('confirm'), c.confirm !== false, t('adv.cmd.confirm', 'Ask for confirmation in the UI'), 'bool')}
            </div>
            <div class="form-row">
                <label class="checkbox-label"><input type="checkbox" ${isAlias ? 'checked' : ''} ${this._act('_advCmdAlias', [key], { el: true, on: 'change' })}>
                    <span>${t('adv.cmd.isAlias', 'A shortcut for another command with fixed parameters (e.g. “restore” = power_limit 100 %)')}</span></label>
            </div>
            ${isAlias ? `<div class="form-row">
                ${this._advField(t('adv.cmd.aliasOf', 'Runs'), this._advSel(P('alias', 'command'), 'str', c.alias.command, otherCmds, { label: 'alias command' }))}
                ${this._advField(t('adv.cmd.aliasParams', 'With parameters'), this._advIn(P('alias', 'params'), 'value', c.alias.params, { ph: '{"value": 100}', label: 'alias params' }), this._esc(t('adv.cmd.aliasParamsHint', 'JSON object: {"value": 100, "revert_s": 0}')))}
            </div>` : `
            <div class="form-group"><label class="form-label">${t('adv.cmd.params', 'Parameters')}</label>
                <div class="field-hint">${t('adv.cmd.paramsHint', 'What the caller gives. The main one is called “value” (Home Assistant shows it as a number). Bounds are enforced before anything is written.')}</div>
                ${params.map(([pn, pd]) => `<div class="form-row tpl-adv-line">
                    <input class="input" value="${this._esc(pn)}" aria-label="param name" style="max-width:120px" ${this._act('_advRenameKey', [P('params'), pn], { el: true, on: 'change' })}>
                    ${this._advIn(P('params', pn, 'label'), 'str', pd.label, { ph: t('calc.label', 'Label'), label: 'param label' })}
                    ${this._advIn(P('params', pn, 'unit'), 'str', pd.unit, { ph: t('calc.unit', 'Unit'), label: 'param unit', style: 'max-width:70px' })}
                    ${this._advIn(P('params', pn, 'min'), 'num', pd.min, { ph: 'min', type: 'number', step: 'any', label: 'min', style: 'max-width:80px' })}
                    ${this._advIn(P('params', pn, 'max'), 'num', pd.max, { ph: 'max', type: 'number', step: 'any', label: 'max', style: 'max-width:80px' })}
                    ${this._advIn(P('params', pn, 'default'), 'num', pd.default, { ph: t('adv.default', 'default'), type: 'number', step: 'any', label: 'default', style: 'max-width:90px' })}
                    ${this._advChk(P('params', pn, 'required'), pd.required, t('adv.required', 'required'), 'flag')}
                    ${this._advDelBtn(P('params', pn))}</div>`).join('')}
                <button type="button" class="btn btn-ghost btn-sm" ${this._act('_advAddKey', [P('params'), 'param', { label: '' }])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('adv.add', 'Add')}</button>
            </div>
            ${list(t('adv.cmd.guard', 'First check (guard)'), t('adv.cmd.guardHint', 'Registers read before writing; if one does not hold, nothing is written. Fill one condition per line.'),
                P('guard'), c.guard, (g, p) => `
                ${this._advSel([...p, 'read'], 'str', g.read, regs, { label: 'read' })}
                ${this._advIn([...p, 'expect'], 'value', g.expect, { ph: '= value', label: 'expect', style: 'max-width:100px' })}
                ${this._advIn([...p, 'in'], 'numlist', g.in, { ph: 'one of: 1, 2', label: 'in', style: 'max-width:110px' })}
                ${this._advIn([...p, 'min'], 'num', g.min, { ph: '≥ min', type: 'number', step: 'any', label: 'min', style: 'max-width:80px' })}
                ${this._advIn([...p, 'max'], 'num', g.max, { ph: '≤ max', type: 'number', step: 'any', label: 'max', style: 'max-width:80px' })}`, { read: '' })}
            ${list(t('adv.cmd.writes', 'Writes, in order'), t('adv.cmd.writesHint', 'Consecutive registers go out as one frame. A value: a number, ${value}, or a JSON rule.'),
                P('writes'), c.writes, (w, p) => `
                ${this._advSel([...p, 'register'], 'str', w.register, regs, { label: 'register' })}
                ${this._advIn([...p, 'value'], 'value', w.value, { ph: '${value}', label: 'value' })}`, { register: '', value: '${value}' })}
            ${list(t('adv.cmd.verify', 'Then check it took (verify)'), t('adv.cmd.verifyHint', 'Read back after the settle time; a difference beyond the tolerance answers “mismatch”.'),
                P('verify'), c.verify, (v, p) => `
                ${this._advSel([...p, 'read'], 'str', v.read, regs, { label: 'read' })}
                ${this._advIn([...p, 'expect'], 'value', v.expect, { ph: '${value}', label: 'expect' })}
                ${this._advIn([...p, 'tolerance'], 'num', v.tolerance, { ph: '± tolerance', type: 'number', step: 'any', label: 'tolerance', style: 'max-width:110px' })}`, { read: '', expect: '${value}' })}
            <div class="form-row">
                ${this._advField(t('adv.cmd.settle', 'Settle time (s)'), this._advIn(P('settle_s'), 'num', c.settle_s, { type: 'number', step: 'any', label: 'settle', style: 'max-width:100px' }), t('adv.cmd.settleHint', 'Wait before verifying.'))}
                ${this._advField(t('adv.cmd.readback', 'Then re-read group'), this._advSel(P('readback_group'), 'str', c.readback_group, groups, { label: 'readback group' }), t('adv.cmd.readbackHint', 'So MQTT/HA see the new value in seconds.'))}
            </div>
            <div class="form-group"><label class="form-label">${t('adv.cmd.safe', 'Safe values')}</label>
                <div class="field-hint">${t('adv.cmd.safeHint', 'The parameters a timed (leased) command is restored with when its lease expires — e.g. value = 100 % for a power limit.')}</div>
                ${Object.entries(c.safe || {}).map(([sp, sv]) => `<div class="form-row tpl-adv-line">
                    <input class="input" value="${this._esc(sp)}" aria-label="safe param" list="advParamList" style="max-width:140px" ${this._act('_advRenameKey', [P('safe'), sp], { el: true, on: 'change' })}>
                    ${this._advIn(P('safe', sp), 'num', sv, { type: 'number', step: 'any', label: 'safe value', style: 'max-width:100px' })}
                    ${this._advDelBtn(P('safe', sp))}</div>`).join('')}
                <datalist id="advParamList">${paramOpts.slice(1).map(([n]) => `<option value="${this._esc(n)}"></option>`).join('')}</datalist>
                <button type="button" class="btn btn-ghost btn-sm" ${this._act('_advAddKey', [P('safe'), params[0]?.[0] || 'value', 0])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('adv.add', 'Add')}</button>
            </div>`}
            <div style="text-align:right;"><button type="button" class="btn btn-primary btn-sm" ${this._act('_advEdit', ['cmd', key])}><i aria-hidden="true" class="bi bi-check-lg"></i> ${t('adv.done', 'Done')}</button></div>
        </div>`;
    },

    _advCmdAlias(key, el) {
        const c = this._tplEdit.data.commands[key];
        if (el.checked) {
            c._unalias = { params: c.params, guard: c.guard, writes: c.writes, verify: c.verify, settle_s: c.settle_s, safe: c.safe, readback_group: c.readback_group };
            for (const k of Object.keys(c._unalias)) delete c[k];
            c.alias = { command: Object.keys(this._tplEdit.data.commands).find(k => k !== key) || '', params: {} };
            delete c._unalias;
        } else {
            delete c.alias;
            c.params = { value: { label: 'Value', required: true } };
            c.writes = [{ register: '', value: '${value}' }];
        }
        this._advRender();
        this._advChanged();
    },

    // ═══ Display ═══════════════════════════════════════════════════════════
    _advDisplayHtml() {
        const d = this._tplEdit.data;
        const disp = d.display || {};
        const t = (k, def) => this.t(k, def);
        const P = (...k) => ['display', ...k];
        const names = this._advNameOptions(true);
        const cats = [['', '—'], ...[...new Set([...Object.keys(d.categories || {}), ...(d.registers || []).map(r => r.category).filter(Boolean)])].map(c => [c, c])];
        const intro = this._advIntro(
            t('adv.disp.lead', 'How a unit of this kind is shown — read live from the template, so improving it reaches every existing unit. Without it the UI uses sensible defaults; declare only what you want different.'),
            `<pre class="tpl-adv-code">unit label      battery pack · plural: battery packs · icon: battery-half
row fields      soc, power, max_cell_temp        ← a unit's row in its installation
fleet fields    soc, power                        ← its row on the fleet page
headline        pack_total_power  "Battery power" (+ = charging)
alarms          alarm_count → warning · protection_count → danger
sections        cells: grid, tiles ^cell_\\d+$, 3 decimals
bitmasks        balancing_bits → "Cell {n}"</pre>`,
            'display');
        const list = (title, hint, key, proto, cells, max) => {
            const items = disp[key] || [];
            return `<div class="form-group"><label class="form-label">${title}</label>${hint ? `<div class="field-hint">${hint}</div>` : ''}
                ${items.map((it, j) => `<div class="form-row tpl-adv-line">${cells(it, P(key, j))}${this._advDelBtn(P(key, j))}</div>`).join('')}
                ${items.length < max ? `<button type="button" class="btn btn-ghost btn-sm" ${this._act('_advAdd', [P(key), proto])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('adv.add', 'Add')}</button>` : ''}</div>`;
        };
        const fieldList = (key, title, hint) => `${this._advField(title,
            `${this._advIn(P(key), 'strlist', disp[key], { ph: 'soc, power', label: key, list: 'advNameList', rerender: true })}`, hint)}`;
        return `${intro}
            <datalist id="advNameList">${this._advNames(true).map(n => `<option value="${this._esc(n)}"></option>`).join('')}</datalist>
            <div class="form-row">
                ${this._advField(t('adv.disp.unitLabel', 'A unit is a…'), this._advIn(P('unit_label'), 'str', disp.unit_label, { ph: 'battery pack', label: 'unit label' }))}
                ${this._advField(t('adv.disp.unitPlural', 'Several are…'), this._advIn(P('unit_label_plural'), 'str', disp.unit_label_plural, { ph: 'battery packs', label: 'unit label plural' }))}
                ${this._advField(t('adv.disp.icon', 'Icon'), this._advIn(P('icon'), 'str', disp.icon, { ph: 'battery-half', label: 'icon' }), t('adv.disp.iconHint', 'A Bootstrap icon name, without “bi-”.'))}
            </div>
            <div class="form-row">
                ${fieldList('glance', t('adv.disp.glance', 'Fields of a unit row'), t('adv.disp.glanceHint', 'In its installation and the device list · up to 12, comma-separated.'))}
                ${fieldList('hero', t('adv.disp.hero', 'Fields on the fleet page'), t('adv.disp.heroHint', 'The first three show · up to 12.'))}
            </div>
            ${list(t('adv.disp.headline', 'Headline of an installation'), t('adv.disp.headlineHint', 'Its top line, from its TOTALS (see Totals of an installation on each row) · up to 6.'),
                'headline', { field: '' }, (h, p) => `
                ${this._advIn([...p, 'field'], 'str', h.field, { ph: 'pack_total_power', label: 'field' })}
                ${this._advIn([...p, 'label'], 'str', h.label, { ph: t('calc.label', 'Label'), label: 'label' })}
                ${this._advIn([...p, 'hint'], 'str', h.hint, { ph: '+ = charging', label: 'hint' })}`, 6)}
            ${list(t('adv.disp.alarms', 'Alarms'), t('adv.disp.alarmsHint', 'A field that is non-zero when something is wrong; it counts on the fleet and the installation, and shows on the unit.'),
                'alarms', { field: '', severity: 'warning' }, (a, p) => `
                ${this._advSel([...p, 'field'], 'str', a.field, names, { label: 'field' })}
                ${this._advSel([...p, 'severity'], 'str', a.severity, [['warning', t('adv.disp.warning', 'warning')], ['danger', t('adv.disp.danger', 'danger')]], { label: 'severity' })}`, 50)}
            <div class="form-group"><label class="form-label">${t('adv.disp.sections', 'Sections')}</label>
                <div class="field-hint">${t('adv.disp.sectionsHint', 'A category shown as a grid of tiles (cell voltages) or only its active rows (alarms). Tiles: a pattern of field names, e.g. ^cell_\\d+$.')}</div>
                ${Object.entries(disp.sections || {}).map(([cat, s]) => `<div class="form-row tpl-adv-line">
                    <select class="input" aria-label="category" ${this._act('_advRenameKey', [P('sections'), cat], { el: true, on: 'change' })}>${cats.map(([v, l]) => `<option value="${this._esc(v)}" ${v === cat ? 'selected' : ''}>${this._esc(l)}</option>`).join('')}</select>
                    ${this._advSel(P('sections', cat, 'widget'), 'str', s.widget, [['grid', t('adv.disp.grid', 'grid of tiles')], ['active_only', t('adv.disp.activeOnly', 'only active rows')]], { label: 'widget' })}
                    ${this._advIn(P('sections', cat, 'tiles'), 'str', s.tiles, { ph: '^cell_\\d+$', label: 'tiles' })}
                    ${this._advIn(P('sections', cat, 'decimals'), 'int', s.decimals, { ph: t('calc.decimals', 'Decimals'), type: 'number', label: 'decimals', style: 'max-width:90px' })}
                    ${this._advDelBtn(P('sections', cat))}</div>`).join('')}
                <button type="button" class="btn btn-ghost btn-sm" ${this._act('_advAddKey', [P('sections'), (cats[1] || [''])[0] || 'section', { widget: 'grid' }])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('adv.add', 'Add')}</button>
            </div>
            <div class="form-group"><label class="form-label">${t('adv.disp.bitmasks', 'Bitmasks')}</label>
                <div class="field-hint">${t('adv.disp.bitmasksHint', 'A field whose bits each mean one thing (balancing cells): shown as the list of what is set. The label carries {n} for the bit number.')}</div>
                ${Object.entries(disp.bitmasks || {}).map(([f, lbl]) => `<div class="form-row tpl-adv-line">
                    <input class="input" value="${this._esc(f)}" list="advNameList" aria-label="field" ${this._act('_advRenameKey', [P('bitmasks'), f], { el: true, on: 'change' })}>
                    ${this._advIn(P('bitmasks', f), 'str', lbl, { ph: 'Cell {n}', label: 'label' })}
                    ${this._advDelBtn(P('bitmasks', f))}</div>`).join('')}
                <button type="button" class="btn btn-ghost btn-sm" ${this._act('_advAddKey', [P('bitmasks'), 'field', 'Bit {n}'])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('adv.add', 'Add')}</button>
            </div>`;
    },

    // ═══ Scan recognition (identify) ═══════════════════════════════════════
    _advIdentifyHtml() {
        const d = this._tplEdit.data;
        const idf = d.identify || {};
        const t = (k, def) => this.t(k, def);
        const P = (...k) => ['identify', ...k];
        const intro = this._advIntro(
            t('adv.idf.lead', 'How a bus scan recognises this device: registers that must read given values (a model code, a fixed marker), and/or what the device says about itself (Modbus identification, FC43). When a scan finds a slave that matches, it proposes this template.'),
            `<pre class="tpl-adv-code">register 11 (holding, uint16) is one of 731           ← Fronius 65A model code
register 40000 (holding, uint32) equals 1400204883      ← "SunS"
FC43 vendor ~ ^Eastron · product ~ SDM630</pre>
            <p class="field-hint">${t('adv.idf.exHint', 'Every register line must hold. FC43 fields are patterns (regular expressions) on the vendor and product names the device reports.')}</p>`,
            'scan-recognition-identify');
        const devs = (this._devices || []).filter(x => ['tcp', 'rtu', 'rtu-tcp'].includes(x.protocol || 'tcp'));
        return `${intro}
            <div class="form-group"><label class="form-label">${t('adv.idf.regs', 'Registers that must read')}</label>
                ${(idf.registers || []).map((r, j) => `<div class="form-row tpl-adv-line">
                    ${this._advIn(P('registers', j, 'address'), 'int', r.address, { type: 'number', ph: t('lbl.address', 'Address'), label: 'address', style: 'max-width:100px' })}
                    ${this._advSel(P('registers', j, 'register_type'), 'str', r.register_type || 'holding', [['holding', 'holding (FC3)'], ['input', 'input (FC4)']], { label: 'table' })}
                    ${this._advSel(P('registers', j, 'data_type'), 'str', r.data_type || 'uint16', ['uint16', 'int16', 'uint32', 'int32', 'float'], { label: 'type' })}
                    ${this._advIn(P('registers', j, 'equals'), 'num', r.equals, { ph: '= value', type: 'number', step: 'any', label: 'equals', style: 'max-width:110px' })}
                    ${this._advIn(P('registers', j, 'in'), 'numlist', r.in, { ph: t('adv.idf.oneOf', 'or one of: 731, 732'), label: 'in' })}
                    ${this._advIn(P('registers', j, 'min'), 'num', r.min, { ph: '≥ min', type: 'number', step: 'any', label: 'min', style: 'max-width:90px' })}
                    ${this._advIn(P('registers', j, 'max'), 'num', r.max, { ph: '≤ max', type: 'number', step: 'any', label: 'max', style: 'max-width:90px' })}
                    ${this._advDelBtn(P('registers', j))}</div>`).join('')}
                ${(idf.registers || []).length < 8 ? `<button type="button" class="btn btn-ghost btn-sm" ${this._act('_advAdd', [P('registers'), { address: 0 }])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('adv.add', 'Add')}</button>` : ''}
            </div>
            <div class="form-row">
                ${this._advField(t('adv.idf.vendor', 'FC43 vendor (pattern)'), this._advIn(P('fc43', 'vendor'), 'str', (idf.fc43 || {}).vendor, { ph: '^Eastron', label: 'fc43 vendor' }))}
                ${this._advField(t('adv.idf.product', 'FC43 product (pattern)'), this._advIn(P('fc43', 'product'), 'str', (idf.fc43 || {}).product, { ph: 'SDM630', label: 'fc43 product' }))}
            </div>
            <div class="form-row" style="align-items:end;">
                ${this._advField(t('adv.idf.try', 'Try it on a device'), `<select class="input" id="advIdfDev" aria-label="device">${devs.map(x => `<option value="${this._esc(x.id)}">${this._esc(x.name || x.id)}</option>`).join('') || `<option value="">${this._esc(t('adv.idf.noDev', 'no Modbus device yet'))}</option>`}</select>`)}
                <div class="form-group"><button type="button" class="btn btn-secondary btn-sm" id="advIdfBtn" ${this._act('_advIdentifyTest', [])} ${devs.length ? '' : 'disabled'}><i aria-hidden="true" class="bi bi-play"></i> ${t('adv.idf.test', 'Test')}</button></div>
            </div>
            <div id="advIdfResult" role="status"></div>`;
    },

    async _advIdentifyTest() {
        const box = document.getElementById('advIdfResult');
        const dev = document.getElementById('advIdfDev')?.value;
        if (!box || !dev) return;
        box.innerHTML = `<span class="btn-spinner"></span>`;
        try {
            const r = await fetch('/api/device-templates/identify-test', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ template: { device_template: this._tplEdit.data }, device: dev }) });
            const d = await r.json();
            if (!r.ok) throw new Error((d.detail?.errors || [d.detail || r.statusText]).join(' · '));
            const fc = d.fc43 && Object.keys(d.fc43).length ? ` · FC43: ${this._esc(Object.entries(d.fc43).map(([k, v]) => `${k} “${v}”`).join(', '))}` : '';
            box.innerHTML = `<div class="calc-preview ${d.ok ? 'ok' : 'err'}"><i aria-hidden="true" class="bi ${d.ok ? 'bi-check-circle' : 'bi-x-circle'}"></i> ${this._esc(d.message)}${fc}</div>`;
        } catch (e) {
            box.innerHTML = `<div class="calc-preview err">${this._esc(String(e.message || e))}</div>`;
        }
    },

    // ═══ Details & categories ══════════════════════════════════════════════
    _advDetailsHtml() {
        const d = this._tplEdit.data;
        const t = (k, def) => this.t(k, def);
        const used = {};
        (d.registers || []).forEach(r => { used[r.category] = (used[r.category] || 0) + 1; });
        const cats = Object.entries(d.categories || {}).sort((a, b) => ((a[1] || {}).order ?? 99) - ((b[1] || {}).order ?? 99));
        return `${this._advIntro(t('adv.det.lead', 'Who made this map, from what, and how its measurements are grouped. Categories decide the sections of a device page and their order.'), '', 'details-and-categories')}
            <div class="form-row">
                ${this._advField(t('adv.det.version', 'Version'), this._advIn(['version'], 'str', d.version, { ph: '1.0.0', label: 'version', style: 'max-width:110px' }), t('adv.det.versionHint', 'Raise it when you change the map: devices made from it are offered the update.'))}
                ${this._advField(t('adv.det.author', 'Author'), this._advIn(['author'], 'str', d.author, { label: 'author' }))}
                ${this._advField(t('adv.det.source', 'Source document'), this._advIn(['source_document'], 'str', d.source_document, { ph: 'EM3 Modbus manual v2.1, p. 14', label: 'source document' }))}
            </div>
            ${this._advField(t('adv.det.description', 'Description'), `<textarea class="input" rows="2" aria-label="description" ${this._act('_advSet', [['description'], 'text'], { el: true, on: 'change' })}>${this._esc(d.description || '')}</textarea>`)}
            <div class="form-group"><label class="form-label">${t('adv.det.cats', 'Categories')}</label>
                <div class="field-hint">${t('adv.det.catsHint', 'Each row of the map names one. Renaming one renames it on its rows; one still used cannot be deleted.')}</div>
                ${cats.map(([id, c]) => `<div class="form-row tpl-adv-line">
                    <input class="input" value="${this._esc(id)}" aria-label="category id" style="max-width:140px" ${this._act('_advRenameCat', [id], { el: true, on: 'change' })}>
                    ${this._advIn(['categories', id, 'label'], 'str', (c || {}).label, { ph: t('calc.label', 'Label'), label: 'category label' })}
                    ${this._advIn(['categories', id, 'order'], 'int', (c || {}).order, { ph: t('adv.det.order', 'order'), type: 'number', label: 'order', style: 'max-width:90px' })}
                    <span class="field-hint" style="min-width:70px;">${used[id] || 0} ${t('adv.det.rows', 'rows')}</span>
                    ${used[id] ? '' : this._advDelBtn(['categories', id])}</div>`).join('')}
                <button type="button" class="btn btn-ghost btn-sm" ${this._act('_advAddKey', [['categories'], 'category', { label: '', order: cats.length + 1 }])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('adv.add', 'Add')}</button>
            </div>`;
    },

    _advRenameCat(oldId, el) {
        const nk = String(el.value).trim();
        if (!nk || nk === oldId) return;
        (this._tplEdit.data.registers || []).forEach(r => { if (r.category === oldId) r.category = nk; });
        this._advRenameKey(['categories'], oldId, el);
        this._tplEditorRender();
    },

    // ═══ Raw JSON ══════════════════════════════════════════════════════════
    _advJsonHtml() {
        const t = (k, def) => this.t(k, def);
        return `${this._advIntro(t('adv.json.lead', 'The whole template as JSON, for what is quicker typed than clicked — or pasted from a colleague. Apply puts it into the editor (nothing is saved until Save); Check shows what a save would refuse.'), '', 'raw-json')}
            <textarea id="advJson" class="input tpl-adv-json" rows="18" spellcheck="false" aria-label="template JSON">${this._esc(JSON.stringify(this._tplEdit.data, null, 2))}</textarea>
            <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;">
                <button type="button" class="btn btn-secondary btn-sm" ${this._act('_advJsonCheck', [])}><i aria-hidden="true" class="bi bi-check2-square"></i> ${t('adv.json.check', 'Check')}</button>
                <button type="button" class="btn btn-primary btn-sm" ${this._act('_advJsonApply', [])}><i aria-hidden="true" class="bi bi-box-arrow-in-down"></i> ${t('adv.json.apply', 'Apply to the editor')}</button>
                <button type="button" class="btn btn-ghost btn-sm" ${this._act('_advJsonReload', [])}><i aria-hidden="true" class="bi bi-arrow-counterclockwise"></i> ${t('adv.json.reload', 'Reload from the editor')}</button>
            </div>
            <div id="advJsonResult" role="status"></div>`;
    },

    _advJsonParse() {
        const box = document.getElementById('advJsonResult');
        try {
            let o = JSON.parse(document.getElementById('advJson').value);
            if (o && o.device_template) o = o.device_template;
            if (!o || typeof o !== 'object' || !Array.isArray(o.registers)) throw new Error(this.t('adv.json.noRegs', 'a template object with a “registers” list'));
            return o;
        } catch (e) {
            if (box) box.innerHTML = `<div class="calc-preview err"><i aria-hidden="true" class="bi bi-x-circle"></i> ${this._esc(this.t('adv.json.bad', 'Not valid JSON'))}: ${this._esc(String(e.message || e))}</div>`;
            return null;
        }
    },

    async _advJsonCheck() {
        const o = this._advJsonParse();
        if (!o) return;
        const box = document.getElementById('advJsonResult');
        const r = await (await fetch('/api/device-templates/check', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device_template: o }) })).json();
        box.innerHTML = r.ok
            ? `<div class="calc-preview ok"><i aria-hidden="true" class="bi bi-check-circle"></i> ${this._esc(this.t('adv.json.ok', 'No problems.'))}</div>`
            : `<div class="calc-preview err"><ul>${r.errors.slice(0, 12).map(x => `<li>${this._esc(x)}</li>`).join('')}</ul></div>`;
    },

    _advJsonApply() {
        const o = this._advJsonParse();
        if (!o) return;
        if (!this._tplEdit.isNew) o.id = this._tplEdit.data.id;      // the id of a saved map is fixed
        this._tplEdit.data = o;
        this._tplEdit.advTab = 'json';
        this._tplEditorRender();
        this.showToast('success', this.t('adv.json.applied', 'Applied to the editor'), this.t('adv.json.appliedHint', 'Save to keep it.'));
    },

    _advJsonReload() {
        this._tplCollectMeta();
        const ta = document.getElementById('advJson');
        if (ta) ta.value = JSON.stringify(this._tplEdit.data, null, 2);
        const box = document.getElementById('advJsonResult');
        if (box) box.innerHTML = '';
    },

    // ═══ Row details (a dialog per register row) ═══════════════════════════
    tplRowExtras(i) {
        this._tplCollectMeta();
        this._tplRowIdx = i;
        const r = this._tplEdit.data.registers[i];
        document.getElementById('tplRowTitle').textContent = `${r.name || '—'} · ${r.register_type || 'holding'} ${r.address}`;
        document.getElementById('tplRowBody').innerHTML = this._advRowHtml(i);
        this.openModal('tplRowModal');
    },

    tplRowDone() {
        this.closeModal('tplRowModal');
        this._tplEditorRender();
        this._advChanged();
    },

    _advRowHtml(i) {
        const r = this._tplEdit.data.registers[i];
        const P = k => ['registers', i, k];
        const t = (k, def) => this.t(k, def);
        const names = this._advNameOptions(false).filter(([n]) => n !== r.name);
        const nanVal = r.nan === true ? 'true' : (Array.isArray(r.nan) ? r.nan.join(', ') : (r.nan ?? ''));
        return `
            <p class="tpl-adv-lead">${t('adv.row.lead', 'What this row carries beyond its address and type. Everything is optional.')}
                <a href="${this._ADV_DOC}#row-details" target="_blank" rel="noopener">${t('adv.learnMore', 'Learn more')} <i aria-hidden="true" class="bi bi-box-arrow-up-right"></i></a></p>
            <div class="form-row">
                ${this._advField(t('adv.row.description', 'Description'), this._advIn(P('description'), 'str', r.description, { label: 'description' }), t('adv.row.descriptionHint', 'Shown where the row is picked.'))}
                ${this._advField(t('adv.row.access', 'Access'), this._advSel(P('access'), 'str', r.access || 'RD', [['RD', t('adv.row.ro', 'read-only')], ['RD/WR', t('adv.row.rw', 'read / write')]], { label: 'access' }), t('adv.row.accessHint', 'Informative; writing is allowed by “Wr” on the row.'))}
            </div>
            <h4 class="tpl-adv-h">${t('adv.row.reading', 'Reading it right')}</h4>
            <div class="form-row">
                ${this._advField(t('adv.row.scaleFrom', 'Scale factor from'), this._advSel(P('scale_from'), 'str', r.scale_from, names, { label: 'scale from' }),
                    t('adv.row.scaleFromHint', 'SunSpec style: another register holds the power of ten (value × 10^SF). The fixed scale then divides after it.'))}
                ${this._advField(t('adv.row.nan', 'Means “not available”'), this._advIn(P('nan'), 'nan', nanVal, { ph: 'true · 65535 · 32768, 65535', label: 'nan' }),
                    t('adv.row.nanHint', '“true”: the type’s standard marker (0x8000, 0xFFFF…); or the raw value(s). Such a reading is missing, not data.'))}
            </div>
            <div class="form-row">
                ${this._advChk(P('monotonic'), r.monotonic, t('adv.row.monotonic', 'Counter that only grows (energy)'), 'flag')}
                ${this._advChk(P('daily'), r.daily, t('adv.row.daily', 'Day counter (resets at midnight)'), 'flag')}
            </div>
            <div class="field-hint">${t('adv.row.counterHint', 'A growing counter ignores a reading that goes backwards (a glitch would look like a reset to Home Assistant and InfluxDB). A day counter keeps the day’s maximum and adopts only the midnight reset.')}</div>
            ${r.writable ? `<div class="form-row">${this._advField(t('adv.row.allowed', 'Only these values may be written'), this._advIn(P('write_allowed'), 'numlist', r.write_allowed, { ph: '0, 1, 2', label: 'write allowed' }), t('adv.row.allowedHint', 'Comma-separated; Home Assistant offers them as a list.'))}</div>` : ''}
            <h4 class="tpl-adv-h">${t('adv.row.installation', 'In an installation')}</h4>
            ${this._advAggHtml(P('aggregates'), Object.entries(r.aggregates || {}))}
            <h4 class="tpl-adv-h">Home Assistant</h4>
            <div class="field-hint">${t('adv.row.haHint', 'Empty: guessed from the unit. Set them when the guess is wrong.')}</div>
            <div class="form-row">
                ${this._advField('device_class', this._advIn(P('device_class'), 'str', r.device_class, { ph: 'power · energy · voltage', label: 'device class', list: 'advHaClasses' }))}
                ${this._advField('state_class', this._advSel(P('state_class'), 'str', r.state_class, [['', '—'], 'measurement', 'total', 'total_increasing', 'none'], { label: 'state class' }))}
                ${this._advField('entity_category', this._advSel(P('entity_category'), 'str', r.entity_category, [['', '—'], 'diagnostic', 'config', 'none'], { label: 'entity category' }))}
            </div>
            <div class="form-row">
                ${this._advField(t('adv.row.haShown', 'Shown by default'), this._advSel(P('enabled_by_default'), 'tri', r.enabled_by_default == null ? '' : String(r.enabled_by_default), [['', '—'], ['true', t('common.yes', 'yes')], ['false', t('common.no', 'no')]], { label: 'enabled by default' }))}
                ${this._advField(t('adv.row.icon', 'Icon'), this._advIn(P('icon'), 'str', r.icon, { ph: 'mdi:flash', label: 'icon' }))}
                ${this._advField(t('adv.row.precision', 'Decimals in HA'), this._advIn(P('suggested_display_precision'), 'int', r.suggested_display_precision, { type: 'number', label: 'precision', style: 'max-width:90px' }))}
            </div>
            <datalist id="advHaClasses">${['power', 'energy', 'voltage', 'current', 'frequency', 'temperature', 'battery', 'power_factor', 'reactive_power', 'apparent_power', 'duration', 'enum']
                .map(c => `<option value="${c}"></option>`).join('')}</datalist>`;
    },
});
