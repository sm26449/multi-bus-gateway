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
        let devTpls = [], vmTpls = [], loadErrors = {};
        try {
            const d = await (await fetch('/api/device-templates')).json();
            devTpls = d.templates || []; loadErrors = d.load_errors || {};
        } catch (e) {}
        try { vmTpls = (await (await fetch('/api/virtual-meters/templates')).json()).templates || []; } catch (e) {}

        const activeTab = this._tmTab || 'devmaps';
        const badgeBuiltin = `<span style="font-size:10.5px;font-weight:700;padding:1px 6px;border-radius:8px;background:rgba(59,130,246,.15);color:#3b82f6;">${t('templates.builtin', 'built-in')}</span>`;
        const badgeUser = `<span style="font-size:10.5px;font-weight:700;padding:1px 6px;border-radius:8px;background:rgba(26,143,76,.15);color:#1a8f4c;">${t('templates.userTag', 'user')}</span>`;

        // ── Device maps ──
        const q = (this._tmSearch || '').toLowerCase();
        const devFiltered = devTpls.filter(x => !q ||
            `${x.id} ${x.name} ${x.vendor} ${x.model}`.toLowerCase().includes(q));
        const devCards = devFiltered.length ? devFiltered.map(x => {
            const inUse = (x.used_by || []).length;
            const meta = [x.vendor, x.model].filter(Boolean).map(s => this._esc(s)).join(' · ');
            const usedRow = inUse
                ? `<div style="margin-top:3px;color:var(--text-secondary);font-size:12px;">${t('templates.inUse', 'in use by')}: ${x.used_by.map(d => `<code>${this._esc(d)}</code>`).join(', ')}</div>`
                : '';
            const viewBtn = `<button class="btn btn-sm" data-tm-view="${this._esc(x.id)}"><i class="bi bi-eye"></i> ${t('templates.view', 'View')}</button>`;
            const actions = x.builtin
                ? `${viewBtn}
                   <button class="btn btn-sm" data-tm-dup="${this._esc(x.id)}"><i class="bi bi-files"></i> ${t('templates.duplicate', 'Duplicate')}</button>
                   <button class="btn btn-ghost btn-sm" data-tm-exp="${this._esc(x.id)}" title="Export JSON"><i class="bi bi-download"></i></button>`
                : `${viewBtn}
                   <button class="btn btn-sm" data-tm-edit="${this._esc(x.id)}"><i class="bi bi-pencil"></i> ${t('common.edit', 'Edit')}</button>
                   <button class="btn btn-sm" data-tm-dup="${this._esc(x.id)}"><i class="bi bi-files"></i> ${t('templates.duplicate', 'Duplicate')}</button>
                   <button class="btn btn-ghost btn-sm" data-tm-exp="${this._esc(x.id)}" title="Export JSON"><i class="bi bi-download"></i></button>
                   <button class="btn btn-ghost btn-sm" data-tm-del="${this._esc(x.id)}" ${inUse ? 'disabled' : ''} title="${inUse ? t('templates.delInUse', 'in use — reassign first') : t('common.delete', 'Delete')}"><i class="bi bi-trash"></i></button>`;
            return `<div class="settings-card" style="margin-bottom:10px;">
                <div class="settings-card-body" style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
                  <div style="min-width:0;">
                    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;"><b>${this._esc(x.name)}</b> ${x.builtin ? badgeBuiltin : badgeUser}</div>
                    <div style="color:var(--text-secondary);font-size:12px;margin-top:3px;">id <code>${this._esc(x.id)}</code>${meta ? ' · ' + meta : ''} · ${x.registers} ${t('templates.regs', 'registers')}${x.version ? ' · v' + this._esc(x.version) : ''}</div>
                    ${usedRow}
                  </div>
                  <div style="display:flex;gap:8px;flex-wrap:wrap;">${actions}</div>
                </div></div>`;
        }).join('') : `<p style="color:var(--text-secondary);">${q ? t('templates.noMatch', 'No templates match your search.') : t('templates.none', 'No device maps yet.')}</p>`;

        const errKeys = Object.keys(loadErrors);
        const errBanner = errKeys.length
            ? `<div class="settings-card" style="border-left:3px solid #c0392b;padding:8px 12px;margin-bottom:10px;color:var(--danger-text,#c0392b);font-size:12.5px;"><i class="bi bi-exclamation-triangle"></i> ${errKeys.length} ${t('templates.loadErrors', 'template file(s) failed to load')}: ${errKeys.map(k => this._esc(k)).join(', ')}</div>`
            : '';
        const devToolbar = `<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap;">
            <input type="search" id="tmSearch" placeholder="${t('templates.search', 'Search maps…')}" value="${this._esc(this._tmSearch || '')}" style="max-width:240px;padding:6px 10px;border:1px solid var(--border-color,#ccc);border-radius:6px;background:var(--input-bg,transparent);color:inherit;">
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
              <button class="btn btn-ghost btn-sm" id="tmImportCsvBtn"><i class="bi bi-filetype-csv"></i> ${t('registers.importCsv', 'Import CSV')}</button>
              <button class="btn btn-ghost btn-sm" id="tmUploadBtn"><i class="bi bi-upload"></i> ${t('templates.upload', 'Upload JSON')}</button>
              <button class="btn btn-sm" id="tmNewBtn"><i class="bi bi-plus-lg"></i> ${t('templates.new', 'New map')}</button>
            </div></div>`;

        // ── Meter emulations (vmeter output templates) ──
        const vmCards = vmTpls.length ? vmTpls.map(tp => `
            <div class="settings-card" style="margin-bottom:10px;">
              <div class="settings-card-body" style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
                <div><b>${this._esc(tp.name)}</b><br>
                  <span style="color:var(--text-secondary);font-size:12px;">id <code>${this._esc(tp.id)}</code> · ${this._esc(tp.kind || '')} · ${tp.registers} ${t('templates.measurements', 'measurements')}</span></div>
                <div style="display:flex;gap:8px;">
                  <button class="btn btn-sm" data-tmvm-edit="${this._esc(tp.id)}"><i class="bi bi-pencil"></i> ${t('common.edit', 'Edit')}</button>
                  <button class="btn btn-ghost btn-sm" data-tmvm-exp="${this._esc(tp.id)}" title="Export YAML"><i class="bi bi-download"></i></button>
                  <button class="btn btn-ghost btn-sm" data-tmvm-del="${this._esc(tp.id)}" title="Delete"><i class="bi bi-trash"></i></button>
                </div>
              </div></div>`).join('') : `<p style="color:var(--text-secondary);">${t('templates.noEmu', 'No meter emulations yet.')}</p>`;
        const vmToolbar = `<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap;">
            <p style="color:var(--text-secondary);font-size:12.5px;margin:0;max-width:560px;">${t('templates.emuHint', 'Map live values into the layout a consumer expects. Run and monitor instances on the Virtual Meters page.')}</p>
            <div style="display:flex;gap:8px;">
              <input type="file" id="tmVmImportFile" accept=".yaml,.yml" style="display:none;">
              <button class="btn btn-ghost btn-sm" id="tmVmImportBtn"><i class="bi bi-upload"></i> ${t('common.import', 'Import')}</button>
              <button class="btn btn-sm" id="tmVmNewBtn"><i class="bi bi-plus-lg"></i> ${t('templates.newEmu', 'New template')}</button>
            </div></div>`;

        el.innerHTML = `
            <div class="config-main-tabs" id="tmTabs">
              <button class="config-main-tab ${activeTab === 'devmaps' ? 'active' : ''}" data-tmtab="devmaps"><i class="bi bi-table"></i> ${t('templates.tab.devmaps', 'Device maps')} <span style="opacity:.6;">(${devTpls.length})</span></button>
              <button class="config-main-tab ${activeTab === 'emulations' ? 'active' : ''}" data-tmtab="emulations"><i class="bi bi-hdd-network"></i> ${t('templates.tab.emu', 'Meter emulations')} <span style="opacity:.6;">(${vmTpls.length})</span></button>
            </div>
            <div data-tmpanel="devmaps" ${activeTab === 'devmaps' ? '' : 'hidden'}>${errBanner}${devToolbar}${devCards}</div>
            <div data-tmpanel="emulations" ${activeTab === 'emulations' ? '' : 'hidden'}>${vmToolbar}${vmCards}</div>`;

        // tab switch
        el.querySelectorAll('[data-tmtab]').forEach(b => b.addEventListener('click', () => {
            this._tmTab = b.dataset.tmtab;
            el.querySelectorAll('[data-tmtab]').forEach(x => x.classList.toggle('active', x === b));
            el.querySelectorAll('[data-tmpanel]').forEach(p => { p.hidden = p.dataset.tmpanel !== this._tmTab; });
        }));
        // search (re-render + keep focus)
        const searchEl = document.getElementById('tmSearch');
        if (searchEl) searchEl.addEventListener('input', () => {
            this._tmSearch = searchEl.value; this._tmRefocusSearch = true; this.renderTemplateManager();
        });
        if (this._tmRefocusSearch) {
            this._tmRefocusSearch = false;
            const s = document.getElementById('tmSearch');
            if (s) { s.focus(); const v = s.value; s.value = ''; s.value = v; }
        }
        // device-map actions (reuse the wizard handlers; their refresh re-renders this page)
        el.querySelectorAll('[data-tm-view]').forEach(b => b.addEventListener('click', () => this.viewTemplate(b.dataset.tmView)));
        document.getElementById('tmNewBtn')?.addEventListener('click', () => this.openTplEditor(null));
        document.getElementById('tmUploadBtn')?.addEventListener('click', () => this.tplUpload());
        document.getElementById('tmImportCsvBtn')?.addEventListener('click', () => this.openCsvImport(true));
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
    async viewTemplate(id) {
        const t = this.t.bind(this);
        let full;
        try {
            full = (await (await fetch(`/api/device-templates/${encodeURIComponent(id)}`)).json()).device_template;
        } catch (e) { this.showToast('error', this.t('toast.template', 'Template'), e.message); return; }
        if (!full) { this.showToast('error', this.t('toast.template', 'Template'), id); return; }
        const regs = full.registers || [];
        const proto = full.protocol || {};
        const bo = proto.byte_order || 'big';
        const boLabel = { big: 'big-endian (ABCD)', little: 'little-endian / word-swap (CDAB)',
                          badc: 'byte-swap (BADC)', dcba: 'full little-endian (DCBA)' }[bo] || bo;
        const fcs = [...new Set(regs.map(r => r.register_type || 'holding'))]
            .map(k => k === 'input' ? 'FC04 input' : 'FC03 holding').join(', ');
        document.getElementById('tmViewTitle').textContent =
            `${full.name || id}${full.builtin ? ' · ' + t('templates.builtin', 'built-in') : ''}`;
        const meta = `<div style="display:flex;gap:18px 26px;flex-wrap:wrap;font-size:12.5px;margin-bottom:10px;">
            <div><div style="color:var(--text-secondary);">id</div><code>${this._esc(full.id)}</code></div>
            <div><div style="color:var(--text-secondary);">${t('templates.vendorModel', 'Vendor / model')}</div>${this._esc([full.vendor, full.model].filter(Boolean).join(' · ') || '—')}</div>
            <div><div style="color:var(--text-secondary);">${t('templates.transport', 'Transport')}</div>${this._esc(fcs || 'FC03 holding')} · ${this._esc(boLabel)}</div>
            <div><div style="color:var(--text-secondary);">${t('templates.regs', 'registers')}</div><b>${regs.length}</b></div>
            ${full.version ? `<div><div style="color:var(--text-secondary);">version</div>v${this._esc(full.version)}</div>` : ''}
          </div>`;
        const src = full.source_document
            ? `<div style="font-size:12px;color:var(--text-secondary);margin-bottom:8px;"><i class="bi bi-journal-check"></i> ${t('templates.source', 'Source')}: ${this._esc(full.source_document)}</div>` : '';
        const desc = full.description
            ? `<details style="margin-bottom:10px;"><summary style="cursor:pointer;font-size:12.5px;color:var(--text-secondary);">${t('templates.details', 'Details / provenance')}</summary><p style="font-size:12.5px;color:var(--text-secondary);margin:6px 0 0;">${this._esc(full.description)}</p></details>` : '';
        const rows = regs.slice().sort((a, b) => (a.address - b.address)).map(r => {
            const a = r.address;
            const hx = (typeof a === 'number') ? ' <span style="color:var(--text-secondary);">0x' + a.toString(16).toUpperCase().padStart(4, '0') + '</span>' : '';
            return `<tr>
                <td style="padding:2px 10px 2px 0;font-variant-numeric:tabular-nums;">${this._esc(String(a))}${hx}</td>
                <td style="padding:2px 10px 2px 0;"><code>${this._esc(r.name || '')}</code></td>
                <td style="padding:2px 10px 2px 0;">${this._esc(r.label || '')}</td>
                <td style="padding:2px 10px 2px 0;">${this._esc(r.data_type || '')}${(r.register_type === 'input') ? ' <span style="color:var(--text-secondary);">(FC04)</span>' : ''}</td>
                <td style="padding:2px 10px 2px 0;text-align:right;font-variant-numeric:tabular-nums;">${this._esc(String(r.scale ?? 1))}</td>
                <td style="padding:2px 10px 2px 0;">${this._esc(r.unit || '')}</td>
                <td style="padding:2px 0;color:var(--text-secondary);">${this._esc(r.poll_group || '')}</td>
              </tr>`;
        }).join('');
        const table = `<div style="overflow-x:auto;"><table style="width:100%;font-size:12.5px;border-collapse:collapse;">
            <thead><tr style="text-align:left;border-bottom:1px solid var(--border-color,#ccc);color:var(--text-secondary);">
              <th style="padding:4px 10px 4px 0;">${t('templates.address', 'Address')}</th><th style="padding:4px 10px 4px 0;">${t('templates.name', 'Name')}</th>
              <th style="padding:4px 10px 4px 0;">${t('templates.label', 'Description')}</th><th style="padding:4px 10px 4px 0;">${t('templates.type', 'Type')}</th>
              <th style="padding:4px 10px 4px 0;text-align:right;">${t('templates.scale', 'Scale')}</th><th style="padding:4px 10px 4px 0;">${t('templates.unit', 'Unit')}</th>
              <th style="padding:4px 0;">${t('templates.poll', 'Poll')}</th></tr></thead>
            <tbody>${rows || `<tr><td colspan="7" style="color:var(--text-secondary);padding:8px 0;">${t('templates.noRegs', 'No registers.')}</td></tr>`}</tbody></table></div>`;
        document.getElementById('tmViewBody').innerHTML = meta + src + desc + table;
        document.getElementById('tmViewExportBtn').onclick = () => this.tplExport(id);
        document.getElementById('tmViewDupBtn').onclick = () => { this.closeModal('tmViewModal'); this.openTplEditor(id, true); };
        this.openModal('tmViewModal');
    },

    // ── CSV register-map import ───────────────────────────────────────────
    // managerMode: from the Template Manager the import SAVES a library template
    // (no device to assign to). From a device's Registers editor it imports and
    // assigns to that device (the original behaviour).
    openCsvImport(managerMode = false) {
        this._csvManagerMode = !!managerMode;
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
        ['csvId', 'csvName', 'csvVendor', 'csvModel', 'csvText'].forEach(k => { const e = document.getElementById(k); if (e) e.value = ''; });
        document.getElementById('csvPreviewResult').innerHTML = '';
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
        const csv = document.getElementById('csvText').value;
        const box = document.getElementById('csvPreviewResult');
        if (!csv.trim()) { box.innerHTML = `<span class="field-hint">${this.t('csv.noData', 'Paste a CSV or open a file first.')}</span>`; return; }
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="btn-spinner"></span>'; }
        try {
            const r = await fetch('/api/device-templates/import-csv', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    csv, id: document.getElementById('csvId').value.trim(),
                    name: document.getElementById('csvName').value.trim(),
                    vendor: document.getElementById('csvVendor').value.trim(),
                    model: document.getElementById('csvModel').value.trim(),
                    default_data_type: document.getElementById('csvDefType').value,
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
            box.innerHTML = `<div class="settings-card" style="padding:10px 12px;">
                <div><b style="color:${verr.length ? 'var(--danger-text,#c0392b)' : 'var(--success-text,#1a8f4c)'};">${res.register_count}</b> ${this.t('csv.parsed', 'measurements parsed')} · ${this.t('csv.cols', 'columns')}: ${(res.columns || []).map(c => this._esc(c)).join(', ')}</div>
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
        const csv = 'address,name,label,unit,type,scale,category,json_path\n'
            + '0x0000,V_L1,Voltage L1-N,V,float,1,voltage,\n'
            + '0x0002,V_L2,Voltage L2-N,V,float,1,voltage,\n'
            + '40,P_total,Total active power,W,int32,10,power,\n'
            + '19000,Freq,Frequency,Hz,uint16,100,frequency,\n';
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
            if (r.status === 409 && !res.builtin) {
                if (confirm(this.t('devtpl.overwriteConfirm',
                    'A template with this id already exists.\n\nYes: overwrite it with the uploaded file.\nNo: cancel the upload.')))
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

    async tplDelete(id) {
        if (!confirm(this.t('devtpl.deleteConfirm',
            'Delete this template?\n\nYes: removes the template file (devices are untouched).\nNo: keeps it.'))) return;
        try {
            const r = await fetch(`/api/device-templates/${encodeURIComponent(id)}`, { method: 'DELETE' });
            if (!r.ok) throw new Error(((await r.json()).detail?.errors || ['delete failed']).join('; '));
            this.showToast('success', this.t('devtpl.deleted', 'Template deleted'), id);
            await this._refreshWizTemplates();
        } catch (e) {
            this.showToast('error', this.t('devtpl.deleteFail', 'Delete failed'), e.message);
        }
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
                data.id = `${data.id}-copy`;
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
        this._tplEdit = { data, isNew: !id || duplicate, search: '' };
        document.getElementById('devTplTitle').textContent =
            (!id ? this.t('devtpl.newTitle', 'New Device Template')
                 : duplicate ? this.t('devtpl.dupTitle', 'Duplicate Template')
                             : this.t('devtpl.editTitle', 'Edit Device Template')) +
            (data.id ? ` — ${data.id}` : '');
        this._tplEditorRender();
        this.openModal('devTplModal');
    },

    _tplEditorRender() {
        const e = this._tplEdit, d = e.data;
        const dataTypes = ['float', 'float32', 'double', 'int16', 'uint16', 'short',
                           'int32', 'uint32', 'int64', 'uint64'];
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
                <td><input class="input tpl-cell" data-f="address" type="number" min="0" max="65535" value="${r.address}" aria-label="Address"></td>
                <td><input class="input tpl-cell" data-f="name" value="${this._esc(r.name)}" aria-label="Name"></td>
                <td><input class="input tpl-cell" data-f="label" value="${this._esc(r.label || '')}" aria-label="Label"></td>
                <td><input class="input tpl-cell" data-f="unit" value="${this._esc(r.unit || '')}" style="width:56px" aria-label="Unit"></td>
                <td><select class="input tpl-cell" data-f="data_type" aria-label="Data type">
                    ${dataTypes.map(t => `<option ${t === r.data_type ? 'selected' : ''}>${t}</option>`).join('')}</select></td>
                <td><input class="input tpl-cell" data-f="scale" type="number" step="any" value="${r.scale ?? 1}" style="width:68px" aria-label="Scale"></td>
                <td><select class="input tpl-cell" data-f="register_type" aria-label="Register type" title="Function code">
                    <option value="holding" ${(r.register_type || 'holding') === 'holding' ? 'selected' : ''}>FC3</option>
                    <option value="input" ${r.register_type === 'input' ? 'selected' : ''}>FC4</option></select></td>
                <td><input class="input tpl-cell" data-f="category" list="tplCatList" value="${this._esc(r.category || '')}" style="width:118px" aria-label="Category"></td>
                <td><input class="input tpl-cell" data-f="poll_group" list="tplGroupList" value="${this._esc(r.poll_group || '')}" style="width:96px" aria-label="Poll group"></td>
                <td style="text-align:center;"><input type="checkbox" class="tpl-cell" data-f="writable" ${r.writable ? 'checked' : ''} title="Writable via the write API" aria-label="Writable"></td>
                <td><input class="input tpl-cell" data-f="write_min" type="number" step="any" value="${r.write_min ?? ''}" style="width:60px" placeholder="min" aria-label="Write min"></td>
                <td><input class="input tpl-cell" data-f="write_max" type="number" step="any" value="${r.write_max ?? ''}" style="width:60px" placeholder="max" aria-label="Write max"></td>
                <td><input class="input tpl-cell" data-f="write_safe" type="number" step="any" value="${r.write_safe ?? ''}" style="width:60px" placeholder="safe" aria-label="Write safe (auto-revert)"></td>
                <td><button class="btn btn-ghost btn-sm" ${this._act('tplDelRow', [i])} title="${this.t('common.delete', 'Delete')}" aria-label="Delete row"><i class="bi bi-trash"></i></button></td>
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
        <div style="display:flex;gap:8px;align-items:center;margin:8px 0;flex-wrap:wrap;">
            <input type="text" id="tplSearch" class="input" placeholder="${this.t('common.search', 'Search')}…"
                   value="${this._esc(e.search || '')}" style="max-width:220px;">
            <button class="btn btn-secondary btn-sm" onclick="app.tplAddRow()">
                <i class="bi bi-plus-lg"></i> ${this.t('devtpl.addRegister', 'Add measurement')}</button>
            <span class="field-hint">${matching.length > MAX
                ? this.t('devtpl.showing', 'showing') + ` ${MAX} / ${matching.length}`
                : `${matching.length} ${this.t('devices.regsSelected', 'measurements')}`}
                · ${d.registers.length} ${this.t('devtpl.total', 'total')}</span>
        </div>
        <datalist id="tplCatList">${cats.map(c => `<option value="${this._esc(c)}"></option>`).join('')}</datalist>
        <datalist id="tplGroupList">${groups.map(g => `<option value="${this._esc(g)}"></option>`).join('')}</datalist>
        <div class="table-container" style="max-height:320px;overflow:auto;">
            <table class="data-table tpl-table">
                <thead><tr><th>Addr</th><th>${this.t('devtpl.colName', 'Name')}</th><th>${this.t('devtpl.colLabel', 'Label')}</th><th>${this.t('devtpl.colUnit', 'Unit')}</th><th>${this.t('devtpl.colType', 'Type')}</th><th>${this.t('devtpl.colScale', 'Scale')}</th><th title="Function code">${this.t('devtpl.colFC', 'FC')}</th><th>${this.t('devtpl.colCategory', 'Category')}</th><th>${this.t('devtpl.colGroup', 'Poll group')}</th><th title="Writable via the write API">${this.t('devtpl.colWr', 'Wr')}</th><th>${this.t('devtpl.colMin', 'Min')}</th><th>${this.t('devtpl.colMax', 'Max')}</th><th title="Auto-revert value on lease expiry">${this.t('devtpl.colSafe', 'Safe')}</th><th></th></tr></thead>
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
                else if (f === 'write_min' || f === 'write_max' || f === 'write_safe')
                    v = inp.value === '' ? null : parseFloat(inp.value);
                else v = inp.value;
                this._tplEdit.data.registers[idx][f] = v;
            });
        });
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
        const g = id => document.getElementById(id);
        if (this._tplEdit.isNew) d.id = g('tplId')?.value.trim() ?? d.id;
        d.name = g('tplName')?.value.trim() ?? d.name;
        d.vendor = g('tplVendor')?.value.trim() ?? d.vendor;
        d.model = g('tplModel')?.value.trim() ?? d.model;
    },

    tplAddRow() {
        this._tplCollectMeta();
        const d = this._tplEdit.data;
        const cat = Object.keys(d.categories || {})[0] || 'basic';
        d.registers.push({ address: 0, name: '', label: '', unit: '',
                           data_type: 'float', scale: 1, register_type: 'holding',
                           access: 'RD', category: cat, poll_group: '', description: '' });
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

    async tplSave() {
        this._tplCollectMeta();
        const d = this._tplEdit.data;
        const fb = document.getElementById('devTplFeedback');
        const errBox = document.getElementById('tplErrors');
        try {
            const r = await fetch('/api/device-templates', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ device_template: d }),
            });
            const res = await r.json();
            if (!r.ok) {
                const errs = res.detail?.errors || ['save failed'];
                errBox.textContent = errs.slice(0, 8).join('\n');
                errBox.style.display = '';
                fb.textContent = this.t('devtpl.fixErrors', 'Fix the errors above');
                fb.className = 'save-feedback err';
                return;
            }
            this.closeModal('devTplModal');
            this.showToast('success', this.t('devtpl.saved', 'Template saved'),
                           `${res.template.id} · ${res.template.registers} reg`);
            await this._refreshWizTemplates(res.template.id);
        } catch (e) {
            fb.textContent = e.message;
            fb.className = 'save-feedback err';
        }
    }
});
