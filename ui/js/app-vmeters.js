// vmeters domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    async renderVirtualMeters() {
        const el = document.getElementById('vmetersContent');
        if (!el) return;
        this._stopVmPolls();                          // re-render → drop stale timers
        const refreshBtn = document.getElementById('vmRefreshBtn');
        if (refreshBtn && !refreshBtn._wired) {
            refreshBtn._wired = true;
            refreshBtn.addEventListener('click', () => this.renderVirtualMeters());
        }
        let data;
        try {
            data = await (await fetch('/api/virtual-meters')).json();
        } catch (e) {
            el.innerHTML = `<p style="color:#c0392b;">${this.t('msg.loadVmeters', "Could not load virtual meters.")}</p>`;
            return;
        }
        const insts = data.instances || [];
        this._vmInsts = Object.fromEntries(insts.map(i => [i.template, i]));   // for the edit modal
        let templates = [];
        try { templates = (await (await fetch('/api/virtual-meters/templates')).json()).templates || []; } catch (e) {}
        // Source devices (Phase 3): a meter re-serves ONE device's live values.
        let devices = [];
        try { devices = (await this._fetchDevices(true)) || []; } catch (e) {}
        const primaryId = this._primaryDeviceId();
        const devName = id => (devices.find(d => d.id === id)?.name) || id;
        const configured = new Set(insts.map(i => i.template));
        const pr = data.port_range || {};
        this._vmPortRange = pr;                       // reused by the template editor
        const opts = templates.filter(t => !configured.has(t.id))
            .map(t => `<option value="${this._esc(t.id)}">${this._esc(t.name)}</option>`).join('');
        const noFree = pr.next_free == null;
        const canAdd = opts && !noFree;
        const portHint = (pr.start != null)
            ? `published range ${pr.start}–${pr.end}${pr.used && pr.used.length ? ' · used: ' + pr.used.join(', ') : ''}`
            : '';
        // add-instance is a modal now; keep the picker data for it
        this._vmAddCtx = { devices, primaryId, templates, configured, pr };
        const addBar = `
            <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap;">
              <button class="btn btn-primary btn-sm" id="vmAddInstanceBtn" ${canAdd ? '' : 'disabled'}>
                <i class="bi bi-plus-lg"></i> ${this.t('vmeter.addInstance', 'Add instance')}</button>
              ${portHint ? `<span style="color:var(--text-secondary);font-size:12px;">${noFree ? '<span style="color:#e08e0b;">No free port in range — widen VMETER_PORT_END.</span> ' : ''}${portHint}</span>` : ''}
            </div>`;
        const cards = !insts.length
            ? '<p style="color:var(--text-secondary);">No virtual meters configured. Use “Add instance”.</p>'
            : insts.map(m => {
            const mid = this._esc(m.template);
            // One status component app-wide (same pill as the Outputs sinks) —
            // LISTENING/STALE/DOWN said six different ways was pure noise.
            const badge = m.state === 'ok'
                ? `<span class="sink-pill ok">${this.t('vmeter.listening', 'listening')}</span>`
                : m.state === 'stale'
                ? `<span class="sink-pill warn" title="source stale — meter stopped responding (consumer fail-safe)">${this.t('vmeter.stale', 'stale')}</span>`
                : m.state === 'down'
                ? `<span class="sink-pill bad" title="enabled but not serving — crashed or failed to start">${this.t('vmeter.down', 'down')}</span>`
                : (m.enabled ? `<span class="sink-pill warn">${this.t('vmeter.starting', 'starting…')}</span>`
                             : `<span class="sink-pill off">${this.t('vmeter.disabled', 'disabled')}</span>`);
            const prev = Object.entries(m.preview || {})
                .map(([k, v]) => `<tr><td style="padding:2px 10px 2px 0;color:var(--text-secondary);">${this._esc(k)}</td>`
                    + `<td style="padding:2px 0;text-align:right;font-variant-numeric:tabular-nums;">`
                    + `${v === null || v === undefined ? '—' : (typeof v === 'number' ? v.toFixed(2) : this._esc(v))}</td></tr>`)
                .join('');
            const conns = m.connections || [];
            const connRows = conns.length
                ? conns.map(c => `<tr><td style="padding:2px 14px 2px 0;color:var(--text-secondary);font-family:monospace;">${this._esc(c.ip)}${c.port ? ':' + c.port : ''}</td>`
                    + `<td style="padding:2px 0;color:var(--text-secondary);font-variant-numeric:tabular-nums;white-space:nowrap;" title="connection uptime">up ${this._dur(c.connected_s)}</td></tr>`).join('')
                : '<tr><td style="color:var(--text-secondary);">no active connections</td></tr>';
            const summary = `:${m.port ?? '—'} · ${conns.length} conn${conns.length === 1 ? '' : 's'}`
                + (m.running ? ` · ${m.requests ?? 0} req · ${m.req_rate ?? 0}/s` : '');
            const t = this.t.bind(this);
            const overview = `
                <div style="display:flex;gap:24px 30px;flex-wrap:wrap;font-size:13px;margin-bottom:14px;">
                  <div><div style="color:var(--text-secondary);font-size:11.5px;">${t('vmeter.sourceDevice', 'Source device')}</div><b>${this._esc(devName(m.device))}</b> <span class="dev-chip">${this._esc(m.device)}</span></div>
                  <div><div style="color:var(--text-secondary);font-size:11.5px;">${this.t('lbl.template', "Template")}</div><b>${mid}</b></div>
                  <div><div style="color:var(--text-secondary);font-size:11.5px;">${this.t('lbl.serving', "Serving")}</div><b>:${m.port ?? '—'}</b> · unit <b>${m.unit_id ?? 1}</b></div>
                  <div><div style="color:var(--text-secondary);font-size:11.5px;">${this.t('lbl.status', "Status")}</div>${badge}</div>
                  <div><div style="color:var(--text-secondary);font-size:11.5px;">${this.t('lbl.throughput', "Throughput")}</div><b>${m.running ? (m.requests ?? 0) : '—'}</b> req · <b>${m.running ? (m.req_rate ?? 0) : 0}</b>/s</div>
                  <div><div style="color:var(--text-secondary);font-size:11.5px;">${this.t('lbl.freshness', "Freshness")}</div>stale after <b>${m.stale_after_s ?? 15}s</b> · <b>${this._fmtInterval(m.update_interval_s ?? 1)}</b> refresh</div>
                  ${m.errors ? `<div style="color:#c0392b;"><div style="font-size:11.5px;">${this.t('lbl.errors', "Errors")}</div><b>${m.errors}</b></div>` : ''}
                </div>
                ${m.last_error ? `<div style="color:#c77700;font-size:12.5px;margin-bottom:10px;" title="${this._esc(m.last_error.message || '')}"><i class="bi bi-exclamation-triangle"></i> ${this._esc(m.last_error.kind || '')}</div>` : ''}
                <div style="font-size:12.5px;margin-bottom:14px;">
                  <div style="color:var(--text-secondary);margin-bottom:4px;"><i class="bi bi-plug"></i> Connections (${conns.length})</div>
                  <table>${connRows}</table></div>
                <div style="display:flex;gap:8px;flex-wrap:wrap;">
                  <button class="btn btn-sm" data-vm-editinst="${mid}"><i class="bi bi-sliders"></i> ${t('common.edit', 'Edit')}</button>
                  <button class="btn btn-ghost btn-sm" data-vm-del="${mid}"><i class="bi bi-trash"></i> ${t('common.delete', 'Delete')}</button>
                </div>`;
            const live = prev
                ? `<div style="font-size:13px;"><div style="color:var(--text-secondary);margin-bottom:6px;">Live values served (source → served value)</div><table>${prev}</table></div>`
                : `<p style="color:var(--text-secondary);">${this.t('msg.noLiveValues', "No live values yet.")}</p>`;
            return `
            <div class="settings-card vm-acc" data-mid="${mid}" style="margin-bottom:12px;">
              <div class="settings-card-header vm-acc-head" tabindex="0" style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;">
                <div class="vm-head-l" style="display:flex;align-items:center;gap:10px;min-width:0;">
                  <i class="bi bi-chevron-right vm-acc-chev" style="transition:transform .15s ease;color:var(--text-secondary);"></i>
                  <h3 style="margin:0;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"><i class="bi bi-hdd-network"></i> ${this._esc(m.name || m.template)}</h3>
                  <span class="dev-chip" title="${t('vmeter.sourceDevice', 'Source device')}"><i class="bi bi-arrow-left-short"></i>${this._esc(devName(m.device))}</span>
                  ${badge}
                </div>
                <div class="vm-head-r" style="display:flex;align-items:center;gap:14px;" onclick="event.stopPropagation()">
                  <span class="vm-head-summary" style="font-size:12px;color:var(--text-secondary);font-variant-numeric:tabular-nums;white-space:nowrap;">${summary}</span>
                  <label class="toggle-switch" title="Enable / disable">
                    <input type="checkbox" data-vm="${mid}" aria-label="Enable virtual meter ${mid}" ${m.enabled ? 'checked' : ''}>
                    <span class="toggle-slider"></span>
                  </label>
                </div>
              </div>
              <div class="settings-card-body vm-acc-body" hidden>
                <div class="config-tabs vm-subtabs" style="margin-bottom:14px;">
                  <button class="config-tab active" data-vmsub="overview">${t('vmeter.tab.overview', 'Overview')}</button>
                  <button class="config-tab" data-vmsub="live">${t('vmeter.tab.live', 'Live value')}</button>
                  <button class="config-tab" data-vmsub="logs">${t('vmeter.tab.logs', 'Logs')}</button>
                  <button class="config-tab" data-vmsub="stats">${t('vmeter.tab.stats', 'Stats & Debug')}</button>
                </div>
                <div data-vmsubpanel="overview">${overview}</div>
                <div data-vmsubpanel="live" hidden>${live}</div>
                <div data-vmsubpanel="logs" hidden><div class="vm-log-host"></div></div>
                <div data-vmsubpanel="stats" hidden><div class="vm-stat-host"></div></div>
              </div>
            </div>`;
        }).join('');
        // ── Templates section (define / edit / delete) ──
        const tmplCards = templates.map(t => `
            <div class="settings-card" style="margin-bottom:10px;">
              <div class="settings-card-body" style="display:flex;justify-content:space-between;align-items:center;gap:12px;">
                <div><b>${this._esc(t.name)}</b><br>
                  <span style="color:var(--text-secondary);font-size:12px;">id <code>${this._esc(t.id)}</code> · ${t.kind} · ${t.registers} measurements</span></div>
                <div style="display:flex;gap:8px;">
                  <button class="btn btn-sm" data-vm-edit="${this._esc(t.id)}"><i class="bi bi-pencil"></i> Edit</button>
                  <button class="btn btn-ghost btn-sm" data-vm-export="${this._esc(t.id)}" title="Export YAML"><i class="bi bi-download"></i></button>
                  <button class="btn btn-ghost btn-sm" data-vm-tpldel="${this._esc(t.id)}" title="Delete template"><i class="bi bi-trash"></i></button>
                </div>
              </div>
            </div>`).join('');
        const tmplSection = `
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                <h3 style="margin:0;"><i class="bi bi-diagram-3"></i> Templates</h3>
                <div style="display:flex;gap:8px;">
                  <button class="btn btn-ghost btn-sm" id="vmImportBtn"><i class="bi bi-upload"></i> Import</button>
                  <button class="btn btn-sm" id="vmNewTplBtn"><i class="bi bi-plus-lg"></i> New template</button>
                </div>
              </div>
              <p style="color:var(--text-secondary);font-size:12.5px;margin:0 0 10px;">Map a device's live measurements into the layout a consumer expects. Editing a template that an instance uses reloads it live. Export shares a template as YAML; Import validates before saving.</p>
              <input type="file" id="vmImportFile" accept=".yaml,.yml" style="display:none;">
              ${tmplCards || `<p style="color:var(--text-secondary);">${this.t('msg.noTemplatesYet', "No templates yet.")}</p>`}`;
        el.innerHTML = `
            <style>
              .vm-log-row{cursor:pointer;}
              .vm-log-row:hover{background:rgba(59,130,246,.08);}
              .vm-log-row.active{background:rgba(59,130,246,.16);}
              .vm-log-row.err{background:rgba(192,57,43,.06);}
              .vm-log-row .vm-view{opacity:0;color:#3b82f6;font-weight:600;white-space:nowrap;transition:opacity .1s;}
              .vm-log-row:hover .vm-view,.vm-log-row.active .vm-view{opacity:1;}
            </style>
            <div class="config-main-tabs" id="vmSubtabs">
              <button class="config-main-tab active" data-vmtab="meters"><i class="bi bi-hdd-network"></i> ${this.t('vmeter.tab.meters', 'Meters')}</button>
              <button class="config-main-tab" data-vmtab="templates"><i class="bi bi-diagram-3"></i> ${this.t('vmeter.tab.templates', 'Templates')}</button>
            </div>
            <div data-vmpanel="meters">${addBar}${cards}</div>
            <div data-vmpanel="templates" hidden>${tmplSection}</div>`;
        this._wireVmSubtabs(el);
        this._wireVmMeterCards(el);
        // wire template editor buttons
        const newTplBtn = document.getElementById('vmNewTplBtn');
        if (newTplBtn) newTplBtn.addEventListener('click', () => this.openTemplateEditor(null));
        el.querySelectorAll('button[data-vm-edit]').forEach(b =>
            b.addEventListener('click', () => this.openTemplateEditor(b.dataset.vmEdit)));
        el.querySelectorAll('button[data-vm-tpldel]').forEach(b =>
            b.addEventListener('click', () => this.deleteTemplate(b.dataset.vmTpldel)));
        el.querySelectorAll('button[data-vm-export]').forEach(b =>
            b.addEventListener('click', () => this.exportTemplate(b.dataset.vmExport)));
        // wire import (file picker → POST yaml)
        const importBtn = document.getElementById('vmImportBtn');
        const importFile = document.getElementById('vmImportFile');
        if (importBtn && importFile) {
            importBtn.addEventListener('click', () => importFile.click());
            importFile.addEventListener('change', () => this.importTemplate(importFile));
        }
        // add-instance → modal
        const addBtn = document.getElementById('vmAddInstanceBtn');
        if (addBtn) addBtn.addEventListener('click', () => this.openAddInstanceModal());
    },

    // Wire the meter accordions: expand/collapse, per-meter sub-tabs
    // (Overview/Live/Logs/Stats), enable toggle, edit + delete.
    _wireVmMeterCards(el) {
        el.querySelectorAll('.vm-acc').forEach(card => {
            const mid = card.dataset.mid;
            const head = card.querySelector('.vm-acc-head');
            const body = card.querySelector('.vm-acc-body');
            const chev = card.querySelector('.vm-acc-chev');
            const toggle = () => {
                const opening = body.hidden;
                body.hidden = !opening;
                if (chev) chev.style.transform = opening ? 'rotate(90deg)' : '';
                if (!opening) this._stopVmPolls();       // collapsed → stop its pollers
            };
            head.addEventListener('click', toggle);
            head.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
            });
            // per-meter sub-tabs
            card.querySelectorAll('.vm-subtabs .config-tab').forEach(tab => {
                tab.addEventListener('click', () => this._switchVmSub(card, mid, tab.dataset.vmsub));
            });
            // enable / disable toggle
            const cb = card.querySelector('input[data-vm]');
            if (cb) cb.addEventListener('change', async () => {
                const on = cb.checked; cb.disabled = true;
                try {
                    await fetch(`/api/virtual-meters/${encodeURIComponent(mid)}/toggle?on=${on}`, { method: 'POST' });
                    this.showToast('success', this.t('toast.vmeter', 'Virtual meter'), `${mid} ${on ? 'enabled' : 'disabled'}`);
                } catch (e) { this.showToast('error', this.t('toast.toggleFailed', 'Toggle failed'), mid); }
                setTimeout(() => this.renderVirtualMeters(), 1200);
            });
            // overview edit + delete
            card.querySelectorAll('button[data-vm-editinst]').forEach(b =>
                b.addEventListener('click', () => this.openEditInstance(mid)));
            card.querySelectorAll('button[data-vm-del]').forEach(b =>
                b.addEventListener('click', () => this.openDeleteInstanceModal(mid)));
        });
    },

    _switchVmSub(card, mid, name) {
        card.querySelectorAll('.vm-subtabs .config-tab').forEach(t =>
            t.classList.toggle('active', t.dataset.vmsub === name));
        card.querySelectorAll('[data-vmsubpanel]').forEach(p =>
            p.hidden = p.dataset.vmsubpanel !== name);
        this._stopVmPolls();                          // one active poller at a time
        if (name === 'logs') this.renderVmLogs(mid, card.querySelector('.vm-log-host'));
        else if (name === 'stats') this.renderVmStats(mid, card.querySelector('.vm-stat-host'));
    },

    // ── Add-instance modal (source device / template / port / unit) ───────
    openAddInstanceModal() {
        const ctx = this._vmAddCtx || {};
        const devices = ctx.devices || [];
        const primaryId = ctx.primaryId;
        const templates = (ctx.templates || []).filter(t => !(ctx.configured || new Set()).has(t.id));
        const pr = ctx.pr || {};
        const set = (id, v) => { const e = document.getElementById(id); if (e) e.value = v; };
        const html = (id, opts) => { const e = document.getElementById(id); if (e) e.innerHTML = opts; };
        html('vmAddDevice', devices.map(d => `<option value="${this._esc(d.id)}" ${d.id === primaryId ? 'selected' : ''}>${this._esc(d.name || d.id)}</option>`).join(''));
        html('vmAddTemplate', templates.length
            ? templates.map(t => `<option value="${this._esc(t.id)}">${this._esc(t.name)} · ${t.registers} measurements</option>`).join('')
            : '<option value="">— all templates already added —</option>');
        set('vmAddPort', pr.next_free ?? '');
        const portEl = document.getElementById('vmAddPort');
        if (portEl && pr.start != null) { portEl.min = pr.start; portEl.max = pr.end; }
        set('vmAddUnit', 1);
        const hint = document.getElementById('vmAddPortHint');
        if (hint) hint.textContent = pr.start != null ? `published range ${pr.start}–${pr.end}` : '';
        const fb = document.getElementById('vmAddFeedback');
        if (fb) { fb.textContent = ''; fb.className = 'save-feedback'; }
        set('vmAddOnStale', 'legacy');
        const _q = document.getElementById('vmAddQuality'); if (_q) _q.checked = false;
        this._wireHoldToggle('vmAddOnStale', 'vmAddHoldWrap');
        this._vmAddPr = pr;
        this.openModal('vmAddInstanceModal');
    },

    // show the max-hold field only when the hold policy is selected
    _wireHoldToggle(selId, wrapId) {
        const sel = document.getElementById(selId);
        const wrap = document.getElementById(wrapId);
        if (!sel || !wrap) return;
        const upd = () => { wrap.style.display = sel.value === 'hold' ? '' : 'none'; };
        sel.onchange = upd;
        upd();
    },

    async submitAddInstance(btn) {
        const template = document.getElementById('vmAddTemplate').value;
        const device = document.getElementById('vmAddDevice')?.value || '';
        const port = parseInt(document.getElementById('vmAddPort').value, 10);
        const unit_id = parseInt(document.getElementById('vmAddUnit').value, 10) || 1;
        const pr = this._vmAddPr || {};
        const fb = document.getElementById('vmAddFeedback');
        if (!template) { if (fb) { fb.textContent = 'Pick a template.'; fb.className = 'save-feedback err'; } return; }
        if (pr.start != null && (port < pr.start || port > pr.end)) {
            if (fb) { fb.textContent = `Port must be in ${pr.start}–${pr.end}.`; fb.className = 'save-feedback err'; } return;
        }
        if (btn) btn.disabled = true;
        try {
            const on_stale = document.getElementById('vmAddOnStale')?.value || 'legacy';
            const quality_block = !!document.getElementById('vmAddQuality')?.checked;
            const max_hold_s = Number(document.getElementById('vmAddMaxHold')?.value) || 30;
            const r = await fetch('/api/virtual-meters', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ template, port, unit_id, enabled: false, device,
                                       on_stale, max_hold_s, quality_block })
            });
            if (r.ok) {
                this.showToast('success', this.t('toast.vmeter', 'Virtual meter'), `${template} ${this.t('toast.addedLc', 'added')}`);
                this.closeModal('vmAddInstanceModal');
            } else {
                if (fb) { fb.textContent = (await r.json()).detail || 'Add failed'; fb.className = 'save-feedback err'; }
                return;
            }
        } catch (e) { if (fb) { fb.textContent = e.message; fb.className = 'save-feedback err'; } return; }
        finally { if (btn) btn.disabled = false; }
        setTimeout(() => this.renderVirtualMeters(), 600);
    },

    // ── Delete instance (double confirm: type DELETE) ─────────────────────
    openDeleteInstanceModal(template) {
        this._vmDelTarget = template;
        document.getElementById('vmDelName').textContent = template;
        const inp = document.getElementById('vmDelConfirm');
        if (inp) inp.value = '';
        const btn = document.getElementById('vmDelBtn');
        if (btn) btn.disabled = true;
        this.openModal('vmDeleteInstanceModal');
        setTimeout(() => inp && inp.focus(), 100);
    },

    async confirmDeleteInstance() {
        const template = this._vmDelTarget;
        if (!template) return;
        try {
            await fetch(`/api/virtual-meters/${encodeURIComponent(template)}`, { method: 'DELETE' });
            this.showToast('success', this.t('toast.vmeter', 'Virtual meter'), `${template} ${this.t('toast.removedLc', 'removed')}`);
            this.closeModal('vmDeleteInstanceModal');
        } catch (e) { this.showToast('error', this.t('toast.removeFailed', 'Remove failed'), template); return; }
        setTimeout(() => this.renderVirtualMeters(), 600);
    },

    openEditInstance(template) {
        const m = (this._vmInsts || {})[template];
        if (!m) { this.showToast('error', this.t('toast.edit', 'Edit'), `${this.t('toast.unknownInstance', 'unknown instance')} ${template}`); return; }
        const pr = this._vmPortRange || {};
        const set = (id, v) => { const e = document.getElementById(id); if (e) e.value = v; };
        document.getElementById('vmEditInstName').textContent = m.name || template;
        set('vmEditInstTemplate', template);
        // source device selector
        const devSel = document.getElementById('vmEditDevice');
        if (devSel) {
            const devices = (this._vmAddCtx && this._vmAddCtx.devices) || this._devices || [];
            devSel.innerHTML = devices.map(d => `<option value="${this._esc(d.id)}" ${d.id === m.device ? 'selected' : ''}>${this._esc(d.name || d.id)}</option>`).join('');
        }
        const portEl = document.getElementById('vmEditPort');
        set('vmEditPort', m.port ?? '');
        if (portEl && pr.start != null) { portEl.min = pr.start; portEl.max = pr.end; }
        document.getElementById('vmEditPortHint').textContent =
            (pr.start != null) ? `published range ${pr.start}–${pr.end}` : '';
        set('vmEditUnit', m.unit_id ?? 1);
        set('vmEditStale', m.stale_after_s ?? 15);
        set('vmEditInterval', m.update_interval_s ?? 1);
        set('vmEditOnStale', m.on_stale || 'legacy');
        const _eq = document.getElementById('vmEditQuality'); if (_eq) _eq.checked = !!m.quality_block;
        set('vmEditMaxHold', m.max_hold_s ?? 30);
        this._wireHoldToggle('vmEditOnStale', 'vmEditHoldWrap');
        this.openModal('vmEditInstanceModal');
    },

    async saveEditInstance() {
        const template = document.getElementById('vmEditInstTemplate').value;
        const num = (id) => { const v = document.getElementById(id).value; return v === '' ? undefined : Number(v); };
        const device = document.getElementById('vmEditDevice')?.value;
        const body = { port: num('vmEditPort'), unit_id: num('vmEditUnit'),
                       stale_after_s: num('vmEditStale'), update_interval_s: num('vmEditInterval'),
                       device,
                       on_stale: document.getElementById('vmEditOnStale')?.value || undefined,
                       quality_block: !!document.getElementById('vmEditQuality')?.checked,
                       max_hold_s: num('vmEditMaxHold') };
        try {
            const r = await fetch(`/api/virtual-meters/${encodeURIComponent(template)}`, {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body) });
            const d = await r.json().catch(() => ({}));
            if (r.ok) {
                this.showToast('success', this.t('toast.instanceUpdated', 'Instance updated'), `${template}${d.restarted ? ' · restarted' : ''}`);
                this.closeModal('vmEditInstanceModal');
            } else {
                this.showToast('error', this.t('toast.updateFailed', 'Update failed'), d.detail || `HTTP ${r.status}`);
                return;
            }
        } catch (e) { this.showToast('error', this.t('toast.updateFailed', 'Update failed'), template); return; }
        setTimeout(() => this.renderVirtualMeters(), 700);
    },

    // ── Virtual-meter sub-tabs (Meters / Templates / Logs / Stats) ──────────
    _wireVmSubtabs(el) {
        const tabs = el.querySelectorAll('#vmSubtabs .config-main-tab');
        const show = (name) => {
            tabs.forEach(t => t.classList.toggle('active', t.dataset.vmtab === name));
            el.querySelectorAll('[data-vmpanel]').forEach(p => { p.hidden = p.dataset.vmpanel !== name; });
            this._stopVmPolls();                      // leaving Meters → stop any per-meter poller
        };
        tabs.forEach(t => t.addEventListener('click', () => show(t.dataset.vmtab)));
    },

    _stopVmPolls() {
        if (this._vmLogTimer) { clearInterval(this._vmLogTimer); this._vmLogTimer = null; }
        if (this._vmStatTimer) { clearInterval(this._vmStatTimer); this._vmStatTimer = null; }
    },

    async exportTemplate(id) {
        try {
            const r = await fetch(`/api/virtual-meters/template/${encodeURIComponent(id)}/export`);
            if (!r.ok) { this.showToast('error', this.t('toast.exportFailed', 'Export failed'), id); return; }
            const d = await r.json();
            const blob = new Blob([d.yaml], { type: 'text/yaml' });
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob); a.download = d.filename || `${id}.yaml`;
            document.body.appendChild(a); a.click(); a.remove();
            URL.revokeObjectURL(a.href);
            this.showToast('success', this.t('toast.exported', 'Exported'), d.filename || id);
        } catch (e) { this.showToast('error', this.t('toast.exportFailed', 'Export failed'), id); }
    },

    async importTemplate(fileInput) {
        const f = fileInput.files && fileInput.files[0];
        if (!f) return;
        fileInput.value = '';
        let text;
        try { text = await f.text(); } catch (e) { this.showToast('error', this.t('toast.readFailed', 'Read failed'), f.name); return; }
        const post = (overwrite) => fetch('/api/virtual-meters/templates/import', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ yaml: text, overwrite })
        });
        try {
            let r = await post(false);
            if (r.status === 409) {
                if (!confirm(this.t('vmeter.overwriteConfirm', 'A template with this id already exists. Overwrite it?'))) return;
                r = await post(true);
            }
            const d = await r.json().catch(() => ({}));
            if (r.ok) { this.showToast('success', this.t('toast.imported', 'Imported'), `${d.id} · ${d.registers} ${this.t('templates.measurements', 'measurements')}`); this._afterVmTemplateChange(); }
            else this.showToast('error', this.t('toast.importFailed', 'Import failed'), d.detail || `HTTP ${r.status}`);
        } catch (e) { this.showToast('error', this.t('toast.importFailed', 'Import failed'), f.name); }
    },

    async renderVmLogs(mid, host) {
        if (!host) return;
        const id = mid;
        // build the logs UI inside the meter's Logs sub-panel (once)
        if (!host._built) {
            host._built = true;
            host.innerHTML = `
              <div style="display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap;">
                <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:var(--text-secondary);">
                  <input type="checkbox" class="vm-log-live" checked> live</label>
                <span class="vm-log-meta" style="color:var(--text-secondary);font-size:12.5px;margin-left:auto;"></span>
              </div>
              <div style="display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap;">
                <div class="vm-log-content" style="flex:2 1 380px;min-width:320px;"></div>
                <aside class="vm-decode-panel" style="flex:1 1 320px;min-width:300px;position:sticky;top:8px;align-self:flex-start;"></aside>
              </div>`;
        }
        const out = host.querySelector('.vm-log-content');
        const meta = host.querySelector('.vm-log-meta');
        const live = host.querySelector('.vm-log-live');
        const panel = host.querySelector('.vm-decode-panel');
        this._vmDecodeSel = null;
        if (panel) panel.innerHTML = `<div class="settings-card" style="padding:16px;color:var(--text-secondary);">
            <div style="font-size:13px;margin-bottom:4px;"><i class="bi bi-braces"></i> Decode</div>
            <div style="font-size:12.5px;line-height:1.5;">Click any row on the left to decode that Modbus read — raw words → value → the source variable each maps to.</div></div>`;
        if (!out._decodeWired) {                  // delegated: click a row → decode in side panel
            out._decodeWired = true;
            out.addEventListener('click', (e) => {
                const row = e.target.closest('.vm-log-row');
                if (!row) return;
                out.querySelectorAll('.vm-log-row.active').forEach(r => r.classList.remove('active'));
                row.classList.add('active');
                this._vmDecodeSel = { addr: +row.dataset.addr, count: +row.dataset.count };
                this.renderDecodeInto(id, this._vmDecodeSel.addr, this._vmDecodeSel.count, panel);
            });
        }
        if (live && !live._wired) { live._wired = true; live.addEventListener('change', () => this.renderVmLogs(mid, host)); }
        const load = async () => {
            if (document.hidden) return;          // don't poll a backgrounded tab
            let d;
            try { d = await (await fetch(`/api/virtual-meters/${encodeURIComponent(id)}/stats?limit=200`)).json(); }
            catch (e) { out.innerHTML = `<p style="color:#c0392b;">${this.t('msg.couldNotLoad', "Could not load.")}</p>`; return; }
            if (d.error) { out.innerHTML = `<p style="color:var(--text-secondary);">${this._esc(d.error)}</p>`; return; }
            if (meta) meta.textContent = `${d.total} reqs · ${d.errors} errors · :${d.port} unit ${d.unit_id}`;
            const qs = (d.queries || []).slice().reverse();
            const rows = qs.map(q => {
                const ms = String(Math.floor((q.ts % 1) * 1000)).padStart(3, '0');
                const t = new Date(q.ts * 1000).toLocaleTimeString('en-GB', { hour12: false }) + '.' + ms;
                const resp = q.resp ? q.resp.slice(0, 6).join(' ') + (q.count > 6 ? ' …' : '') : '—';
                const res = q.err ? '<span style="color:#c0392b;font-weight:600;">EXC</span>'
                                  : '<span style="color:var(--success-text);">OK</span>';
                return `<tr class="vm-log-row${q.err ? ' err' : ''}" data-addr="${q.addr}" data-count="${q.count}" title="click to decode this read" style="border-bottom:1px solid var(--border-light);">
                  <td style="padding:3px 10px 3px 0;color:var(--text-secondary);font-variant-numeric:tabular-nums;">${t}</td>
                  <td style="padding:3px 10px 3px 0;">FC${q.fc}</td>
                  <td style="padding:3px 10px 3px 0;font-variant-numeric:tabular-nums;">${q.addr}</td>
                  <td style="padding:3px 10px 3px 0;font-variant-numeric:tabular-nums;">${q.count}</td>
                  <td style="padding:3px 10px 3px 0;">${res}</td>
                  <td style="padding:3px 10px 3px 0;color:var(--text-secondary);font-variant-numeric:tabular-nums;">${q.lat_us}µs</td>
                  <td style="padding:3px 10px 3px 0;font-family:monospace;font-size:12px;color:#5a6470;">${this._esc(resp)}</td>
                  <td style="padding:3px 0;text-align:right;"><span class="vm-view">decode ›</span></td></tr>`;
            }).join('');
            out.innerHTML = `<div style="max-height:62vh;overflow:auto;">
              <table style="width:100%;min-width:560px;font-size:12.5px;border-collapse:collapse;">
                <thead><tr style="text-align:left;color:var(--text-secondary);border-bottom:2px solid var(--border);position:sticky;top:0;background:var(--bg-secondary);">
                  <th style="padding:4px 10px 4px 0;">time</th><th>fc</th><th>addr</th><th>count</th><th>result</th><th>latency</th><th>response</th><th></th>
                </tr></thead><tbody>${rows || `<tr><td colspan="8" style="color:var(--text-secondary);padding:8px 0;">${this.t('msg.noQueries', "No queries yet.")}</td></tr>`}</tbody></table></div>`;
            // keep the decoded row highlighted across live refreshes
            if (this._vmDecodeSel) {
                const sel = out.querySelector(`.vm-log-row[data-addr="${this._vmDecodeSel.addr}"][data-count="${this._vmDecodeSel.count}"]`);
                if (sel) sel.classList.add('active');
            }
        };
        const gen = (this._vmPollGen = (this._vmPollGen || 0) + 1);
        await load();
        if (gen !== this._vmPollGen) return;   // user switched panels mid-load
        this._stopVmPolls();
        if (live && live.checked) this._vmLogTimer = setInterval(load, 2000);
    },

    async renderVmStats(mid, host) {
        if (!host) return;
        const id = mid;
        if (!host._built) {
            host._built = true;
            host.innerHTML = `
              <div style="display:flex;gap:10px;align-items:center;margin-bottom:12px;">
                <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:var(--text-secondary);">
                  <input type="checkbox" class="vm-stat-live" checked> live</label>
              </div>
              <div class="vm-stat-content"></div>`;
        }
        const out = host.querySelector('.vm-stat-content');
        const live = host.querySelector('.vm-stat-live');
        if (live && !live._wired) { live._wired = true; live.addEventListener('change', () => this.renderVmStats(mid, host)); }
        const load = async () => {
            if (document.hidden) return;          // don't poll a backgrounded tab
            let d;
            try { d = await (await fetch(`/api/virtual-meters/${encodeURIComponent(id)}/stats?limit=1`)).json(); }
            catch (e) { out.innerHTML = `<p style="color:#c0392b;">${this.t('msg.couldNotLoad', "Could not load.")}</p>`; return; }
            if (d.error) { out.innerHTML = `<p style="color:var(--text-secondary);">${this._esc(d.error)}</p>`; return; }
            const upt = d.first_ts ? Math.max(1, Math.round(Date.now() / 1000 - d.first_ts)) : 0;
            const rps = upt ? d.total / upt : 0;
            const kb = (n) => n > 1024 ? (n / 1024).toFixed(1) + ' KB' : n + ' B';
            const cards = [
                ['Total requests', d.total, ''],
                ['Errors', d.errors, d.errors ? 'var(--danger-text)' : 'var(--success-text)'],
                ['Avg rate', rps.toFixed(2) + '/s', ''],
                ['RX', kb(d.bytes_rx), ''],
                ['TX', kb(d.bytes_tx), ''],
                ['Uptime', upt + ' s', ''],
            ].map(([k, v, c]) => `<div class="settings-card" style="flex:1;min-width:120px;padding:12px;">
                <div style="color:var(--text-secondary);font-size:12px;">${k}</div>
                <div style="font-size:22px;font-weight:600;color:${c || 'inherit'};font-variant-numeric:tabular-nums;">${v}</div></div>`).join('');
            const rate = d.rate || [];
            const spark = this._sparkline(rate.map(r => r[1]), 560, 72);
            const top = d.top_addrs || [];
            const maxc = Math.max(1, ...top.map(t => t[1]));
            const bars = top.map(([addr, c]) => `<div style="display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12.5px;">
                <code style="width:70px;text-align:right;color:#5a6470;">${addr}</code>
                <div style="flex:1;background:var(--border-light);border-radius:3px;overflow:hidden;"><div style="width:${(c / maxc * 100).toFixed(1)}%;background:#3b82f6;height:14px;"></div></div>
                <span style="width:52px;text-align:right;font-variant-numeric:tabular-nums;color:var(--text-secondary);">${c}</span></div>`).join('');
            const evColor = { error: '#c0392b', warn: '#c77700', info: '#5a6470' };
            const events = (d.events || []).slice().reverse();   // newest first
            const evRows = events.map(e => {
                const t = e.ts ? new Date(e.ts * 1000).toLocaleTimeString('en-GB') : '';
                const c = evColor[e.level] || '#5a6470';
                return `<tr>
                  <td style="white-space:nowrap;color:var(--text-secondary);font-variant-numeric:tabular-nums;">${t}</td>
                  <td><span style="display:inline-block;padding:1px 7px;border-radius:3px;font-size:11px;font-weight:600;color:#fff;background:${c};">${this._esc(e.level || '')}</span></td>
                  <td><code style="color:#5a6470;">${this._esc(e.kind || '')}</code></td>
                  <td style="color:var(--text);">${this._esc(e.message || '')}</td></tr>`;
            }).join('');
            out.innerHTML = `
              <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px;">${cards}</div>
              <div class="settings-card" style="padding:14px;margin-bottom:16px;">
                <div style="color:var(--text-secondary);font-size:12.5px;margin-bottom:8px;">Requests / second (last ${rate.length}s)</div>${spark}</div>
              <div class="settings-card" style="padding:14px;margin-bottom:16px;">
                <div style="color:var(--text-secondary);font-size:12.5px;margin-bottom:8px;">Recent events &amp; errors (last ${events.length}) — what happened to this meter</div>
                ${events.length ? `<div style="overflow-x:auto;"><table class="data-table" style="width:100%;font-size:12.5px;">
                  <thead><tr><th>${this.t('lbl.time', "Time")}</th><th>${this.t('lbl.level', "Level")}</th><th>${this.t('lbl.kind', "Kind")}</th><th>${this.t('lbl.message', "Message")}</th></tr></thead>
                  <tbody>${evRows}</tbody></table></div>`
                  : '<span style="color:#1a8f4c;">No errors — meter has been healthy.</span>'}</div>
              <div class="settings-card" style="padding:14px;">
                <div style="color:var(--text-secondary);font-size:12.5px;margin-bottom:8px;">${this.t('msg.mostRead', "Most-read addresses (where the consumer looks)")}</div>
                ${bars || `<span style="color:var(--text-secondary);">${this.t('msg.noReads', "No reads yet.")}</span>`}</div>`;
        };
        const gen = (this._vmPollGen = (this._vmPollGen || 0) + 1);
        await load();
        if (gen !== this._vmPollGen) return;   // user switched panels mid-load
        this._stopVmPolls();
        if (live && live.checked) this._vmStatTimer = setInterval(load, 2000);
    },

    _sparkline(values, w, h) {
        if (!values || !values.length) return `<span style="color:var(--text-secondary);">${this.t('msg.noDataYet', "No data yet.")}</span>`;
        const max = Math.max(1, ...values), n = values.length;
        const step = n > 1 ? w / (n - 1) : w;
        const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(h - (v / max) * (h - 6) - 3).toFixed(1)}`).join(' ');
        const area = `0,${h} ${pts} ${((n - 1) * step).toFixed(1)},${h}`;
        return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" preserveAspectRatio="none" role="img" aria-label="requests per second, peak ${max}" style="display:block;">
          <polygon points="${area}" fill="rgba(59,130,246,0.13)"/>
          <polyline points="${pts}" fill="none" stroke="#3b82f6" stroke-width="1.5"/>
          <text x="3" y="13" font-size="11" fill="var(--text-secondary)">peak ${max}/s</text></svg>`;
    },

    // ── Decode a logged read: raw words → value → the source variable ──────
    _buildDecodeBody(d) {
        const rows = (d.registers || []).map(r => {
            const words = (r.words || []).join(' ');
            const val = (r.value === null || r.value === undefined) ? '—' : r.value;
            const scale = (r.scale !== 1) ? ` <span style="opacity:.6">×${r.scale}</span>` : '';
            return `<tr style="border-bottom:1px solid var(--border-light);">
              <td style="padding:3px 10px 3px 0;font-variant-numeric:tabular-nums;color:var(--text-secondary);white-space:nowrap;">${r.addr} <span style="opacity:.55;">0x${(r.addr).toString(16)}</span></td>
              <td style="padding:3px 10px 3px 0;font-family:monospace;">${this._esc(r.source)}</td>
              <td style="padding:3px 10px 3px 0;color:var(--text-secondary);white-space:nowrap;">${this._esc(r.type)}${scale}</td>
              <td style="padding:3px 10px 3px 0;font-family:monospace;font-size:11.5px;color:#5a6470;">${this._esc(words)}</td>
              <td style="padding:3px 0;font-weight:600;font-variant-numeric:tabular-nums;">${this._esc(val)}</td></tr>`;
        }).join('');
        return `<div style="overflow:auto;"><table style="width:100%;border-collapse:collapse;font-size:12.5px;">
            <thead><tr style="text-align:left;color:var(--text-secondary);border-bottom:2px solid var(--border);">
              <th style="padding:4px 10px 4px 0;">addr</th><th>source / variable</th><th>type</th><th>raw words</th><th>value</th>
            </tr></thead><tbody>${rows || `<tr><td colspan="5" style="color:var(--text-secondary);padding:8px 0;">${this.t('msg.noRangeMap', "No measurements map into this range.")}</td></tr>`}</tbody></table></div>`;
    },

    async renderDecodeInto(id, addr, count, panelEl) {
        const panel = panelEl || document.getElementById('vmDecodePanel');
        if (!panel || !id || !Number.isFinite(addr)) return;
        const wrap = (inner) => `<div class="settings-card" style="padding:14px;">
            <h4 style="margin:0 0 8px;font-size:13.5px;"><i class="bi bi-braces"></i> Decode · addr ${addr} · count ${count}</h4>${inner}</div>`;
        panel.innerHTML = wrap('<p style="color:var(--text-secondary);font-size:12.5px;margin:0;">Decoding…</p>');
        let d;
        try {
            d = await (await fetch(`/api/virtual-meters/${encodeURIComponent(id)}/decode?addr=${addr}&count=${count}`)).json();
        } catch (e) { panel.innerHTML = wrap(`<p style="color:#c0392b;font-size:12.5px;margin:0;">${this.t('msg.decodeFailed', "Decode failed.")}</p>`); return; }
        if (d.error) { panel.innerHTML = wrap(`<p style="color:var(--text-secondary);font-size:12.5px;margin:0;">${this._esc(d.error)}</p>`); return; }
        panel.innerHTML = wrap(this._buildDecodeBody(d));
    },

    async openTemplateEditor(templateId) {
        if (!this._vmSources) {
            try {
                const s = await (await fetch('/api/virtual-meters/sources')).json();
                this._vmSources = s.sources || [];
                this._vmTypes = (s.types && s.types.length) ? s.types
                    : ['int16', 'uint16', 'int32', 'uint32', 'int64', 'uint64', 'float', 'double', 'string'];
                if (s.port_range) this._vmPortRange = s.port_range;
            } catch (e) {
                this._vmSources = [];
                this._vmTypes = ['int16', 'uint16', 'int32', 'uint32', 'float', 'string'];
            }
        }
        // Composite: source groups per device. Group '' = "the instance's own
        // device" (bare names — resolved at runtime against whichever device the
        // instance binds); named groups emit explicit `device.register` values.
        try {
            const devices = (this._vmAddCtx && this._vmAddCtx.devices) || this._devices || [];
            const groups = [{ device: '', label: this.t('vmeter.srcInstance', "Instance's source device (bare name)"),
                              sources: this._vmSources }];
            const others = await Promise.all(devices.map(async d => {
                try {
                    const s = await (await fetch(`/api/virtual-meters/sources?device=${encodeURIComponent(d.id)}`)).json();
                    return { device: d.id, label: `${d.name || d.id} (${d.id})`, sources: s.sources || [] };
                } catch (e) { return { device: d.id, label: d.id, sources: [] }; }
            }));
            this._vmSourceGroups = groups.concat(others.filter(g => g.sources.length));
            this._vmDeviceIds = new Set(devices.map(d => d.id));
        } catch (e) {
            this._vmSourceGroups = [{ device: '', label: 'Live values', sources: this._vmSources }];
            this._vmDeviceIds = new Set();
        }
        const isNew = !templateId;
        let tpl = {
            id: '', name: '', byte_order: 'big',
            transport: { port: 1502, unit_id: 1, bind: '0.0.0.0' }, registers: [], in_use: false
        };
        if (!isNew) {
            try {
                tpl = await (await fetch(`/api/virtual-meters/template/${encodeURIComponent(templateId)}`)).json();
                if (tpl.error) { this.showToast('error', this.t('toast.loadFailed', 'Load failed'), tpl.error); return; }
            } catch (e) { this.showToast('error', this.t('toast.loadFailed', 'Load failed'), templateId); return; }
        }
        document.getElementById('vmTplTitle').textContent = isNew ? 'New template' : `Edit: ${tpl.name}`;
        this._renderTemplateForm(tpl, isNew);
        this.openModal('vmTemplateModal');
    },

    _renderTemplateForm(tpl, isNew) {
        const body = document.getElementById('vmTplBody');
        const t = tpl.transport || {};
        const pr = this._vmPortRange || {};
        const defPort = isNew ? (pr.next_free ?? t.port ?? 1502) : (t.port ?? 1502);
        const portHint = (pr.start != null) ? `published range ${pr.start}–${pr.end}` : '';
        body.innerHTML = `
          <div class="form-row">
            <div class="form-group"><label class="form-label">${this.t('lbl.templateId', "Template id")}</label>
              <input id="vmfId" class="input" value="${this._esc(tpl.id || '')}" ${isNew ? '' : 'readonly'} placeholder="my_meter (a-z 0-9 _)"></div>
            <div class="form-group"><label class="form-label">${this.t('lbl.name', "Name")}</label>
              <input id="vmfName" class="input" value="${this._esc(tpl.name || '')}" placeholder="My Custom Meter"></div>
          </div>
          <div class="form-row">
            <div class="form-group"><label class="form-label">${this.t('lbl.byteOrder', "Byte order")}</label>
              <select id="vmfByteOrder" class="input">
                <option value="big" ${tpl.byte_order === 'big' ? 'selected' : ''}>ABCD · big (high word first)</option>
                <option value="little" ${tpl.byte_order === 'little' ? 'selected' : ''}>CDAB · little (word swap)</option>
                <option value="badc" ${tpl.byte_order === 'badc' ? 'selected' : ''}>BADC · byte swap</option>
                <option value="dcba" ${tpl.byte_order === 'dcba' ? 'selected' : ''}>DCBA · full little-endian</option>
              </select></div>
            <div class="form-group"><label class="form-label">${this.t('lbl.port', "Port")}</label>
              <input id="vmfPort" class="input" type="number" value="${defPort}" min="${pr.start ?? ''}" max="${pr.end ?? ''}">
              ${portHint ? `<span class="hint-text" style="font-size:11px;">${portHint}</span>` : ''}</div>
            <div class="form-group"><label class="form-label">${this.t('lbl.unitIdLc', "Unit id")}</label>
              <input id="vmfUnit" class="input" type="number" value="${t.unit_id ?? 1}"></div>
            <div class="form-group"><label class="form-label">Bind</label>
              <input id="vmfBind" class="input" value="${this._esc(t.bind || '0.0.0.0')}"></div>
          </div>
          ${tpl.in_use ? '<p class="hint-text" style="color:#e08e0b;"><i class="bi bi-exclamation-triangle"></i> Used by an instance — saving reloads it live.</p>' : ''}
          <div class="form-section">
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <h4 style="margin:0;">${this.t('lbl.measurements', "Measurements")}</h4>
              <button class="btn btn-sm" id="vmAddRowBtn"><i class="bi bi-plus-lg"></i> Add measurement</button>
            </div>
            <div style="overflow-x:auto;margin-top:8px;">
              <table style="width:100%;border-collapse:collapse;font-size:12.5px;">
                <thead><tr style="text-align:left;color:var(--text-secondary);">
                  <th style="padding:4px 6px;">${this.t('lbl.address', "Address")}</th><th>${this.t('lbl.type', "Type")}</th><th>${this.t('lbl.source', "Source")}</th>
                  <th>${this.t('lbl.valueSource', "Value / Source")}</th><th title="${this.t('vmeter.scaleHint', 'raw = value × scale; the consumer reads raw ÷ scale')}">${this.t('lbl.scale', "Scale")}</th><th>Len</th>
                  <th title="${this.t('vmeter.rowStaleHint', 'Per-row freshness bound (s) for composite policies — empty = auto (source device / instance)')}">${this.t('vmeter.rowStale', "Stale s")}</th>
                  <th>${this.t('lbl.note', "Note")}</th><th></th>
                </tr></thead>
                <tbody id="vmRegRows"></tbody>
              </table>
            </div>
            <p class="hint-text">Address in hex (e.g. <code>0x0028</code>) or decimal. Scale: raw = value × scale (consumer reads raw ÷ scale). Length applies to <code>string</code> only.</p>
          </div>`;
        (tpl.registers || []).forEach(r => this._addTemplateRow(r));
        if (!(tpl.registers || []).length) this._addTemplateRow();
        document.getElementById('vmAddRowBtn').addEventListener('click', () => this._addTemplateRow());
        document.getElementById('vmTplSaveBtn').onclick = () => this.saveTemplate();
    },

    // Next free address after the LAST row (addr + type span) — adding ten
    // registers no longer means retyping ten addresses by hand.
    _nextTemplateAddr() {
        const rows = [...document.querySelectorAll('#vmRegRows tr')];
        if (!rows.length) return 0;
        const spans = { int16: 1, uint16: 1, int32: 2, uint32: 2, float: 2, float32: 2,
                        int64: 4, uint64: 4, double: 4 };
        const last = rows[rows.length - 1];
        const a = last.querySelector('.vm-addr')?.value.trim() || '0';
        const base = a.startsWith('0x') ? parseInt(a, 16) : parseInt(a, 10) || 0;
        const ty = last.querySelector('.vm-type')?.value || 'int32';
        const len = parseInt(last.querySelector('.vm-len')?.value, 10) || 1;
        return base + (ty === 'string' ? Math.max(1, len) : (spans[ty] || 2));
    },

    _addTemplateRow(reg) {
        reg = reg || { addr: this._nextTemplateAddr(), type: 'int32', scale: 1,
                       source_kind: 'const', source: 0, length: 1, note: '' };
        const tbody = document.getElementById('vmRegRows');
        const tr = document.createElement('tr');
        tr.style.borderTop = '1px solid var(--border, #e5e7eb)';
        const typeOpts = this._vmTypes.map(ty => `<option ${ty === reg.type ? 'selected' : ''}>${ty}</option>`).join('');
        // Grouped source options: bare names = the instance's own device;
        // `device.register` values = explicit cross-device (composite) sources.
        const groups = this._vmSourceGroups
            || [{ device: '', label: 'Live values', sources: this._vmSources }];
        const optFor = (s, val) => {
            const v = (s.value === null || s.value === undefined) ? '—'
                : (typeof s.value === 'number' ? s.value.toFixed(1) : s.value);
            return `<option value="${this._esc(val)}" ${val === reg.source ? 'selected' : ''}>`
                + `${this._esc(s.name)} — ${this._esc(s.label || '')} (${v}${s.unit ? ' ' + this._esc(s.unit) : ''})</option>`;
        };
        const srcOpts = groups.map(g =>
            `<optgroup label="${this._esc(g.label)}">`
            + g.sources.map(s => optFor(s, g.device ? `${g.device}.${s.name}` : s.name)).join('')
            + '</optgroup>').join('');
        const addrHex = '0x' + Number(reg.addr || 0).toString(16).padStart(4, '0');
        const isLive = reg.source_kind === 'live';
        const srcVal = isLive ? '' : (Array.isArray(reg.source) ? reg.source.join(', ') : String(reg.source ?? ''));
        tr.innerHTML = `
          <td style="padding:3px 6px;"><input class="input input-sm vm-addr" style="width:82px;" value="${addrHex}"></td>
          <td><select class="input input-sm vm-type" style="width:92px;">${typeOpts}</select></td>
          <td><select class="input input-sm vm-kind" style="width:110px;">
            <option value="const" ${reg.source_kind === 'const' ? 'selected' : ''}>${this.t('lbl.constNum', "Const num")}</option>
            <option value="const_str" ${reg.source_kind === 'const_str' ? 'selected' : ''}>${this.t('lbl.constText', "Const text")}</option>
            <option value="live" ${isLive ? 'selected' : ''}>${this.t('lbl.liveValue', "Live value")}</option>
            <option value="sum" ${reg.source_kind === 'sum' ? 'selected' : ''}>Sum (live…)</option>
          </select></td>
          <td>
            <select class="input input-sm vm-src-live" style="min-width:240px;${isLive ? '' : 'display:none;'}">${srcOpts || '<option value="">(no live values)</option>'}</select>
            <input class="input input-sm vm-src-val" style="width:200px;${isLive ? 'display:none;' : ''}" placeholder="${reg.source_kind === 'sum' ? 'name1, name2, …' : ''}" value="${this._esc(srcVal)}">
          </td>
          <td><input class="input input-sm vm-scale" style="width:64px;" value="${reg.scale ?? 1}"></td>
          <td><input class="input input-sm vm-len" type="number" style="width:52px;" value="${reg.length ?? 1}"></td>
          <td><input class="input input-sm vm-stale" type="number" style="width:64px;" min="1" placeholder="auto" value="${reg.stale_after_s ?? ''}"></td>
          <td><input class="input input-sm vm-note" style="width:130px;" value="${this._esc(reg.note || '')}"></td>
          <td style="white-space:nowrap;"><button class="btn btn-ghost btn-sm vm-row-dup" title="Duplicate row"><i class="bi bi-copy"></i></button><button class="btn btn-ghost btn-sm vm-row-del" title="Remove"><i class="bi bi-x-lg"></i></button></td>`;
        tbody.appendChild(tr);
        const kindSel = tr.querySelector('.vm-kind');
        const liveSel = tr.querySelector('.vm-src-live');
        const valInp = tr.querySelector('.vm-src-val');
        kindSel.addEventListener('change', () => {
            const live = kindSel.value === 'live';
            liveSel.style.display = live ? '' : 'none';
            valInp.style.display = live ? 'none' : '';
            valInp.placeholder = kindSel.value === 'sum' ? 'name1, name2, …' : '';
        });
        tr.querySelector('.vm-row-del').addEventListener('click', () => tr.remove());
        // duplicate: copy every field, give the clone the next free address
        tr.querySelector('.vm-row-dup').addEventListener('click', () => {
            const kind2 = tr.querySelector('.vm-kind').value;
            this._addTemplateRow({
                addr: this._nextTemplateAddr(),
                type: tr.querySelector('.vm-type').value,
                scale: parseFloat(tr.querySelector('.vm-scale').value) || 1,
                length: parseInt(tr.querySelector('.vm-len').value, 10) || 1,
                note: tr.querySelector('.vm-note').value,
                source_kind: kind2,
                source: kind2 === 'live' ? tr.querySelector('.vm-src-live').value
                    : tr.querySelector('.vm-src-val').value,
                stale_after_s: tr.querySelector('.vm-stale')?.value || undefined,
            });
        });
    },

    async saveTemplate() {
        const id = document.getElementById('vmfId').value.trim();
        const name = document.getElementById('vmfName').value.trim();
        const byte_order = document.getElementById('vmfByteOrder').value;
        const port = parseInt(document.getElementById('vmfPort').value, 10);
        const unit_id = parseInt(document.getElementById('vmfUnit').value, 10);
        const bind = document.getElementById('vmfBind').value.trim() || '0.0.0.0';
        if (!/^[a-z0-9_]+$/.test(id)) { this.showToast('error', this.t('toast.invalidId', 'Invalid id'), this.t('toast.useIdChars', 'Use a-z 0-9 _ only')); return; }
        if (!name) { this.showToast('error', this.t('toast.nameRequired', 'Name required'), ''); return; }
        const registers = [];
        const unknownDevs = new Set();
        document.querySelectorAll('#vmRegRows tr').forEach(tr => {
            const kind = tr.querySelector('.vm-kind').value;
            const source = kind === 'live' ? tr.querySelector('.vm-src-live').value
                : tr.querySelector('.vm-src-val').value;
            const row = {
                addr: tr.querySelector('.vm-addr').value.trim(),
                type: tr.querySelector('.vm-type').value,
                scale: parseFloat(tr.querySelector('.vm-scale').value) || 1,
                length: parseInt(tr.querySelector('.vm-len').value, 10) || 1,
                note: tr.querySelector('.vm-note').value.trim(),
                source_kind: kind,
                source,
            };
            const st = tr.querySelector('.vm-stale')?.value;
            if (st) row.stale_after_s = Number(st);
            // soft validation: a dotted source whose prefix is not a known
            // device would resolve as a bare name (or come up missing at runtime)
            if (kind === 'live' && source.includes('.') && this._vmDeviceIds
                && !this._vmDeviceIds.has(source.split('.')[0])) {
                unknownDevs.add(source.split('.')[0]);
            }
            registers.push(row);
        });
        if (!registers.length) { this.showToast('error', this.t('toast.noMeasurements', 'No measurements'), this.t('toast.addOneMeasurement', 'Add at least one measurement')); return; }
        if (unknownDevs.size
            && !confirm(`${this.t('vmeter.unknownDevWarn', 'Warning: source prefix is not a known device id')}: ${[...unknownDevs].join(', ')}\n${this.t('vmeter.unknownDevWarn2', 'Those rows will read as plain register names (or show as missing). Save anyway?')}`)) {
            return;
        }
        const btn = document.getElementById('vmTplSaveBtn');
        btn.disabled = true;
        try {
            const r = await fetch(`/api/virtual-meters/template/${encodeURIComponent(id)}`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id, name, byte_order, port, unit_id, bind, registers })
            });
            const j = await r.json();
            if (r.ok) {
                this.showToast('success', this.t('toast.templateSaved', 'Template saved'),
                    `${id} (${j.registers} ${this.t('vmeter.registersLc', 'registers')})${j.reloaded ? ' — ' + this.t('vmeter.reloadedLive', 'reloaded live') : ''}`);
                this.closeModal('vmTemplateModal');
                this._afterVmTemplateChange();
            } else {
                this.showToast('error', this.t('toast.saveFailed', 'Save failed'), j.detail || this.t('toast.validationError', 'validation error'));
            }
        } catch (e) { this.showToast('error', this.t('toast.saveFailed', 'Save failed'), String(e)); }
        finally { btn.disabled = false; }
    },

    async deleteTemplate(id) {
        if (!confirm(`${this.t('vmeter.deleteConfirm', 'Delete template')} "${id}"?\n${this.t('vmeter.deleteConfirmHint', '(only if no instance uses it)')}`)) return;
        try {
            const r = await fetch(`/api/virtual-meters/template/${encodeURIComponent(id)}`, { method: 'DELETE' });
            const j = await r.json();
            if (r.ok) this.showToast('success', this.t('toast.templateDeleted', 'Template deleted'), id);
            else this.showToast('error', this.t('toast.deleteFailed', 'Delete failed'), j.detail || '');
        } catch (e) { this.showToast('error', this.t('toast.deleteFailed', 'Delete failed'), id); }
        this._afterVmTemplateChange();
    },

    // The vmeter-template editor/import/delete handlers are shared between the
    // Virtual Meters page and the Template Manager; refresh whichever is open.
    _afterVmTemplateChange() {
        if (this.currentPage === 'templates') this.renderTemplateManager();
        else this.renderVirtualMeters();
    }
});
