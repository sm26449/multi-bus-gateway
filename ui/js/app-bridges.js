// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
//
// bridges — augments JanitzaMonitor.prototype
//
// A bridge is the box a serial bus is reached through: our ser2net container
// on a Raspberry Pi, a Waveshare / USR / Elfin converter. It lives on the
// Devices page with its buses (ports) and the devices on each; what a KIND of
// bridge can do comes from the server (bridge_types/*.json), never from here.

Object.assign(JanitzaMonitor.prototype, {

    async _loadBridges() {
        try {
            const [b, t] = await Promise.all([fetch('/api/bridges'), fetch('/api/bridge-types')]);
            this._bridges = (await b.json()).bridges || [];
            this._bridgeTypes = (await t.json()).types || [];
        } catch (e) { this._bridges = this._bridges || []; this._bridgeTypes = this._bridgeTypes || []; }
        return this._bridges;
    },

    _bridgeType(id) { return (this._bridgeTypes || []).find(t => t.id === id) || {}; },

    // ── the Devices page section ────────────────────────────────────────────
    _bridgesSectionHtml(bridges, healthColor) {
        if (!bridges.length) return '';
        const t = (k, d, p) => this.t(k, d, p);
        const dot = { online: 'var(--success,#22c55e)', degraded: 'var(--warning,#f59e0b)',
                      offline: 'var(--danger,#ef4444)', unknown: 'var(--text-secondary)', checking: 'var(--text-secondary)' };
        return bridges.map(b => {
            const ty = this._bridgeType(b.type);
            const st = b.state || {};
            const ports = (b.ports || []).map(p => `
                <div class="bridge-port">
                  <div class="bridge-port-head">
                    <i aria-hidden="true" class="bi bi-diagram-2"></i>
                    <code>:${this._esc(p.port)}</code>
                    ${p.label ? `<span>${this._esc(p.label)}</span>` : ''}
                    ${p.serial_text || p.serial ? `<span class="dev-chip">${this._esc(p.serial_text || p.serial)}</span>` : ''}
                    <span class="field-hint">${t('bridges.nDevices', '{n} device(s)', { n: (p.devices || []).length })}</span>
                    <span class="bridge-port-actions">
                      <button data-admin class="btn btn-ghost btn-sm" ${this._act('addDeviceOnBus', [b.id, p.port])} title="${this._esc(t('bridges.addDeviceHint', 'Add a device (a slave) on this bus'))}"><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('bridges.addDevice', 'Device')}</button>
                      <button data-admin class="btn btn-ghost btn-sm" ${this._act('bridgeProbe', [b.id, p.port])} title="${this._esc(t('bridges.probeHint', 'Ask a slave on this bus one register, as RTU and as Modbus TCP — tells whether the box is transparent or a gateway'))}"><i aria-hidden="true" class="bi bi-activity"></i> ${t('bridges.probe', 'Check mode')}</button>
                    </span>
                  </div>
                  ${(p.devices || []).map(d => `
                    <div class="bridge-dev" role="button" tabindex="0" ${this._act(d.endpoint_id ? 'openEndpointDetail' : 'openDeviceDetail', [d.endpoint_id || d.id])}>
                      <span class="status-dot" style="--dot:${healthColor[d.health] || healthColor.idle}"></span>
                      <span class="dev-chip">unit ${this._esc(d.unit_id)}</span>
                      <span>${this._esc(d.name || d.id)}</span>
                      ${d.endpoint_id ? `<span class="field-hint">${t('bridges.inInstallation', 'in an installation')}</span>` : ''}
                    </div>`).join('') || `<div class="field-hint bridge-dev">${t('bridges.noDevices', 'No device on this bus yet.')}</div>`}
                </div>`).join('');
            return `
            <div class="bridge-card" data-bridge="${this._esc(b.id)}">
              <div class="bridge-head">
                <i aria-hidden="true" class="bi bi-hdd-network"></i>
                <span class="status-dot" style="--dot:${dot[st.status] || dot.unknown}" title="${this._esc(st.detail || st.status || '')}"></span>
                <b>${this._esc(b.name || b.id)}</b>
                <span class="dev-chip">${this._esc(ty.name || b.type)}</span>
                <code>${this._esc(b.host)}</code>
                <span class="field-hint">${this._esc(t('bridges.state.' + (st.status || 'unknown'), st.status || 'unknown'))}${st.detail ? ' — ' + this._esc(st.detail) : ''}</span>
                <span class="bridge-actions">
                  ${ty.discovery === 'api' ? `<button class="btn btn-ghost btn-sm" ${this._act('bridgeScan', [b.id])}><i aria-hidden="true" class="bi bi-arrow-repeat"></i> ${t('bridges.scan', 'Buses')}</button>` : ''}
                  <button class="btn btn-ghost btn-sm" ${this._act('bridgeSetup', [b.id])}><i aria-hidden="true" class="bi bi-wrench"></i> ${t('bridges.setup', 'Set-up')}</button>
                  <button data-admin class="btn btn-ghost btn-sm" ${this._act('openBridgeModal', [b.id])} aria-label="${t('common.edit', 'Edit')}"><i aria-hidden="true" class="bi bi-pencil"></i></button>
                  <button data-admin class="btn btn-ghost btn-sm" ${this._act('deleteBridge', [b.id])} aria-label="${t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-trash"></i></button>
                </span>
              </div>
              ${ports || `<div class="field-hint bridge-dev">${ty.discovery === 'api'
                    ? t('bridges.noPortsApi', 'No bus seen yet — start the bridge on its host (Set-up), plug an adapter in, then Buses.')
                    : t('bridges.noPorts', 'No bus declared.')}</div>`}
            </div>`;
        }).join('');
    },

    // ── add / edit ──────────────────────────────────────────────────────────
    async openBridgeModal(id = null) {
        await this._loadBridges();
        const t = (k, d, p) => this.t(k, d, p);
        const b = id ? (this._bridges || []).find(x => x.id === id) : null;
        this._bridgeEdit = { id, data: b ? JSON.parse(JSON.stringify(b)) : {
            id: '', name: '', type: (this._bridgeTypes[0] || {}).id || 'rtu_transparent', host: '',
            ports: [], max_connections: 1 } };
        document.getElementById('bridgeTitle').textContent = id
            ? t('bridges.editTitle', 'Edit bridge') : t('bridges.addTitle', 'Add a bridge');
        document.getElementById('bridgeFeedback').textContent = '';
        this._renderBridgeForm();
        this.openModal('bridgeModal');
    },

    _renderBridgeForm() {
        const t = (k, d, p) => this.t(k, d, p);
        const e = this._bridgeEdit, d = e.data;
        const ty = this._bridgeType(d.type);
        const manual = ty.discovery !== 'api';
        if (manual && !(d.ports || []).length) d.ports = [{ port: ty.default_port || 4196, label: '' }];
        document.getElementById('bridgeBody').innerHTML = `
          <div class="bridge-types" role="radiogroup">
            ${(this._bridgeTypes || []).map(x => `
              <label class="bridge-type ${x.id === d.type ? 'on' : ''}">
                <input type="radio" name="brType" value="${this._esc(x.id)}" ${x.id === d.type ? 'checked' : ''} ${e.id ? 'disabled' : ''}>
                <b>${this._esc(x.name)}</b>
                <span>${this._esc(x.description || '')}</span>
              </label>`).join('')}
          </div>
          <div class="form-row">
            <div class="form-group flex-2"><label class="form-label" for="brName">${t('bridges.name', 'Name')}</label>
              <input class="input" id="brName" value="${this._esc(d.name || '')}" placeholder="${this._esc(t('bridges.namePh', 'Pi in the garage'))}"></div>
            <div class="form-group"><label class="form-label" for="brId">Id</label>
              <input class="input" id="brId" value="${this._esc(d.id || '')}" ${e.id ? 'disabled' : ''} placeholder="pi-garage"></div>
            <div class="form-group"><label class="form-label" for="brHost">${t('bridges.host', 'IP / host')}</label>
              <input class="input" id="brHost" value="${this._esc(d.host || '')}" placeholder="192.168.1.50"></div>
          </div>
          ${ty.framing === 'modbus_tcp' ? `
          <div class="form-row"><div class="form-group"><label class="form-label" for="brMax">${t('bridges.maxConn', 'Connections it accepts at once')}</label>
            <input class="input" id="brMax" type="number" min="1" max="16" value="${d.max_connections || 1}">
            <div class="field-hint">${t('bridges.maxConnHint', 'With multi-host on, several systems can read it; the gateway opens this many.')}</div></div></div>` : ''}
          ${ty.discovery === 'api' ? `
          <div class="form-row"><div class="form-group"><label class="form-label" for="brCtrl">${t('bridges.ctrlPort', 'Control port')}</label>
            <input class="input" id="brCtrl" type="number" value="${d.control_port || ty.control_port || 7000}"></div></div>
          <div class="field-hint">${t('bridges.apiHint', 'Its buses are read from the bridge itself; after saving, Set-up gives the command to start it on its host, with its token.')}</div>` : `
          <div class="form-group"><label class="form-label">${t('bridges.ports', 'Buses on this box (TCP ports)')}</label>
            ${(d.ports || []).map((p, i) => `
              <div class="form-row bridge-port-row">
                <input class="input br-port" data-i="${i}" type="number" min="1" max="65535" value="${this._esc(p.port || '')}" aria-label="TCP port">
                <input class="input br-label" data-i="${i}" value="${this._esc(p.label || '')}" placeholder="${this._esc(t('bridges.portLabel', 'label, e.g. panel A'))}">
                <input class="input br-serial" data-i="${i}" value="${this._esc(p.serial || '')}" placeholder="9600 8N1" title="${this._esc(t('bridges.serialNote', 'What the converter is set to — for your reference; set it in its web page'))}">
                <button class="btn btn-ghost btn-sm" ${this._act('bridgeDelPort', [i])} aria-label="${t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-x-lg"></i></button>
              </div>`).join('')}
            ${(d.ports || []).length < (ty.max_ports || 4) ? `<button class="btn btn-ghost btn-sm" ${this._act('bridgeAddPort', [])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('bridges.addPort', 'Bus')}</button>` : ''}
          </div>`}
          <details class="tpl-proto" open><summary><i aria-hidden="true" class="bi bi-wrench"></i> ${t('bridges.onTheBox', 'What to set on the box')}</summary>
            <ul class="tplu-list">${(ty.setup || []).map(s => `<li>${this._esc(s)}</li>`).join('')}</ul></details>`;
        document.querySelectorAll('#bridgeBody input[name="brType"]').forEach(r => r.addEventListener('change', () => {
            this._bridgeCollect(); d.type = r.value; d.ports = []; this._renderBridgeForm(); }));
        const n = document.getElementById('brName'), idf = document.getElementById('brId');
        if (n && idf && !e.id) n.addEventListener('input', () => {
            if (!idf.dataset.touched) idf.value = n.value.toLowerCase().normalize('NFD').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40);
        });
        idf?.addEventListener('input', () => { idf.dataset.touched = '1'; });
    },

    _bridgeCollect() {
        const d = this._bridgeEdit.data, g = id => document.getElementById(id);
        d.name = g('brName')?.value.trim() ?? d.name;
        if (!this._bridgeEdit.id) d.id = g('brId')?.value.trim() ?? d.id;
        d.host = g('brHost')?.value.trim() ?? d.host;
        if (g('brMax')) d.max_connections = parseInt(g('brMax').value, 10) || 1;
        if (g('brCtrl')) d.control_port = parseInt(g('brCtrl').value, 10) || 7000;
        document.querySelectorAll('#bridgeBody .br-port').forEach(inp => {
            const i = +inp.dataset.i;
            d.ports[i] = { ...d.ports[i], port: parseInt(inp.value, 10) || null,
                           label: document.querySelector(`#bridgeBody .br-label[data-i="${i}"]`)?.value.trim() || '',
                           serial: document.querySelector(`#bridgeBody .br-serial[data-i="${i}"]`)?.value.trim() || '' };
        });
    },

    bridgeAddPort() {
        this._bridgeCollect();
        const d = this._bridgeEdit.data;
        const next = Math.max(0, ...(d.ports || []).map(p => p.port || 0)) + 1;
        d.ports.push({ port: next > 1 ? next : (this._bridgeType(d.type).default_port || 4196), label: '' });
        this._renderBridgeForm();
    },

    bridgeDelPort(i) {
        this._bridgeCollect();
        this._bridgeEdit.data.ports.splice(i, 1);
        this._renderBridgeForm();
    },

    async saveBridge() {
        const t = (k, d, p) => this.t(k, d, p);
        this._bridgeCollect();
        const e = this._bridgeEdit, d = e.data;
        const body = { id: d.id, name: d.name, type: d.type, host: d.host,
                       max_connections: d.max_connections || 1,
                       ports: (d.ports || []).filter(p => p.port).map(({ devices, ...p }) => p) };
        if (d.control_port) body.control_port = d.control_port;
        const fb = document.getElementById('bridgeFeedback');
        try {
            const r = await fetch(e.id ? `/api/bridges/${encodeURIComponent(e.id)}` : '/api/bridges', {
                method: e.id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body) });
            const res = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error((res.detail?.errors || [r.statusText]).join('\n'));
            this.closeModal('bridgeModal');
            this.showToast('success', t('bridges.saved', 'Bridge saved'),
                (res.restarted || []).length ? t('bridges.restarted', 'its devices reconnected: {list}', { list: res.restarted.join(', ') }) : res.name || res.id);
            await this.renderDevicesList();
            if (!e.id && this._bridgeType(d.type).discovery === 'api') this.bridgeSetup(res.id);
        } catch (err) {
            fb.textContent = String(err.message || err); fb.className = 'save-feedback err';
        }
    },

    async deleteBridge(id) {
        const t = (k, d, p) => this.t(k, d, p);
        if (!confirm(t('bridges.deleteConfirm', 'Delete the bridge {id}?', { id }))) return;
        const r = await fetch(`/api/bridges/${encodeURIComponent(id)}`, { method: 'DELETE' });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { this.showToast('error', t('bridges.deleteFail', 'Cannot delete'), (d.detail?.errors || [r.statusText]).join(' ')); return; }
        this.renderDevicesList();
    },

    // ── information panels: set-up, buses, mode check ───────────────────────
    _bridgeInfo(title, html) {
        document.getElementById('bridgeInfoTitle').textContent = title;
        document.getElementById('bridgeInfoBody').innerHTML = html;
        this.openModal('bridgeInfoModal');
    },

    async bridgeSetup(id) {
        const t = (k, d, p) => this.t(k, d, p);
        const s = await (await fetch(`/api/bridges/${encodeURIComponent(id)}/setup`)).json();
        const steps = `<ol class="tplu-list">${(s.steps || []).map(x => `<li>${this._esc(x)}</li>`).join('')}</ol>`;
        const run = s.docker_run ? `
          <h4 class="tplu-h">${t('bridges.runOnHost', 'On the host the adapters are plugged into')}</h4>
          <p class="field-hint">${t('bridges.runHint', 'Any Linux with Docker — a Raspberry Pi, a server. Copy, run, and the bridge shows online here within a few seconds.')}</p>
          <pre class="code-block" id="brRun">${this._esc(s.docker_run)}</pre>
          <button class="btn btn-secondary btn-sm" ${this._act('copyText', ['brRun'])}><i aria-hidden="true" class="bi bi-clipboard"></i> ${t('common.copy', 'Copy')}</button>
          <p class="field-hint" style="margin-top:8px;">${t('bridges.composeHint', 'Prefer compose: serial-bridge/docker-compose.bridge.yml, with BRIDGE_TOKEN set to the token in the command.')}
            <a href="https://github.com/sm26449/multi-bus-gateway/blob/main/docs/rtu-over-network.md" target="_blank" rel="noopener">${t('bridges.guide', 'Guide')}</a></p>` : '';
        this._bridgeInfo(t('bridges.setupTitle', 'Set-up'), run + `<h4 class="tplu-h">${t('bridges.onTheBox', 'What to set on the box')}</h4>` + steps);
    },

    copyText(elId) {
        const txt = document.getElementById(elId)?.textContent || '';
        navigator.clipboard?.writeText(txt).then(() => this.showToast('success', this.t('common.copied', 'Copied'), ''));
    },

    async bridgeScan(id) {
        const t = (k, d, p) => this.t(k, d, p);
        const r = await fetch(`/api/bridges/${encodeURIComponent(id)}/scan`);
        const s = await r.json().catch(() => ({}));
        if (!r.ok) { this._bridgeInfo(t('bridges.scanTitle', 'Buses'), `<div class="field-error">${this._esc((s.detail?.errors || [r.statusText]).join(' '))}</div>`); return; }
        const PAR = ['N', 'E', 'O'], BAUD = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200];
        const rows = (s.ports || []).map((p, i) => {
            const sp = p.serial_params || {};
            return `<tr>
              <td><code>:${this._esc(p.port)}</code></td><td>${this._esc(p.label || p.dev || '')}${p.excluded ? ` <span class="sink-pill warn">${t('bridges.excluded', 'excluded')}</span>` : ''}</td>
              <td><select class="input input-sm br-baud" data-i="${i}">${BAUD.map(b => `<option ${b === sp.baud ? 'selected' : ''}>${b}</option>`).join('')}</select></td>
              <td><select class="input input-sm br-par" data-i="${i}">${PAR.map(x => `<option ${x === sp.parity ? 'selected' : ''}>${x}</option>`).join('')}</select></td>
              <td><select class="input input-sm br-stop" data-i="${i}">${[1, 2].map(x => `<option ${x === sp.stopbits ? 'selected' : ''}>${x}</option>`).join('')}</select></td>
              <td style="white-space:nowrap;"><button data-admin class="btn btn-ghost btn-sm" ${this._act('bridgeSetSerial', [id, p.key, i])}>${t('common.save', 'Save')}</button>
                <button data-admin class="btn btn-ghost btn-sm" ${this._act('addDeviceOnBus', [id, p.port])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('bridges.addDevice', 'Device')}</button></td></tr>`;
        }).join('');
        this._scanPorts = s.ports || [];
        this._bridgeInfo(t('bridges.scanTitle', 'Buses'), `
          <p class="field-hint">${this._esc(t('bridges.scanLead', '{host} · bridge {version} — one bus per adapter; serial settings apply to that bus only.', { host: s.hostname || id, version: s.version || '' }))}</p>
          ${rows ? `<table class="tplu-table"><thead><tr><th>Port</th><th>${t('bridges.adapter', 'Adapter')}</th><th>Baud</th><th>${t('bridges.parity', 'Parity')}</th><th>Stop</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
                 : `<p>${t('bridges.noAdapters', 'No adapter plugged in on that host.')}</p>`}`);
    },

    async bridgeSetSerial(id, key, i) {
        const t = (k, d, p) => this.t(k, d, p);
        const q = c => document.querySelector(`#bridgeInfoBody .${c}[data-i="${i}"]`)?.value;
        const r = await fetch(`/api/bridges/${encodeURIComponent(id)}/ports/${encodeURIComponent(key)}/serial`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ baud: +q('br-baud'), parity: q('br-par'), stopbits: +q('br-stop'), databits: 8 }) });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { this.showToast('error', t('bridges.serialFail', 'Not applied'), (d.detail?.errors || [r.statusText]).join(' ')); return; }
        this.showToast('success', t('bridges.serialDone', 'Bus settings applied'), d.serial_text || '');
        this.renderDevicesList();
    },

    async bridgeProbe(id, port) {
        const t = (k, d, p) => this.t(k, d, p);
        const unit = prompt(t('bridges.probeUnit', 'Unit id of a slave on this bus:'), '1');
        if (unit === null) return;
        this._bridgeInfo(t('bridges.probeTitle', 'Check mode'), `<p class="field-hint">${t('bridges.probing', 'Asking… (a converter that takes one client drops the gateway\'s connection for a moment)')}</p>`);
        const r = await fetch(`/api/bridges/${encodeURIComponent(id)}/probe`, { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ port, unit_id: +unit || 1 }) });
        const d = await r.json().catch(() => ({}));
        document.getElementById('bridgeInfoBody').innerHTML = `
          <p><b>${this._esc(d.verdict || d.error || r.statusText)}</b></p>
          <table class="tplu-table"><tbody>
            <tr><td>RTU</td><td>${this._esc(d.rtu || '—')}</td></tr>
            <tr><td>Modbus TCP</td><td>${this._esc(d.modbus_tcp || '—')}</td></tr>
          </tbody></table>`;
    },

    addDeviceOnBus(bridgeId, port) {
        this.closeModal?.('bridgeInfoModal');
        const used = ((this._bridges || []).find(b => b.id === bridgeId)?.ports || [])
            .find(p => +p.port === +port)?.devices?.map(d => +d.unit_id) || [];
        let unit = 1; while (used.includes(unit)) unit++;
        this.openDeviceWizard(null, { protocol: 'rtu-tcp', bridge: bridgeId, bridge_port: +port, unit_id: unit });
    },
});
