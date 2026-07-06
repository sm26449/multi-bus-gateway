// devices domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    // Devices page: show the list, hide the detail + register-editor overlays.
    _showDevicesList() {
        const list = document.getElementById('devicesListView');
        const detail = document.getElementById('deviceDetailView');
        const reg = document.getElementById('deviceRegistersView');
        if (list) list.style.display = '';
        if (detail) detail.style.display = 'none';
        if (reg) reg.style.display = 'none';
    },

    // Close the per-device register editor and return to the Devices list.
    closeDeviceRegisters() {
        const reg = document.getElementById('deviceRegistersView');
        if (reg) reg.style.display = 'none';
        this._maybeResetRegDevice();
        this._showDevicesList();
    },

    // ── Shared "viewing device" selector (Monitor / History / Energy) ─────
    // One selection across the three data pages; shown only when there is more
    // than one device (no clutter with a single meter).
    _viewDeviceId() {
        return this._viewDevice || this._primaryDeviceId();
    },

    _viewDeviceQS(sep = '?') {
        const id = this._viewDeviceId();
        return (id && id !== this._primaryDeviceId()) ? `${sep}device=${encodeURIComponent(id)}` : '';
    },

    async _renderViewDeviceSelector(selId, onChange) {
        const devices = await this._fetchDevices();
        const sel = document.getElementById(selId);
        if (!sel) return;
        if (!this._viewDevice) this._viewDevice = this._primaryDeviceId();
        if (devices.length <= 1) { sel.style.display = 'none'; this._viewDevice = this._primaryDeviceId(); return; }
        sel.style.display = '';
        const cur = this._viewDeviceId();
        sel.innerHTML = devices.map(d =>
            `<option value="${this._esc(d.id)}" ${d.id === cur ? 'selected' : ''}>${this._esc(d.name || d.id)}</option>`).join('');
        sel.onchange = () => {
            this._viewDevice = sel.value;
            if (onChange) onChange();
        };
    },

    async _fetchDevices(force = false) {
        if (this._devices && !force) return this._devices;
        try {
            const r = await fetch('/api/devices');
            this._devices = (await r.json()).devices || [];
        } catch (e) { console.error('devices load failed:', e); this._devices = this._devices || []; }
        return this._devices;
    },

    async renderDevicesList() {
        const el = document.getElementById('devicesList');
        if (!el) return;
        const devices = await this._fetchDevices(true);
        this._renderRegDeviceSelectors();
        if (!devices.length) {
            el.innerHTML = `<span class="field-hint">${this.t('devices.none', 'No devices configured.')}</span>`;
            return;
        }
        const healthColor = { ok: 'var(--success,#22c55e)', degraded: 'var(--warning,#f59e0b)',
                              down: 'var(--danger,#ef4444)', idle: 'var(--text-secondary,var(--text-secondary))' };
        el.innerHTML = devices.map(d => {
            const proto = d.protocol === 'rtu'
                ? `RTU · ${this._esc(d.serial?.serial_port || '—')}`
                : d.protocol === 'http'
                ? `HTTP · ${this._esc((d.connection?.url || d.http_url || '').replace(/^https?:\/\//, '').split('/')[0] || '—')}`
                : d.protocol === 'mqtt'
                ? `MQTT · ${this._esc(d.mqtt_in_broker || d.connection?.broker || '—')} · ${this._esc(d.mqtt_in_topic || d.connection?.topic || '')}`
                : `TCP · ${this._esc(d.host || '')}:${d.port}`;
            const stats = d.connected
                ? `${(d.poll_rate ?? 0).toFixed ? (d.poll_rate ?? 0).toFixed(1) : d.poll_rate} poll/s · ` +
                  `${d.staleness_age_s != null ? d.staleness_age_s + 's' : '—'} ${this.t('devices.age', 'age')}`
                : this.t('devices.notConnected', 'not connected');
            const actions = [
                `<button class="btn btn-ghost btn-sm" ${this._act('jumpToDeviceRegisters', [d.id])} title="${this.t('devices.registers', 'Measurements')}"><i class="bi bi-list-check"></i></button>`,
                `<button class="btn btn-ghost btn-sm" ${this._act('testDevice', [d.id], {el: true})} title="${this.t('devices.test', 'Test read')}"><i class="bi bi-activity"></i></button>`,
            ];
            actions.push(`<button class="btn btn-ghost btn-sm" ${this._act('openDeviceDetail', [d.id])} title="${this.t('common.edit', 'Edit')}"><i class="bi bi-pencil"></i></button>`);
            if (!d.primary) {
                actions.push(`<button class="btn btn-ghost btn-sm" ${this._act('deleteDevice', [d.id])} title="${this.t('common.delete', 'Delete')}"><i class="bi bi-trash"></i></button>`);
            }
            // The row (outside its action buttons) opens the full-page detail;
            // data-guard keeps clicks inside the action-buttons cell from bubbling
            // into the row action, data-key-enter keeps Enter working on the div.
            return `<div class="device-row" role="button" tabindex="0"
                 ${this._act('openDeviceDetail', [d.id], {guard: '.device-row-actions'})} data-key-enter>
                <span class="status-dot" style="--dot:${healthColor[d.data_health] || healthColor.idle}"
                      title="${this._esc(d.data_health || 'idle')}"></span>
                <div class="device-row-main">
                    <div class="device-row-title">${this._esc(d.name || d.id)}
                        <span class="dev-chip">${this._esc(d.id)}</span></div>
                    <div class="device-row-sub">${proto} · unit ${d.unit_id}
                        · ${this._esc(d.template || '—')} · ${d.selected_registers ?? 0} ${this.t('devices.regsSelected', 'measurements')}</div>
                    <div class="device-row-routing">→ MQTT <code>${this._esc(d.mqtt_topic_prefix || '')}/…</code>
                        &nbsp;→ InfluxDB <code>${this._esc(d.influxdb_bucket || '')}</code></div>
                </div>
                <div class="device-row-stats">${stats}</div>
                <div class="device-row-actions">${actions.join('')}</div>
            </div>`;
        }).join('');
    },

    // ── Device discovery (generic; Fronius Solar API is the first method) ──
    openDiscoverModal() {
        // Prefill the Fronius host from a Fronius-ish device, and the Modbus CIDR
        // from the primary device's /24 (a sensible starting guess).
        const fdev = (this._devices || []).find(d => (d.template || '').includes('fronius'));
        const furl = fdev?.connection?.url || fdev?.http?.url || '';
        const furlHost = furl ? furl.replace(/^https?:\/\//, '').split('/')[0].split(':')[0] : '';
        const hostInp = document.getElementById('discoverHost');
        if (hostInp) hostInp.value = fdev?.connection?.host || fdev?.host || furlHost || '';
        const primary = (this._devices || []).find(d => d.primary) || (this._devices || [])[0];
        const phost = primary?.connection?.host || primary?.host || '';
        const cidrInp = document.getElementById('discoverCidr');
        if (cidrInp && phost && /^\d+\.\d+\.\d+\.\d+$/.test(phost)) {
            cidrInp.value = phost.split('.').slice(0, 3).join('.') + '.0/24';
        }
        const res = document.getElementById('discoverResult');
        if (res) res.innerHTML = '';
        this._syncDiscoverMethod();
        this.openModal('discoverModal');
    },

    _syncDiscoverMethod() {
        const method = document.getElementById('discoverMethod')?.value || 'modbus';
        const fg = document.getElementById('discoverHostGroup');
        const mg = document.getElementById('discoverModbusGroup');
        if (fg) fg.style.display = method === 'fronius' ? '' : 'none';
        if (mg) mg.style.display = method === 'modbus' ? '' : 'none';
    },

    async runDiscover(btn) {
        const method = document.getElementById('discoverMethod')?.value || 'modbus';
        const box = document.getElementById('discoverResult');
        const busy = () => { if (box) box.innerHTML = `<div class="settings-card" style="padding:12px;color:var(--text-secondary);"><i class="bi bi-arrow-repeat spin"></i> ${this.t('devices.scanning', 'Scanning…')}</div>`; };
        const err = (m) => { if (box) box.innerHTML = `<div class="settings-card" style="padding:12px;color:var(--danger-text,#c0392b);">${this._esc(m)}</div>`; };
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="bi bi-arrow-repeat spin"></i>'; }
        try {
            if (method === 'modbus') {
                const cidr = (document.getElementById('discoverCidr')?.value || '').trim();
                if (!cidr) { err(this.t('devices.discoverNoCidr', 'Enter a network range (CIDR) to scan.')); return; }
                busy();
                const r = await fetch('/api/discover/modbus/scan', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        cidr,
                        port: parseInt(document.getElementById('discoverPort')?.value) || 502,
                        unit_id: parseInt(document.getElementById('discoverUnit')?.value) || 1,
                    }) });
                const d = await r.json();
                if (!r.ok) { err(d.detail?.errors?.join(' · ') || 'scan failed'); return; }
                this._renderModbusScan(d);
            } else {
                const host = (document.getElementById('discoverHost')?.value || '').trim();
                if (!host) { err(this.t('devices.discoverNoHost', 'Enter a host / IP to scan.')); return; }
                busy();
                const r = await fetch('/api/fronius/discover?host=' + encodeURIComponent(host));
                const d = await r.json();
                if (!r.ok) { err(d.detail || 'discovery failed'); return; }
                this._renderDiscoverResult(d, host);
            }
        } catch (e) { err(e.message); }
        finally { if (btn) { btn.disabled = false; btn.innerHTML = orig; } }
    },

    _renderModbusScan(d) {
        const box = document.getElementById('discoverResult');
        if (!box) return;
        const results = d.results || [];
        if (!results.length) {
            box.innerHTML = `<div class="settings-card" style="padding:12px;color:var(--text-secondary);">${this.t('devices.scanNone', 'No devices answered on')} ${d.scanned} ${this.t('devices.scanHosts', 'hosts.')}</div>`;
            return;
        }
        const rows = results.map(r => {
            const badge = r.modbus
                ? `<span class="sink-pill ok">Modbus</span>`
                : `<span class="sink-pill warn">${this.t('devices.portOpen', 'port open')}</span>`;
            return `<tr>
                <td style="padding:4px 12px 4px 0;font-variant-numeric:tabular-nums;"><b>${this._esc(r.host)}</b>:${r.port}</td>
                <td style="padding:4px 12px 4px 0;">${badge}</td>
                <td style="padding:4px 0;white-space:nowrap;">
                    <button class="btn btn-ghost btn-sm" ${this._act('sweepModbusUnits', [r.host, r.port], {el: true})} title="${this.t('devices.findUnits', 'Find unit ids')}"><i class="bi bi-diagram-3"></i></button>
                    <button class="btn btn-ghost btn-sm" ${this._act('useDiscoveredModbus', [r.host, r.port, r.unit_id])}>${this.t('devices.add', 'Add Device')}</button>
                </td></tr>
                <tr><td colspan="3" style="padding:0;"><div class="disc-units" id="discUnits-${this._esc(r.host).replace(/\./g, '_')}"></div></td></tr>`;
        }).join('');
        box.innerHTML = `<div class="settings-card" style="padding:14px;">
            <div style="margin-bottom:8px;"><b><i class="bi bi-hdd-network"></i> ${results.length} ${this.t('devices.scanFound', 'device(s) found')}</b> <span style="color:var(--text-secondary);">· ${d.scanned} ${this.t('devices.scanScanned', 'scanned')}</span></div>
            <table>${rows}</table></div>`;
    },

    async sweepModbusUnits(host, port, btn) {
        const cell = document.getElementById('discUnits-' + host.replace(/\./g, '_'));
        if (cell) cell.innerHTML = `<span class="field-hint"><i class="bi bi-arrow-repeat spin"></i> ${this.t('devices.scanning', 'Scanning…')}</span>`;
        try {
            const r = await fetch('/api/discover/modbus/units', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ protocol: 'tcp', host, port, unit_start: 1, unit_end: 32 }) });
            const d = await r.json();
            const units = d.units || [];
            if (!cell) return;
            cell.innerHTML = units.length
                ? `<span class="field-hint">${this.t('devices.unitsFound', 'Unit ids:')}</span> ` + units.map(u =>
                    `<button class="calc-chip" ${this._act('useDiscoveredModbus', [host, port, u])}>${u}</button>`).join(' ')
                : `<span class="field-hint">${this.t('devices.noUnits', 'No unit ids answered (1–32).')}</span>`;
        } catch (e) { if (cell) cell.innerHTML = `<span class="field-hint">${this._esc(e.message)}</span>`; }
    },

    useDiscoveredModbus(host, port, unit) {
        this.closeModal('discoverModal');
        const existing = new Set((this._devices || []).map(d => d.id));
        let id = ('meter-' + host.split('.').pop() + (unit ? '-u' + unit : '')).replace(/[^a-z0-9_-]/g, '-');
        if (existing.has(id)) { let n = 2; while (existing.has(`${id}-${n}`)) n++; id = `${id}-${n}`; }
        this.openDeviceWizard(null, {
            id, name: `Modbus device ${host}`,
            protocol: 'tcp', host, port: parseInt(port) || 502, unit_id: parseInt(unit) || 1,
        });
    },

    _renderDiscoverResult(d, host) {
        const box = document.getElementById('discoverResult');
        if (!box) return;
        // Stash for the per-row "Add Device" prefill (index → meter).
        this._discover = { host, meters: d.meters || [] };
        const meterRows = (d.meters || []).map((m, i) => `<tr>
            <td style="padding:3px 12px 3px 0;"><b>${this._esc(m.model || 'meter')}</b></td>
            <td style="padding:3px 12px 3px 0;color:var(--text-secondary);">Solar-API id ${this._esc(m.solar_api_id)} · Modbus unit ${m.modbus_unit_hint}</td>
            <td style="padding:3px 12px 3px 0;font-variant-numeric:tabular-nums;">${m.power_w != null ? Math.round(m.power_w) + ' W' : '—'} · ${m.freq_hz != null ? m.freq_hz + ' Hz' : ''}</td>
            <td style="padding:3px 0;"><button class="btn btn-ghost btn-sm" ${this._act('addDiscoveredMeter', [i])}>${this.t('devices.add', 'Add Device')}</button></td></tr>`).join('');
        const invRows = (d.inverters || []).map(i =>
            `<tr><td style="padding:2px 12px 2px 0;">${this.t('lbl.inverter', 'Inverter')}</td><td style="padding:2px 12px 2px 0;color:var(--text-secondary);">id ${this._esc(i.solar_api_id)} · DT ${i.dt} · SN ${this._esc(i.serial || '')}</td></tr>`).join('');
        box.innerHTML = `<div class="settings-card" style="padding:14px;">
            <div style="margin-bottom:8px;"><b><i class="bi bi-hdd-network"></i> ${this.t('devices.discoverTitle', 'Devices behind')} ${this._esc(host)}</b></div>
            ${meterRows ? `<div style="color:var(--text-secondary);font-size:12px;margin:6px 0 2px;">${this.t('lbl.meters', "Meters")}</div><table>${meterRows}</table>` : `<div style="color:var(--text-secondary);">${this.t('msg.noMeters', "No meters reported.")}</div>`}
            ${invRows ? `<div style="color:var(--text-secondary);font-size:12px;margin:10px 0 2px;">${this.t('lbl.inverters', "Inverters")}</div><table>${invRows}</table>` : ''}
            <p class="field-hint" style="margin-top:10px;">${this._esc(d.modbus_note || '')}</p>
        </div>`;
    },

    // Open the Add Device wizard prefilled from a discovered meter. Discovery is
    // over the Fronius Solar API, so the reliable path is an HTTP/JSON device on
    // that API (not the fragile Modbus-via-DataManager path) — that's what we seed.
    addDiscoveredMeter(idx) {
        const disc = this._discover || {};
        const m = (disc.meters || [])[idx];
        this.closeModal('discoverModal');
        if (!m || !disc.host) { this.openDeviceWizard(); return; }
        const sid = m.solar_api_id;
        const existing = new Set((this._devices || []).map(d => d.id));
        let id = ('fronius-meter-' + sid).toLowerCase().replace(/[^a-z0-9_-]/g, '-');
        if (existing.has(id)) { let n = 2; while (existing.has(`${id}-${n}`)) n++; id = `${id}-${n}`; }
        this.openDeviceWizard(null, {
            id,
            name: (m.model || 'Fronius Meter') + ' (Solar API)',
            protocol: 'http',
            template: 'fronius_solar_api',
            url: `http://${disc.host}/solar_api/v1/GetMeterRealtimeData.cgi?Scope=Device&DeviceId=${encodeURIComponent(sid)}`,
        });
    },

    // ── Full-page device detail (edit) ────────────────────────────────────
    // Opens a device as a full-page form inside the Devices tab (Connection /
    // Template / Data routing / Registers). The primary device (UMG512) keeps
    // its published identity: id, topic, bucket and Influx tag are read-only so
    // existing history and Home Assistant entities never break.
    async openDeviceDetail(id) {
        if (this.currentPage !== 'devices') this.navigateTo('devices');
        this._showDevicesList();
        await this._fetchDevices(true);
        const dev = (this._devices || []).find(d => d.id === id);
        if (!dev) { this.showToast('error', this.t('devices.notFound', 'Device not found'), id); return; }
        let templates = [];
        try { templates = (await (await fetch('/api/device-templates')).json()).templates || []; }
        catch (e) { console.error(e); }
        const c = dev.connection || {};
        this._devDetail = {
            id, primary: !!dev.primary, templates,
            data: {
                name: dev.name || id, template: dev.template || '',
                enabled: dev.enabled !== false,
                protocol: dev.protocol || 'tcp',
                host: c.host || dev.host || '', port: c.port || dev.port || 502,
                unit_id: c.unit_id ?? dev.unit_id ?? 1, timeout: c.timeout ?? 3,
                serial_port: c.serial_port || dev.serial?.serial_port || '',
                baudrate: c.baudrate || dev.serial?.baudrate || 9600,
                parity: c.parity || dev.serial?.parity || 'N',
                stopbits: c.stopbits || dev.serial?.stopbits || 1,
                url: c.url || dev.http?.url || '',
                broker: c.broker || '', mqtt_port: c.port || 1883,
                topic: c.topic || dev.mqtt_in_topic || '',
                mqtt_username: c.username || '', mqtt_password: '',
                mqtt_tls: !!c.tls,
                topic_prefix: dev.mqtt_topic_prefix || '',
                bucket: dev.influxdb_bucket || '',
                device_tag: dev.influxdb_device_tag || id,
                ha_discovery_enabled: dev.ha_discovery_enabled !== false,
                mqtt_enabled: dev.mqtt_enabled !== false,
                influxdb_enabled: dev.influxdb_enabled !== false,
                http_output_enabled: dev.http_output_enabled === true,
                selected_registers: dev.selected_registers ?? 0,
            },
            sinks: dev.sinks || {},
            entry: dev,                 // full live entry for the Overview tab
        };
        document.getElementById('devicesListView').style.display = 'none';
        const view = document.getElementById('deviceDetailView');
        view.style.display = '';
        view.innerHTML = this._deviceDetailHtml();
        this._wireDeviceDetail();
    },

    closeDeviceDetail() {
        this._restoreWsPages();       // return embedded pages home BEFORE wiping the view
        const view = document.getElementById('deviceDetailView');
        if (view) { view.style.display = 'none'; view.innerHTML = ''; }
        const list = document.getElementById('devicesListView');
        if (list) list.style.display = '';
        this.renderDevicesList();
    },

    _deviceDetailHtml() {
        const s = this._devDetail, d = s.data;
        const primary = s.primary;
        const tcp = d.protocol !== 'rtu';
        const tplOptions = [`<option value="">${this.t('devices.detail.noTemplate', '(no template)')}</option>`]
            .concat(s.templates.map(t =>
                `<option value="${this._esc(t.id)}" ${t.id === d.template ? 'selected' : ''}>${this._esc(t.name)} · ${t.registers} measurements</option>`)).join('');
        const t = this.t.bind(this);
        const gMon = d.enabled ? '' : 'disabled title="Enable polling to view live values"';
        const gInf = d.influxdb_enabled ? '' : 'disabled title="Enable the InfluxDB output"';
        return `
        <div class="section-header">
            <h2><button class="btn btn-ghost btn-sm" onclick="app.closeDeviceDetail()" aria-label="${t('common.back', 'Back')}"><i class="bi bi-arrow-left"></i></button>
                <i class="bi bi-cpu"></i> ${this._esc(d.name)}
                <span class="dev-chip">${this._esc(s.id)}</span></h2>
            <div class="header-actions">
                ${primary ? '' : `<button class="btn btn-ghost btn-sm" ${this._act('deleteDevice', [s.id])}><i class="bi bi-trash"></i> ${t('common.delete', 'Delete')}</button>`}
            </div>
        </div>

        <!-- device workspace: every tab opens IN-PLACE — the user never leaves
             the device (Measurements/Monitor/History/Energy are embedded, scoped
             to this device, with no device selector). -->
        <div class="config-main-tabs" id="deviceWsTabs" style="margin-bottom:14px;">
            <button class="config-main-tab active" data-dtab="overview"><i class="bi bi-grid-1x2"></i> ${t('devices.tab.overview', 'Overview')}</button>
            <button class="config-main-tab" data-dtab="edit"><i class="bi bi-pencil-square"></i> ${t('devices.tab.edit', 'Edit')}</button>
            <button class="config-main-tab" data-dtab="outputs"><i class="bi bi-signpost-split"></i> ${t('devices.detail.outputs', 'Outputs')}</button>
            <button class="config-main-tab" data-dtab="measurements"><i class="bi bi-list-check"></i> ${t('devices.registers', 'Measurements')} (${d.selected_registers})</button>
            <button class="config-main-tab" data-dtab="calculated"><i class="bi bi-calculator"></i> ${t('calc.tab', 'Calculated')}</button>
            <button class="config-main-tab" data-dtab="monitor" ${gMon}><i class="bi bi-graph-up"></i> ${t('nav.monitor', 'Monitor')}</button>
            <button class="config-main-tab" data-dtab="history" ${gInf}><i class="bi bi-clock-history"></i> ${t('nav.history', 'History')}</button>
            <button class="config-main-tab" data-dtab="energy" ${gInf}><i class="bi bi-lightning-charge"></i> ${t('nav.energy', 'Energy')}</button>
        </div>

        <!-- host panels for the embedded, device-scoped views -->
        <div data-dpanel="measurements" class="ws-embed-host" hidden></div>
        <div data-dpanel="monitor" class="ws-embed-host" hidden></div>
        <div data-dpanel="history" class="ws-embed-host" hidden></div>
        <div data-dpanel="energy" class="ws-embed-host" hidden></div>

        <!-- ── Calculated (formula-derived measurements) ── -->
        <div data-dpanel="calculated" hidden></div>

        <!-- ── Overview (read-only) ── -->
        <div data-dpanel="overview">${this._deviceOverviewHtml(d, s.entry || {})}</div>

        <!-- ── Edit ── -->
        <div data-dpanel="edit" hidden>
        <div class="settings-card">
            <div class="settings-card-header"><h3><i class="bi bi-ethernet"></i> ${t('devices.detail.connection', 'Connection')}</h3></div>
            <div class="settings-card-body">
                <div class="form-group" style="margin-bottom:10px;">
                    <label class="form-label">${t('devices.wizard.protocol', 'Protocol')}</label>
                    <div style="display:flex;gap:14px;flex-wrap:wrap;">
                        <label><input type="radio" name="ddvProto" value="tcp" ${d.protocol === 'tcp' ? 'checked' : ''} disabled> Modbus TCP</label>
                        <label style="opacity:.55;"><input type="radio" name="ddvProto" value="rtu" ${d.protocol === 'rtu' ? 'checked' : ''} disabled> Modbus RTU</label>
                        <label><input type="radio" name="ddvProto" value="http" ${d.protocol === 'http' ? 'checked' : ''} disabled> HTTP / JSON</label>
                        <label><input type="radio" name="ddvProto" value="mqtt" ${d.protocol === 'mqtt' ? 'checked' : ''} disabled> MQTT</label>
                    </div>
                    <div class="field-hint"><i class="bi bi-lock"></i> ${t('devices.wizard.protoLocked', 'Fixed after creation — the template map is transport-specific.')}</div>
                </div>
                <div id="ddvHttp" style="display:${d.protocol === 'http' ? '' : 'none'}">
                    <div class="form-group"><label class="form-label" for="ddvUrl">${t('devices.wizard.httpUrl', 'JSON endpoint URL')}</label>
                        <input type="text" id="ddvUrl" class="input" value="${this._esc(d.url || '')}" placeholder="http://192.168.1.50/rpc/Shelly.GetStatus"></div>
                </div>
                <div id="ddvTcp" style="display:${d.protocol === 'tcp' ? '' : 'none'}">
                    <div class="form-row">
                        <div class="form-group flex-2"><label class="form-label" for="ddvHost">${this.t('lbl.hostIp', "Host / IP")}</label>
                            <input type="text" id="ddvHost" class="input" value="${this._esc(d.host)}" placeholder="192.168.1.60"></div>
                        <div class="form-group"><label class="form-label" for="ddvPort">${this.t('lbl.port', "Port")}</label>
                            <input type="number" id="ddvPort" class="input" aria-label="Port" value="${d.port}" min="1" max="65535"></div>
                        <div class="form-group"><label class="form-label" for="ddvUnit">${this.t('lbl.unitId', "Unit ID")}</label>
                            <input type="number" id="ddvUnit" class="input" aria-label="Unit ID" value="${d.unit_id}" min="0" max="255"></div>
                        <div class="form-group"><label class="form-label" for="ddvTimeout">${this.t('lbl.timeoutS', "Timeout (s)")}</label>
                            <input type="number" id="ddvTimeout" class="input" aria-label="Timeout seconds" value="${d.timeout}" min="1" max="30"></div>
                    </div>
                </div>
                <div id="ddvRtu" style="display:${d.protocol === 'rtu' ? '' : 'none'}">
                    <div class="form-row">
                        <div class="form-group flex-2"><label class="form-label" for="ddvSerial">${t('devices.wizard.serialPort', 'Serial port')}</label>
                            <input type="text" id="ddvSerial" class="input" value="${this._esc(d.serial_port)}" placeholder="/dev/ttyUSB0"></div>
                        <div class="form-group"><label class="form-label" for="ddvBaud">${this.t('lbl.baudrate', "Baudrate")}</label>
                            <select id="ddvBaud" class="input" aria-label="Baudrate">${[9600, 19200, 38400, 57600, 115200].map(b => `<option ${b === +d.baudrate ? 'selected' : ''}>${b}</option>`).join('')}</select></div>
                        <div class="form-group"><label class="form-label" for="ddvParity">${this.t('lbl.parity', "Parity")}</label>
                            <select id="ddvParity" class="input" aria-label="Parity">${['N', 'E', 'O'].map(x => `<option ${x === d.parity ? 'selected' : ''}>${x}</option>`).join('')}</select></div>
                        <div class="form-group"><label class="form-label" for="ddvUnitR">${this.t('lbl.unitId', "Unit ID")}</label>
                            <input type="number" id="ddvUnitR" class="input" aria-label="Unit ID" value="${d.unit_id}" min="0" max="255"></div>
                    </div>
                </div>
                <div id="ddvMqtt" style="display:${d.protocol === 'mqtt' ? '' : 'none'}">
                    <div class="form-row">
                        <div class="form-group flex-2"><label class="form-label" for="ddvBroker">${t('devices.wizard.broker', 'MQTT broker')}</label>
                            <input type="text" id="ddvBroker" class="input" value="${this._esc(d.broker || '')}" placeholder="192.168.1.100"></div>
                        <div class="form-group"><label class="form-label" for="ddvMqttPort">${this.t('lbl.port', 'Port')}</label>
                            <input type="number" id="ddvMqttPort" class="input" value="${d.mqtt_port || 1883}" min="1" max="65535"></div>
                    </div>
                    <div class="form-group"><label class="form-label" for="ddvMqttTopic">${t('devices.wizard.mqttTopic', 'Topic (subscribe)')}</label>
                        <input type="text" id="ddvMqttTopic" class="input" value="${this._esc(d.topic || '')}" placeholder="sensors/# or shellies/shelly1/status"></div>
                    <div class="form-row">
                        <div class="form-group"><label class="form-label" for="ddvMqttUser">${this.t('lbl.username', 'Username')}</label>
                            <input type="text" id="ddvMqttUser" class="input" value="${this._esc(d.mqtt_username || '')}" autocomplete="off"></div>
                        <div class="form-group"><label class="form-label" for="ddvMqttPass">${this.t('lbl.password', 'Password')}</label>
                            <input type="password" id="ddvMqttPass" class="input" value="" placeholder="••••••" autocomplete="new-password"></div>
                        <div class="form-group" style="align-self:end;"><label style="display:flex;align-items:center;gap:8px;">
                            <input type="checkbox" id="ddvMqttTls" ${d.mqtt_tls ? 'checked' : ''}> ${t('devices.wizard.mqttTls', 'TLS (8883)')}</label></div>
                    </div>
                    <div class="field-hint">${t('devices.editPassKeep', 'Leave the password blank to keep the stored one.')}</div>
                </div>
                <button class="btn btn-secondary btn-sm" onclick="app.deviceDetailTest(this)"><i class="bi bi-activity"></i> ${t('devices.wizard.testConn', 'Test connection')}</button>
                <span class="wiz-test-result" id="ddvTestResult" role="status"></span>
            </div>
        </div>

        <div class="settings-card">
            <div class="settings-card-header"><h3><i class="bi bi-diagram-3"></i> ${t('devices.detail.identityTemplate', 'Identity & template')}</h3></div>
            <div class="settings-card-body">
                <div class="form-row">
                    <div class="form-group flex-2"><label class="form-label" for="ddvName">${t('devices.wizard.name', 'Device name')}</label>
                        <input type="text" id="ddvName" class="input" value="${this._esc(d.name)}"></div>
                    <div class="form-group"><label class="form-label" for="ddvTemplate">${t('devices.wizard.template', 'Template')}</label>
                        <select id="ddvTemplate" class="input" aria-label="${t('devices.wizard.template', 'Template')}">${tplOptions}</select></div>
                </div>
            </div>
        </div>

        <div class="settings-card">
            <div class="settings-card-header"><h3><i class="bi bi-arrow-repeat"></i> ${t('devices.detail.polling', 'Polling')}</h3></div>
            <div class="settings-card-body">
                <label style="display:flex;align-items:center;gap:8px;">
                    <input type="checkbox" id="ddvEnabled" ${d.enabled ? 'checked' : ''}>
                    ${t('devices.wizard.enabled', 'Start polling immediately after saving')}</label>
                <div style="margin-top:14px;">
                    <div style="color:var(--text-secondary);font-size:12px;margin-bottom:6px;">${t('devices.pollGroups', 'Poll-group intervals (seconds)')}</div>
                    <div id="ddvPollGroups"><span class="field-hint">${t('common.loading', 'Loading…')}</span></div>
                    <div style="margin-top:8px;display:flex;align-items:center;gap:10px;">
                        <button class="btn btn-secondary btn-sm" ${this._act('saveDevicePollGroups', [s.id], {el: true})}><i class="bi bi-clock"></i> ${t('devices.savePollGroups', 'Save intervals')}</button>
                        <span class="save-feedback" id="ddvPgFeedback"></span>
                    </div>
                </div>
            </div>
            <div class="settings-card-footer">
                <span class="save-feedback" id="ddvFeedback"></span>
                <button class="btn btn-primary btn-sm" onclick="app.saveDeviceDetail(this)"><i class="bi bi-check-lg"></i> ${t('settings.saveApply', 'Save & Apply')}</button>
            </div>
        </div>
        </div>

        <!-- ── Outputs ── -->
        <div data-dpanel="outputs" hidden>
            ${this._sinkCardMqtt(d, primary)}
            ${this._sinkCardInflux(d, primary)}
            ${this._sinkCardHttp(d, primary)}
            ${this._sinkCardRest(d, primary)}
            <div class="settings-card"><div class="settings-card-footer">
                <span class="save-feedback" id="ddvFeedback2"></span>
                <button class="btn btn-primary btn-sm" onclick="app.saveDeviceDetail(this)"><i class="bi bi-check-lg"></i> ${t('settings.saveApply', 'Save & Apply')}</button>
            </div></div>
        </div>`;
    },

    // Live status pill for a sink. green = actively delivering; amber "idle" =
    // sink is up but the device isn't producing (down/no data) so nothing flows;
    // amber "not connected" = broker/db is down; grey = turned off for this device.
    _sinkStatusPill(sink, enabled, deviceLive) {
        if (!enabled) return `<span class="sink-pill off">${this.t('devices.sink.off', 'off')}</span>`;
        if (!sink?.available) return `<span class="sink-pill off">${this.t('devices.sink.noService', 'service off')}</span>`;
        if (!sink.connected) return `<span class="sink-pill warn">${this.t('devices.sink.down', 'not connected')}</span>`;
        if (deviceLive === false) return `<span class="sink-pill warn">${this.t('devices.sink.idle', 'idle · no data')}</span>`;
        return `<span class="sink-pill ok">${this.t('devices.sink.active', 'active')}</span>`;
    },

    _sinkCardMqtt(d, primary) {
        const s = this._devDetail.sinks?.mqtt || {};
        const lock = primary;   // device #1 always publishes (protects HA/dashboards)
        return `
        <div class="settings-card">
            <div class="settings-card-header">
                <h3><i class="bi bi-broadcast"></i> MQTT ${this._sinkStatusPill(s, d.mqtt_enabled, this._devDetail?.entry?.connected)}</h3>
                <label class="switch-label" title="${lock ? this.t('devices.sink.lockedPrimary', 'Always on for device #1') : ''}">
                    <input type="checkbox" id="ddvMqttEnabled" ${d.mqtt_enabled ? 'checked' : ''} ${lock ? 'disabled' : ''}>
                    <span>${this.t('devices.sink.enable', 'Enable')}</span>
                </label>
            </div>
            <div class="settings-card-body">
                <div class="form-row">
                    <div class="form-group flex-2"><label class="form-label" for="ddvTopic">${this.t('devices.wizard.topicPrefix', 'MQTT topic prefix')}</label>
                        <input type="text" id="ddvTopic" class="input" value="${this._esc(d.topic_prefix)}" disabled></div>
                </div>
                <label style="display:flex;align-items:center;gap:8px;margin-top:6px;">
                    <input type="checkbox" id="ddvHaDisc" ${d.ha_discovery_enabled ? 'checked' : ''}>
                    ${this.t('devices.wizard.haDiscovery', 'Publish Home Assistant MQTT discovery for this device')}</label>
                <p class="field-hint"><i class="bi bi-lock"></i> ${primary
                    ? this.t('devices.sink.mqttLockNote', 'Topic and MQTT output are fixed for device #1 (protects existing history & Home Assistant).')
                    : this.t('devices.sink.routeLockNote', 'Topic prefix is fixed after creation — changing it would orphan existing history & HA entities.')}</p>
            </div>
        </div>`;
    },

    _sinkCardInflux(d, primary) {
        const s = this._devDetail.sinks?.influxdb || {};
        const lock = primary;
        return `
        <div class="settings-card">
            <div class="settings-card-header">
                <h3><i class="bi bi-database"></i> InfluxDB ${this._sinkStatusPill(s, d.influxdb_enabled, this._devDetail?.entry?.connected)}</h3>
                <label class="switch-label" title="${lock ? this.t('devices.sink.lockedPrimary', 'Always on for device #1') : ''}">
                    <input type="checkbox" id="ddvInfluxEnabled" ${d.influxdb_enabled ? 'checked' : ''} ${lock ? 'disabled' : ''}>
                    <span>${this.t('devices.sink.enable', 'Enable')}</span>
                </label>
            </div>
            <div class="settings-card-body">
                <div class="form-row">
                    <div class="form-group"><label class="form-label" for="ddvBucket">${this.t('devices.wizard.bucket', 'InfluxDB bucket')}</label>
                        <input type="text" id="ddvBucket" class="input" value="${this._esc(d.bucket)}" disabled></div>
                    <div class="form-group"><label class="form-label" for="ddvTag">${this.t('devices.wizard.deviceTag', 'Influx device tag')}</label>
                        <input type="text" id="ddvTag" class="input" value="${this._esc(d.device_tag)}" disabled></div>
                </div>
                <p class="field-hint"><i class="bi bi-lock"></i> ${primary
                    ? this.t('devices.sink.influxLockNote', 'Bucket and tag are fixed for device #1 (protects existing history).')
                    : this.t('devices.sink.influxLockNoteEdit', 'Bucket and tag are fixed after creation — changing them would orphan existing history.')}</p>
            </div>
        </div>`;
    },

    _wireDeviceDetail() {
        document.querySelectorAll('input[name="ddvProto"]').forEach(r =>
            r.addEventListener('change', () => {
                const p = document.querySelector('input[name="ddvProto"]:checked')?.value || 'tcp';
                const show = (id, on) => { const e = document.getElementById(id); if (e) e.style.display = on ? '' : 'none'; };
                show('ddvTcp', p === 'tcp'); show('ddvRtu', p === 'rtu'); show('ddvHttp', p === 'http'); show('ddvMqtt', p === 'mqtt');
            }));
        // in-place workspace tabs (Overview / Edit / Outputs)
        document.querySelectorAll('#deviceWsTabs .config-main-tab[data-dtab]').forEach(tab =>
            tab.addEventListener('click', () => this._switchDeviceTab(tab.dataset.dtab)));
        this._loadDevicePollGroups(this._devDetail?.id);
        this._loadDeviceSnapshot(this._devDetail?.id);   // Overview is the default tab
    },

    _switchDeviceTab(name) {
        const id = this._devDetail?.id;
        this._restoreWsPages();                       // return any embedded view home first
        document.querySelectorAll('#deviceWsTabs .config-main-tab[data-dtab]').forEach(t =>
            t.classList.toggle('active', t.dataset.dtab === name));
        document.querySelectorAll('#deviceDetailView [data-dpanel]').forEach(p =>
            p.hidden = p.dataset.dpanel !== name);
        this._viewDevice = id;                        // lock the scoped views to THIS device
        if (name === 'overview') this._loadDeviceSnapshot(id);
        else if (name === 'outputs') { if (this._devDetail?.data?.http_output_enabled) this._loadHttpFeed(id); }
        else if (name === 'calculated') this._initCalculated(id);
        else if (name === 'measurements') this._embedRegisters(id);
        else if (name === 'monitor') this._embedWsPage('monitor', () => this.initMonitorPage());
        else if (name === 'history') this._embedWsPage('history', () => this.initHistoryPage());
        else if (name === 'energy') this._embedWsPage('energy', () => this.initEnergyPage());
    },

    // Relocate a standalone page (#<page>Page) into its workspace panel and render
    // it there — reusing the exact page logic, scoped to the current device. The
    // page is moved back to its DOM home on tab-switch / close (see _restoreWsPages).
    _embedWsPage(page, init) {
        const el = document.getElementById(`${page}Page`);
        const panel = document.querySelector(`#deviceDetailView [data-dpanel="${page}"]`);
        if (!el || !panel) return;
        this._wsEmbedded = this._wsEmbedded || {};
        if (!this._wsEmbedded[page]) this._wsEmbedded[page] = { parent: el.parentElement, next: el.nextSibling };
        panel.appendChild(el);
        el.classList.add('active');                   // .page is shown only when .active
        try { init(); } catch (e) { console.error('embed ' + page, e); }
    },

    // Measurements = the per-device register editor (deviceRegistersView), embedded.
    _embedRegisters(id) {
        const el = document.getElementById('deviceRegistersView');
        const panel = document.querySelector('#deviceDetailView [data-dpanel="measurements"]');
        if (!el || !panel) return;
        this._wsEmbedded = this._wsEmbedded || {};
        if (!this._wsEmbedded.registers) this._wsEmbedded.registers = { parent: el.parentElement, next: el.nextSibling };
        panel.appendChild(el);
        el.style.display = '';
        this.setRegDevice(id).then(() => {
            this.registerSearchPage = 1;
            this.renderRegistersTable();
            this.updateConfigTabs();
            this.renderSelectedRegistersList();
            this._wireDeviceRegTabs();
            this.switchDeviceRegTab('available');
        });
    },

    // Move every embedded view back to its original DOM location (so the pages
    // keep working elsewhere and are never destroyed with the detail view).
    _restoreWsPages() {
        const em = this._wsEmbedded || {};
        for (const key in em) {
            const el = key === 'registers'
                ? document.getElementById('deviceRegistersView')
                : document.getElementById(`${key}Page`);
            if (el && em[key].parent) {
                el.classList.remove('active');
                if (key === 'registers') el.style.display = 'none';
                em[key].parent.insertBefore(el, em[key].next);
            }
        }
        this._wsEmbedded = {};
        if (this._stopMonitorPoll) this._stopMonitorPoll();   // stop the monitor poll when leaving
    },

    // Read-only status view — what you see first, no forms.
    _deviceOverviewHtml(d, entry) {
        const t = this.t.bind(this);
        const health = entry.data_health || (entry.connected ? 'ok' : 'idle');
        const hColor = { ok: 'var(--success,#22c55e)', degraded: 'var(--warning,#f59e0b)',
                         down: 'var(--danger,#ef4444)', idle: 'var(--text-secondary,#8a94a0)' };
        const hLabel = { ok: 'OK', degraded: 'degraded', down: 'down', idle: 'idle' }[health] || health;
        const src = d.protocol === 'http' ? this._esc(d.url || '—')
                  : d.protocol === 'rtu' ? `${this._esc(d.serial_port || '—')} · ${d.baudrate}${d.parity} · unit ${d.unit_id}`
                  : `${this._esc(d.host || '—')}:${d.port} · unit ${d.unit_id}`;
        const proto = { tcp: 'Modbus TCP', rtu: 'Modbus RTU', http: 'HTTP / JSON' }[d.protocol] || d.protocol;
        const fact = (label, val) => `<div><div style="color:var(--text-secondary);font-size:11.5px;">${label}</div><div style="font-weight:600;font-size:13.5px;">${val}</div></div>`;
        const mp = this._sinkStatusPill(this._devDetail.sinks?.mqtt, d.mqtt_enabled, entry.connected);
        const ip = this._sinkStatusPill(this._devDetail.sinks?.influxdb, d.influxdb_enabled, entry.connected);
        return `
        <div class="settings-card">
            <div class="settings-card-body">
                <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;">
                    <span class="status-dot" style="width:12px;height:12px;border-radius:50%;--dot:${hColor[health]};background:var(--dot);box-shadow:0 0 0 3px color-mix(in srgb, var(--dot) 16%, transparent);"></span>
                    <span style="font-weight:600;font-size:15px;">${entry.connected ? t('devices.connected', 'Connected') : t('devices.notConnected', 'not connected')}</span>
                    <span class="dev-chip">${hLabel}</span>
                </div>
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px 24px;margin-bottom:8px;">
                    ${fact(t('devices.overview.source', 'Source'), `${proto}<br><span style="font-weight:400;font-size:12px;color:var(--text-secondary);word-break:break-all;">${src}</span>`)}
                    ${fact(t('devices.overview.template', 'Template'), this._esc(d.template || '—'))}
                    ${fact(t('devices.overview.registers', 'Measurements polled'), d.selected_registers)}
                    ${fact(t('devices.overview.pollRate', 'Poll rate'), entry.poll_rate != null ? entry.poll_rate + '/s' : '—')}
                    ${fact(t('devices.overview.lastRead', 'Last read'), entry.staleness_age_s != null ? entry.staleness_age_s + 's ago' : '—')}
                    ${fact('MQTT', `${mp} <code style="font-size:11px;">${this._esc(d.topic_prefix || '')}</code>`)}
                    ${fact('InfluxDB', `${ip} <code style="font-size:11px;">${this._esc(d.bucket || '')}</code>`)}
                </div>
            </div>
        </div>
        <div class="settings-card">
            <div class="settings-card-header"><h3><i class="bi bi-activity"></i> ${t('devices.overview.live', 'Live values')}</h3></div>
            <div class="settings-card-body"><div id="ddvSnapshot"><span class="field-hint">${t('common.loading', 'Loading…')}</span></div></div>
        </div>`;
    },

    async _loadDeviceSnapshot(id) {
        const box = document.getElementById('ddvSnapshot');
        if (!box || !id) return;
        try {
            const d = await (await fetch('/api/values?device=' + encodeURIComponent(id))).json();
            const vals = Object.values(d.values || {});
            if (!vals.length) { box.innerHTML = `<span class="field-hint">${this.t('devices.overview.noValues', 'No values yet — the device may not be polling.')}</span>`; return; }
            const fmt = (val) => (typeof val === 'number' && !Number.isInteger(val))
                ? Number(val.toFixed(3)).toString()   // cap noise at 3 decimals, drop trailing zeros
                : this._esc(String(val));
            box.innerHTML = `<table style="width:100%;font-size:13px;">${vals.slice(0, 40).map(v => `<tr>
                <td style="padding:2px 12px 2px 0;color:var(--text-secondary);">${this._esc(v.label || v.name || '')}${v.label && v.name && v.label !== v.name ? ` <span style="color:var(--text-tertiary,#9aa4af);font-size:11px;">${this._esc(v.name)}</span>` : ''}</td>
                <td style="padding:2px 0;text-align:right;font-variant-numeric:tabular-nums;">${fmt(v.value)} <span style="color:var(--text-secondary);">${this._esc(v.unit || '')}</span></td></tr>`).join('')}</table>`;
        } catch (e) { box.innerHTML = `<span class="field-hint">${this.t('common.error', 'Error')}</span>`; }
    },

    async _loadDevicePollGroups(id) {
        const box = document.getElementById('ddvPollGroups');
        if (!box || !id) return;
        try {
            const d = await (await fetch(`/api/devices/${encodeURIComponent(id)}/poll-groups`)).json();
            const groups = d.poll_groups || {};
            box.innerHTML = Object.entries(groups).map(([name, g]) => `
                <div style="display:flex;align-items:center;gap:8px;margin:4px 0;">
                    <label style="width:90px;font-size:13px;">${this._esc(name)}</label>
                    <input type="number" class="input input-sm ddv-pg" data-group="${this._esc(name)}"
                           value="${g.interval}" min="0.05" step="0.05" style="width:110px;" aria-label="${this._esc(name)} interval seconds">
                    <span style="color:var(--text-secondary);font-size:12px;">${this._esc(g.description || '')}</span>
                </div>`).join('') || `<span class="field-hint">${this.t('devices.noPollGroups', 'No poll groups.')}</span>`;
        } catch (e) {
            box.innerHTML = `<span class="field-hint">${this.t('common.error', 'Error')}</span>`;
        }
    },

    async saveDevicePollGroups(id, btn) {
        const fb = document.getElementById('ddvPgFeedback');
        const groups = {};
        document.querySelectorAll('#ddvPollGroups .ddv-pg').forEach(inp => {
            const v = parseFloat(inp.value);
            if (isFinite(v) && v > 0) groups[inp.dataset.group] = { interval: v };
        });
        if (!Object.keys(groups).length) return;
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="bi bi-arrow-repeat spin"></i>'; }
        if (fb) { fb.textContent = ''; fb.className = 'save-feedback'; }
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(id)}/poll-groups`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ poll_groups: groups }) });
            if (!r.ok) { const e = await r.json(); if (fb) { fb.textContent = e.detail || 'failed'; fb.className = 'save-feedback err'; } return; }
            if (fb) { fb.textContent = '✓ ' + this.t('settings.saved', 'Saved & applied'); fb.className = 'save-feedback ok'; setTimeout(() => { if (fb.classList.contains('ok')) fb.textContent = ''; }, 3000); }
        } catch (e) {
            if (fb) { fb.textContent = e.message; fb.className = 'save-feedback err'; }
        } finally { if (btn) { btn.disabled = false; btn.innerHTML = orig; } }
    },

    _gatherDeviceDetail() {
        const g = id => document.getElementById(id);
        const s = this._devDetail, d = s.data;
        d.protocol = document.querySelector('input[name="ddvProto"]:checked')?.value || 'tcp';
        d.name = g('ddvName')?.value.trim() || s.id;
        d.template = g('ddvTemplate')?.value || '';
        d.enabled = !!g('ddvEnabled')?.checked;
        d.ha_discovery_enabled = !!g('ddvHaDisc')?.checked;
        // Per-device output sinks. The primary is locked on (inputs disabled →
        // fall back to its stored value so it never flips off).
        d.mqtt_enabled = s.primary ? true : !!g('ddvMqttEnabled')?.checked;
        d.influxdb_enabled = s.primary ? true : !!g('ddvInfluxEnabled')?.checked;
        if (d.protocol === 'http') {
            d.url = g('ddvUrl')?.value.trim() || '';
        } else if (d.protocol === 'mqtt') {
            d.broker = g('ddvBroker')?.value.trim() || '';
            d.mqtt_port = parseInt(g('ddvMqttPort')?.value, 10) || 1883;
            d.topic = g('ddvMqttTopic')?.value.trim() || '';
            d.mqtt_username = g('ddvMqttUser')?.value.trim() || '';
            d.mqtt_password = g('ddvMqttPass')?.value ?? '';   // blank = keep stored
            d.mqtt_tls = !!g('ddvMqttTls')?.checked;
        } else if (d.protocol === 'tcp') {
            d.host = g('ddvHost')?.value.trim() || '';
            d.port = parseInt(g('ddvPort')?.value, 10) || 502;
            d.unit_id = parseInt(g('ddvUnit')?.value, 10) || 0;
            d.timeout = parseFloat(g('ddvTimeout')?.value) || 3;
        } else {
            d.serial_port = g('ddvSerial')?.value.trim() || '';
            d.baudrate = parseInt(g('ddvBaud')?.value, 10) || 9600;
            d.parity = g('ddvParity')?.value || 'N';
            d.unit_id = parseInt(g('ddvUnitR')?.value, 10) || 0;
        }
        // routing fields are disabled (unchanged) for the primary device
        if (!s.primary) {
            d.topic_prefix = g('ddvTopic')?.value.trim() || d.topic_prefix;
            d.bucket = g('ddvBucket')?.value.trim() || '';
            d.device_tag = g('ddvTag')?.value.trim() || '';
        }
        return d;
    },

    async deviceDetailTest(btn) {
        const d = this._gatherDeviceDetail();
        const out = document.getElementById('ddvTestResult');
        const orig = btn.innerHTML;
        btn.disabled = true; btn.innerHTML = '<span class="btn-spinner"></span> …';
        try {
            const conn = d.protocol === 'http'
                ? { protocol: 'http', url: d.url }
                : d.protocol === 'mqtt'
                ? { protocol: 'mqtt', broker: d.broker, port: d.mqtt_port || 1883, topic: d.topic,
                    username: d.mqtt_username || '', password: d.mqtt_password || '', tls: !!d.mqtt_tls }
                : d.protocol === 'rtu'
                ? { protocol: 'rtu', serial_port: d.serial_port, baudrate: d.baudrate, parity: d.parity, stopbits: d.stopbits, unit_id: d.unit_id, timeout: d.timeout }
                : { protocol: 'tcp', host: d.host, port: d.port, unit_id: d.unit_id, timeout: d.timeout };
            const r = await fetch('/api/devices/test', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ connection: conn }),
            });
            const res = await r.json();
            out.className = 'wiz-test-result ' + (res.ok ? 'ok' : 'err');
            out.textContent = (res.ok ? '✓ ' : '✗ ') + (res.message || '');
        } catch (e) {
            out.className = 'wiz-test-result err'; out.textContent = '✗ ' + e.message;
        } finally { btn.disabled = false; btn.innerHTML = orig; }
    },

    _sinkCardHttp(d, primary) {
        const s = this._devDetail.sinks?.http || {};
        const on = !!d.http_output_enabled;
        const id = this._devDetail.id;
        const url = `${location.origin}/api/meters/${id}`;
        return `
        <div class="settings-card">
            <div class="settings-card-header">
                <h3><i class="bi bi-braces"></i> ${this.t('devices.sink.httpTitle', 'HTTP / JSON output')} ${this._sinkStatusPill(s, on, this._devDetail?.entry?.connected)}</h3>
                <label class="switch-label">
                    <input type="checkbox" id="ddvHttpEnabled" ${on ? 'checked' : ''} onchange="app.toggleHttpOutput('${this._esc(id)}', this)">
                    <span>${this.t('devices.sink.enable', 'Enable')}</span>
                </label>
            </div>
            <div class="settings-card-body">
                <div id="ddvHttpUrlRow" class="form-group flex-2" style="display:${on ? '' : 'none'}">
                    <label class="form-label" for="ddvHttpUrl">${this.t('devices.sink.httpUrl', 'JSON feed URL')}</label>
                    <div style="display:flex;gap:6px;align-items:center;">
                        <input type="text" id="ddvHttpUrl" class="input" value="${this._esc(url)}" readonly onclick="this.select()">
                        <button type="button" class="btn btn-sm btn-secondary" ${this._act('copyText', [url], {el: true})} title="${this.t('common.copy', 'Copy')}"><i class="bi bi-clipboard"></i></button>
                        <a class="btn btn-sm btn-secondary" href="${this._esc(url)}" target="_blank" rel="noopener"><i class="bi bi-box-arrow-up-right"></i> ${this.t('common.open', 'Open')}</a>
                    </div>
                </div>
                <div id="ddvHttpFeed" class="http-feed" style="display:${on ? '' : 'none'}">
                    <div class="http-feed-head">
                        <span id="ddvHttpStats" class="http-feed-stats">${this.t('common.loading', 'Loading…')}</span>
                        <button type="button" class="btn btn-sm btn-secondary" ${this._act('_loadHttpFeed', [id])} title="${this.t('common.refresh', 'Refresh')}"><i class="bi bi-arrow-clockwise"></i> ${this.t('common.refresh', 'Refresh')}</button>
                    </div>
                    <pre id="ddvHttpPreview" class="http-feed-preview" aria-live="polite"></pre>
                </div>
                <p class="field-hint"><i class="bi bi-info-circle"></i> ${this.t('devices.sink.httpNote', 'Serves the live values of this device as read-only JSON, keyed by measurement name (Solar-API style) — pull it from any HTTP client without MQTT or InfluxDB. Guarded by the same IP allowlist / auth as the UI.')}</p>
            </div>
        </div>`;
    },

    _sinkCardRest(d, primary) {
        const rp = this._devDetail.entry?.rest_push || {};
        const s = this._devDetail.sinks?.rest || {};
        const on = !!rp.enabled;
        const last = rp.last || {};
        const hdrText = Object.entries(rp.headers || {}).map(([k, v]) => `${k}: ${v}`).join('\n');
        const statusLine = last.ts
            ? (last.ok ? `✓ ${this.t('rest.ok', 'last push OK')} ${last.code ? '(' + last.code + ')' : ''}`
                       : `✕ ${this._esc(last.error || 'failed')}`)
            : '';
        return `
        <div class="settings-card">
            <div class="settings-card-header">
                <h3><i class="bi bi-cloud-upload"></i> ${this.t('rest.title', 'REST push')} ${this._sinkStatusPill(s, on, this._devDetail?.entry?.connected)}</h3>
                <label class="switch-label"><input type="checkbox" id="ddvRestEnabled" ${on ? 'checked' : ''}><span>${this.t('devices.sink.enable', 'Enable')}</span></label>
            </div>
            <div class="settings-card-body">
                <div class="form-row">
                    <div class="form-group flex-2"><label class="form-label" for="ddvRestUrl">${this.t('rest.url', 'POST URL')}</label>
                        <input type="text" id="ddvRestUrl" class="input" value="${this._esc(rp.url || '')}" placeholder="https://my-cloud/api/telemetry"></div>
                    <div class="form-group" style="max-width:110px"><label class="form-label" for="ddvRestInterval">${this.t('rest.interval', 'Interval (s)')}</label>
                        <input type="number" id="ddvRestInterval" class="input" min="5" value="${rp.interval_s || 30}"></div>
                    <div class="form-group" style="max-width:120px"><label class="form-label" for="ddvRestFormat">${this.t('rest.format', 'Format')}</label>
                        <select id="ddvRestFormat" class="input"><option value="native" ${rp.format !== 'flat' ? 'selected' : ''}>native</option><option value="flat" ${rp.format === 'flat' ? 'selected' : ''}>flat</option></select></div>
                </div>
                <div class="form-group"><label class="form-label" for="ddvRestHeaders">${this.t('rest.headers', 'Headers (Name: Value per line)')}</label>
                    <textarea id="ddvRestHeaders" class="input" rows="2" spellcheck="false" placeholder="Authorization: Bearer …">${this._esc(hdrText)}</textarea></div>
                <label style="display:flex;align-items:center;gap:8px;margin-top:6px;">
                    <input type="checkbox" id="ddvRestVerify" ${rp.verify_tls !== false ? 'checked' : ''}> ${this.t('rest.verifyTls', 'Verify TLS certificate')}</label>
                <div class="rest-status" id="ddvRestStatus">${statusLine}</div>
                <div class="calc-editor-actions" style="margin-top:12px">
                    <button class="btn btn-ghost btn-sm" ${this._act('testRestPush', [this._devDetail.id])}><i class="bi bi-send"></i> ${this.t('rest.test', 'Test push')}</button>
                    <button class="btn btn-primary btn-sm" ${this._act('saveRestPush', [this._devDetail.id])}><i class="bi bi-check-lg"></i> ${this.t('common.save', 'Save')}</button>
                </div>
                <p class="field-hint"><i class="bi bi-info-circle"></i> ${this.t('rest.note', 'POSTs the live values of this device as JSON to the URL every interval. External URLs are allowed. Put a Bearer / API key in Headers (stored masked).')}</p>
            </div>
        </div>`;
    },

    _restPushPayload() {
        const parseHeaders = (txt) => {
            const h = {};
            (txt || '').split('\n').forEach(line => {
                const i = line.indexOf(':');
                if (i < 1) return;
                const k = line.slice(0, i).trim();
                if (k) h[k] = line.slice(i + 1).trim();
            });
            return h;
        };
        return {
            enabled: document.getElementById('ddvRestEnabled').checked,
            url: document.getElementById('ddvRestUrl').value.trim(),
            interval_s: parseInt(document.getElementById('ddvRestInterval').value) || 30,
            format: document.getElementById('ddvRestFormat').value,
            verify_tls: document.getElementById('ddvRestVerify').checked,
            headers: parseHeaders(document.getElementById('ddvRestHeaders').value),
        };
    },

    async saveRestPush(id) {
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(id)}/rest-push`,
                { method: 'POST', headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify(this._restPushPayload()) });
            if (!r.ok) throw new Error((await r.json()).detail?.errors?.join(' · ') || 'failed');
            if (this._devDetail?.entry) this._devDetail.entry.rest_push = (await r.json()).rest_push;
            this.showToast('success', this.t('rest.title', 'REST push'), this.t('settings.saved', 'Saved & applied'));
            this._fetchDevices(true);
            return true;
        } catch (e) {
            this.showToast('error', this.t('common.error', 'Error'), e.message);
            return false;
        }
    },

    async testRestPush(id) {
        if (!(await this.saveRestPush(id))) return;   // test uses the saved config
        const box = document.getElementById('ddvRestStatus');
        try {
            const d = await (await fetch(`/api/devices/${encodeURIComponent(id)}/rest-push/test`, { method: 'POST' })).json();
            if (box) box.textContent = d.ok ? `✓ ${this.t('rest.ok', 'push OK')} ${d.code ? '(' + d.code + ')' : ''}` : `✕ ${d.error || 'failed'}`;
            this.showToast(d.ok ? 'success' : 'error', this.t('rest.title', 'REST push'),
                d.ok ? this.t('rest.ok', 'push OK') : (d.error || 'failed'));
        } catch (e) { this.showToast('error', this.t('common.error', 'Error'), e.message); }
    },

    async toggleHttpOutput(id, el) {
        const enabled = !!el.checked;
        el.disabled = true;
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(id)}/http-output`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled }),
            });
            if (!r.ok) throw new Error((await r.json()).detail?.errors?.join(' · ') || 'failed');
            if (this._devDetail) {
                if (this._devDetail.data) this._devDetail.data.http_output_enabled = enabled;
                this._devDetail.sinks = this._devDetail.sinks || {};
                this._devDetail.sinks.http = { ...(this._devDetail.sinks.http || {}), enabled, active: enabled };
            }
            const row = document.getElementById('ddvHttpUrlRow');
            if (row) row.style.display = enabled ? '' : 'none';
            const feed = document.getElementById('ddvHttpFeed');
            if (feed) feed.style.display = enabled ? '' : 'none';
            if (enabled) this._loadHttpFeed(id);         // show what it serves right away
            this.showToast('success', this.t('devices.sink.httpTitle', 'HTTP / JSON output'),
                enabled ? this.t('devices.sink.httpOn', 'JSON feed enabled')
                        : this.t('devices.sink.httpOff', 'JSON feed disabled'));
            this._fetchDevices(true);   // refresh list pills/badges
        } catch (e) {
            el.checked = !enabled;      // revert on failure
            this.showToast('error', this.t('common.error', 'Error'), e.message);
        } finally {
            el.disabled = false;
        }
    },

    // Load a live snapshot of what the JSON feed is serving, straight into the
    // Outputs card (count · freshness · timestamp + a JSON preview) so the operator
    // sees the data without opening the raw endpoint in another tab.
    async _loadHttpFeed(id) {
        const stats = document.getElementById('ddvHttpStats');
        const pre = document.getElementById('ddvHttpPreview');
        if (!stats && !pre) return;
        if (stats) stats.textContent = this.t('common.loading', 'Loading…');
        try {
            const r = await fetch(`/api/meters/${encodeURIComponent(id)}`);
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const d = await r.json();
            const n = Object.keys(d.values || {}).length;
            const fresh = !d.stale;
            const badge = `<span class="sink-pill ${fresh ? 'ok' : 'warn'}">${fresh
                ? this.t('devices.sink.freshBadge', 'fresh') : this.t('devices.sink.staleBadge', 'stale')}</span>`;
            if (stats) stats.innerHTML =
                `<strong>${n}</strong> ${this.t('devices.sink.httpValues', 'values')} · `
                + `${this.t('devices.sink.updated', 'updated')} ${this._esc(this._fmtFeedTime(d.ts))} ${badge}`;
            if (pre) pre.textContent = JSON.stringify(d, null, 2);
        } catch (e) {
            if (stats) stats.textContent =
                this.t('devices.sink.httpFetchErr', 'Could not load feed') + ': ' + e.message;
            if (pre) pre.textContent = '';
        }
    },

    _fmtFeedTime(ts) {
        if (!ts) return '—';
        const d = new Date(ts);
        if (isNaN(d.getTime())) return ts;
        return d.toLocaleTimeString();
    },

    async copyText(text, btn) {
        try { await navigator.clipboard.writeText(text); }
        catch (e) { /* clipboard blocked (non-HTTPS/older browser) — the field is selectable as fallback */ }
        if (btn) {
            const o = btn.innerHTML;
            btn.innerHTML = '<i class="bi bi-check-lg"></i>';
            setTimeout(() => { btn.innerHTML = o; }, 1200);
        }
    },

    async saveDeviceDetail(btn) {
        const d = this._gatherDeviceDetail();
        const s = this._devDetail;
        const fb = document.getElementById('ddvFeedback');
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="bi bi-arrow-repeat spin"></i>'; }
        if (fb) { fb.textContent = ''; fb.className = 'save-feedback'; }
        const payload = {
            id: s.id, name: d.name, template: d.template, enabled: d.enabled,
            connection: d.protocol === 'http'
                ? { protocol: 'http', url: d.url }
                : d.protocol === 'mqtt'
                ? { protocol: 'mqtt', broker: d.broker, port: d.mqtt_port || 1883, topic: d.topic,
                    username: d.mqtt_username || '', password: d.mqtt_password || '', tls: !!d.mqtt_tls }
                : d.protocol === 'tcp'
                ? { protocol: 'tcp', host: d.host, port: d.port, unit_id: d.unit_id, timeout: d.timeout }
                : { protocol: 'rtu', serial_port: d.serial_port, baudrate: d.baudrate, parity: d.parity, stopbits: d.stopbits, unit_id: d.unit_id },
            mqtt: { topic_prefix: d.topic_prefix || `meters/${s.id}`, enabled: d.mqtt_enabled },
            influxdb: { bucket: d.bucket || undefined, device_tag: d.device_tag || undefined, enabled: d.influxdb_enabled },
            ha_discovery_enabled: d.ha_discovery_enabled,
        };
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(s.id)}`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
            });
            if (!r.ok) {
                const errs = (await r.json()).detail?.errors || ['save failed'];
                if (fb) { fb.textContent = errs.join(' · '); fb.className = 'save-feedback err'; }
                return;
            }
            if (fb) { fb.textContent = '✓ ' + this.t('settings.saved', 'Saved & applied'); fb.className = 'save-feedback ok'; }
            this.showToast('success', this.t('devices.updated', 'Device updated'), d.name);
            await this._fetchDevices(true);
            setTimeout(() => this.closeDeviceDetail(), 700);
        } catch (e) {
            if (fb) { fb.textContent = e.message; fb.className = 'save-feedback err'; }
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = orig; }
        }
    },

    async testDevice(id, btn) {
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="btn-spinner"></span>'; }
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(id)}/test`, { method: 'POST' });
            const d = await r.json();
            this.showToast(d.ok ? 'success' : 'error',
                           this.t(d.ok ? 'devices.testOk' : 'devices.testFail', d.ok ? 'Device answered' : 'Test failed'),
                           d.message || '');
        } catch (e) {
            this.showToast('error', this.t('devices.testFail', 'Test failed'), e.message);
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = orig; }
        }
    },

    async deleteDevice(id) {
        const msg = this.t('devices.deleteConfirm',
            'Delete this device?\n\nYes: stops its polling and removes it from config.yaml (its measurement selection file is kept on disk).\nNo: keeps everything as is.');
        if (!confirm(msg)) return;
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(id)}`, { method: 'DELETE' });
            if (!r.ok) throw new Error((await r.json()).detail?.errors?.join('; ') || r.statusText);
            this.showToast('success', this.t('devices.deleted', 'Device deleted'), id);
            if (this._regDevice === id) this._regDevice = this._primaryDeviceId();
            await this.renderDevicesList();
        } catch (e) {
            this.showToast('error', this.t('devices.deleteFail', 'Delete failed'), e.message);
        }
    }
});
