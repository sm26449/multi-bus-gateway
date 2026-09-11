// plants domain — augments JanitzaMonitor.prototype
// A plant = one template + one endpoint + N unit ids, materialized into N
// devices (managed through the plant; device CRUD refuses them).
Object.assign(JanitzaMonitor.prototype, {

    async renderPlantsList() {
        const card = document.getElementById('plantsCard');
        const el = document.getElementById('plantsList');
        if (!card || !el) return;
        let plants = [];
        try { plants = (await (await fetch('/api/plants')).json()).plants || []; }
        catch (e) { console.error(e); }
        this._plants = plants;
        card.style.display = plants.length ? '' : 'none';
        if (!plants.length) { el.innerHTML = ''; return; }
        el.innerHTML = plants.map(p => {
            const conn = p.connection || {};
            const proto = (conn.protocol || 'tcp') === 'rtu-tcp' ? 'RTU/TCP' : 'TCP';
            const unitChips = (p.units || []).map(u => {
                const dot = u.connected ? 'var(--success,#22c55e)'
                    : (u.enabled && u.running) ? 'var(--warning,#f59e0b)'
                    : 'var(--text-secondary,#8a94a0)';
                return `<span class="dev-chip" title="${this._esc(u.device_id)}">
                    <span class="status-dot" style="--dot:${dot};margin-right:4px;"></span>
                    ${this.t('plants.unit', 'unit')} ${u.unit_id}</span>`;
            }).join(' ');
            return `<div class="device-row" style="cursor:default;">
                <span class="status-dot" style="--dot:${p.online_units ? 'var(--success,#22c55e)' : 'var(--text-secondary,#8a94a0)'}"
                      title="${p.online_units}/${p.total_units} ${this.t('plants.online', 'online')}"></span>
                <div class="device-row-main">
                    <div class="device-row-title">${this._esc(p.name || p.id)}
                        <span class="dev-chip">${this._esc(p.id)}</span></div>
                    <div class="device-row-sub">${proto} · ${this._esc(conn.host || '')}:${conn.port || 502}
                        · ${this._esc(p.template || '—')} · ${p.total_units} ${this.t('plants.units', 'units')}</div>
                    <div class="device-row-routing">${unitChips}</div>
                </div>
                <div class="device-row-stats">${p.enabled === false
                    ? this.t('devices.disabled', 'disabled')
                    : `${p.online_units}/${p.total_units} ${this.t('plants.online', 'online')}`}</div>
                <div class="device-row-actions">
                    <button class="btn btn-ghost btn-sm" ${this._act('openPlantModal', [p.id])} title="${this.t('common.edit', 'Edit')}"><i aria-hidden="true" class="bi bi-pencil"></i></button>
                    <button class="btn btn-ghost btn-sm" ${this._act('deletePlantUi', [p.id])} title="${this.t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-trash"></i></button>
                </div>
            </div>`;
        }).join('');
    },

    async openPlantModal(editId = null) {
        let templates = [];
        try { templates = (await (await fetch('/api/device-templates')).json()).templates || []; }
        catch (e) { console.error(e); }
        const p = editId ? (this._plants || []).find(x => x.id === editId) : null;
        this._plantEditId = editId;
        const conn = p?.connection || {};
        const units = (p?.units || []).map(u => u.unit_id).join(', ');
        const tplOptions = ['<option value="">—</option>'].concat(templates.map(t =>
            `<option value="${this._esc(t.id)}" ${p?.template === t.id ? 'selected' : ''}>${this._esc(t.name || t.id)}</option>`)).join('');
        document.getElementById('plantModalTitle').textContent = editId
            ? this.t('plants.titleEdit', 'Edit Plant') : this.t('plants.titleAdd', 'Add Plant');
        document.getElementById('plantModalBody').innerHTML = `
            <p class="field-hint" style="margin:0 0 12px;">${this.t('plants.intro',
                'One template + one endpoint + several unit IDs. Each unit becomes its own device (own socket, independent failure) named <plant>-u<unit>.')}</p>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="plId">${this.t('plants.id', 'Plant ID')}</label>
                    <input id="plId" class="input" value="${this._esc(p?.id || '')}" ${editId ? 'disabled' : ''} placeholder="fronius"></div>
                <div class="form-group flex-2"><label class="form-label" for="plName">${this.t('plants.name', 'Name')}</label>
                    <input id="plName" class="input" value="${this._esc(p?.name || '')}" placeholder="Fronius PV"></div>
            </div>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="plProto">${this.t('devices.wizard.protocol', 'Protocol')}</label>
                    <select id="plProto" class="input">
                        <option value="tcp" ${(conn.protocol || 'tcp') === 'tcp' ? 'selected' : ''}>Modbus TCP</option>
                        <option value="rtu-tcp" ${conn.protocol === 'rtu-tcp' ? 'selected' : ''}>Modbus RTU over TCP</option>
                    </select></div>
                <div class="form-group flex-2"><label class="form-label" for="plHost">${this.t('plants.host', 'Host')}</label>
                    <input id="plHost" class="input" value="${this._esc(conn.host || '')}" placeholder="192.168.1.50"></div>
                <div class="form-group"><label class="form-label" for="plPort">${this.t('plants.port', 'Port')}</label>
                    <input id="plPort" class="input" type="number" value="${conn.port || 502}"></div>
            </div>
            <div class="form-row">
                <div class="form-group flex-2"><label class="form-label" for="plUnits">${this.t('plants.unitsLabel', 'Unit IDs')}</label>
                    <input id="plUnits" class="input" value="${this._esc(units)}" placeholder="1, 2, 3, 4">
                    <div class="field-hint">${this.t('plants.unitsHint', 'Comma-separated, ranges allowed (1-4).')}</div></div>
                <div class="form-group flex-2"><label class="form-label" for="plTemplate">${this.t('devices.wizard.template', 'Template')}</label>
                    <select id="plTemplate" class="input">${tplOptions}</select></div>
            </div>
            <div class="form-row">
                <div class="form-group flex-2"><label class="form-label" for="plTopic">${this.t('devices.wizard.topicPrefix', 'MQTT topic prefix')}</label>
                    <input id="plTopic" class="input" value="${this._esc((p?.mqtt || {}).topic_prefix || '')}" placeholder="mbg/fronius/inverter/\${unit_id}"></div>
                <div class="form-group"><label class="form-label" for="plBucket">${this.t('devices.wizard.bucket', 'InfluxDB bucket')}</label>
                    <input id="plBucket" class="input" value="${this._esc((p?.influxdb || {}).bucket || '')}"></div>
                <div class="form-group"><label class="form-label" for="plTag">${this.t('devices.wizard.deviceTag', 'Influx device tag')}</label>
                    <input id="plTag" class="input" value="${this._esc((p?.influxdb || {}).device_tag || '')}" placeholder="inverter_\${unit_id}"></div>
            </div>
            <div class="field-hint" style="margin:-4px 0 8px;">${this.t('plants.subHint',
                'Use \${unit_id} / \${plant_id} in the topic prefix, bucket and tag — substituted per unit.')}</div>
            <label class="form-label" style="display:flex;align-items:center;gap:8px;">
                <input type="checkbox" id="plEnabled" ${p ? (p.enabled ? 'checked' : '') : 'checked'}>
                ${this.t('devices.wizard.enabled', 'Start polling immediately after saving')}</label>`;
        document.getElementById('plantFeedback').textContent = '';
        this.openModal('plantModal');
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

    async savePlant() {
        const fb = document.getElementById('plantFeedback');
        const units = this._parseUnitList(document.getElementById('plUnits').value);
        if (!units || !units.length) {
            fb.textContent = this.t('plants.badUnits', 'Unit IDs: use numbers, commas and ranges (e.g. 1, 2, 5-8).');
            return;
        }
        const body = {
            id: (this._plantEditId || document.getElementById('plId').value || '').trim().toLowerCase(),
            name: document.getElementById('plName').value.trim(),
            template: document.getElementById('plTemplate').value,
            enabled: document.getElementById('plEnabled').checked,
            connection: {
                protocol: document.getElementById('plProto').value,
                host: document.getElementById('plHost').value.trim(),
                port: parseInt(document.getElementById('plPort').value, 10) || 502,
            },
            units,
        };
        const topic = document.getElementById('plTopic').value.trim();
        if (topic) body.mqtt = { topic_prefix: topic };
        const bucket = document.getElementById('plBucket').value.trim();
        const tag = document.getElementById('plTag').value.trim();
        if (bucket || tag) {
            body.influxdb = {};
            if (bucket) body.influxdb.bucket = bucket;
            if (tag) body.influxdb.device_tag = tag;
        }
        const url = this._plantEditId
            ? `/api/plants/${encodeURIComponent(this._plantEditId)}` : '/api/plants';
        const rsp = await fetch(url, {
            method: this._plantEditId ? 'PUT' : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const d = await rsp.json().catch(() => ({}));
        if (!rsp.ok) {
            const errs = d.detail?.errors || [d.detail || rsp.statusText];
            fb.textContent = Array.isArray(errs) ? errs.join(' · ') : String(errs);
            return;
        }
        this.closeModal('plantModal');
        this.showToast('success', this.t('plants.saved', 'Plant saved'),
            (d.devices || []).map(x => x.id).join(', '));
        await this.renderPlantsList();
        await this.renderDevicesList();
    },

    async deletePlantUi(id) {
        if (!confirm(this.t('plants.deleteConfirm',
                'Delete this plant and stop all its units? Register selections are kept on disk.') + `\n${id}`)) return;
        const rsp = await fetch(`/api/plants/${encodeURIComponent(id)}`, { method: 'DELETE' });
        const d = await rsp.json().catch(() => ({}));
        if (!rsp.ok) {
            const errs = d.detail?.errors || [d.detail || rsp.statusText];
            this.showToast('error', this.t('plants.deleteFail', 'Delete failed'),
                Array.isArray(errs) ? errs.join(' · ') : String(errs));
            return;
        }
        this.showToast('success', this.t('plants.deleted', 'Plant deleted'),
            (d.removed_devices || []).join(', '));
        await this.renderPlantsList();
        await this.renderDevicesList();
    },
});
