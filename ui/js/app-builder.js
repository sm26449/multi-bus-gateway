// Device Builder domain — augments JanitzaMonitor.prototype.
// Drives an external ESPHome dashboard through /api/builder/* (backend proxy):
// node list, YAML editor with server-side validation, compile/OTA with a live
// log console (WS relay), artifact downloads. The feature is opt-in; without
// esphome.enabled the page shows the settings form and nothing else breaks.
Object.assign(JanitzaMonitor.prototype, {

    async renderBuilder() {
        const el = document.getElementById('builderContent');
        if (!el) return;
        const refreshBtn = document.getElementById('builderRefreshBtn');
        if (refreshBtn && !refreshBtn._wired) {
            refreshBtn._wired = true;
            refreshBtn.addEventListener('click', () => this.renderBuilder());
        }
        let st;
        try {
            st = await (await fetch('/api/builder/status')).json();
        } catch (e) {
            el.innerHTML = `<p style="color:var(--danger,#ef4444);">${this.t('builder.loadFail', 'Could not load builder status.')}</p>`;
            return;
        }
        this._builderStatus = st;
        if (!st.enabled) {
            el.innerHTML = `<div style="max-width:680px;">${this._builderSettingsFormHtml()}</div>`;
            this._wireBuilderSettings();
            return;
        }
        const banner = st.reachable
            ? `<span class="sink-pill ok">ESPHome ${this._esc(st.version)}</span>
               <span style="color:var(--text-secondary);font-size:12px;">${this._esc(st.url)}</span>`
            : `<span class="sink-pill bad">${this.t('builder.unreachable', 'unreachable')}</span>
               <span style="color:var(--text-secondary);font-size:12px;">${this._esc(st.error || st.url)}</span>`;
        el.innerHTML = `
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:14px;">
                ${banner}
                <span style="flex:1;"></span>
                <button class="btn btn-primary btn-sm" id="builderGenBtn"><i class="bi bi-magic"></i> ${this.t('builder.generate', 'Generate from template')}</button>
                <button class="btn btn-sm" id="builderNewBtn"><i class="bi bi-plus-lg"></i> ${this.t('builder.newNode', 'New node')}</button>
                <button class="btn btn-sm" id="builderImportBtn"><i class="bi bi-upload"></i> ${this.t('builder.importYaml', 'Import YAML')}</button>
                <button class="btn btn-sm" id="builderUpdateAllBtn" title="${this.t('builder.updateAllTip', 'Rebuild and OTA every node whose firmware is out of date')}"><i class="bi bi-arrow-repeat"></i> ${this.t('builder.updateAll', 'Update all')}</button>
                <button class="btn btn-ghost btn-sm" id="builderSettingsBtn" title="${this.t('builder.settingsTitle', 'ESPHome connection')}"><i class="bi bi-gear"></i></button>
            </div>
            <div id="builderSettingsPanel" style="display:none;max-width:680px;margin-bottom:14px;">
                ${this._builderSettingsFormHtml()}
            </div>
            <div id="builderNodes">${st.reachable ? '' : `<span class="field-hint">${this.t('builder.fixConn', 'Fix the connection to list nodes.')}</span>`}</div>`;
        this._wireBuilderSettings();
        document.getElementById('builderSettingsBtn').addEventListener('click', () => {
            const p = document.getElementById('builderSettingsPanel');
            p.style.display = p.style.display === 'none' ? '' : 'none';
        });
        document.getElementById('builderNewBtn').addEventListener('click', () => this.openBuilderEditor(''));
        document.getElementById('builderImportBtn').addEventListener('click', () => this._builderImportYaml());
        document.getElementById('builderGenBtn').addEventListener('click', () => this.openBuilderGenerator());
        document.getElementById('builderUpdateAllBtn').addEventListener('click', () => {
            if (confirm(this.t('builder.updateAllQ', 'Rebuild and OTA-update every node now?')))
                this.openBuilderConsole('update-all', '');
        });
        if (st.reachable) this._renderBuilderNodes();
    },

    async _renderBuilderNodes() {
        const el = document.getElementById('builderNodes');
        if (!el) return;
        let data;
        try {
            const rsp = await fetch('/api/builder/nodes');
            if (!rsp.ok) throw new Error((await rsp.json()).detail || rsp.status);
            data = await rsp.json();
        } catch (e) {
            el.innerHTML = `<p style="color:var(--danger,#ef4444);">${this._esc(String(e.message || e))}</p>`;
            return;
        }
        const nodes = data.configured || [];
        const importable = data.importable || [];
        if (!nodes.length && !importable.length) {
            el.innerHTML = `<span class="field-hint">${this.t('builder.empty',
                'No nodes yet — create one with "New node" or import an existing YAML.')}</span>`;
            return;
        }
        const rows = nodes.map(n => {
            const name = this._esc(n.configuration);
            const seen = !!n.address;
            const dot = seen ? 'var(--success,#22c55e)' : 'var(--text-secondary,#8a94a0)';
            const sub = [
                n.target_platform ? this._esc(String(n.target_platform).toUpperCase()) : null,
                n.deployed_version ? `v${this._esc(n.deployed_version)}` : this.t('builder.notBuilt', 'not built yet'),
                n.address ? this._esc(n.address) : null,
            ].filter(Boolean).join(' · ');
            const actions = [
                `<button class="btn btn-ghost btn-sm" data-act="edit" data-name="${name}" title="${this.t('common.edit', 'Edit')}"><i class="bi bi-pencil"></i></button>`,
                `<button class="btn btn-ghost btn-sm" data-act="validate" data-name="${name}" title="${this.t('builder.validate', 'Validate')}"><i class="bi bi-check2-circle"></i></button>`,
                `<button class="btn btn-ghost btn-sm" data-act="compile" data-name="${name}" title="${this.t('builder.build', 'Build')}"><i class="bi bi-hammer"></i></button>`,
                `<button class="btn btn-ghost btn-sm" data-act="upload" data-name="${name}" title="${this.t('builder.flashOta', 'Flash OTA')}"><i class="bi bi-broadcast-pin"></i></button>`,
                `<button class="btn btn-ghost btn-sm" data-act="flash-usb" data-name="${name}" title="${this.t('builder.flashUsbTip', 'First-time flash over USB, from this browser (WebSerial)')}"><i class="bi bi-usb-plug"></i></button>`,
                `<button class="btn btn-ghost btn-sm" data-act="logs" data-name="${name}" title="${this.t('builder.logs', 'Logs')}"><i class="bi bi-terminal"></i></button>`,
                `<button class="btn btn-ghost btn-sm" data-act="downloads" data-name="${name}" title="${this.t('builder.binaries', 'Binaries')}"><i class="bi bi-download"></i></button>`,
                `<button class="btn btn-ghost btn-sm" data-act="delete" data-name="${name}" title="${this.t('builder.deleteTip', 'Archive on the ESPHome dashboard (recoverable there)')}"><i class="bi bi-trash"></i></button>`,
            ].join('');
            return `
            <div class="device-row" style="cursor:default;">
                <span class="status-dot" style="--dot:${dot}"
                      title="${seen ? this.t('builder.seenOnNet', 'seen on the network') : this.t('builder.notSeen', 'not seen on the network yet')}"></span>
                <div class="device-row-main">
                    <div class="device-row-title">${this._esc(n.friendly_name || n.name || n.configuration)}
                        <span class="dev-chip">${name}</span></div>
                    <div class="device-row-sub">${sub}</div>
                </div>
                <div class="device-row-actions">${actions}</div>
            </div>`;
        }).join('');
        const imp = importable.length ? `
            <div class="card-header" style="margin:18px 0 8px;border:none;background:none;padding:0;">
                ${this.t('builder.discovered', 'Discovered on the network (adoptable)')}</div>
            ${importable.map((n, i) => `<div class="device-row" style="cursor:default;">
                <span class="status-dot" style="--dot:var(--warning,#f59e0b)"></span>
                <div class="device-row-main">
                    <div class="device-row-title">${this._esc(n.name || '')}</div>
                    <div class="device-row-sub">${this._esc(n.friendly_name || '')} ${this._esc(n.project_name || '')} ${this._esc(n.network || '')}</div>
                </div>
                <div class="device-row-actions">
                    <button class="btn btn-ghost btn-sm" data-imp="${i}" title="${this.t('builder.importTip', 'Adopt onto the ESPHome dashboard — it becomes a managed node (build/OTA from here)')}">
                        <i class="bi bi-box-arrow-in-down"></i> ${this.t('builder.import', 'Import')}</button>
                </div>
            </div>`).join('')}` : '';
        el.innerHTML = rows + imp;
        el.querySelectorAll('button[data-imp]').forEach(b => b.addEventListener('click', () =>
            this._builderImportNode(importable[parseInt(b.dataset.imp, 10)], b)));
        el.querySelectorAll('button[data-act]').forEach(b => b.addEventListener('click', () => {
            const name = b.dataset.name, act = b.dataset.act;
            if (act === 'edit') this.openBuilderEditor(name);
            else if (act === 'validate') this.openBuilderConsole('validate', name);
            else if (act === 'compile') this.openBuilderConsole('compile', name);
            else if (act === 'upload') this.openBuilderConsole('upload', name);
            else if (act === 'logs') this.openBuilderConsole('logs', name);
            else if (act === 'downloads') this._builderShowDownloads(name);
            else if (act === 'flash-usb') this.openBuilderFlasher(name);
            else if (act === 'delete') this._builderDeleteNode(name);
        }));
    },

    // ---- settings ------------------------------------------------------------

    _builderSettingsFormHtml() {
        return `
            <div class="card">
                <div class="card-header"><i class="bi bi-gear"></i> ${this.t('builder.settingsTitle', 'ESPHome connection')}</div>
                <div class="card-body">
                    <div class="form-group"><label class="checkbox-label">
                        <input type="checkbox" id="bsEnabled"> ${this.t('builder.enable', 'Enable the Device Builder')}</label></div>
                    <div class="form-group"><label>URL</label>
                        <input type="text" id="bsUrl" placeholder="http://esphome:6052" style="width:100%;">
                        <div class="field-hint">${this.t('builder.urlHint', 'As reachable from the gateway container — the bundled compose service is http://esphome:6052.')}</div></div>
                    <div style="display:flex;gap:10px;flex-wrap:wrap;">
                        <div class="form-group" style="flex:1;min-width:160px;"><label>${this.t('builder.user', 'Username (optional)')}</label>
                            <input type="text" id="bsUser" autocomplete="off" style="width:100%;"></div>
                        <div class="form-group" style="flex:1;min-width:160px;"><label>${this.t('builder.pass', 'Password (optional)')}</label>
                            <input type="password" id="bsPass" autocomplete="new-password" style="width:100%;"></div>
                    </div>
                    <button class="btn btn-primary btn-sm" id="bsSaveBtn"><i class="bi bi-save"></i> ${this.t('common.save', 'Save')}</button>
                    <span id="bsMsg" style="margin-left:10px;font-size:13px;"></span>
                </div>
            </div>`;
    },

    async _wireBuilderSettings() {
        const btn = document.getElementById('bsSaveBtn');
        if (!btn) return;
        try {
            const s = await (await fetch('/api/builder/settings')).json();
            document.getElementById('bsEnabled').checked = !!s.enabled;
            document.getElementById('bsUrl').value = s.url || 'http://esphome:6052';
            document.getElementById('bsUser').value = s.username || '';
            if (s.password_set) document.getElementById('bsPass').placeholder = '(unchanged)';
        } catch (e) { /* form stays with defaults */ }
        btn.addEventListener('click', async () => {
            const body = {
                enabled: document.getElementById('bsEnabled').checked,
                url: document.getElementById('bsUrl').value.trim(),
                username: document.getElementById('bsUser').value.trim(),
                password: document.getElementById('bsPass').value,
            };
            const rsp = await fetch('/api/builder/settings', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body) });
            if (rsp.ok) {
                this.showToast('success', this.t('common.saved', 'Saved'), '');
                this.renderBuilder();
            } else {
                const d = await rsp.json().catch(() => ({}));
                const msg = (d.detail && d.detail.errors) ? d.detail.errors.join('; ') : (d.detail || rsp.status);
                document.getElementById('bsMsg').innerHTML = `<span style="color:var(--danger,#ef4444);">${this._esc(String(msg))}</span>`;
            }
        });
    },

    // ---- YAML editor -----------------------------------------------------------

    async openBuilderEditor(name) {
        const isNew = !name;
        let content = '';
        if (!isNew) {
            const rsp = await fetch(`/api/builder/nodes/${encodeURIComponent(name)}/config`);
            if (!rsp.ok) {
                this.showToast('error', 'Builder', this.t('builder.loadYamlFail', 'Could not load the node YAML.'));
                return;
            }
            content = await rsp.text();
        } else {
            content = [
                'esphome:', '  name: my-node', '',
                'esp32:', '  board: esp32dev', '',
                'wifi:', '  ssid: !secret wifi_ssid', '  password: !secret wifi_password', '',
                'ota:', '  - platform: esphome', '',
                'logger:', '', 'api:', ''].join('\n');
        }
        document.getElementById('builderEdTitle').textContent =
            isNew ? this.t('builder.newNode', 'New node') : name;
        document.getElementById('builderEdName').value = name;
        document.getElementById('builderEdName').disabled = !isNew;
        document.getElementById('builderEdText').value = content;
        const save = document.getElementById('builderEdSaveBtn');
        const validate = document.getElementById('builderEdValidateBtn');
        save.onclick = () => this._builderEditorSave(isNew);
        validate.onclick = () => {
            const n = document.getElementById('builderEdName').value.trim();
            if (n) this._builderEditorSave(isNew, true);
        };
        this.openModal('builderEditorModal');
    },

    async _builderEditorSave(isNew, thenValidate = false) {
        const name = document.getElementById('builderEdName').value.trim();
        const content = document.getElementById('builderEdText').value;
        if (!/^[A-Za-z0-9][A-Za-z0-9_.-]*\.(yaml|yml)$/.test(name)) {
            this.showToast('error', 'Builder', this.t('builder.badName', 'Node filename must be like my-node.yaml'));
            return;
        }
        const rsp = await fetch(`/api/builder/nodes/${encodeURIComponent(name)}/config`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content, overwrite: !isNew }) });
        if (rsp.status === 409) {
            this.showToast('error', 'Builder', this.t('builder.exists', 'A node with this filename already exists.'));
            return;
        }
        if (!rsp.ok) {
            const d = await rsp.json().catch(() => ({}));
            this.showToast('error', 'Builder', this._esc(String(d.detail || rsp.status)));
            return;
        }
        this.closeModal('builderEditorModal');
        this.showToast('success', 'Builder', this.t('builder.savedYaml', 'Node YAML saved.'));
        this._renderBuilderNodes();
        if (thenValidate) this.openBuilderConsole('validate', name);
    },

    _builderImportYaml() {
        const input = document.createElement('input');
        input.type = 'file';
        input.accept = '.yaml,.yml';
        input.onchange = async () => {
            const f = input.files[0];
            if (!f) return;
            const text = await f.text();
            const name = f.name.replace(/[^A-Za-z0-9_.-]/g, '_');
            document.getElementById('builderEdTitle').textContent = this.t('builder.importYaml', 'Import YAML');
            document.getElementById('builderEdName').value = name;
            document.getElementById('builderEdName').disabled = false;
            document.getElementById('builderEdText').value = text;
            document.getElementById('builderEdSaveBtn').onclick = () => this._builderEditorSave(true);
            document.getElementById('builderEdValidateBtn').onclick = () => this._builderEditorSave(true, true);
            this.openModal('builderEditorModal');
        };
        input.click();
    },

    async _builderDeleteNode(name) {
        if (!confirm(this.t('builder.confirmDelete', 'Archive this node on the ESPHome dashboard?') + `\n${name}`)) return;
        const rsp = await fetch(`/api/builder/nodes/${encodeURIComponent(name)}`, { method: 'DELETE' });
        if (rsp.ok) {
            this.showToast('success', 'Builder', this.t('builder.archived', 'Node archived.'));
            this._renderBuilderNodes();
        } else {
            const d = await rsp.json().catch(() => ({}));
            this.showToast('error', 'Builder', this._esc(String(d.detail || rsp.status)));
        }
    },

    async _builderShowDownloads(name) {
        const rsp = await fetch(`/api/builder/nodes/${encodeURIComponent(name)}/downloads`);
        if (!rsp.ok) {
            this.showToast('error', 'Builder', this.t('builder.noBinaries', 'No binaries — build the node first.'));
            return;
        }
        const d = (await rsp.json()).downloads || [];
        const body = document.getElementById('builderDlBody');
        body.innerHTML = d.length ? d.map(x => `
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
                <div style="flex:1;"><b>${this._esc(x.title || x.file)}</b><br>
                    <span style="color:var(--text-secondary);font-size:12px;">${this._esc(x.description || '')}</span></div>
                <a class="btn btn-sm" href="/api/builder/nodes/${encodeURIComponent(name)}/download?file=${encodeURIComponent(x.file)}&download=${encodeURIComponent(x.download || '')}">
                    <i class="bi bi-download"></i> ${this._esc(x.file)}</a>
            </div>`).join('')
            : `<p style="color:var(--text-secondary);">${this.t('builder.noBinaries', 'No binaries — build the node first.')}</p>`;
        this.openModal('builderDownloadsModal');
    },

    // ---- live console (WS relay) --------------------------------------------------

    openBuilderConsole(command, name) {
        const titles = { compile: this.t('builder.build', 'Build'),
                         validate: this.t('builder.validate', 'Validate'),
                         upload: this.t('builder.flashOta', 'Flash OTA'),
                         logs: this.t('builder.logs', 'Logs'),
                         'update-all': this.t('builder.updateAll', 'Update all'),
                         clean: 'Clean' };
        document.getElementById('builderConTitle').textContent =
            (titles[command] || command) + (name ? ` — ${name}` : '');
        const out = document.getElementById('builderConOut');
        out.textContent = '';
        const status = document.getElementById('builderConStatus');
        status.textContent = this.t('builder.running', 'running…');
        status.className = 'sink-pill';
        this._builderCloseWs();
        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        const ws = new WebSocket(`${proto}://${location.host}/api/builder/stream/${command}?configuration=${encodeURIComponent(name)}&port=OTA`);
        this._builderWs = ws;
        // strip color codes — ESPHome emits both real ESC bytes and the
        // literal text "\033[32m" depending on the subprocess tty mode
        const ansi = /\x1b\[[0-9;]*m|\\033\[[0-9;]*m/g;
        ws.onmessage = (ev) => {
            let msg;
            try { msg = JSON.parse(ev.data); } catch (e) { return; }
            if (msg.event === 'line') {
                const atBottom = out.scrollTop + out.clientHeight >= out.scrollHeight - 8;
                out.textContent += String(msg.data || '').replace(ansi, '');
                if (atBottom) out.scrollTop = out.scrollHeight;
            } else if (msg.event === 'exit') {
                const ok = msg.code === 0;
                status.textContent = ok ? this.t('builder.success', 'success')
                                        : `${this.t('builder.failed', 'failed')} (exit ${msg.code})`;
                status.className = 'sink-pill ' + (ok ? 'ok' : 'bad');
                if (ok && command === 'compile') this._builderLastBuilt = name;
            } else if (msg.event === 'error') {
                status.textContent = this.t('builder.failed', 'failed');
                status.className = 'sink-pill bad';
                out.textContent += `\n${msg.data || 'error'}\n`;
            }
        };
        ws.onclose = () => {
            if (status.textContent === this.t('builder.running', 'running…')) {
                status.textContent = this.t('builder.stopped', 'stopped');
                status.className = 'sink-pill warn';
            }
        };
        this.openModal('builderConsoleModal');
    },

    _builderCloseWs() {
        if (this._builderWs) {
            try { this._builderWs.close(); } catch (e) { /* already closed */ }
            this._builderWs = null;
        }
    },

    // Adopt an mDNS-importable node onto the dashboard (shared by the Builder
    // card list and the ESPHome discovery results).
    async _builderImportNode(entry, btn) {
        if (!entry) return;
        if (btn) { btn.disabled = true; }
        const rsp = await fetch('/api/builder/import', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: entry.name, friendly_name: entry.friendly_name || '',
                project_name: entry.project_name || '',
                package_import_url: entry.package_import_url || '',
            }) });
        const d = await rsp.json().catch(() => ({}));
        if (btn) { btn.disabled = false; }
        if (rsp.ok) {
            this.showToast('success', 'Builder',
                `${d.configuration} ${this.t('builder.imported', 'imported — it is now a managed node.')}`);
            this._renderBuilderNodes();
        } else {
            const msg = (d.detail && d.detail.errors) ? d.detail.errors.join('; ') : (d.detail || rsp.status);
            this.showToast('error', 'Builder', this._esc(String(msg)));
        }
    },

    // ESPHome LAN sweep results (Discover devices modal). `nodesInfo` is the
    // dashboard view: configured → "managed" chip, importable → Import action.
    _renderEsphomeScan(d, nodesInfo) {
        const box = document.getElementById('discoverResult');
        if (!box) return;
        const results = d.results || [];
        if (!results.length) {
            box.innerHTML = `<div class="settings-card" style="padding:12px;color:var(--text-secondary);">${this.t('devices.scanNone', 'No devices answered on')} ${d.scanned} ${this.t('devices.scanHosts', 'hosts.')}</div>`;
            return;
        }
        const managed = new Set(((nodesInfo && nodesInfo.configured) || []).map(n => n.name));
        const importable = ((nodesInfo && nodesInfo.importable) || []);
        const impByName = Object.fromEntries(importable.map((n, i) => [n.name, i]));
        box.innerHTML = `<div class="settings-card" style="padding:6px 12px;">` + results.map(r => {
            const name = r.name || this.t('builder.espUnknown', 'ESPHome device');
            const chips = [];
            if (r.encrypted) chips.push(`<span class="dev-chip" title="${this.t('builder.encryptedTip', 'Native API uses encryption — identity not readable, but the device is ESPHome')}">${this.t('builder.encrypted', 'encrypted API')}</span>`);
            if (managed.has(r.name)) chips.push(`<span class="dev-chip">${this.t('builder.managed', 'managed here')}</span>`);
            const impIdx = impByName[r.name];
            const action = impIdx !== undefined
                ? `<button class="btn btn-ghost btn-sm" data-scan-imp="${impIdx}"><i class="bi bi-box-arrow-in-down"></i> ${this.t('builder.import', 'Import')}</button>`
                : (managed.has(r.name) || r.encrypted ? ''
                   : `<span class="field-hint">${this.t('builder.espForeign', 'not adoptable — connect it via MQTT or reflash with the Builder')}</span>`);
            return `<div class="device-row" style="cursor:default;">
                <span class="status-dot" style="--dot:var(--success,#22c55e)"></span>
                <div class="device-row-main">
                    <div class="device-row-title">${this._esc(name)} ${chips.join(' ')}</div>
                    <div class="device-row-sub">${this._esc(r.host)}:${r.port}${r.api_version ? ` · API ${this._esc(r.api_version)}` : ''}${r.server_info ? ` · ${this._esc(r.server_info)}` : ''}</div>
                </div>
                <div class="device-row-actions">${action}</div>
            </div>`;
        }).join('') + `</div>
        <div class="field-hint" style="margin-top:6px;">${d.scanned} ${this.t('devices.scanHosts', 'hosts.')} · ${d.elapsed_s}s</div>`;
        box.querySelectorAll('button[data-scan-imp]').forEach(b => b.addEventListener('click', () =>
            this._builderImportNode(importable[parseInt(b.dataset.scanImp, 10)], b)));
    },

    // "Deploy new device" in the Devices toolbar: straight into the generator
    // when the Builder is ready, otherwise land the user on its settings.
    openBuilderDeploy() {
        const st = this._builderStatus;
        if (st && st.enabled && st.reachable) {
            this.openBuilderGenerator();
            return;
        }
        const card = document.getElementById('builderCard');
        if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
        const panel = document.getElementById('builderSettingsPanel');
        if (panel) panel.style.display = '';
        this.showToast('info', 'Builder', this.t('builder.configureFirst',
            'Connect the Builder to an ESPHome instance first (settings below).'));
    },

    // ---- USB web flasher (esp-web-tools, vendored — no CDN, no cloud) ----------

    async openBuilderFlasher(name) {
        const body = document.getElementById('builderFlashBody');
        if (!('serial' in navigator)) {
            body.innerHTML = `<p style="color:var(--warning,#f59e0b);max-width:520px;">${this.t('builder.noWebSerial',
                'This browser/context has no WebSerial. Use Chrome/Edge over HTTPS (or http://localhost), or download the factory binary from "Binaries" and flash it with any esptool.')}</p>`;
            this.openModal('builderFlashModal');
            return;
        }
        if (!this._espWebToolsLoaded) {
            const s = document.createElement('script');
            s.type = 'module';
            s.src = '/static/vendor/esp-web-tools/install-button.js';
            document.head.appendChild(s);
            this._espWebToolsLoaded = true;
        }
        body.innerHTML = `
            <p style="max-width:520px;">${this.t('builder.flashUsbHelp',
                'Connect the board over USB, then click Install. After flashing, the same dialog can provision Wi-Fi over the cable (Improv).')}</p>
            <esp-web-install-button manifest="/api/builder/nodes/${encodeURIComponent(name)}/manifest">
                <button class="btn btn-primary" slot="activate"><i class="bi bi-usb-plug"></i> ${this.t('builder.install', 'Install')}</button>
                <span slot="unsupported" style="color:var(--warning,#f59e0b);">${this.t('builder.noWebSerial2', 'WebSerial not available in this browser.')}</span>
                <span slot="not-allowed" style="color:var(--danger,#ef4444);">${this.t('builder.notAllowed', 'Not allowed in an insecure context — open the UI over HTTPS.')}</span>
            </esp-web-install-button>`;
        this.openModal('builderFlashModal');
    },

    // ---- generator wizard (template → node firmware + paired device) ----------

    async openBuilderGenerator() {
        let templates = [];
        try {
            templates = (await (await fetch('/api/device-templates')).json()).templates
                .filter(t => t.transport === 'modbus');
        } catch (e) { /* empty list renders a message */ }
        this._bgLoadProfiles();
        const sel = document.getElementById('bgTemplate');
        sel.innerHTML = templates.length
            ? templates.map(t => `<option value="${this._esc(t.id)}">${this._esc(t.name)} (${t.registers})</option>`).join('')
            : `<option value="">${this.t('builder.noModbusTpl', 'No Modbus templates available')}</option>`;
        sel.onchange = () => this._bgLoadRegisters(sel.value);
        document.getElementById('bgRegFilter').oninput = () => this._bgFilterRegisters();
        document.getElementById('bgSelAll').onclick = () => this._bgToggleAll(true);
        document.getElementById('bgSelNone').onclick = () => this._bgToggleAll(false);
        document.getElementById('bgGenerateBtn').onclick = () => this._bgGenerate();
        document.getElementById('bgSaveBtn').onclick = () => this._bgSave(false);
        document.getElementById('bgAdoptBtn').onclick = () => this._bgSave(true);
        document.getElementById('bgPreviewWrap').style.display = 'none';
        this._bgResult = null;
        if (templates.length) this._bgLoadRegisters(templates[0].id);
        this.openModal('builderGenModal');
    },

    async _bgLoadRegisters(tplId) {
        const box = document.getElementById('bgRegList');
        box.innerHTML = '…';
        if (!tplId) { box.innerHTML = ''; return; }
        const d = await (await fetch(`/api/device-templates/${encodeURIComponent(tplId)}`)).json();
        const regs = (d.device_template && d.device_template.registers) || [];
        this._bgRegs = regs;
        box.innerHTML = regs.map((r, i) => {
            const preselect = regs.length <= 40 || (r.defaults && Object.keys(r.defaults).length);
            return `<label class="bg-reg" style="display:flex;gap:6px;align-items:center;font-size:12px;padding:1px 0;">
                <input type="checkbox" data-reg="${this._esc(r.name)}" ${preselect ? 'checked' : ''}>
                <code>${this._esc(r.name)}</code>
                <span style="color:var(--text-secondary);">${this._esc(r.label || '')} ${r.unit ? '[' + this._esc(r.unit) + ']' : ''} @${r.address}</span>
            </label>`;
        }).join('');
    },

    _bgFilterRegisters() {
        const q = document.getElementById('bgRegFilter').value.toLowerCase();
        document.querySelectorAll('#bgRegList .bg-reg').forEach(el => {
            el.style.display = !q || el.textContent.toLowerCase().includes(q) ? '' : 'none';
        });
    },

    _bgToggleAll(on) {
        document.querySelectorAll('#bgRegList .bg-reg').forEach(el => {
            if (el.style.display !== 'none') el.querySelector('input').checked = on;
        });
    },

    async _bgLoadProfiles() {
        const sel = document.getElementById('bgProfile');
        let profiles = [];
        try {
            profiles = (await (await fetch('/api/builder/profiles')).json()).profiles || [];
        } catch (e) { /* dropdown stays empty */ }
        this._bgProfiles = profiles;
        sel.innerHTML = `<option value="">${this.t('builder.customHw', '(custom hardware)')}</option>`
            + profiles.map(p => `<option value="${this._esc(p.id)}">${this._esc(p.name)}${p.builtin ? '' : ' *'}</option>`).join('');
        sel.onchange = () => {
            const p = this._bgProfiles.find(x => x.id === sel.value);
            if (!p) return;
            document.getElementById('bgPlatform').value = p.platform || 'esp32';
            document.getElementById('bgBoard').value = p.board || '';
            document.getElementById('bgTx').value = p.tx_pin || '';
            document.getElementById('bgRx').value = p.rx_pin || '';
            document.getElementById('bgFlow').value = p.flow_control_pin || '';
            document.getElementById('bgBaud').value = String(p.baud_rate || 9600);
            document.getElementById('bgParity').value = p.parity || 'NONE';
            document.getElementById('bgStop').value = String(p.stop_bits || 1);
        };
        document.getElementById('bgProfileSave').onclick = async () => {
            const name = prompt(this.t('builder.profileNameQ', 'Profile name (e.g. LilyGO T-CAN485):'));
            if (!name) return;
            const v = id => document.getElementById(id).value.trim();
            const rsp = await fetch('/api/builder/profiles', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, platform: v('bgPlatform'), board: v('bgBoard'),
                    tx_pin: v('bgTx'), rx_pin: v('bgRx'), flow_control_pin: v('bgFlow'),
                    baud_rate: parseInt(v('bgBaud') || '9600', 10),
                    parity: v('bgParity'), stop_bits: parseInt(v('bgStop') || '1', 10) }) });
            if (rsp.ok) { this.showToast('success', 'Builder', this.t('builder.profileSaved', 'Hardware profile saved.')); this._bgLoadProfiles(); }
        };
    },

    _bgPayload() {
        const v = id => document.getElementById(id).value.trim();
        const registers = [...document.querySelectorAll('#bgRegList input:checked')]
            .map(i => i.dataset.reg);
        return {
            template_id: document.getElementById('bgTemplate').value,
            registers,
            node: { name: v('bgName'), friendly_name: v('bgFriendly'),
                    platform: v('bgPlatform'), board: v('bgBoard') },
            uart: { tx_pin: v('bgTx'), rx_pin: v('bgRx'),
                    flow_control_pin: v('bgFlow'),
                    baud_rate: parseInt(v('bgBaud') || '9600', 10),
                    parity: v('bgParity'), stop_bits: parseInt(v('bgStop') || '1', 10) },
            modbus: { unit_id: parseInt(v('bgUnitId') || '1', 10) },
            mqtt: { broker: v('bgBroker'), topic_prefix: v('bgPrefix') },
        };
    },

    async _bgGenerate() {
        const msg = document.getElementById('bgMsg');
        msg.textContent = '';
        const rsp = await fetch('/api/builder/generate', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(this._bgPayload()) });
        const d = await rsp.json().catch(() => ({}));
        if (!rsp.ok) {
            const errs = (d.detail && d.detail.errors) ? d.detail.errors.join('; ') : (d.detail || rsp.status);
            msg.innerHTML = `<span style="color:var(--danger,#ef4444);">${this._esc(String(errs))}</span>`;
            return;
        }
        this._bgResult = d;
        document.getElementById('bgPreviewWrap').style.display = '';
        document.getElementById('bgYaml').value = d.yaml;
        const warn = (d.warnings || []).map(w => `<li>${this._esc(w)}</li>`).join('');
        document.getElementById('bgWarnings').innerHTML = warn
            ? `<ul style="color:var(--warning,#f59e0b);margin:6px 0;">${warn}</ul>` : '';
        msg.innerHTML = `<span style="color:var(--text-secondary);">${d.topics.length} ${this.t('builder.topicsReady', 'MQTT topics — review the YAML, then save.')}</span>`;
    },

    async _bgSave(adopt) {
        const d = this._bgResult;
        if (!d) return;
        const msg = document.getElementById('bgMsg');
        const step = async (label, fn) => {
            msg.textContent = label + '…';
            const rsp = await fn();
            if (!rsp.ok && rsp.status !== 409) {
                const e = await rsp.json().catch(() => ({}));
                const errs = (e.detail && e.detail.errors) ? e.detail.errors.join('; ') : (e.detail || rsp.status);
                throw new Error(`${label}: ${errs}`);
            }
            return rsp;
        };
        try {
            // 1) firmware YAML onto the dashboard (ask before overwriting)
            let rsp = await step(this.t('builder.savingYaml', 'Saving node YAML'), () =>
                fetch(`/api/builder/nodes/${encodeURIComponent(d.node_yaml_name)}/config`, {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ content: d.yaml }) }));
            if (rsp.status === 409) {
                if (!confirm(this.t('builder.overwriteQ', 'Node YAML already exists on the dashboard. Overwrite?'))) return;
                await step(this.t('builder.savingYaml', 'Saving node YAML'), () =>
                    fetch(`/api/builder/nodes/${encodeURIComponent(d.node_yaml_name)}/config`, {
                        method: 'PUT', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ content: d.yaml, overwrite: true }) }));
            }
            // 2) required secrets get placeholders/values (never overwrites)
            await step(this.t('builder.ensuringSecrets', 'Ensuring secrets.yaml keys'), () =>
                fetch('/api/builder/secrets/ensure', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ keys: d.secrets }) }));
            if (adopt) {
                // 3) paired template + 4) mqtt-in device (existing endpoints)
                await step(this.t('builder.savingTpl', 'Uploading paired template'), () =>
                    fetch('/api/device-templates/upload', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ template: d.device_template, overwrite: true }) }));
                await step(this.t('builder.creatingDev', 'Creating gateway device'), () =>
                    fetch('/api/devices', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(d.device_payload) }));
            }
            this.closeModal('builderGenModal');
            this.showToast('success', 'Builder', adopt
                ? this.t('builder.adopted', 'Node saved and adopted — fill secrets.yaml, build, flash, and data flows in.')
                : this.t('builder.genSaved', 'Node YAML saved on the ESPHome dashboard.'));
            this._renderBuilderNodes();
        } catch (e) {
            msg.innerHTML = `<span style="color:var(--danger,#ef4444);">${this._esc(String(e.message || e))}</span>`;
        }
    },
});
