/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
/* Sequences — on the Rules page, under the rules (rules phase 3 §4).
 *
 * One card per sequence: its steps in words, Run (asks for the parameters),
 * Dry run (the plan, nothing written), Cancel while it runs, the last run and
 * why it ended, Edit (YAML) and Delete.
 */
Object.assign(JanitzaMonitor.prototype, {

    async _loadSequences(quiet = false) {
        const el = document.getElementById('sequencesContent');
        if (!el) return;
        const t = (k, d) => this.t(k, d);
        let items;
        try { items = (await (await fetch('/api/sequences')).json()).sequences || []; }
        catch (e) { if (!quiet) el.innerHTML = `<p style="color:var(--danger,#c0392b);">${this._esc(e.message)}</p>`; return; }
        this._sequences = items;
        if (!items.length) {
            el.innerHTML = this._emptyState({ icon: 'bi-list-ol', title: t('seq.noneTitle', 'No sequences yet'),
                body: t('seq.noneBody', 'A sequence runs commands in order — open a valve, wait until it is open, start the pump — and undoes them if a step fails.') });
            return;
        }
        const plan = this._seqPlan || {};
        el.innerHTML = items.map(s => this._seqCardHtml(s, plan[s.id])).join('');
    },

    _seqStepText(st) {
        const t = (k, d) => this.t(k, d);
        if (st.command) {
            const p = Object.entries(st.command.params || {}).map(([k, v]) => `${k}=${v}`).join(', ');
            return `${st.command.device} · ${st.command.name}${p ? ` (${p})` : ''}`;
        }
        if (st.wait_s != null) return `${t('seq.wait', 'wait')} ${st.wait_s} s`;
        if (st.wait_until) return `${t('seq.waitUntil', 'wait until')} ${st.wait_until} (≤ ${st.timeout_s} s)`;
        if (st.check) return `${t('seq.check', 'check')} ${st.check}${st.else === 'continue' ? ` — ${t('seq.orContinue', 'else continue')}` : ''}`;
        return JSON.stringify(st);
    },

    _seqStatus(run) {
        const t = (k, d) => this.t(k, d);
        const cls = { done: 'ok', running: 'warn', undoing: 'warn', cancelled: 'off', aborted: 'bad', dry_run: 'off' }[run.status] || 'off';
        const label = t('seq.status_' + run.status, run.status);
        return `<span class="sink-pill ${cls}">${this._esc(label)}</span>`;
    },

    _seqCardHtml(s, plan) {
        const t = (k, d) => this.t(k, d);
        const live = s.running, last = (s.last || [])[0];
        const steps = (s.steps || []).map((st, i) => `<li ${live && live.step === i ? 'style="font-weight:600;"' : ''}>${this._esc(this._seqStepText(st))}</li>`).join('');
        const undo = (s.on_abort || []).map(st => this._esc(this._seqStepText(st))).join(' · ');
        const runLine = live ? `${this._seqStatus(live)} ${t('seq.step', 'step')} ${live.step + 1}/${(s.steps || []).length} · ${this._esc(live.by || '')}`
            : last ? `${this._seqStatus(last)} ${new Date(last.ended * 1000).toLocaleString(this._loc())} · ${this._esc(last.by || '')}${last.reason ? ` — ${this._esc(last.reason)}` : ''}`
            : `<span class="field-hint">${t('seq.never', 'Not run yet.')}</span>`;
        return `
        <div class="settings-card" data-sequence="${this._esc(s.id)}">
            <div class="settings-card-header" style="flex-wrap:wrap;gap:8px;">
                <h3 style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">${this._esc(s.label || s.id)} <span class="dev-chip">${this._esc(s.id)}</span></h3>
                <div class="header-actions" style="display:flex;gap:6px;flex-wrap:wrap;">
                    ${live ? `<button data-live class="btn btn-sm btn-secondary" ${this._act('cancelSequence', [s.id])}><i aria-hidden="true" class="bi bi-stop-circle"></i> ${t('seq.cancel', 'Cancel')}</button>`
                           : `<button data-live class="btn btn-sm btn-primary" ${this._act('runSequence', [s.id])}><i aria-hidden="true" class="bi bi-play-fill"></i> ${t('seq.run', 'Run…')}</button>`}
                    <button data-live class="btn btn-ghost btn-sm" ${this._act('dryRunSequence', [s.id])}><i aria-hidden="true" class="bi bi-eye"></i> ${t('seq.dryRun', 'Dry run')}</button>
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('openSequenceModal', [s.id])} title="${t('common.edit', 'Edit')}" aria-label="${t('common.edit', 'Edit')}"><i aria-hidden="true" class="bi bi-pencil"></i></button>
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('deleteSequence', [s.id])} title="${t('common.delete', 'Delete')}" aria-label="${t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-trash"></i></button>
                </div>
            </div>
            <div class="settings-card-body">
                <ol style="margin:0 0 8px 18px;padding:0;font-size:13px;">${steps}</ol>
                ${undo ? `<div class="field-hint" style="margin-bottom:8px;">${t('seq.undo', 'If it fails or is cancelled')}: ${undo}</div>` : ''}
                <div style="font-size:12.5px;"><b>${t('seq.lastRun', 'Last run')}</b>: ${runLine}</div>
                ${plan ? `<div class="field-hint" style="margin-top:8px;" data-seq-plan>${t('seq.plan', 'Dry run')}: ${plan.status !== 'dry_run' ? `${this._seqStatus(plan)} ` : ''}${(plan.steps || []).map(x => `${x.ok ? '✓' : '✗'} ${this._esc(x.detail)}`).join(' · ')}</div>` : ''}
            </div>
        </div>`;
    },

    // a sequence's parameters as dialog fields; empty = its default
    _seqFields(s) {
        return Object.entries(s.params || {}).map(([name, p]) => ({
            name, label: p.label || name, kind: 'number', min: p.min, max: p.max, unit: p.unit,
            initial: p.default ?? '', required: false,
        }));
    },

    _seqParamValues(v) {
        return Object.fromEntries(Object.entries(v || {}).filter(([, x]) => x !== null));
    },

    // one dialog: its parameters (if any) and what it writes to; the run is
    // sent from the dialog, so a refusal is said there
    async runSequence(id) {
        const t = (k, d, p) => this.t(k, d, p);
        const s = (this._sequences || []).find(x => x.id === id);
        if (!s) return;
        const fields = this._seqFields(s);
        const run = async params => {
            try { await this._ruleOp(`/api/sequences/${encodeURIComponent(id)}/run`, { params }); return null; }
            catch (e) { return e.message; }
        };
        const ask = {
            title: t('seq.runTitle', 'Run {name}', { name: s.label || id }),
            message: t('seq.runConfirm', 'Its commands write to {devices}.', { devices: (s.devices || []).join(', ') || '—' }),
            confirmLabel: t('seq.runGo', 'Run'),
        };
        if (fields.length) await this.askValues({ ...ask, fields, submit: v => run(this._seqParamValues(v)) });
        else await this.confirmAction({ ...ask, submit: () => run({}) });
        this._loadSequences();
    },

    async dryRunSequence(id) {
        const t = (k, d, p) => this.t(k, d, p);
        const s = (this._sequences || []).find(x => x.id === id);
        if (!s) return;
        const fields = this._seqFields(s);
        let params = {};
        if (fields.length) {
            const v = await this.askValues({
                title: t('seq.dryRunTitle', 'Dry run {name}', { name: s.label || id }),
                message: t('seq.dryRunHint', 'Nothing is written: each step is checked and listed.'),
                fields, confirmLabel: t('seq.dryRun', 'Dry run'),
            });
            if (v === null) return;
            params = this._seqParamValues(v);
        }
        try {
            const plan = await this._ruleOp(`/api/sequences/${encodeURIComponent(id)}/run`, { params, dry_run: true });
            this._seqPlan = { ...(this._seqPlan || {}), [id]: plan };
        } catch (e) { this._opError(t('seq.dryRun', 'Dry run'), e); }
        this._loadSequences();
    },

    async cancelSequence(id) {
        try { await this._ruleOp(`/api/sequences/${encodeURIComponent(id)}/cancel`, {}); }
        catch (e) { this._opError(this.t('seq.cancelTitle', 'Cancel sequence'), e); }
        this._loadSequences();
    },

    async deleteSequence(id) {
        const t = (k, d, p) => this.t(k, d, p);
        if (!await this.confirmAction({
            title: t('seq.deleteTitle', 'Delete sequence {name}', { name: id }),
            confirmLabel: t('common.delete', 'Delete'), danger: true,
            submit: async () => {
                try { await this._ruleOp(`/api/sequences/${encodeURIComponent(id)}`, null, 'DELETE'); return null; }
                catch (e) { return e.message; }
            },
        })) return;
        this._loadSequences();
    },

    async openSequenceModal(id) {
        const t = (k, d) => this.t(k, d);
        this._seqEdit = id || '';
        let text = `id: start-pump
label: Open the valve, then start the pump
params:
  speed: { min: 15, max: 50, default: 30, unit: Hz }
steps:
  - command: { device: valve1, name: open }
  - wait_until: 'valve1.position >= 95'
    timeout_s: 30
  - command: { device: vfd1, name: run_at, params: { value: '\${speed}' } }
  - wait_s: 10
  - check: 'vfd1.output_current > 0.5'
on_abort:
  - command: { device: vfd1, name: run_at, params: { value: 0 } }
  - command: { device: valve1, name: close }
`;
        if (id) {
            try { text = (await (await fetch(`/api/sequences/${encodeURIComponent(id)}/yaml`)).json()).yaml; }
            catch (e) { this._opError(t('seq.edit', 'Edit sequence'), e); return; }
        }
        document.getElementById('seqModalTitle').textContent = id ? `${t('seq.edit', 'Edit sequence')} · ${id}` : t('seq.add', 'Add sequence');
        document.getElementById('seqText').value = text;
        document.getElementById('seqFeedback').textContent = '';
        this.openModal('seqModal');
    },

    async saveSequence() {
        const fb = document.getElementById('seqFeedback');
        const body = { yaml: document.getElementById('seqText').value };
        try {
            if (this._seqEdit) await this._ruleOp(`/api/sequences/${encodeURIComponent(this._seqEdit)}`, body, 'PUT');
            else await this._ruleOp('/api/sequences', body);
        } catch (e) { fb.textContent = e.message; return; }
        this.closeModal('seqModal');
        this._loadSequences();
    },
});
