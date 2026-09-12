// endpoints domain — augments JanitzaMonitor.prototype
// An endpoint = one template + one endpoint + N unit ids, materialized into N
// devices (managed through the endpoint; device CRUD refuses them). An endpoint is an
// ENTITY, not just a grouping: it has its own page, its own output, and its
// own health, the same way a device does.
Object.assign(JanitzaMonitor.prototype, {

    // ── endpoint workspace (full page, in place, like a device detail) ──────────

    async openEndpointDetail(id) {
        if (this.currentPage !== 'devices') this.navigateTo('devices');
        this._stopEndpointDetail();
        let p = null;
        try {
            const r = await fetch(`/api/endpoints/${encodeURIComponent(id)}`);
            if (r.ok) p = await r.json();
        } catch (e) { console.error(e); }
        if (!p || !p.id) {
            this.showToast('error', this.t('endpoints.notFound', 'Endpoint not found'), id);
            return;
        }
        this._endpointDetail = p;
        const list = document.getElementById('devicesListView');
        if (list) list.style.display = 'none';
        const view = document.getElementById('deviceDetailView');
        view.style.display = '';
        view.innerHTML = this._endpointDetailHtml(p);
        // live totals are the point of the page; the tick self-heals if the
        // user navigated away without closing (no orphan timer, ever)
        this._endpointTimer = setInterval(() => this._refreshEndpointDetail(id), 5000);
    },

    _stopEndpointDetail() {
        if (this._endpointTimer) { clearInterval(this._endpointTimer); this._endpointTimer = null; }
    },

    closeEndpointDetail() {
        this._stopEndpointDetail();
        const view = document.getElementById('deviceDetailView');
        if (view) { view.style.display = 'none'; view.innerHTML = ''; }
        const list = document.getElementById('devicesListView');
        if (list) list.style.display = '';
        this.renderDevicesList();
    },

    async _refreshEndpointDetail(id) {
        const view = document.getElementById('deviceDetailView');
        if (!view || view.style.display === 'none' || !view.querySelector('[data-endpoint-page]')) {
            this._stopEndpointDetail();
            return;
        }
        let p = null;
        try {
            const r = await fetch(`/api/endpoints/${encodeURIComponent(id)}`);
            if (r.ok) p = await r.json();
        } catch (e) { return; }                    // a blip must not blank the page
        if (!p || !p.id) return;
        this._endpointDetail = p;
        const set = (sel, html) => {
            const el = view.querySelector(sel);
            if (el) el.innerHTML = html;
        };
        const dot = view.querySelector('#plStatusDot');
        if (dot) dot.style.setProperty('--dot', this._endpointStatusColor(p));
        set('#plStatusWord', this._esc(this._endpointStatusWord(p)));
        set('#plCensus', `${p.online_units}/${p.total_units} ${this.t('endpoints.online', 'online')}`);
        set('#plAggGrid', this._endpointAggGridHtml(p));
        // never redraw the unit rows while one of them is being renamed
        if (!view.querySelector('#plUnitsBody [data-renaming]')) {
            set('#plUnitsBody', this._endpointUnitRowsHtml(p));
        }
    },

    _endpointStatusColor(p) {
        if (p.enabled === false) return 'var(--text-secondary,#8a94a0)';
        return { online: 'var(--success,#22c55e)', partial: 'var(--warning,#f59e0b)',
                 offline: 'var(--danger,#ef4444)' }[p.status] || 'var(--text-secondary,#8a94a0)';
    },

    _endpointStatusWord(p) {
        if (p.enabled === false) return this.t('devices.disabled', 'disabled');
        return { online: this.t('endpoints.status.online', 'online'),
                 partial: this.t('endpoints.status.partial', 'partial'),
                 offline: this.t('endpoints.status.offline', 'offline') }[p.status]
            || this.t('endpoints.status.unknown', 'unknown');
    },

    // A measurement with its unit, scaled where the magnitude asks for it.
    _endpointValue(v, unit) {
        if (typeof v !== 'number') return this._esc(String(v));
        if (unit === 'W' || unit === 'VA' || unit === 'var') {
            if (Math.abs(v) >= 1000) return `${(v / 1000).toFixed(2)} k${unit}`;
        }
        if (unit === 'Wh') {
            if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(3)} MWh`;
            if (Math.abs(v) >= 1000) return `${(v / 1000).toFixed(2)} kWh`;
        }
        const r = Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
        return unit ? `${r} ${unit}` : r;
    },

    _endpointAggGridHtml(p) {
        const meta = p.aggregate_fields || {};
        const skip = new Set(['units_online', 'units_total', 'status']);
        const names = Object.keys(p.aggregates || {}).filter(n => !skip.has(n))
            .sort((a, b) => ((meta[a] || {}).topic || a).localeCompare((meta[b] || {}).topic || b));
        if (!names.length) {
            return `<span class="field-hint">${this.t('endpoints.noAggregates',
                'Nothing is fresh right now — the endpoint publishes its census and status, and resumes totals when a unit reports.')}</span>`;
        }
        return `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px 22px;">`
            + names.map(n => {
                const m = meta[n] || {};
                return `<div>
                    <div style="color:var(--text-secondary);font-size:11.5px;" title="${this._esc(m.topic || n)}">${this._esc(m.label || n)}</div>
                    <div style="font-weight:600;font-size:14px;">${this._endpointValue(p.aggregates[n], m.unit || '')}</div>
                </div>`;
            }).join('') + `</div>`;
    },

    _endpointUnitRowsHtml(p) {
        const hc = { ok: 'var(--success,#22c55e)', degraded: 'var(--warning,#f59e0b)',
                     down: 'var(--danger,#ef4444)', idle: 'var(--text-secondary,#8a94a0)' };
        const t = this.t.bind(this);
        return (p.units || []).map(u => {
            const age = u.staleness_age_s != null ? `${u.staleness_age_s}s` : '—';
            return `<tr data-unit="${this._esc(u.device_id)}">
                <td><span class="status-dot" style="--dot:${hc[u.health] || hc.idle}"
                          title="${this._esc(u.health || 'idle')}"></span> ${u.unit_id}</td>
                <td><span class="dev-chip">${this._esc(u.device_id)}</span></td>
                <td data-unit-name>${this._esc(u.name || '')}</td>
                <td>${this._esc(u.health || 'idle')}</td>
                <td title="${this._esc(u.last_seen || '')}">${age}</td>
                <td>${u.poll_rate != null ? u.poll_rate + '/s' : '—'}</td>
                <td>${u.failed_reads ?? '—'}</td>
                <td style="text-align:right;white-space:nowrap;">
                    <button class="btn btn-ghost btn-sm" ${this._act('endpointRenameUnit', [u.device_id])}
                            title="${t('endpoints.renameUnit', 'Rename this unit')}"><i aria-hidden="true" class="bi bi-pencil"></i></button>
                    <button class="btn btn-ghost btn-sm" ${this._act('openDeviceDetail', [u.device_id])}
                            title="${t('endpoints.openUnit', 'Open this unit')}"><i aria-hidden="true" class="bi bi-box-arrow-up-right"></i></button>
                </td>
            </tr>`;
        }).join('') || `<tr><td colspan="8"><span class="field-hint">${this.t('endpoints.noUnits', 'No units materialized.')}</span></td></tr>`;
    },

    _endpointDetailHtml(p) {
        const t = this.t.bind(this);
        const conn = p.connection || {};
        const proto = (conn.protocol || 'tcp') === 'rtu-tcp' ? 'RTU/TCP' : 'TCP';
        const mq = p.mqtt || {}, ix = p.influxdb || {};
        const fact = (label, val) => `<div><div style="color:var(--text-secondary);font-size:11.5px;">${label}</div>
            <div style="font-weight:600;font-size:13.5px;word-break:break-all;">${val}</div></div>`;
        return `
        <div data-endpoint-page="${this._esc(p.id)}">
        <div class="section-header">
            <h2><button class="btn btn-ghost btn-sm" onclick="app.closeEndpointDetail()" aria-label="${t('common.back', 'Back')}"><i aria-hidden="true" class="bi bi-arrow-left"></i></button>
                <i aria-hidden="true" class="bi bi-diagram-3"></i> ${this._esc(p.name || p.id)}
                <span class="dev-chip">${this._esc(p.id)}</span></h2>
            <div class="header-actions">
                <button class="btn btn-secondary btn-sm" ${this._act('testEndpointUi', [p.id], { el: true })}
                        title="${t('endpoints.testHint', 'Probes every unit on the shared endpoint. Opens another Modbus client — dataloggers serve only a few at once.')}"><i aria-hidden="true" class="bi bi-activity"></i> ${t('endpoints.test', 'Test units')}</button>
                <button class="btn btn-secondary btn-sm" ${this._act('openEndpointModal', [p.id])}><i aria-hidden="true" class="bi bi-pencil-square"></i> ${t('common.edit', 'Edit')}</button>
                <button class="btn btn-ghost btn-sm" ${this._act('deleteEndpointUi', [p.id])}><i aria-hidden="true" class="bi bi-trash"></i> ${t('common.delete', 'Delete')}</button>
            </div>
        </div>

        <div class="settings-card">
            <div class="settings-card-body">
                <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;">
                    <span class="status-dot" id="plStatusDot" style="width:12px;height:12px;border-radius:50%;--dot:${this._endpointStatusColor(p)};background:var(--dot);box-shadow:0 0 0 3px color-mix(in srgb, var(--dot) 16%, transparent);"></span>
                    <span style="font-weight:600;font-size:15px;" id="plStatusWord">${this._esc(this._endpointStatusWord(p))}</span>
                    <span class="dev-chip" id="plCensus">${p.online_units}/${p.total_units} ${t('endpoints.online', 'online')}</span>
                    ${p.write_locked ? `<span class="sink-pill warn">${t('devices.writeLock.locked', 'locked')}</span>` : ''}
                </div>
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px 24px;">
                    ${fact(t('endpoints.endpoint', 'Endpoint'), `${proto}<br><span style="font-weight:400;font-size:12px;color:var(--text-secondary);">${this._esc(conn.host || '')}:${conn.port || 502}</span>`)}
                    ${fact(t('devices.overview.template', 'Template'), this._esc(p.template || '—'))}
                    ${fact(t('endpoints.unitsTitle', 'Units'), (p.units || []).length)}
                    ${fact('MQTT', `<code style="font-size:11px;">mbg/endpoints/${this._esc(p.id)}/…</code>`)}
                    ${fact('InfluxDB', `<code style="font-size:11px;">${this._esc(ix.bucket || '—')}</code>`)}
                </div>
            </div>
        </div>

        <div class="settings-card">
            <div class="settings-card-header">
                <h3><i aria-hidden="true" class="bi bi-bounding-box"></i> ${t('endpoints.output', 'Endpoint output')}</h3>
                <label class="switch-label">
                    <input type="checkbox" id="plAggEnabled" ${p.aggregates_enabled !== false ? 'checked' : ''}
                           onchange="app.toggleEndpointAggregates('${this._esc(p.id)}', this)">
                    <span>${t('endpoints.aggEnable', 'Publish endpoint totals')}</span>
                </label>
            </div>
            <div class="settings-card-body">
                <div id="plAggGrid">${this._endpointAggGridHtml(p)}</div>
                <p class="field-hint" style="margin-top:14px;"><i aria-hidden="true" class="bi bi-info-circle"></i>
                    ${t('endpoints.aggNote', 'Sums for powers and currents, averages for voltages, frequency and temperatures, and a complete-census sum for energy counters — a unit that stops reporting drops out of a total but never out of a counter. Published on')}
                    <code>mbg/endpoints/${this._esc(p.id)}/…</code>
                    ${t('endpoints.aggNote2', 'and into InfluxDB tagged')} <code>aggregate=endpoint</code>.</p>
            </div>
        </div>

        <div class="settings-card">
            <div class="settings-card-header">
                <h3><i aria-hidden="true" class="bi bi-cpu"></i> ${t('endpoints.unitsTitle', 'Units')}</h3>
            </div>
            <div class="settings-card-body" style="overflow-x:auto;">
                <table class="data-table" style="width:100%;">
                    <thead><tr>
                        <th>${t('endpoints.unitId', 'Unit')}</th>
                        <th>${t('endpoints.deviceId', 'Device')}</th>
                        <th>${t('devices.wizard.name', 'Name')}</th>
                        <th>${t('endpoints.health', 'Health')}</th>
                        <th>${t('devices.overview.lastRead', 'Last read')}</th>
                        <th>${t('dashboard.pollRate', 'Poll rate')}</th>
                        <th>${t('endpoints.errors', 'Errors')}</th>
                        <th></th>
                    </tr></thead>
                    <tbody id="plUnitsBody">${this._endpointUnitRowsHtml(p)}</tbody>
                </table>
                <div id="plTestOut" style="margin-top:10px;"></div>
            </div>
        </div>

        <div class="settings-card">
            <div class="settings-card-header">
                <h3><i aria-hidden="true" class="bi bi-signpost-split"></i> ${t('devices.detail.outputs', 'Outputs')}</h3>
            </div>
            <div class="settings-card-body">
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px 24px;">
                    ${fact(t('devices.wizard.topicPrefix', 'MQTT topic prefix'), `<code style="font-size:11px;">${this._esc(mq.topic_prefix || '—')}</code>`)}
                    ${fact(t('devices.wizard.bucket', 'InfluxDB bucket'), `<code style="font-size:11px;">${this._esc(ix.bucket || '—')}</code>`)}
                    ${fact(t('devices.wizard.deviceTag', 'Influx device tag'), `<code style="font-size:11px;">${this._esc(ix.device_tag || '—')}</code>`)}
                    ${fact(t('endpoints.haDiscovery', 'Home Assistant discovery'), mq.ha_discovery ? t('common.on', 'on') : t('common.off', 'off'))}
                    ${fact(t('devices.sink.httpTitle', 'HTTP / JSON output'), p.http_output_enabled ? t('common.on', 'on') : t('common.off', 'off'))}
                    ${fact(t('devices.sink.restTitle', 'REST push'), (p.rest_push || {}).enabled ? t('common.on', 'on') : t('common.off', 'off'))}
                </div>
                <p class="field-hint" style="margin-top:14px;"><i aria-hidden="true" class="bi bi-lock"></i>
                    ${t('endpoints.routingFixed', 'Routing identity is fixed after creation — changing it would re-route every unit and orphan their history and Home Assistant entities. The per-unit sinks are edited on any unit’s Outputs tab and apply to the whole endpoint.')}</p>
            </div>
        </div>
        </div>`;
    },

    // ── endpoint actions ───────────────────────────────────────────────────────

    async toggleEndpointAggregates(id, el) {
        const p = this._endpointDetail;
        if (!p) return;
        const body = this._endpointPutBody(p, { aggregates: !!el.checked });
        const rsp = await fetch(`/api/endpoints/${encodeURIComponent(id)}`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!rsp.ok) {
            el.checked = !el.checked;
            const d = await rsp.json().catch(() => ({}));
            const errs = d.detail?.errors || [d.detail || rsp.statusText];
            this.showToast('error', this.t('endpoints.saveFail', 'Save failed'),
                Array.isArray(errs) ? errs.join(' · ') : String(errs));
            return;
        }
        this.showToast('success', this.t('endpoints.saved', 'Endpoint saved'),
            el.checked ? this.t('endpoints.aggOn', 'Endpoint totals are published')
                       : this.t('endpoints.aggOff', 'Endpoint totals are off'));
        this._refreshEndpointDetail(id);
    },

    // The PUT body for an edit made from the PAGE: identity + membership, plus
    // whatever the caller overrides. Routing and the endpoint-level flags are kept
    // server-side, so they cannot be dropped by a partial form.
    _endpointPutBody(p, extra = {}) {
        const defName = (u) => `${p.name || p.id} unit ${u.unit_id}`;
        return {
            id: p.id, name: p.name, template: p.template,
            enabled: p.enabled !== false,
            connection: p.connection || {},
            units: (p.units || []).map(u => ({
                unit_id: u.unit_id, id: u.device_id,
                ...(u.name && u.name !== defName(u) ? { name: u.name } : {}),
            })),
            ...extra,
        };
    },

    endpointRenameUnit(deviceId) {
        const row = document.querySelector(`#plUnitsBody tr[data-unit="${CSS.escape(deviceId)}"]`);
        if (!row || row.hasAttribute('data-renaming')) return;
        const cell = row.querySelector('[data-unit-name]');
        const cur = (this._endpointDetail?.units || []).find(u => u.device_id === deviceId);
        row.setAttribute('data-renaming', '1');
        cell.innerHTML = `<div style="display:flex;gap:6px;">
            <input class="input" id="plUnitName" style="min-width:140px;" value="${this._esc(cur?.name || '')}">
            <button class="btn btn-primary btn-sm" ${this._act('saveEndpointUnitName', [deviceId])}><i aria-hidden="true" class="bi bi-check-lg"></i></button>
            <button class="btn btn-ghost btn-sm" ${this._act('cancelEndpointUnitName', [deviceId])}><i aria-hidden="true" class="bi bi-x-lg"></i></button>
        </div>`;
        cell.querySelector('input')?.focus();
    },

    cancelEndpointUnitName(deviceId) {
        const row = document.querySelector(`#plUnitsBody tr[data-unit="${CSS.escape(deviceId)}"]`);
        if (row) row.removeAttribute('data-renaming');
        this._refreshEndpointDetail(this._endpointDetail?.id);
    },

    async saveEndpointUnitName(deviceId) {
        const p = this._endpointDetail;
        const input = document.getElementById('plUnitName');
        if (!p || !input) return;
        const name = input.value.trim();
        const body = this._endpointPutBody(p);
        const u = body.units.find(x => x.id === deviceId);
        if (!u) return;
        if (name) u.name = name; else delete u.name;
        const rsp = await fetch(`/api/endpoints/${encodeURIComponent(p.id)}`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const d = await rsp.json().catch(() => ({}));
        const row = document.querySelector(`#plUnitsBody tr[data-unit="${CSS.escape(deviceId)}"]`);
        if (row) row.removeAttribute('data-renaming');
        if (!rsp.ok) {
            const errs = d.detail?.errors || [d.detail || rsp.statusText];
            this.showToast('error', this.t('endpoints.saveFail', 'Save failed'),
                Array.isArray(errs) ? errs.join(' · ') : String(errs));
        } else {
            this.showToast('success', this.t('endpoints.saved', 'Endpoint saved'), deviceId);
        }
        this._refreshEndpointDetail(p.id);
    },

    async testEndpointUi(id, el) {
        const out = document.getElementById('plTestOut');
        if (el) { el.disabled = true; }
        if (out) out.innerHTML = `<span class="field-hint">${this.t('common.loading', 'Loading…')}</span>`;
        let d = {};
        try {
            const rsp = await fetch(`/api/endpoints/${encodeURIComponent(id)}/test`, { method: 'POST' });
            d = await rsp.json().catch(() => ({}));
        } catch (e) { d = { units: [] }; }
        if (el) { el.disabled = false; }
        if (!out) return;
        const rows = (d.units || []).map(u => `
            <div style="display:flex;gap:8px;align-items:center;font-size:12.5px;">
                <span class="status-dot" style="--dot:${u.ok ? 'var(--success,#22c55e)' : 'var(--danger,#ef4444)'}"></span>
                <span class="dev-chip">${this._esc(u.device_id)}</span>
                <span>${this._esc(u.message || (u.ok ? 'ok' : 'no answer'))}</span>
            </div>`).join('');
        out.innerHTML = rows || `<span class="field-hint">${this.t('endpoints.testNone', 'No units to probe.')}</span>`;
    },

    // ── add / edit dialog ───────────────────────────────────────────────────

    async openEndpointModal(editId = null) {
        let templates = [];
        try { templates = (await (await fetch('/api/device-templates')).json()).templates || []; }
        catch (e) { console.error(e); }
        const p = editId ? (this._endpointDetail?.id === editId ? this._endpointDetail
            : (this._endpoints || []).find(x => x.id === editId)) : null;
        this._endpointEditId = editId;
        const conn = p?.connection || {};
        const units = (p?.units || []).map(u => u.unit_id).join(', ');
        const lock = editId ? 'disabled' : '';
        const tplOptions = ['<option value="">—</option>'].concat(templates.map(t =>
            `<option value="${this._esc(t.id)}" ${p?.template === t.id ? 'selected' : ''}>${this._esc(t.name || t.id)}</option>`)).join('');
        document.getElementById('endpointModalTitle').textContent = editId
            ? this.t('endpoints.titleEdit', 'Edit Endpoint') : this.t('endpoints.titleAdd', 'Add Endpoint');
        document.getElementById('endpointModalBody').innerHTML = `
            <p class="field-hint" style="margin:0 0 12px;">${this.t('endpoints.intro',
                'One template + one endpoint + several unit IDs. Each unit becomes its own device (own socket, independent failure) named <endpoint>-u<unit>.')}</p>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="plId">${this.t('endpoints.id', 'Endpoint ID')}</label>
                    <input id="plId" class="input" value="${this._esc(p?.id || '')}" ${editId ? 'disabled' : ''} placeholder="fronius"></div>
                <div class="form-group flex-2"><label class="form-label" for="plName">${this.t('endpoints.name', 'Name')}</label>
                    <input id="plName" class="input" value="${this._esc(p?.name || '')}" placeholder="Fronius PV"></div>
            </div>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="plProto">${this.t('devices.wizard.protocol', 'Protocol')}</label>
                    <select id="plProto" class="input">
                        <option value="tcp" ${(conn.protocol || 'tcp') === 'tcp' ? 'selected' : ''}>Modbus TCP</option>
                        <option value="rtu-tcp" ${conn.protocol === 'rtu-tcp' ? 'selected' : ''}>Modbus RTU over TCP</option>
                    </select></div>
                <div class="form-group flex-2"><label class="form-label" for="plHost">${this.t('endpoints.host', 'Host')}</label>
                    <input id="plHost" class="input" value="${this._esc(conn.host || '')}" placeholder="192.168.1.50"></div>
                <div class="form-group"><label class="form-label" for="plPort">${this.t('endpoints.port', 'Port')}</label>
                    <input id="plPort" class="input" type="number" value="${conn.port || 502}"></div>
            </div>
            <div class="form-row">
                <div class="form-group flex-2"><label class="form-label" for="plUnits">${this.t('endpoints.unitsLabel', 'Unit IDs')}</label>
                    <input id="plUnits" class="input" value="${this._esc(units)}" placeholder="1, 2, 3, 4">
                    <div class="field-hint">${this.t('endpoints.unitsHint', 'Comma-separated, ranges allowed (1-4).')}
                        ${editId ? this.t('endpoints.unitsKeep', 'Per-unit names are kept — rename a unit on the endpoint page.') : ''}</div></div>
                <div class="form-group flex-2"><label class="form-label" for="plTemplate">${this.t('devices.wizard.template', 'Template')}</label>
                    <select id="plTemplate" class="input" ${lock}>${tplOptions}</select></div>
            </div>
            <div class="form-row">
                <div class="form-group flex-2"><label class="form-label" for="plTopic">${this.t('devices.wizard.topicPrefix', 'MQTT topic prefix')}</label>
                    <input id="plTopic" class="input" ${lock} value="${this._esc((p?.mqtt || {}).topic_prefix || '')}" placeholder="mbg/devices/\${device_id}"></div>
                <div class="form-group"><label class="form-label" for="plBucket">${this.t('devices.wizard.bucket', 'InfluxDB bucket')}</label>
                    <input id="plBucket" class="input" ${lock} value="${this._esc((p?.influxdb || {}).bucket || '')}"></div>
                <div class="form-group"><label class="form-label" for="plTag">${this.t('devices.wizard.deviceTag', 'Influx device tag')}</label>
                    <input id="plTag" class="input" ${lock} value="${this._esc((p?.influxdb || {}).device_tag || '')}" placeholder="inverter_\${unit_id}"></div>
            </div>
            <div class="field-hint" style="margin:-4px 0 8px;">${editId
                ? this.t('endpoints.routingLocked', 'Routing identity is fixed after creation — changing it would re-route every unit and orphan their history and Home Assistant entities.')
                : this.t('endpoints.subHint', 'Use ${unit_id} / ${endpoint_id} in the topic prefix, bucket and tag — substituted per unit.')}</div>
            <div class="form-row" style="gap:20px;flex-wrap:wrap;">
                <label class="form-label" style="display:flex;align-items:center;gap:8px;">
                    <input type="checkbox" id="plMqttEnabled" ${p ? ((p.mqtt || {}).enabled !== false ? 'checked' : '') : 'checked'}>
                    ${this.t('endpoints.mqttEnabled', 'Publish units to MQTT')}</label>
                <label class="form-label" style="display:flex;align-items:center;gap:8px;">
                    <input type="checkbox" id="plInfluxEnabled" ${p ? ((p.influxdb || {}).enabled !== false ? 'checked' : '') : 'checked'}>
                    ${this.t('endpoints.influxEnabled', 'Write units to InfluxDB')}</label>
                <label class="form-label" style="display:flex;align-items:center;gap:8px;">
                    <input type="checkbox" id="plHaDisc" ${p ? ((p.mqtt || {}).ha_discovery ? 'checked' : '') : ''}>
                    ${this.t('devices.wizard.haDiscovery', 'Publish Home Assistant MQTT discovery for this device')}</label>
                <label class="form-label" style="display:flex;align-items:center;gap:8px;">
                    <input type="checkbox" id="plAggregates" ${p ? (p.aggregates_enabled !== false ? 'checked' : '') : 'checked'}>
                    ${this.t('endpoints.aggEnable', 'Publish endpoint totals')}</label>
            </div>
            <label class="form-label" style="display:flex;align-items:center;gap:8px;">
                <input type="checkbox" id="plEnabled" ${p ? (p.enabled ? 'checked' : '') : 'checked'}>
                ${this.t('devices.wizard.enabled', 'Start polling immediately after saving')}</label>`;
        document.getElementById('endpointFeedback').textContent = '';
        this.openModal('endpointModal');
    },

    _parseUnitList(text) {
        const out = [];
        for (const part of String(text || '').split(',')) {
            const s = part.trim();
            if (!s) continue;
            const m = s.match(/^(\d+)\s*-\s*(\d+)$/);
            if (m) {
                for (let i = +m[1]; i <= +m[2] && out.length < 256; i++) out.push(i);
            } else if (/^\d+$/.test(s)) {
                out.push(+s);
            } else {
                return null;                       // invalid token → caller shows error
            }
        }
        return [...new Set(out)];
    },

    async saveEndpoint() {
        const fb = document.getElementById('endpointFeedback');
        const units = this._parseUnitList(document.getElementById('plUnits').value);
        if (!units || !units.length) {
            fb.textContent = this.t('endpoints.badUnits', 'Unit IDs: use numbers, commas and ranges (e.g. 1, 2, 5-8).');
            return;
        }
        const editing = !!this._endpointEditId;
        const body = {
            id: (this._endpointEditId || document.getElementById('plId').value || '').trim().toLowerCase(),
            name: document.getElementById('plName').value.trim(),
            enabled: document.getElementById('plEnabled').checked,
            connection: {
                protocol: document.getElementById('plProto').value,
                host: document.getElementById('plHost').value.trim(),
                port: parseInt(document.getElementById('plPort').value, 10) || 502,
            },
            units,
            aggregates: !!document.getElementById('plAggregates')?.checked,
        };
        body.mqtt = {
            enabled: !!document.getElementById('plMqttEnabled')?.checked,
            ha_discovery: !!document.getElementById('plHaDisc')?.checked,
        };
        body.influxdb = { enabled: !!document.getElementById('plInfluxEnabled')?.checked };
        if (editing) {
            // routing identity + template are locked after creation: the server
            // pins them anyway, but sending the stored values keeps the two
            // sides honest instead of relying on one of them
            const p = this._endpointDetail?.id === body.id ? this._endpointDetail
                : (this._endpoints || []).find(x => x.id === body.id) || {};
            body.template = p.template || '';
        } else {
            body.template = document.getElementById('plTemplate').value;
            const topic = document.getElementById('plTopic').value.trim();
            if (topic) body.mqtt.topic_prefix = topic;
            const bucket = document.getElementById('plBucket').value.trim();
            const tag = document.getElementById('plTag').value.trim();
            if (bucket) body.influxdb.bucket = bucket;
            if (tag) body.influxdb.device_tag = tag;
        }
        const url = editing
            ? `/api/endpoints/${encodeURIComponent(this._endpointEditId)}` : '/api/endpoints';
        const rsp = await fetch(url, {
            method: editing ? 'PUT' : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const d = await rsp.json().catch(() => ({}));
        if (!rsp.ok) {
            const errs = d.detail?.errors || [d.detail || rsp.statusText];
            fb.textContent = Array.isArray(errs) ? errs.join(' · ') : String(errs);
            return;
        }
        this.closeModal('endpointModal');
        this.showToast('success', this.t('endpoints.saved', 'Endpoint saved'),
            (d.devices || []).map(x => x.id).join(', '));
        if (this._endpointDetail && this._endpointDetail.id === body.id
            && document.querySelector('[data-endpoint-page]')) {
            this.openEndpointDetail(body.id);        // stay on the page, redrawn
        } else {
            await this.renderDevicesList();
        }
    },

    async deleteEndpointUi(id) {
        if (!confirm(this.t('endpoints.deleteConfirm',
                'Delete this endpoint and stop all its units? Register selections are kept on disk.') + `\n${id}`)) return;
        const rsp = await fetch(`/api/endpoints/${encodeURIComponent(id)}`, { method: 'DELETE' });
        const d = await rsp.json().catch(() => ({}));
        if (!rsp.ok) {
            const errs = d.detail?.errors || [d.detail || rsp.statusText];
            this.showToast('error', this.t('endpoints.deleteFail', 'Delete failed'),
                Array.isArray(errs) ? errs.join(' · ') : String(errs));
            return;
        }
        this.showToast('success', this.t('endpoints.deleted', 'Endpoint deleted'),
            (d.removed_devices || []).join(', '));
        if (document.querySelector('[data-endpoint-page]')) this.closeEndpointDetail();
        else await this.renderDevicesList();
    },
});
