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

    // the bus monitor (Diagnostics), showing only this bus's traffic
    openBusMonitor(bus) {
        this._diagBus = bus;
        const sel = document.getElementById('diagBusFilter');
        if (sel) sel.value = bus;
        this.navigateTo('diagnostics');
    },

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
        const bar = `<div class="bridges-bar"><span class="field-hint"><i aria-hidden="true" class="bi bi-hdd-network"></i> ${t('bridges.title', 'Bridges')}</span>
            <a class="btn btn-ghost btn-sm" href="/api/bridges/export" download><i aria-hidden="true" class="bi bi-download"></i> ${t('bridges.export', 'Export')}</a>
            <button data-admin class="btn btn-ghost btn-sm" ${this._act('openBridgesImport', [])}><i aria-hidden="true" class="bi bi-upload"></i> ${t('bridges.import', 'Import')}</button>
            <button data-admin class="btn btn-ghost btn-sm" ${this._act('openBridgesDiscover', [])}><i aria-hidden="true" class="bi bi-radar"></i> ${t('bridges.find', 'Find on the LAN')}</button></div>`;
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
                    ${p.busy ? `<span class="sink-pill ${p.busy.level === 'full' ? 'bad' : p.busy.level === 'high' ? 'warn' : 'ok'}" title="${this._esc(this._busyTitle(p))}">${t('bridges.busy', 'bus {pct}% busy', { pct: p.busy.pct })}</span>` : ''}
                    ${p.tapped ? `<span class="sink-pill ${p.wire?.error ? 'bad' : 'ok'}" title="${this._esc(p.wire?.error || t('bridges.tappedHint', 'Another master polls this bus; the gateway only listens. Scan, Check mode and polled devices are off here — they would transmit.'))}"><i aria-hidden="true" class="bi bi-ear"></i> ${p.wire?.error ? this._esc(t('bridges.tapLost', 'listening — link lost')) : this._esc(t('bridges.tapped', 'listening · {frames} frames · {crc} CRC errors', { frames: p.wire?.frames ?? 0, crc: p.wire?.crc_errors ?? 0 }))}</span>` : ''}
                    ${p.tapped ? '' : `<span class="bridge-port-actions">
                      <button data-admin class="btn btn-ghost btn-sm" ${this._act('busScan', [b.id, p.port])} title="${this._esc(t('bridges.scanBusHint', 'Ask every unit id on this bus who answers, and what it probably is'))}"><i aria-hidden="true" class="bi bi-search"></i> ${t('bridges.scanBus', 'Scan')}</button>
                      <button data-admin class="btn btn-ghost btn-sm" ${this._act('addDeviceOnBus', [b.id, p.port])} title="${this._esc(t('bridges.addDeviceHint', 'Add a device (a slave) on this bus'))}"><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('bridges.addDevice', 'Device')}</button>
                      <button data-admin class="btn btn-ghost btn-sm" ${this._act('bridgeProbe', [b.id, p.port])} title="${this._esc(t('bridges.probeHint', 'Ask a slave on this bus one register, as RTU and as Modbus TCP — tells whether the box is transparent or a gateway'))}"><i aria-hidden="true" class="bi bi-activity"></i> ${t('bridges.probe', 'Check mode')}</button>
                      <button class="btn btn-ghost btn-sm" ${this._act('openBusMonitor', [`${b.host}:${p.port}`])} title="${this._esc(t('bridges.monitorHint', 'Diagnostics, this bus only: every question and answer on the wire, and the register probe for any unit on it'))}"><i aria-hidden="true" class="bi bi-reception-4"></i> ${t('bridges.monitor', 'Monitor')}</button>
                    </span>`}
                  </div>
                  ${(p.devices || []).map(d => `
                    <div class="bridge-dev" role="button" tabindex="0" ${this._act(d.endpoint_id ? 'openEndpointDetail' : 'openDeviceDetail', [d.endpoint_id || d.id])}>
                      <span class="status-dot" style="--dot:${healthColor[d.health] || healthColor.idle}"></span>
                      <span class="dev-chip">unit ${this._esc(d.unit_id)}</span>
                      ${d.tap ? `<span class="dev-chip" title="${this._esc(t('bridges.tapDevHint', 'listen-only'))}"><i aria-hidden="true" class="bi bi-ear"></i></span>` : ''}
                      <span>${this._esc(d.name || d.id)}</span>
                      ${d.endpoint_id ? `<span class="field-hint">${t('bridges.inInstallation', 'in an installation')}</span>` : ''}
                      ${!d.endpoint_id ? `<button data-admin class="btn btn-ghost btn-sm bridge-move" ${this._act('openDeviceWizard', [d.id])} title="${this._esc(t('bridges.moveHint', 'Edit — or move it to another bus or bridge; its topics and history stay'))}" aria-label="${this._esc(t('common.edit', 'Edit'))}"><i aria-hidden="true" class="bi bi-arrow-left-right"></i></button>` : ''}
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
        }).join('').replace(/^/, bar);
    },

    _busyTitle(p) {
        const t = (k, d, x) => this.t(k, d, x);
        const lines = (p.devices || []).filter(d => d.bus_share != null)
            .map(d => `${d.name || d.id}: ${Math.round(d.bus_share * 100)}%`);
        const advice = p.busy.level === 'full'
            ? t('bridges.busyFull', 'The bus cannot keep up: requests queue and readings age. Slow the poll groups that need no speed, raise the baud rate (every slave and the bus), or split the slaves over two buses.')
            : p.busy.level === 'high' ? t('bridges.busyHigh', 'Close to full: another device or a faster poll group will make readings late.') : '';
        return [t('bridges.busyLead', 'Measured share of the wire, per device:'), ...lines, advice].filter(Boolean).join('\n');
    },

    // ── scan a bus ──────────────────────────────────────────────────────────
    async busScan(bridgeId, port) {
        const t = (k, d, x) => this.t(k, d, x);
        this._bridgeInfo(t('bridges.scanBusTitle', 'Scan bus :{port}', { port }), `
          <p class="field-hint">${t('bridges.scanBusLead', 'Asks unit ids 1–247 one by one through the bus\'s own connection — the devices on it keep reading in between. About a minute and a quarter for the whole range.')}</p>
          <div class="bus-scan-bar"><div id="busScanFill"></div></div>
          <p class="field-hint" id="busScanState">${t('bridges.scanStarting', 'starting…')}</p>
          <div id="busScanResult"></div>`);
        const r = await fetch(`/api/bridges/${encodeURIComponent(bridgeId)}/ports/${port}/scan`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { document.getElementById('busScanState').textContent = (d.detail?.errors || [r.statusText]).join(' '); return; }
        this._busScanJob = d.job;
        const tick = async () => {
            // the panel was closed or another scan started: stop this one on the
            // server too — a scan left running keeps asking the bus
            if (this._busScanJob !== d.job || !document.querySelector('#bridgeInfoModal.active #busScanFill')) {
                fetch(`/api/bus-scan/${d.job}`, { method: 'DELETE' }).catch(() => {});
                return;
            }
            const st = await (await fetch(`/api/bus-scan/${d.job}`)).json();
            document.getElementById('busScanFill').style.width = `${Math.round(100 * st.done / st.total)}%`;
            document.getElementById('busScanState').textContent = st.state === 'running'
                ? t('bridges.scanProgress', '{done}/{total} asked · {n} answered', { done: st.done, total: st.total, n: st.found.length })
                : st.state === 'failed' ? `${t('bridges.scanFailed', 'Scan failed')}: ${st.error}`
                : t('bridges.scanDone', 'Done — {n} slave(s) answered.', { n: st.found.length });
            document.getElementById('busScanResult').innerHTML = this._busScanTable(bridgeId, port, st.found);
            if (st.state === 'running') setTimeout(tick, 1000);
        };
        tick();
    },

    _busScanTable(bridgeId, port, found) {
        const t = (k, d, x) => this.t(k, d, x);
        if (!found.length) return '';
        return `<table class="tplu-table"><thead><tr><th>Unit</th><th>${t('bridges.scanWhat', 'What it is')}</th><th></th></tr></thead><tbody>
          ${found.map(f => {
              const what = f.matches.length ? f.matches.map(m => this._esc(m.name)).join(' / ')
                  : f.fc43 && (f.fc43.vendor || f.fc43.product) ? this._esc(`${f.fc43.vendor || ''} ${f.fc43.product || ''}`.trim())
                  : f.sunspec ? 'SunSpec' : `<span class="field-hint">${t('bridges.scanUnknown', 'not recognised — pick a template')}</span>`;
              const act = f.device ? `<span class="field-hint">${t('bridges.scanKnown', 'already: {id}', { id: this._esc(f.device) })}</span>`
                  : `<button data-admin class="btn btn-secondary btn-sm" ${this._act('addScannedDevice', [bridgeId, port, f.unit_id, f.suggested || ''])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('bridges.scanAdd', 'Add')}</button>`;
              return `<tr><td><code>${f.unit_id}</code></td><td>${what}</td><td>${act}</td></tr>`;
          }).join('')}</tbody></table>`;
    },

    addScannedDevice(bridgeId, port, unit, template) {
        if (this._busScanJob) fetch(`/api/bus-scan/${this._busScanJob}`, { method: 'DELETE' }).catch(() => {});
        this._busScanJob = null;
        this.closeModal('bridgeInfoModal');
        this.openDeviceWizard(null, { protocol: 'rtu-tcp', bridge: bridgeId, bridge_port: +port,
                                      unit_id: +unit, ...(template ? { template } : {}) });
    },

    // ── find bridges on the LAN ─────────────────────────────────────────────
    openBridgesDiscover() {
        const t = (k, d, x) => this.t(k, d, x);
        this.closeModal?.('bridgeModal');
        // a starting guess: the /24 of a bridge or a device we already know
        const ip = [...(this._bridges || []).map(b => b.host), ...(this._devices || []).map(d => d.host || d.connection?.host)]
            .find(h => /^\d+\.\d+\.\d+\.\d+$/.test(h || ''));
        this._bridgeInfo(t('bridges.findTitle', 'Find bridges on the LAN'), `
          <p class="field-hint">${t('bridges.findLead2', 'Looks at every address of the range for the ports each kind of bridge declares: our serial bridge answers its /health; a converter only has an open port, so one register is asked of a unit, as RTU and as Modbus TCP, to tell transparent from gateway. Bridges already added are not knocked on.')}</p>
          <div class="form-row" style="align-items:end;">
            <div class="form-group flex-2"><label class="form-label" for="brFindCidr">${t('bridges.findRange', 'Range')}</label>
              <input class="input" id="brFindCidr" value="${this._esc(ip ? ip.split('.').slice(0, 3).join('.') + '.0/24' : '')}" placeholder="192.168.1.0/24"></div>
            <div class="form-group"><label class="form-label" for="brFindUnit">${t('bridges.findUnit', 'Ask unit')}</label>
              <input class="input" id="brFindUnit" type="number" min="1" max="247" value="1"></div>
            <div class="form-group"><button class="btn btn-primary btn-sm" id="brFindBtn" ${this._act('bridgesDiscoverRun', [])}><i aria-hidden="true" class="bi bi-radar"></i> ${t('bridges.findGo', 'Search')}</button></div>
          </div>
          <div id="brFindResult" role="status"></div>`);
    },

    async bridgesDiscoverRun() {
        const t = (k, d, x) => this.t(k, d, x);
        const box = document.getElementById('brFindResult');
        const btn = document.getElementById('brFindBtn');
        btn.disabled = true;
        box.innerHTML = `<div class="field-hint"><span class="btn-spinner"></span> ${t('bridges.finding', 'Searching… a /24 takes a few seconds.')}</div>`;
        try {
            const r = await fetch('/api/bridges/discover', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ cidr: document.getElementById('brFindCidr').value.trim(),
                                       unit_id: +document.getElementById('brFindUnit').value || 1 }) });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error((d.detail?.errors || [d.detail || r.statusText]).join(' '));
            this._brFound = d.found || [];
            if (!this._brFound.length) {
                box.innerHTML = `<div class="field-hint">${t('bridges.findNone', 'Nothing found in {n} addresses. A converter may use another port: add it by hand with the port from its web page.', { n: d.scanned })}</div>`;
                return;
            }
            box.innerHTML = `<table class="tplu-table"><tbody>${this._brFound.map((x, i) => `<tr>
                <td><code>${this._esc(x.host)}${x.port ? ':' + this._esc(x.port) : ''}</code></td>
                <td>${x.bridge ? `<span class="sink-pill ok">${this._esc(t('bridges.findKnown', 'added as {id}', { id: x.bridge }))}</span>`
                    : x.type ? `<b>${this._esc(x.type_name || x.type)}</b>${x.version ? ' · ' + this._esc(x.version) : ''}`
                    : `<span class="sink-pill warn">${this._esc(t('bridges.findMaybe', 'a converter?'))}</span>`}</td>
                <td class="field-hint">${this._esc(x.detail || '')}</td>
                <td>${x.bridge ? '' : `<button data-admin class="btn btn-ghost btn-sm" ${this._act('addFoundBridge', [i])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('bridges.add', 'Add bridge')}</button>`}</td>
              </tr>`).join('')}</tbody></table>`;
        } catch (e) {
            box.innerHTML = `<div class="field-error">${this._esc(String(e.message || e))}</div>`;
        } finally {
            btn.disabled = false;
        }
    },

    addFoundBridge(i) {
        const x = (this._brFound || [])[i];
        if (!x) return;
        const type = x.type || (x.candidates || [])[0] || 'rtu_transparent';
        const ty = this._bridgeType(type);
        const preset = { type, host: x.host, name: x.host };
        if (ty.discovery === 'api') preset.control_port = x.port;
        else preset.ports = [{ port: x.port, label: '' }];
        this.closeModal('bridgeInfoModal');
        this.openBridgeModal(null, preset);
    },

    // ── import bridges ──────────────────────────────────────────────────────
    openBridgesImport() {
        const t = (k, d, x) => this.t(k, d, x);
        this._bridgeInfo(t('bridges.importTitle', 'Import bridges'), `
          <p class="field-hint">${t('bridges.importLead', 'A file exported from a gateway (Devices → Bridges → Export). Each bridge is checked first; tokens travel only in an admin\'s export.')}</p>
          <textarea id="brImportText" class="input" rows="9" spellcheck="false" style="font-family:var(--font-mono,monospace);font-size:12px;"></textarea>
          <label class="checkbox-label"><input type="checkbox" id="brImportReplace"> <span>${t('bridges.importReplace', 'Replace bridges that already exist here')}</span></label>
          <div style="display:flex;gap:8px;margin-top:8px;">
            <button class="btn btn-secondary btn-sm" ${this._act('bridgesImportRun', [false])}>${t('rules.import.check', 'Check')}</button>
            <button data-admin class="btn btn-primary btn-sm" ${this._act('bridgesImportRun', [true])}>${t('rules.import', 'Import')}</button></div>
          <div id="brImportResult" style="margin-top:10px;"></div>`);
    },

    async bridgesImportRun(apply) {
        const t = (k, d, x) => this.t(k, d, x);
        const r = await fetch('/api/bridges/import', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ yaml: document.getElementById('brImportText').value, apply,
                                   replace: document.getElementById('brImportReplace').checked }) });
        const d = await r.json().catch(() => ({}));
        const box = document.getElementById('brImportResult');
        if (!r.ok) { box.innerHTML = `<div class="field-error">${this._esc((d.detail?.errors || [r.statusText]).join(' '))}</div>`; return; }
        box.innerHTML = `<table class="tplu-table"><tbody>${d.bridges.map(x => `<tr><td><code>${this._esc(x.id)}</code> ${this._esc(x.name || '')}</td>
            <td><span class="sink-pill ${x.status === 'invalid' ? 'bad' : x.status === 'exists' ? 'warn' : 'ok'}">${this._esc(x.status)}</span></td>
            <td class="field-hint">${(x.errors || []).map(e => this._esc(e)).join('<br>')}</td></tr>`).join('')}</tbody></table>`;
        if (apply && d.ok) { this.showToast('success', t('bridges.imported', 'Bridges imported'), `${d.ok}/${d.total}`); this.renderDevicesList(); }
    },

    // ── add / edit ──────────────────────────────────────────────────────────
    async openBridgeModal(id = null, preset = null) {
        await this._loadBridges();
        const t = (k, d, p) => this.t(k, d, p);
        const b = id ? (this._bridges || []).find(x => x.id === id) : null;
        this._bridgeEdit = { id, data: b ? JSON.parse(JSON.stringify(b)) : {
            id: '', name: '', type: (this._bridgeTypes[0] || {}).id || 'rtu_transparent', host: '',
            ports: [], max_connections: 1, ...(preset || {}) } };
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
          ${e.id ? '' : `<div class="field-hint" style="margin:-4px 0 8px;"><a href="#" ${this._act('openBridgesDiscover', [])}><i aria-hidden="true" class="bi bi-radar"></i> ${t('bridges.findLead', 'Not sure of its IP or port? Find bridges on the LAN')}</a></div>`}
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
        this.openDeviceWizard(null, { protocol: 'rtu-tcp', bridge: bridgeId, bridge_port: +port, unit_id: unit });   // a gateway's bus opens as TCP
    },
});
