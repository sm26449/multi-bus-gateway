/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// devices domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    // Devices page: show the list, hide the detail + register-editor overlays.
    _showDevicesList() {
        this._view = { type: 'list' };    // what the address names (app-router.js)
        this._leaveDeviceDetail();        // a device page hidden here stops what it started
        if (this.updatePollGroupsStatus) this.updatePollGroupsStatus();
        if (this._stopEndpointDetail) this._stopEndpointDetail();
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
        this._pickDevice(sel);
    },

    _fetchDevices(force = false) {
        if (this._devices && !force) return Promise.resolve(this._devices);
        // several callers in one step (a link opening a device page) share
        // the request in flight — never one started before a write
        const gen = this._writeGen || 0;
        if (this._devFetch && this._devFetch.gen === gen) return this._devFetch.p;
        const p = this._fetchDevicesNow().finally(() => { if (this._devFetch?.p === p) this._devFetch = null; });
        this._devFetch = { gen, p };
        return p;
    },

    async _fetchDevicesNow() {
        try {
            const r = await fetch('/api/devices');
            this._devices = (await r.json()).devices || [];
            // the dashboard device is remembered across visits; one that has
            // since been deleted (or renamed) must not be restored blindly
            if (this.dashDevice && !this._devices.some(d => d.id === this.dashDevice)) {
                this.dashDevice = null;
                try { localStorage.removeItem('mbg-dash-device'); } catch (e2) { /* private mode */ }
            }
        } catch (e) { console.error('devices load failed:', e); this._devices = this._devices || []; }
        return this._devices;
    },

    async renderDevicesList() {
        const el = document.getElementById('devicesList');
        if (!el) return;
        // the three lists the page draws, asked together
        const [devices, endpoints, bridges] = await Promise.all([
            this._fetchDevices(true),
            fetch('/api/endpoints').then(r => r.json()).then(d => d.endpoints || [])
                .catch(e => { console.error(e); return []; }),
            this._loadBridges ? this._loadBridges() : Promise.resolve([]),
        ]);
        this._renderRegDeviceSelectors();
        this._renderRestorableDevices();          // deleted-but-restorable list
        this._renderFoundOnMqtt();                // announced over MQTT, to adopt
        this._endpoints = endpoints;
        const findHost = document.getElementById('devFind');
        if (!devices.length && !endpoints.length && !bridges.length) {
            el.innerHTML = `<span class="field-hint">${this.t('devices.none', 'No devices configured.')}</span>`;
            if (findHost) findHost.hidden = true;
            return;
        }
        if (findHost) findHost.hidden = false;
        const healthColor = { ok: 'var(--success,#22c55e)', degraded: 'var(--warning,#f59e0b)',
                              down: 'var(--danger,#ef4444)', idle: 'var(--text-secondary,var(--text-secondary))' };
        // Devices belonging to an endpoint render NESTED under their endpoint group
        // (expandable); only standalone devices render as top-level rows.
        // a device on a bridge is listed under its bus (an installation's unit
        // stays under its installation, the bridge only names it)
        // a device's components are listed under it, never on their own
        const standalone = devices.filter(d => !d.endpoint_id && !d.bridge && !d.component_of);
        const byEndpoint = {}, byParent = {};
        devices.forEach(d => { if (d.endpoint_id) (byEndpoint[d.endpoint_id] ||= []).push(d); });
        devices.forEach(d => { if (d.component_of) (byParent[d.component_of] ||= []).push(d); });
        const html = (this._bridgesSectionHtml ? this._bridgesSectionHtml(bridges, healthColor) : '')
            + standalone.map(d => this._deviceRowHtml(d, healthColor)
                + this._componentChildrenHtml(d, byParent[d.id] || [], healthColor)).join('')
            + endpoints.map(p => this._endpointGroupHtml(p, byEndpoint[p.id] || [], healthColor)).join('');
        el.innerHTML = html;
        this._wireComponentToggles(el);
        // search, filters and the list view (app-find.js) over what was drawn
        if (this._devFindIndex) {
            this._devFindIndex(devices, endpoints, bridges);
            this._devFindAfterRender();
        }
        // "used by N": the last known counts at once, the gateway's when they
        // arrive — the list never waits for them
        this._ubPaintList(el);
        this._ubCountsLater(() => this._ubPaintList(document.getElementById('devicesList')));
        // expand/collapse wiring (state persists per endpoint in localStorage)
        el.querySelectorAll('[data-endpoint-toggle]').forEach(h =>
            h.addEventListener('click', (ev) => {
                if (ev.target.closest('.device-row-actions, .device-row-name')) return;
                const pid = h.dataset.endpointToggle;
                const box = el.querySelector(`[data-endpoint-units="${CSS.escape(pid)}"]`);
                const open = box ? box.style.display === 'none' : !this._endpointOpenState(pid);
                try { localStorage.setItem(`mbg-endpoint-open-${pid}`, open ? '1' : '0'); } catch (e) { /* private mode */ }
                const btn = h.querySelector('[data-endpoint-disclose]');
                if (this._findDisclose) this._findDisclose(btn, box, open);
            }));
    },

    _endpointOpenState(pid) {
        try { return localStorage.getItem(`mbg-endpoint-open-${pid}`) !== '0'; }
        catch (e) { return true; }
    },

    // One endpoint as an expandable GROUP: header row (identity + unit census +
    // endpoint actions) + its unit devices nested underneath.
    _endpointGroupHtml(p, units, healthColor) {
        const open = this._endpointOpenState(p.id);
        const online = units.filter(u => u.connected).length;
        // the template's first headline total (a bank's power) when declared,
        // else the installation's power now — one scaling rule everywhere
        const h0 = (p.headline_items || [])[0];
        const pw = h0 ? h0.value : ((p.headline || {}).power_now ?? p.aggregates?.power_active_total);
        const unit = h0 ? (h0.unit || '') : 'W';
        let pwTxt = '';
        if (typeof pw === 'number') {
            const f = this.formatValueWithUnit(pw, unit);
            pwTxt = `${h0 ? this._esc(h0.label) + ' ' : ''}${this._fmtNum(f.value, f.decimals)} ${this._esc(f.unit)}${h0 ? '' : ' ' + this.t('devices.now', 'now')} · `;
        }
        const stats = p.enabled === false
            ? this.t('devices.disabled', 'disabled')
            : `${pwTxt}${online}/${units.length} ${this.t('endpoints.answeringShort', 'answering')}`;
        // what it holds and how it is read — not the fallback connection
        const ways = [...new Set((p.read_via || []).map(r => r.id))];
        const holds = (p.groups || []).map(g => this._groupLabel ? this._groupLabel(g) : g.id);
        const dot = p.enabled === false ? 'var(--text-secondary,#8a94a0)'
            : online === units.length && units.length ? 'var(--success,#22c55e)'
            : online ? 'var(--warning,#f59e0b)' : 'var(--danger,#ef4444)';
        const rows = units.map(d => this._deviceRowHtml(d, healthColor, true)).join('')
            || `<div class="field-hint" style="margin:6px 0 6px 34px;">${this.t('endpoints.noUnits', 'No units materialized.')}</div>`;
        return `
        <div class="device-row endpoint-row" data-endpoint-toggle="${this._esc(p.id)}">
            <button type="button" class="btn btn-ghost btn-sm disclose-btn" data-endpoint-disclose
                    aria-expanded="${open}" aria-controls="epUnits-${this._esc(p.id)}"
                    ${this._ib(this.t('endpoints.toggle', 'Show or hide the installation\'s units'), p.name || p.id)}><i aria-hidden="true" class="bi bi-chevron-right endpoint-chevron"
               style="transition:transform .15s;${open ? 'transform:rotate(90deg);' : ''}"></i></button>
            <span class="status-dot" style="--dot:${dot}" title="${this._esc(stats)}"></span>
            <div class="device-row-main">
                <div class="device-row-title"><i aria-hidden="true" class="bi bi-diagram-3"></i> <a class="device-row-name" href="#/installations/${encodeURIComponent(p.id)}">${this._esc(p.name || p.id)}</a>
                    <span class="dev-chip">${this._esc(p.id)}</span>${this._ubSlotHtml(p.id, 'installation')}
                    <span class="dev-chip">${units.length} ${units.length === 1 ? this.t('endpoints.unit', 'unit') : this.t('endpoints.units', 'units')}</span>
                    ${((p.template_update || {}).missing_templates || []).length ? `<span class="sink-pill bad" title="${this._esc(this.t('tplu.missingTitle', 'Template not available'))}"><i aria-hidden="true" class="bi bi-exclamation-triangle"></i> ${this._esc(this.t('tplu.missingBadge', 'template missing'))}</span>` : ''}
                    ${(p.template_update || {}).pending ? `<span class="sink-pill warn" title="${this._esc(this.t('tplu.noticeTitle', 'The template has been updated'))}"><i aria-hidden="true" class="bi bi-stars"></i> ${this._esc(this.t('tplu.badge', 'template update'))}</span>` : ''}</div>
                <div class="device-row-sub">${this._esc(holds.join(' · ') || p.template || '')}${ways.length ? ` · ${this.t('endpoints.readVia', 'Read via')} ${this._esc(ways.join(' + '))}` : ''}</div>
            </div>
            <div class="device-row-stats">${stats}</div>
            <div class="device-row-actions">
                <button class="btn btn-ghost btn-sm" ${this._act('openEndpointDetail', [p.id])} ${this._ib(this.t('endpoints.open', 'Open the installation'), p.name || p.id)}><i aria-hidden="true" class="bi bi-box-arrow-up-right"></i></button>
                <button data-admin class="btn btn-ghost btn-sm" ${this._act('openEndpointModal', [p.id])} ${this._ib(this.t('common.edit', 'Edit'), p.name || p.id)}><i aria-hidden="true" class="bi bi-pencil"></i></button>
                <button data-admin class="btn btn-ghost btn-sm" data-ub-del="${this._esc(p.id)}" data-ub-name="${this._esc(p.name || p.id)}" ${this._act('deleteEndpointUi', [p.id])} ${this._ib(this.t('common.delete', 'Delete'), p.name || p.id)}><i aria-hidden="true" class="bi bi-trash"></i></button>
            </div>
        </div>
        <div data-endpoint-units="${this._esc(p.id)}" id="epUnits-${this._esc(p.id)}" style="display:${open ? '' : 'none'};
             margin-left:22px;border-left:2px solid var(--border,#e5e7eb);padding-left:8px;">
            ${rows}
        </div>`;
    },

    _deviceRowHtml(d, healthColor, nested = false) {
        {
            const PROTO = this._PROTO_LABEL;
            const viaUnits = (d.read_via || []).length;
            const reach = this._reachWords(d);
            const proto = viaUnits
                ? d.read_via.map(r => `${this._esc(r.id)} ${PROTO[r.protocol] || this._esc(r.protocol)}${r.interval_s != null && r.protocol !== 'rtu_tap' ? ` ${r.interval_s} s` : ''}`).join(' · ')
                : `${this._esc(reach.proto)} · ${this._esc(reach.address)}`;
            const stats = this._deviceStatsWords(d, reach);
            const actions = [
                `<button class="btn btn-ghost btn-sm" ${this._act('jumpToDeviceRegisters', [d.id])} ${this._ib(this.t('devices.registers', 'Measurements'), d.name || d.id)}><i aria-hidden="true" class="bi bi-list-check"></i></button>`,
                `<button data-live class="btn btn-ghost btn-sm" ${this._act('testDevice', [d.id], {el: true})} ${this._ib(this.t('devices.test', 'Test read'), d.name || d.id)}><i aria-hidden="true" class="bi bi-activity"></i></button>`,
            ];
            actions.push(`<button class="btn btn-ghost btn-sm" ${this._act('openDeviceDetail', [d.id])} ${this._ib(this.t('common.open', 'Open'), d.name || d.id)}><i aria-hidden="true" class="bi bi-box-arrow-up-right"></i></button>`);
            if (!d.primary && !d.endpoint_id && !d.component_of) {
                // held back while in use (_ubPaintList): still reachable, it shows what uses it
                actions.push(`<button data-admin class="btn btn-ghost btn-sm" data-ub-del="${this._esc(d.id)}" data-ub-name="${this._esc(d.name || d.id)}" ${this._act('deleteDevice', [d.id])} ${this._ib(this.t('common.delete', 'Delete'), d.name || d.id)}><i aria-hidden="true" class="bi bi-trash"></i></button>`);
            }
            // nested rows sit under their endpoint group — the chip would repeat it
            const endpointChip = (d.endpoint_id && !nested)
                ? ` <span class="dev-chip" title="${this.t('devices.endpointManaged', 'Managed through its installation — edit or delete the installation')}"><i aria-hidden="true" class="bi bi-diagram-3"></i> ${this._esc(d.endpoint_id)}</span>`
                : '';
            // The row (outside its action buttons) opens the full-page detail;
            // data-guard keeps clicks inside the action-buttons cell from bubbling
            // into the row action, data-key-enter keeps Enter working on the div.
            return `<div class="device-row" data-f="${this._esc(d.id)}"
                 ${this._act('openDeviceDetail', [d.id], {guard: '.device-row-actions, .device-row-name'})}>
                <span class="status-dot" style="--dot:${healthColor[d.data_health] || healthColor.idle}"
                      title="${this._esc(this._verdictWord(d))}"></span>
                <div class="device-row-main">
                    <div class="device-row-title">${this._kindIconHtml ? this._kindIconHtml(d.kind) : ''}<a class="device-row-name" href="#/devices/${encodeURIComponent(d.id)}">${this._esc(d.name || d.id)}</a>
                        <span class="dev-chip">${this._esc(d.id)}</span>${endpointChip}${this._ubSlotHtml(d.id)}</div>
                    <div class="device-row-sub">${proto}${viaUnits ? '' : `${reach.unit != null ? ` · unit ${reach.unit}` : ''} · ${this._esc(d.template || '—')}`} · ${d.selected_registers ?? 0} ${this.t('devices.regsSelected', 'measurements')}</div>
                </div>
                <div class="device-row-stats">${stats}</div>
                <div class="device-row-actions">${actions.join('')}</div>
            </div>`;
        }
    },

    // Deleted devices whose settings were kept — restore rebuilds the exact
    // device (connection + template + register selection), forget drops it.
    // ── devices that announce themselves over MQTT (docs/mqtt-discovery.md) ──
    async _renderFoundOnMqtt() {
        const box = document.getElementById('foundOnMqtt');
        if (!box) return;
        let d;
        try { d = await (await fetch('/api/mqtt-discovery')).json(); } catch (e) { box.innerHTML = ''; return; }
        const items = d.items || [];
        const show = items.filter(x => x.state === 'new' || x.state === 'template_missing' || (this._showIgnored && x.state === 'ignored'));
        const inMbg = items.filter(x => x.state === 'in_mbg').length;
        const ignored = items.filter(x => x.state === 'ignored').length;
        if (!show.length && !ignored) { box.innerHTML = ''; return; }
        const nodes = d.nodes_url ? `<a href="${this._esc(d.nodes_url)}">${this.t('found.nodesPage', 'the ESP nodes page')}</a>` : this.t('found.nodesPage', 'the ESP nodes page');
        box.innerHTML = `
            <div class="settings-card devices-card" style="margin-top:16px;">
                <div class="settings-card-header">
                    <h3><i aria-hidden="true" class="bi bi-broadcast"></i> ${this.t('found.title', 'Found on MQTT')} <span class="dev-chip">${show.filter(x => x.state !== 'ignored').length}</span></h3>
                </div>
                <div class="settings-card-body">
                    <p class="field-hint" style="margin:0 0 10px;">${this.t('found.hint',
                        'Devices announcing themselves on the gateway\'s broker (ESP nodes, Tasmota). Nothing is added by itself: Adopt makes a device, off, read with its template.')}</p>
                    ${show.map(x => `
                    <div class="device-row" style="cursor:default;">
                        <span class="status-dot" style="--dot:var(--text-secondary,#8a94a0)"></span>
                        <div class="device-row-main">
                            <div class="device-row-title">${this._esc(x.name)} <span class="dev-chip">${this._esc(x.kind)}</span></div>
                            <div class="device-row-sub">${this._esc(x.connection.topic)} · ${this._esc(x.template)}${Object.entries(x.info || {}).filter(([, v]) => v !== null && v !== '' && v !== undefined).map(([k, v]) => ` · ${this._esc(k)} ${this._esc(v)}`).join('')}
                            ${x.state === 'template_missing' ? `<br><span class="text-warning">${this.t('found.noTemplate', 'Its template is not here yet')}${x.rule === 'mbg_node' ? `: ${this.t('found.sendFromNodes', 'send the node to MBG from')} ${nodes}` : ''}.</span>` : ''}</div>
                        </div>
                        <div class="device-row-actions">
                            ${x.state === 'new' ? `<button data-admin class="btn btn-sm btn-primary" data-adopt="${this._esc(x.key)}"><i aria-hidden="true" class="bi bi-plus-lg"></i> ${this.t('found.adopt', 'Adopt')}</button>` : ''}
                            ${`<button data-admin class="btn btn-ghost btn-sm" data-ignore="${this._esc(x.key)}" data-on="${x.state === 'ignored' ? '0' : '1'}">${x.state === 'ignored' ? this.t('found.unignore', 'Show again') : this.t('found.ignore', 'Ignore')}</button>`}
                        </div>
                    </div>`).join('')}
                    <p class="field-hint" style="margin:10px 0 0;">${inMbg} ${this.t('found.inMbg', 'already in the gateway')} · ${ignored} ${this.t('found.ignored', 'ignored')}
                        ${ignored ? `<button class="btn btn-ghost btn-sm" id="foundToggleIgnored">${this._showIgnored ? this.t('found.hideIgnored', 'hide them') : this.t('found.showIgnored', 'show them')}</button>` : ''}
                        ${d.error ? ` · <span class="text-danger">${this._esc(d.error)}</span>` : ''}</p>
                </div>
            </div>`;
        box.querySelectorAll('button[data-adopt]').forEach(b => b.addEventListener('click', async () => {
            b.disabled = true;
            try {
                const r = await fetch(`/api/mqtt-discovery/${encodeURIComponent(b.dataset.adopt)}/adopt`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
                const j = await r.json();
                if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail));
                this.showToast('success', this.t('found.title', 'Found on MQTT'),
                    `${j.device_id}: ${this.t('found.adopted', 'added, off: turn it on when ready')}`);
                await this._fetchDevices(true);
                this.renderDevicesList();
            } catch (e) {
                b.disabled = false;
                this.showToast('error', this.t('found.title', 'Found on MQTT'), String(e.message || e));
            }
        }));
        box.querySelectorAll('button[data-ignore]').forEach(b => b.addEventListener('click', async () => {
            await fetch(`/api/mqtt-discovery/${encodeURIComponent(b.dataset.ignore)}/ignore`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ignore: b.dataset.on === '1' }) });
            this._renderFoundOnMqtt();
        }));
        const tg = box.querySelector('#foundToggleIgnored');
        if (tg) tg.addEventListener('click', () => { this._showIgnored = !this._showIgnored; this._renderFoundOnMqtt(); });
    },

    async _renderRestorableDevices() {
        const box = document.getElementById('restorableDevices');
        if (!box) return;
        let items = [];
        try {
            items = (await (await fetch('/api/devices/restorable')).json()).devices || [];
        } catch (e) {
            // an error must not look identical to "nothing to restore"
            box.innerHTML = `<div class="hint-text text-danger">${this._esc(this.t('devices.restorableLoadFail', 'Could not load restorable devices.'))}</div>`;
            return;
        }
        if (!items.length) { box.innerHTML = ''; return; }
        const when = ts => ts ? new Date(ts * 1000).toLocaleDateString(this._loc()) : '';
        // folded under the list, its count in the summary (app-find.js shows
        // the ones a search matches)
        box.innerHTML = `
            <details class="settings-card devices-card restorable-card" style="margin-top:16px;">
                <summary class="settings-card-header">
                    <h3><i aria-hidden="true" class="bi bi-chevron-right restorable-chevron"></i> <i aria-hidden="true" class="bi bi-arrow-counterclockwise"></i> ${this.t('devices.restorable', 'Deleted devices (restorable)')} <span class="dev-chip">${items.length}</span></h3>
                </summary>
                <div class="settings-card-body">
                    <p class="field-hint" style="margin:0 0 10px;">${this.t('devices.restorableHint',
                        'Settings kept when these were deleted. Restore rebuilds the exact device; Forget drops them for good.')}</p>
                    ${items.map(d => `
                    <div class="device-row" style="cursor:default;" data-restorable="${this._esc(d.id)}" data-text="${this._esc(this._fold ? this._fold(`${d.id} ${d.name} ${d.template || ''} ${d.protocol || ''}`) : '')}">
                        <span class="status-dot" style="--dot:var(--text-secondary,#8a94a0)"></span>
                        <div class="device-row-main">
                            <div class="device-row-title">${this._esc(d.name)} <span class="dev-chip">${this._esc(d.id)}</span></div>
                            <div class="device-row-sub">${this._esc(this._protoLabel(d.protocol || ''))} · ${this._esc(d.template || '—')} · ${d.registers} ${this.t('devices.regsSelected', 'measurements')}${d.deleted_ts ? ` · ${this.t('devices.deletedOn', 'deleted')} ${when(d.deleted_ts)}` : ''}</div>
                        </div>
                        <div class="device-row-actions">
                            <button data-admin class="btn btn-ghost btn-sm" data-restore="${this._esc(d.id)}"><i aria-hidden="true" class="bi bi-arrow-counterclockwise"></i> ${this.t('devices.restore', 'Restore')}</button>
                            <button data-admin class="btn btn-ghost btn-sm" data-forget="${this._esc(d.id)}" ${this._ib(this.t('devices.forget', 'Forget'), d.name || d.id)}><i aria-hidden="true" class="bi bi-trash"></i></button>
                        </div>
                    </div>`).join('')}
                </div>
            </details>`;
        if (this._devFindRestorable) this._devFindRestorable(this._findFiltering(this._findState('devices')));
        box.querySelectorAll('button[data-restore]').forEach(b =>
            b.addEventListener('click', () => this._restoreDevice(b.dataset.restore)));
        box.querySelectorAll('button[data-forget]').forEach(b =>
            b.addEventListener('click', () => this._forgetDevice(b.dataset.forget)));
    },

    async _restoreDevice(id) {
        const rsp = await fetch(`/api/devices/${encodeURIComponent(id)}/restore`, { method: 'POST' });
        const d = await rsp.json().catch(() => ({}));
        if (rsp.ok) {
            this.showToast('success', this.t('devices.restored', 'Device restored'), id);
            this.renderDevicesList();
        } else {
            this.showToast('error', 'Restore', this._errMsg((d.detail && d.detail.errors) ? d.detail.errors.join('; ') : (d.detail ?? rsp.status)));
        }
    },

    async _forgetDevice(id) {
        if (!await this.confirmAction({
            title: this.t('devices.forgetTitle', 'Forget {id}', { id }),
            message: this.t('devices.forgetConfirm', 'Its kept settings are deleted for good: it can no longer be restored.'),
            confirmLabel: this.t('devices.forget', 'Forget'), danger: true,
        })) return;
        const rsp = await fetch(`/api/devices/restorable/${encodeURIComponent(id)}`, { method: 'DELETE' });
        if (rsp.ok) {
            this.showToast('success', this.t('devices.forgotten', 'Settings forgotten'), id);
            this._renderRestorableDevices();
        } else {
            const d = await rsp.json().catch(() => ({}));
            this.showToast('error', 'Forget', this._errMsg(d.detail ?? rsp.status));
        }
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
        const eg = document.getElementById('discoverEsphomeGroup');
        if (fg) fg.style.display = method === 'fronius' ? '' : 'none';
        if (mg) mg.style.display = method === 'modbus' ? '' : 'none';
        if (eg) eg.style.display = method === 'esphome' ? '' : 'none';
        // the ESPHome sweep reuses the Modbus CIDR guess
        const src = document.getElementById('discoverCidr');
        const dst = document.getElementById('discoverEspCidr');
        if (method === 'esphome' && src && dst && !dst.value) dst.value = src.value;
    },

    async runDiscover(btn) {
        const method = document.getElementById('discoverMethod')?.value || 'modbus';
        const box = document.getElementById('discoverResult');
        const busy = () => { if (box) box.innerHTML = `<div class="settings-card" style="padding:12px;color:var(--text-secondary);"><i aria-hidden="true" class="bi bi-arrow-repeat spin"></i> ${this.t('devices.scanning', 'Scanning…')}</div>`; };
        const err = (m) => { if (box) box.innerHTML = `<div class="settings-card" style="padding:12px;color:var(--danger-text,#c0392b);">${this._esc(m)}</div>`; };
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<i aria-hidden="true" class="bi bi-arrow-repeat spin"></i>'; }
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
            } else if (method === 'esphome') {
                const cidr = (document.getElementById('discoverEspCidr')?.value || '').trim();
                if (!cidr) { err(this.t('devices.discoverNoCidr', 'Enter a network range (CIDR) to scan.')); return; }
                busy();
                const r = await fetch('/api/discover/esphome', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        cidr,
                        port: parseInt(document.getElementById('discoverEspPort')?.value) || 6053,
                    }) });
                const d = await r.json();
                if (!r.ok) { err(d.detail?.errors?.join(' · ') || 'scan failed'); return; }
                this._renderEsphomeScan(d);
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
            box.innerHTML = `<div class="settings-card" style="padding:12px;color:var(--text-secondary);">${this.tn('devices.scanNone', d.scanned, 'No devices answered on {n} hosts.')}</div>`;
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
                    <button data-live class="btn btn-ghost btn-sm" ${this._act('sweepModbusUnits', [r.host, r.port], {el: true})} ${this._ib(this.t('devices.findUnits', 'Find unit ids'), `${r.host}:${r.port}`)}><i aria-hidden="true" class="bi bi-diagram-3"></i></button>
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('useDiscoveredModbus', [r.host, r.port, r.unit_id])}>${this.t('devices.add', 'Add Device')}</button>
                </td></tr>
                <tr><td colspan="3" style="padding:0;"><div class="disc-units" id="discUnits-${this._esc(r.host).replace(/\./g, '_')}"></div></td></tr>`;
        }).join('');
        box.innerHTML = `<div class="settings-card" style="padding:14px;">
            <div style="margin-bottom:8px;"><b><i aria-hidden="true" class="bi bi-hdd-network"></i> ${results.length} ${this.t('devices.scanFound', 'device(s) found')}</b> <span style="color:var(--text-secondary);">· ${d.scanned} ${this.t('devices.scanScanned', 'scanned')}</span></div>
            <table>${rows}</table></div>`;
    },

    // ESPHome nodes answering on the native-API port. Flashing and adopting
    // them is the companion nodes service's job (ui.nodes_url), not this one.
    _renderEsphomeScan(d) {
        const box = document.getElementById('discoverResult');
        if (!box) return;
        const results = d.results || [];
        if (!results.length) {
            box.innerHTML = `<div class="settings-card" style="padding:12px;color:var(--text-secondary);">${this.tn('devices.scanNone', d.scanned, 'No devices answered on {n} hosts.')}</div>`;
            return;
        }
        box.innerHTML = `<div class="settings-card" style="padding:6px 12px;">` + results.map(r => {
            const name = r.name || this.t('devices.espUnknown', 'ESPHome device');
            const chip = r.encrypted
                ? ` <span class="dev-chip" title="${this.t('devices.espEncryptedTip', 'Native API uses encryption — identity not readable, but the device is ESPHome')}">${this.t('devices.espEncrypted', 'encrypted API')}</span>`
                : '';
            return `<div class="device-row" style="cursor:default;">
                <span class="status-dot" style="--dot:var(--success,#22c55e)"></span>
                <div class="device-row-main">
                    <div class="device-row-title">${this._esc(name)}${chip}</div>
                    <div class="device-row-sub">${this._esc(r.host)}:${r.port}${r.api_version ? ` · API ${this._esc(r.api_version)}` : ''}${r.server_info ? ` · ${this._esc(r.server_info)}` : ''}</div>
                </div>
            </div>`;
        }).join('') + `</div>
        <div class="field-hint" style="margin-top:6px;">${this.tn('devices.scanHosts', d.scanned, '{n} hosts.')} · ${d.elapsed_s}s</div>`;
    },

    async sweepModbusUnits(host, port, btn) {
        const cell = document.getElementById('discUnits-' + host.replace(/\./g, '_'));
        if (cell) cell.innerHTML = `<span class="field-hint"><i aria-hidden="true" class="bi bi-arrow-repeat spin"></i> ${this.t('devices.scanning', 'Scanning…')}</span>`;
        try {
            const r = await fetch('/api/discover/modbus/units', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ protocol: 'tcp', host, port, unit_start: 1, unit_end: 32 }) });
            const d = await r.json();
            const units = d.units || [];
            if (!cell) return;
            cell.innerHTML = units.length
                ? `<span class="field-hint">${this.t('devices.unitsFound', 'Unit ids:')}</span> ` + units.map(u =>
                    `<button data-admin class="calc-chip" ${this._act('useDiscoveredModbus', [host, port, u])}>${u}</button>`).join(' ')
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
            <td style="padding:3px 12px 3px 0;color:var(--text-secondary);">${this._esc(this.t('devices.meterIds', 'Solar API id {sid} · Modbus unit {unit}', { sid: m.solar_api_id, unit: m.modbus_unit_hint }))}</td>
            <td style="padding:3px 12px 3px 0;font-variant-numeric:tabular-nums;">${m.power_w != null ? Math.round(m.power_w) + ' W' : '—'} · ${m.freq_hz != null ? m.freq_hz + ' Hz' : ''}</td>
            <td style="padding:3px 0;"><button data-admin class="btn btn-ghost btn-sm" ${this._act('addDiscoveredMeter', [i])}>${this.t('devices.add', 'Add Device')}</button></td></tr>`).join('');
        const invRows = (d.inverters || []).map(i =>
            `<tr><td style="padding:2px 12px 2px 0;">${this.t('lbl.inverter', 'Inverter')}</td><td style="padding:2px 12px 2px 0;color:var(--text-secondary);">id ${this._esc(i.solar_api_id)} · DT ${i.dt} · SN ${this._esc(i.serial || '')}</td></tr>`).join('');
        box.innerHTML = `<div class="settings-card" style="padding:14px;">
            <div style="margin-bottom:8px;"><b><i aria-hidden="true" class="bi bi-hdd-network"></i> ${this.t('devices.discoverTitle', 'Devices behind')} ${this._esc(host)}</b></div>
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
        // (another device opens right after: the one left open is not restarted)
        if (this.currentPage !== 'devices') this.navigateTo('devices', { _opening: true });
        this._showDevicesList();        // also stops an endpoint page's live tick
        await this._fetchDevices(true);
        const dev = (this._devices || []).find(d => d.id === id);
        if (!dev) { this.showToast('error', this.t('devices.notFound', 'Device not found'), id); return false; }
        let templates = [];
        try { templates = (await (await fetch('/api/device-templates')).json()).templates || []; }
        catch (e) { console.error(e); }
        if (this._loadBridges) await this._loadBridges();   // the buses it may move to (or onto)
        const c = dev.connection || {};
        this._devDetail = {
            id, primary: !!dev.primary, endpoint_id: dev.endpoint_id || '',
            // acquisition runs for it (an enabled device that does not is
            // revived by sending its settings again: Reconnect)
            running: dev.running !== false,
            endpoint_name: dev.endpoint_name || '', templates,
            // a component: managed and reached through its parent
            component_of: dev.component_of || '', component_type: dev.component_type || '',
            component_type_name: dev.component_type_name || dev.component_type || '',
            component_instance: dev.component_instance || '',
            parent_name: dev.component_of ? ((this._devices || []).find(x => x.id === dev.component_of) || {}).name || '' : '',
            data: {
                // name, template, enabled and outputs: as the model reads them
                // (the PUT it builds is pinned by tests/test_connform_payloads.py)
                ...window.ConnformModel.pageFields({ ...dev, id }),
                protocol: dev.protocol || 'tcp',
                host: c.host || dev.host || '', port: c.port || dev.port || 502,
                unit_id: c.unit_id ?? dev.unit_id ?? 1, timeout: c.timeout ?? 3,
                serial_port: c.serial_port || dev.serial?.serial_port || '',
                baudrate: c.baudrate || dev.serial?.baudrate || 9600,
                parity: c.parity || dev.serial?.parity || 'N',
                stopbits: c.stopbits || dev.serial?.stopbits || 1,
                bytesize: c.bytesize || dev.serial?.bytesize || 8,
                // a device on a bridge stores the reference, not the address
                bridge: dev.bridge || '', bridge_port: dev.bridge_port ?? null,
                url: c.url || dev.http?.url || '',
                broker: c.broker || '', mqtt_port: c.port || 1883,
                topic: c.topic || dev.mqtt_in_topic || '',
                mqtt_username: c.username || '', mqtt_password: '',
                mqtt_tls: !!c.tls, mqtt_tls_insecure: !!c.tls_insecure,
                mqtt_vars: c.vars || {},
                // the device's own keepalive: a dict, false (off), or absent (the template's)
                keepalive: c.keepalive, template_keepalive: c.template_keepalive || null,
                http_output_enabled: dev.http_output_enabled === true,
                opcua_enabled: dev.opcua_enabled !== false,
                opcua_fields: Array.isArray(dev.opcua_fields) ? dev.opcua_fields : [],
                selected_registers: dev.selected_registers ?? 0,
                pq_supported: dev.pq_supported === true,
                write_locked: dev.write_locked === true,
                commands: dev.commands || [],
                commands_offered: dev.commands_offered || [],
                energy_fields: dev.energy_fields,          // 0 → no Energy tab
                template_notes: dev.template_notes || [],
            },
            sinks: dev.sinks || {},
            entry: dev,                 // full live entry for the Overview tab
        };
        // what the form opened with: Test asks the saved device when the
        // connection is still this (its stored secrets, its running input)
        this._devDetail.orig = JSON.parse(JSON.stringify(this._devDetail.data));
        document.getElementById('devicesListView').style.display = 'none';
        const view = document.getElementById('deviceDetailView');
        // what the previous device page started stops, and the pages it
        // embedded go home, BEFORE its view is wiped (a Monitor embedded there
        // would be destroyed with it)
        this._leaveDeviceDetail();
        view.style.display = '';
        view.innerHTML = this._deviceDetailHtml();
        this._wireDeviceDetail();
        // the status bar states this unit's own rhythm while it is open
        this._footerOverride = (dev.read_via || []).length ? dev.read_via : null;
        if (this.updatePollGroupsStatus) this.updatePollGroupsStatus();
        this._view = { type: 'device', id };
        return true;
    },

    // Back from a unit is its installation; from a standalone device, the list.
    closeDeviceDetail(toList = false) {
        const pid = this._devDetail?.endpoint_id;
        const parent = this._devDetail?.component_of;
        this._leaveDeviceDetail();    // return embedded pages home BEFORE wiping the view
        const view = document.getElementById('deviceDetailView');
        if (view) { view.style.display = 'none'; view.innerHTML = ''; }
        if (this.updatePollGroupsStatus) this.updatePollGroupsStatus();
        if (pid && !toList && this.openEndpointDetail) return this.openEndpointDetail(pid);
        if (parent && !toList) return this.openDeviceDetail(parent);
        const list = document.getElementById('devicesListView');
        if (list) list.style.display = '';
        this._view = { type: 'list' };
        return this.renderDevicesList();
    },

    _deviceDetailHtml() {
        const s = this._devDetail, d = s.data;
        const primary = s.primary;
        const endpointId = s.endpoint_id || '';
        const endpointLk = endpointId ? 'disabled' : '';   // endpoint-owned field lock
        const compOf = s.component_of || '';
        // a parent whose template declares components shows their card
        const holds = ((s.templates || []).find(x => x.id === d.template) || {}).components || [];
        const tcp = d.protocol !== 'rtu';
        const tplOptions = [`<option value="">${this.t('devices.detail.noTemplate', '(no template)')}</option>`]
            .concat(s.templates.map(t =>
                `<option value="${this._esc(t.id)}" ${t.id === d.template ? 'selected' : ''}>${this._esc(t.name)} · ${t.registers} measurements</option>`)).join('');
        const t = this.t.bind(this);
        // the write lock is about what is written TO the device: it sits with
        // the commands when there are some, else with the device's settings
        const lockIn = (d.commands_offered || []).length ? 'commands' : 'connection';
        const meterOnly = d.protocol === 'none';
        const e = s.entry || {};
        // the tabs, grouped: what it shows (Live), what it is told (Configure),
        // what to look at when it misbehaves (Diagnose) — one strip at a time
        const offMon = d.enabled ? '' : t('ddv.offMonitor', 'Polling is off for this device: enable it to view live values.');
        const offInf = d.influxdb_enabled ? '' : t('ddv.offInflux', 'The InfluxDB output is off for this device: enable it on the Outputs tab.');
        const tabs = [
            ['overview', 'bi-grid-1x2', t('devices.tab.overview', 'Overview')],
            ['history', 'bi-clock-history', t('nav.history', 'History'), offInf],
            d.energy_fields === 0 ? null : ['energy', 'bi-lightning-charge', t('nav.energy', 'Energy'), offInf],
            d.pq_supported && !endpointId ? ['pq', 'bi-activity', t('nav.pq', 'Power Quality'), offInf] : null,
            endpointId || compOf
                ? ['connection', 'bi-diagram-3', t('devices.tab.readVia', 'Read via')]
                : ['connection', meterOnly ? 'bi-person-vcard' : 'bi-ethernet',
                   meterOnly ? t('ddv.tabIdentity', 'Identity') : t('ddv.tabConnection', 'Connection'), '', true],
            meterOnly ? null : ['measurements', 'bi-list-check', `${t('devices.registers', 'Measurements')} (${d.selected_registers})`],
            ['calculated', 'bi-calculator', t('calc.tab', 'Calculated')],
            (d.commands_offered || []).length ? ['commands', 'bi-send', `${t('commands.tab', 'Commands')} (${(d.commands || []).length})`] : null,
            endpointId ? null : ['meters', 'bi-lightning', t('meters.tab', 'Meters')],
            ['outputs', 'bi-signpost-split', t('devices.detail.outputs', 'Outputs')],
            ['attributes', 'bi-tags', t('attr.title', 'Attributes')],
            ['monitor', 'bi-graph-up', t('nav.monitor', 'Monitor'), offMon],
            ['logs', 'bi-journal-text', t('devices.tab.logs', 'Logs')],
        ].filter(Boolean);
        const tabHtml = ([name, icon, label, off, admin]) => `<button type="button" class="config-main-tab" id="dtab-${name}" data-dtab="${name}"
                data-dgroup="${this._DTAB_GROUP[name]}" data-panel-id="dpanel-${name}" ${this._DTAB_GROUP[name] === 'live' ? '' : 'hidden'}
                ${off ? `aria-disabled="true" aria-describedby="dtab-${name}-why" title="${this._esc(off)}"` : ''} ${admin ? 'data-admin' : ''}><i aria-hidden="true" class="bi ${icon}"></i> ${this._esc(label)}</button>`;
        // why a tab is off: said once, as its description (never inside its name)
        const whyHtml = tabs.filter(x => x[3]).map(([name, , , off]) => `<span class="sr-only" id="dtab-${name}-why">${this._esc(off)}</span>`).join('');
        const offNote = (name, text) => text ? `<p class="dpanel-off" data-off-for="${name}" hidden><i aria-hidden="true" class="bi bi-info-circle"></i> ${this._esc(text)}</p>` : '';
        const crumb = endpointId || compOf;
        // the ⋯ menu: what is done TO the device (Kind, Disable, Delete) and
        // where it belongs; Test stays in sight
        const menu = [
            endpointId ? `<button type="button" role="menuitem" ${this._act('openEndpointDetail', [endpointId])}><i aria-hidden="true" class="bi bi-diagram-3"></i> ${t('devices.openInstallation', 'Open installation')}</button>` : '',
            compOf ? `<button type="button" role="menuitem" ${this._act('openDeviceDetail', [compOf])}><i aria-hidden="true" class="bi bi-cpu"></i> ${t('components.openParent', 'Open device')}</button>` : '',
            (primary || endpointId) ? '' : `<button data-admin type="button" role="menuitem" id="ddvSetKind" ${this._act('setDeviceKind', [s.id])}><i aria-hidden="true" class="bi bi-tag"></i> ${t('kind.set', 'Kind')}: ${this._esc(this._kindLabel ? this._kindLabel(e.kind) : (e.kind || ''))}…</button>`,
            (primary || endpointId || compOf) ? '' : `<button data-admin type="button" role="menuitem" id="ddvEnableToggle" ${this._act('ddvSetEnabled', [s.id, !d.enabled])}><i aria-hidden="true" class="bi bi-${d.enabled ? 'pause-circle' : 'play-circle'}"></i> ${d.enabled ? t('ddv.disable', 'Disable') : t('ddv.enable', 'Enable')}</button>
                <button data-admin type="button" role="menuitem" id="ddvDisableInstead" hidden ${this._act('ubDisableInstead', ['device', s.id])}><i aria-hidden="true" class="bi bi-pause-circle"></i> ${t('usedby.disableInstead', 'Disable instead')}</button>
                <button data-admin type="button" role="menuitem" class="mbg-menu-danger" id="ddvDeleteBtn" aria-describedby="ddvDeleteWhy" ${this._act('deleteDevice', [s.id])}><i aria-hidden="true" class="bi bi-trash"></i> ${t('common.delete', 'Delete')}</button>`,
        ].filter(Boolean).join('');
        return `
        <div class="section-header ddv-head">
            <h2><button class="btn btn-ghost btn-sm" data-action="closeDeviceDetail" aria-label="${t('common.back', 'Back')}"><i aria-hidden="true" class="bi bi-arrow-left"></i></button>
                ${crumb ? `<nav aria-label="${t('common.breadcrumb', 'Where you are')}" class="ddv-crumbs">
                    <a class="ddv-crumb-far" href="#/devices" ${this._act('closeDeviceDetail', [true])}>${t('nav.devices', 'Devices')}</a>
                    <span class="ddv-crumb-far" aria-hidden="true">›</span>
                    ${endpointId
                        ? `<a href="#/installations/${encodeURIComponent(endpointId)}" ${this._act('openEndpointDetail', [endpointId])}><i aria-hidden="true" class="bi bi-diagram-3"></i> ${this._esc(s.endpoint_name || endpointId)}</a>`
                        : `<a href="#/devices/${encodeURIComponent(compOf)}" ${this._act('openDeviceDetail', [compOf])}><i aria-hidden="true" class="bi bi-cpu"></i> ${this._esc(s.parent_name || compOf)}</a>`}
                    <span aria-hidden="true">›</span>
                    <span class="ddv-name" title="${this._esc(d.name)}">${this._kindIconHtml ? this._kindIconHtml(e.kind) : ''}${this._esc(d.name)}</span>
                    <span class="dev-chip">${this._esc(s.id)}</span></nav>`
                : `<span class="ddv-name" title="${this._esc(d.name)}">${this._kindIconHtml ? this._kindIconHtml(e.kind) : ''}${this._esc(d.name)}</span>
                <span class="dev-chip">${this._esc(s.id)}</span>`}</h2>
            <div class="header-actions">
                <button type="button" class="btn btn-ghost btn-sm ddv-usedby" id="ddvUsedByBadge" hidden ${this._act('usedByOpen', ['device', s.id, 'usedby'])}><i aria-hidden="true" class="bi bi-diagram-2"></i> <span></span></button>
                ${endpointId || compOf || meterOnly ? '' : `<button data-live type="button" class="btn btn-secondary btn-sm" id="ddvHeadTest" ${this._act('testDevice', [s.id], { el: true })}><i aria-hidden="true" class="bi bi-activity"></i> ${t('ddv.test', 'Test')}</button>`}
                ${menu ? `<div class="mbg-menu-wrap" ${endpointId || compOf ? '' : 'data-admin'}>
                    <button type="button" class="btn btn-ghost btn-sm" id="ddvActions" aria-haspopup="menu" aria-expanded="false" aria-controls="ddvActionsMenu"
                        ${this._act('_menuToggle', [], { el: true })} aria-label="${this._esc(t('ddv.actions', 'Actions for {name}', { name: d.name }))}"><i aria-hidden="true" class="bi bi-three-dots"></i></button>
                    <div class="mbg-menu" role="menu" id="ddvActionsMenu" aria-labelledby="ddvActions" hidden>${menu}</div>
                    <span id="ddvDeleteWhy" class="sr-only"></span></div>` : ''}
            </div>
            <p class="ddv-meta" id="ddvMeta">${this._ddvMetaHtml(s.id, e)}</p>
        </div>
        <div id="devTplUpdate"></div>
        ${this._tplNotesHtml(d.template_notes, { folded: true })}

        <!-- device workspace: three groups of tabs, every tab opens IN-PLACE
             (Measurements/Monitor/History/Energy are embedded, scoped to this
             device). The group tablist's one panel holds the tab strip and
             the tab panels (ARIA: a tablist controls tabpanels only). -->
        <div class="dgroups seg" id="deviceWsGroups" aria-label="${this._esc(t('ddv.sections', 'Device sections'))}">
            ${this._DGROUPS.map(g => `<button type="button" role="tab" class="seg-btn dgroup ${g === 'live' ? 'active on' : ''}" id="dgrp-${g}" data-dgroup="${g}" data-panel-id="dgrpPanel"
                ${this._act('_switchDeviceGroup', [g])}>${this._esc(this._dgroupLabel(g))}</button>`).join('')}
        </div>
        <div id="dgrpPanel" role="tabpanel" aria-labelledby="dgrp-live">
        <div class="config-main-tabs dtabs" id="deviceWsTabs" aria-label="${this._esc(this._dgroupLabel('live'))}" style="margin-bottom:14px;">
            ${tabs.map(tabHtml).join('')}
        </div>${whyHtml}

        <!-- host panels for the embedded, device-scoped views -->
        <div data-dpanel="measurements" class="ws-embed-host" hidden></div>
        <div data-dpanel="monitor" class="ws-embed-host" hidden>${offNote('monitor', offMon)}</div>
        <div data-dpanel="history" class="ws-embed-host" hidden>${offNote('history', offInf)}</div>
        <div data-dpanel="energy" class="ws-embed-host" hidden>${offNote('energy', offInf)}</div>
        <div data-dpanel="pq" class="ws-embed-host" hidden>${offNote('pq', offInf)}${d.pq_supported && !endpointId ? this._pqRecorderCard(d, primary) : ''}</div>

        <!-- ── Calculated (formula-derived measurements) ── -->
        <div data-dpanel="calculated" hidden></div>

        <!-- ── Meters: the meters that measure it, and what they give ── -->
        <div data-dpanel="meters" hidden><div class="dev-meters-host"></div></div>

        <!-- ── Logs — what this device's acquisition has been doing ── -->
        <div data-dpanel="logs" hidden><div class="dev-log-host"></div></div>
        <div data-dpanel="attributes" hidden><div class="dev-attr-host"></div></div>
        <div data-dpanel="commands" hidden>${lockIn === 'commands' ? this._writeLockCard(d, primary) : ''}<div class="dev-cmd-host"></div></div>

        <!-- ── Overview (read-only) ── -->
        <div data-dpanel="overview">${compOf ? this._componentOfHtml(s) : ''}${this._deviceOverviewHtml(d, s.entry || {})}
            <div id="ddvPairLine"></div>
            ${holds.length && !compOf ? '<div id="devComponentsCard"></div>' : ''}
            <section id="devUsedBy"></section></div>

        <!-- ── Connection ── one layout for every device; endpoint-owned
             fields are disabled and marked (defined once on the endpoint);
             a meter-only device has no address, only its identity -->
        <div data-dpanel="connection" hidden>
        ${endpointId ? this._unitReadViaHtml(s, d) : compOf ? this._componentReadViaHtml(s) : meterOnly ? this._meterOnlyIdentityHtml(s, d) : `
        <div class="settings-card">
            <div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-ethernet"></i> ${t('devices.detail.connection', 'Connection')}</h3></div>
            <div class="settings-card-body">
                <div id="ddvConn" class="cf-host"></div>
            </div>
        </div>

        <div class="settings-card">
            <div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-diagram-3"></i> ${t('devices.detail.identityTemplate', 'Identity & template')}</h3></div>
            <div class="settings-card-body">
                <div class="form-row">
                    <div class="form-group flex-2"><label class="form-label" for="ddvName">${t('devices.wizard.name', 'Device name')}</label>
                        <input type="text" id="ddvName" class="input" value="${this._esc(d.name)}" ${endpointLk}></div>
                    <div class="form-group"><label class="form-label" for="ddvTemplate">${t('devices.wizard.template', 'Template')}</label>
                        <select id="ddvTemplate" class="input" aria-label="${t('devices.wizard.template', 'Template')}" ${endpointLk}>${tplOptions}</select></div>
                </div>
            </div>
        </div>

        <div class="settings-card">
            <div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-arrow-repeat"></i> ${t('devices.detail.polling', 'Polling')}</h3></div>
            <div class="settings-card-body">
                <label style="display:flex;align-items:center;gap:8px;">
                    <input type="checkbox" id="ddvEnabled" ${d.enabled ? 'checked' : ''} ${endpointLk}>
                    ${t('devices.wizard.enabled', 'Start polling immediately after saving')}</label>
                <div style="margin-top:14px;">
                    <div style="color:var(--text-secondary);font-size:12px;margin-bottom:6px;">${t('devices.pollGroups', 'Poll-group intervals (seconds)')}</div>
                    <div id="ddvPollGroups"><span class="field-hint">${t('common.loading', 'Loading…')}</span></div>
                    <div class="field-hint" style="margin-top:6px;">${t('devices.pollGroupsSaved', 'Saved with Save & Apply below; a changed interval applies at once, without a restart.')}</div>
                </div>
            </div>
            <div class="settings-card-footer">
                <span class="save-feedback" id="ddvFeedback"></span>
                <button data-admin class="btn btn-primary btn-sm" data-action="saveDeviceDetail" data-with-el><i aria-hidden="true" class="bi bi-check-lg"></i> ${t('settings.saveApply', 'Save & Apply')}</button>
            </div>
        </div>
        `}
        ${lockIn === 'connection' ? this._writeLockCard(d, primary) : ''}
        </div>

        <!-- ── Outputs ── where the values go: a list of outputs and the chosen
             one's settings (app-outputs.js) -->
        <div data-dpanel="outputs" hidden><div id="outputsHost"></div></div>
        </div>`;
    },

    // Write protection (F3a): the per-device lock. Locked = every write is
    // refused regardless of guards. Toggle is immediate + audited (own
    // endpoint, admin-only). Shown with the commands (or the settings).
    _writeLockCard(d, primary) {
        const locked = !!d.write_locked;
        return `
        <div class="settings-card">
            <div class="settings-card-header">
                <h3><i aria-hidden="true" class="bi bi-${locked ? 'lock-fill' : 'unlock'}"></i> ${this.t('devices.writeLock.title', 'Write protection')}
                    ${locked ? `<span class="sink-pill warn">${this.t('devices.writeLock.locked', 'locked')}</span>`
                             : `<span class="sink-pill ok">${this.t('devices.writeLock.unlocked', 'writes allowed')}</span>`}</h3>
                <label class="switch-label">
                    <input data-admin type="checkbox" id="ddvWriteLock" ${locked ? 'checked' : ''}
                           ${this._act('toggleWriteLock', [this._devDetail.id], {el: true, on: "change"})}>
                    <span>${this.t('devices.writeLock.lock', 'Lock')}</span>
                </label>
            </div>
            <div class="settings-card-body">
                <p class="field-hint" style="margin:0;">${primary
                    ? this.t('devices.writeLock.primaryHint', 'The primary device ships locked (security.primary_write_locked). Unlock only deliberately — this is the meter your live systems read.')
                    : this._devDetail?.endpoint_id
                    ? this.t('devices.writeLock.endpointHint', 'Installation-level lock — locking/unlocking here applies to EVERY unit of the installation (one physical connection). The global master switch must also be on for any write.')
                    : this.t('devices.writeLock.hint', 'Locked: every Modbus write to this device is refused, declared or not. The global master switch (Settings → Security → Modbus writes) must also be on for any write.')}</p>
            </div>
        </div>`;
    },

    async toggleWriteLock(id, cb) {
        const locked = !!cb.checked;
        cb.disabled = true;
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(id)}/write-lock`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ locked }) });
            const res = await r.json().catch(() => ({}));
            if (!r.ok) {
                cb.checked = !locked;
                const errs = res.detail?.errors || [res.detail || 'failed'];
                this.showToast('error', this.t('devices.writeLock.title', 'Write protection'),
                               Array.isArray(errs) ? errs.join(' · ') : String(errs));
                return;
            }
            if (this._devDetail?.data) this._devDetail.data.write_locked = locked;
            this.showToast('success', this.t('devices.writeLock.title', 'Write protection'),
                           locked ? this.t('devices.writeLock.nowLocked', 'Device locked — writes refused')
                                  : this.t('devices.writeLock.nowUnlocked', 'Device unlocked — writes allowed (master switch permitting)'));
        } finally { cb.disabled = false; }
    },




    _wireDeviceDetail() {
        // in-place workspace tabs; the group bar is wired by data-action
        document.querySelectorAll('#deviceWsTabs .config-main-tab[data-dtab]').forEach(tab =>
            tab.addEventListener('click', () => this._switchDeviceTab(tab.dataset.dtab)));
        // every panel is the tabpanel of its tab
        document.querySelectorAll('#deviceDetailView [data-dpanel]').forEach(p => {
            const n = p.dataset.dpanel;
            if (!document.getElementById(`dtab-${n}`)) return;
            p.id = `dpanel-${n}`;
            p.setAttribute('role', 'tabpanel');
            p.setAttribute('aria-labelledby', `dtab-${n}`);
        });
        this._dgroupLast = {};
        this._tabAria('#deviceWsGroups');
        this._tabAria('#deviceWsTabs');
        this._renderPairLine();
        this._loadDevicePollGroups(this._devDetail?.id);
        this._loadDeviceSnapshot(this._devDetail?.id);   // Overview is the default tab
        this._loadDeviceTplUpdate(this._devDetail);
        this._compFoundLast = null;
        if (document.getElementById('devComponentsCard')) this.renderComponentsCard();
        this._loadDeviceUsedBy(this._devDetail?.id);
        this._mountDeviceConnection();
    },

    // the Connection tab's form (app-connform.js): how the device is reached,
    // the same form Add Device uses — its class kept (Modbus variants may
    // change into one another, HTTP and MQTT stay), the primary on Modbus TCP,
    // a device holding components kept as it is reached
    _mountDeviceConnection() {
        const host = document.getElementById('ddvConn');
        const s = this._devDetail;
        if (!host || !s) return;
        const CM = window.ConnformModel;
        const M = CM.model(s.entry || {});
        const holds = ((s.templates || []).find(x => x.id === s.data.template) || {}).components || [];
        this._cfMount(host, 'ddv', M, {
            context: 'edit', deviceId: s.id, primary: !!s.primary,
            lockClass: CM.classOf(M.mode), lockMode: holds.length > 0,
            template: () => document.getElementById('ddvTemplate')?.value || s.data.template || '',
        });
    },

    // what uses this device: the Overview section, the header's count, and
    // Delete held back (with the reason said) while anything does
    async _loadDeviceUsedBy(id) {
        if (!id) return;
        const s = this._devDetail;
        const ctx = s.primary ? 'primary' : s.endpoint_id ? 'unit' : s.component_of ? 'component' : 'device';
        const deps = await this._ubMount('device', id, document.getElementById('devUsedBy'), ctx);
        if (deps) this._applyDeviceUsedBy(id, deps);
    },

    _applyDeviceUsedBy(id, deps) {
        if (this._devDetail?.id !== id) return;
        this._devDetail.usedBy = deps;
        const n = this._ubCount(deps);
        const badge = document.getElementById('ddvUsedByBadge');
        if (badge) {
            badge.hidden = !n;
            badge.querySelector('span').textContent = this.tn('usedby.badge', n, 'used by {n}');
            badge.setAttribute('aria-label', this.tn('usedby.badgeAria', n, 'Used by {n}: show what uses it'));
        }
        this._ubHold(document.getElementById('ddvDeleteBtn'), n, document.getElementById('ddvDeleteWhy'));
        const dis = document.getElementById('ddvDisableInstead');
        if (dis) dis.hidden = !n || this._devDetail.data?.enabled === false;
        const tog = document.getElementById('ddvEnableToggle');
        if (tog) tog.hidden = !!n && this._devDetail.data?.enabled !== false;
    },

    // a standalone device made from a template that has improved since: the
    // notice appears only then (an installation's units are updated together,
    // from the installation page)
    async _loadDeviceTplUpdate(s) {
        const box = document.getElementById('devTplUpdate');
        if (!box || !s || s.primary || s.endpoint_id) return;
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(s.id)}/template-changes`);
            if (!r.ok) return;
            const plan = await r.json();
            if (this._devDetail?.id !== s.id) return;    // the operator moved on
            box.innerHTML = this._tplUpdateNotice('device', s.id, {
                pending: plan.pending, changes: (plan.changes || []).length,
                new: (plan.new || []).length, units_affected: 0,
                missing_templates: plan.missing_templates || [] });
        } catch (e) { /* the notice is a convenience */ }
    },

    // ── Logs ────────────────────────────────────────────────────────────────
    //
    // What this device's acquisition has been doing, with the live per-group
    // state beside it. The connection has always kept this ring; until now the
    // only reader was the alert harvester, so diagnosing a struggling endpoint
    // meant grepping container logs for facts the process already had.

    _stopDeviceLogs() {
        if (this._devLogTimer) { clearInterval(this._devLogTimer); this._devLogTimer = null; }
    },

    // everything a device page started, stopped: its log poll, the pages it
    // embedded (sent home), its footer rhythm. Called before another device
    // page is drawn, on close, and when the user leaves the Devices page.
    _leaveDeviceDetail() {
        this._stopDeviceLogs();
        this._stopDeviceChannels();
        this._restoreWsPages();
        this._footerOverride = null;
    },

    // ── Commands tab ─────────────────────────────────────────────────────────
    //
    // What this unit can be told, from its template: one card per command with
    // its parameters, Run and Test, the last result, and the recent history
    // from the audit log. The same commands a controller runs over MQTT.
    async _initDeviceCommands(id) {
        const t = (k, d) => this.t(k, d);
        const host = document.querySelector('#deviceDetailView [data-dpanel="commands"] .dev-cmd-host');
        if (!host) return;
        host.innerHTML = `<div class="field-hint">${t('common.loading', 'Loading…')}</div>`;
        let cmds = [], hist = [], chans = [];
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(id)}/commands`);
            cmds = (await r.json()).commands || [];
            hist = ((await (await fetch(`/api/commands/history?device=${encodeURIComponent(id)}&limit=10`)).json()).history) || [];
        } catch (e) { host.innerHTML = `<div class="field-hint">${this._esc(e.message)}</div>`; return; }
        // who is in charge of what these commands drive: a tab that cannot ask
        // still draws as before
        try {
            const cr = await fetch(`/api/channels/${encodeURIComponent(id)}`);
            if (cr.ok) chans = (await cr.json()).channels || [];
        } catch (e) { /* no channel view: the commands alone */ }
        if (this._devDetail?.id !== id) return;          // another device opened meanwhile
        this._devCmds = cmds;
        this._devChans = chans;
        const chanOf = name => chans.find(ch => (ch.commands || []).includes(name)) || null;
        // a device of its own enables commands here; an installation's unit, in its group
        const devRow = (this._devices || []).find(x => x.id === id) || {};
        const standalone = !devRow.endpoint_id;
        const fmtTs = ts => ts ? new Date((typeof ts === 'number' ? ts * 1000 : ts)).toLocaleString(this._loc()) : '';
        host.innerHTML = `
        <div class="settings-card">
            <div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-send"></i> ${t('commands.tab', 'Commands')}</h3></div>
            <div class="settings-card-body">
                <p class="field-hint" style="margin:0 0 12px;">${t('commands.unitIntro',
                    'What this unit can be told. Each command writes what its template declares — Modbus registers, or a value published over MQTT — then reads back and says whether it took. The same commands are accepted over MQTT and from Home Assistant; every run is audited.')}</p>
                ${cmds.length ? '' : `<div class="field-hint">${t('commands.none', 'This unit\'s template declares no commands.')}</div>`}
                <div class="cmd-channels-host">${this._channelsCardHtml(chans)}</div>
                ${cmds.map(c => `
                <div class="cmd-card" data-cmd="${this._esc(c.name)}" style="border:1px solid var(--border,#e5e7eb);border-radius:8px;padding:12px;margin-bottom:12px;${c.enabled ? '' : 'opacity:.75;'}">
                    <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px;">
                        <b>${this._esc(c.label || c.name)}</b> <span class="dev-chip">${this._esc(c.name)}</span>
                        ${c.alias ? `<span class="field-hint">= ${this._esc(c.alias.command)} ${this._esc(Object.entries(c.alias.params || {}).map(([k, v]) => `${k} ${v}`).join(', '))}</span>` : ''}
                        ${this._tplNoteFor(devRow.template_notes, c.name) ? `<span class="sink-pill warn" title="${this._esc(this._tplNoteFor(devRow.template_notes, c.name))}"><i aria-hidden="true" class="bi bi-exclamation-triangle"></i> ${t('tplNotes.seeNote', 'see the template note')}</span>` : ''}
                        ${c.enabled ? '' : `<span class="sink-pill warn">${t('commands.notEnabled', 'not enabled on this unit')}</span>`}
                        ${c.refreshing ? `<span class="sink-pill ok" title="${this._esc(t('commands.refreshingHint', 'The device forgets this setting by itself: it is written again until the safe value is sent.'))}"><i aria-hidden="true" class="bi bi-arrow-repeat"></i> ${t('commands.refreshing', 'kept alive')}${c.refresh_s ? ` · ${Math.round(c.refresh_s / 60)} min` : ''}</span>` : ''}
                        ${standalone && c.from_template ? `<label class="checkbox-label" data-admin style="margin-left:8px;"><input type="checkbox" ${c.enabled ? 'checked' : ''} ${this._act('toggleDeviceCommand', [id, c.name], { el: true, on: 'change' })}> <span>${t('commands.enabledHere', 'Enabled on this device')}</span></label>` : ''}
                        <span class="field-hint" style="margin-left:auto;">MQTT <code>${this._esc(c.mqtt_topic || '')}</code></span>
                    </div>
                    ${c.enabled ? '' : `<div class="field-hint" style="margin-bottom:8px;">${standalone
                        ? t('commands.enableHintDevice', 'Test shows what it would write without writing. Tick “Enabled on this device” to allow Run, the API and MQTT.')
                        : t('commands.enableHint', 'Tick it under Commands in the group editor of the installation.')}</div>`}
                    ${c.writes && c.writes.length ? `<div class="field-hint" style="margin-bottom:8px;">${t('commands.writes', 'Writes')}: ${c.writes.map(w => `<code>${this._esc(w.register)}</code>`).join(', ')}${c.guard && c.guard.length ? ` · ${t('commands.guarded', 'only if')} ${c.guard.map(g => `<code>${this._cmdGuardText(g)}</code>`).join(', ')}` : ''}${c.verify && c.verify.length ? ` · ${t('commands.verifies', 'verifies')} ${c.verify.map(g => `<code>${this._esc(g.read)}</code>`).join(', ')}` : ''}</div>` : ''}
                    ${this._cmdFormHtml(c, `dcmd_${c.name}`)}
                    <div class="cmd-chan">${this._cmdChannelHtml(c, chanOf(c.name))}</div>
                    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                        <button type="button" data-live class="btn btn-primary btn-sm cmd-run" data-enabled="${c.enabled ? 1 : 0}" ${c.enabled && !chanOf(c.name)?.safety_holder ? '' : 'disabled'} ${this._act('runDeviceCommand', [id, c.name, false])}><i aria-hidden="true" class="bi bi-send"></i> ${t('commands.run', 'Run')}</button>
                        <button type="button" data-live class="btn btn-ghost btn-sm" ${this._act('runDeviceCommand', [id, c.name, true])}
                                title="${t('commands.testHint', 'Reads the device and shows what would be written — nothing is written.')}"><i aria-hidden="true" class="bi bi-eye"></i> ${t('commands.test', 'Test')}</button>
                        <span class="field-hint">${c.last ? `${t('commands.last', 'Last')}: ${this._cmdVerdict(c.last.status)} · ${fmtTs(c.last.ts)} · ${this._esc(c.last.via || '')} ${this._esc(c.last.by || '')}` : t('commands.neverRun', 'Never run since start')}</span>
                    </div>
                    <div class="cmd-out" role="status" aria-live="polite" style="margin-top:8px;font-size:12.5px;"></div>
                </div>`).join('')}
            </div>
        </div>
        <div class="settings-card">
            <div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-clock-history"></i> ${t('commands.history', 'Recent commands')}</h3></div>
            <div class="settings-card-body">
                ${hist.length ? `<div class="table-wrap"><table class="table"><thead><tr>
                    <th>${t('commands.h.when', 'When')}</th><th>${t('commands.h.command', 'Command')}</th><th>${t('commands.h.params', 'Parameters')}</th>
                    <th>${t('commands.h.result', 'Result')}</th><th>${t('commands.h.by', 'By')}</th></tr></thead><tbody>
                    ${hist.map(h => { const d = h.detail || {}; return `<tr>
                        <td>${this._esc(fmtTs(h.ts || h.time || h.timestamp))}</td>
                        <td><code>${this._esc(String(h.target || '').split(' ').pop())}</code></td>
                        <td>${this._esc(Object.entries(d.params || {}).map(([k, v]) => `${k} = ${v}`).join(', '))}</td>
                        <td><span class="status-dot" style="--dot:${this._cmdDot(h.status)}" aria-hidden="true"></span> ${this._cmdVerdict(h.status)}${d.reason ? ` · ${this._esc(d.reason)}` : ''}</td>
                        <td>${this._esc(h.user || '')} <span class="field-hint">${this._esc(d.via || '')}</span></td></tr>`; }).join('')}
                </tbody></table></div>`
                : `<div class="field-hint">${t('commands.noHistory', 'Nothing yet. Every command — from this page, the API, MQTT or Home Assistant — is recorded here.')}</div>`}
            </div>
        </div>`;
        // who holds a channel changes on its own (a safety rule starts or stops
        // acting, a pause runs out): kept current while the tab is shown
        this._stopDeviceChannels();
        if (chans.length) this._devChanTimer = setInterval(() => {
            if (this._devDetail?.id !== id || !host.isConnected || host.closest('[hidden]')) { this._stopDeviceChannels(); return; }
            this._refreshDeviceChannels(id);
        }, 10000);
    },

    _stopDeviceChannels() {
        if (this._devChanTimer) { clearInterval(this._devChanTimer); this._devChanTimer = null; }
    },

    // the channels asked again: the "Who drives it" card, each command's
    // channel line and its Run button follow (a typed pause is kept)
    async _refreshDeviceChannels(id) {
        let chans;
        try {
            const r = await fetch(`/api/channels/${encodeURIComponent(id)}`);
            if (!r.ok) return this._devChans || [];
            chans = (await r.json()).channels || [];
        } catch (e) { return this._devChans || []; }
        if (this._devDetail?.id !== id) return chans;
        this._devChans = chans;
        const host = document.querySelector('#deviceDetailView [data-dpanel="commands"] .dev-cmd-host');
        if (!host) return chans;
        const top = host.querySelector('.cmd-channels-host');
        if (top) top.innerHTML = this._channelsCardHtml(chans);
        for (const c of this._devCmds || []) {
            const card = host.querySelector(`.cmd-card[data-cmd="${CSS.escape(c.name)}"]`);
            if (!card) continue;
            const ch = chans.find(x => (x.commands || []).includes(c.name)) || null;
            const box = card.querySelector('.cmd-chan');
            const typed = card.querySelector('.cmd-override')?.value;
            const html = this._cmdChannelHtml(c, ch);
            if (box && box.dataset.html !== html) {
                box.innerHTML = html;
                box.dataset.html = html;
                const inp = card.querySelector('.cmd-override');
                if (inp && typed != null) inp.value = typed;
            }
            const run = card.querySelector('.cmd-run');
            if (run) run.disabled = !(run.dataset.enabled === '1' && !ch?.safety_holder);
        }
        return chans;
    },

    async runDeviceCommand(id, name, dryRun) {
        const t = (k, d) => this.t(k, d);
        const cmd = (this._devCmds || []).find(c => c.name === name);
        const card = document.querySelector(`#deviceDetailView .cmd-card[data-cmd="${name}"]`);
        const out = card?.querySelector('.cmd-out');
        if (!cmd || !out) return;
        const read = this._cmdReadParams(cmd, `dcmd_${name}`);
        if (read.error) { out.innerHTML = `<span class="text-danger">${this._esc(read.error)}</span>`; return; }
        const said = Object.entries(read.params).map(([k, v]) => `${k} = ${v}`).join(', ') || '—';
        // who holds the channel now (asked again: it changes on its own)
        const chans = dryRun ? (this._devChans || []) : await this._refreshDeviceChannels(id);
        const ch = (chans || []).find(x => (x.commands || []).includes(name));
        let pause = '';
        if (!dryRun && ch?.safety_holder) {
            out.innerHTML = `<span class="text-danger">${this._esc(this.t('commands.safetyHolds', 'Safety rule {rule} holds this now — it cannot be overridden from here.', { rule: ch.safety_holder }))}</span>`;
            return;
        }
        if (!dryRun && ch?.owner) {
            // the server asks for a pause whenever a rule owns the channel;
            // it pauses only the rules under the safety layer
            const who = this._chanPause(ch);
            const inp = card.querySelector('.cmd-override');
            const max = ch.max_override_s || 3600;
            let v = Math.min(600, max);
            if (inp) {
                v = this._intOrNull(inp.value);
                if (v === null || v < 1 || v > max) {
                    this._placeFieldErrors(card, { override_s: this.t('commands.overrideRange', 'Seconds, from 1 to {max}', { max }) }, () => inp.id);
                    return;
                }
            }
            read.params.override_s = v;
            pause = [
                who.pausable.length ? this.t('commands.pausesFor', '{rule} pauses for {time}.', { rule: who.pausable.join(', '), time: this._fmtDuration(v) }) : '',
                who.protect ? this.t('commands.protectNotPaused', 'Safety rule {rule} is not paused: it takes the channel back as soon as it acts.', { rule: who.protect }) : '',
            ].filter(Boolean).join('\n');
        }
        if (!dryRun && (cmd.confirm !== false || pause) && !await this._confirmCommand(cmd, said, id, pause)) return;
        out.innerHTML = t('common.loading', 'Loading…');
        const r = await this._postCommand(`/api/devices/${encodeURIComponent(id)}/commands/${encodeURIComponent(name)}${dryRun ? '/dry-run' : ''}`, read.params, id);
        out.innerHTML = r._gate ? `<span class="text-danger">${this._esc(r._gate)}</span>` : this._cmdResultHtml(r, false);
        if (!dryRun && !r._gate) this._initDeviceCommands(id);
    },

    _initDeviceLogs(id) {
        const host = document.querySelector('#deviceDetailView [data-dpanel="logs"] .dev-log-host');
        if (!host) return;
        const t = (k, d) => this.t(k, d);
        if (!host.dataset.built) {
            host.dataset.built = '1';
            host.innerHTML = `
              <style>
                .dev-log-row td{padding:3px 12px 3px 0;vertical-align:top;}
                .dev-log-row.error td{background:rgba(192,57,43,.07);}
                .dev-log-row.warn td{background:rgba(245,158,11,.08);}
                .dev-log-when{white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--text-secondary);}
                .dev-log-kind{white-space:nowrap;font-weight:600;}
              </style>
              <div class="settings-card"><div class="settings-card-body">
                <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:12px;">
                  <label class="form-label" style="display:flex;align-items:center;gap:6px;margin:0;">
                    <input type="checkbox" class="dev-log-live" checked> ${t('devices.logs.live', 'live')}</label>
                  <label class="form-label" style="display:flex;align-items:center;gap:6px;margin:0;">
                    <input type="checkbox" class="dev-log-onlybad"> ${t('devices.logs.onlyBad', 'problems only')}</label>
                  <span class="dev-log-meta" style="color:var(--text-secondary);font-size:12.5px;margin-left:auto;"></span>
                </div>
                <div class="dev-log-groups" style="margin-bottom:14px;"></div>
                <div class="dev-log-body"></div>
              </div></div>`;
            host.querySelector('.dev-log-onlybad').addEventListener(
                'change', () => this._refreshDeviceLogs(id));
            host.querySelector('.dev-log-live').addEventListener('change', e => {
                if (e.target.checked) this._initDeviceLogs(id); else this._stopDeviceLogs();
            });
        }
        this._refreshDeviceLogs(id);
        this._stopDeviceLogs();
        this._devLogTimer = setInterval(() => {
            // a timer belongs to the page it was started on: never poll
            // another device's log, nor one whose page is gone
            if (this._devDetail?.id !== id || !host.isConnected) { this._stopDeviceLogs(); return; }
            const live = host.querySelector('.dev-log-live');
            if (live && live.checked) this._refreshDeviceLogs(id);
        }, 5000);
    },

    async _refreshDeviceLogs(id) {
        const host = document.querySelector('#deviceDetailView [data-dpanel="logs"] .dev-log-host');
        if (!host || !id || !host.querySelector('.dev-log-body') || this._devDetail?.id !== id) return;
        const t = (k, d) => this.t(k, d);
        const bad = host.querySelector('.dev-log-onlybad')?.checked;
        let d;
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(id)}/events?limit=300`
                                  + (bad ? '&level=error,warn' : ''));
            if (!r.ok) throw new Error(r.status === 404
                ? t('devices.logs.notRunning', 'This device is not running, so it has no acquisition log.')
                : r.statusText);
            d = await r.json();
        } catch (e) {
            if (!host.isConnected) return;
            host.querySelector('.dev-log-body').innerHTML =
                `<span style="color:var(--text-secondary);">${this._esc(e.message)}</span>`;
            host.querySelector('.dev-log-groups').innerHTML = '';
            return;
        }
        if (!host.isConnected || this._devDetail?.id !== id) return;
        const c = d.counters || {};
        const pct = (c.successful_reads && (c.successful_reads + c.failed_reads))
            ? (100 * c.failed_reads / (c.successful_reads + c.failed_reads)) : 0;
        host.querySelector('.dev-log-meta').innerHTML =
            `${c.successful_reads ?? '—'} ${t('devices.logs.ok', 'ok')} · `
            + `${c.failed_reads ?? '—'} ${t('devices.logs.failed', 'failed')}`
            + (pct ? ` (${pct.toFixed(1)}%)` : '')
            + (c.forced_reopens ? ` · ${c.forced_reopens} ${t('devices.logs.reopens', 'reopens')}` : '')
            + (c.last_latency_ms != null ? ` · ${t('devices.logs.lastRead', 'last read')} ${c.last_latency_ms} ms` : '');

        // live per-group state: "what happened" and "what it is doing now" are
        // the same question when an endpoint is struggling
        const groups = d.poll_groups || [];
        host.querySelector('.dev-log-groups').innerHTML = groups.length ? `
            <table style="font-size:12.5px;"><tr style="color:var(--text-secondary);">
              <td style="padding-right:14px;">${t('devices.logs.group', 'group')}</td>
              <td style="padding-right:14px;">${t('devices.logs.interval', 'interval')}</td>
              <td style="padding-right:14px;">${t('devices.logs.sweep', 'last sweep')}</td>
              <td style="padding-right:14px;">${t('devices.logs.reads', 'reads')}</td>
              <td style="padding-right:14px;">${t('devices.logs.overruns', 'overruns')}</td>
              <td>${t('devices.logs.age', 'age')}</td></tr>
            ${groups.map(g => {
                const over = g.cycle_s != null && g.interval && g.cycle_s > g.interval;
                return `<tr style="font-variant-numeric:tabular-nums;">
                  <td style="padding-right:14px;"><b>${this._esc(g.name || '')}</b></td>
                  <td style="padding-right:14px;">${g.interval ?? '—'}s</td>
                  <td style="padding-right:14px;${over ? 'color:var(--danger,#ef4444);font-weight:600;' : ''}">${g.cycle_s != null ? g.cycle_s + 's' : '—'}</td>
                  <td style="padding-right:14px;">${g.reads ?? '—'}</td>
                  <td style="padding-right:14px;${g.overruns ? 'color:var(--warning,#f59e0b);font-weight:600;' : ''}">${g.overruns ?? 0}</td>
                  <td>${g.age_s != null ? g.age_s + 's' : '—'}</td></tr>`;
            }).join('')}</table>` : '';

        const evs = d.events || [];
        host.querySelector('.dev-log-body').innerHTML = evs.length ? `
            <table style="width:100%;font-size:12.5px;">${evs.map(e => `
              <tr class="dev-log-row ${this._esc(e.level || 'info')}">
                <td class="dev-log-when">${new Date((e.ts || 0) * 1000).toLocaleTimeString(this._loc())}</td>
                <td class="dev-log-kind">${this._esc(e.kind || '')}</td>
                <td>${this._esc(e.message || '')}</td></tr>`).join('')}</table>
            ${d.truncated ? `<div class="field-hint" style="margin-top:8px;">${t('devices.logs.truncated',
                'The ring is full — older entries have been dropped.')}</div>` : ''}`
            : `<span style="color:var(--text-secondary);">${bad
                ? t('devices.logs.noneBad', 'No errors or warnings recorded.')
                : t('devices.logs.none', 'Nothing recorded yet. Acquisition events appear here as they happen.')}</span>`;
    },

    // ── the device page's groups and tabs ─────────────────────────────────
    // A tab's name is its address (#/devices/<id>/<tab>); its group follows
    // from it. `edit` is the earlier name of `connection`, kept for links.
    _DTAB_ALIAS: { edit: 'connection', registers: 'measurements', regs: 'measurements', values: 'overview' },
    _DTAB_GROUP: { overview: 'live', history: 'live', energy: 'live', pq: 'live',
                   connection: 'configure', measurements: 'configure', calculated: 'configure',
                   commands: 'configure', meters: 'configure', outputs: 'configure', attributes: 'configure',
                   monitor: 'diagnose', logs: 'diagnose' },
    _DGROUPS: ['live', 'configure', 'diagnose'],

    // the header's second line: health in words, how fresh, what measures it
    _ddvMetaHtml(id, e) {
        const t = (k, d, p) => this.t(k, d, p);
        const health = e.enabled === false ? 'idle' : (e.data_health || (e.connected ? 'ok' : 'idle'));
        const hColor = { ok: 'var(--success,#22c55e)', degraded: 'var(--warning,#f59e0b)',
                         down: 'var(--danger,#ef4444)', idle: 'var(--text-secondary,#8a94a0)' };
        const ago = e.staleness_age_s != null ? t('ddv.readAgo', 'read {s} s ago', { s: e.staleness_age_s }) : '';
        const names = (e.measured_by || []).map(m => ((this._devices || []).find(x => x.id === m) || {}).name || m);
        const measured = e.protocol === 'none' && names.length
            ? (e.data_health === 'down' && e.enabled !== false
                ? t('ddv.metersSilent', '{list}', { list: names.join(', ') })     // the verdict already says it
                : t('ddv.measuredBy', 'measured by {list}', { list: names.join(', ') })) : '';
        return `<span class="dev-chip ddv-meta-id" aria-hidden="true">${this._esc(id)}</span>
                <span class="status-dot" style="--dot:${hColor[health]}" aria-hidden="true"></span>
                <span class="ddv-verdict">${this._esc(this._verdictWord(e))}</span>${ago ? ` <span aria-hidden="true">·</span> <span>${this._esc(ago)}</span>` : ''}${measured ? ` <span aria-hidden="true">·</span> <span>${this._esc(measured)}</span>` : ''}`;
    },

    async _refreshDdvMeta(id) {
        const list = await this._fetchDevices(true).catch(() => null);
        const e = (list || []).find(x => x.id === id);
        const box = document.getElementById('ddvMeta');
        if (!e || !box || this._devDetail?.id !== id) return;
        this._devDetail.entry = { ...(this._devDetail.entry || {}), ...e };
        box.innerHTML = this._ddvMetaHtml(id, e);
    },

    // ⋯ → Disable / Enable: a device nothing uses is disabled with a plain
    // write confirm; one that is used goes through "Disable instead" (which
    // names what reacts); enabling asks nothing (one click undoes it)
    async ddvSetEnabled(id, on) {
        const t = (k, d, p) => this.t(k, d, p);
        if (!on) {
            const deps = this._devDetail?.id === id ? this._devDetail.usedBy : null;
            if (deps && this._ubCount(deps)) return this.ubDisableInstead('device', id);
            const ok = await this.confirmAction({
                title: t('usedby.disableDevTitle', 'Disable device {id}', { id }),
                message: t('ddv.disableMsg', 'Its reading stops; nothing uses it. It can be enabled again.'),
                confirmLabel: t('usedby.disable', 'Disable'),
            });
            if (!ok) return;
        }
        const r = await fetch(`/api/devices/${encodeURIComponent(id)}`, { method: 'PUT',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !!on }) });
        if (!r.ok) { this.showToast('error', t('toast.saveFailed', 'Save Failed'), this._respError(r, await r.json().catch(() => ({})))); return; }
        this.showToast('success', on ? t('ddv.enabled', 'Enabled') : t('usedby.disabledTitle', 'Disabled'), id);
        await this.openDeviceDetail(id);
        const h = document.querySelector('#deviceDetailView h2');
        if (h) { h.tabIndex = -1; h.focus({ preventScroll: true }); }
    },

    _dgroupLabel(g) {
        return ({ live: this.t('ddv.group.live', 'Live'), configure: this.t('ddv.group.configure', 'Configure'),
                  diagnose: this.t('ddv.group.diagnose', 'Diagnose') })[g] || g;
    },

    // a tab this account may open (a configuration tab is not opened for a
    // viewer even by a link — by role, never by whether it is on screen)
    _dtabAllowed(btn) {
        return !!btn && !(btn.hasAttribute('data-admin') && this._isAdminView && !this._isAdminView());
    },

    // the tab a group opens on: the one last used in it on this page, else
    // its first — Configure on a meter-only device opens on its Meters
    _dgroupTab(g) {
        const tabs = [...document.querySelectorAll(`#deviceWsTabs [data-dtab][data-dgroup="${g}"]`)]
            .filter(b => this._dtabAllowed(b));
        const last = (this._dgroupLast || {})[g];
        if (last && tabs.some(b => b.dataset.dtab === last)) return last;
        if (g === 'configure' && this._devDetail?.data?.protocol === 'none' && tabs.some(b => b.dataset.dtab === 'meters')) return 'meters';
        if (g === 'diagnose' && tabs.some(b => b.dataset.dtab === 'logs')) return 'logs';
        return tabs[0]?.dataset.dtab || 'overview';
    },

    _switchDeviceGroup(g) {
        return this._switchDeviceTab(this._dgroupTab(g));
    },

    _switchDeviceTab(name) {
        const id = this._devDetail?.id;
        name = this._DTAB_ALIAS[name] || name;
        if (this._DGROUPS.includes(name)) name = this._dgroupTab(name);
        let btn = document.querySelector(`#deviceWsTabs [data-dtab="${CSS.escape(name)}"]`);
        if (!this._dtabAllowed(btn)) { name = 'overview'; btn = document.querySelector('#deviceWsTabs [data-dtab="overview"]'); }
        const group = btn?.dataset.dgroup || 'live';
        (this._dgroupLast || (this._dgroupLast = {}))[group] = name;
        // the group: its button selected, its tabs the strip's only ones
        document.querySelectorAll('#deviceWsGroups [data-dgroup]').forEach(b => {
            const on = b.dataset.dgroup === group;
            b.classList.toggle('active', on);
            b.classList.toggle('on', on);
        });
        document.querySelectorAll('#deviceWsTabs [data-dtab]').forEach(b => { b.hidden = b.dataset.dgroup !== group; });
        document.getElementById('dgrpPanel')?.setAttribute('aria-labelledby', `dgrp-${group}`);
        document.getElementById('deviceWsTabs')?.setAttribute('aria-label', this._dgroupLabel(group));
        this._tabAria('#deviceWsGroups');
        if (name !== 'logs') this._stopDeviceLogs();
        if (name !== 'commands') this._stopDeviceChannels();
        this._restoreWsPages();                       // return any embedded view home first
        document.querySelectorAll('#deviceWsTabs .config-main-tab[data-dtab]').forEach(t =>
            t.classList.toggle('active', t.dataset.dtab === name));
        this._tabAria('#deviceWsTabs');
        btn?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
        document.querySelectorAll('#deviceDetailView [data-dpanel]').forEach(p =>
            p.hidden = p.dataset.dpanel !== name);
        this._viewDevice = id;                        // lock the scoped views to THIS device
        // a tab that cannot show anything now (polling off, no InfluxDB
        // output) opens on why — its page is not started
        const off = btn?.getAttribute('aria-disabled') === 'true';
        document.querySelectorAll('#deviceDetailView .dpanel-off').forEach(n => { n.hidden = !(off && n.dataset.offFor === name); });
        // a panel that starts with no control is a tab stop itself (APG), so
        // Tab from the strip lands in it, not past the page
        const panel = document.querySelector(`#deviceDetailView [data-dpanel="${CSS.escape(name)}"]`);
        if (panel) {
            const first = panel.querySelector('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea, [tabindex="0"]');
            if (name === 'overview' || !first) panel.tabIndex = 0; else panel.removeAttribute('tabindex');
        }
        if (off) return;
        if (name === 'overview') { this._pairLive = null; this._loadDeviceSnapshot(id); this._renderPairLine(); this._refreshDdvMeta(id); }
        else if (name === 'outputs') this._renderOutputs();
        else if (name === 'calculated') this._initCalculated(id);
        else if (name === 'meters') this._renderMetersTab();
        else if (name === 'logs') this._initDeviceLogs(id);
        else if (name === 'commands') this._initDeviceCommands(id);
        else if (name === 'attributes') this._loadAttributes('devices', id,
            document.querySelector('#deviceDetailView [data-dpanel="attributes"] .dev-attr-host'));
        else if (name === 'measurements') this._embedRegisters(id);
        else if (name === 'monitor') this._embedWsPage('monitor', () => this.initMonitorPage());
        else if (name === 'history') this._embedWsPage('history', () => this.initHistoryPage());
        else if (name === 'energy') this._embedWsPage('energy', () => this.initEnergyPage());
        else if (name === 'pq') {
            this._embedWsPage('pq', () => this.initPowerQualityPage());
            if (this._devDetail?.data?.pq_supported) this._loadPqSinkStatus(id);
        }
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
            this.switchDeviceRegTab('selected');      // what is read comes first
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

    // every protocol a device may speak, and its families — the same tuples
    // as multibus/config.py (pinned by tests/test_unchanged_save.py)
    _DEVICE_PROTOCOLS: ['tcp', 'udp', 'rtu-tcp', 'ascii-tcp', 'rtu', 'ascii', 'rtu_tap', 'http', 'mqtt'],
    _NET_PROTOCOLS: ['tcp', 'udp', 'rtu-tcp', 'ascii-tcp'],
    _SERIAL_PROTOCOLS: ['rtu', 'ascii', 'rtu_tap'],

    // a serial line's settings, shown whole: an editor that shows half of
    // them writes the other half back as defaults (a 19200-baud bus read at
    // 9600). ids: `${prefix}Baud`, `${prefix}Parity`, `${prefix}Stop`, `${prefix}Bits`
    _serialLineHtml(prefix, d, lock = '') {
        const t = (k, def) => this.t(k, def);
        const opts = (list, cur) => [...new Set([...list, cur].filter(x => x !== undefined && x !== null && x !== ''))]
            .map(x => `<option ${String(x) === String(cur) ? 'selected' : ''}>${this._esc(String(x))}</option>`).join('');
        return `<div class="form-row">
            <div class="form-group"><label class="form-label" for="${prefix}Baud">${t('lbl.baudrate', 'Baudrate')}</label>
                <select id="${prefix}Baud" class="input" ${lock}>${opts([1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200], d.baudrate ?? 9600)}</select></div>
            <div class="form-group"><label class="form-label" for="${prefix}Parity">${t('lbl.parity', 'Parity')}</label>
                <select id="${prefix}Parity" class="input" ${lock}>${opts(['N', 'E', 'O'], d.parity ?? 'N')}</select></div>
            <div class="form-group"><label class="form-label" for="${prefix}Stop">${t('lbl.stopbits', 'Stop bits')}</label>
                <select id="${prefix}Stop" class="input" ${lock}>${opts([1, 2], d.stopbits ?? 1)}</select></div>
            <div class="form-group"><label class="form-label" for="${prefix}Bits">${t('lbl.bytesize', 'Data bits')}</label>
                <select id="${prefix}Bits" class="input" ${lock}>${opts([7, 8], d.bytesize ?? 8)}</select></div>
        </div>`;
    },

    // the line as the form holds it, or null when the form has none
    _serialLineRead(prefix) {
        const v = id => document.getElementById(prefix + id)?.value;
        if (v('Baud') === undefined) return null;
        return { baudrate: parseInt(v('Baud'), 10), parity: v('Parity'),
                 stopbits: parseInt(v('Stop'), 10), bytesize: parseInt(v('Bits'), 10) };
    },

    // a protocol in words; a device measured only by its meters has none
    _protoLabel(p) {
        return p === 'none' ? this.t('ddv.meterOnlyProto', 'Measured by its meters') : (this._PROTO_LABEL[p] || p);
    },

    _PROTO_LABEL: { http: 'HTTP', tcp: 'Modbus TCP', udp: 'Modbus UDP', 'rtu-tcp': 'Modbus RTU/TCP', 'ascii-tcp': 'Modbus ASCII/TCP',
                    rtu: 'Modbus RTU', ascii: 'Modbus ASCII', rtu_tap: 'Modbus RTU tap', mqtt: 'MQTT', none: 'Measured by its meters' },

    // How a device is reached, in one set of words for the list, the Overview
    // and the component page: its protocol as it goes on the wire (UDP and
    // ASCII named as such), the address that protocol has (a broker, a URL's
    // host, a serial line, a bridge's bus), a unit only where there is one,
    // and whether values are pushed to it (no poll rate to show).
    _reachWords(e) {
        const p = e.protocol || 'tcp';
        const c = e.connection || {};
        const ser = e.serial || {};
        let address;
        if (p === 'none') {
            return { proto: this.t('ddv.meterOnlyProto', 'Measured by its meters'),
                     address: (e.measured_by || []).join(', ') || '—', unit: null, pushed: true, link: false };
        }
        if (e.bridge) {
            address = `${e.bridge} · ${this.t('devices.reach.bus', 'bus')} :${e.bridge_port ?? '—'}`;
        } else if (p === 'http') {
            address = (c.url || e.http_url || '').replace(/^https?:\/\//, '').split('/')[0] || '—';
        } else if (p === 'mqtt') {
            const broker = e.mqtt_in_broker || c.broker || '—';
            address = `${broker}${c.port ? ':' + c.port : ''}${c.tls ? ' · TLS' : ''}`
                + ((e.mqtt_in_topic || c.topic) ? ' · ••••' : '');
        } else if (this._SERIAL_PROTOCOLS.includes(p)) {
            const port = ser.serial_port || c.serial_port || '—';
            const baud = ser.baudrate || c.baudrate;
            const line = baud ? ` · ${baud} ${c.bytesize || ser.bytesize || 8}${c.parity || ser.parity || 'N'}${c.stopbits || ser.stopbits || 1}` : '';
            address = port + line;
        } else {
            address = `${e.host || c.host || '—'}:${e.port ?? c.port ?? '—'}`;
        }
        return {
            proto: this._protoLabel(p),
            address,
            unit: (p === 'http' || p === 'mqtt') ? null : (e.unit_id ?? c.unit_id ?? null),
            pushed: p === 'mqtt' || p === 'rtu_tap',
            // a connection that means something about the device: a TCP
            // session or a broker. A datagram socket or an open tty does not.
            link: ['tcp', 'rtu-tcp', 'ascii-tcp', 'mqtt'].includes(p),
        };
    },

    // the data's verdict, in words — the dot's colour said aloud
    _verdictWord(e) {
        const t = (k, d) => this.t(k, d);
        if (e.enabled === false) return t('devices.verdict.disabled', 'Disabled');
        // no protocol of its own: it is down when its meters are
        if (e.protocol === 'none' && e.data_health === 'down') return t('devices.verdict.metersDown', 'Its meters are not reporting');
        return ({
            ok: t('devices.verdict.ok', 'Reading'),
            degraded: t('devices.verdict.degraded', 'Degraded'),
            down: t('devices.verdict.down', 'No answer'),
        })[e.data_health] || t('devices.verdict.idle', 'Idle');
    },

    // the list row's right column: how fresh, and how often where polled
    _deviceStatsWords(e, reach) {
        const t = (k, d, p) => this.t(k, d, p);
        if (e.enabled === false) return t('devices.verdict.disabled', 'Disabled');
        const age = e.staleness_age_s != null ? `${e.staleness_age_s}s ${t('devices.age', 'age')}` : null;
        if (!reach.pushed && e.poll_rate != null && (e.connected || !reach.link)) {
            return `${Number(e.poll_rate).toFixed(1)} poll/s · ${age || '— ' + t('devices.age', 'age')}`;
        }
        if (reach.pushed && age) return t('devices.reach.lastValue', 'last value {age} ago', { age: e.staleness_age_s + 's' });
        if (reach.link && !e.connected) return t('devices.verdict.notConnected', 'Not connected');
        return this._verdictWord(e);
    },

    // Every way this unit is read, one line each: protocol, address, rhythm,
    // cost and verdict — the same shape the installation page uses.
    _unitReadViaLines(rv) {
        const t = this.t.bind(this);
        const hc = { ok: 'var(--success,#22c55e)', degraded: 'var(--warning,#f59e0b)',
                     down: 'var(--danger,#ef4444)', idle: 'var(--text-secondary,#8a94a0)' };
        return (rv || []).map(r => {
            const bits = [
                `${this._PROTO_LABEL[r.protocol] || this._esc(r.protocol)}${r.protocol === 'http' ? '' : ' ' + this._esc(r.address || '')}`,
                // a tap asks nothing: it hears the bus as the master asks
                r.protocol === 'rtu_tap' ? t('endpoints.listening', 'listening')
                    : r.interval_s != null ? `${t('endpoints.every', 'every')} ${r.interval_s} s` : null,
                r.latency_ms != null ? `~${Math.round(r.latency_ms)} ms` : null,
                r.fail_pct_5m != null ? `${r.fail_pct_5m} % ${t('endpoints.failed5m', 'failed (5 min)')}` : null,
                `${t('devices.provides', 'provides')} ${r.provides} ${t('endpoints.srcFields', 'fields')}`,
            ].filter(Boolean);
            return `<div style="display:flex;gap:8px;align-items:baseline;font-size:12.5px;padding:2px 0;flex-wrap:wrap;" data-read-via="${this._esc(r.id)}">
                <span class="status-dot" style="--dot:${hc[r.status] || hc.idle}" aria-hidden="true"></span>
                <b>${this._esc(r.id)}</b>
                <span style="color:var(--text-secondary);">${this._esc(r.status || 'idle')}</span>
                <span>${bits.join(' · ')}</span></div>`;
        }).join('');
    },

    // The "Read via" panel of a unit: the truth about how it is read, and the
    // one thing that is the unit's own — its name. Sources, intervals and the
    // template belong to the installation and are edited there.
    _unitReadViaHtml(s, d) {
        const t = this.t.bind(this);
        const rv = (s.entry || {}).read_via || [];
        const rows = rv.map((r, i) => `<tr data-src-row="${this._esc(r.id)}" style="border-top:1px solid var(--border,#2a3038);">
            <td style="padding:8px 10px 8px 0;white-space:nowrap;"><span class="dev-chip">#${i + 1}</span> <b>${this._esc(r.id)}</b>
                ${r.enabled === false ? `<span class="sink-pill warn" style="margin-left:6px;">${t('devices.disabled', 'disabled')}</span>` : ''}</td>
            <td style="padding:8px 10px 8px 0;">${this._PROTO_LABEL[r.protocol] || this._esc(r.protocol)}</td>
            <td style="padding:8px 10px 8px 0;font-size:12px;word-break:break-all;max-width:300px;">${this._esc(r.address || '')}</td>
            <td style="padding:8px 10px 8px 0;font-size:12px;">${this._esc(r.template || '—')}</td>
            <td style="padding:8px 10px 8px 0;white-space:nowrap;">${r.interval_s != null ? `${r.interval_s} s` : '—'}</td>
            <td style="padding:8px 10px 8px 0;white-space:nowrap;">${r.stale_after_s ? `${r.stale_after_s} s` : t('endpoints.never', 'never')}</td>
            <td style="padding:8px 10px 8px 0;white-space:nowrap;font-variant-numeric:tabular-nums;">${r.provides} / ${r.registers}</td>
            <td style="padding:8px 0;white-space:nowrap;font-variant-numeric:tabular-nums;">${this._esc(r.status || 'idle')}${r.latency_ms != null ? ` · ${Math.round(r.latency_ms)} ms` : ''}${r.fail_pct_5m ? ` · <span style="color:var(--danger,#ef4444);">${r.fail_pct_5m} % ${t('endpoints.failed5m', 'failed (5 min)')}</span>` : ''}</td>
        </tr>`).join('');
        return `
        <div class="field-hint" style="margin:0 0 12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
            <span><i aria-hidden="true" class="bi bi-diagram-3"></i> ${t('devices.unit.readViaIntro', 'This unit is read as part of')} <b>${this._esc(s.endpoint_name || s.endpoint_id)}</b>. ${t('devices.unit.readViaIntro2', 'Its sources, intervals and template are set there.')}</span>
            <button class="btn btn-secondary btn-sm" ${this._act('openEndpointDetail', [s.endpoint_id])}><i aria-hidden="true" class="bi bi-diagram-3"></i> ${t('devices.openInstallation', 'Open installation')}</button>
        </div>
        <div class="settings-card">
            <div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-tag"></i> ${t('devices.unit.identity', 'Name')}</h3></div>
            <div class="settings-card-body">
                <div class="form-row align-end">
                    <div class="form-group flex-2"><label class="form-label" for="ddvName">${t('devices.wizard.name', 'Device name')}</label>
                        <input type="text" id="ddvName" class="input" value="${this._esc(d.name)}"></div>
                    <div class="form-group"><button data-admin class="btn btn-primary btn-sm" ${this._act('saveUnitName', [s.id], { el: true })}><i aria-hidden="true" class="bi bi-check-lg"></i> ${t('common.save', 'Save')}</button>
                        <span class="save-feedback" id="ddvNameFeedback" role="status"></span></div>
                </div>
                <div class="field-hint">${t('devices.unit.nameHint', 'The only thing that is this unit’s own. Its id, topics and bucket stay as they are.')}</div>
            </div>
        </div>
        <div class="settings-card">
            <div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-diagram-3"></i> ${t('devices.tab.readVia', 'Read via')}</h3></div>
            <div class="settings-card-body">
                <div style="overflow-x:auto;"><table style="width:100%;font-size:13px;">
                    <tr style="color:var(--text-secondary);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;">
                        <td style="padding-right:10px;">${t('endpoints.srcName', 'source')}</td>
                        <td style="padding-right:10px;">${t('devices.wizard.protocol', 'Protocol')}</td>
                        <td style="padding-right:10px;">${t('endpoints.srcAddress', 'address')}</td>
                        <td style="padding-right:10px;">${t('devices.wizard.template', 'Template')}</td>
                        <td style="padding-right:10px;">${t('devices.unit.interval', 'interval')}</td>
                        <td style="padding-right:10px;">${t('endpoints.srcStale', 'stale after')}</td>
                        <td style="padding-right:10px;" title="${t('devices.unit.providesHint', 'Fields this source supplies right now / fields selected from it')}">${t('endpoints.srcOwns', 'provides')}</td>
                        <td>${t('endpoints.srcLive', 'live')}</td></tr>
                    ${rows || `<tr><td colspan="8"><span class="field-hint">${t('endpoints.srcNone', 'No source declared yet.')}</span></td></tr>`}
                </table></div>
                <p class="field-hint" style="margin-top:12px;"><i aria-hidden="true" class="bi bi-list-check"></i>
                    ${t('devices.unit.tickHint', 'What is read from each source is ticked in the Measurements tab — pick the source there.')}</p>
            </div>
        </div>`;
    },

    async saveUnitName(deviceId, btn) {
        const s = this._devDetail;
        const fb = document.getElementById('ddvNameFeedback');
        const name = (document.getElementById('ddvName')?.value || '').trim();
        if (!s?.endpoint_id) return;
        if (btn) btn.disabled = true;
        try {
            const p = await (await fetch(`/api/endpoints/${encodeURIComponent(s.endpoint_id)}`)).json();
            const body = this._endpointPutBody(p);
            const u = body.units.find(x => x.id === deviceId);
            if (!u) return;
            if (name) u.name = name; else delete u.name;
            const rsp = await fetch(`/api/endpoints/${encodeURIComponent(s.endpoint_id)}`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            const d = await rsp.json().catch(() => ({}));
            if (!rsp.ok) {
                const errs = d.detail?.errors || [d.detail || rsp.statusText];
                if (fb) { fb.textContent = Array.isArray(errs) ? errs.join(' · ') : String(errs); fb.className = 'save-feedback err'; }
                return;
            }
            const made = (d.devices || []).find(x => x.id === deviceId);
            s.data.name = made?.name || name || s.data.name;
            const crumb = document.querySelector('#deviceDetailView .section-header h2 .ddv-name');
            if (crumb) {
                crumb.innerHTML = `${this._kindIconHtml ? this._kindIconHtml(s.entry?.kind) : ''}${this._esc(s.data.name)}`;
                crumb.title = s.data.name;
            }
            if (fb) { fb.textContent = '✓ ' + this.t('settings.saved', 'Saved & applied'); fb.className = 'save-feedback ok'; setTimeout(() => { if (fb.classList.contains('ok')) fb.textContent = ''; }, 3000); }
        } finally { if (btn) btn.disabled = false; }
    },

    // Read-only status view — what you see first, no forms.
    _deviceOverviewHtml(d, entry) {
        const t = this.t.bind(this);
        const health = entry.data_health || (entry.connected ? 'ok' : 'idle');
        const hColor = { ok: 'var(--success,#22c55e)', degraded: 'var(--warning,#f59e0b)',
                         down: 'var(--danger,#ef4444)', idle: 'var(--text-secondary,#8a94a0)' };
        if ((entry.read_via || []).length) {
            // a unit of an installation: what it shows now, how it is read,
            // where it publishes — no fallback connection, no template dash
            const live = entry.live || {}, meta = entry.fields || {};
            const glance = (entry.glance || Object.keys(live)).filter(n => live[n] != null).map(n => `<div style="min-width:110px;">
                <div style="color:var(--text-secondary);font-size:11.5px;">${this._esc((meta[n] || {}).label || (this._liveLabel ? this._liveLabel(n) : n))}</div>
                <div style="font-weight:700;font-size:20px;letter-spacing:-.3px;font-variant-numeric:tabular-nums;">${this._endpointValue ? this._endpointValue(live[n], (meta[n] || {}).unit || '') : live[n]}</div></div>`).join('');
            return `
        <div class="settings-card">
            <div class="settings-card-body">
                <div style="display:flex;flex-wrap:wrap;gap:18px 36px;align-items:flex-start;">
                    <div style="min-width:150px;">
                        <div style="display:flex;align-items:center;gap:10px;">
                            <span class="status-dot" style="width:12px;height:12px;border-radius:50%;--dot:${hColor[health]};background:var(--dot);box-shadow:0 0 0 3px color-mix(in srgb, var(--dot) 16%, transparent);" aria-hidden="true"></span>
                            <span style="font-weight:600;font-size:15px;">${this._esc(this._verdictWord(entry))}</span>
                        </div>
                        <div style="color:var(--text-secondary);font-size:12px;margin-top:8px;">${t('devices.overview.lastRead', 'Last read')} ${entry.staleness_age_s != null ? entry.staleness_age_s + ' s ago' : '—'}</div>
                    </div>
                    <div style="display:flex;gap:18px 32px;flex-wrap:wrap;">${glance || `<span class="field-hint">${t('devices.overview.noValues', 'No values yet — the device may not be polling.')}</span>`}</div>
                    ${((entry.alarms || {}).active || []).length ? `<div style="flex-basis:100%;display:flex;gap:6px;flex-wrap:wrap;">${entry.alarms.active.map(a =>
                        `<span class="sink-pill ${a.severity === 'danger' ? 'bad' : 'warn'}"><i aria-hidden="true" class="bi bi-exclamation-triangle"></i> ${this._esc(a.label)}: ${this._esc(String(a.value))}</span>`).join('')}</div>` : ''}
                </div>
                <div style="margin-top:16px;">
                    <div style="color:var(--text-secondary);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;margin-bottom:4px;">${t('endpoints.readVia', 'Read via')}</div>
                    ${this._unitReadViaLines(entry.read_via)}
                </div>
                <div style="margin-top:12px;font-size:12.5px;display:flex;flex-wrap:wrap;gap:4px 8px;align-items:baseline;">
                    <span style="color:var(--text-secondary);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;">${t('endpoints.publishesTo', 'Publishes to')}</span>
                    <code style="font-size:11px;">${this._esc(d.topic_prefix || '')}/…</code> → InfluxDB <code style="font-size:11px;">${this._esc(d.bucket || '')}</code> · ${this._esc(d.device_tag || '')}
                </div>
            </div>
        </div>
        <div class="settings-card">
            <div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-activity"></i> ${t('devices.overview.live', 'Live values')}</h3></div>
            <div class="settings-card-body"><div id="ddvSnapshot"><span class="field-hint">${t('common.loading', 'Loading…')}</span></div></div>
        </div>`;
        }
        const reach = this._reachWords(entry);
        const src = d.protocol === 'http' ? this._esc(d.url || '—')
                  : `${this._esc(reach.address)}${reach.unit != null ? ` · unit ${reach.unit}` : ''}`;
        const proto = this._esc(reach.proto);
        const fact = (label, val) => `<div><div style="color:var(--text-secondary);font-size:11.5px;">${label}</div><div style="font-weight:600;font-size:13.5px;">${val}</div></div>`;
        const mp = this._sinkStatusPill(this._devDetail.sinks?.mqtt, d.mqtt_enabled, entry.connected);
        const ip = this._sinkStatusPill(this._devDetail.sinks?.influxdb, d.influxdb_enabled, entry.connected);
        return `
        <div class="settings-card">
            <div class="settings-card-body">
                <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;">
                    <span class="status-dot" style="width:12px;height:12px;border-radius:50%;--dot:${hColor[health]};background:var(--dot);box-shadow:0 0 0 3px color-mix(in srgb, var(--dot) 16%, transparent);"></span>
                    <span style="font-weight:600;font-size:15px;" data-verdict="${this._esc(entry.enabled === false ? 'disabled' : health)}">${this._esc(this._verdictWord(entry))}</span>
                </div>
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px 24px;margin-bottom:8px;">
                    ${fact(t('devices.overview.source', 'Source'), `${proto}<br><span style="font-weight:400;font-size:12px;color:var(--text-secondary);word-break:break-all;">${src}</span>`)}
                    ${fact(t('devices.overview.template', 'Template'), this._esc(d.template || '—'))}
                    ${this._kindLabel ? fact(t('find.facet.kind', 'Kind'), `${this._kindCellHtml(entry.kind)}<br><span style="font-weight:400;font-size:12px;color:var(--text-secondary);" data-kind-source="${this._esc(entry.kind_source || '')}">${this._esc(this._kindSourceWords(entry.kind_source))}</span>`) : ''}
                    ${fact(t('devices.overview.registers', 'Measurements polled'), d.selected_registers)}
                    ${reach.link ? fact(t('devices.overview.link', 'Link'), entry.connected ? t('devices.connected', 'Connected') : t('devices.verdict.notConnected', 'Not connected')) : ''}
                    ${reach.pushed ? '' : fact(t('devices.overview.pollRate', 'Poll rate'), entry.poll_rate != null ? entry.poll_rate + '/s' : '—')}
                    ${fact(t('devices.overview.lastRead', 'Last read'), entry.staleness_age_s != null ? entry.staleness_age_s + 's ago' : '—')}
                    ${fact('MQTT', `${mp} <code style="font-size:11px;">${this._esc(d.topic_prefix || '')}</code>`)}
                    ${fact('InfluxDB', `${ip} <code style="font-size:11px;">${this._esc(d.bucket || '')}</code>`)}
                </div>
            </div>
        </div>
        <div class="settings-card">
            <div class="settings-card-header"><h3><i aria-hidden="true" class="bi bi-activity"></i> ${t('devices.overview.live', 'Live values')}</h3></div>
            <div class="settings-card-body"><div id="ddvSnapshot"><span class="field-hint">${t('common.loading', 'Loading…')}</span></div></div>
        </div>`;
    },

    async _loadDeviceSnapshot(id) {
        const box = document.getElementById('ddvSnapshot');
        if (!box || !id) return;
        try {
            const [d, sel] = await Promise.all([
                fetch('/api/values?device=' + encodeURIComponent(id)).then(r => r.json()),
                fetch('/api/registers/selected?device=' + encodeURIComponent(id)).then(r => r.json()).catch(() => ({})),
            ]);
            // group the way the dashboard does: the template's category (with
            // its label), else the canonical one — never the name's first word
            const catBy = {};
            (sel.registers || []).forEach(r => { catBy[r.name] = r; });
            const vals = Object.values(d.values || {}).map(v => {
                const r = catBy[v.name] || {};
                return { ...v, category: v.category || r.category, category_label: r.category_label };
            });
            if (!vals.length) { box.innerHTML = `<span class="field-hint">${this.t('devices.overview.noValues', 'No values yet — the device may not be polling.')}</span>`; return; }
            const bitmasks = (sel.display || {}).bitmasks || {};
            // a mask says what is set; a number goes through the one value
            // formatter the dashboard uses (unit scaling, separators, durations)
            const fmt = v => {
                if (bitmasks[v.name] && typeof v.value === 'number') return { text: this._esc(this._bitList(v.value, bitmasks[v.name])), unit: '' };
                const f = this._fmtValue(v.value, v.unit);
                return { text: this._esc(f.text), unit: f.unit };
            };
            // grouped by what they measure; a unit read two ways says which
            // source each value came from
            const manySources = ((this._devDetail?.entry || {}).read_via || []).length > 1;
            const catOf = v => {
                if (v.category_label) return v.category_label;
                const c = v.category || 'other';
                return this.t(`sec.${c}`, c.charAt(0).toUpperCase() + c.slice(1).replace(/_/g, ' '));
            };
            const groups = new Map();
            vals.slice(0, 120).forEach(v => { const k = catOf(v); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(v); });
            const row = v => `<tr>
                <td style="padding:2px 12px 2px 0;color:var(--text-secondary);">${this._esc(v.label || v.name || '')}${v.label && v.name && v.label !== v.name ? ` <span style="color:var(--text-tertiary,#9aa4af);font-size:11px;">${this._esc(v.name)}</span>` : ''}</td>
                <td style="padding:2px 0;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;">${(f => `${f.text} <span style="color:var(--text-secondary);">${this._esc(f.unit)}</span>`)(fmt(v))}${manySources && v.source ? ` <span class="dev-chip" style="margin-left:6px;" title="${this.t('devices.valueSource', 'Source of this value')}">${this._esc(v.source)}</span>` : ''}</td></tr>`;
            box.innerHTML = [...groups.entries()].map(([cat, items]) => `
                <div style="color:var(--text-secondary);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;margin:10px 0 2px;">${this._esc(cat)}</div>
                <table style="width:100%;font-size:13px;">${items.map(row).join('')}</table>`).join('');
        } catch (e) { box.innerHTML = `<span class="field-hint">${this.t('common.error', 'Error')}</span>`; }
    },

    async _loadDevicePollGroups(id) {
        const box = document.getElementById('ddvPollGroups');
        if (!box || !id) return;
        try {
            const d = await (await fetch(`/api/devices/${encodeURIComponent(id)}/poll-groups`)).json();
            const groups = d.poll_groups || {};
            if (this._devDetail?.id !== id) return;
            box.innerHTML = Object.entries(groups).map(([name, g]) => `
                <div class="form-group" style="display:flex;align-items:center;gap:8px;margin:4px 0;flex-wrap:wrap;">
                    <label style="width:90px;font-size:13px;" for="ddvPg-${this._esc(name)}">${this._esc(name)}</label>
                    <input type="text" inputmode="decimal" class="input input-sm ddv-pg" id="ddvPg-${this._esc(name)}" data-group="${this._esc(name)}"
                           data-orig="${g.interval}" value="${g.interval}" style="width:110px;" aria-label="${this._esc(name)} interval seconds">
                    <span style="color:var(--text-secondary);font-size:12px;">${this._esc(g.description || '')}</span>
                </div>`).join('') || `<span class="field-hint">${this.t('devices.noPollGroups', 'No poll groups.')}</span>`;
        } catch (e) {
            box.innerHTML = `<span class="field-hint">${this.t('common.error', 'Error')}</span>`;
        }
    },

    // the intervals the Edit tab shows: only the groups changed since it
    // loaded (the server keeps the others), and the inputs that are wrong
    _pollGroupEdits() {
        const changed = {}, fields = {};
        document.querySelectorAll('#ddvPollGroups .ddv-pg').forEach(inp => {
            const raw = inp.value.trim().replace(',', '.');
            const v = /^\d+(\.\d*)?$|^\.\d+$/.test(raw) ? Number(raw) : NaN;
            if (!Number.isFinite(v) || v < 0.05 || v > 86400) {
                fields[`poll_groups.${inp.dataset.group}`] = this.t('devices.pgRange', 'Seconds, from 0.05 to 86400');
            } else if (v !== Number(inp.dataset.orig)) {
                changed[inp.dataset.group] = { interval: v };
            }
        });
        return { changed, fields };
    },

    // Errors put at their inputs: {path: message} → the field it names is
    // marked and told, the first one focused. What has no input on this
    // page is returned, for the form's own feedback line.
    _placeFieldErrors(scope, fields, idOf) {
        if (!scope) return Object.entries(fields || {}).map(([k, v]) => `${k}: ${v}`);
        scope.querySelectorAll('.field-error[data-placed]').forEach(e => e.remove());
        // the error's id is added to what describes the input (its hints
        // stay), and only that id is taken away again
        const describe = (el, id, on) => {
            const ids = (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(x => x && x !== id);
            if (on) ids.push(id);
            if (ids.length) el.setAttribute('aria-describedby', ids.join(' '));
            else el.removeAttribute('aria-describedby');
        };
        const clear = el => {
            el.removeAttribute('aria-invalid');
            if (el.dataset.placedInvalid) describe(el, el.dataset.placedInvalid, false);
            el.removeAttribute('data-placed-invalid');
        };
        scope.querySelectorAll('[data-placed-invalid]').forEach(clear);
        const left = [];
        let first = null;
        for (const [path, msg] of Object.entries(fields || {})) {
            // a field a picker stands in for: the error on the box a person
            // sees, named after the field (#<field>-err) as before
            const field = idOf(path) && document.getElementById(idOf(path));
            const el = this._pickVisible(field);
            // an input not shown (another mode's section, display:none) cannot
            // carry its error: it is said on the form's own line instead
            if (!el || !scope.contains(el) || el.closest('[hidden]') || !el.getClientRects().length) { left.push(`${path}: ${msg}`); continue; }
            const err = document.createElement('div');
            err.className = 'field-error';
            err.dataset.placed = '';
            err.id = `${field.id}-err`;
            err.textContent = msg;
            el.setAttribute('aria-invalid', 'true');
            el.setAttribute('data-placed-invalid', err.id);
            describe(el, err.id, true);
            (el.closest('.form-group') || el.parentElement).appendChild(err);
            el.addEventListener('input', () => { clear(el); err.remove(); }, { once: true });
            first = first || el;
        }
        if (first) first.focus();
        return left;
    },

    // a device page path → its input: the page's own (name, template,
    // intervals), the connection form's for a connection path
    _ddvFieldId(path) {
        if (path.startsWith('poll_groups.')) return `ddvPg-${path.slice('poll_groups.'.length)}`;
        if (path.startsWith('connection.')) return (this._cf || {}).ddv?.fieldId(path) || null;
        return ({ name: 'ddvName', template: 'ddvTemplate' })[path] || null;
    },

    // a device's Edit tab, from anywhere (the bridge card's move button)
    async openDeviceEdit(id) {
        await this.openDeviceDetail(id);
        if (this._devDetail?.id === id) this._switchDeviceTab('connection');
    },

    // a whole number as typed, else null — "1.5" or "abc" is not a unit id,
    // and a blank one is said, never sent as a silent 1
    _intOrNull(v) {
        const s = String(v ?? '').trim();
        return /^-?\d+$/.test(s) ? parseInt(s, 10) : null;
    },

    // the one Unit ID rule of every device form: a whole number 0–255
    _unitIdError(v) {
        return (v === null || v < 0 || v > 255)
            ? this.t('devices.unitIdNeeded', 'Unit ID: a whole number 0–255 (most devices answer on 1).')
            : null;
    },

    _gatherDeviceDetail() {
        const g = id => document.getElementById(id);
        const s = this._devDetail, d = s.data;
        // protocol is fixed after creation — fall back to the STORED protocol,
        // never to 'tcp' (a missing radio must not silently rewrite rtu-tcp)
        d.protocol = s.data.protocol || 'tcp';
        d.name = g('ddvName')?.value.trim() || s.id;
        d.template = g('ddvTemplate')?.value || '';
        d.enabled = !!g('ddvEnabled')?.checked;
        // The outputs are switched from their own tab, at once; an edit sends
        // them as they stand (the primary always routes).
        if (s.primary) { d.mqtt_enabled = true; d.influxdb_enabled = true; }
        if (d.protocol === 'none') d.template = '';
        return d;
    },

    // Test on the Connection tab: the form's (the saved device when nothing
    // is edited, else the connection as the form holds it)
    deviceDetailTest(btn) {
        return this._cfTest('ddv', btn);
    },

    // a Test's answer: ok / no answer to give (null: a pushed source not yet
    // heard, a tap) / failed — and a refusal says why, not an empty failure
    _testVerdict(r, res) {
        if (!r.ok) {
            const det = res.detail;
            return { cls: 'err', mark: '✗', text: det?.errors?.join(' · ') || (typeof det === 'string' ? det : r.statusText) };
        }
        if (res.ok === null || res.ok === undefined) return { cls: 'info', mark: 'ℹ', text: res.message || '' };
        return res.ok ? { cls: 'ok', mark: '✓', text: res.message || '' } : { cls: 'err', mark: '✗', text: res.message || '' };
    },

    _showTestResult(out, r, res) {
        const v = this._testVerdict(r, res);
        out.className = 'wiz-test-result ' + v.cls;
        out.textContent = `${v.mark} ${v.text}`;
    },












    // PQ recorder card (Jasic/Janitza only — see docs/pq-recorder.md), on the
    // Power Quality tab with the events it records. The live status line
    // (running / last poll / counters) is filled by _loadPqSinkStatus.
    _pqRecorderCard(d, primary) {
        const pq = this._devDetail.entry?.pq_recorder || d.pq_recorder || {};
        const on = !!pq.enabled;
        return `
        <div class="settings-card">
            <div class="settings-card-header">
                <h3><i aria-hidden="true" class="bi bi-activity"></i> ${this.t('pq.sinkTitle', 'PQ event recorder')}</h3>
                <label class="switch-label"><input type="checkbox" id="ddvPqEnabled" ${on ? 'checked' : ''}><span>${this.t('devices.sink.enable', 'Enable')}</span></label>
            </div>
            <div class="settings-card-body">
                <div class="form-row">
                    <div class="form-group" style="max-width:130px"><label class="form-label" for="ddvPqPoll">${this.t('pq.pollS', 'Poll (s)')}</label>
                        <input type="number" id="ddvPqPoll" class="input" min="30" value="${pq.poll_s || 300}"></div>
                    <div class="form-group flex-2"><label class="form-label" for="ddvPqBaseUrl">${this.t('pq.baseUrl', 'Base URL')}</label>
                        <input type="text" id="ddvPqBaseUrl" class="input" value="${this._esc(pq.base_url || '')}" placeholder="${this._esc(this._pqDefaultUrl(d))}">
                        <div class="field-hint" id="ddvPqBaseUrlHint">${this._esc(this.t('pq.baseUrlBlank', 'Blank: {url}', { url: this._pqDefaultUrl(d) }))}</div></div>
                </div>
                <label style="display:flex;align-items:center;gap:8px;margin-top:6px;">
                    <input type="checkbox" id="ddvPqArchive" ${pq.archive_waveforms !== false ? 'checked' : ''}> ${this.t('pq.archiveWf', 'Archive event waveforms (RMS traces of the implicated channels)')}</label>
                <div class="rest-status" id="ddvPqStatus"></div>
                <div class="calc-editor-actions" style="margin-top:12px">
                    <button data-admin class="btn btn-primary btn-sm" ${this._act('savePqRecorder', [this._devDetail.id])}><i aria-hidden="true" class="bi bi-check-lg"></i> ${this.t('common.save', 'Save')}</button>
                </div>
                <p class="field-hint"><i aria-hidden="true" class="bi bi-info-circle"></i> ${this.t('pq.sinkNote', 'Archives the on-device PQ event ring (dips, outages, rapid voltage changes) + waveforms into InfluxDB, publishes new events to MQTT and the alert channels. Browse them in the Power Quality tab.')}</p>
            </div>
        </div>`;
    },

    // what a blank Base URL means: the device's own host over HTTP
    _pqDefaultUrl(d) {
        const host = (d?.connection || {}).host || d?.host || (this._devDetail?.data || {}).host || '';
        return host ? `http://${host}` : 'http://<host>';
    },

    async savePqRecorder(id) {
        const payload = {
            enabled: document.getElementById('ddvPqEnabled').checked,
            poll_s: parseInt(document.getElementById('ddvPqPoll').value) || 300,
            archive_waveforms: document.getElementById('ddvPqArchive').checked,
            base_url: document.getElementById('ddvPqBaseUrl').value.trim(),
        };
        try {
            const r = await fetch(`/api/pq/config?device=${encodeURIComponent(id)}`,
                { method: 'POST', headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify(payload) });
            if (!r.ok) throw new Error((await r.json()).detail || 'failed');
            if (this._devDetail?.entry) this._devDetail.entry.pq_recorder = (await r.json()).pq_recorder;
            this.showToast('success', this.t('pq.sinkTitle', 'PQ event recorder'), this.t('settings.saved', 'Saved & applied'));
            this._fetchDevices(true);
            this._loadPqSinkStatus(id);
            this._loadPqStatus?.();          // the tab's own "not enabled" line
        } catch (e) {
            this.showToast('error', this.t('pq.sinkTitle', 'PQ event recorder'), String(e.message || e));
        }
    },

    async _loadPqSinkStatus(id) {
        const el = document.getElementById('ddvPqStatus');
        if (!el) return;
        try {
            const r = await fetch('/api/pq/status');
            if (!r.ok) return;
            const dev = ((await r.json()).devices || []).find(x => x.device === id);
            if (!dev) return;
            if (!dev.enabled) { el.textContent = ''; return; }
            const bits = [];
            bits.push(dev.running ? `✓ ${this.t('pq.running', 'recorder running')}`
                                  : `✕ ${this.t('pq.notRunning', 'recorder not running')}`);
            if (dev.last_poll) bits.push(`${this.t('pq.lastPoll', 'last poll')} ${new Date(dev.last_poll * 1000).toLocaleTimeString(this._loc())}`);
            const c = dev.counters || {};
            if (c.events != null) bits.push(`${c.events} ${this.t('pq.eventsWord', 'events')} / ${c.transients ?? 0} ${this.t('pq.transients', 'transients')}`);
            if (dev.last_error) bits.push(`${this.t('pq.lastError', 'last error')}: ${dev.last_error}`);
            el.textContent = bits.join(' · ');
        } catch (e) { /* best-effort status line */ }
    },







    async copyText(text, btn) {
        try { await navigator.clipboard.writeText(text); }
        catch (e) { /* clipboard blocked (non-HTTPS/older browser) — the field is selectable as fallback */ }
        if (btn) {
            const o = btn.innerHTML;
            btn.innerHTML = '<i aria-hidden="true" class="bi bi-check-lg"></i>';
            setTimeout(() => { btn.innerHTML = o; }, 1200);
        }
    },

    // a command the template offers, enabled (or not) on this device of its own
    async toggleDeviceCommand(id, name, el) {
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(id)}/commands/${encodeURIComponent(name)}/enabled`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: el.checked }) });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error((d.detail?.errors || [d.detail || r.statusText]).join(' '));
            this.showToast('success', this.t('commands.tab', 'Commands'), `${name}: ${el.checked ? this.t('commands.nowEnabled', 'enabled') : this.t('commands.nowDisabled', 'disabled')}`);
        } catch (e) {
            el.checked = !el.checked;
            this.showToast('error', this.t('commands.tab', 'Commands'), String(e.message || e));
        }
        await this._fetchDevices?.(true);
        this._initDeviceCommands(id);
    },

    // sends an enabled, stopped device's settings again: the gateway starts
    // its acquisition (update_device saves a stalled device even unchanged)
    reconnectDevice() {
        return this.saveDeviceDetail(document.querySelector('#deviceDetailView [data-action="saveDeviceDetail"]'), true);
    },

    async saveDeviceDetail(btn, reconnect = false) {
        const d = this._gatherDeviceDetail();
        const s = this._devDetail;
        const fb = document.getElementById('ddvFeedback');
        const panel = document.querySelector('#deviceDetailView [data-dpanel="connection"]');
        const say = (text, ok) => { if (fb) { fb.textContent = text; fb.className = `save-feedback ${ok ? 'ok' : 'err'}`; } };
        // everything wrong that the page can see is said at its input before
        // anything is sent: a bad interval never leaves a half-saved edit
        const pg = this._pollGroupEdits();
        const form = d.protocol === 'none' ? null : (this._cf || {}).ddv;
        const conn = form ? form.read() : { errors: {} };
        const local = { ...pg.fields, ...conn.errors };
        if (Object.keys(local).length) {
            const left = this._placeFieldErrors(panel, local, p => this._ddvFieldId(p));
            say(left.length ? left.join(' · ') : this.t('devices.fixMarked', 'Fix the marked fields.'), false);
            return;
        }
        this._placeFieldErrors(panel, {}, () => null);
        // the connection says only what was changed in it ({} = nothing)
        const connPayload = form ? form.payload() : {};
        const payload = this._ddvPayload(s.id, d, connPayload);
        // only intervals changed: the device itself is not saved (a save of
        // the primary meter reconnects it, of any device writes its file)
        const pgChanged = Object.keys(pg.changed).length > 0;
        const formSame = !Object.keys(connPayload).length
            && JSON.stringify(this._ddvPayload(s.id, d)) === JSON.stringify(this._ddvPayload(s.id, s.orig));
        // nothing edited: nothing is sent (FINDINGS PROD-1 — an untouched
        // Save of the primary reconnected the meter live systems read). An
        // enabled device that is not running is revived by saving it again:
        // that is offered as its own action, never done by a plain Save
        if (formSame && !pgChanged && !reconnect) {
            if (d.enabled && s.running === false && fb) {
                fb.className = 'save-feedback err';
                fb.replaceChildren(document.createTextNode(
                    this.t('devices.notRunningReconnect', 'Nothing to save — but this device is not running.') + ' '));
                const b = document.createElement('button');
                b.type = 'button';
                b.className = 'btn btn-sm btn-secondary';
                b.id = 'ddvReconnect';
                b.dataset.action = 'reconnectDevice';
                b.setAttribute('data-admin', '');
                b.textContent = this.t('devices.reconnect', 'Reconnect');
                fb.append(b);
                return;
            }
            say(this.t('devices.nothingToSave', 'Nothing to save — nothing was sent.'), true);
            return;
        }
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<i aria-hidden="true" class="bi bi-arrow-repeat spin"></i>'; }
        if (fb) { fb.textContent = ''; fb.className = 'save-feedback'; }
        try {
            let res = { status: 'unchanged' };
            if (!(formSame && pgChanged)) {
                const r = await fetch(`/api/devices/${encodeURIComponent(s.id)}`, {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
                });
                res = await r.json().catch(() => ({}));
                if (!r.ok) {
                    const det = res.detail;
                    // each refusal at its input; the rest on the feedback line
                    const left = det?.fields
                        ? this._placeFieldErrors(panel, det.fields, p => this._ddvFieldId(p))
                        : [];
                    const keys = Object.keys(det?.fields || {});
                    const rest = [...left, ...(det?.errors || [typeof det === 'string' ? det : this.t('toast.saveFailed', 'Save Failed')])
                        .filter(e => !keys.some(k => String(e).startsWith(k + ': ')))];
                    say(rest.length ? rest.join(' · ') : this.t('devices.fixMarked', 'Fix the marked fields.'), false);
                    return;
                }
            }
            const unchanged = res.status === 'unchanged';
            // the intervals go after the device: only the groups changed, the
            // server keeps the others
            let pgErr = null;
            if (pgChanged) {
                try {
                    const pr = await fetch(`/api/devices/${encodeURIComponent(s.id)}/poll-groups`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ poll_groups: pg.changed }) });
                    if (!pr.ok) pgErr = this._respError(pr, await pr.json().catch(() => ({})));
                } catch (e) { pgErr = e.message || 'failed'; }
            }
            if (unchanged && !pgChanged) {
                say(this.t('devices.unchanged', 'Nothing changed — nothing was restarted.'), true);
                return;
            }
            // a save that answers no `restarted` restarted acquisition (the
            // quiet path says restarted: false)
            const saved = unchanged ? null
                : res.restarted === false ? this.t('devices.savedKept', 'Saved — kept running')
                : this.t('devices.savedRestarted', 'Saved — restarted');
            if (!unchanged) this.showToast('success', this.t('devices.updated', 'Device updated'), d.name);
            if (pgErr) {
                // a half save is said as one, never a plain "Saved"; the page
                // stays as typed, the interval the gateway refused marked
                if (!unchanged) this._devDetail.orig = JSON.parse(JSON.stringify(d));
                const m = /^([^:\s]+): (.+)$/.exec(pgErr);
                if (m && pg.changed[m[1]]) this._placeFieldErrors(panel, { [`poll_groups.${m[1]}`]: m[2] }, p => this._ddvFieldId(p));
                say(unchanged
                    ? this.t('devices.intervalsFailed', 'Intervals NOT applied: {err}', { err: pgErr })
                    : this.t('devices.savedIntervalsFailed', 'Saved; intervals NOT applied: {err}', { err: pgErr }), false);
                return;
            }
            const words = [saved, pgChanged ? this.t('devices.intervalsApplied', 'Intervals applied') : null].filter(Boolean).join(' · ');
            // stay here, redrawn from what is stored now
            await this.openDeviceDetail(s.id);
            this._switchDeviceTab('connection');
            const fb2 = document.getElementById('ddvFeedback');
            if (fb2) { fb2.textContent = '✓ ' + words; fb2.className = 'save-feedback ok'; }
        } catch (e) {
            if (fb) { fb.textContent = e.message; fb.className = 'save-feedback err'; }
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = orig; }
        }
    },

    // the PUT a device page sends: its name, template, enabled and outputs as
    // shown, and the connection as the connection form says it — only what
    // was changed ({} keeps it whole: the server's rule)
    _ddvPayload(id, d, conn = {}) {
        // a device that IS its meters: its identity only — never an address
        // nor a line setting (FINDINGS MD-N3: a serial branch was sent and
        // refused, so it could not even be renamed)
        return window.ConnformModel.pagePayload(id, d, conn);
    },

    async testDevice(id, btn) {
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="btn-spinner"></span>'; }
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(id)}/test`, { method: 'POST' });
            const v = this._testVerdict(r, await r.json().catch(() => ({})));
            this.showToast(v.cls === 'ok' ? 'success' : v.cls === 'info' ? 'info' : 'error',
                           v.cls === 'ok' ? this.t('devices.testOk', 'Device answered')
                               : v.cls === 'info' ? this.t('devices.testInfo', 'Test') : this.t('devices.testFail', 'Test failed'),
                           v.text);
        } catch (e) {
            this.showToast('error', this.t('devices.testFail', 'Test failed'), e.message);
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = orig; }
        }
    },

    // a command writes to equipment: say what, with which values, on what
    _confirmCommand(cmd, said, target, extra = '') {
        const t = (k, d, p) => this.t(k, d, p);
        return this.confirmAction({
            title: t('commands.runTitle', 'Run {name}', { name: cmd.label || cmd.name }),
            message: t('commands.runOn', 'On {target}, with {params}.', { target, params: said }) + (extra ? `\n${extra}` : ''),
            confirmLabel: t('commands.confirm', 'Run'),
        });
    },

    // seconds said as a person would: "45 s", "10 min", "1 h 5 min"
    _fmtDuration(sec) {
        const n = Math.max(0, Math.round(Number(sec) || 0));
        if (n < 60) return `${n} s`;
        if (n < 3600) return `${Math.round(n / 60)} min`;
        const h = Math.floor(n / 3600), m = Math.round((n % 3600) / 60);
        return m ? `${h} h ${m} min` : `${h} h`;
    },

    // "Who drives it": one row per channel these commands control — the rule
    // in charge (its layer, mode, what it asks for), the others on it, and
    // the motor's state. Nothing when no channel is known.
    _channelsCardHtml(chans) {
        if (!(chans || []).length) return '';
        const t = (k, d, p) => this.t(k, d, p);
        const rows = chans.map(ch => {
            // in charge: the rule winning the channel now, else the one that
            // owns it (armed, asking nothing yet)
            const lead = ch.winner || ch.safety_holder || ch.owner;
            const win = (ch.rules || []).find(r => r.rule === lead);
            const others = (ch.rules || []).filter(r => r.rule !== lead);
            const st = ch.state || {};
            const motor = st.on == null ? '' : `${st.on ? t('commands.ch.on', 'on') : t('commands.ch.off', 'off')}`
                + ((st.on ? st.on_for_s : st.off_for_s) != null ? ` · ${t('commands.ch.since', 'for')} ${this._fmtDuration(st.on ? st.on_for_s : st.off_for_s)}` : '')
                + (st.starts_last_h ? ` · ${this.tn('commands.ch.starts', st.starts_last_h, '{n} start(s) in the last hour')}` : '');
            return `<tr data-channel="${this._esc(ch.channel)}">
                <td><b>${this._esc(ch.label || ch.channel)}</b><div class="field-hint">${(ch.commands || []).map(c => `<code>${this._esc(c)}</code>`).join(' ')}</div></td>
                <td>${win ? `<a href="#" ${this._act('openRule', [win.rule])}>${this._esc(win.rule)}</a>
                        <span class="dev-chip">${this._esc(win.layer)}</span> <span class="dev-chip">${this._esc(win.mode)}</span>
                        ${win.want != null ? `<div class="field-hint">${t('commands.ch.asks', 'asks for')} ${this._esc(typeof win.want === 'object' ? Object.entries(win.want).map(([k, v]) => `${k} = ${v}`).join(', ') : String(win.want))}</div>` : ''}`
                    : `<span class="field-hint">${t('commands.ch.noRule', 'No rule — commands from here, the API or MQTT')}</span>`}
                    ${ch.paused_until_s ? `<div class="field-hint">${t('commands.ch.paused', 'Paused by an override — {time} left.', { time: this._fmtDuration(ch.paused_until_s) })}</div>` : ''}</td>
                <td>${others.length ? `<details><summary class="field-hint">${this.tn('commands.ch.others', others.length, '{n} other rule(s)')}</summary>${others.map(r =>
                        `<div><a href="#" ${this._act('openRule', [r.rule])}>${this._esc(r.rule)}</a> <span class="field-hint">${this._esc(r.layer)} · ${this._esc(r.mode)}</span></div>`).join('')}</details>` : ''}</td>
                <td>${this._esc(motor)}</td></tr>`;
        }).join('');
        return `<div class="cmd-channels" style="margin-bottom:14px;">
            <div style="font-weight:600;margin-bottom:6px;"><i aria-hidden="true" class="bi bi-diagram-3"></i> ${t('commands.ch.title', 'Who drives it')}</div>
            <div style="overflow-x:auto;"><table class="data-table" style="width:100%;">
                <thead><tr><th scope="col">${t('commands.ch.channel', 'Channel')}</th><th scope="col">${t('commands.ch.inCharge', 'In charge')}</th>
                    <th scope="col">${t('commands.ch.alsoOn', 'Also on it')}</th><th scope="col">${t('commands.ch.state', 'State')}</th></tr></thead>
                <tbody>${rows}</tbody></table></div></div>`;
    },

    // what a command meets on its channel, said on its card before Run
    // On a channel a rule owns, what an outside command does to the rules:
    // those under the safety layer step back for the pause asked; a safety
    // rule (priority ≥ 90, as the server's pause_channel) is never paused.
    _chanPause(ch) {
        const safety = r => r.layer === 'safety' || (r.priority ?? 0) >= 90;
        const rules = (ch?.rules || []).filter(r => r.mode === 'armed' && r.enabled !== false);
        const owner = rules.find(r => r.rule === ch?.owner);
        return {
            protect: owner && safety(owner) ? owner.rule : null,
            pausable: rules.filter(r => !safety(r)).map(r => r.rule),
        };
    },

    _cmdChannelHtml(c, ch) {
        if (!ch) return '';
        const t = (k, d, p) => this.t(k, d, p);
        if (ch.safety_holder) {
            return `<div class="hint-banner" style="margin-bottom:8px;" data-cmd-safety><i aria-hidden="true" class="bi bi-shield-lock hint-banner-icon"></i>
                <span>${this._esc(t('commands.safetyHolds', 'Safety rule {rule} holds this now — it cannot be overridden from here.', { rule: ch.safety_holder }))}</span></div>`;
        }
        const paused = ch.paused_until_s ? `<div class="field-hint" style="margin-bottom:6px;">${t('commands.ch.paused', 'Paused by an override — {time} left.', { time: this._fmtDuration(ch.paused_until_s) })}</div>` : '';
        if (!ch.owner) return paused;
        const who = this._chanPause(ch);
        const protect = who.protect ? `<div class="hint-banner" style="margin-bottom:8px;" data-cmd-protect><i aria-hidden="true" class="bi bi-shield-check hint-banner-icon"></i>
                <span>${this._esc(t('commands.protectWatches', 'Protection — cannot be paused: safety rule {rule} watches this channel and takes it back as soon as it acts.', { rule: who.protect }))}</span></div>` : '';
        // nothing below the safety layer to pause: no pause to ask for
        if (!who.pausable.length) return `${paused}${protect}`;
        const max = ch.max_override_s || 3600;
        const id = `dcmd_${c.name}__override`;
        return `${paused}${protect}<div class="form-group" data-cmd-owner style="margin-bottom:8px;">
            <label class="form-label" for="${this._esc(id)}">${this._esc(t('commands.pauseAsk', 'Running it pauses {rules} for (seconds):', { rules: who.pausable.join(', ') }))}</label>
            <input type="text" inputmode="numeric" id="${this._esc(id)}" class="input input-sm cmd-override" value="${Math.min(600, max)}" style="width:120px;" data-live>
            <div class="field-hint">${t('commands.overrideRange', 'Seconds, from 1 to {max}', { max })}</div></div>`;
    },

    async deleteDevice(id) {
        const t = (k, d, p) => this.t(k, d, p);
        const dev = (this._devices || []).find(x => x.id === id) || {};
        const onPage = this._devDetail?.id === id && document.getElementById('deviceDetailView')?.style.display !== 'none';
        // held back while something uses it: asked again (the page may be
        // older than the gateway), then the section says what, not a dialog
        if (onPage && this._ubCount(this._devDetail.usedBy)
            && !await this._ubRecheck('device', id, document.getElementById('devUsedBy'), deps => this._applyDeviceUsedBy(id, deps))) return;
        // what goes with it (components, attribute entries) is said by the
        // server's own answer, inside the dialog
        const details = [];
        if (dev.bridge) details.push(t('devices.deleteBus', 'It leaves bus :{port} of {bridge}.',
            { port: dev.bridge_port ?? '—', bridge: dev.bridge }));
        const ask = {
            kind: 'device', id, details,
            host: onPage ? document.getElementById('devUsedBy') : null,
            title: t('devices.deleteTitle', 'Delete device {id}', { id }),
            message: t('devices.deleteConfirm',
                'Its polling stops and it leaves the configuration. It can be restored from "Deleted devices"; its measurement selection stays on disk.'),
        };
        if (!await this._ubDelete(ask)) {
            if (onPage && ask.refused) this._applyDeviceUsedBy(id, ask.refused);
            return;
        }
        this.showToast('success', t('devices.deleted', 'Device deleted'), id);
        if (this._regDevice === id) this._regDevice = this._primaryDeviceId();
        // its own page open: back to the list, not left on a device that is gone
        if (this._devDetail?.id === id && document.getElementById('deviceDetailView')?.style.display !== 'none') {
            this.closeDeviceDetail(true);
            return;
        }
        await this.renderDevicesList();
    }
});
