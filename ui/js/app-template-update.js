// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
//
// template updates — augments JanitzaMonitor.prototype
//
// A unit's register map is a COPY of its template taken when the unit was
// made. When the template improves (a clearer label, a new calculated field)
// the copies are told so here: the page shows a notice only while there is
// something to bring in, and the modal lists exactly what would change —
// computed by the server with the same rule the apply uses — before anything
// is written. Nothing to apply → nothing to press.

Object.assign(JanitzaMonitor.prototype, {

    // the notice on an installation / device page; '' when up to date
    _tplUpdateNotice(kind, id, summary) {
        const s = summary || {};
        const miss = s.missing_templates || [];
        if (miss.length) {
            // the map the units were made from is gone: they keep working
            // from their own copy, but nothing can update them
            return `
            <div class="tpl-update-notice tpl-missing" role="status">
                <i aria-hidden="true" class="bi bi-exclamation-triangle"></i>
                <div class="tpl-update-text">
                    <b>${this._esc(this.t('tplu.missingTitle', 'Template not available'))}</b>
                    <span>${this._esc(this.t('tplu.missingText', '{tpl} is not in the library (deleted, renamed, or it failed to load). The units keep reading with their own copy, but cannot be updated from it. Restore or re-import the template on the Templates page.', { tpl: miss.join(', ') }))}</span>
                </div>
            </div>`;
        }
        if (!s.pending) return '';
        const t = (k, d, p) => this.t(k, d, p);
        const parts = [];
        if (s.new) parts.push(t('tplu.nNew', '{n} new field(s)', { n: s.new }));
        if (s.changes) parts.push(t('tplu.nChanges', '{n} change(s)', { n: s.changes }));
        return `
        <div class="tpl-update-notice" role="status">
            <i aria-hidden="true" class="bi bi-stars"></i>
            <div class="tpl-update-text">
                <b>${this._esc(t('tplu.noticeTitle', 'The template has been updated'))}</b>
                <span>${this._esc(parts.join(' · '))}${s.units_affected ? ' · ' + this._esc(t('tplu.nUnits', '{n} unit(s) affected', { n: s.units_affected })) : ''}</span>
                <span class="tpl-update-viewer">${this._esc(t('tplu.adminApplies', 'An admin can review and apply it.'))}</span>
            </div>
            <button data-admin class="btn btn-primary btn-sm" ${this._act('openTemplateUpdate', [kind, id])}>
                <i aria-hidden="true" class="bi bi-eye"></i> ${this._esc(t('tplu.review', 'Review and apply'))}</button>
        </div>`;
    },

    async openTemplateUpdate(kind, id) {
        const t = (k, d, p) => this.t(k, d, p);
        const base = kind === 'endpoint' ? '/api/endpoints/' : '/api/devices/';
        this._tplUpdate = { kind, id, base };
        const body = document.getElementById('tplUpdateBody');
        const fb = document.getElementById('tplUpdateFeedback');
        const apply = document.getElementById('tplUpdateApply');
        if (fb) fb.textContent = '';
        body.innerHTML = `<p class="field-hint">${this._esc(t('common.loading', 'Loading…'))}</p>`;
        this.openModal('tplUpdateModal');
        let plan;
        try {
            const r = await fetch(`${base}${encodeURIComponent(id)}/template-changes`);
            plan = await r.json();
            if (!r.ok) throw new Error(plan.detail || r.statusText);
        } catch (e) {
            body.innerHTML = `<p class="field-hint">${this._esc(String(e.message || e))}</p>`;
            if (apply) apply.disabled = true;
            return;
        }
        this._tplUpdate.plan = plan;
        if (apply) apply.disabled = !plan.pending;
        body.innerHTML = this._tplUpdateHtml(plan, kind);
    },

    _tplUpdateHtml(plan, kind) {
        const t = (k, d, p) => this.t(k, d, p);
        const esc = v => this._esc(v);
        const tpl = (plan.templates || []).map(x => `${esc(x.name)}${x.version ? ' <span class="dev-chip">' + esc(x.version) + '</span>' : ''}`).join(', ');
        if (!plan.pending) {
            return `<p><i aria-hidden="true" class="bi bi-check-circle"></i> ${esc(t('tplu.upToDate', 'Up to date with its template — nothing to apply.'))}</p>`;
        }
        const whole = kind === 'endpoint'
            ? t('tplu.introBank', 'The template {tpl} has changes for this installation. They are applied to every unit, so all units stay alike.', { tpl: '§' })
            : t('tplu.introDevice', 'The template {tpl} has changes for this device.', { tpl: '§' });
        const KEY = {
            label: t('tplu.k.label', 'Name'), unit: t('tplu.k.unit', 'Unit'),
            description: t('tplu.k.description', 'Description'), category: t('tplu.k.category', 'Section'),
            aggregates: t('tplu.k.aggregates', 'Installation totals'),
        };
        const show = v => {
            if (v == null || v === '') return `<span class="tplu-none">${esc(t('tplu.none', 'none'))}</span>`;
            if (typeof v === 'object') return esc(Object.entries(v).map(([k, o]) => `${k}: ${o}`).join(', '));
            const s = String(v);
            return esc(s.length > 90 ? s.slice(0, 87) + '…' : s);
        };
        const units = n => plan.units_total > 1
            ? ` <span class="tplu-units">${esc(t('tplu.onUnits', '{n}/{m} units', { n, m: plan.units_total }))}</span>` : '';
        let html = `<p style="margin-top:0;">${esc(whole).replace('§', tpl)}</p>`;
        if ((plan.new || []).length) {
            html += `<h4 class="tplu-h">${esc(t('tplu.newTitle', 'New calculated fields'))}</h4>
            <label class="tplu-check"><input type="checkbox" id="tplUpdateAddNew" checked>
                ${esc(t('tplu.addNew', 'Add them'))}</label>
            <ul class="tplu-list">${plan.new.map(n => `
                <li><b>${esc(n.label)}</b> <code>${esc(n.field)}</code>${units(n.units)}
                ${n.description ? `<div class="field-hint">${esc(n.description)}</div>` : ''}</li>`).join('')}</ul>`;
        }
        if ((plan.changes || []).length) {
            html += `<h4 class="tplu-h">${esc(t('tplu.changesTitle', 'Changes to existing fields'))}</h4>
            <table class="tplu-table"><thead><tr>
                <th>${esc(t('tplu.field', 'Field'))}</th><th>${esc(t('tplu.what', 'What'))}</th>
                <th>${esc(t('tplu.now', 'Now'))}</th><th>${esc(t('tplu.becomes', 'Becomes'))}</th></tr></thead><tbody>
            ${plan.changes.map(c => `<tr>
                <td><code>${esc(c.field)}</code>${units(c.units)}</td>
                <td>${esc(KEY[c.key] || c.key)}</td>
                <td class="tplu-before">${show(c.before)}</td>
                <td class="tplu-after">${show(c.after)}</td></tr>`).join('')}
            </tbody></table>`;
        }
        html += `<p class="field-hint tplu-kept"><i aria-hidden="true" class="bi bi-shield-check"></i>
            ${esc(t('tplu.kept', 'Kept as you set them: which fields are read, dashboard choices, MQTT/InfluxDB outputs, thresholds and every formula. The units restart for a few seconds.'))}</p>`;
        return html;
    },

    async applyTemplateUpdate() {
        const st = this._tplUpdate;
        if (!st || !st.plan || !st.plan.pending) return;
        const t = (k, d, p) => this.t(k, d, p);
        const btn = document.getElementById('tplUpdateApply');
        const fb = document.getElementById('tplUpdateFeedback');
        const addNew = !!document.getElementById('tplUpdateAddNew')?.checked;
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="btn-spinner"></span> …'; }
        try {
            const r = await fetch(`${st.base}${encodeURIComponent(st.id)}/refresh-from-template${addNew ? '?add_new=true' : ''}`, { method: 'POST' });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) {
                const det = d.detail;
                const msgs = Array.isArray(det) ? det.map(x => x?.msg || String(x))
                    : det?.errors || [typeof det === 'string' ? det : r.statusText];
                throw new Error(msgs.join(' · '));
            }
            const list = d.units || [d];
            const sum = k => list.reduce((a, u) => a + (u[k] || 0), 0);
            this.closeModal('tplUpdateModal');
            this.showToast('success', t('tplu.done', 'Template update applied'),
                t('tplu.doneDetail', '{c} field(s) updated, {a} added, on {u} unit(s)',
                  { c: sum('registers') + sum('calculated'), a: sum('added'), u: list.length }));
            // refresh wherever the review was opened from
            if (this.currentPage === 'templates') this.renderTemplateManager?.();
            else if (st.kind === 'endpoint') this._refreshEndpointDetail?.(st.id);
            else this.openDeviceDetail?.(st.id);
        } catch (e) {
            if (fb) fb.textContent = String(e.message || e);
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = orig; }
        }
    },
});
