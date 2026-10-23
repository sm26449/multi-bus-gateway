/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
/* Rules — the declarative controller's page (docs/rules-design.md §6).
 *
 * One card per rule: state in words, the signal, want → actual per unit, the
 * last decision and its reason, Arm / Shadow, Clamp, Override, Edit, Delete,
 * and the decision log. The editor builds a rule from its parts with a live
 * preview of what it would decide right now. A new rule is always shadow.
 */
Object.assign(JanitzaMonitor.prototype, {

    _stopRulesPolls() {
        if (this._rulesTimer) { clearInterval(this._rulesTimer); this._rulesTimer = null; }
    },

    async renderRulesPage() {
        const el = document.getElementById('rulesContent');
        if (!el) return;
        this._stopRulesPolls();
        for (const [id, fn] of [['rulesAddBtn', () => this.openRuleModal('')], ['rulesRefreshBtn', () => this.renderRulesPage()],
                                ['rulesImportBtn', () => this.openRulesImport()]]) {
            const b = document.getElementById(id);
            if (b && !b._wired) { b._wired = true; b.addEventListener('click', fn); }
        }
        const sa = document.getElementById('seqAddBtn');
        if (sa && !sa._wired) { sa._wired = true; sa.addEventListener('click', () => this.openSequenceModal('')); }
        await this._loadRules();
        if (this._loadSequences) await this._loadSequences();
        this._rulesTimer = setInterval(() => { this._loadRules(true); if (this._loadSequences) this._loadSequences(true); }, 5000);
    },

    async _loadRules(quiet = false) {
        const el = document.getElementById('rulesContent');
        if (!el) return;
        const t = (k, d) => this.t(k, d);
        let rules;
        try { rules = (await (await fetch('/api/rules')).json()).rules || []; }
        catch (e) { if (!quiet) el.innerHTML = `<p style="color:var(--danger,#c0392b);">${this._esc(e.message)}</p>`; return; }
        this._rules = rules;
        const open = new Set([...el.querySelectorAll('[data-rule-decisions]:not([hidden])')].map(x => x.dataset.ruleDecisions));
        if (!rules.length) {
            el.innerHTML = this._emptyState({ icon: 'bi-sliders2', title: t('rules.noneTitle', 'No rules yet'),
                body: t('rules.noneBody', 'A rule watches live values and asks a device for what it wants — an over-voltage limit, a restore when the grid is gone. A new rule starts in shadow: it decides and says so, but writes nothing until you arm it.') });
            return;
        }
        el.innerHTML = rules.map(r => this._ruleCardHtml(r, open.has(r.id))).join('');
        for (const id of open) this._loadRuleDecisions(id);
        // a rule opened from the Status page keeps its highlight across a redraw
        if (this._applyPendingFocus) this._applyPendingFocus();
    },

    _ruleStateDot(r) {
        const st = r.live?.state;
        if (st === 'stale') return 'var(--text-secondary,#888)';
        if (st === 'normal' || st === 'false' || st === 'off' || st === 'holding') return 'var(--success,#22c55e)';
        if (r.kind === 'steps') {
            const i = (r.steps || []).findIndex(s => s.label === st);
            return i >= (r.steps || []).length - 1 ? 'var(--danger,#ef4444)' : 'var(--warning,#f59e0b)';
        }
        return 'var(--warning,#f59e0b)';
    },

    // a rule's signal: a number, a yes/no, or — a condition with a release —
    // the pair [when, release]
    _tipWant() {
        return this.t('rules.wantTip', 'What the rule wants the device to hold. In shadow, what it would send.');
    },

    _tipActual() {
        return this.t('rules.actualTip', 'What the device says it holds, read back from it; in brackets, how long ago that reading was taken.');
    },

    _fmtSig(sig) {
        if (Array.isArray(sig)) {
            const yn = x => x ? this.t('rules.yes', 'yes') : this.t('rules.no', 'no');
            return `${this.t('rules.when', 'When')} ${yn(sig[0])} · ${this.t('rules.untilShort', 'until')} ${yn(sig[1])}`;
        }
        return typeof sig === 'number' ? sig.toFixed(1) : String(sig);
    },

    // days as people read them on the card (translated), or as the field
    // takes them back (`raw`: Mon-Fri, `all`) whatever the page language
    _daysText(days, raw = false) {
        const names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map(d => raw ? d : this.t(`rules.day${d}`, d));
        if ((days || []).length === 7) return raw ? 'all' : this.t('rules.everyDay', 'every day');
        const runs = []; let start = null;
        days.forEach((d, i) => {
            if (start === null) start = d;
            if (i + 1 === days.length || days[i + 1] !== d + 1) { runs.push(start === d ? names[d - 1] : `${names[start - 1]}-${names[d - 1]}`); start = null; }
        });
        return runs.join(',');
    },

    _scheduleLine(r) {
        return (r.schedule || []).map(e => `${e.label ? e.label + ': ' : ''}${this._daysText(e.days)} ${e.at} → ${e.params ? this._fmtWant(e.params) : e.value}`).join(' · ')
            + (r.when ? ` · ${this.t('rules.onlyWhen', 'only when')} ${r.when}` : '');
    },

    _trimLine(r) {
        const t = (k, d) => this.t(k, d);
        const by = r.gain != null ? `${t('rules.trimGainShort', 'gain')} ${r.gain} (≤ ${r.step})` : `± ${r.step}`;
        return `${t('rules.trimAbove', 'Raise while over')} ${r.above} · ${t('rules.trimBelow', 'Lower while under')} ${r.below} · ${by} · ${r.min}…${r.max}`
            + (r.off_value != null ? ` · ${t('rules.trimOffShort', 'off')} ${r.off_value}` : '');
    },

    _fmtMinutes(m) {
        return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
    },

    _fmtWant(w) {
        if (!w) return '—';
        return Object.entries(w).map(([k, v]) => `${k} ${v}`).join(', ');
    },

    _ruleTargetText(r) {
        const tg = r.target || {};
        return tg.device ? `${tg.device} · ${tg.command}` : `${tg.endpoint} / ${tg.group} · ${tg.command}`;
    },

    // what a rule's formulas read that this gateway does not have (the
    // server's `inputs`), one problem in words
    _ruleInputText(p) {
        const t = (k, d, x) => this.t(k, d, x);
        const ref = p.ref || '';
        const dot = ref.indexOf('.');
        const dev = dot < 0 ? ref : ref.slice(0, dot), field = dot < 0 ? '' : ref.slice(dot + 1);
        if (p.problem === 'no_device') return t('rules.in.noDevice', '{ref}: there is no device or installation {dev}', { ref, dev });
        if (p.problem === 'nothing_read') return t('rules.in.nothingRead', '{ref}: {dev} reads no measurement yet', { ref, dev });
        return t('rules.in.noField', '{ref}: {dev} has no field {field}', { ref, dev, field })
            + (p.hint ? ' — ' + t('rules.in.didYouMean', 'did you mean {hint}?', { hint: `${dev}.${p.hint}` }) : '');
    },

    // red when an armed rule reads a device that is not here (it can only
    // ever be stale); amber for a field or a device that reads nothing yet
    _ruleInputsSev(r) {
        const ins = r.inputs || [];
        if (!ins.length) return '';
        return r.mode === 'armed' && r.enabled !== false && ins.some(p => p.problem === 'no_device') ? 'bad' : 'warn';
    },

    _ruleInputsHtml(inputs, sev) {
        if (!inputs || !inputs.length) return '';
        const color = sev === 'bad' ? 'var(--danger,#c0392b)' : 'var(--warning,#b7791f)';
        return `<ul data-rule-inputs-list style="margin:0 0 8px;padding-left:18px;color:${color};font-size:12.5px;">
            ${inputs.map(p => `<li>${this._esc(this._ruleInputText(p))}</li>`).join('')}</ul>`;
    },

    _ruleCardHtml(r, decisionsOpen) {
        const t = (k, d) => this.t(k, d);
        const lv = r.live || {};
        const armed = r.mode === 'armed';
        const units = Object.entries(lv.units || {});
        const last = lv.last;
        const tm = r.timing || {};
        const inSev = this._ruleInputsSev(r);
        // on/off is known when the rule has an off value: `off`, or a condition's `else`
        const counting = r.off_value != null || (r.kind === 'condition' && r.else) || ['min_on_s', 'min_off_s', 'max_on_s', 'max_starts_per_h'].some(k => tm[k]);
        const today = u => `${this._fmtMinutes(u.run_today_min || 0)} · ${u.starts_today || 0}×`;
        const unitsHtml = units.map(([dev, u]) => {
            const paused = u.paused_until && u.paused_until * 1000 > Date.now();
            return `<tr>
                <td><code>${this._esc(dev)}</code></td>
                <td>${this._esc(u.state || '')}</td>
                <td>${this._esc(this._fmtWant(u.want))}</td>
                ${counting ? `<td title="${t('rules.todayHint', 'Run time and starts today (local time); in shadow, what it would have done')}">${this._esc(today(u))}</td>` : ''}
                <td>${u.actual != null ? `${u.actual}${u.actual_age_s != null ? ` <span class="field-hint">(${this.t('rules.readAgo', 'read {s} s ago', { s: Math.round(u.actual_age_s) })})</span>` : ''}` : '—'}</td>
                <td>${u.clamp ? `≤ ${u.clamp.max}${u.clamp.expires_at ? ` ${t('rules.until', 'until')} ${new Date(u.clamp.expires_at * 1000).toLocaleTimeString(this._loc())}` : ''}` : ''}
                    ${paused ? `<span class="sink-pill warn">${t('rules.paused', 'paused')} ${t('rules.until', 'until')} ${new Date(u.paused_until * 1000).toLocaleTimeString(this._loc())}</span>` : ''}
                    ${u.failures ? `<span class="sink-pill bad">${u.failures} ${t('rules.failures', 'failures')}</span>` : ''}
                    ${u.yielding_to ? `<span class="sink-pill off" title="${t('rules.yieldsHint', 'Another rule drives this target now; this one writes nothing until it wins again')}">${t('rules.yieldsTo', 'yields to')} ${this._esc(u.yielding_to)}</span>` : ''}
                    ${u.forced && u.forced.until * 1000 > Date.now() ? `<span class="sink-pill warn">${t('rules.forcedPill', 'forced')} ${this._esc(this._fmtWant(u.forced.params))} ${t('rules.until', 'until')} ${new Date(u.forced.until * 1000).toLocaleTimeString(this._loc())}</span>` : ''}</td></tr>`;
        }).join('');
        return `
        <div class="settings-card" data-rule="${this._esc(r.id)}">
            <div class="settings-card-header" style="flex-wrap:wrap;gap:8px;">
                <h3 style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                    <span class="status-dot" style="--dot:${this._ruleStateDot(r)}" aria-hidden="true"></span>
                    ${this._esc(r.label || r.id)} <span class="dev-chip">${this._esc(r.id)}</span>
                    <span class="sink-pill ${armed ? 'ok' : 'warn'}">${armed ? t('rules.armed', 'Armed') : t('rules.shadow', 'Shadow')}</span>
                    ${r.layer || r.priority != null ? `<span class="dev-chip" title="${t('rules.layerHint', 'Several rules may drive one target: the higher priority wins it')}">${this._esc(t('rules.layer_' + (r.layer || 'schedule'), r.layer || 'schedule'))}${r.priority != null ? ` · ${r.priority}` : ''}</span>` : ''}
                    ${r.enabled ? '' : `<span class="sink-pill off">${t('devices.disabled', 'disabled')}</span>`}
                    ${lv.error ? `<span class="sink-pill bad" title="${this._esc(lv.error)}">${t('rules.cannotRun', 'cannot run')}</span>` : ''}
                    ${inSev ? `<span class="sink-pill ${inSev}" data-rule-inputs title="${this._esc((r.inputs || []).map(p => this._ruleInputText(p)).join('\n'))}">${t('rules.inputsUnknown', 'inputs unknown')}</span>` : ''}
                </h3>
                <div class="header-actions" style="display:flex;gap:6px;flex-wrap:wrap;">
                    <button data-admin class="btn btn-sm ${armed ? 'btn-secondary' : 'btn-primary'}" ${this._act('setRuleMode', [r.id, armed ? 'shadow' : 'armed'])} ${lv.error && !armed ? 'disabled' : ''}>
                        <i aria-hidden="true" class="bi ${armed ? 'bi-eye' : 'bi-shield-check'}"></i> ${armed ? t('rules.toShadow', 'To shadow') : t('rules.arm', 'Arm…')}</button>
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('setRuleEnabled', [r.id, !r.enabled])} title="${r.enabled ? t('rules.disableHint', 'Stop the rule: it decides nothing until enabled again') : t('rules.enableHint', 'Let the rule decide again')}"><i aria-hidden="true" class="bi ${r.enabled ? 'bi-toggle-on' : 'bi-toggle-off'}"></i> ${r.enabled ? t('rules.disable', 'Disable') : t('rules.enable', 'Enable')}</button>
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('openRuleClamp', [r.id])} title="${t('rules.clampHint', 'A ceiling on what the rule may ask for, for a stated time')}"><i aria-hidden="true" class="bi bi-arrow-down-square"></i> ${t('rules.clamp', 'Clamp…')}</button>
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('openRuleOverride', [r.id])} title="${t('rules.overrideHint', 'Pause the rule so you or another system can command its target')}"><i aria-hidden="true" class="bi bi-pause-circle"></i> ${t('rules.override', 'Pause…')}</button>
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('openRuleForce', [r.id])} title="${t('rules.forceHint', 'Ask for a value for a while (run for an hour), then the rule decides again')}"><i aria-hidden="true" class="bi bi-lightning-charge"></i> ${t('rules.force', 'Force…')}</button>
                    <button class="btn btn-ghost btn-sm" ${this._act('toggleRuleDecisions', [r.id])}><i aria-hidden="true" class="bi bi-journal-text"></i> ${t('rules.decisions', 'Decisions')}</button>
                    <a class="btn btn-ghost btn-sm" href="/api/rules/export?ids=${encodeURIComponent(r.id)}" download title="${t('rules.exportOne', 'Export this rule (YAML)')}" aria-label="${t('rules.exportOne', 'Export this rule (YAML)')}"><i aria-hidden="true" class="bi bi-download"></i></a>
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('openRuleModal', [r.id])} title="${t('common.edit', 'Edit')}" aria-label="${t('common.edit', 'Edit')}"><i aria-hidden="true" class="bi bi-pencil"></i></button>
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('deleteRule', [r.id])} title="${t('common.delete', 'Delete')}" aria-label="${t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-trash"></i></button>
                </div>
            </div>
            <div class="settings-card-body">
                ${lv.error ? `<p style="margin:0 0 8px;color:var(--danger,#c0392b);">${this._esc(lv.error)}</p>` : ''}
                ${this._ruleInputsHtml(r.inputs, inSev)}
                <div style="display:flex;gap:18px;flex-wrap:wrap;font-size:13px;margin-bottom:8px;">
                    <span><b>${t('rules.target', 'Target')}</b>: ${this._esc(this._ruleTargetText(r))}</span>
                    <span><b>${t('rules.state', 'State')}</b>: ${this._esc(lv.state || '—')}</span>
                    <span title="${t('rules.signalTip', 'The rule\'s expression, worked out now from the live values. Steps compare it with their thresholds; a trim lowers while it is under the low threshold, raises while it is over the high one, and holds between.')}"><b>${t('rules.signal', 'Signal')}</b>: ${lv.signal == null ? '—' : this._esc(this._fmtSig(lv.signal))}${r.signal_unit && lv.signal != null ? ` ${this._esc(r.signal_unit)}` : ''}
                        <span class="field-hint">${this._esc(r.kind === 'condition' || r.kind === 'schedule' ? (r.when || '') : r.signal)}</span></span>
                </div>
                ${r.kind === 'schedule' ? `<div class="field-hint" style="margin-bottom:8px;">${this._esc(this._scheduleLine(r))}</div>`
                 : r.kind === 'trim' ? `<div class="field-hint" style="margin-bottom:8px;">${this._esc(this._trimLine(r))}</div>`
                 : r.kind === 'steps' ? `<div class="field-hint" style="margin-bottom:8px;">${t('rules.stepsLine', 'Steps')}: ${this._esc((r.steps || []).map(s => `${s.label} ≥ ${s.at} → ${s.value}${s.fast ? ' ⚡' : ''}`).join(' · '))} · ${t('rules.releaseLine', 'normal under')} ${this._esc(r.release_below)} → ${this._esc(this._fmtWant(r.normal))}${this._ruleTuningLine(r)}</div>`
                                     : `<div class="field-hint" style="margin-bottom:8px;">${t('rules.whenTrue', 'when true')}: ${this._esc(this._fmtWant(r.then?.params))} · ${t('rules.whenFalse', 'when false')}: ${this._esc(r.else ? this._fmtWant(r.else.params) : t('rules.nothing', 'nothing'))}</div>`}
                ${units.length ? `<div class="table-wrap"><table class="table"><thead><tr>
                    <th>${t('rules.unit', 'Unit')}</th><th>${t('rules.state', 'State')}</th><th title="${this._esc(this._tipWant())}">${t('rules.want', 'Want')} <i aria-hidden="true" class="bi bi-info-circle"></i></th>${counting ? `<th>${t('rules.today', 'Today')}</th>` : ''}<th title="${this._esc(this._tipActual())}">${t('rules.actual', 'Actual')} <i aria-hidden="true" class="bi bi-info-circle"></i></th><th></th></tr></thead>
                    <tbody>${unitsHtml}</tbody></table></div>` : ''}
                <div style="margin-top:8px;font-size:12.5px;">
                    ${last ? `<b>${t('rules.lastDecision', 'Last decision')}</b>: ${this._esc(last.action)} — ${this._esc(last.reason || '')} <span class="field-hint">${new Date(last.ts * 1000).toLocaleTimeString(this._loc())}${last.result ? ` · ${this._esc(last.result)}` : ''}</span>`
                           : `<span class="field-hint">${t('rules.noDecision', 'No decision yet.')}</span>`}
                </div>
                <div data-rule-decisions="${this._esc(r.id)}" ${decisionsOpen ? '' : 'hidden'} style="margin-top:10px;"></div>
            </div>
        </div>`;
    },

    async toggleRuleDecisions(id) {
        const box = document.querySelector(`[data-rule-decisions="${CSS.escape(id)}"]`);
        if (!box) return;
        box.hidden = !box.hidden;
        if (!box.hidden) this._loadRuleDecisions(id);
    },

    async _loadRuleDecisions(id) {
        const t = (k, d) => this.t(k, d);
        const box = document.querySelector(`[data-rule-decisions="${CSS.escape(id)}"]`);
        if (!box) return;
        const sigUnit = ((this._rules || []).find(x => x.id === id) || {}).signal_unit || '';
        let rows = [];
        try { rows = (await (await fetch(`/api/rules/${encodeURIComponent(id)}/decisions?limit=50`)).json()).decisions || []; } catch (e) { return; }
        box.innerHTML = rows.length ? `<div class="table-wrap"><table class="table"><thead><tr>
            <th>${t('rules.h.when', 'When')}</th><th>${t('rules.unit', 'Unit')}</th><th>${t('rules.signal', 'Signal')}${sigUnit ? ` <span class="field-hint">(${this._esc(sigUnit)})</span>` : ''}</th><th>${t('rules.state', 'State')}</th>
            <th title="${this._esc(this._tipWant())}">${t('rules.want', 'Want')}</th><th title="${this._esc(this._tipActual())}">${t('rules.actual', 'Actual')}</th><th>${t('rules.h.decision', 'Decision')}</th></tr></thead><tbody>
            ${rows.map(d => `<tr><td>${new Date(d.ts * 1000).toLocaleTimeString(this._loc())}</td><td><code>${this._esc(d.device)}</code></td>
                <td>${d.signal == null ? '—' : this._esc(this._fmtSig(d.signal))}</td>
                <td>${this._esc(d.state)}</td><td>${this._esc(this._fmtWant(d.want))}</td><td>${d.actual == null ? '—' : d.actual}</td>
                <td><b>${this._esc(d.action)}</b> — ${this._esc(d.reason || '')}${d.result ? ` · ${this._esc(d.result)}${d.result_reason ? ` (${this._esc(d.result_reason)})` : ''}` : ''}</td></tr>`).join('')}
            </tbody></table></div>` : `<div class="field-hint">${t('rules.noDecision', 'No decision yet.')}</div>`;
    },

    async _ruleOp(url, body, method = 'POST') {
        const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) {
            const e = new Error((d.detail?.errors || [d.detail || d.reason || r.statusText || `HTTP ${r.status}`]).join(' · '));
            e.status = r.status;            // a role's 403 is already explained (_roleSpeaks)
            e.detail = d.detail;
            throw e;
        }
        return d;
    },

    // what leaving armed does to the target: the safe values go out now,
    // unless the rule says to leave the device as it is
    _ruleLeaveText(r) {
        const t = (k, d, p) => this.t(k, d, p);
        const safe = Object.entries(r.safe_params || {}).map(([k, v]) => `${k}=${v}`).join(', ');
        return (r.on_disable || (r.raw || {}).on_disable || 'safe') === 'hold' || !safe
            ? t('rules.disarmHold', '{target} keeps the values it has now.', { target: this._ruleTargetText(r) })
            : t('rules.disarmSafe', '{target} is sent its safe values now: {safe}.', { target: this._ruleTargetText(r), safe });
    },

    async setRuleMode(id, mode) {
        const t = (k, d, p) => this.t(k, d, p);
        const r = (this._rules || []).find(x => x.id === id);
        if (!r) return;
        const title = `${r.label || r.id}`;
        if (mode === 'armed' && !await this.confirmAction({
            title: t('rules.armTitle', 'Arm {name}', { name: title }),
            message: r.enabled === false
                ? t('rules.armDisabledConfirm', 'It is disabled: it writes to {target} once enabled.', { target: this._ruleTargetText(r) })
                : t('rules.armConfirm', 'It will write to {target}.', { target: this._ruleTargetText(r) }),
            confirmLabel: t('rules.armGo', 'Arm'),
        })) return;
        if (mode !== 'armed' && r.mode === 'armed' && r.enabled !== false && !await this.confirmAction({
            title: t('rules.disarmTitle', 'Put {name} in shadow', { name: title }),
            message: t('rules.disarmConfirm', 'It stops writing.') + '\n' + this._ruleLeaveText(r),
            confirmLabel: t('rules.disarmGo', 'Shadow'),
        })) return;
        if (this._ruleBusy) return;                    // one change at a time: a second click is not a second request
        this._ruleBusy = true;
        try {
            const d = await this._ruleOp(`/api/rules/${encodeURIComponent(id)}/mode`, { mode });
            this._toastReleasing(r, d.releasing, d.release_warning);
        } catch (e) { this._opError(t('nav.rules', 'Rules'), e); }
        finally { this._ruleBusy = false; }
        this._loadRules();
    },

    // Disable / Enable: an armed rule writes on both — disabling sends its safe
    // values (on_disable), enabling lets it command its target again
    async setRuleEnabled(id, enabled) {
        const t = (k, d, p) => this.t(k, d, p);
        const r = (this._rules || []).find(x => x.id === id);
        if (!r || this._ruleBusy) return;
        if (r.mode === 'armed' && !await this.confirmAction(enabled ? {
            title: t('rules.enableTitle', 'Enable {name}', { name: r.label || r.id }),
            message: t('rules.enableConfirm', 'It is armed: it will write to {target} again.', { target: this._ruleTargetText(r) }),
            confirmLabel: t('rules.enableGo', 'Enable'),
        } : {
            title: t('rules.disableTitle', 'Disable {name}', { name: r.label || r.id }),
            message: t('rules.disableConfirm', 'It is armed: it stops writing.') + '\n' + this._ruleLeaveText(r),
            confirmLabel: t('rules.disableGo', 'Disable'),
        })) return;
        if (this._ruleBusy) return;
        this._ruleBusy = true;
        try {
            const d = await this._ruleOp(`/api/rules/${encodeURIComponent(id)}/enable`, { enabled });
            this._toastReleasing(r, d.releasing, d.release_warning);
        } catch (e) {
            this.showToast('error', this.t('nav.rules', 'Rules'), e.message);
        } finally { this._ruleBusy = false; }
        this._loadRules();
    },

    // leaving armed sends the safe values in the background: say so, and to
    // which units — and what could not be sent back (`release_warning`)
    _toastReleasing(r, units, warning = '') {
        if (units && units.length) this.showToast('info', r.label || r.id, this.t('rules.releasing',
            'Returning {units} to their safe values in the background; the Events page says when each is done.',
            { units: units.join(', ') }), 8000);
        if (warning) this.showToast('warning', `${r.label || r.id} — ${this.t('rules.releaseWarnTitle', 'not returned to safe')}`,
            String(warning), 15000);
    },

    async deleteRule(id) {
        const t = (k, d, p) => this.t(k, d, p);
        const r = (this._rules || []).find(x => x.id === id);
        let releasing = null, warning = '';
        if (!r || !await this.confirmAction({
            title: t('rules.deleteTitle', 'Delete rule {target}', { target: r.label || id }),
            message: r.mode === 'armed' && r.enabled !== false ? t('rules.deleteArmed', 'It is armed.') + '\n' + this._ruleLeaveText(r) : '',
            confirmLabel: t('common.delete', 'Delete'), danger: true,
            submit: async () => {
                try {
                    const d = await this._ruleOp(`/api/rules/${encodeURIComponent(id)}`, null, 'DELETE');
                    releasing = d.releasing; warning = d.release_warning || '';
                    return null;
                } catch (e) { return e.message; }
            },
        })) return;
        this._toastReleasing(r, releasing, warning);
        this._loadRules();
    },

    // Clamp, Pause and Force send from the dialog: a value that is not a
    // number is refused there, and the server's refusal is said there
    _ruleSend(url, body, method) {
        return this._ruleOp(url, body, method).then(() => null, e => e.message);
    },

    async openRuleClamp(id) {
        const t = (k, d, p) => this.t(k, d, p);
        const r = (this._rules || []).find(x => x.id === id);
        const now = Object.values(r?.live?.units || {}).find(u => u.clamp)?.clamp;
        const url = `/api/rules/${encodeURIComponent(id)}/clamp`;
        await this.askValues({
            title: t('rules.clampTitle', 'Clamp {name}', { name: r?.label || id }),
            message: t('rules.clampHint', 'A ceiling on what the rule may ask for, for a stated time'),
            fields: [
                { name: 'max', label: t('rules.clampAsk', 'Ceiling'), initial: now ? now.max : '', required: false,
                  hint: t('rules.clampEmpty', 'Empty clears the clamp.') },
                { name: 'hours', label: t('rules.clampHours', 'For how many hours'), unit: 'h', min: 0, initial: 2, required: false,
                  hint: t('rules.clampForever', 'Empty = until cleared.') },
            ],
            confirmLabel: t('common.apply', 'Apply'),
            submit: v => v.max === null ? this._ruleSend(url, null, 'DELETE')
                : this._ruleSend(url, { max: v.max, expires_s: v.hours === null ? null : v.hours * 3600 }),
        });
        this._loadRules();
    },

    async openRuleOverride(id) {
        const t = (k, d, p) => this.t(k, d, p);
        const r = (this._rules || []).find(x => x.id === id);
        await this.askValue({
            title: t('rules.overrideTitle', 'Pause {name}', { name: r?.label || id }),
            message: t('rules.overrideHint', 'Pause the rule so you or another system can command its target'),
            label: t('rules.overrideAsk', 'For how many minutes'), unit: 'min', min: 0, max: 1440, initial: 15,
            hint: t('rules.overrideLift', '0 lifts the pause.'),
            confirmLabel: t('rules.overrideGo', 'Pause'),
            submit: m => this._ruleSend(`/api/rules/${encodeURIComponent(id)}/override`, { seconds: m * 60 }),
        });
        this._loadRules();
    },

    // ── the editor ──────────────────────────────────────────────────────────

    async _ruleTargets() {
        // every unit that offers an enabled command, and every group whose units do
        let devices = [];
        try { devices = (await this._fetchDevices(true)) || []; } catch (e) {}
        const units = devices.filter(d => (d.commands || []).length).map(d => ({ key: `d:${d.id}`, label: `${d.name || d.id} (${d.id})`, commands: d.commands, target: { device: d.id } }));
        const groups = [];
        try {
            const eps = (await (await fetch('/api/endpoints')).json()).endpoints || [];
            for (const p of eps) {
                let detail = null;
                try { detail = await (await fetch(`/api/endpoints/${encodeURIComponent(p.id)}`)).json(); } catch (e) {}
                for (const g of (detail?.groups || [])) {
                    const cmds = (g.commands || []).filter(c => c.enabled && !c.alias).map(c => c.name);
                    if (cmds.length) groups.push({ key: `g:${p.id}/${g.id}`, label: `${p.name || p.id} / ${g.id} (${g.total_units} ${this.t('rules.units', 'units')})`, commands: cmds, target: { endpoint: p.id, group: g.id } });
                }
            }
        } catch (e) {}
        return [...groups, ...units];
    },

    async openRuleModal(id, fromPreset = null) {
        const t = (k, d) => this.t(k, d);
        const r = id ? (this._rules || []).find(x => x.id === id) : null;
        // a new rule may start from an example a device's template ships
        let presets = [];
        if (!r) {
            try { presets = (await (await fetch('/api/rule-presets')).json()).presets || []; } catch (e) {}
        }
        this._rulePresets = presets;
        const raw = r ? (r.raw || r) : (fromPreset ? fromPreset.rule : null);
        // what the form starts from: the stored rule, or the preset's — the
        // keys the form does not show come from it
        this._ruleEdit = { id: r ? r.id : '', targets: await this._ruleTargets(),
                           base: raw ? JSON.parse(JSON.stringify(raw)) : {} };
        const tg = raw?.target || {};
        const curKey = tg.device ? `d:${tg.device}` : tg.endpoint ? `g:${tg.endpoint}/${tg.group}` : '';
        // the rule's own target, when nothing offers its command now: kept and
        // said, never silently swapped for the first target listed
        const cur = this._ruleEdit.targets.find(x => x.key === curKey);
        let notOffered = '';
        if (curKey && (!cur || (tg.command && !cur.commands.includes(tg.command)))) {
            notOffered = tg.command || '';
            const label = `${tg.device || `${tg.endpoint} / ${tg.group}`} · ${tg.command || '?'} — ${t('rules.notOfferedNow', 'not offered now')}`;
            if (cur) { cur.commands = [...cur.commands, tg.command]; cur.notOffered = tg.command; }
            else this._ruleEdit.targets.unshift({ key: curKey, label, commands: tg.command ? [tg.command] : [], target: { ...tg }, stale: true, notOffered: tg.command });
        }
        const saveBtn = document.querySelector('#ruleModal [data-rule-save]');
        if (saveBtn) saveBtn.style.display = '';            // .btn's display beats [hidden]
        if (!this._ruleEdit.targets.length) {
            // nothing here offers a command: a rule could not be saved, so the
            // form is not shown — only what to do about it
            document.getElementById('ruleModalTitle').textContent = t('rules.addTitle', 'Add rule');
            document.getElementById('ruleModalBody').innerHTML = `<div class="empty-state" data-rule-empty style="padding:18px 6px;text-align:center;">
                <i aria-hidden="true" class="bi bi-plug" style="font-size:28px;color:var(--text-secondary);"></i>
                <p style="margin:10px 0 6px;"><b>${t('rules.emptyTitle', 'Nothing here offers a command yet')}</b></p>
                <p class="field-hint" style="margin:0 0 12px;">${t('rules.emptyBody', 'A rule asks a device or a group for something through an enabled command. Enable one on a device\'s Commands tab (or an installation group\'s), then add the rule.')}</p>
                <button type="button" class="btn btn-secondary btn-sm" ${this._act('_ruleGoDevices', [])}><i aria-hidden="true" class="bi bi-hdd-network"></i> ${t('rules.emptyGo', 'Go to Devices')}</button>
            </div>`;
            document.getElementById('ruleFeedback').textContent = '';
            if (saveBtn) saveBtn.style.display = 'none';
            this.openModal('ruleModal');
            return;
        }
        const kind = raw?.kind || 'steps';
        const tm = raw?.timing || {};
        const kv = o => Object.entries(o || {}).map(([k, v]) => `${k}=${v}`).join(', ');
        document.getElementById('ruleModalTitle').textContent = r ? t('rules.editTitle', 'Edit rule') : t('rules.addTitle', 'Add rule');
        const presetPick = presets.length ? `
            <div class="form-group"><label class="form-label" for="rlPreset">${t('rules.fromPreset', 'Start from')}</label>
                <select id="rlPreset" class="input" ${this._act('_rulePresetPicked', [], { el: true, on: 'change' })}>
                    <option value="">${t('rules.blank', 'a blank rule')}</option>
                    ${presets.map((p, i) => `<option value="${i}" ${fromPreset && fromPreset.device === p.device && fromPreset.preset === p.preset ? 'selected' : ''}>${this._esc(`${p.device_name} · ${p.label}`)}</option>`).join('')}
                </select>
                <div class="field-hint">${this._esc(fromPreset?.description || t('rules.presetHint', 'Examples from the templates of your devices. A rule always starts in shadow: it decides and logs, it writes nothing until you arm it.'))}</div>
            </div>` : '';
        const staleLine = curKey && this._ruleEdit.targets.find(x => x.key === curKey)?.stale || notOffered
            ? `<p class="field-error" data-rule-not-offered style="margin:0 0 10px;">${this._esc(r
                ? this.t('rules.notOfferedLine',
                    'Its target no longer offers {command}: the rule cannot run until it does. Enable the command on the device, or pick another target.',
                    { command: tg.command || '?' })
                : this.t('rules.notOfferedNew',
                    'This example\'s target does not offer {command} here: enable the command on the device, or pick another target.',
                    { command: tg.command || '?' }))}</p>` : '';
        document.getElementById('ruleModalBody').innerHTML = `${presetPick}${staleLine}
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="rlId">${t('rules.id', 'Rule ID')}</label>
                    <input id="rlId" class="input" value="${this._esc(raw?.id || '')}" ${r ? 'disabled' : ''} placeholder="ov-u1"></div>
                <div class="form-group flex-2"><label class="form-label" for="rlLabel">${t('rules.label', 'Name')}</label>
                    <input id="rlLabel" class="input" value="${this._esc(raw?.label || '')}" placeholder="${t('rules.labelPh', 'Over-voltage protection · inverter 1')}"></div>
                <div class="form-group"><label class="form-label" for="rlKind">${t('rules.kind', 'Kind')}</label>
                    <select id="rlKind" class="input" data-action="_ruleKindChanged" data-on="change">
                        <option value="steps" ${kind === 'steps' ? 'selected' : ''}>${t('rules.kindSteps', 'Steps (thresholds → value)')}</option>
                        <option value="condition" ${kind === 'condition' ? 'selected' : ''}>${t('rules.kindCondition', 'Condition (true / false)')}</option>
                        <option value="trim" ${kind === 'trim' ? 'selected' : ''}>${t('rules.kindTrim', 'Trim (nudge up / down)')}</option>
                        <option value="schedule" ${kind === 'schedule' ? 'selected' : ''}>${t('rules.kindSchedule', 'Schedule (days and times → value)')}</option>
                    </select></div>
            </div>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="rlLayer">${t('rules.layer', 'Layer')}</label>
                    <select id="rlLayer" class="input">${['safety', 'manual', 'schedule', 'optimize'].map(l => `<option value="${l}" ${(raw?.layer || 'schedule') === l ? 'selected' : ''}>${t('rules.layer_' + l, l)} (${{ safety: 90, manual: 70, schedule: 50, optimize: 30 }[l]})</option>`).join('')}</select>
                    <div class="field-hint">${t('rules.layerFieldHint', 'When several rules drive one target, the higher layer wins; a rule with no opinion lets the next one act.')}</div></div>
                <div class="form-group"><label class="form-label" for="rlPriority">${t('rules.priority', 'Priority (optional)')}</label>
                    <input id="rlPriority" class="input" type="number" min="0" max="100" value="${raw?.priority ?? ''}" placeholder="${t('rules.priorityPh', 'the layer\'s')}"></div>
            </div>
            <div class="form-row">
                <div class="form-group flex-2"><label class="form-label" for="rlTarget">${t('rules.target', 'Target')}</label>
                    <select id="rlTarget" class="input" data-action="_ruleTargetChanged" data-on="change">
                        ${this._ruleEdit.targets.map(x => `<option value="${this._esc(x.key)}" ${x.key === curKey ? 'selected' : ''}${x.notOffered ? ' data-not-offered' : ''}>${this._esc(x.label)}${x.notOffered && !x.stale ? ` — ${this._esc(t('rules.cmdNotOffered', '{command} not offered now').replace('{command}', x.notOffered))}` : ''}</option>`).join('')}
                    </select>
                    <div class="field-hint">${t('rules.targetHint', 'A group applies the command to every unit; a unit, to that one. Only targets with an enabled command are listed.')}</div></div>
                <div class="form-group"><label class="form-label" for="rlCommand">${t('commands.which', 'Command')}</label>
                    <select id="rlCommand" class="input"></select></div>
            </div>
            <div data-rl-kind="steps" ${kind === 'steps' ? '' : 'hidden'}>
                <div class="form-group"><label class="form-label" for="rlSignal">${t('rules.signal', 'Signal')}</label>
                    <input id="rlSignal" class="input" value="${this._esc(raw?.signal || '')}" placeholder="max(pv-u1.voltage_l1_n, pv-u1.voltage_l2_n, pv-u1.voltage_l3_n)" style="font-family:monospace;">
                    <div class="field-hint">${t('rules.signalHint', 'An expression over live values, written device.register — the same grammar as calculated registers (min, max, avg, abs, + - * /).')} ${t('rules.timeHint', 'Time: hour(), weekday() (1 = Monday), between("10:00", "16:00"); counters: run_today_h("rule-id"), starts_today("rule-id").')}</div></div>
                <div class="form-group"><label class="form-label" for="rlSignalUnit">${t('rules.signalUnit', 'What the signal means')}</label>
                    <input id="rlSignalUnit" class="input" maxlength="40" value="${this._esc(kind === 'steps' ? (raw?.signal_unit || '') : '')}" placeholder="${t('rules.signalUnitPh', 'W under the cap')}">
                    <div class="field-hint">${t('rules.signalUnitHint', 'Shown next to the number on the card and in the decisions, so a sum such as 20000 + meter.power reads as what it is.')}</div></div>
                <div class="form-group"><label class="form-label">${t('rules.steps', 'Steps')}</label>
                    <div class="table-wrap"><table class="table" id="rlSteps"><thead><tr><th>${t('rules.stepAt', 'Signal at or above')}</th><th>${t('rules.stepValue', 'Ask for')}</th><th>${t('rules.stepLabel', 'Label')}</th><th>${t('rules.stepFast', 'Immediate')}</th><th></th></tr></thead>
                    <tbody>${(raw?.steps || [{ at: 250, value: 80, label: 'Warning' }]).map(s => this._ruleStepRow(s)).join('')}</tbody></table></div>
                    <button type="button" class="btn btn-ghost btn-sm" data-action="_ruleAddStep"><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('rules.addStep', 'Add step')}</button>
                    <div class="field-hint">${t('rules.stepsHint', 'The highest matching step wins. An immediate step skips the debounce — the emergency path.')}</div></div>
                <div class="form-row">
                    <div class="form-group"><label class="form-label" for="rlRelease">${t('rules.release', 'Normal again under')}</label>
                        <input id="rlRelease" class="input" type="number" step="any" value="${raw?.release_below ?? ''}">
                        <div class="field-hint">${t('rules.releaseHint', 'Between this and the first step the rule holds what it has — the dead band.')}</div></div>
                    <div class="form-group"><label class="form-label" for="rlNormal">${t('rules.normal', 'Normal asks for')}</label>
                        <input id="rlNormal" class="input" value="${this._esc(raw && raw.normal === null ? '' : kv(raw?.normal || { value: 100 }))}" placeholder="value=100" ${raw && raw.normal === null ? 'disabled' : ''}>
                        <label class="field-hint" style="display:flex;align-items:center;gap:6px;margin-top:4px;">
                            <input type="checkbox" id="rlNormalNone" ${raw && raw.normal === null ? 'checked' : ''} ${this._act('_ruleNoOpinion', ['rlNormal'], { el: true, on: 'change' })}>
                            ${t('rules.noOpinion', 'No opinion — a lower layer decides')}</label></div>
                    <div class="form-group"><label class="form-label" for="rlValidMin">${t('rules.valid', 'Valid signal range')}</label>
                        <div style="display:flex;gap:6px;"><input id="rlValidMin" class="input" type="number" step="any" placeholder="min" value="${raw?.signal_valid?.min ?? ''}" aria-label="${this._esc(this.t('rules.validMin', 'Lowest valid value'))}"><input id="rlValidMax" class="input" type="number" step="any" placeholder="max" value="${raw?.signal_valid?.max ?? ''}" aria-label="${this._esc(this.t('rules.validMax', 'Highest valid value'))}"><input id="rlValidStep" class="input" type="number" step="any" min="0" placeholder="${t('rules.validStep', 'max step')}" value="${raw?.signal_valid?.max_step ?? ''}" aria-label="${t('rules.validStepLabel', 'largest plausible change between two samples')}"></div>
                        <div class="field-hint">${t('rules.validHint', 'A sample outside it is ignored as sensor garbage. A change larger than the max step is held until the next sample confirms it — a one-sample artefact never reaches a step.')}</div></div>
                </div>
                <details class="form-group" data-admin ${(tm.boot_wait_s || tm.urgent_retries || raw?.resume_on_boot) ? 'open' : ''}>
                    <summary class="form-label" style="cursor:pointer;">${t('rules.adv.title', 'Advanced: after a restart, urgent sends')}</summary>
                    <div class="form-row" style="margin-top:8px;">
                        <div class="form-group"><label class="form-label" for="rlBootWait">${t('rules.adv.bootWait', 'After a restart, wait for the read-back up to (s)')}</label>
                            <input id="rlBootWait" class="input" type="number" min="0" max="600" step="1" value="${tm.boot_wait_s || ''}" placeholder="0" aria-describedby="rlBootWaitHint">
                            <div class="field-hint" id="rlBootWaitHint">${t('rules.adv.bootWaitHint', 'Before sending "Normal" after a restart, read what the device holds first — if it holds it already, nothing is sent. A step or an urgent want never waits. 0 or empty = off.')}</div></div>
                        <div class="form-group"><label class="form-label" for="rlUrgentRetries">${t('rules.adv.urgentRetries', 'Send an urgent want again, at most (times)')}</label>
                            <input id="rlUrgentRetries" class="input" type="number" min="0" max="5" step="1" value="${tm.urgent_retries || ''}" placeholder="0" aria-describedby="rlUrgentRetriesHint">
                            <div class="field-hint" id="rlUrgentRetriesHint">${t('rules.adv.urgentRetriesHint', 'An immediate step or a stale safe value that did not get through is sent again 5 s later instead of after "Min. between commands". The same want only. 0 or empty = off.')}</div></div>
                    </div>
                    <label class="field-hint" style="display:flex;align-items:center;gap:6px;">
                        <input type="checkbox" id="rlResumeOnBoot" ${raw?.resume_on_boot === true ? 'checked' : ''} aria-describedby="rlResumeHint">
                        ${t('rules.adv.resume', 'After a restart, resume the step the device holds')}</label>
                    <div class="field-hint" id="rlResumeHint">${t('rules.adv.resumeHint', 'A device already at a step\'s value starts there, and nothing is sent; the signal moves it as usual.')}</div>
                </details>
            </div>
            <div data-rl-kind="condition" ${kind === 'condition' ? '' : 'hidden'}>
                <div class="form-group"><label class="form-label" for="rlWhen">${t('rules.when', 'When')}</label>
                    <input id="rlWhen" class="input" value="${this._esc(raw?.when || '')}" placeholder="pv-meter.frequency < 45 or pv-meter.voltage_ln_avg < 100" style="font-family:monospace;">
                    <div class="field-hint">${t('rules.timeHint', 'Time: hour(), weekday() (1 = Monday), between("10:00", "16:00"); counters: run_today_h("rule-id"), starts_today("rule-id").')}</div></div>
                <div class="form-group"><label class="form-label" for="rlUntil">${t('rules.untilField', 'Until (optional)')}</label>
                    <input id="rlUntil" class="input" value="${this._esc(raw?.release || '')}" placeholder="heat-pump.iu0_water_in_temperature > 35" style="font-family:monospace;">
                    <div class="field-hint">${t('rules.untilHint', 'Hysteresis: once "When" holds, the rule stays on "True asks for" until this holds. Empty: "False asks for" follows as soon as "When" turns false.')}</div></div>
                <div class="form-row">
                    <div class="form-group"><label class="form-label" for="rlThen">${t('rules.then', 'True asks for')}</label>
                        <input id="rlThen" class="input" value="${this._esc(kv(raw?.then?.params))}" placeholder="value=100, revert_s=0"></div>
                    <div class="form-group"><label class="form-label" for="rlElse">${t('rules.else', 'False asks for')}</label>
                        <input id="rlElse" class="input" value="${this._esc(kv(raw?.else?.params))}" placeholder="${t('rules.elsePh', 'empty = nothing')}"></div>
                </div>
            </div>
            <div data-rl-kind="schedule" ${kind === 'schedule' ? '' : 'hidden'}>
                <div class="form-group"><label class="form-label">${t('rules.scheduleEntries', 'Entries')}</label>
                    <div class="table-wrap"><table class="table" id="rlSched"><thead><tr><th>${t('rules.schedDays', 'Days')}</th><th>${t('rules.schedAt', 'From')}</th><th>${t('rules.stepValue', 'Ask for')}</th><th title="${this._esc(t('rules.noOpinion', 'No opinion — a lower layer decides'))}">${t('rules.noOpinionShort', 'No opinion')}</th><th>${t('rules.stepLabel', 'Label')}</th><th></th></tr></thead>
                    <tbody>${(kind === 'schedule' && raw?.schedule ? raw.schedule : [{ days: 'Mon-Fri', at: '06:00', value: 25 }, { days: 'Mon-Fri', at: '22:00', value: 0 }]).map(e => this._ruleSchedRow(e)).join('')}</tbody></table></div>
                    <button type="button" class="btn btn-ghost btn-sm" data-action="_ruleAddSched"><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('rules.addEntry', 'Add entry')}</button>
                    <div class="field-hint">${t('rules.schedHint', 'Days: Mon-Fri, Sat,Sun, 1-5 (1 = Monday) or all. The last entry already reached is in force, looking back up to a week.')}</div></div>
                <div class="form-group"><label class="form-label" for="rlSchedWhen">${t('rules.schedWhen', 'Only when (optional)')}</label>
                    <input id="rlSchedWhen" class="input" value="${this._esc(kind === 'schedule' ? (raw?.when || '') : '')}" placeholder="tank.temperature < 60" style="font-family:monospace;">
                    <div class="field-hint">${t('rules.schedWhenHint', 'While it is false the rule asks for the off value (under Timers).')}</div></div>
            </div>
            <div data-rl-kind="trim" ${kind === 'trim' ? '' : 'hidden'}>
                <div class="form-group"><label class="form-label" for="rlTrimSignal">${t('rules.signal', 'Signal')}</label>
                    <input id="rlTrimSignal" class="input" value="${this._esc(kind === 'trim' ? (raw?.signal || '') : '')}" placeholder="-site-meter.power_active_total" style="font-family:monospace;">
                    <div class="field-hint">${t('rules.trimSignalHint', 'Write it so that more means "more of the value": export as a positive number, for example.')}</div></div>
                <div class="form-group"><label class="form-label" for="rlTrimSignalUnit">${t('rules.signalUnit', 'What the signal means')}</label>
                    <input id="rlTrimSignalUnit" class="input" maxlength="40" value="${this._esc(kind === 'trim' ? (raw?.signal_unit || '') : '')}" placeholder="${t('rules.signalUnitPh', 'W under the cap')}">
                    <div class="field-hint">${t('rules.signalUnitHint', 'Shown next to the number on the card and in the decisions, so a sum such as 20000 + meter.power reads as what it is.')}</div></div>
                <div class="form-row">
                    <div class="form-group"><label class="form-label" for="rlTrimAbove">${t('rules.trimAbove', 'Raise while over')}</label>
                        <input id="rlTrimAbove" class="input" type="number" step="any" value="${raw?.above ?? 500}"></div>
                    <div class="form-group"><label class="form-label" for="rlTrimBelow">${t('rules.trimBelow', 'Lower while under')}</label>
                        <input id="rlTrimBelow" class="input" type="number" step="any" value="${raw?.below ?? 100}">
                        <div class="field-hint">${t('rules.trimBandHint', 'Between the two it holds.')}</div></div>
                    <div class="form-group"><label class="form-label" for="rlTrimStep">${t('rules.trimStep', 'Step')}</label>
                        <input id="rlTrimStep" class="input" type="number" step="any" min="0" value="${raw?.step ?? 2}"></div>
                    <div class="form-group"><label class="form-label" for="rlTrimGain">${t('rules.trimGain', 'Gain (optional)')}</label>
                        <input id="rlTrimGain" class="input" type="number" step="any" min="0" value="${raw?.gain ?? ''}">
                        <div class="field-hint">${t('rules.trimGainHint', 'Proportional: gain × the distance past the threshold, at most the step. Empty = always the step.')}</div></div>
                </div>
                <div class="form-row">
                    <div class="form-group"><label class="form-label" for="rlTrimMin">${t('rules.trimMin', 'Min')}</label>
                        <input id="rlTrimMin" class="input" type="number" step="any" value="${raw?.min ?? 15}"></div>
                    <div class="form-group"><label class="form-label" for="rlTrimMax">${t('rules.trimMax', 'Max')}</label>
                        <input id="rlTrimMax" class="input" type="number" step="any" value="${raw?.max ?? 50}"></div>
                    <div class="form-group"><label class="form-label" for="rlTrimStart">${t('rules.trimStart', 'Start at')}</label>
                        <input id="rlTrimStart" class="input" type="number" step="any" value="${raw?.start ?? ''}" placeholder="min"></div>
                    <div class="form-group"><label class="form-label" for="rlTrimOff">${t('rules.trimOff', 'Off value (optional)')}</label>
                        <input id="rlTrimOff" class="input" type="number" step="any" value="${raw?.off_value ?? ''}">
                        <div class="field-hint">${t('rules.trimOffHint', 'Asked for under min (e.g. 0 = stop); from off it starts again at "Start at".')}</div></div>
                </div>
            </div>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="rlParams">${t('rules.params', 'Sent with every want')}</label>
                    <input id="rlParams" class="input" value="${this._esc(kv(raw?.params))}" placeholder="revert_s=0, ramp_s=10">
                    <div class="field-hint">${t('rules.paramsHint', 'Fixed parameters of the command, name=value.')}</div></div>
                <div class="form-group"><label class="form-label" for="rlStale">${t('rules.staleAfter', 'Signal stale after (s)')}</label>
                    <input id="rlStale" class="input" type="number" min="1" value="${raw?.stale_after_s ?? 60}"></div>
                <div class="form-group"><label class="form-label" for="rlOnStale">${t('rules.onStale', 'When stale')}</label>
                    <select id="rlOnStale" class="input" ${this._act('_ruleOnStaleChanged', [], {value: true, on: 'change'})}><option value="hold" ${(raw?.on_stale || 'hold') === 'hold' ? 'selected' : ''}>${t('rules.hold', 'hold the last want')}</option><option value="safe" ${raw?.on_stale === 'safe' ? 'selected' : ''}>${t('rules.safe', 'ask for the safe values')}</option><option value="value" ${(typeof raw?.on_stale === 'number') ? 'selected' : ''}>${t('rules.staleValue', 'ask for a fixed value (fail closed)')}</option></select>
                    <span id="rlStaleValueWrap" ${(typeof raw?.on_stale === 'number') ? '' : 'hidden'}><input id="rlStaleValue" class="input" type="number" step="any" value="${(typeof raw?.on_stale === 'number') ? raw.on_stale : 80}" aria-label="${t('rules.staleValueLabel', 'value while stale')}"></span></div>
                <div class="form-group"><label class="form-label" for="rlOnDisable">${t('rules.onDisable', 'When disabled')}</label>
                    <select id="rlOnDisable" class="input"><option value="safe" ${(raw?.on_disable || 'safe') === 'safe' ? 'selected' : ''}>${t('rules.safe', 'ask for the safe values')}</option><option value="hold" ${raw?.on_disable === 'hold' ? 'selected' : ''}>${t('rules.holdLeave', 'leave the device as it is')}</option></select></div>
            </div>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="rlEvery">${t('rules.every', 'Evaluate every (s)')}</label><input id="rlEvery" class="input" type="number" step="any" min="0.5" value="${tm.every_s ?? 2}"></div>
                <div class="form-group"><label class="form-label" for="rlDebounce">${t('rules.debounce', 'Debounce (evaluations)')}</label><input id="rlDebounce" class="input" type="number" min="1" value="${tm.debounce ?? 3}"></div>
                <div class="form-group"><label class="form-label" for="rlMinInt">${t('rules.minInterval', 'Min. between commands (s)')}</label><input id="rlMinInt" class="input" type="number" min="0" value="${tm.min_interval_s ?? 30}"></div>
                <div class="form-group"><label class="form-label" for="rlReassert">${t('rules.reassert', 'Re-command after drift (s)')}</label><input id="rlReassert" class="input" type="number" min="0" value="${tm.reassert_s ?? 120}"><div class="field-hint">${t('rules.reassertHint', '0 = never')}</div></div>
            </div>
            <details class="form-group" ${['for_s', 'min_on_s', 'min_off_s', 'max_on_s', 'max_starts_per_h'].some(k => tm[k]) ? 'open' : ''}>
                <summary class="form-label" style="cursor:pointer;">${t('rules.timers', 'Timers: wait, minimum run and stop, maximum run, starts per hour')}</summary>
                <div class="form-row" style="margin-top:8px;">
                    <div class="form-group"><label class="form-label" for="rlForS">${t('rules.forS', 'Wait before acting (s)')}</label><input id="rlForS" class="input" type="number" min="0" value="${tm.for_s ?? 0}"><div class="field-hint">${t('rules.forSHint', 'A change must be wanted this long, e.g. 300 = "for 5 minutes".')}</div></div>
                    <div class="form-group"><label class="form-label" for="rlMinOn">${t('rules.minOn', 'Minimum run (s)')}</label><input id="rlMinOn" class="input" type="number" min="0" value="${tm.min_on_s ?? 0}"></div>
                    <div class="form-group"><label class="form-label" for="rlMinOff">${t('rules.minOff', 'Minimum stop (s)')}</label><input id="rlMinOff" class="input" type="number" min="0" value="${tm.min_off_s ?? 0}"></div>
                    <div class="form-group"><label class="form-label" for="rlMaxOn">${t('rules.maxOn', 'Maximum run (s)')}</label><input id="rlMaxOn" class="input" type="number" min="0" value="${tm.max_on_s ?? 0}"><div class="field-hint">${t('rules.maxOnHint', 'Then it stops and rests for the minimum stop.')}</div></div>
                    <div class="form-group"><label class="form-label" for="rlMaxStarts">${t('rules.maxStarts', 'Starts per hour, at most')}</label><input id="rlMaxStarts" class="input" type="number" min="0" value="${tm.max_starts_per_h ?? 0}"></div>
                    <div class="form-group" data-rl-notrim ${kind === 'trim' ? 'hidden' : ''}><label class="form-label" for="rlOff">${t('rules.offValue', 'Off value')}</label><input id="rlOff" class="input" type="number" step="any" value="${kind !== 'trim' ? (raw?.off_value ?? '') : ''}" placeholder="${t('rules.offPh', 'from "False asks for"')}"><div class="field-hint">${t('rules.offHint', 'What the command asks for when the target is stopped (e.g. 0). Empty: a condition takes "False asks for", steps take "Normal".')}</div></div>
                </div>
                <div class="field-hint">${t('rules.timersHint', '0 = off. A stale signal asking for the safe values never waits for a timer.')}</div>
            </details>
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                <button type="button" class="btn btn-ghost btn-sm" data-action="previewRule"><i aria-hidden="true" class="bi bi-eye"></i> ${t('rules.preview', 'Preview now')}</button>
                <span id="rlPreview" class="field-hint" role="status" aria-live="polite">${t('rules.previewHint', 'What the rule would see and want right now, nothing kept.')}</span>
            </div>
            ${r ? '' : `<p class="field-hint" style="margin:10px 0 0;">${t('rules.newShadow', 'A new rule is saved in shadow: it decides and says so, but writes nothing until you arm it.')}</p>`}`;
        document.getElementById('ruleFeedback').textContent = '';
        this._ruleTargetChanged(tg.command || '');
        this._ruleWirePickers();
        // what the form sends untouched, its defaults included: a Save that
        // sends the same is no change (no confirmation, no request)
        this._ruleEdit.loaded = r ? this._ruleCanon(this._ruleFromForm()) : null;
        this._ruleFormErrors = [];
        this.openModal('ruleModal');
    },

    // the target picked from a searchable list; the formulas complete
    // `device.field` as typed and say a name the gateway lacks
    _ruleWirePickers() {
        this._pickDevice('rlTarget');
        if (document.getElementById('rlPreset')) this._pickPlain('rlPreset');
        ['rlSignal', 'rlWhen', 'rlUntil', 'rlTrimSignal'].forEach(id => this._pickFormula(id));
    },

    _ruleCanon(o) {
        return JSON.stringify(o, (k, v) => (v && typeof v === 'object' && !Array.isArray(v))
            ? Object.fromEntries(Object.keys(v).sort().map(x => [x, v[x]])) : v);
    },

    _ruleGoDevices() {
        this.closeModal('ruleModal');
        this.navigateTo('devices');
    },

    // a server refusal, said with the form's own words: "target.param: …"
    // becomes "Target: …"
    _ruleErrText(msg) {
        const FIELD = { label: 'rlLabel', kind: 'rlKind', layer: 'rlLayer', priority: 'rlPriority', target: 'rlTarget',
                        signal: 'rlSignal', release_below: 'rlRelease', normal: 'rlNormal', when: 'rlWhen', release: 'rlUntil',
                        then: 'rlThen', else: 'rlElse', params: 'rlParams', stale_after_s: 'rlStale', on_stale: 'rlOnStale',
                        on_disable: 'rlOnDisable' };
        const m = /^([a-z_]+)(?:[.[][^:\s]*)?:\s*(.*)$/s.exec(String(msg));
        if (!m) return String(msg);
        const id = FIELD[m[1]];
        const lbl = id && document.querySelector(`#ruleModal label[for="${id}"], #ruleModal label[data-for-select="${id}"]`)?.textContent.trim();
        return lbl ? `${lbl}: ${m[2]}` : String(msg);
    },

    _ruleFeedbackErrors(errs) {
        const fb = document.getElementById('ruleFeedback');
        if (!fb) return;
        fb.innerHTML = errs.map(e => `<span style="display:block;color:var(--danger,#c0392b);">${this._esc(this._ruleErrText(e))}</span>`).join('');
    },

    _rulePresetPicked(el) {
        const p = (this._rulePresets || [])[Number(el.value)];
        this.openRuleModal('', el.value === '' ? null : p);
    },

    _ruleStepRow(s = {}) {
        return `<tr>
            <td><input class="input" type="number" step="any" value="${s.at ?? ''}" data-st="at" aria-label="${this._esc(this.t('rules.stepAt', 'Signal at or above'))}"></td>
            <td><input class="input" type="number" step="any" value="${s.value ?? ''}" data-st="value" aria-label="${this._esc(this.t('rules.stepValue', 'Ask for'))}"></td>
            <td><input class="input" value="${this._esc(s.label || '')}" data-st="label" aria-label="${this._esc(this.t('rules.stepLabel', 'Label'))}"></td>
            <td style="text-align:center;"><input type="checkbox" ${s.fast ? 'checked' : ''} data-st="fast" aria-label="${this._esc(this.t('rules.stepFast', 'Immediate'))}"></td>
            <td><button type="button" class="btn btn-ghost btn-sm" data-action="_removeRow" data-with-el aria-label="${this.t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-x-lg"></i></button></td></tr>`;
    },

    _ruleSchedRow(e = {}) {
        const days = Array.isArray(e.days) ? this._daysText(e.days.map(Number), true) : (e.days ?? 'all');
        const none = !e.params && 'value' in e && e.value === null;
        // what the row cannot show (a key a hand-written entry carries) rides along
        const extra = Object.fromEntries(Object.entries(e).filter(([k]) => !['days', 'at', 'value', 'params', 'label'].includes(k)));
        const shown = e.params ? Object.entries(e.params).map(([k, v]) => `${k}=${v}`).join(', ') : (none ? '' : (e.value ?? ''));
        return `<tr data-extra="${this._esc(JSON.stringify(extra))}">
            <td><input class="input" value="${this._esc(days)}" data-sc="days" aria-label="${this.t('rules.schedDays', 'Days')}" placeholder="Mon-Fri"></td>
            <td><input class="input" type="time" value="${this._esc(e.at || '')}" data-sc="at" aria-label="${this.t('rules.schedAt', 'From')}"></td>
            <td><input class="input" value="${this._esc(String(shown))}" data-sc="value" ${none ? 'disabled' : ''} aria-label="${this.t('rules.stepValue', 'Ask for')}" placeholder="25 ${this._esc(this.t('rules.orParams', 'or value=25, ramp_s=10'))}"></td>
            <td style="text-align:center;"><input type="checkbox" data-sc="none" ${none ? 'checked' : ''} ${this._act('_ruleRowNoOpinion', [], { el: true, on: 'change' })} aria-label="${this._esc(this.t('rules.noOpinion', 'No opinion — a lower layer decides'))}"></td>
            <td><input class="input" value="${this._esc(e.label || '')}" data-sc="label" aria-label="${this.t('rules.stepLabel', 'Label')}"></td>
            <td><button type="button" class="btn btn-ghost btn-sm" data-action="_removeRow" data-with-el aria-label="${this.t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-x-lg"></i></button></td></tr>`;
    },

    // "No opinion" is said, never inferred from an empty box
    _ruleNoOpinion(inputId, cb) {
        const el = document.getElementById(inputId);
        if (el) { el.disabled = cb.checked; if (!cb.checked && !el.value.trim()) el.value = 'value=100'; }
    },

    _ruleRowNoOpinion(cb) {
        const el = cb.closest('tr')?.querySelector('[data-sc="value"]');
        if (el) el.disabled = cb.checked;
    },

    _ruleAddSched() {
        document.querySelector('#rlSched tbody')?.insertAdjacentHTML('beforeend', this._ruleSchedRow());
    },

    async openRuleForce(id) {
        const t = (k, d, p) => this.t(k, d, p);
        const r = (this._rules || []).find(x => x.id === id);
        const url = `/api/rules/${encodeURIComponent(id)}/force`;
        await this.askValues({
            title: t('rules.forceTitle', 'Force {name}', { name: r?.label || id }),
            message: t('rules.forceHint', 'Ask for a value for a while (run for an hour), then the rule decides again'),
            fields: [
                { name: 'value', label: t('rules.forceAsk', 'Value'), required: false,
                  hint: t('rules.forceEmpty', 'Empty ends a force.') },
                { name: 'minutes', label: t('rules.forceMinutes', 'For how many minutes'), unit: 'min', min: 1, max: 1440, step: 1, initial: 60,
                  required: false, validate: (m, all) => (all.value !== null && m === null ? t('dialog.required', 'Required') : null) },
            ],
            confirmLabel: t('rules.forceGo', 'Force'),
            submit: v => v.value === null ? this._ruleSend(url, null, 'DELETE')
                : this._ruleSend(url, { value: v.value, seconds: v.minutes * 60 }),
        });
        this._loadRules();
    },

    _ruleOnStaleChanged(value) {
        const wrap = document.getElementById('rlStaleValueWrap');
        if (wrap) wrap.hidden = value !== 'value';
    },

    _ruleAddStep() {
        document.querySelector('#rlSteps tbody')?.insertAdjacentHTML('beforeend', this._ruleStepRow());
    },

    _ruleKindChanged() {
        const k = document.getElementById('rlKind').value;
        document.querySelectorAll('#ruleModal [data-rl-kind]').forEach(el => el.hidden = el.dataset.rlKind !== k);
        document.querySelectorAll('#ruleModal [data-rl-notrim]').forEach(el => el.hidden = k === 'trim');
    },

    _ruleTargetChanged(keep = '') {
        const key = document.getElementById('rlTarget')?.value;
        const tg = (this._ruleEdit?.targets || []).find(x => x.key === key);
        const sel = document.getElementById('rlCommand');
        if (!sel) return;
        // back on the rule's own target: its own command, not the first listed
        const own = this._ruleEdit?.base?.target || {};
        const ownKey = own.device ? `d:${own.device}` : own.endpoint ? `g:${own.endpoint}/${own.group}` : '';
        if (!keep && key === ownKey) keep = own.command || '';
        const cmds = tg ? tg.commands : [];
        sel.innerHTML = cmds.map(c => `<option value="${this._esc(c)}" ${c === keep ? 'selected' : ''}${tg.notOffered === c ? ' data-not-offered' : ''}>${this._esc(c)}${tg.notOffered === c ? ` — ${this._esc(this.t('rules.notOfferedNow', 'not offered now'))}` : ''}</option>`).join('');
    },

    // the opt-in keys (ADR-0012) in words on the card, or nothing
    _ruleTuningLine(r) {
        const t = (k, d) => this.t(k, d), tm = r.timing || {}, out = [];
        if (tm.boot_wait_s) out.push(t('rules.adv.cardBootWait', 'after a restart waits up to {s} s for the read-back').replace('{s}', this._esc(tm.boot_wait_s)));
        if (r.resume_on_boot) out.push(t('rules.adv.cardResume', 'resumes the step the device holds'));
        if (tm.urgent_retries) out.push(t('rules.adv.cardRetries', 'urgent wants sent again up to {n}×').replace('{n}', this._esc(tm.urgent_retries)));
        return out.length ? `<br>${out.join(' · ')}` : '';
    },

    // every key the editor renders — removed from the stored rule before the
    // form writes them back, so clearing a field clears it; the keys the form
    // never shows (target extras, a schedule row's extras) ride along
    _RULE_FORM_KEYS: ['label', 'kind', 'layer', 'priority', 'params', 'stale_after_s', 'on_stale', 'on_disable',
                      'off_value', 'signal', 'signal_unit', 'steps', 'release_below', 'normal', 'when', 'release',
                      'then', 'else', 'schedule', 'above', 'below', 'step', 'gain', 'min', 'max', 'start',
                      'resume_on_boot'],
    _RULE_TIMING_KEYS: ['every_s', 'debounce', 'min_interval_s', 'reassert_s', 'for_s', 'min_on_s', 'min_off_s',
                        'max_on_s', 'max_starts_per_h', 'boot_wait_s', 'urgent_retries'],
    // what belongs to one kind alone (signal_valid is shared by steps and trim)
    _RULE_KIND_KEYS: { steps: ['signal_valid'], trim: ['signal_valid'], schedule: [], condition: [] },

    // name=value, … → {name: number}; anything else is an error, never a null
    _ruleKv(text, field, errors) {
        const out = {};
        for (const part of String(text || '').split(',').map(x => x.trim()).filter(Boolean)) {
            const i = part.indexOf('=');
            const k = (i < 0 ? '' : part.slice(0, i)).trim(), raw = (i < 0 ? part : part.slice(i + 1)).trim();
            const n = raw === '' ? NaN : Number(raw);
            if (!k || !Number.isFinite(n)) {
                errors.push(this.t('rules.err.kv', '{field}: write name=number, separated by commas ("{part}")')
                    .replace('{field}', field).replace('{part}', part));
                continue;
            }
            out[k] = n;
        }
        return out;
    },

    _ruleNum(text, field, errors, { required = true } = {}) {
        const s = String(text ?? '').trim();
        if (s === '') {
            if (required) errors.push(this.t('rules.err.number', '{field}: a number is required').replace('{field}', field));
            return null;
        }
        const n = Number(s);
        if (!Number.isFinite(n)) {
            errors.push(this.t('rules.err.number', '{field}: a number is required').replace('{field}', field));
            return null;
        }
        return n;
    },

    // The rule the form describes: the stored rule, minus every key the form
    // renders, plus what the form says. Errors are collected in
    // this._ruleFormErrors (the caller shows them and sends nothing).
    _ruleFromForm() {
        const t = (k, d) => this.t(k, d);
        const v = id => (document.getElementById(id) || {}).value;
        const errors = [];
        const lbl = id => (document.querySelector(`label[for="${id}"], label[data-for-select="${id}"]`)?.textContent || id).trim();
        const prev = this._ruleEdit?.id ? (this._rules || []).find(x => x.id === this._ruleEdit.id) : null;
        const raw = JSON.parse(JSON.stringify(this._ruleEdit?.base || prev?.raw || {}));
        const kind = v('rlKind');
        // a kind change leaves the old kind's own keys behind
        if (raw.kind && raw.kind !== kind) {
            for (const k of this._RULE_KIND_KEYS[raw.kind] || []) {
                if (!(this._RULE_KIND_KEYS[kind] || []).includes(k)) delete raw[k];
            }
        }
        for (const k of this._RULE_FORM_KEYS) delete raw[k];
        if (kind === 'steps') delete raw.signal_valid;            // the steps form shows it
        const timing = { ...(raw.timing || {}) };
        for (const k of this._RULE_TIMING_KEYS) delete timing[k];

        const tg = (this._ruleEdit?.targets || []).find(x => x.key === v('rlTarget'));
        const command = v('rlCommand');
        const oldT = raw.target || {};
        const sameKey = tg && (oldT.device ? `d:${oldT.device}` : oldT.endpoint ? `g:${oldT.endpoint}/${oldT.group}` : '') === tg.key;
        // the parameter belongs to the command: kept while the command is the same
        const target = { ...(sameKey ? oldT : {}), ...(tg ? tg.target : {}), command };
        if (oldT.param && oldT.command === command) target.param = oldT.param;
        else if (!sameKey || oldT.command !== command) delete target.param;

        const out = {
            ...raw,
            id: (this._ruleEdit?.id || v('rlId') || '').trim().toLowerCase(),
            label: v('rlLabel') || '', kind, target,
            params: this._ruleKv(v('rlParams'), lbl('rlParams'), errors),
            stale_after_s: Number(v('rlStale')) || 60,
            on_stale: v('rlOnStale') === 'value' ? this._ruleNum(v('rlStaleValue'), lbl('rlOnStale'), errors) : v('rlOnStale'),
            on_disable: v('rlOnDisable'),
            timing: { ...timing, every_s: Number(v('rlEvery')) || 2, debounce: Number(v('rlDebounce')) || 3,
                      min_interval_s: Number(v('rlMinInt')) || 0, reassert_s: Number(v('rlReassert')) || 0 },
        };
        if (v('rlLayer') && v('rlLayer') !== 'schedule') out.layer = v('rlLayer');
        if ((v('rlPriority') || '').trim() !== '') out.priority = this._ruleNum(v('rlPriority'), lbl('rlPriority'), errors);
        for (const [k, id] of [['for_s', 'rlForS'], ['min_on_s', 'rlMinOn'], ['min_off_s', 'rlMinOff'], ['max_on_s', 'rlMaxOn'], ['max_starts_per_h', 'rlMaxStarts']]) {
            const n = Number(v(id));
            if (n > 0) out.timing[k] = n;
        }
        if (kind !== 'trim' && (v('rlOff') || '').trim() !== '') out.off_value = this._ruleNum(v('rlOff'), lbl('rlOff'), errors);
        if (kind === 'steps') {
            out.signal = v('rlSignal');
            if ((v('rlSignalUnit') || '').trim()) out.signal_unit = v('rlSignalUnit').trim();
            out.steps = [...document.querySelectorAll('#rlSteps tbody tr')].map((tr, i) => {
                const row = `${t('rules.steps', 'Steps')} ${i + 1}`;
                const st = { at: this._ruleNum(tr.querySelector('[data-st="at"]').value, `${row} · ${t('rules.stepAt', 'Signal at or above')}`, errors),
                             value: this._ruleNum(tr.querySelector('[data-st="value"]').value, `${row} · ${t('rules.stepValue', 'Ask for')}`, errors) };
                const l = tr.querySelector('[data-st="label"]').value.trim();
                if (l) st.label = l;
                if (tr.querySelector('[data-st="fast"]').checked) st.fast = true;
                return st;
            });
            out.release_below = v('rlRelease') === '' ? null : this._ruleNum(v('rlRelease'), lbl('rlRelease'), errors);
            if (document.getElementById('rlNormalNone')?.checked) out.normal = null;
            else {
                out.normal = this._ruleKv(v('rlNormal'), lbl('rlNormal'), errors);
                if (!Object.keys(out.normal).length) errors.push(t('rules.err.normal', 'Normal asks for: give a value (value=100), or tick "No opinion".'));
            }
            // the opt-in keys (ADR-0012): written only when set
            const bw = (v('rlBootWait') || '').trim(), ur = (v('rlUrgentRetries') || '').trim();
            if (bw !== '' && Number(bw) !== 0) out.timing.boot_wait_s = this._ruleNum(bw, lbl('rlBootWait'), errors);
            if (ur !== '' && Number(ur) !== 0) out.timing.urgent_retries = this._ruleNum(ur, lbl('rlUrgentRetries'), errors);
            if (document.getElementById('rlResumeOnBoot')?.checked) out.resume_on_boot = true;
            const vmin = v('rlValidMin'), vmax = v('rlValidMax'), vstep = v('rlValidStep');
            if (vmin !== '' || vmax !== '' || vstep !== '') out.signal_valid = {
                ...(vmin !== '' ? { min: Number(vmin) } : {}), ...(vmax !== '' ? { max: Number(vmax) } : {}),
                ...(vstep !== '' ? { max_step: Number(vstep) } : {}) };
        } else if (kind === 'schedule') {
            out.schedule = [...document.querySelectorAll('#rlSched tbody tr')].map((tr, i) => {
                let extra = {};
                try { extra = JSON.parse(tr.dataset.extra || '{}'); } catch (e) { extra = {}; }
                const e = { ...extra, days: tr.querySelector('[data-sc="days"]').value.trim() || 'all',
                            at: tr.querySelector('[data-sc="at"]').value };
                const row = `${t('rules.scheduleEntries', 'Entries')} ${i + 1}`;
                const val = tr.querySelector('[data-sc="value"]').value.trim();
                if (tr.querySelector('[data-sc="none"]').checked) e.value = null;
                else if (val.includes('=')) e.params = this._ruleKv(val, row, errors);
                else e.value = this._ruleNum(val, `${row} · ${t('rules.stepValue', 'Ask for')}`, errors);
                const l = tr.querySelector('[data-sc="label"]').value.trim();
                if (l) e.label = l;
                return e;
            });
            if ((v('rlSchedWhen') || '').trim()) out.when = v('rlSchedWhen').trim();
        } else if (kind === 'trim') {
            out.signal = v('rlTrimSignal');
            if ((v('rlTrimSignalUnit') || '').trim()) out.signal_unit = v('rlTrimSignalUnit').trim();
            for (const [k, id] of [['above', 'rlTrimAbove'], ['below', 'rlTrimBelow'], ['step', 'rlTrimStep'], ['min', 'rlTrimMin'], ['max', 'rlTrimMax']])
                out[k] = this._ruleNum(v(id), lbl(id), errors);
            for (const [k, id] of [['gain', 'rlTrimGain'], ['start', 'rlTrimStart'], ['off_value', 'rlTrimOff']])
                if ((v(id) || '').trim() !== '') out[k] = this._ruleNum(v(id), lbl(id), errors);
        } else {
            out.when = v('rlWhen');
            if (v('rlUntil').trim()) out.release = v('rlUntil').trim();
            // a condition's empty side is its documented "nothing" (placeholder says so)
            out.then = v('rlThen').trim() ? { params: this._ruleKv(v('rlThen'), lbl('rlThen'), errors) } : null;
            out.else = v('rlElse').trim() ? { params: this._ruleKv(v('rlElse'), lbl('rlElse'), errors) } : null;
        }
        out.mode = prev ? prev.mode : 'shadow';
        out.enabled = prev ? prev.enabled : true;
        this._ruleFormErrors = errors;
        return out;
    },

    async previewRule() {
        const t = (k, d) => this.t(k, d);
        const out = document.getElementById('rlPreview');
        const body = this._ruleFromForm();
        if (this._ruleFormErrors.length) {
            out.innerHTML = `<span style="color:var(--danger,#c0392b);">${this._esc(this._ruleFormErrors.join(' · '))}</span>`;
            return;
        }
        try {
            const p = await this._ruleOp('/api/rules/validate', body);
            if (p.errors) { out.innerHTML = `<span style="color:var(--danger,#c0392b);">${this._esc(p.errors.map(e => this._ruleErrText(e)).join(' · '))}</span>`; return; }
            const units = Object.entries(p.units || {}).map(([d, u]) => `${d}: ${u.actual ?? '—'}`).join(', ');
            const ins = p.inputs || [];
            // "no signal" says why when the formula names what is not here
            const sig = p.signal == null
                ? (ins.length ? `${t('rules.noSignal', 'no signal')}: ${this._ruleInputText(ins[0])}` : t('rules.noSignal', 'no signal'))
                : this._fmtSig(p.signal);
            out.innerHTML = this._ruleInputsHtml(ins, 'warn') + this._esc(`${t('rules.now', 'Now')}: ${sig} → ${p.state}, ${t('rules.want', 'Want')} ${this._fmtWant(p.want)}${units ? ` · ${t('rules.actual', 'Actual')} ${units}` : ''}${p.stale ? ` · ${t('rules.staleNow', 'stale')}` : ''}`);
        } catch (e) { out.innerHTML = `<span style="color:var(--danger,#c0392b);">${this._esc(this._ruleErrText(e.message))}</span>`; }
    },

    async saveRule() {
        const fb = document.getElementById('ruleFeedback');
        const body = this._ruleFromForm();
        if (!body.id) { fb.textContent = this.t('rules.needId', 'Rule ID is required.'); return; }
        if (this._ruleFormErrors.length) { fb.textContent = this._ruleFormErrors.join(' · '); return; }
        const prev = this._ruleEdit?.id ? (this._rules || []).find(x => x.id === this._ruleEdit.id) : null;
        const canon = o => this._ruleCanon(o);
        // nothing changed: the stored rule, or what the untouched form sent
        if (prev && (canon(body) === canon(prev.raw || {}) || canon(body) === this._ruleEdit.loaded)) { this.closeModal('ruleModal'); return; }
        if (prev && prev.mode === 'armed' && prev.enabled !== false) {
            // an armed rule's new settings act at once: say so, and say it
            // louder when a value it asked for becomes "no opinion"
            const old = prev.raw || {};
            const lost = (old.normal && body.normal === null)
                || (old.schedule || []).some((e, i) => e.value !== null && body.schedule?.[i]?.value === null && !body.schedule?.[i]?.params);
            let msg = this.t('rules.armedSaveConfirm', 'This rule is armed: the new settings drive {target} at once.')
                .replace('{target}', this._ruleTargetText(prev));
            if (lost) msg += '\n\n' + this.t('rules.armedNoOpinionConfirm',
                'A value it asked for becomes "no opinion": nothing will ask for it any more, and the target keeps its last value until another rule or an operator sets it.');
            if (!await this.confirmAction({
                title: this.t('rules.armedSaveTitle', 'Save an armed rule'),
                message: msg, confirmLabel: this.t('common.save', 'Save'),
            })) return;
        }
        let d;
        try {
            if (this._ruleEdit?.id) d = await this._ruleOp(`/api/rules/${encodeURIComponent(this._ruleEdit.id)}`, body, 'PUT');
            else d = await this._ruleOp('/api/rules', body);
        } catch (e) {
            this._ruleFeedbackErrors(Array.isArray(e.detail?.errors) ? e.detail.errors : [e.message]);
            return;
        }
        if (prev) this._toastReleasing(prev, d.releasing, d.release_warning);
        this._loadRules();
        const ins = d.rule?.inputs || [];
        if (ins.length) {
            // saved, but it reads what is not here: it can only be stale —
            // said, and the editor stays open to fix it
            if (d.rule?.id && !this._ruleEdit.id) {
                // saved under this id: a later edit of the box would be ignored
                this._ruleEdit.id = d.rule.id;
                const idBox = document.getElementById('rlId');
                if (idBox) idBox.disabled = true;
            }
            fb.innerHTML = this._ruleInputsHtml(ins, 'warn');
            this.showToast('warning', this.t('rules.savedInputsTitle', 'Saved — it reads names this gateway does not have'),
                ins.map(p => this._ruleInputText(p)).join('\n'), 10000);
            return;
        }
        this.closeModal('ruleModal');
    },

    // ── import: preview what would happen to each rule, then apply ────────
    openRulesImport() {
        const t = (k, d) => this.t(k, d);
        this._rulesImportText = '';
        document.getElementById('rulesImportText').value = '';
        document.getElementById('rulesImportReplace').checked = false;
        document.getElementById('rulesImportResult').innerHTML = '';
        document.getElementById('rulesImportApply').disabled = true;
        const file = document.getElementById('rulesImportFile');
        if (file && !file._wired) {
            file._wired = true;
            file.addEventListener('change', async () => {
                const f = file.files[0]; file.value = '';
                if (f) { document.getElementById('rulesImportText').value = await f.text(); this.rulesImportPreview(); }
            });
        }
        this.openModal('rulesImportModal');
    },

    async _rulesImportPost(apply) {
        const text = document.getElementById('rulesImportText').value;
        const replace = document.getElementById('rulesImportReplace').checked;
        const r = await fetch('/api/rules/import', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ yaml: text, apply, replace }) });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error((d.detail?.errors || [r.statusText]).join(' · '));
        return d;
    },

    async rulesImportPreview() {
        const t = (k, d, p) => this.t(k, d, p);
        const box = document.getElementById('rulesImportResult');
        const apply = document.getElementById('rulesImportApply');
        if (!document.getElementById('rulesImportText').value.trim()) {
            box.innerHTML = `<p class="field-hint">${this._esc(t('rules.import.empty', 'Paste the YAML or open a file first.'))}</p>`;
            return;
        }
        try {
            const d = await this._rulesImportPost(false);
            box.innerHTML = this._rulesImportTable(d);
            apply.disabled = !d.ok;
            apply.textContent = this.tn('rules.import.applyN', d.ok, 'Import {n} rules in shadow');
        } catch (e) {
            box.innerHTML = `<div class="field-error">${this._esc(String(e.message || e))}</div>`;
            apply.disabled = true;
        }
    },

    async rulesImportApply() {
        const t = (k, d, p) => this.t(k, d, p);
        try {
            const d = await this._rulesImportPost(true);
            document.getElementById('rulesImportResult').innerHTML = this._rulesImportTable(d);
            this.showToast('success', t('rules.import.done', 'Rules imported'),
                t('rules.import.doneDetail', '{n} of {m} imported — in shadow until you arm them', { n: d.ok, m: d.total }));
            // a rule from another installation names its devices: what is not
            // here is said now, before anyone arms it
            const unknown = (d.rules || []).filter(x => (x.inputs || []).length && ['created', 'replaced'].includes(x.status));
            if (unknown.length) this.showToast('warning', t('rules.import.inputsTitle', 'Some imported rules read names this gateway does not have'),
                unknown.map(x => `${x.id}: ${x.inputs.map(p => this._ruleInputText(p)).join('; ')}`).join('\n'), 15000);
            if (d.ok === d.total && !unknown.length) this.closeModal('rulesImportModal');
            this._loadRules(true);
        } catch (e) {
            this.showToast('error', t('rules.import.failed', 'Import failed'), String(e.message || e));
        }
    },

    _rulesImportTable(d) {
        const t = (k, def) => this.t(k, def);
        const WORD = {
            new: ['ok', t('rules.import.new', 'new')], replace: ['warn', t('rules.import.replace', 'replaces the one here')],
            created: ['ok', t('rules.import.created', 'imported')], replaced: ['ok', t('rules.import.replaced', 'replaced')],
            exists: ['warn', t('rules.import.exists', 'already here')], invalid: ['bad', t('rules.import.invalid', 'cannot import')],
        };
        return `<table class="tplu-table"><thead><tr>
            <th>${this._esc(t('rules.import.rule', 'Rule'))}</th><th>${this._esc(t('rules.import.what', 'What happens'))}</th><th></th></tr></thead><tbody>
            ${d.rules.map(x => {
                const [cls, word] = WORD[x.status] || ['', x.status];
                return `<tr><td style="white-space:nowrap;"><code>${this._esc(x.id)}</code>${x.label ? ' ' + this._esc(x.label) : ''}</td>
                    <td><span class="sink-pill ${cls}">${this._esc(word)}</span>${x.was_armed ? ` <span class="field-hint">${this._esc(t('rules.import.wasArmed', 'armed in the file — arrives in shadow'))}</span>` : ''}</td>
                    <td class="field-hint">${(x.errors || []).map(e => this._esc(e)).join('<br>')}${(x.inputs || []).length ? `<div data-rule-import-inputs style="color:var(--warning,#b7791f);">${(x.inputs || []).map(p => this._esc(this._ruleInputText(p))).join('<br>')}</div>` : ''}</td></tr>`;
            }).join('')}</tbody></table>`;
    },
});
