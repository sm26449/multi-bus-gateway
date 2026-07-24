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
            el.innerHTML = `<p style="color:#c0392b;">${this.t('builder.loadFail', 'Could not load builder status.')}</p>`;
            return;
        }
        this._builderStatus = st;
        if (!st.enabled) {
            el.innerHTML = `
                <div class="card" style="max-width:680px;">
                    <p style="margin-top:0;">${this.t('builder.introOff',
                        'Author, compile and flash ESP32/ESP8266 node firmware from this UI, using an external ESPHome dashboard as the build engine. Point the gateway at your ESPHome container to enable it.')}</p>
                    ${this._builderSettingsFormHtml()}
                </div>`;
            this._wireBuilderSettings();
            return;
        }
        const banner = st.reachable
            ? `<span class="sink-pill ok">ESPHome ${this._esc(st.version)}</span>
               <span style="color:var(--text-secondary);font-size:12px;">${this._esc(st.url)}</span>`
            : `<span class="sink-pill bad">${this.t('builder.unreachable', 'unreachable')}</span>
               <span style="color:var(--text-secondary);font-size:12px;">${this._esc(st.error || st.url)}</span>`;
        el.innerHTML = `
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px;">
                ${banner}
                <span style="flex:1;"></span>
                <button class="btn" id="builderNewBtn"><i class="bi bi-plus-lg"></i> ${this.t('builder.newNode', 'New node')}</button>
                <button class="btn" id="builderImportBtn"><i class="bi bi-upload"></i> ${this.t('builder.importYaml', 'Import YAML')}</button>
                <button class="btn btn-ghost" id="builderSettingsBtn"><i class="bi bi-gear"></i> ${this.t('common.settings', 'Settings')}</button>
            </div>
            <div id="builderSettingsPanel" style="display:none;max-width:680px;margin-bottom:14px;" class="card">
                ${this._builderSettingsFormHtml()}
            </div>
            <div id="builderNodes">${st.reachable ? '' : `<p style="color:var(--text-secondary);">${this.t('builder.fixConn', 'Fix the connection to list nodes.')}</p>`}</div>`;
        this._wireBuilderSettings();
        document.getElementById('builderSettingsBtn').addEventListener('click', () => {
            const p = document.getElementById('builderSettingsPanel');
            p.style.display = p.style.display === 'none' ? '' : 'none';
        });
        document.getElementById('builderNewBtn').addEventListener('click', () => this.openBuilderEditor(''));
        document.getElementById('builderImportBtn').addEventListener('click', () => this._builderImportYaml());
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
            el.innerHTML = `<p style="color:#c0392b;">${this._esc(String(e.message || e))}</p>`;
            return;
        }
        const nodes = data.configured || [];
        const importable = data.importable || [];
        if (!nodes.length && !importable.length) {
            el.innerHTML = `<p style="color:var(--text-secondary);">${this.t('builder.empty',
                'No nodes yet — create one with "New node" or import an existing YAML.')}</p>`;
            return;
        }
        const rows = nodes.map(n => {
            const name = this._esc(n.configuration);
            const online = n.address ? `<span class="sink-pill ok" title="${this._esc(n.address)}">online</span>` : '';
            const ver = n.deployed_version ? `<span style="color:var(--text-secondary);font-size:12px;">v${this._esc(n.deployed_version)}</span>` : '';
            return `
            <div class="card" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px;padding:10px 14px;">
                <i class="bi bi-cpu" style="font-size:20px;"></i>
                <div style="min-width:180px;">
                    <b>${this._esc(n.name || n.configuration)}</b><br>
                    <span style="color:var(--text-secondary);font-size:12px;">${name}</span>
                </div>
                ${online} ${ver}
                <span style="flex:1;"></span>
                <button class="btn btn-sm" data-act="edit" data-name="${name}"><i class="bi bi-pencil"></i> ${this.t('common.edit', 'Edit')}</button>
                <button class="btn btn-sm" data-act="validate" data-name="${name}"><i class="bi bi-check2-circle"></i> ${this.t('builder.validate', 'Validate')}</button>
                <button class="btn btn-sm" data-act="compile" data-name="${name}"><i class="bi bi-hammer"></i> ${this.t('builder.build', 'Build')}</button>
                <button class="btn btn-sm" data-act="upload" data-name="${name}"><i class="bi bi-broadcast-pin"></i> ${this.t('builder.flashOta', 'Flash OTA')}</button>
                <button class="btn btn-sm" data-act="logs" data-name="${name}"><i class="bi bi-terminal"></i> ${this.t('builder.logs', 'Logs')}</button>
                <button class="btn btn-sm" data-act="downloads" data-name="${name}"><i class="bi bi-download"></i> ${this.t('builder.binaries', 'Binaries')}</button>
                <button class="btn btn-sm btn-ghost" data-act="delete" data-name="${name}" title="${this.t('builder.deleteTip', 'Archive on the ESPHome dashboard (recoverable there)')}"><i class="bi bi-trash"></i></button>
            </div>`;
        }).join('');
        const imp = importable.length ? `
            <h4 style="margin:18px 0 8px;">${this.t('builder.discovered', 'Discovered on the network (adoptable)')}</h4>
            ${importable.map(n => `<div class="card" style="margin-bottom:8px;padding:10px 14px;">
                <i class="bi bi-broadcast"></i> <b>${this._esc(n.name || '')}</b>
                <span style="color:var(--text-secondary);font-size:12px;">${this._esc(n.friendly_name || '')} ${this._esc(n.network || '')}</span>
            </div>`).join('')}` : '';
        el.innerHTML = rows + imp;
        el.querySelectorAll('button[data-act]').forEach(b => b.addEventListener('click', () => {
            const name = b.dataset.name, act = b.dataset.act;
            if (act === 'edit') this.openBuilderEditor(name);
            else if (act === 'validate') this.openBuilderConsole('validate', name);
            else if (act === 'compile') this.openBuilderConsole('compile', name);
            else if (act === 'upload') this.openBuilderConsole('upload', name);
            else if (act === 'logs') this.openBuilderConsole('logs', name);
            else if (act === 'downloads') this._builderShowDownloads(name);
            else if (act === 'delete') this._builderDeleteNode(name);
        }));
    },

    // ---- settings ------------------------------------------------------------

    _builderSettingsFormHtml() {
        return `
            <h4 style="margin:0 0 10px;"><i class="bi bi-gear"></i> ${this.t('builder.settingsTitle', 'ESPHome connection')}</h4>
            <div class="form-group"><label><input type="checkbox" id="bsEnabled"> ${this.t('builder.enable', 'Enable the Device Builder')}</label></div>
            <div class="form-group"><label>URL</label>
                <input type="text" id="bsUrl" placeholder="http://esphome:6052" style="width:100%;"></div>
            <div style="display:flex;gap:10px;flex-wrap:wrap;">
                <div class="form-group" style="flex:1;"><label>${this.t('builder.user', 'Username (optional)')}</label>
                    <input type="text" id="bsUser" autocomplete="off" style="width:100%;"></div>
                <div class="form-group" style="flex:1;"><label>${this.t('builder.pass', 'Password (optional)')}</label>
                    <input type="password" id="bsPass" autocomplete="new-password" placeholder="••••" style="width:100%;"></div>
            </div>
            <button class="btn btn-primary" id="bsSaveBtn"><i class="bi bi-save"></i> ${this.t('common.save', 'Save')}</button>
            <span id="bsMsg" style="margin-left:10px;font-size:13px;"></span>`;
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
                document.getElementById('bsMsg').innerHTML = `<span style="color:#c0392b;">${this._esc(String(msg))}</span>`;
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
                         clean: 'Clean' };
        document.getElementById('builderConTitle').textContent = `${titles[command] || command} — ${name}`;
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
});
