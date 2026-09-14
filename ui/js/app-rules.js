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
        for (const [id, fn] of [['rulesAddBtn', () => this.openRuleModal('')], ['rulesRefreshBtn', () => this.renderRulesPage()]]) {
            const b = document.getElementById(id);
            if (b && !b._wired) { b._wired = true; b.addEventListener('click', fn); }
        }
        await this._loadRules();
        this._rulesTimer = setInterval(() => this._loadRules(true), 5000);
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
            el.innerHTML = `<div class="settings-card"><div class="settings-card-body"><p class="field-hint" style="margin:0;">${t('rules.none',
                'No rules yet. A rule watches live values and asks a device for what it wants — an over-voltage limit, a restore when the grid is gone. Add one; it starts in shadow.')}</p></div></div>`;
            return;
        }
        el.innerHTML = rules.map(r => this._ruleCardHtml(r, open.has(r.id))).join('');
        for (const id of open) this._loadRuleDecisions(id);
    },

    _ruleStateDot(r) {
        const st = r.live?.state;
        if (st === 'stale') return 'var(--text-secondary,#888)';
        if (st === 'normal' || st === 'false') return 'var(--success,#22c55e)';
        if (r.kind === 'steps') {
            const i = (r.steps || []).findIndex(s => s.label === st);
            return i >= (r.steps || []).length - 1 ? 'var(--danger,#ef4444)' : 'var(--warning,#f59e0b)';
        }
        return 'var(--warning,#f59e0b)';
    },

    _fmtWant(w) {
        if (!w) return '—';
        return Object.entries(w).map(([k, v]) => `${k} ${v}`).join(', ');
    },

    _ruleTargetText(r) {
        const tg = r.target || {};
        return tg.device ? `${tg.device} · ${tg.command}` : `${tg.endpoint} / ${tg.group} · ${tg.command}`;
    },

    _ruleCardHtml(r, decisionsOpen) {
        const t = (k, d) => this.t(k, d);
        const lv = r.live || {};
        const armed = r.mode === 'armed';
        const units = Object.entries(lv.units || {});
        const last = lv.last;
        const unitsHtml = units.map(([dev, u]) => {
            const paused = u.paused_until && u.paused_until * 1000 > Date.now();
            return `<tr>
                <td><code>${this._esc(dev)}</code></td>
                <td>${this._esc(u.state || '')}</td>
                <td>${this._esc(this._fmtWant(u.want))}</td>
                <td>${u.actual != null ? `${u.actual}${u.actual_age_s != null ? ` <span class="field-hint">(${Math.round(u.actual_age_s)} s)</span>` : ''}` : '—'}</td>
                <td>${u.clamp ? `≤ ${u.clamp.max}${u.clamp.expires_at ? ` ${t('rules.until', 'until')} ${new Date(u.clamp.expires_at * 1000).toLocaleTimeString()}` : ''}` : ''}
                    ${paused ? `<span class="sink-pill warn">${t('rules.paused', 'paused')} ${t('rules.until', 'until')} ${new Date(u.paused_until * 1000).toLocaleTimeString()}</span>` : ''}
                    ${u.failures ? `<span class="sink-pill bad">${u.failures} ${t('rules.failures', 'failures')}</span>` : ''}</td></tr>`;
        }).join('');
        return `
        <div class="settings-card" data-rule="${this._esc(r.id)}">
            <div class="settings-card-header" style="flex-wrap:wrap;gap:8px;">
                <h3 style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                    <span class="status-dot" style="--dot:${this._ruleStateDot(r)}" aria-hidden="true"></span>
                    ${this._esc(r.label || r.id)} <span class="dev-chip">${this._esc(r.id)}</span>
                    <span class="sink-pill ${armed ? 'ok' : 'warn'}">${armed ? t('rules.armed', 'Armed') : t('rules.shadow', 'Shadow')}</span>
                    ${r.enabled ? '' : `<span class="sink-pill off">${t('devices.disabled', 'disabled')}</span>`}
                    ${lv.error ? `<span class="sink-pill bad" title="${this._esc(lv.error)}">${t('rules.cannotRun', 'cannot run')}</span>` : ''}
                </h3>
                <div class="header-actions" style="display:flex;gap:6px;flex-wrap:wrap;">
                    <button class="btn btn-sm ${armed ? 'btn-secondary' : 'btn-primary'}" ${this._act('setRuleMode', [r.id, armed ? 'shadow' : 'armed'])} ${lv.error && !armed ? 'disabled' : ''}>
                        <i aria-hidden="true" class="bi ${armed ? 'bi-eye' : 'bi-shield-check'}"></i> ${armed ? t('rules.toShadow', 'To shadow') : t('rules.arm', 'Arm…')}</button>
                    <button class="btn btn-ghost btn-sm" ${this._act('openRuleClamp', [r.id])} title="${t('rules.clampHint', 'A ceiling on what the rule may ask for, for a stated time')}"><i aria-hidden="true" class="bi bi-arrow-down-square"></i> ${t('rules.clamp', 'Clamp…')}</button>
                    <button class="btn btn-ghost btn-sm" ${this._act('openRuleOverride', [r.id])} title="${t('rules.overrideHint', 'Pause the rule so you or another system can command its target')}"><i aria-hidden="true" class="bi bi-pause-circle"></i> ${t('rules.override', 'Pause…')}</button>
                    <button class="btn btn-ghost btn-sm" ${this._act('toggleRuleDecisions', [r.id])}><i aria-hidden="true" class="bi bi-journal-text"></i> ${t('rules.decisions', 'Decisions')}</button>
                    <button class="btn btn-ghost btn-sm" ${this._act('openRuleModal', [r.id])} title="${t('common.edit', 'Edit')}" aria-label="${t('common.edit', 'Edit')}"><i aria-hidden="true" class="bi bi-pencil"></i></button>
                    <button class="btn btn-ghost btn-sm" ${this._act('deleteRule', [r.id])} title="${t('common.delete', 'Delete')}" aria-label="${t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-trash"></i></button>
                </div>
            </div>
            <div class="settings-card-body">
                ${lv.error ? `<p style="margin:0 0 8px;color:var(--danger,#c0392b);">${this._esc(lv.error)}</p>` : ''}
                <div style="display:flex;gap:18px;flex-wrap:wrap;font-size:13px;margin-bottom:8px;">
                    <span><b>${t('rules.target', 'Target')}</b>: ${this._esc(this._ruleTargetText(r))}</span>
                    <span><b>${t('rules.state', 'State')}</b>: ${this._esc(lv.state || '—')}</span>
                    <span><b>${t('rules.signal', 'Signal')}</b>: ${lv.signal == null ? '—' : this._esc(typeof lv.signal === 'number' ? lv.signal.toFixed(1) : String(lv.signal))}
                        <span class="field-hint">${this._esc(r.kind === 'steps' ? r.signal : r.when)}</span></span>
                </div>
                ${r.kind === 'steps' ? `<div class="field-hint" style="margin-bottom:8px;">${t('rules.stepsLine', 'Steps')}: ${(r.steps || []).map(s => `${s.label} ≥ ${s.at} → ${s.value}${s.fast ? ' ⚡' : ''}`).join(' · ')} · ${t('rules.releaseLine', 'normal under')} ${r.release_below} → ${this._fmtWant(r.normal)}</div>`
                                     : `<div class="field-hint" style="margin-bottom:8px;">${t('rules.whenTrue', 'when true')}: ${this._esc(this._fmtWant(r.then?.params))} · ${t('rules.whenFalse', 'when false')}: ${this._esc(r.else ? this._fmtWant(r.else.params) : t('rules.nothing', 'nothing'))}</div>`}
                ${units.length ? `<div class="table-wrap"><table class="table"><thead><tr>
                    <th>${t('rules.unit', 'Unit')}</th><th>${t('rules.state', 'State')}</th><th>${t('rules.want', 'Want')}</th><th>${t('rules.actual', 'Actual')}</th><th></th></tr></thead>
                    <tbody>${unitsHtml}</tbody></table></div>` : ''}
                <div style="margin-top:8px;font-size:12.5px;">
                    ${last ? `<b>${t('rules.lastDecision', 'Last decision')}</b>: ${this._esc(last.action)} — ${this._esc(last.reason || '')} <span class="field-hint">${new Date(last.ts * 1000).toLocaleTimeString()}${last.result ? ` · ${this._esc(last.result)}` : ''}</span>`
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
        let rows = [];
        try { rows = (await (await fetch(`/api/rules/${encodeURIComponent(id)}/decisions?limit=50`)).json()).decisions || []; } catch (e) { return; }
        box.innerHTML = rows.length ? `<div class="table-wrap"><table class="table"><thead><tr>
            <th>${t('rules.h.when', 'When')}</th><th>${t('rules.unit', 'Unit')}</th><th>${t('rules.signal', 'Signal')}</th><th>${t('rules.state', 'State')}</th>
            <th>${t('rules.want', 'Want')}</th><th>${t('rules.actual', 'Actual')}</th><th>${t('rules.h.decision', 'Decision')}</th></tr></thead><tbody>
            ${rows.map(d => `<tr><td>${new Date(d.ts * 1000).toLocaleTimeString()}</td><td><code>${this._esc(d.device)}</code></td>
                <td>${d.signal == null ? '—' : this._esc(typeof d.signal === 'number' ? d.signal.toFixed(1) : String(d.signal))}</td>
                <td>${this._esc(d.state)}</td><td>${this._esc(this._fmtWant(d.want))}</td><td>${d.actual == null ? '—' : d.actual}</td>
                <td><b>${this._esc(d.action)}</b> — ${this._esc(d.reason || '')}${d.result ? ` · ${this._esc(d.result)}${d.result_reason ? ` (${this._esc(d.result_reason)})` : ''}` : ''}</td></tr>`).join('')}
            </tbody></table></div>` : `<div class="field-hint">${t('rules.noDecision', 'No decision yet.')}</div>`;
    },

    async _ruleOp(url, body, method = 'POST') {
        const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error((d.detail?.errors || [d.detail || d.reason || r.statusText]).join(' · '));
        return d;
    },

    async setRuleMode(id, mode) {
        const t = (k, d) => this.t(k, d);
        const r = (this._rules || []).find(x => x.id === id);
        if (!r) return;
        if (mode === 'armed' && !confirm(`${t('rules.armConfirm', 'Arm this rule? It will write to')} ${this._ruleTargetText(r)}.`)) return;
        try { await this._ruleOp(`/api/rules/${encodeURIComponent(id)}/mode`, { mode }); }
        catch (e) { alert(e.message); }
        this._loadRules();
    },

    async deleteRule(id) {
        const t = (k, d) => this.t(k, d);
        const r = (this._rules || []).find(x => x.id === id);
        if (!r || !confirm(`${t('rules.deleteConfirm', 'Delete rule')} ${r.label || id}?${r.mode === 'armed' ? ' ' + t('rules.deleteArmed', 'It is armed: its target is restored to the safe values first.') : ''}`)) return;
        try { await this._ruleOp(`/api/rules/${encodeURIComponent(id)}`, null, 'DELETE'); }
        catch (e) { alert(e.message); }
        this._loadRules();
    },

    async openRuleClamp(id) {
        const t = (k, d) => this.t(k, d);
        const v = prompt(t('rules.clampAsk', 'Ceiling for what the rule may ask (empty clears it):'), '');
        if (v === null) return;
        try {
            if (v.trim() === '') await this._ruleOp(`/api/rules/${encodeURIComponent(id)}/clamp`, null, 'DELETE');
            else {
                const h = prompt(t('rules.clampHours', 'For how many hours? (empty = until cleared)'), '2');
                if (h === null) return;
                await this._ruleOp(`/api/rules/${encodeURIComponent(id)}/clamp`, { max: Number(v), expires_s: h.trim() ? Number(h) * 3600 : null });
            }
        } catch (e) { alert(e.message); }
        this._loadRules();
    },

    async openRuleOverride(id) {
        const t = (k, d) => this.t(k, d);
        const m = prompt(t('rules.overrideAsk', 'Pause the rule for how many minutes? (0 lifts the pause)'), '15');
        if (m === null) return;
        try { await this._ruleOp(`/api/rules/${encodeURIComponent(id)}/override`, { seconds: Number(m) * 60 }); }
        catch (e) { alert(e.message); }
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

    async openRuleModal(id) {
        const t = (k, d) => this.t(k, d);
        const r = id ? (this._rules || []).find(x => x.id === id) : null;
        const raw = r ? (r.raw || r) : null;
        this._ruleEdit = { id: r ? r.id : '', targets: await this._ruleTargets() };
        const tg = raw?.target || {};
        const curKey = tg.device ? `d:${tg.device}` : tg.endpoint ? `g:${tg.endpoint}/${tg.group}` : '';
        const kind = raw?.kind || 'steps';
        const tm = raw?.timing || {};
        const kv = o => Object.entries(o || {}).map(([k, v]) => `${k}=${v}`).join(', ');
        document.getElementById('ruleModalTitle').textContent = r ? t('rules.editTitle', 'Edit rule') : t('rules.addTitle', 'Add rule');
        document.getElementById('ruleModalBody').innerHTML = `
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="rlId">${t('rules.id', 'Rule ID')}</label>
                    <input id="rlId" class="input" value="${this._esc(raw?.id || '')}" ${r ? 'disabled' : ''} placeholder="ov-u1"></div>
                <div class="form-group flex-2"><label class="form-label" for="rlLabel">${t('rules.label', 'Name')}</label>
                    <input id="rlLabel" class="input" value="${this._esc(raw?.label || '')}" placeholder="${t('rules.labelPh', 'Over-voltage protection · inverter 1')}"></div>
                <div class="form-group"><label class="form-label" for="rlKind">${t('rules.kind', 'Kind')}</label>
                    <select id="rlKind" class="input" onchange="app._ruleKindChanged()">
                        <option value="steps" ${kind === 'steps' ? 'selected' : ''}>${t('rules.kindSteps', 'Steps (thresholds → value)')}</option>
                        <option value="condition" ${kind === 'condition' ? 'selected' : ''}>${t('rules.kindCondition', 'Condition (true / false)')}</option>
                    </select></div>
            </div>
            <div class="form-row">
                <div class="form-group flex-2"><label class="form-label" for="rlTarget">${t('rules.target', 'Target')}</label>
                    <select id="rlTarget" class="input" onchange="app._ruleTargetChanged()">
                        ${this._ruleEdit.targets.map(x => `<option value="${this._esc(x.key)}" ${x.key === curKey ? 'selected' : ''}>${this._esc(x.label)}</option>`).join('')}
                    </select>
                    <div class="field-hint">${t('rules.targetHint', 'A group applies the command to every unit; a unit, to that one. Only targets with an enabled command are listed.')}</div></div>
                <div class="form-group"><label class="form-label" for="rlCommand">${t('commands.which', 'Command')}</label>
                    <select id="rlCommand" class="input"></select></div>
            </div>
            <div data-rl-kind="steps" ${kind === 'steps' ? '' : 'hidden'}>
                <div class="form-group"><label class="form-label" for="rlSignal">${t('rules.signal', 'Signal')}</label>
                    <input id="rlSignal" class="input" value="${this._esc(raw?.signal || '')}" placeholder="max(pv-u1.voltage_l1_n, pv-u1.voltage_l2_n, pv-u1.voltage_l3_n)" style="font-family:monospace;">
                    <div class="field-hint">${t('rules.signalHint', 'An expression over live values, written device.register — the same grammar as calculated registers (min, max, avg, abs, + - * /).')}</div></div>
                <div class="form-group"><label class="form-label">${t('rules.steps', 'Steps')}</label>
                    <div class="table-wrap"><table class="table" id="rlSteps"><thead><tr><th>${t('rules.stepAt', 'Signal at or above')}</th><th>${t('rules.stepValue', 'Ask for')}</th><th>${t('rules.stepLabel', 'Label')}</th><th>${t('rules.stepFast', 'Immediate')}</th><th></th></tr></thead>
                    <tbody>${(raw?.steps || [{ at: 250, value: 80, label: 'Warning' }]).map(s => this._ruleStepRow(s)).join('')}</tbody></table></div>
                    <button type="button" class="btn btn-ghost btn-sm" onclick="app._ruleAddStep()"><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('rules.addStep', 'Add step')}</button>
                    <div class="field-hint">${t('rules.stepsHint', 'The highest matching step wins. An immediate step skips the debounce — the emergency path.')}</div></div>
                <div class="form-row">
                    <div class="form-group"><label class="form-label" for="rlRelease">${t('rules.release', 'Normal again under')}</label>
                        <input id="rlRelease" class="input" type="number" step="any" value="${raw?.release_below ?? ''}">
                        <div class="field-hint">${t('rules.releaseHint', 'Between this and the first step the rule holds what it has — the dead band.')}</div></div>
                    <div class="form-group"><label class="form-label" for="rlNormal">${t('rules.normal', 'Normal asks for')}</label>
                        <input id="rlNormal" class="input" value="${this._esc(kv(raw?.normal || { value: 100 }))}" placeholder="value=100"></div>
                    <div class="form-group"><label class="form-label" for="rlValidMin">${t('rules.valid', 'Valid signal range')}</label>
                        <div style="display:flex;gap:6px;"><input id="rlValidMin" class="input" type="number" step="any" placeholder="min" value="${raw?.signal_valid?.min ?? ''}" aria-label="min"><input id="rlValidMax" class="input" type="number" step="any" placeholder="max" value="${raw?.signal_valid?.max ?? ''}" aria-label="max"></div>
                        <div class="field-hint">${t('rules.validHint', 'A sample outside it is ignored as sensor garbage.')}</div></div>
                </div>
            </div>
            <div data-rl-kind="condition" ${kind === 'condition' ? '' : 'hidden'}>
                <div class="form-group"><label class="form-label" for="rlWhen">${t('rules.when', 'When')}</label>
                    <input id="rlWhen" class="input" value="${this._esc(raw?.when || '')}" placeholder="pv-meter.frequency < 45 or pv-meter.voltage_ln_avg < 100" style="font-family:monospace;"></div>
                <div class="form-row">
                    <div class="form-group"><label class="form-label" for="rlThen">${t('rules.then', 'True asks for')}</label>
                        <input id="rlThen" class="input" value="${this._esc(kv(raw?.then?.params))}" placeholder="value=100, revert_s=0"></div>
                    <div class="form-group"><label class="form-label" for="rlElse">${t('rules.else', 'False asks for')}</label>
                        <input id="rlElse" class="input" value="${this._esc(kv(raw?.else?.params))}" placeholder="${t('rules.elsePh', 'empty = nothing')}"></div>
                </div>
            </div>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="rlParams">${t('rules.params', 'Sent with every want')}</label>
                    <input id="rlParams" class="input" value="${this._esc(kv(raw?.params))}" placeholder="revert_s=0, ramp_s=10">
                    <div class="field-hint">${t('rules.paramsHint', 'Fixed parameters of the command, name=value.')}</div></div>
                <div class="form-group"><label class="form-label" for="rlStale">${t('rules.staleAfter', 'Signal stale after (s)')}</label>
                    <input id="rlStale" class="input" type="number" min="1" value="${raw?.stale_after_s ?? 60}"></div>
                <div class="form-group"><label class="form-label" for="rlOnStale">${t('rules.onStale', 'When stale')}</label>
                    <select id="rlOnStale" class="input" onchange="document.getElementById('rlStaleValueWrap').hidden = this.value !== 'value'"><option value="hold" ${(raw?.on_stale || 'hold') === 'hold' ? 'selected' : ''}>${t('rules.hold', 'hold the last want')}</option><option value="safe" ${raw?.on_stale === 'safe' ? 'selected' : ''}>${t('rules.safe', 'ask for the safe values')}</option><option value="value" ${(typeof raw?.on_stale === 'number') ? 'selected' : ''}>${t('rules.staleValue', 'ask for a fixed value (fail closed)')}</option></select>
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
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                <button type="button" class="btn btn-ghost btn-sm" onclick="app.previewRule()"><i aria-hidden="true" class="bi bi-eye"></i> ${t('rules.preview', 'Preview now')}</button>
                <span id="rlPreview" class="field-hint" role="status" aria-live="polite">${t('rules.previewHint', 'What the rule would see and want right now, nothing kept.')}</span>
            </div>
            ${r ? '' : `<p class="field-hint" style="margin:10px 0 0;">${t('rules.newShadow', 'A new rule is saved in shadow: it decides and says so, but writes nothing until you arm it.')}</p>`}`;
        document.getElementById('ruleFeedback').textContent = '';
        this._ruleTargetChanged(tg.command || '');
        this.openModal('ruleModal');
    },

    _ruleStepRow(s = {}) {
        return `<tr>
            <td><input class="input" type="number" step="any" value="${s.at ?? ''}" data-st="at" aria-label="at"></td>
            <td><input class="input" type="number" step="any" value="${s.value ?? ''}" data-st="value" aria-label="value"></td>
            <td><input class="input" value="${this._esc(s.label || '')}" data-st="label" aria-label="label"></td>
            <td style="text-align:center;"><input type="checkbox" ${s.fast ? 'checked' : ''} data-st="fast" aria-label="immediate"></td>
            <td><button type="button" class="btn btn-ghost btn-sm" onclick="this.closest('tr').remove()" aria-label="${this.t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-x-lg"></i></button></td></tr>`;
    },

    _ruleAddStep() {
        document.querySelector('#rlSteps tbody')?.insertAdjacentHTML('beforeend', this._ruleStepRow());
    },

    _ruleKindChanged() {
        const k = document.getElementById('rlKind').value;
        document.querySelectorAll('#ruleModal [data-rl-kind]').forEach(el => el.hidden = el.dataset.rlKind !== k);
    },

    _ruleTargetChanged(keep = '') {
        const key = document.getElementById('rlTarget')?.value;
        const tg = (this._ruleEdit?.targets || []).find(x => x.key === key);
        const sel = document.getElementById('rlCommand');
        if (!sel) return;
        const cmds = tg ? tg.commands : [];
        sel.innerHTML = cmds.map(c => `<option value="${this._esc(c)}" ${c === keep ? 'selected' : ''}>${this._esc(c)}</option>`).join('');
    },

    _ruleFromForm() {
        const v = id => (document.getElementById(id) || {}).value;
        const kv = s => Object.fromEntries((s || '').split(',').map(x => x.trim()).filter(Boolean).map(x => {
            const [k, val] = x.split('=').map(y => y.trim()); return [k, Number(val)];
        }));
        const tg = (this._ruleEdit?.targets || []).find(x => x.key === v('rlTarget'));
        const kind = v('rlKind');
        const out = {
            id: (this._ruleEdit?.id || v('rlId') || '').trim().toLowerCase(),
            label: v('rlLabel') || '', kind,
            target: { ...(tg ? tg.target : {}), command: v('rlCommand') },
            params: kv(v('rlParams')), stale_after_s: Number(v('rlStale')) || 60,
            on_stale: v('rlOnStale') === 'value' ? Number(v('rlStaleValue')) : v('rlOnStale'), on_disable: v('rlOnDisable'),
            timing: { every_s: Number(v('rlEvery')) || 2, debounce: Number(v('rlDebounce')) || 3,
                      min_interval_s: Number(v('rlMinInt')) || 0, reassert_s: Number(v('rlReassert')) || 0 },
        };
        if (kind === 'steps') {
            out.signal = v('rlSignal');
            out.steps = [...document.querySelectorAll('#rlSteps tbody tr')].map(tr => ({
                at: Number(tr.querySelector('[data-st="at"]').value), value: Number(tr.querySelector('[data-st="value"]').value),
                label: tr.querySelector('[data-st="label"]').value.trim(), fast: tr.querySelector('[data-st="fast"]').checked }));
            out.release_below = v('rlRelease') === '' ? null : Number(v('rlRelease'));
            out.normal = kv(v('rlNormal'));
            const vmin = v('rlValidMin'), vmax = v('rlValidMax');
            if (vmin !== '' || vmax !== '') out.signal_valid = { ...(vmin !== '' ? { min: Number(vmin) } : {}), ...(vmax !== '' ? { max: Number(vmax) } : {}) };
        } else {
            out.when = v('rlWhen');
            out.then = v('rlThen').trim() ? { params: kv(v('rlThen')) } : null;
            out.else = v('rlElse').trim() ? { params: kv(v('rlElse')) } : null;
        }
        const prev = this._ruleEdit?.id ? (this._rules || []).find(x => x.id === this._ruleEdit.id) : null;
        out.mode = prev ? prev.mode : 'shadow';
        out.enabled = prev ? prev.enabled : true;
        return out;
    },

    async previewRule() {
        const t = (k, d) => this.t(k, d);
        const out = document.getElementById('rlPreview');
        try {
            const p = await this._ruleOp('/api/rules/validate', this._ruleFromForm());
            if (p.errors) { out.innerHTML = `<span style="color:var(--danger,#c0392b);">${this._esc(p.errors.join(' · '))}</span>`; return; }
            const units = Object.entries(p.units || {}).map(([d, u]) => `${d}: ${u.actual ?? '—'}`).join(', ');
            out.textContent = `${t('rules.now', 'Now')}: ${p.signal == null ? t('rules.noSignal', 'no signal') : (typeof p.signal === 'number' ? p.signal.toFixed(1) : p.signal)} → ${p.state}, ${t('rules.want', 'want')} ${this._fmtWant(p.want)}${units ? ` · ${t('rules.actual', 'actual')} ${units}` : ''}${p.stale ? ` · ${t('rules.staleNow', 'stale')}` : ''}`;
        } catch (e) { out.innerHTML = `<span style="color:var(--danger,#c0392b);">${this._esc(e.message)}</span>`; }
    },

    async saveRule() {
        const fb = document.getElementById('ruleFeedback');
        const body = this._ruleFromForm();
        if (!body.id) { fb.textContent = this.t('rules.needId', 'Rule ID is required.'); return; }
        try {
            if (this._ruleEdit?.id) await this._ruleOp(`/api/rules/${encodeURIComponent(this._ruleEdit.id)}`, body, 'PUT');
            else await this._ruleOp('/api/rules', body);
        } catch (e) { fb.textContent = e.message; return; }
        this.closeModal('ruleModal');
        this._loadRules();
    },
});
