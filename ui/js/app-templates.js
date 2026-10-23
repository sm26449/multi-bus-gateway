/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// templates domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    // ── Template Manager (dedicated page) ─────────────────────────────────
    // One home for both template kinds: "Device maps" (input register maps used
    // to READ real meters) and "Meter emulations" (output layouts re-served as a
    // Modbus slave). All actions reuse the existing wizard / vmeter handlers.
    async renderTemplateManager() {
        const el = document.getElementById('templatesContent');
        if (!el) return;
        const t = this.t.bind(this);
        const refreshBtn = document.getElementById('tmRefreshBtn');
        if (refreshBtn && !refreshBtn._wired) {
            refreshBtn._wired = true;
            refreshBtn.addEventListener('click', () => this.renderTemplateManager());
        }
        let devTpls = [], vmTpls = [], loadErrors = {}, loadWarnings = {};
        await this._fetchDevices();             // who uses a map: units counted as their installation
        try {
            const d = await (await fetch('/api/device-templates')).json();
            devTpls = d.templates || []; loadErrors = d.load_errors || {}; loadWarnings = d.load_warnings || {};
        } catch (e) {}
        try { vmTpls = (await (await fetch('/api/virtual-meters/templates')).json()).templates || []; } catch (e) {}

        const activeTab = this._tmTab || 'devmaps';
        const badgeBuiltin = `<span style="font-size:10.5px;font-weight:700;padding:1px 6px;border-radius:8px;background:rgba(59,130,246,.15);color:#3b82f6;">${t('templates.builtin', 'built-in')}</span>`;
        const badgeUser = `<span style="font-size:10.5px;font-weight:700;padding:1px 6px;border-radius:8px;background:rgba(26,143,76,.15);color:#1a8f4c;">${t('templates.userTag', 'user')}</span>`;

        // ── Device maps ──
        // every map is drawn once; the search and filters only hide cards
        // (app-find.js) — a keystroke never re-renders the page
        if (this._tplFindIndex) this._tplFindIndex(devTpls);
        const devCards = devTpls.length ? devTpls.map(x => {
            const users = this._tplUsers(x.used_by);
            const inUse = users.length;
            const meta = [x.vendor, x.model].filter(Boolean).map(s => this._esc(s)).join(' · ');
            const usedRow = inUse
                ? `<div class="tm-used">${this._esc(this.tn('templates.usedBy', inUse, 'Used by {n}'))}: ${users.map(u => `<a href="#" class="ub-inline" aria-label="${this._esc(t(`usedby.open.${u.type}`, 'Open {id}', { id: u.label }))}" ${this._act('usedByOpen', [u.type, u.id, ''])}>${this._esc(u.label)}</a>`).join(', ')}</div>`
                : '';
            // installations / devices with an update from this map waiting
            const pend = x.pending_updates || [];
            const pendRow = pend.length
                ? `<div class="tm-pending"><i aria-hidden="true" class="bi bi-stars"></i> ${t('templates.pending', 'Update waiting in')}: ${pend.map(p => `<span class="tm-pending-item">${this._esc(p.name)} <button data-admin class="btn btn-ghost btn-sm" ${this._act('openTemplateUpdate', [p.kind, p.id])}>${t('tplu.review', 'Review and apply')}</button></span>`).join(' ')}</div>`
                : '';
            const viewBtn = `<button class="btn btn-ghost btn-sm" data-tm-view="${this._esc(x.id)}"><i aria-hidden="true" class="bi bi-eye"></i> ${t('templates.view', 'View')}</button>`;
            const actions = x.builtin
                ? `${viewBtn}
                   <button data-admin class="btn btn-ghost btn-sm" data-tm-dup="${this._esc(x.id)}"><i aria-hidden="true" class="bi bi-files"></i> ${t('templates.duplicate', 'Duplicate')}</button>
                   <button class="btn btn-ghost btn-sm" data-tm-exp="${this._esc(x.id)}" ${this._ib(t('templates.exportOne', 'Export template'), x.name || x.id)}><i aria-hidden="true" class="bi bi-download"></i></button>`
                : `${viewBtn}
                   <button data-admin class="btn btn-ghost btn-sm" data-tm-edit="${this._esc(x.id)}"><i aria-hidden="true" class="bi bi-pencil"></i> ${t('common.edit', 'Edit')}</button>
                   <button data-admin class="btn btn-ghost btn-sm" data-tm-dup="${this._esc(x.id)}"><i aria-hidden="true" class="bi bi-files"></i> ${t('templates.duplicate', 'Duplicate')}</button>
                   <button class="btn btn-ghost btn-sm" data-tm-exp="${this._esc(x.id)}" ${this._ib(t('templates.exportOne', 'Export template'), x.name || x.id)}><i aria-hidden="true" class="bi bi-download"></i></button>
                   <button data-admin class="btn btn-ghost btn-sm" data-tm-del="${this._esc(x.id)}" ${inUse ? 'aria-disabled="true"' : ''} title="${this._esc(inUse ? this.tn('usedby.deleteHeld', inUse, 'Cannot be deleted while {n} thing(s) use it — see "Used by".') : t('common.delete', 'Delete'))}" aria-label="${this._esc(inUse ? `${t('common.delete', 'Delete')}: ${this.tn('usedby.deleteHeld', inUse, 'Cannot be deleted while {n} thing(s) use it — see "Used by".')}` : `${t('common.delete', 'Delete')} ${x.id}`)}"><i aria-hidden="true" class="bi bi-trash"></i></button>`;
            const proto = x.transport ? ` · ${this._esc(this._findValueLabel ? this._findValueLabel('proto', String(x.transport).toLowerCase()) : x.transport)}` : '';
            return `<div class="settings-card" style="margin-bottom:10px;" data-f="${this._esc(x.id)}">
                <div class="settings-card-body" style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
                  <div style="min-width:0;">
                    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">${this._kindIconHtml ? this._kindIconHtml(x.device_kind) : ''}<b>${this._esc(x.name)}</b> ${x.builtin ? badgeBuiltin : badgeUser}</div>
                    <div style="color:var(--text-secondary);font-size:12px;margin-top:3px;">id <code>${this._esc(x.id)}</code>${meta ? ' · ' + meta : ''}${proto} · ${x.registers} ${t('templates.regs', 'registers')}${x.version ? ' · v' + this._esc(x.version) : ''}</div>
                    ${usedRow}
                    ${pendRow}
                  </div>
                  <div style="display:flex;gap:8px;flex-wrap:wrap;">${actions}</div>
                </div></div>`;
        }).join('') : (`<div class="settings-card" style="padding:14px;"><p style="margin:0 0 8px;">${t('templates.noneLead', 'No device maps yet. A map (template) says which registers a device has and how to read them — every device uses one.')}</p>
                <div style="display:flex;gap:8px;flex-wrap:wrap;">
                  <button data-admin class="btn btn-primary btn-sm" ${this._act('openTplEditor', [null])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('templates.createFirst', 'Create a map')}</button>
                  <button data-admin class="btn btn-ghost btn-sm" ${this._act('tplUpload', [])}><i aria-hidden="true" class="bi bi-upload"></i> ${t('templates.uploadFirst', 'Upload a map file')}</button>
                  <a class="btn btn-ghost btn-sm" href="https://github.com/sm26449/multi-bus-gateway/blob/main/docs/device-templates.md" target="_blank" rel="noopener"><i aria-hidden="true" class="bi bi-book"></i> ${t('devtpl.guide', 'Guide: writing a template')}</a>
                </div></div>`);

        const errKeys = Object.keys(loadErrors);
        const errBanner = errKeys.length
            ? `<div class="settings-card" style="border-left:3px solid #c0392b;padding:8px 12px;margin-bottom:10px;color:var(--danger-text,#c0392b);font-size:12.5px;"><i aria-hidden="true" class="bi bi-exclamation-triangle"></i> ${errKeys.length} ${t('templates.loadErrors', 'template file(s) failed to load')}: ${errKeys.map(k => this._esc(k)).join(', ')}</div>`
            : '';
        const warnKeys = Object.keys(loadWarnings);
        const warnBanner = warnKeys.length
            ? `<div class="settings-card" style="border-left:3px solid #f59e0b;padding:8px 12px;margin-bottom:10px;color:var(--warning-text,#b45309);font-size:12.5px;"><i aria-hidden="true" class="bi bi-exclamation-circle"></i> ${t('templates.canonicalWarn', 'Non-canonical field names (see docs/canonical-fields.md)')}: ${warnKeys.map(k => `<b>${this._esc(k)}</b> — ${this._esc(loadWarnings[k])}`).join(' · ')}</div>`
            : '';
        const devToolbar = `<div style="display:flex;justify-content:flex-end;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap;">
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
              <button data-admin class="btn btn-ghost btn-sm" id="tmImportCsvBtn"><i aria-hidden="true" class="bi bi-filetype-csv"></i> ${t('registers.importCsv', 'Import CSV')}</button>
              <button data-admin class="btn btn-ghost btn-sm" id="tmImportYamlBtn"><i aria-hidden="true" class="bi bi-filetype-yml"></i> ${t('registers.importYaml', 'Import YAML')}</button>
              <button data-admin class="btn btn-ghost btn-sm" id="tmUploadBtn"><i aria-hidden="true" class="bi bi-upload"></i> ${t('templates.upload', 'Upload JSON')}</button>
              <button data-admin class="btn btn-primary btn-sm" id="tmNewBtn"><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('templates.new', 'New map')}</button>
            </div></div>`;

        // ── Meter emulations (vmeter output templates) ──
        const vmCards = vmTpls.length ? vmTpls.map(tp => `
            <div class="settings-card" style="margin-bottom:10px;">
              <div class="settings-card-body" style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
                <div><b>${this._esc(tp.name)}</b><br>
                  <span style="color:var(--text-secondary);font-size:12px;">id <code>${this._esc(tp.id)}</code> · ${this._esc(tp.kind || '')} · ${tp.registers} ${t('templates.measurements', 'measurements')}</span></div>
                <div style="display:flex;gap:8px;">
                  <button data-admin class="btn btn-ghost btn-sm" data-tmvm-edit="${this._esc(tp.id)}"><i aria-hidden="true" class="bi bi-pencil"></i> ${t('common.edit', 'Edit')}</button>
                  <button class="btn btn-ghost btn-sm" data-tmvm-exp="${this._esc(tp.id)}" ${this._ib(t('templates.exportOne', 'Export template'), tp.name || tp.id)}><i aria-hidden="true" class="bi bi-download"></i></button>
                  <button data-admin class="btn btn-ghost btn-sm" data-tmvm-del="${this._esc(tp.id)}" ${this._ib(t('common.delete', 'Delete'), tp.name || tp.id)}><i aria-hidden="true" class="bi bi-trash"></i></button>
                </div>
              </div></div>`).join('') : `<p style="color:var(--text-secondary);">${t('templates.noEmu', 'No meter emulations yet.')}</p>`;
        const vmToolbar = `<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap;">
            <p style="color:var(--text-secondary);font-size:12.5px;margin:0;max-width:560px;">${t('templates.emuHint', 'Map live values into the layout a consumer expects. Run and monitor instances on the Virtual Meters page.')}</p>
            <div style="display:flex;gap:8px;">
              <input type="file" id="tmVmImportFile" accept=".yaml,.yml" style="display:none;">
              <button data-admin class="btn btn-ghost btn-sm" id="tmVmImportBtn"><i aria-hidden="true" class="bi bi-upload"></i> ${t('common.import', 'Import')}</button>
              <button data-admin class="btn btn-primary btn-sm" id="tmVmNewBtn"><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('templates.newEmu', 'New template')}</button>
            </div></div>`;

        el.innerHTML = `
            <div class="config-main-tabs" id="tmTabs">
              <button class="config-main-tab ${activeTab === 'devmaps' ? 'active' : ''}" data-tmtab="devmaps"><i aria-hidden="true" class="bi bi-table"></i> ${t('templates.tab.devmaps', 'Device maps')} <span style="opacity:.6;">(${devTpls.length})</span></button>
              <button class="config-main-tab ${activeTab === 'emulations' ? 'active' : ''}" data-tmtab="emulations"><i aria-hidden="true" class="bi bi-hdd-network"></i> ${t('templates.tab.emu', 'Meter emulations')} <span style="opacity:.6;">(${vmTpls.length})</span></button>
              <button class="config-main-tab ${activeTab === 'fields' ? 'active' : ''}" data-tmtab="fields"><i aria-hidden="true" class="bi bi-tags"></i> ${t('templates.tab.fields', 'Canonical fields')}</button>
            </div>
            <div data-tmpanel="devmaps" ${activeTab === 'devmaps' ? '' : 'hidden'}>${errBanner}${warnBanner}${devToolbar}
                ${devTpls.length && this._findBarHtml ? this._findBarHtml('templates') : ''}
                <div class="tm-cards">${devCards}</div>
                <div class="tm-table" hidden></div>
                <div class="tm-find-empty find-empty" hidden><i aria-hidden="true" class="bi bi-search"></i>
                    <div><b>${t('find.noMatch', 'Nothing matches')}</b><div class="field-hint">${t('find.noMatchHint', 'Try fewer words, or clear the filters.')}</div></div>
                    <button type="button" class="btn btn-secondary btn-sm" data-find-clear-empty>${t('find.clear', 'Clear filters')}</button></div>
            </div>
            <div data-tmpanel="emulations" ${activeTab === 'emulations' ? '' : 'hidden'}>${vmToolbar}${vmCards}</div>
            <div data-tmpanel="fields" id="tmFieldsPanel" ${activeTab === 'fields' ? '' : 'hidden'}></div>`;

        // tab switch
        el.querySelectorAll('[data-tmtab]').forEach(b => b.addEventListener('click', () => {
            this._tmTab = b.dataset.tmtab;
            el.querySelectorAll('[data-tmtab]').forEach(x => x.classList.toggle('active', x === b));
            el.querySelectorAll('[data-tmpanel]').forEach(p => { p.hidden = p.dataset.tmpanel !== this._tmTab; });
            if (this._tmTab === 'fields') this.renderFieldsPanel();
        }));
        if (activeTab === 'fields') this.renderFieldsPanel();
        // search and filters hide cards in place (app-find.js)
        if (devTpls.length && this._findWireBar) {
            this._findWireBar('templates', viewChanged => this._tplFindApply(viewChanged));
            this._findWireSlash();
            el.querySelector('[data-find-clear-empty]')?.addEventListener('click', () =>
                el.querySelector('.find-bar[data-find="templates"] .find-clear')?.click());
            this._tplFindApply(true);
        }
        // device-map actions (reuse the wizard handlers; their refresh re-renders this page)
        el.querySelectorAll('[data-tm-view]').forEach(b => b.addEventListener('click', () => this.viewTemplate(b.dataset.tmView)));
        document.getElementById('tmNewBtn')?.addEventListener('click', () => this.openTplEditor(null));
        document.getElementById('tmUploadBtn')?.addEventListener('click', () => this.tplUpload());
        document.getElementById('tmImportCsvBtn')?.addEventListener('click', () => this.openCsvImport(true, 'csv'));
        document.getElementById('tmImportYamlBtn')?.addEventListener('click', () => this.openCsvImport(true, 'yaml'));
        el.querySelectorAll('[data-tm-edit]').forEach(b => b.addEventListener('click', () => this.openTplEditor(b.dataset.tmEdit)));
        el.querySelectorAll('[data-tm-dup]').forEach(b => b.addEventListener('click', () => this.openTplEditor(b.dataset.tmDup, true)));
        el.querySelectorAll('[data-tm-exp]').forEach(b => b.addEventListener('click', () => this.tplExport(b.dataset.tmExp)));
        el.querySelectorAll('[data-tm-del]').forEach(b => b.addEventListener('click', () => this.tplDelete(b.dataset.tmDel)));
        // meter-emulation actions (reuse the vmeter template handlers)
        document.getElementById('tmVmNewBtn')?.addEventListener('click', () => this.openTemplateEditor(null));
        const vmImportBtn = document.getElementById('tmVmImportBtn');
        const vmImportFile = document.getElementById('tmVmImportFile');
        if (vmImportBtn && vmImportFile) {
            vmImportBtn.addEventListener('click', () => vmImportFile.click());
            vmImportFile.addEventListener('change', () => this.importTemplate(vmImportFile));
        }
        el.querySelectorAll('[data-tmvm-edit]').forEach(b => b.addEventListener('click', () => this.openTemplateEditor(b.dataset.tmvmEdit)));
        el.querySelectorAll('[data-tmvm-exp]').forEach(b => b.addEventListener('click', () => this.exportTemplate(b.dataset.tmvmExp)));
        el.querySelectorAll('[data-tmvm-del]').forEach(b => b.addEventListener('click', () => this.deleteTemplate(b.dataset.tmvmDel)));
    },

    // Read-only register-map viewer for a device template (built-in or user).
    // a map read over Modbus (any framing), or none named yet
    _tplIsModbus(transports) {
        const tr = (transports || []).map(x => String(x).toLowerCase());
        return !tr.length || tr.some(x => ['tcp', 'rtu', 'rtu-tcp', 'rtu_tap', 'ascii', 'udp'].includes(x));
    },

    async viewTemplate(id) {
        const t = this.t.bind(this);
        let full;
        try {
            full = (await (await fetch(`/api/device-templates/${encodeURIComponent(id)}`)).json()).device_template;
        } catch (e) { this.showToast('error', this.t('toast.template', 'Template'), e.message); return; }
        if (!full) { this.showToast('error', this.t('toast.template', 'Template'), id); return; }
        const regs = full.registers || [];
        const proto = full.protocol || {};
        const tr = (proto.transports || []).map(x => String(x).toLowerCase());
        // what a register is read by: a Modbus function, a JSON path, a topic
        const modbus = this._tplIsModbus(tr);
        const mqtt = !modbus && tr.includes('mqtt');
        const FC = { holding: ['FC03', t('templates.fcHolding', 'holding')], input: ['FC04', t('templates.fcInput', 'input')],
                     coil: ['FC01', t('templates.fcCoil', 'coil')], discrete: ['FC02', t('templates.fcDiscrete', 'discrete input')] };
        const fcOf = r => FC[r.register_type || 'holding'] || FC.holding;
        let transport;
        if (modbus) {
            const bo = proto.byte_order || 'big';
            const boLabel = { big: 'big-endian (ABCD)', little: 'little-endian / word-swap (CDAB)',
                              badc: 'byte-swap (BADC)', dcba: 'full little-endian (DCBA)' }[bo] || bo;
            const fcs = [...new Set(regs.map(r => fcOf(r).join(' ')))].join(', ') || 'FC03 holding';
            transport = `${fcs} · ${boLabel}`;
        } else {
            transport = mqtt ? t('templates.viaMqtt', 'MQTT · topics') : t('templates.viaHttp', 'HTTP · JSON');
        }
        document.getElementById('tmViewTitle').textContent =
            `${full.name || id}${full.builtin ? ' · ' + t('templates.builtin', 'built-in') : ''}`;
        const meta = `<div style="display:flex;gap:18px 26px;flex-wrap:wrap;font-size:12.5px;margin-bottom:10px;">
            <div><div style="color:var(--text-secondary);">id</div><code>${this._esc(full.id)}</code></div>
            <div><div style="color:var(--text-secondary);">${t('templates.vendorModel', 'Vendor / model')}</div>${this._esc([full.vendor, full.model].filter(Boolean).join(' · ') || '—')}</div>
            <div data-tpl-transport><div style="color:var(--text-secondary);">${t('templates.transport', 'Transport')}</div>${this._esc(transport)}</div>
            <div><div style="color:var(--text-secondary);">${t('templates.regs', 'registers')}</div><b>${regs.length}</b></div>
            ${full.version ? `<div><div style="color:var(--text-secondary);">version</div>v${this._esc(full.version)}</div>` : ''}
          </div>`;
        const src = full.source_document
            ? `<div style="font-size:12px;color:var(--text-secondary);margin-bottom:8px;"><i aria-hidden="true" class="bi bi-journal-check"></i> ${t('templates.source', 'Source')}: ${this._esc(full.source_document)}</div>` : '';
        const desc = full.description
            ? `<details style="margin-bottom:10px;"><summary style="cursor:pointer;font-size:12.5px;color:var(--text-secondary);">${t('templates.details', 'Details / provenance')}</summary><p style="font-size:12.5px;color:var(--text-secondary);margin:6px 0 0;">${this._esc(full.description)}</p></details>` : '';
        const also = this._tplViewAlso(full);
        const firstHead = modbus ? t('templates.address', 'Address') : mqtt ? t('templates.topic', 'Topic') : t('templates.jsonPath', 'JSON path');
        const sorted = modbus ? regs.slice().sort((a, b) => (a.address - b.address)) : regs;
        const rows = sorted.map(r => {
            let first;
            if (modbus) {
                const a = r.address;
                const hx = (typeof a === 'number') ? ' <span style="color:var(--text-secondary);">0x' + a.toString(16).toUpperCase().padStart(4, '0') + '</span>' : '';
                first = `${this._esc(String(a))}${hx}`;
            } else {
                // a topic with its JSON path, or the path alone (HTTP)
                const where = mqtt ? [r.topic, r.json_path && r.json_path !== 'value' ? r.json_path : ''].filter(Boolean).join(' → ') : (r.json_path || '');
                first = `<code style="word-break:break-all;">${this._esc(where || '—')}</code>`;
            }
            const fcNote = modbus && (r.register_type || 'holding') !== 'holding' ? ` <span style="color:var(--text-secondary);">(${fcOf(r)[0]})</span>` : '';
            return `<tr>
                <td style="padding:2px 10px 2px 0;font-variant-numeric:tabular-nums;">${first}</td>
                <td style="padding:2px 10px 2px 0;"><code>${this._esc(r.name || '')}</code></td>
                <td style="padding:2px 10px 2px 0;">${this._esc(r.label || '')}</td>
                <td style="padding:2px 10px 2px 0;">${this._esc(r.data_type || '')}${fcNote}</td>
                <td style="padding:2px 10px 2px 0;text-align:right;font-variant-numeric:tabular-nums;" ${r.scale_from ? `title="dynamic SunSpec scale factor: engineering = raw × 10^${this._esc(r.scale_from)}"` : ''}>${r.scale_from ? `×10^<code>${this._esc(r.scale_from)}</code>` : this._esc(String(r.scale ?? 1))}</td>
                <td style="padding:2px 10px 2px 0;">${this._esc(r.unit || '')}</td>
                <td style="padding:2px 0;color:var(--text-secondary);">${this._esc(r.poll_group || '')}</td>
              </tr>`;
        }).join('');
        const table = `<div style="overflow-x:auto;"><table style="width:100%;font-size:12.5px;border-collapse:collapse;" data-tpl-view-regs>
            <thead><tr style="text-align:left;border-bottom:1px solid var(--border);color:var(--text-secondary);">
              <th style="padding:4px 10px 4px 0;">${firstHead}</th><th style="padding:4px 10px 4px 0;">${t('templates.name', 'Name')}</th>
              <th style="padding:4px 10px 4px 0;">${t('templates.label', 'Description')}</th><th style="padding:4px 10px 4px 0;">${t('templates.type', 'Type')}</th>
              <th style="padding:4px 10px 4px 0;text-align:right;">${t('templates.scale', 'Scale')}</th><th style="padding:4px 10px 4px 0;">${t('templates.unit', 'Unit')}</th>
              <th style="padding:4px 0;">${t('templates.poll', 'Poll')}</th></tr></thead>
            <tbody>${rows || `<tr><td colspan="7" style="color:var(--text-secondary);padding:8px 0;">${t('templates.noRegs', 'No registers.')}</td></tr>`}</tbody></table></div>`;
        document.getElementById('tmViewBody').innerHTML = meta + src + desc + also + '<section id="tmUsedBy"></section>' + table;
        document.getElementById('tmViewExportBtn').onclick = () => this.tplExport(id);
        document.getElementById('tmViewDupBtn').onclick = () => { this.closeModal('tmViewModal'); this.openTplEditor(id, true); };
        this.openModal('tmViewModal');
        this._ubMount('template', id, document.getElementById('tmUsedBy'));
    },

    // what else the map holds, beside its registers, read-only: each count
    // opens to the names (the editor's Advanced tabs and Raw JSON change them)
    _tplViewAlso(full) {
        const t = (k, d) => this.t(k, d);
        const calc = (full.calculated || []).map(c => [c.name, c.label || c.expr || '']);
        const cmds = Object.entries(full.commands || {}).map(([k, c]) => [k, (c && (c.label || c.description)) || '']);
        const chRaw = full.channels || {};
        const chans = Array.isArray(chRaw) ? chRaw.map(c => [c.name || c.id || '', c.label || ''])
            : Object.entries(chRaw).map(([k, c]) => [k, (c && (c.label || c.description || (c.combine && `${this.t('templates.combine', 'combine')}: ${c.combine}`))) || '']);
        const comps = Object.entries(full.components || {}).map(([k, c]) => [k, `${(c && c.name) || ''}${c && c.registers ? ` · ${this.tn('templates.nRegs', c.registers.length, '{n} registers')}` : ''}`]);
        const presets = (full.rule_presets || full.rules || []).map(p => [p.id || p.name || '', p.label || '']);
        const parts = [
            ['calculated', 'templates.nCalc', '{n} calculated', calc],
            ['commands', 'templates.nCmds', '{n} commands', cmds],
            ['channels', 'templates.nChannels', '{n} channels', chans],
            ['components', 'templates.nComps', '{n} components', comps],
            ['presets', 'templates.nPresets', '{n} rule examples', presets],
        ].filter(([, , , list]) => list.length);
        if (!parts.length) return '';
        return `<div data-tpl-view-also style="display:flex;gap:6px 18px;flex-wrap:wrap;font-size:12.5px;margin-bottom:10px;">
            <span style="color:var(--text-secondary);">${t('templates.alsoIn', 'Also in this map')}:</span>
            ${parts.map(([key, k, d, list]) => `<details data-also="${key}"><summary style="cursor:pointer;">${this._esc(this.tn(k, list.length, d))}</summary>
                <ul style="margin:4px 0 0;padding-left:18px;">${list.map(([n, l]) => `<li><code>${this._esc(n)}</code>${l ? ` — ${this._esc(l)}` : ''}</li>`).join('')}</ul></details>`).join('')}
          </div>`;
    },

    // ── CSV register-map import ───────────────────────────────────────────
    // managerMode: from the Template Manager the import SAVES a library template
    // (no device to assign to). From a device's Registers editor it imports and
    // assigns to that device (the original behaviour).
    openCsvImport(managerMode = false, fmt = 'csv') {
        this._csvManagerMode = !!managerMode;
        this._importFmt = fmt === 'yaml' ? 'yaml' : 'csv';
        if (!managerMode) {
            const id = this._regDevice || this._primaryDeviceId();
            if (id === this._primaryDeviceId()) {
                this.showToast('info', this.t('registers.upload.primaryTitle', 'Uses built-in map'),
                               this.t('registers.upload.primaryMsg',
                                      'The primary device keeps its built-in measurement map. Upload applies to other devices.'));
                return;
            }
        }
        this._csvTemplate = null;
        ['csvId', 'csvName', 'csvVendor', 'csvModel', 'csvText', 'csvByteOrder', 'csvTransport'].forEach(k => { const e = document.getElementById(k); if (e) e.value = ''; });
        document.getElementById('csvPreviewResult').innerHTML = '';
        // dress the shared modal for the chosen format
        const yaml = this._importFmt === 'yaml';
        const set = (id, prop, val) => { const e = document.getElementById(id); if (e) e[prop] = val; };
        const show = (id, on) => { const e = document.getElementById(id); if (e) e.style.display = on ? '' : 'none'; };
        set('csvModalTitle', 'innerHTML', yaml
            ? '<i aria-hidden="true" class="bi bi-filetype-yml"></i> ' + this._esc(this.t('yaml.title', 'Import register map from YAML'))
            : '<i aria-hidden="true" class="bi bi-filetype-csv"></i> ' + this._esc(this.t('csv.title', 'Import register map from CSV')));
        show('csvIntroCsv', !yaml); show('csvIntroYaml', yaml);
        show('csvExampleLink', !yaml);           // example download is CSV-only
        set('csvTextLabel', 'textContent', yaml ? 'YAML' : 'CSV');
        set('csvPickLabel', 'textContent', yaml ? this.t('yaml.pickFile', 'open a .yaml file')
                                                : this.t('csv.pickFile', 'open a .csv file'));
        set('csvFile', 'accept', yaml ? '.yaml,.yml,text/yaml' : '.csv,text/csv');
        set('csvText', 'placeholder', yaml
            ? 'registers:\n  - { address: 0x0000, name: voltage_l1_n, unit: V, data_type: float, scale: 1 }\n  - { address: 40, name: power_active_total, unit: W, data_type: int32, scale: 10 }'
            : 'address,name,label,unit,type,scale\n0x0000,V_L1,Voltage L1-N,V,float,1');
        const impBtn = document.getElementById('csvImportBtn');
        impBtn.disabled = true;
        impBtn.textContent = managerMode ? this.t('csv.importLib', 'Import to library')
                                         : this.t('csv.import', 'Import & assign');
        const file = document.getElementById('csvFile');
        if (file && !file._wired) {
            file._wired = true;
            file.addEventListener('change', async () => {
                const f = file.files[0]; file.value = '';
                if (f) document.getElementById('csvText').value = await f.text();
            });
        }
        this.openModal('csvImportModal');
    },

    async csvPreview(btn) {
        await this._loadCanonicalFields();   // ensure the dict is ready for the canonical count
        const yaml = this._importFmt === 'yaml';
        const text = document.getElementById('csvText').value;
        const box = document.getElementById('csvPreviewResult');
        if (!text.trim()) {
            box.innerHTML = `<span class="field-hint">${yaml
                ? this.t('yaml.noData', 'Paste a YAML map or open a file first.')
                : this.t('csv.noData', 'Paste a CSV or open a file first.')}</span>`;
            return;
        }
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="btn-spinner"></span>'; }
        try {
            const r = await fetch(yaml ? '/api/device-templates/import-yaml'
                                       : '/api/device-templates/import-csv', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    [yaml ? 'yaml' : 'csv']: text,
                    id: document.getElementById('csvId').value.trim(),
                    name: document.getElementById('csvName').value.trim(),
                    vendor: document.getElementById('csvVendor').value.trim(),
                    model: document.getElementById('csvModel').value.trim(),
                    default_data_type: document.getElementById('csvDefType').value,
                    byte_order: document.getElementById('csvByteOrder')?.value || '',
                    transports: (() => {
                        const v = document.getElementById('csvTransport')?.value || '';
                        return v ? [v] : [];
                    })(),
                }),
            });
            const res = await r.json();
            if (!r.ok) {
                this._csvTemplate = null;
                document.getElementById('csvImportBtn').disabled = true;
                box.innerHTML = `<div class="settings-card" style="padding:10px;color:var(--danger-text,#c0392b);">${(res.detail?.errors || ['import failed']).map(e => this._esc(e)).join('<br>')}</div>`;
                return;
            }
            this._csvTemplate = res.device_template;
            const verr = res.validation_errors || [];
            const warn = res.warnings || [];
            document.getElementById('csvImportBtn').disabled = verr.length > 0 || res.register_count === 0;
            // canonical check on the parsed names — nudge toward uniform naming
            const _inner = this._csvTemplate && (this._csvTemplate.device_template || this._csvTemplate);
            const _regs = (_inner && _inner.registers) || [];
            const _named = _regs.filter(r => r.name);
            const _canonOk = _named.filter(r => this._isCanonical(r.name)).length;
            const _nonCanon = _named.length - _canonOk;
            const canonLine = _named.length
                ? `<div style="margin-top:4px;font-size:12.5px;color:${_nonCanon ? 'var(--warning-text,#c77700)' : 'var(--success-text,#1a8f4c)'};">`
                  + `${_nonCanon ? '⚠' : '✓'} ${_canonOk}/${_named.length} ${this.t('devtpl.canonCount', 'canonical')}`
                  + (_nonCanon ? ` — ${this.t('csv.canonFix', 'edit the map after import to canonicalize names')}` : '')
                  + `</div>`
                : '';
            box.innerHTML = `<div class="settings-card" style="padding:10px 12px;">
                <div><b style="color:${verr.length ? 'var(--danger-text,#c0392b)' : 'var(--success-text,#1a8f4c)'};">${res.register_count}</b> ${this.t('csv.parsed', 'measurements parsed')}${(res.columns && res.columns.length) ? ` · ${this.t('csv.cols', 'columns')}: ${res.columns.map(c => this._esc(c)).join(', ')}` : ''}</div>
                ${canonLine}
                ${verr.length ? `<div style="color:var(--danger-text,#c0392b);font-size:12.5px;margin-top:6px;">${this.t('csv.fixFirst', 'Fix before importing')}: ${verr.slice(0, 5).map(e => this._esc(e)).join('<br>')}</div>` : ''}
                ${warn.length ? `<details style="margin-top:6px;"><summary style="cursor:pointer;color:var(--warning-text,#c77700);font-size:12.5px;">${warn.length} ${this.t('csv.warnings', 'skipped rows / coercions')}</summary><div style="font-size:12px;color:var(--text-secondary);margin-top:4px;">${warn.slice(0, 20).map(w => this._esc(w)).join('<br>')}</div></details>` : ''}</div>`;
        } catch (e) { box.innerHTML = `<span style="color:var(--danger-text,#c0392b);">${this._esc(e.message)}</span>`; }
        finally { if (btn) { btn.disabled = false; btn.innerHTML = orig; } }
    },

    async csvImport(btn) {
        if (!this._csvTemplate) return;
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="btn-spinner"></span>'; }
        try {
            if (this._csvManagerMode) {
                // Template Manager: save to the library only (no device assign).
                await this._tplUploadPost(this._csvTemplate, false);
            } else {
                const id = this._regDevice || this._primaryDeviceId();
                await this._uploadMapAndAssign(this._csvTemplate, id, false);
            }
            this.closeModal('csvImportModal');
        } finally { if (btn) { btn.disabled = false; btn.innerHTML = orig; } }
    },

    downloadCsvExample() {
        // canonical names in the example so imports start uniform (see docs/canonical-fields.md)
        const csv = 'address,name,label,unit,type,scale,category,json_path\n'
            + '0x0000,voltage_l1_n,Voltage L1-N,V,float,1,voltage,\n'
            + '0x0002,voltage_l2_n,Voltage L2-N,V,float,1,voltage,\n'
            + '40,power_active_total,Total active power,W,int32,10,power_active,\n'
            + '19000,frequency,Frequency,Hz,uint16,100,frequency,\n';
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
        a.download = 'register-map-example.csv';
        document.body.appendChild(a); a.click(); a.remove();
    },

    // ── Device Template editor / upload / export (Tier 2 Phase C) ──────────
    async _refreshWizTemplates(selectId = null) {
        try {
            const templates = (await (await fetch('/api/device-templates')).json()).templates || [];
            // The device-template handlers (new/upload/edit/delete) are shared with
            // the standalone Template Manager, which has no wizard context.
            if (this._devWiz) {
                this._devWiz.templates = templates;
                if (selectId) this._devWiz.data.template = selectId;
                if (this._devWiz.step === 2) this._devWizRender();
            }
            if (this.currentPage === 'templates') this.renderTemplateManager();
        } catch (e) { console.error(e); }
    },

    tplUpload() {
        const input = document.getElementById('devTplUploadInput');
        input.onchange = async () => {
            const file = input.files[0];
            input.value = '';
            if (!file) return;
            let data;
            try { data = JSON.parse(await file.text()); }
            catch (e) {
                this.showToast('error', this.t('devtpl.uploadFail', 'Upload failed'),
                               this.t('devtpl.badJson', 'Not valid JSON: ') + e.message);
                return;
            }
            await this._tplUploadPost(data, false);
        };
        input.click();
    },

    async _tplUploadPost(data, overwrite) {
        try {
            const r = await fetch('/api/device-templates/upload', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ template: data, overwrite }),
            });
            const res = await r.json();
            if (r.status === 409 && !res.detail?.builtin) {
                if (await this._tplOverwriteAsk(data?.device_template?.id || data?.id))
                    return this._tplUploadPost(data, true);
                return;
            }
            if (!r.ok) {
                const errs = res.detail?.errors || ['upload failed'];
                this.showToast('error', this.t('devtpl.uploadFail', 'Upload failed'),
                               errs.slice(0, 4).join('\n'));
                return;
            }
            this.showToast('success', this.t('devtpl.uploaded', 'Template uploaded'),
                           `${res.template.id} · ${res.template.registers} reg`);
            await this._refreshWizTemplates(res.template.id);
        } catch (e) {
            this.showToast('error', this.t('devtpl.uploadFail', 'Upload failed'), e.message);
        }
    },

    tplExport(id) {
        const a = document.createElement('a');
        a.href = `/api/device-templates/${encodeURIComponent(id)}/export`;
        a.download = `${id}.json`;
        a.click();
    },

    // an upload whose id is taken: replace that template, or keep it
    _tplOverwriteAsk(id) {
        return this.confirmAction({
            title: this.t('devtpl.overwriteTitle', 'Overwrite template {id}?', { id: id || '' }),
            message: this.t('devtpl.overwriteConfirm', 'A template with this id already exists. The uploaded file replaces it.'),
            confirmLabel: this.t('devtpl.overwrite', 'Overwrite'), danger: true,
        });
    },

    // who uses a map, as the gateway counts it: a standalone device, or the
    // installation its units belong to (once)
    _tplUsers(ids) {
        const out = new Map();
        for (const id of ids || []) {
            const d = (this._devices || []).find(x => x.id === id);
            if (d && d.endpoint_id) out.set(`installation:${d.endpoint_id}`, { type: 'installation', id: d.endpoint_id, label: d.endpoint_name || d.endpoint_id });
            else out.set(`device:${id}`, { type: 'device', id, label: (d && d.name) || id });
        }
        return [...out.values()];
    },

    async tplDelete(id) {
        // asks the server first: a template in use opens its View, whose
        // "Used by" says what uses it
        if (!await this._ubDelete({
            kind: 'template', id,
            title: this.t('devtpl.deleteTitle', 'Delete template {id}', { id }),
            message: this.t('devtpl.deleteConfirm', 'The template file is removed; nothing uses it.'),
        })) return;
        this.showToast('success', this.t('devtpl.deleted', 'Template deleted'), id);
        await this._refreshWizTemplates();
    },

    async openTplEditor(id, duplicate = false) {
        let data;
        if (id) {
            try {
                const full = await (await fetch(`/api/device-templates/${encodeURIComponent(id)}`)).json();
                data = full.device_template;
            } catch (e) {
                this.showToast('error', this.t('toast.template', 'Template'), e.message);
                return;
            }
            if (duplicate) {
                // a free id: duplicating twice must not give two maps one id
                let ids = new Set();
                try { ids = new Set(((await (await fetch('/api/device-templates')).json()).templates || []).map(x => x.id)); }
                catch (e) { /* the server refuses a taken id anyway */ }
                let n = 1, cand = `${data.id}-copy`;
                while (ids.has(cand)) cand = `${data.id}-copy-${++n}`;
                data.id = cand;
                data.name = `${data.name} (copy)`;
                data.author = 'user';
            }
        } else {
            data = { schema_version: 1, id: '', name: '', vendor: '', model: '', version: '1.0.0',
                     author: 'user', description: '',
                     protocol: { transports: ['tcp'], default_unit_id: 1, functions: [3],
                                 byte_order: 'big', max_registers_per_read: 125 },
                     poll_groups: { realtime: { interval: 1 }, normal: { interval: 5 }, slow: { interval: 60 } },
                     categories: { basic: { label: 'Basic', order: 1 } },
                     registers: [{ address: 0, name: '', label: '', unit: '', data_type: 'float',
                                   access: 'RD', category: 'basic', description: '' }] };
        }
        this._tplEdit = { data, isNew: !id || duplicate, openedId: (!id || duplicate) ? '' : id, search: '' };
        await this._loadCanonicalFields();   // canonical dictionary for name guidance (before first render)
        document.getElementById('devTplTitle').textContent =
            (!id ? this.t('devtpl.newTitle', 'New Device Template')
                 : duplicate ? this.t('devtpl.dupTitle', 'Duplicate Template')
                             : this.t('devtpl.editTitle', 'Edit Device Template')) +
            (data.id ? ` — ${data.id}` : '');
        this._tplEditorRender();
        this.openModal('devTplModal');
    },

    // Unit cell, flagged amber when the row's CANONICAL name expects a
    // different unit (the canonical dictionary is a unit contract too — a
    // kWh value under an energy_* name is a silent 1000x error downstream;
    // the Fronius 65A shipped mislabeled exactly this way).
    _tplUnitState(r) {
        const cf = r.name && this._canonicalFields
            && this._canonicalFields[String(r.name).toLowerCase()];
        const want = (cf && cf.unit) || '';
        const bad = !!(want && (r.unit || '') && r.unit !== want);
        const title = bad
            ? `${r.name}: ${this.t('devtpl.unitCanon', 'canonical unit is')} ${want} — `
              + this.t('devtpl.unitFix', 'adjust the scale to deliver it (a native-kWh map needs scale/1000), or rename the register')
            : (want ? `${this.t('devtpl.unitCanon', 'canonical unit is')} ${want}` : '');
        return { want, bad, title };
    },

    _tplUnitCell(r) {
        const { want, bad, title } = this._tplUnitState(r);
        return `<td style="white-space:nowrap"><input class="input tpl-cell${bad ? ' tpl-noncanon' : ''}" data-f="unit" `
             + `value="${this._esc(r.unit || '')}" style="width:56px" aria-label="${this._esc(this.t('devtpl.colUnit', 'Unit'))}"`
             + `${title ? ` title="${this._esc(title)}"` : ''}>`
             + `<button type="button" class="btn btn-ghost btn-sm tpl-unitfix"${bad ? '' : ' style="display:none"'} `
             + `title="${this._esc(this.t('devtpl.unitUse', 'Use the canonical unit (the value itself must be in it: check the scale)'))}">→ ${this._esc(want)}</button></td>`;
    },

    // Bring a row's Unit cell in line with its state (class, title, the fix button).
    _tplSyncUnit(tr, row) {
        const uinp = tr && tr.querySelector('input[data-f="unit"]');
        if (!uinp) return;
        const { want, bad, title } = this._tplUnitState(row);
        uinp.value = row.unit || '';
        uinp.classList.toggle('tpl-noncanon', bad);
        uinp.title = title;
        const fix = tr.querySelector('.tpl-unitfix');
        if (fix) { fix.style.display = bad ? '' : 'none'; fix.textContent = `→ ${want}`; }   // .btn's display beats [hidden]
    },

    // A canonical name promises a unit and a measurement (the row's
    // category). The unit is filled while it is empty or still what the
    // dictionary put there. The category is the InfluxDB measurement, so it
    // is filled only on a row added in this session (tplAddRow, _advAddCalc
    // mark it with its starting section): renaming a row or a calculated
    // field that was already in the map never moves its data to another
    // measurement. What the user typed stays. The marks map a row to what the
    // field held before the dictionary filled it, so a name that stops being
    // canonical gives the field back. They live beside the rows, never in the
    // saved template.
    _tplAuto() {
        return this._tplEdit.auto || (this._tplEdit.auto = { unit: new WeakMap(), category: new WeakMap() });
    },

    _tplCanonFill(row) {
        const cf = row.name && this._canonicalFields
            && this._canonicalFields[String(row.name).toLowerCase()];
        const auto = this._tplAuto();
        const unit = cf && cf.unit;
        if (unit && (!row.unit || auto.unit.has(row))) {
            if (!auto.unit.has(row)) auto.unit.set(row, row.unit || '');
            row.unit = unit;
        } else if (!unit && auto.unit.has(row)) {
            row.unit = auto.unit.get(row);
            auto.unit.delete(row);
        }
        // only a row this session added carries a category mark
        if (auto.category.has(row)) {
            const v = (cf && cf.measurement) || auto.category.get(row);
            if (v) row.category = v; else delete row.category;
        }
    },

    // a category the dictionary filled in on a new row (never one the user
    // typed) is declared in the template's sections, or Save would refuse it
    // as undeclared; a typo stays an error. A section the editor declared is
    // dropped again once no row uses it — never one the map had before.
    _tplDeclareCanonCats(d) {
        const cats = d.categories;
        if (!cats || !Object.keys(cats).length || !this._tplEdit.auto) return;
        const auto = this._tplEdit.auto.category;
        const mine = this._tplEdit.declaredCats || (this._tplEdit.declaredCats = new Set());
        const rows = [...(d.registers || []), ...(d.calculated || [])];
        const used = new Set(rows.map(r => r && r.category).filter(Boolean));
        for (const c of [...mine]) if (!used.has(c)) { delete cats[c]; mine.delete(c); }
        const dict = Object.values(this._canonicalFields || {}).filter(f => !f.user);
        let order = Math.max(0, ...Object.values(cats).map(c => Number(c && c.order) || 0));
        for (const row of rows) {
            const c = row && row.category;
            if (!c || c in cats || !auto.has(row)) continue;
            const f = dict.find(x => x.measurement === c);
            if (!f) continue;
            cats[c] = { label: f.measurement_label || c.replace(/_/g, ' ').replace(/^./, x => x.toUpperCase()), order: ++order };
            mine.add(c);
        }
    },

    // One template-editor Name cell, flagged amber + 'did you mean' title when
    // the name isn't canonical (uses the shared _isCanonical/_canonicalSuggest).
    _tplNameCell(r) {
        const canon = !r.name || this._isCanonical(r.name);
        const s = canon ? '' : this._canonicalSuggest(String(r.name).toLowerCase());
        const title = canon ? '' : (s
            ? `${this.t('registers.canon.non', 'Non-canonical name')} — ${this.t('registers.canon.didYouMean', 'did you mean')} ${s}?`
            : this.t('registers.canon.non', 'Non-canonical name'));
        return `<td><input class="input tpl-cell${canon ? '' : ' tpl-noncanon'}" data-f="name" `
            + `list="tplCanonList" value="${this._esc(r.name)}" aria-label="${this._esc(this.t('devtpl.colName', 'Name'))}" title="${this._esc(title)}"></td>`;
    },

    // "N/M canonical" summary for the editor header.
    _tplCanonSummaryText() {
        const regs = this._tplEdit?.data?.registers || [];
        const named = regs.filter(r => r.name);
        const ok = named.filter(r => this._isCanonical(r.name)).length;
        return `${ok}/${named.length} ${this.t('devtpl.canonCount', 'canonical')}`;
    },

    _tplEditorRender() {
        const e = this._tplEdit, d = e.data;
        const dataTypes = ['float', 'double', 'int16', 'uint16', 'int32', 'uint32',
                           'int64', 'uint64', 'sm16', 'sm32', 'string:8', 'string:16'];
        const proto = d.protocol || (d.protocol = {});
        const tr = (proto.transports || []).map(x => String(x).toLowerCase());
        const isModbus = this._tplIsModbus(tr);
        const usesPath = tr.includes('http') || tr.includes('mqtt');
        const usesTopic = tr.includes('mqtt');
        // Offer every category / poll group the registers actually use (bundled
        // templates ship categories:{} and no poll_groups block, yet their rows
        // reference voltage/current/realtime/slow/…), plus any declared — and
        // via a datalist you can also type a brand-new value.
        const cats = [...new Set([...Object.keys(d.categories || {}),
            ...d.registers.map(r => r.category).filter(Boolean)])].sort();
        const groups = [...new Set([...Object.keys(d.poll_groups || {}),
            ...d.registers.map(r => r.poll_group).filter(Boolean)])];
        const q = (e.search || '').toLowerCase();
        const matching = d.registers.map((r, i) => ({ r, i })).filter(({ r }) =>
            !q || `${r.address} ${r.name} ${r.label} ${r.description} ${r.category}`.toLowerCase().includes(q));
        const MAX = 150;
        const shown = matching.slice(0, MAX);
        const rows = shown.map(({ r, i }) => `
            <tr data-idx="${i}">
                <td><input class="input tpl-cell" data-f="address" type="number" min="0" max="65535" value="${r.address}" aria-label="${this._esc(this.t('devtpl.aria.address', 'Address'))}"></td>
                ${this._tplNameCell(r)}
                <td><input class="input tpl-cell" data-f="label" value="${this._esc(r.label || '')}" aria-label="${this._esc(this.t('devtpl.colLabel', 'Label'))}"></td>
                ${this._tplUnitCell(r)}
                <td><input class="input tpl-cell" data-f="data_type" list="tplTypeList" value="${this._esc(r.data_type || 'float')}" style="width:84px" aria-label="${this._esc(this.t('devtpl.aria.dataType', 'Data type'))}"></td>
                <td><input class="input tpl-cell" data-f="scale" type="number" step="any" value="${r.scale ?? 1}" style="width:68px" aria-label="${this._esc(this.t('devtpl.colScale', 'Scale'))}"></td>
                <td><input class="input tpl-cell" data-f="offset" type="number" step="any" value="${r.offset ?? 0}" style="width:68px" aria-label="${this._esc(this.t('devtpl.colOffset', 'Offset'))}" title="${this._esc(this.t('devtpl.tip.offset', 'engineering = raw / scale + offset'))}"></td>
                ${isModbus ? `<td><select class="input tpl-cell" data-f="register_type" aria-label="${this._esc(this.t('devtpl.aria.registerType', 'Register type'))}" title="${this._esc(this.t('devtpl.fcHint', 'Modbus function code: FC3 holding / FC4 input registers; FC1 coils / FC2 discrete inputs are single bits'))}">
                    <option value="holding" ${(r.register_type || 'holding') === 'holding' ? 'selected' : ''}>FC3</option>
                    <option value="input" ${r.register_type === 'input' ? 'selected' : ''}>FC4</option>
                    <option value="coil" ${r.register_type === 'coil' ? 'selected' : ''}>FC1</option>
                    <option value="discrete" ${r.register_type === 'discrete' ? 'selected' : ''}>FC2</option></select></td>` : ''}
                ${usesPath ? `<td><input class="input tpl-cell" data-f="json_path" value="${this._esc(r.json_path || '')}" style="width:150px" placeholder="Body.Data.PAC" aria-label="${this._esc(this.t('devtpl.aria.jsonPath', 'JSON path'))}"></td>` : ''}
                ${usesTopic ? `<td><input class="input tpl-cell" data-f="topic" value="${this._esc(r.topic || '')}" style="width:120px" placeholder="~/soc" aria-label="${this._esc(this.t('devtpl.aria.topic', 'MQTT topic'))}"></td>` : ''}
                ${usesTopic ? `<td><input class="input tpl-cell" data-f="write_topic" value="${this._esc(r.write_topic || '')}" style="width:120px" placeholder="~/soc/set" aria-label="${this._esc(this.t('devtpl.aria.writeTopic', 'MQTT write topic'))}"></td>` : ''}
                <td><input class="input tpl-cell" data-f="category" list="tplCatList" value="${this._esc(r.category || '')}" style="width:118px" aria-label="${this._esc(this.t('devtpl.colCategory', 'Category'))}"></td>
                <td><input class="input tpl-cell" data-f="poll_group" list="tplGroupList" value="${this._esc(r.poll_group || '')}" style="width:96px" aria-label="${this._esc(this.t('devtpl.colGroup', 'Poll group'))}"></td>
                <td style="text-align:center;"><input type="checkbox" class="tpl-cell" data-f="writable" ${r.writable ? 'checked' : ''} title="${this._esc(this.t('devtpl.tip.writable', 'Writable via the write API'))}" aria-label="${this._esc(this.t('devtpl.aria.writable', 'Writable'))}"></td>
                <td><input class="input tpl-cell" data-f="write_min" type="number" step="any" value="${r.write_min ?? ''}" style="width:60px" placeholder="min" aria-label="${this._esc(this.t('devtpl.aria.writeMin', 'Write min'))}"></td>
                <td><input class="input tpl-cell" data-f="write_max" type="number" step="any" value="${r.write_max ?? ''}" style="width:60px" placeholder="max" aria-label="${this._esc(this.t('devtpl.aria.writeMax', 'Write max'))}"></td>
                <td><input class="input tpl-cell" data-f="write_safe" type="number" step="any" value="${r.write_safe ?? ''}" style="width:60px" placeholder="safe" aria-label="${this._esc(this.t('devtpl.aria.writeSafe', 'Write safe (auto-revert)'))}"></td>
                <td style="white-space:nowrap;">
                    <button class="btn btn-ghost btn-sm" ${this._act('tplEditStates', [i])} title="${this.t('devtpl.editStates', 'Decode states (enum / bitfield)')}" aria-label="${this._esc(this.t('devtpl.aria.states', 'Decode states'))}" style="${(r.enum || r.bits) ? 'color:var(--accent,#3b82f6);' : ''}"><i aria-hidden="true" class="bi bi-list-ol"></i>${(r.enum || r.bits) ? ` <span style="font-size:10px;">${r.bits ? 'bits' : Object.keys(r.enum).length}</span>` : ''}</button>
                    <button class="btn btn-ghost btn-sm" ${this._act('tplRowExtras', [i])} title="${this._esc(this.t('adv.row.title', 'Row details: description, not-available marker, counters, scale factor, totals, Home Assistant'))}" aria-label="${this._esc(this.t('devtpl.aria.rowDetails', 'Row details'))}" style="${this._advRowHasExtras?.(r) ? 'color:var(--accent,#3b82f6);' : ''}"><i aria-hidden="true" class="bi bi-sliders"></i></button>
                    <button class="btn btn-ghost btn-sm" ${this._act('tplDelRow', [i])} title="${this.t('common.delete', 'Delete')}" aria-label="${this._esc(this.t('devtpl.aria.deleteRow', 'Delete row'))}"><i aria-hidden="true" class="bi bi-trash"></i></button>
                </td>
            </tr>`).join('');
        document.getElementById('devTplBody').innerHTML = `
        <div class="form-row">
            <div class="form-group">
                <label class="form-label" for="tplId">Id</label>
                <input class="input" id="tplId" value="${this._esc(d.id)}" ${e.isNew ? '' : 'disabled'}
                       pattern="[a-z0-9][a-z0-9_-]{1,63}">
                <div class="field-hint">a-z 0-9 - _</div>
            </div>
            <div class="form-group flex-2">
                <label class="form-label" for="tplName">${this.t('devtpl.name', 'Name')}</label>
                <input class="input" id="tplName" value="${this._esc(d.name)}">
            </div>
            <div class="form-group">
                <label class="form-label" for="tplVendor">${this.t('devtpl.vendor', 'Vendor')}</label>
                <input class="input" id="tplVendor" value="${this._esc(d.vendor || '')}">
            </div>
            <div class="form-group">
                <label class="form-label" for="tplModel">${this.t('devtpl.model', 'Model')}</label>
                <input class="input" id="tplModel" value="${this._esc(d.model || '')}">
            </div>
        </div>
        ${this._tplProtocolHtml(proto, d)}
        ${this._tplAdvancedHtml ? this._tplAdvancedHtml(d) : ''}
        <datalist id="tplTypeList">${dataTypes.map(t => `<option value="${t}"></option>`).join('')}</datalist>
        <div style="display:flex;gap:8px;align-items:center;margin:8px 0;flex-wrap:wrap;">
            <input type="text" id="tplSearch" class="input" placeholder="${this.t('common.search', 'Search…')}…"
                   value="${this._esc(e.search || '')}" style="max-width:220px;">
            <button class="btn btn-secondary btn-sm" data-action="tplAddRow">
                <i aria-hidden="true" class="bi bi-plus-lg"></i> ${this.t('devtpl.addRegister', 'Add measurement')}</button>
            <button data-admin class="btn btn-secondary btn-sm" data-action="tplAutoCanonicalize"
                    title="${this._esc(this.t('devtpl.autoCanonHint', 'Infer canonical names from each row\'s label/unit (conservative — leaves anything uncertain untouched). Review, then Save.'))}">
                <i aria-hidden="true" class="bi bi-magic"></i> ${this.t('devtpl.autoCanon', 'Auto-canonicalize')}</button>
            <span class="field-hint">${matching.length > MAX
                ? this.t('devtpl.showing', 'showing') + ` ${MAX} / ${matching.length}`
                : `${matching.length} ${this.t('devices.regsSelected', 'measurements')}`}
                · ${d.registers.length} ${this.t('devtpl.total', 'total')}</span>
            <label class="checkbox-label" style="margin-left:auto;" title="${this._esc(this.t('devtpl.canonHint', 'Validate register names against the canonical dictionary (flags non-canonical names).'))}">
                <input type="checkbox" id="tplCanonical" ${d.canonical ? 'checked' : ''}>
                <span>${this.t('devtpl.canonical', 'Canonical map')}</span></label>
            <span class="field-hint" id="tplCanonSummary">${this._esc(this._tplCanonSummaryText())}</span>
        </div>
        <datalist id="tplCanonList">${(this._canonicalNames || []).map(n => {
            const f = this._canonicalFields[n];
            return `<option value="${this._esc(n)}">${this._esc(f.description || '')}</option>`;
        }).join('')}</datalist>
        <datalist id="tplCatList">${cats.map(c => `<option value="${this._esc(c)}"></option>`).join('')}</datalist>
        <datalist id="tplGroupList">${groups.map(g => `<option value="${this._esc(g)}"></option>`).join('')}</datalist>
        <div class="table-container" style="max-height:320px;overflow:auto;">
            <table class="data-table tpl-table">
                <thead><tr><th>${this.t('devtpl.colAddr', 'Addr')}</th><th>${this.t('devtpl.colName', 'Name')}</th><th>${this.t('devtpl.colLabel', 'Label')}</th><th>${this.t('devtpl.colUnit', 'Unit')}</th><th>${this.t('devtpl.colType', 'Type')}</th><th>${this.t('devtpl.colScale', 'Scale')}</th><th title="${this._esc(this.t('devtpl.tip.offset', 'engineering = raw / scale + offset'))}">${this.t('devtpl.colOffset', 'Offset')}</th>${isModbus ? `<th title="${this._esc(this.t('devtpl.tip.fc', 'Function code'))}">${this.t('devtpl.colFC', 'FC')}</th>` : ''}${usesPath ? `<th title="${this._esc(this.t('devtpl.jsonPathHint', 'Where the value sits in the JSON payload, dot/bracket path'))}">json_path</th>` : ''}${usesTopic ? `<th title="${this._esc(this.t('devtpl.topicHint', 'Its own MQTT topic; ~/leaf is relative to the device topic'))}">topic</th>` : ''}${usesTopic ? `<th title="${this._esc(this.t('devtpl.writeTopicHint', 'Where a command publishes this row’s new value; what arrives on its topic is the read-back. Empty: not written over MQTT.'))}">write topic</th>` : ''}<th>${this.t('devtpl.colCategory', 'Category')}</th><th>${this.t('devtpl.colGroup', 'Poll group')}</th><th title="${this._esc(this.t('devtpl.tip.writable', 'Writable via the write API'))}">${this.t('devtpl.colWr', 'Wr')}</th><th>${this.t('devtpl.colMin', 'Min')}</th><th>${this.t('devtpl.colMax', 'Max')}</th><th title="${this._esc(this.t('devtpl.tip.safe', 'Auto-revert value on lease expiry'))}">${this.t('devtpl.colSafe', 'Safe')}</th><th></th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
        <div class="field-error" id="tplErrors" style="display:none;white-space:pre-line;"></div>`;
        // wire cell edits back into the working copy
        document.querySelectorAll('#devTplBody .tpl-cell').forEach(inp => {
            inp.addEventListener('change', () => {
                const idx = parseInt(inp.closest('tr').dataset.idx, 10);
                const f = inp.dataset.f;
                let v;
                if (inp.type === 'checkbox') v = inp.checked;
                else if (f === 'address') v = parseInt(inp.value, 10);
                else if (f === 'scale') v = parseFloat(inp.value) || 1;
                else if (f === 'offset') v = parseFloat(inp.value) || 0;
                else if (f === 'write_min' || f === 'write_max' || f === 'write_safe')
                    v = inp.value === '' ? null : parseFloat(inp.value);
                else v = inp.value;
                // an edit that lands after the table was redrawn or the
                // editor closed (the blur of a removed field) has no row left
                const row = inp.isConnected && this._tplEdit?.data?.registers?.[idx];
                if (!row) return;
                row[f] = v;
                const auto = this._tplEdit.auto;
                if (auto && (f === 'unit' || f === 'category')) auto[f].delete(row);   // typed by hand: it stays
                if (f === 'name') {
                    this._tplCanonFill(row);
                    const cat = inp.closest('tr').querySelector('input[data-f="category"]');
                    if (cat) cat.value = row.category || '';
                }
                if (f === 'unit' || f === 'name') this._tplSyncUnit(inp.closest('tr'), row);   // live unit-contract feedback
                if (f === 'name') {
                    // live canonical feedback: flag the cell + refresh the summary
                    const canon = !v || this._isCanonical(v);
                    inp.classList.toggle('tpl-noncanon', !canon);
                    const s = canon ? '' : this._canonicalSuggest(String(v).toLowerCase());
                    inp.title = canon ? '' : (s
                        ? `${this.t('registers.canon.non', 'Non-canonical name')} — ${this.t('registers.canon.didYouMean', 'did you mean')} ${s}?`
                        : this.t('registers.canon.non', 'Non-canonical name'));
                    const sum = document.getElementById('tplCanonSummary');
                    if (sum) sum.textContent = this._tplCanonSummaryText();
                }
            });
        });
        document.querySelectorAll('#devTplBody .tpl-unitfix').forEach(btn => {
            btn.addEventListener('click', () => {
                const tr = btn.closest('tr');
                const row = this._tplEdit.data.registers[parseInt(tr.dataset.idx, 10)];
                row.unit = this._tplUnitState(row).want;
                if (this._tplEdit.auto) this._tplEdit.auto.unit.delete(row);
                this._tplSyncUnit(tr, row);
            });
        });
        this._tplWireProtocol();
        if (this._tplWireAdvanced) this._tplWireAdvanced();
        const search = document.getElementById('tplSearch');
        search.addEventListener('input', () => {
            this._tplCollectMeta();
            this._tplEdit.search = search.value;
            this._tplEditorRender();
            const s = document.getElementById('tplSearch');
            s.focus(); s.setSelectionRange(s.value.length, s.value.length);
        });
    },

    _tplCollectMeta() {
        const d = this._tplEdit.data;
        this._tplDeclareCanonCats(d);
        const g = id => document.getElementById(id);
        if (this._tplEdit.isNew) d.id = g('tplId')?.value.trim() ?? d.id;
        d.name = g('tplName')?.value.trim() ?? d.name;
        d.vendor = g('tplVendor')?.value.trim() ?? d.vendor;
        d.model = g('tplModel')?.value.trim() ?? d.model;
        const canon = g('tplCanonical');
        if (canon) d.canonical = canon.checked;
        const p = d.protocol || (d.protocol = {});
        const tr = (p.transports || []);
        if (this._tplIsModbus(tr)) {
            const uid = parseInt(g('tplUnitId')?.value, 10);
            if (Number.isFinite(uid)) p.default_unit_id = uid;
            const gap = parseFloat(g('tplWriteGap')?.value);
            if (Number.isFinite(gap) && gap > 0) p.min_write_gap_s = gap; else delete p.min_write_gap_s;
            const single = g('tplSingleWrites');
            if (single) { if (single.checked) p.single_register_writes = true; else delete p.single_register_writes; }
            const mx = parseInt(g('tplMaxRead')?.value, 10);
            if (Number.isFinite(mx)) p.max_registers_per_read = mx;
            if (g('tplByteOrder')) p.byte_order = g('tplByteOrder').value;
        }
        if (tr.includes('mqtt') && g('tplAvTopic')) {
            // how an MQTT source says it is alive, and how it is asked to keep publishing
            const avTopic = g('tplAvTopic').value.trim();
            if (avTopic) p.availability = { topic: avTopic, online: g('tplAvOn').value || 'online',
                                            offline: g('tplAvOff').value || 'offline' };
            else delete p.availability;
            const kaTopic = g('tplKaTopic').value.trim();
            if (kaTopic) {
                const every = parseFloat(g('tplKaEvery').value);
                p.keepalive = { topic: kaTopic, first_payload: g('tplKaFirst').value,
                                payload: g('tplKaPayload').value, every_s: Number.isFinite(every) ? every : 30 };
            } else delete p.keepalive;
        }
    },

    // ── an MQTT source: is it alive, and does it need to be asked ──────────
    _tplMqttProtoHtml(proto) {
        const t = (k, def) => this.t(k, def);
        const av = proto.availability || {}, ka = proto.keepalive || {};
        const v = x => this._esc(x ?? '');
        return `
            <div class="form-row">
              <div class="form-group flex-2">
                <label class="form-label" for="tplAvTopic">${t('devtpl.avTopic', 'Availability topic (MQTT)')}</label>
                <input class="input" id="tplAvTopic" value="${v(av.topic)}" placeholder="~/status">
                <div class="field-hint">${t('devtpl.avHint', 'Where the device says it is online or offline (its birth and last will). Empty: judged by how recently something arrived.')}</div>
              </div>
              <div class="form-group"><label class="form-label" for="tplAvOn">online</label>
                <input class="input" id="tplAvOn" value="${v(av.online || 'online')}"></div>
              <div class="form-group"><label class="form-label" for="tplAvOff">offline</label>
                <input class="input" id="tplAvOff" value="${v(av.offline || 'offline')}"></div>
            </div>
            <div class="form-row">
              <div class="form-group flex-2">
                <label class="form-label" for="tplKaTopic">${t('devtpl.kaTopic', 'Keepalive topic')}</label>
                <input class="input" id="tplKaTopic" value="${v(ka.topic)}" placeholder="R/{portal}/keepalive">
                <div class="field-hint">${t('devtpl.kaHint', 'For a device that publishes only while it is asked (a Victron GX). The first payload goes out on connect, the other every N seconds. A device can turn it off with keepalive: false.')}</div>
              </div>
              <div class="form-group"><label class="form-label" for="tplKaFirst">${t('devtpl.kaFirst', 'First payload')}</label>
                <input class="input" id="tplKaFirst" value="${v(ka.first_payload ?? ka.payload)}"></div>
              <div class="form-group flex-2"><label class="form-label" for="tplKaPayload">${t('devtpl.kaPayload', 'Then')}</label>
                <input class="input" id="tplKaPayload" value="${v(ka.payload)}"></div>
              <div class="form-group"><label class="form-label" for="tplKaEvery">${t('devtpl.kaEvery', 'Every (s)')}</label>
                <input class="input" id="tplKaEvery" type="number" min="5" max="3600" value="${v(ka.every_s ?? 30)}"></div>
            </div>`;
    },

    // ── how the device is reached and how its words are laid out ──────────
    _tplProtocolHtml(proto, d) {
        const t = (k, def) => this.t(k, def);
        const tr = (proto.transports || []).map(x => String(x).toLowerCase());
        const TRANSPORTS = [['tcp', 'Modbus TCP'], ['rtu', 'Modbus RTU'], ['rtu_tap', t('devtpl.trTap', 'RTU listen-only')],
                            ['http', 'HTTP/JSON'], ['mqtt', 'MQTT']];
        const bo = proto.byte_order || 'big';
        // word order, unit id and read size mean something only over Modbus
        const modbus = this._tplIsModbus(tr);
        const ORDERS = [
            ['big', 'ABCD', t('devtpl.boBig', 'big-endian, high word first — the Modbus default (most meters)')],
            ['little', 'CDAB', t('devtpl.boLittle', 'word-swapped: low word first (common on many meters and PLCs)')],
            ['badc', 'BADC', t('devtpl.boBadc', 'bytes swapped inside each word')],
            ['dcba', 'DCBA', t('devtpl.boDcba', 'fully little-endian')]];
        // every group the rows use, with its interval (declared, else the
        // gateway's default) — a map without a poll_groups block still shows them
        const DEF = { realtime: 1, normal: 5, slow: 60 };
        // a row without a group reads on `normal`: show it, so its rate can be set
        const names = [...new Set([...Object.keys(d.poll_groups || {}),
            ...(d.registers || []).map(r => r.poll_group || 'normal')])];
        const groups = names.map(n => [n, (d.poll_groups || {})[n] || { interval: DEF[n] ?? 5 }]);
        return `
        <details class="tpl-proto" open>
          <summary><i aria-hidden="true" class="bi bi-plug"></i> ${t('devtpl.protoTitle', 'How the device is read')}
            <a class="tpl-guide" href="https://github.com/sm26449/multi-bus-gateway/blob/main/docs/device-templates.md" target="_blank" rel="noopener">${t('devtpl.guide', 'Guide: writing a template')} <i aria-hidden="true" class="bi bi-box-arrow-up-right"></i></a></summary>
          <div class="tpl-proto-body">
            <div class="form-group">
              <label class="form-label">${t('devtpl.transports', 'Ways to reach it')}</label>
              <div class="tpl-proto-checks">${TRANSPORTS.map(([v, l]) => `
                <label class="checkbox-label"><input type="checkbox" class="tpl-tr" value="${v}" ${tr.includes(v) ? 'checked' : ''}> <span>${l}</span></label>`).join('')}</div>
              <div class="field-hint">${t('devtpl.transportsHint', 'What the map is written for. HTTP and MQTT rows read a value by its json_path instead of an address; the columns appear when you tick them.')}</div>
            </div>
            <div class="form-row" ${modbus ? '' : 'style="display:none"'}>
              <div class="form-group flex-2">
                <label class="form-label" for="tplByteOrder">${t('devtpl.byteOrder', 'Word / byte order (32- and 64-bit values)')}</label>
                <select class="input" id="tplByteOrder">${ORDERS.map(([v, code, help]) => `
                  <option value="${v}" ${bo === v ? 'selected' : ''}>${code} · ${this._esc(help)}</option>`).join('')}</select>
                <div class="field-hint">${t('devtpl.byteOrderHint', 'If 230 V reads as a huge or tiny number, the order is wrong: try CDAB. Diagnostics → Register Probe shows every order side by side on the live device.')}</div>
              </div>
              <div class="form-group">
                <label class="form-label" for="tplUnitId">${t('devtpl.unitId', 'Default unit ID')}</label>
                <input class="input" id="tplUnitId" type="number" min="0" max="255" value="${proto.default_unit_id ?? 1}">
              </div>
              <div class="form-group">
                <label class="form-label" for="tplMaxRead">${t('devtpl.maxRead', 'Max registers per read')}</label>
                <input class="input" id="tplMaxRead" type="number" min="1" max="125" value="${proto.max_registers_per_read ?? 125}">
                <div class="field-hint">${t('devtpl.maxReadHint', 'Lower it (e.g. 40) for devices that reject long reads.')}</div>
              </div>
              <div class="form-group">
                <label class="form-label" for="tplWriteGap">${t('devtpl.writeGap', 'Pause between writes (s)')}</label>
                <input class="input" id="tplWriteGap" type="number" min="0" max="60" step="0.5" value="${proto.min_write_gap_s ?? 0}">
                <div class="field-hint">${t('devtpl.writeGapHint', 'For interfaces that relay writes slowly and choke on a burst (e.g. 2). 0 = none.')}</div>
              </div>
              <div class="form-group">
                <label class="checkbox-label"><input type="checkbox" id="tplSingleWrites" ${proto.single_register_writes ? 'checked' : ''}> ${t('devtpl.singleWrites', 'Single-register writes only (FC6)')}</label>
                <div class="field-hint">${t('devtpl.singleWritesHint', 'For devices that refuse FC16: every write goes out as one FC6 per register, in order.')}</div>
              </div>
            </div>
            ${tr.includes('mqtt') ? this._tplMqttProtoHtml(proto) : ''}
            <div class="form-group">
              <label class="form-label">${t('devtpl.pollGroups', 'Poll groups (seconds between reads)')}</label>
              <div class="tpl-proto-groups">${groups.map(([n, g]) => `
                <label class="tpl-pg"><code>${this._esc(n)}</code>
                  <input class="input input-sm tpl-pg-iv" data-pg="${this._esc(n)}" type="number" min="0.05" step="any" value="${g.interval ?? 5}" aria-label="${this._esc(n)} interval"></label>`).join('')}
                <button class="btn btn-ghost btn-sm" data-action="tplAddPollGroup"><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('devtpl.addGroup', 'Group')}</button>
              </div>
            </div>
          </div>
        </details>`;
    },

    _tplWireProtocol() {
        const d = this._tplEdit.data;
        const p = d.protocol || (d.protocol = {});
        document.querySelectorAll('#devTplBody .tpl-tr').forEach(cb => cb.addEventListener('change', () => {
            this._tplCollectMeta();
            p.transports = [...document.querySelectorAll('#devTplBody .tpl-tr:checked')].map(x => x.value);
            if (p.transports.length && !this._tplIsModbus(p.transports)) {
                // an HTTP / MQTT map carries no Modbus settings
                ['functions', 'max_registers_per_read', 'byte_order', 'default_unit_id'].forEach(k => delete p[k]);
            }
            this._tplEditorRender();       // json_path / topic / FC columns follow
        }));
        document.querySelectorAll('#devTplBody .tpl-pg-iv').forEach(inp => inp.addEventListener('change', () => {
            // once a map declares poll_groups, every group a row uses must be
            // declared — so write them all, not just the one edited
            const all = {};
            document.querySelectorAll('#devTplBody .tpl-pg-iv').forEach(x => {
                const v = parseFloat(x.value);
                all[x.dataset.pg] = { ...((d.poll_groups || {})[x.dataset.pg] || {}), interval: v > 0 ? v : 5 };
            });
            d.poll_groups = all;
        }));
    },

    async tplAddPollGroup() {
        this._tplCollectMeta();
        const have = Object.keys(this._tplEdit.data.poll_groups || {});
        const v = await this.askValue({
            title: this.t('devtpl.groupAdd', 'New poll group'),
            kind: 'text', label: this.t('devtpl.groupName', 'Name of the new poll group (e.g. fast, hourly):'),
            hint: this.t('devtpl.groupNameRule', 'Lower-case letters, digits and _, starting with a letter.'),
            validate: x => !/^[a-z][a-z0-9_]{0,31}$/.test(x.toLowerCase())
                ? this.t('devtpl.groupNameRule', 'Lower-case letters, digits and _, starting with a letter.')
                : have.includes(x.toLowerCase()) ? this.t('devtpl.groupExists', 'This poll group already exists.') : null,
            confirmLabel: this.t('common.add', 'Add'),
        });
        if (v === null || !this._tplEdit) return;
        const name = v.toLowerCase();
        const d = this._tplEdit.data;
        const all = {};
        document.querySelectorAll('#devTplBody .tpl-pg-iv').forEach(x => {
            const v = parseFloat(x.value);
            all[x.dataset.pg] = { ...((d.poll_groups || {})[x.dataset.pg] || {}), interval: v > 0 ? v : 5 };
        });
        d.poll_groups = { ...all, [name]: { interval: 10 } };
        this._tplEditorRender();
    },

    // Infer canonical names for the non-canonical rows from label/name/unit.
    // Conservative: only renames on a confident, dictionary-validated guess and
    // never creates a duplicate (a colliding guess is left for the human). Purely
    // in-memory — the user reviews the grid and still has to Save.
    async tplAutoCanonicalize() {
        this._tplCollectMeta();
        const regs = this._tplEdit.data.registers;
        // classify the non-canonical rows on the server (the single, unit-tested
        // source of truth — the classifier deliberately returns null when unsure)
        const idx = [], batch = [];
        regs.forEach((r, i) => {
            if (this._isCanonical(r.name)) return;
            idx.push(i);
            batch.push({ name: r.name, label: r.label, unit: r.unit, description: r.description });
        });
        let guesses = [];
        if (batch.length) {
            try {
                const rsp = await fetch('/api/canonical-fields/guess', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ registers: batch }),
                });
                if (!rsp.ok) throw new Error(`HTTP ${rsp.status}`);
                guesses = (await rsp.json()).guesses || [];
            } catch (e) {
                this.showToast('error', this.t('devtpl.autoCanon', 'Auto-canonicalize'), e.message);
                return;
            }
        }
        // apply, never creating a duplicate name (a colliding guess is left amber)
        const taken = new Set(regs.filter(r => this._isCanonical(r.name))
                                  .map(r => String(r.name).toLowerCase()));
        let mapped = 0, conflict = 0, unresolved = 0;
        idx.forEach((ri, k) => {
            const g = guesses[k];
            if (!g) { unresolved++; return; }
            if (taken.has(g)) { conflict++; return; }
            regs[ri].name = g; taken.add(g); mapped++;
            this._tplCanonFill(regs[ri]);                  // and the unit / category it promises, where empty
        });
        if (mapped) this._tplEdit.data.canonical = true;   // opt into validation
        this._tplEdit.search = '';                         // show all rows so renames are visible
        this._tplEditorRender();
        const parts = [`${mapped} ${this.t('devtpl.canonMapped', 'renamed')}`];
        if (conflict) parts.push(`${conflict} ${this.t('devtpl.canonConflict', 'conflicts')}`);
        if (unresolved) parts.push(`${unresolved} ${this.t('devtpl.canonUnresolved', 'left for review')}`);
        this.showToast(conflict || unresolved ? 'info' : 'success',
                       this.t('devtpl.autoCanon', 'Auto-canonicalize'), parts.join(' · '));
    },

    tplAddRow() {
        this._tplCollectMeta();
        const d = this._tplEdit.data;
        const cat = Object.keys(d.categories || {})[0] || 'basic';
        const row = { address: 0, name: '', label: '', unit: '',
                      data_type: 'float', scale: 1, register_type: 'holding',
                      access: 'RD', category: cat, poll_group: '', description: '' };
        // the section is the editor's guess, not the user's: a canonical name
        // replaces it, and gives it back when the name stops being canonical
        this._tplAuto().category.set(row, cat);
        d.registers.push(row);
        this._tplEdit.search = '';
        this._tplEditorRender();
        const body = document.querySelector('#devTplBody .table-container');
        if (body) body.scrollTop = body.scrollHeight;
    },

    tplDelRow(idx) {
        this._tplCollectMeta();
        this._tplEdit.data.registers.splice(idx, 1);
        this._tplEditorRender();
    },

    // ── enum / bitfield state builder ────────────────────────────────────────
    // Build a register's decode map from a small table instead of hand-writing
    // JSON, so the format is always valid (unique integer keys, non-empty
    // labels). Writes reg.enum / reg.bits on the in-memory working copy; Save
    // ships it like any other register field.
    _enumParseInt(s) {
        s = String(s).trim();
        if (!s) return NaN;
        return /^0x[0-9a-f]+$/i.test(s) ? parseInt(s, 16) : parseInt(s, 10);
    },

    tplEditStates(idx) {
        this._tplCollectMeta();
        const reg = this._tplEdit.data.registers[idx];
        let mode = 'enum', map = reg.enum;
        if (reg.bits) { mode = 'bits'; map = reg.bits; }
        const rows = Object.keys(map || {})
            .sort((a, b) => this._enumParseInt(a) - this._enumParseInt(b))
            .map(k => ({ v: k, label: map[k] }));
        if (!rows.length) rows.push({ v: '', label: '' });
        this._enumBuilder = { idx, mode, rows,
                              mask: reg.mask != null ? '0x' + Number(reg.mask).toString(16) : '',
                              shift: reg.shift != null ? String(reg.shift) : '' };
        document.getElementById('enumBuilderTitle').textContent =
            `${this.t('enum.title', 'Decode states')} · ${reg.name || reg.label || ('0x' + Number(reg.address || 0).toString(16))}`;
        document.querySelector(`input[name="enumMode"][value="${mode}"]`).checked = true;
        const el = id => document.getElementById(id);
        el('enumMask').value = this._enumBuilder.mask;
        el('enumShift').value = this._enumBuilder.shift;
        el('enumBuilderError').style.display = 'none';
        this._enumBuilderSyncMode();
        this.enumBuilderRenderRows();
        this.openModal('enumBuilderModal');
    },

    _enumBuilderSyncMode() {
        const bits = this._enumBuilder.mode === 'bits';
        document.getElementById('enumValCol').textContent = bits ? this.t('enum.bitCol', 'Bit (0-63)') : this.t('enum.value', 'Value');
        document.getElementById('enumAdvanced').style.display = bits ? 'none' : '';
    },

    enumBuilderSetMode(mode) {
        this._enumBuilderCollect();               // keep what's typed
        this._enumBuilder.mode = mode;
        this._enumBuilderSyncMode();
        this.enumBuilderRenderRows();
    },

    enumBuilderRenderRows() {
        const body = document.getElementById('enumBuilderRows');
        body.innerHTML = this._enumBuilder.rows.map((row, i) => `
            <tr>
              <td><input class="input enum-cell" data-f="v" data-i="${i}" value="${this._esc(row.v)}" style="width:120px;" placeholder="${this._enumBuilder.mode === 'bits' ? '0' : 'e.g. 4'}"></td>
              <td><input class="input enum-cell" data-f="label" data-i="${i}" value="${this._esc(row.label)}" placeholder="e.g. ${this._enumBuilder.mode === 'bits' ? 'overtemp' : 'MPPT'}"></td>
              <td><button class="btn btn-ghost btn-sm" ${this._act('enumBuilderDelRow', [i])} aria-label="${this._esc(this.t('common.remove', 'Remove'))}"><i aria-hidden="true" class="bi bi-x-lg"></i></button></td>
            </tr>`).join('');
        body.querySelectorAll('.enum-cell').forEach(inp => inp.addEventListener('change', () => {
            const r = this._enumBuilder.rows[parseInt(inp.dataset.i, 10)];
            if (r) r[inp.dataset.f] = inp.value;
        }));
    },

    _enumBuilderCollect() {
        document.querySelectorAll('#enumBuilderRows .enum-cell').forEach(inp => {
            const r = this._enumBuilder.rows[parseInt(inp.dataset.i, 10)];
            if (r) r[inp.dataset.f] = inp.value;
        });
    },

    enumBuilderAddRow() {
        this._enumBuilderCollect();
        this._enumBuilder.rows.push({ v: '', label: '' });
        this.enumBuilderRenderRows();
    },

    enumBuilderDelRow(i) {
        this._enumBuilderCollect();
        this._enumBuilder.rows.splice(i, 1);
        if (!this._enumBuilder.rows.length) this._enumBuilder.rows.push({ v: '', label: '' });
        this.enumBuilderRenderRows();
    },

    enumBuilderClear() {
        const reg = this._tplEdit.data.registers[this._enumBuilder.idx];
        delete reg.enum; delete reg.bits; delete reg.mask; delete reg.shift;
        this.closeModal('enumBuilderModal');
        this._tplEditorRender();
    },

    enumBuilderApply() {
        this._enumBuilderCollect();
        const b = this._enumBuilder;
        const errBox = document.getElementById('enumBuilderError');
        const fail = msg => { errBox.textContent = msg; errBox.style.display = 'block'; };
        const map = {}, seen = new Set();
        const nonEmpty = b.rows.filter(r => String(r.v).trim() !== '' || String(r.label).trim() !== '');
        if (!nonEmpty.length) return this.enumBuilderClear();   // emptied → remove the map
        for (const r of nonEmpty) {
            const n = this._enumParseInt(r.v);
            if (!Number.isInteger(n) || n < 0) return fail(`Value "${r.v}" must be a non-negative integer (decimal or 0x…).`);
            if (b.mode === 'bits' && n > 63) return fail(`Bit ${n} is out of range (0-63).`);
            if (seen.has(n)) return fail(`Duplicate ${b.mode === 'bits' ? 'bit' : 'value'} ${n}.`);
            const label = String(r.label).trim();
            if (!label) return fail(`${b.mode === 'bits' ? 'Bit' : 'Value'} ${n} needs a label.`);
            seen.add(n);
            map[n] = label;
        }
        const reg = this._tplEdit.data.registers[b.idx];
        delete reg.enum; delete reg.bits; delete reg.mask; delete reg.shift;
        if (b.mode === 'bits') {
            reg.bits = map;
        } else {
            reg.enum = map;
            const mask = document.getElementById('enumMask').value.trim();
            const shift = document.getElementById('enumShift').value.trim();
            if (mask) {
                const m = this._enumParseInt(mask);
                if (!Number.isInteger(m) || m < 0) return fail(`mask "${mask}" is not a valid integer.`);
                reg.mask = m;
            }
            if (shift) {
                const s = parseInt(shift, 10);
                if (!Number.isInteger(s) || s < 0) return fail('shift must be a non-negative integer.');
                reg.shift = s;
            }
        }
        this.closeModal('enumBuilderModal');
        this._tplEditorRender();
    },

    async tplSave() {
        this._tplCollectMeta();
        const d = this._tplEdit.data;
        const fb = document.getElementById('devTplFeedback');
        const errBox = document.getElementById('tplErrors');
        // a new map, a duplicate, or an edit saved under another id must not
        // replace an existing template silently
        const create = !!this._tplEdit.isNew || (this._tplEdit.openedId && d.id !== this._tplEdit.openedId);
        try {
            let r = await fetch('/api/device-templates', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ device_template: d, ...(create ? { create: true } : {}) }),
            });
            let res = await r.json();
            if (r.status === 409) {
                if (res.detail?.builtin) {
                    errBox.textContent = this.t('devtpl.builtinTaken', 'A built-in template already has the id "{id}" — choose another id.')
                        .replace('{id}', d.id);
                    errBox.style.display = '';
                    fb.textContent = ''; fb.className = 'save-feedback';
                    return;
                }
                if (!await this.confirmAction({
                    title: this.t('devtpl.overwriteTitle', 'Overwrite template {id}?', { id: d.id }),
                    message: this.t('devtpl.overwriteSaveConfirm',
                        'A template with the id "{id}" already exists. Replace it with this one, or cancel and change the id here.', { id: d.id }),
                    confirmLabel: this.t('devtpl.replace', 'Replace'), danger: true,
                })) return;
                r = await fetch('/api/device-templates', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ device_template: d }),
                });
                res = await r.json();
            }
            if (!r.ok) {
                const errs = res.detail?.errors || ['save failed'];
                errBox.textContent = errs.slice(0, 8).join('\n');
                errBox.style.display = '';
                fb.textContent = this.t('devtpl.fixErrors', 'Fix the errors above');
                fb.className = 'save-feedback err';
                return;
            }
            this.closeModal('devTplModal');
            const rs = res.restarted || [];
            this.showToast('success', this.t('devtpl.saved', 'Template saved'),
                           `${res.template.id} · ${res.template.registers} reg` + (rs.length
                               ? ' · ' + this.t('devtpl.restarted', 'how it is read changed — restarted {list}', { list: rs.join(', ') })
                               : ''));
            await this._refreshWizTemplates(res.template.id);
        } catch (e) {
            fb.textContent = e.message;
            fb.className = 'save-feedback err';
        }
    },

    // ── canonical fields: the gateway's dictionary + this installation's own ──
    async renderFieldsPanel() {
        const t = (k, d, p) => this.t(k, d, p);
        const box = document.getElementById('tmFieldsPanel');
        if (!box) return;
        let fields = {};
        try { fields = (await (await fetch('/api/canonical-fields')).json()).fields || {}; } catch (e) {}
        this._canonFieldsAll = fields;
        const q = (this._tfSearch || '').toLowerCase();
        const cat = this._tfCat || '';                 // '' all · '__mine' yours · else a category
        const rows = Object.entries(fields)
            .filter(([n, f]) => !q || `${n} ${f.measurement} ${f.description} ${f.unit}`.toLowerCase().includes(q))
            .filter(([, f]) => !cat || (cat === '__mine' ? f.user : f.measurement === cat))
            .sort((a, b) => (b[1].user - a[1].user) || a[1].measurement.localeCompare(b[1].measurement) || a[0].localeCompare(b[0], undefined, { numeric: true }));
        const cats = [...new Set(Object.values(fields).map(f => f.measurement))].sort();
        const own = Object.values(fields).filter(f => f.user).length;
        const perCat = {};
        Object.values(fields).forEach(f => { perCat[f.measurement] = (perCat[f.measurement] || 0) + 1; });
        box.innerHTML = `
            <p class="field-hint" style="max-width:820px;">${t('fields.intro', 'A register\'s name becomes its MQTT topic and its InfluxDB field. Canonical names make the same quantity look the same on every device, so dashboards, Node-RED and virtual meters can rely on them; each also promises a unit. The gateway ships a dictionary; add your own for quantities it does not cover — they become canonical everywhere, and travel with a template you export.')}
               <a href="https://github.com/sm26449/multi-bus-gateway/blob/main/docs/canonical-fields.md" target="_blank" rel="noopener">${t('fields.guide', 'How names are built')}</a></p>
            <details class="tpl-proto" id="tfForm" ${this._tfEdit ? 'open' : ''} data-admin>
              <summary><i aria-hidden="true" class="bi bi-plus-lg"></i> ${this._tfEdit ? t('fields.editTitle', 'Edit your field') : t('fields.addTitle', 'Add your own field')}</summary>
              <div class="tpl-proto-body">
                <div class="form-row">
                  <div class="form-group"><label class="form-label" for="tfName">${t('fields.name', 'Name')}</label>
                    <input class="input" id="tfName" placeholder="heat_pump_cop" ${this._tfEdit ? 'disabled' : ''} value="${this._esc(this._tfEdit?.name || '')}">
                    <div class="field-hint">${t('fields.nameHint', '<quantity>_<where>, lowercase: pool_chlorine, heat_pump_cop')}</div></div>
                  <div class="form-group"><label class="form-label" for="tfCat">${t('fields.category', 'Category (InfluxDB measurement)')}</label>
                    <input class="input" id="tfCat" list="tfCatList" placeholder="heat_pump" value="${this._esc(this._tfEdit?.measurement || '')}">
                    <datalist id="tfCatList">${cats.map(c => `<option value="${this._esc(c)}"></option>`).join('')}</datalist></div>
                  <div class="form-group"><label class="form-label" for="tfUnit">${t('fields.unit', 'Unit')}</label>
                    <input class="input" id="tfUnit" placeholder="°C, W, m³/h…" value="${this._esc(this._tfEdit?.unit || '')}"></div>
                </div>
                <div class="form-row">
                  <div class="form-group"><label class="form-label" for="tfTopic">${t('fields.topic', 'MQTT topic leaf')}</label>
                    <input class="input" id="tfTopic" placeholder="${t('fields.topicAuto', 'category/name (automatic)')}" value="${this._esc(this._tfEdit?.mqtt_topic || '')}"></div>
                  <div class="form-group flex-2"><label class="form-label" for="tfDesc">${t('fields.description', 'Description')}</label>
                    <input class="input" id="tfDesc" maxlength="160" value="${this._esc(this._tfEdit?.description || '')}"></div>
                  <div class="form-group"><label class="checkbox-label" style="margin-top:24px;"><input type="checkbox" id="tfCounter" ${this._tfEdit?.counter ? 'checked' : ''}> <span>${t('fields.counter', 'Counter (only grows)')}</span></label></div>
                </div>
                <div class="field-hint">${t('fields.lockHint', 'Once a device reads a field, its category and topic stay as they are — they hold history. Unit and description can still change.')}</div>
                <div style="display:flex;gap:8px;margin-top:8px;align-items:center;">
                  <button data-admin class="btn btn-primary btn-sm" ${this._act('saveUserField', [])}><i aria-hidden="true" class="bi bi-check-lg"></i> ${t('common.save', 'Save')}</button>
                  ${this._tfEdit ? `<button class="btn btn-ghost btn-sm" ${this._act('editUserField', [''])}>${t('common.cancel', 'Cancel')}</button>` : ''}
                  <span class="save-feedback" id="tfFeedback"></span>
                </div>
              </div>
            </details>
            <div style="display:flex;gap:10px;align-items:center;margin:10px 0;flex-wrap:wrap;">
              <input type="text" id="tfSearch" class="input" style="max-width:260px;" placeholder="${t('common.search', 'Search…')}" value="${this._esc(this._tfSearch || '')}">
              <select id="tfCatFilter" class="input" style="max-width:240px;" aria-label="${this._esc(t('fields.categoryShort', 'Category'))}" ${this._act('_tfSetCat', [], { value: true, on: 'change' })}>
                <option value="" ${cat ? '' : 'selected'}>${t('fields.allCats', 'All categories')} (${Object.keys(fields).length})</option>
                ${own ? `<option value="__mine" ${cat === '__mine' ? 'selected' : ''}>${t('fields.mineOnly', 'Only yours')} (${own})</option>` : ''}
                ${cats.map(c => `<option value="${this._esc(c)}" ${c === cat ? 'selected' : ''}>${this._esc(c)} (${perCat[c]})</option>`).join('')}
              </select>
              <span class="field-hint">${t('fields.shown', '{shown} shown', { shown: rows.length })} · ${this.tn('fields.count', Object.keys(fields).length, '{n} fields · {own} yours', { own })}</span>
            </div>
            <div class="table-container tf-list">
              <table class="data-table"><thead><tr>
                <th>${t('fields.name', 'Name')}</th><th>${t('fields.unit', 'Unit')}</th><th>${t('fields.categoryShort', 'Category')}</th>
                <th>MQTT</th><th>${t('fields.description', 'Description')}</th><th></th></tr></thead><tbody>
              ${rows.map(([n, f]) => `<tr>
                <td><code>${this._esc(n)}</code>${f.user ? ` <span class="sink-pill ok">${t('fields.yours', 'yours')}</span>` : ''}${f.counter ? ` <span class="sink-pill" title="${this._esc(t('fields.counterHint', 'a lifetime counter'))}">Σ</span>` : ''}</td>
                <td>${this._esc(f.unit || '—')}</td><td>${this._esc(f.measurement)}</td>
                <td><code>${this._esc(f.mqtt_topic)}</code></td><td>${this._esc(f.description || '')}</td>
                <td style="white-space:nowrap;">${f.user ? `<button data-admin class="btn btn-ghost btn-sm" ${this._act('editUserField', [n])} aria-label="${t('common.edit', 'Edit')}"><i aria-hidden="true" class="bi bi-pencil"></i></button>
                  <button data-admin class="btn btn-ghost btn-sm" ${this._act('deleteUserField', [n])} aria-label="${t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-trash"></i></button>` : ''}</td></tr>`).join('')}
              </tbody></table></div>`;
        const s = document.getElementById('tfSearch');
        s?.addEventListener('input', () => {
            this._tfSearch = s.value;
            clearTimeout(this._tfT);
            this._tfT = setTimeout(async () => { await this.renderFieldsPanel(); const x = document.getElementById('tfSearch'); x?.focus(); x?.setSelectionRange(x.value.length, x.value.length); }, 200);
        });
    },

    _tfSetCat(value) {
        this._tfCat = value;
        this.renderFieldsPanel();
    },

    editUserField(name) {
        this._tfEdit = name ? { name, ...(this._canonFieldsAll || {})[name] } : null;
        this.renderFieldsPanel();
    },

    async saveUserField() {
        const t = (k, d, p) => this.t(k, d, p);
        const v = id => (document.getElementById(id)?.value || '').trim();
        const fb = document.getElementById('tfFeedback');
        const body = { name: this._tfEdit ? this._tfEdit.name : v('tfName'), category: v('tfCat'),
                       unit: v('tfUnit'), description: v('tfDesc'),
                       counter: !!document.getElementById('tfCounter')?.checked };
        if (v('tfTopic')) body.topic = v('tfTopic');
        try {
            const r = await fetch('/api/canonical-fields/user', { method: 'POST',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error((d.detail?.errors || [r.statusText]).join(' · '));
            this._tfEdit = null;
            this.showToast('success', t('fields.saved', 'Field saved'), `${d.field.name} → ${d.field.topic}`);
            await this._loadCanonicalFields?.(true);
            this.renderFieldsPanel();
        } catch (e) {
            if (fb) { fb.textContent = String(e.message || e); fb.className = 'save-feedback err'; }
        }
    },

    async deleteUserField(name) {
        const t = (k, d, p) => this.t(k, d, p);
        if (!await this.confirmAction({
            title: t('fields.deleteTitle', 'Delete field {name}', { name }),
            message: t('fields.deleteHint', 'Refused while a template uses it.'),
            confirmLabel: t('common.delete', 'Delete'), danger: true,
        })) return;
        const r = await fetch(`/api/canonical-fields/user/${encodeURIComponent(name)}`, { method: 'DELETE' });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) {
            this.showToast('error', t('fields.deleteFail', 'Cannot delete'), (d.detail?.errors || [r.statusText]).join(' · '));
            return;
        }
        await this._loadCanonicalFields?.(true);
        this.renderFieldsPanel();
    },
});
