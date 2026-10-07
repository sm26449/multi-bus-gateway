/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
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
        set('#plCensus', this._endpointCensusText(p));
        set('#plHeadline', this._endpointHeadlineHtml(p));
        set('#plReadVia', this._endpointReadViaHtml(p));
        // never redraw while a unit is being renamed or a source saved; and a
        // "How it is read" the operator opened stays open across the tick
        if (!view.querySelector('[data-renaming]')
            && !view.querySelector('[data-src-busy]')) {
            const open = new Set([...view.querySelectorAll('details[data-group-details][open]')]
                .map(d => d.getAttribute('data-group-details')));
            set('#plGroups', this._endpointGroupsHtml(p));
            open.forEach(gid => {
                const d = view.querySelector(`details[data-group-details="${CSS.escape(gid)}"]`);
                if (d) d.open = true;
            });
        }
    },

    _endpointCensusText(p) {
        const al = p.units_alarming ? ` · ${p.units_alarming} ${this.t('endpoints.alarming', 'alarming')}` : '';
        return `${p.online_units}/${p.total_units} ${this.t('endpoints.answering', 'units answering')}${al}`;
    },

    // what the unit itself declares wrong (its template's alarm fields)
    _unitAlarmPill(al) {
        if (!al || !(al.danger || al.warning)) return '';
        const sev = al.danger ? 'danger' : 'warning';
        const n = (al.danger || 0) + (al.warning || 0);
        const what = (al.active || []).map(a => `${a.label}: ${a.value}`).join(' · ');
        const said = `${n} ${this.t('endpoints.alarming', 'alarming')}: ${what}`;
        return ` <span class="sink-pill ${sev === 'danger' ? 'bad' : 'warn'}" role="img" aria-label="${this._esc(said)}" title="${this._esc(what)}"><i aria-hidden="true" class="bi bi-exclamation-triangle"></i><span class="col-wide-text"> ${n}</span></span>`;
    },

    // The four numbers an operator looks for first. Only what the installation
    // actually measures: the site's balance when the datalogger gives one, the
    // inverters' sum otherwise — a missing figure is shown as missing, never
    // invented from something else.
    _endpointHeadlineHtml(p) {
        const h = p.headline || {};
        const t = (k, d) => this.t(k, d);
        const big = (label, v, unit, hint) => `<div style="min-width:120px;">
            <div style="color:var(--text-secondary);font-size:11.5px;">${label}</div>
            <div style="font-weight:700;font-size:22px;letter-spacing:-.3px;font-variant-numeric:tabular-nums;">${
                v == null ? '<span style="color:var(--text-tertiary,#8a94a0);font-weight:400;">—</span>' : this._endpointValue(v, unit)}</div>
            ${hint ? `<div style="color:var(--text-tertiary,#8a94a0);font-size:11px;">${this._esc(hint)}</div>` : ''}
        </div>`;
        // the template says what this installation's top line is (a battery
        // bank: power with its sign, SOC, energy, status, alarms)
        if ((p.headline_items || []).length) {
            return p.headline_items.map(i => big(this._esc(i.label), i.value, i.unit || '', i.hint)).join('');
        }
        const site = h.energy_today != null || h.autonomy != null || h.self_consumption != null;
        return big(t('endpoints.head.now', 'Producing now'), h.power_now, 'W')
            + (site ? big(t('endpoints.head.today', 'Today'), h.energy_today, 'Wh')
                    + big(t('endpoints.head.autonomy', 'Autonomy'), h.autonomy, '%')
                    + big(t('endpoints.head.selfUse', 'Self-consumption'), h.self_consumption, '%')
               : '');
    },

    // Every way the installation is read, each with what it costs right now.
    _endpointReadViaHtml(p) {
        const t = (k, d) => this.t(k, d);
        const rows = p.read_via || [];
        if (!rows.length) return '';
        const hc = { ok: 'var(--success,#22c55e)', degraded: 'var(--warning,#f59e0b)',
                     down: 'var(--danger,#ef4444)', idle: 'var(--text-secondary,#8a94a0)' };
        const proto = { http: 'HTTP', tcp: 'Modbus TCP', 'rtu-tcp': 'Modbus RTU/TCP',
                        rtu: 'Modbus RTU', mqtt: 'MQTT',
                        rtu_tap: 'Modbus RTU tap (listen-only)' };
        return `<div style="color:var(--text-secondary);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;margin-bottom:4px;">${t('endpoints.readVia', 'Read via')}</div>`
            + rows.map(r => {
                const where = r.protocol === 'http' ? '' : ` ${this._esc(r.address || '')}`;
                const bits = [
                    `${proto[r.protocol] || this._esc(r.protocol)}${where}`,
                    r.interval_s != null ? `${t('endpoints.every', 'every')} ${r.interval_s} s` : null,
                    r.latency_ms != null ? `~${Math.round(r.latency_ms)} ms` : null,
                    r.fail_pct_5m != null ? `${r.fail_pct_5m} % ${t('endpoints.failed5m', 'failed (5 min)')}` : null,
                    `${r.units_ok}/${r.units_total} ${t('endpoints.units', 'units')}`,
                ].filter(Boolean);
                return `<div style="display:flex;gap:8px;align-items:baseline;font-size:12.5px;padding:2px 0;flex-wrap:wrap;" data-read-via="${this._esc(r.id)}">
                    <span class="status-dot" style="--dot:${hc[r.status] || hc.idle}" aria-hidden="true"></span>
                    <b>${this._esc(r.id)}</b>
                    <span style="color:var(--text-secondary);">${this._esc(r.status || 'idle')}</span>
                    <span>${bits.join(' · ')}</span>
                </div>`;
            }).join('');
    },

    // Where the units publish — the REAL per-group paths, not the endpoint
    // default no group may be using.
    // a per-unit topic pattern, shown as the path an operator will see
    _topicShown(pattern, p) {
        return String(pattern || '').replace(/\$\{unit_id\}/g, 'N')
            .replace(/\$\{device_id\}/g, '<unit>').replace(/\$\{endpoint_id\}/g, p.id);
    },

    _endpointPublishHtml(p) {
        const t = (k, d) => this.t(k, d);
        const groups = p.groups || [];
        const rows = groups.length ? groups.map(g => ({
            label: this._groupLabel(g), topic: (g.outputs || {}).topic_prefix || (p.mqtt || {}).topic_prefix || '',
            bucket: (g.outputs || {}).bucket || (p.influxdb || {}).bucket || '',
        })) : [{ label: '', topic: (p.mqtt || {}).topic_prefix || '', bucket: (p.influxdb || {}).bucket || '' }];
        const mq = rows.map(r => `${r.label ? this._esc(r.label) + ' ' : ''}<code style="font-size:11px;">${this._esc(this._topicShown(r.topic, p))}/…</code>`).join(' · ');
        const buckets = [...new Set(rows.map(r => r.bucket).filter(Boolean))];
        return `<span style="color:var(--text-secondary);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;margin-right:8px;">${t('endpoints.publishesTo', 'Publishes to')}</span>
            <span style="font-size:12.5px;">${mq}${buckets.length ? ` → InfluxDB <code style="font-size:11px;">${this._esc(buckets.join(', '))}</code>` : ''}</span>`;
    },

    // What this access point costs, measured on its own wire.
    //
    // Every interval an operator can ask for is bounded by one number nobody
    // can look up: how long a transaction actually takes on THIS master, times
    // how many the sweep needs, divided by the sockets serving it. So it is
    // measured and shown next to the interval it constrains — an interval under
    // the floor is a promise the wire cannot keep, and the page says so instead
    // of leaving the operator to infer it from overruns.
    _endpointBusHtml(p) {
        const b = p.bus || {};
        const t = (k, d) => this.t(k, d);
        if (!b.samples) {
            return `<span style="color:var(--text-secondary);">${t('endpoints.bus.quiet',
                'No transactions measured yet on this access point.')}</span>`;
        }
        const lanes = b.lanes || 1, want = b.max_connections || 1;
        const bits = [
            `<span class="dev-chip" title="${t('endpoints.bus.lanesHint',
                'Sockets open to this master. More is not always faster: one that serializes internally gains nothing and loses client slots. Measure it with scripts/calibrate_endpoint.py.')}">${lanes} ${lanes === 1
                ? t('endpoints.bus.lane', 'connection') : t('endpoints.bus.lanes', 'connections')}</span>`,
            `<span class="dev-chip">${t('endpoints.bus.tx', 'per read')} ${b.tx_p50_s.toFixed(2)}s <span style="color:var(--text-secondary);">p95 ${b.tx_p95_s.toFixed(2)}s</span></span>`,
        ];
        if (b.reads_per_sweep) {
            bits.push(`<span class="dev-chip">${b.reads_per_sweep} ${t('endpoints.bus.reads', 'reads/sweep')}</span>`);
        }
        if (b.floor_s) {
            bits.push(`<span class="dev-chip" title="${t('endpoints.bus.floorHint',
                'The fastest honest cadence at this shape: p95 per read, times the reads a sweep needs, across the connections serving them.')}">${t('endpoints.bus.floor', 'floor')} ~${b.floor_s}s</span>`);
        }
        if (b.missed_turns) {
            bits.push(`<span class="sink-pill warn" title="${t('endpoints.bus.missedHint',
                'Reads that gave up waiting for their turn. The cycle was skipped, not failed — but the interval is asking for more than this wire gives.')}">${b.missed_turns} ${t('endpoints.bus.missed', 'missed turns')}</span>`);
        }
        if (want > lanes) {
            bits.push(`<span class="sink-pill warn">${t('endpoints.bus.pending',
                'configured for')} ${want}</span>`);
        }
        return bits.join(' ');
    },

    // ── Groups ──────────────────────────────────────────────────────────────
    //
    // An installation is not one kind of thing: a PV plant holds inverters AND
    // the meter at its grid connection. They differ in template, in rhythm, and
    // in what it means to add them up — so each group carries its own units,
    // its own sources and its own total.

    _ROLE_ICON: { inverter: 'bi-sun', meter: 'bi-speedometer2', site: 'bi-house',
                  battery: 'bi-battery-half', sensor: 'bi-thermometer-half' },
    // what a row shows at a glance, by what the unit IS (mirrors the server)
    _ROLE_LIVE: {
        inverter: ['power_active_total', 'voltage_ln_avg', 'voltage_dc', 'power_limit_pct'],
        meter: ['power_active_total', 'energy_active_import', 'energy_active_export'],
        site: ['power_pv', 'power_load', 'power_grid', 'autonomy', 'self_consumption', 'energy_today'],
        battery: ['power_active_total', 'voltage_dc'],
    },
    _liveLabel(name) {
        const d = { power_active_total: 'Power', voltage_ln_avg: 'AC voltage', voltage_dc: 'DC voltage',
                    energy_active_import: 'Imported', energy_active_export: 'Exported',
                    power_limit_pct: 'Limit', power_pv: 'PV', power_load: 'Load', power_grid: 'Grid', autonomy: 'Autonomy',
                    self_consumption: 'Self-consumption', energy_today: 'Today' }[name] || name;
        return this.t(`endpoints.live.${name}`, d);
    },
    _groupLabel(g) {
        if (g.unit_label_plural) return g.unit_label_plural;   // the template's own word
        const d = { inverter: 'Inverters', meter: 'Grid meter', site: 'Site totals',
                    battery: 'Battery', sensor: 'Sensors' }[g.role];
        return d ? this.t(`endpoints.role.${g.role}`, d) : g.id;
    },
    _groupIcon(g) {
        return g.icon ? `bi-${g.icon}` : (this._ROLE_ICON[g.role] || 'bi-cpu');
    },

    _endpointGroupsHtml(p) {
        const t = (k, d) => this.t(k, d);
        const groups = p.groups || [];
        if (!groups.length) {
            return `<div class="settings-card"><div class="settings-card-body">
                <span style="color:var(--text-secondary);">${t('endpoints.groupNone',
                    'No group yet. Add one to say what this installation holds.')}</span>
            </div></div>`;
        }
        return groups.map(g => {
            const off = g.enabled === false;
            const label = this._groupLabel(g);
            const nSrc = (g.sources || []).length;
            // the template's glance fields, else the role defaults
            const cols = (g.glance || []).length ? g.glance : (this._ROLE_LIVE[g.role] || ['power_active_total']);
            const colLabel = c => ((p.fields || {})[c] || {}).label || this._liveLabel(c);
            const hasTotal = g.aggregates && Object.keys(g.aggregates).some(k => !['units_online', 'units_total', 'status'].includes(k));
            const modbusHere = (g.sources || []).some(sx => sx.protocol !== 'http' && sx.protocol !== 'mqtt');
            return `
            <div class="settings-card" data-group="${this._esc(g.id)}" ${off ? 'style="opacity:.62;"' : ''}>
              <div class="settings-card-header">
                <h3><i aria-hidden="true" class="bi ${this._esc(this._groupIcon(g))}"></i> ${this._esc(label)}
                  ${label !== g.id ? `<span class="dev-chip">${this._esc(g.id)}</span>` : ''}
                  <span class="dev-chip">${g.online_units}/${g.total_units} ${t('endpoints.answeringShort', 'answering')}</span>
                  ${off ? `<span class="sink-pill warn">${t('devices.disabled', 'disabled')}</span>` : ''}
                </h3>
                <div class="header-actions">
                  <label class="switch-label" data-admin title="${t('endpoints.groupToggleHint', 'Stop reading this group. Its units stay visible and editable.')}">
                    <input data-admin type="checkbox" ${off ? '' : 'checked'}
                           ${this._act('toggleGroup', [p.id, g.id], {el: true, on: "change"})}>
                    <span>${t('endpoints.groupOn', 'Read')}</span></label>
                  ${(g.commands || []).some(c => c.enabled) ? `<button data-admin class="btn btn-secondary btn-sm" ${this._act('openCommandModal', [p.id, g.id, '', ''])}
                          title="${t('commands.groupHint', 'Tell the units of this group something — a power limit, a restore. Every command is verified and audited.')}"><i aria-hidden="true" class="bi bi-send"></i> ${t('commands.button', 'Commands…')}</button>` : ''}
                  <button class="btn btn-ghost btn-sm" data-admin ${this._act('openSourceModal', [p.id, '', g.id])}
                          title="${t('endpoints.srcAdd', 'Add source')}" aria-label="${t('endpoints.srcAdd', 'Add source')}"><i aria-hidden="true" class="bi bi-plus-lg"></i></button>
                  <button class="btn btn-ghost btn-sm" data-admin ${this._act('openGroupModal', [p.id, g.id])}
                          title="${t('common.edit', 'Edit')}" aria-label="${t('common.edit', 'Edit')}"><i aria-hidden="true" class="bi bi-pencil"></i></button>
                  <button class="btn btn-ghost btn-sm" data-admin ${groups.length < 2 ? 'disabled' : ''}
                          ${this._act('deleteGroup', [p.id, g.id])}
                          title="${groups.length < 2 ? t('endpoints.groupLast', 'An installation needs at least one group') : t('common.delete', 'Delete')}"
                          aria-label="${t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-trash"></i></button>
                </div>
              </div>
              <div class="settings-card-body">
                <div style="overflow-x:auto;"><table class="data-table" style="width:100%;">
                  <thead><tr>
                    <th>${t('endpoints.unitId', 'Unit')}</th>
                    <th class="col-wide">${t('devices.wizard.name', 'Name')}</th>
                    ${cols.map((c, i) => `<th style="text-align:right;" ${i > 1 ? 'class="col-wide"' : ''}>${this._esc(colLabel(c))}</th>`).join('')}
                    <th class="col-health">${t('endpoints.health', 'Health')}</th>
                    <th class="col-wide">${t('devices.overview.lastRead', 'Last read')}</th>
                    <th class="col-wide">${t('endpoints.readVia', 'Read via')}</th>
                    <th></th>
                  </tr></thead>
                  <tbody data-group-units="${this._esc(g.id)}">${this._endpointUnitRowsHtml({ ...p, units: g.units }, cols)}</tbody>
                </table></div>
                ${hasTotal ? `<div data-group-totals="${this._esc(g.id)}" style="margin-top:14px;">
                    <div style="color:var(--text-secondary);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px;">
                      ${t('endpoints.groupTotal', 'Group total')} <span style="text-transform:none;letter-spacing:0;">→ <code style="font-size:11px;">${this._esc(g.topic || '')}/…</code></span></div>
                    ${this._endpointAggGridHtml({ aggregates: g.aggregates, aggregate_fields: g.aggregate_fields,
                                                  aggregate_order: g.aggregate_order })}
                  </div>` : (g.total_units > 1 ? `<div data-group-totals="${this._esc(g.id)}" style="margin-top:14px;">
                    <div style="color:var(--text-secondary);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px;">
                      ${t('endpoints.groupTotal', 'Group total')} <span style="text-transform:none;letter-spacing:0;">→ <code style="font-size:11px;">${this._esc(g.topic || '')}/…</code></span></div>
                    ${this._endpointAggGridHtml({ aggregates: {}, aggregate_fields: {} })}
                  </div>` : '')}
                <details data-group-details="${this._esc(g.id)}" style="margin-top:14px;">
                  <summary style="cursor:pointer;font-size:12.5px;color:var(--text-secondary);">
                    ${t('endpoints.howRead', 'How it is read')} · ${nSrc} ${nSrc === 1 ? t('endpoints.srcOne', 'source') : t('endpoints.srcMany', 'sources')}</summary>
                  <div style="margin-top:10px;" data-group-sources="${this._esc(g.id)}">${this._endpointSourcesHtml({ ...p, sources: g.sources }, g.id)}</div>
                  ${modbusHere && (p.bus || {}).samples ? `<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px;font-size:12px;" id="plBus">${this._endpointBusHtml(p)}</div>` : ''}
                </details>
              </div>
            </div>`;
        }).join('');
    },

    async toggleGroup(endpointId, groupId, el) {
        const p = this._endpointDetail;
        const groups = (p.groups || []).map(g => this._rawGroup(p, g.id,
            g.id === groupId ? { enabled: el.checked } : {}));
        if (!await this._saveGroups(endpointId, groups)) el.checked = !el.checked;
    },

    async deleteGroup(endpointId, groupId) {
        const p = this._endpointDetail;
        if ((p.groups || []).length < 2) return;
        if (!confirm(this.t('endpoints.groupDeleteAsk', 'Remove group') + ` "${groupId}"?\n\n`
            + this.t('endpoints.groupDeleteNote',
                'Its units stop being managed by this endpoint. Their register files stay on disk.'))) return;
        const rest = (p.groups || []).filter(g => g.id !== groupId)
            .map(g => this._rawGroup(p, g.id));
        await this._saveGroups(endpointId, rest);
    },

    // The card renders config + live merged; a save must send back only the
    // declared half, or live counters would be written into the config.
    _rawGroup(p, id, over) {
        const g = (p.groups || []).find(x => x.id === id) || {};
        const out = {
            id: g.id, role: g.role || '',
            enabled: g.enabled !== false,
            units: (g.units || []).map(u => ({ unit_id: u.unit_id, id: u.device_id,
                                               ...(u.name ? { name: u.name } : {}) })),
            ...over,
        };
        if (g.template) out.template = g.template;
        const cmds = (g.commands || []).filter(c => c.binding).map(c => c.binding);
        if (cmds.length) out.commands = cmds;
        const srcs = (g.sources || []).map(s => this._rawSource({ sources: g.sources }, s.id));
        if (srcs.length === 1 && srcs[0].id === 'default') {
            const s = srcs[0];
            out.connection = { protocol: s.protocol, ...(s.host ? { host: s.host, port: s.port } : {}),
                               ...(s.url ? { url: s.url } : {}),
                               ...(s.serial_port ? { serial_port: s.serial_port } : {}) };
        } else if (srcs.length) {
            out.sources = srcs;
        }
        return out;
    },

    async _saveGroups(endpointId, groups) {
        const host = document.querySelector('#plGroups');
        if (host) host.dataset.srcBusy = '1';
        try {
            const p = this._endpointDetail;
            const rsp = await fetch(`/api/endpoints/${encodeURIComponent(endpointId)}`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    id: endpointId, name: p.name, enabled: p.enabled,
                    connection: p.connection, template: p.template,
                    units: (groups[0] || {}).units || [], groups,
                }),
            });
            if (!rsp.ok) {
                const d = await rsp.json().catch(() => ({}));
                this.showToast((d.detail?.errors || [d.detail || rsp.statusText]).join('\n'), 'error');
                return false;
            }
            this.showToast(this.t('endpoints.groupSaved', 'Groups updated'), 'success');
            return true;
        } finally {
            if (host) delete host.dataset.srcBusy;
            await this._refreshEndpointDetail(endpointId);
        }
    },

    // ── Sources ─────────────────────────────────────────────────────────────
    //
    // The ordered ways of reaching one endpoint's units. Configuration and live
    // state together on purpose: "which source is this value from" and "is that
    // source still alive" are the same question for an operator.

    _endpointSourcesHtml(p, groupId) {
        const t = (k, d) => this.t(k, d);
        const srcs = p.sources || [];
        const gq = groupId ? `, '${this._esc(groupId)}'` : '';
        if (!srcs.length) {
            return `<span style="color:var(--text-secondary);">${t('endpoints.srcNone',
                'No source declared yet.')}</span>
                <button data-admin class="btn btn-ghost btn-sm" ${this._act('openSourceModal', [p.id, '', groupId || ''])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('endpoints.srcAdd', 'Add source')}</button>`;
        }
        const rows = srcs.map((s, i) => {
            const ok = s.units_total ? s.units_ok === s.units_total : null;
            const dot = s.enabled === false ? 'var(--text-secondary,#8a94a0)'
                : ok === null ? 'var(--text-secondary,#8a94a0)'
                : ok ? 'var(--success,#22c55e)'
                : s.units_ok ? 'var(--warning,#f59e0b)' : 'var(--danger,#ef4444)';
            const groups = Object.entries(s.poll_groups || {})
                .map(([k, v]) => `${this._esc(k)} ${v}s`).join(' · ') || '—';
            const fails = s.fail_pct_5m
                ? ` <span style="color:var(--danger,#ef4444);">${s.fail_pct_5m} % ${t('endpoints.failed5m', 'failed (5 min)')}</span>` : '';
            return `
            <tr data-src-row="${this._esc(s.id)}" style="border-top:1px solid var(--border,#2a3038);">
              <td style="padding:8px 10px 8px 0;white-space:nowrap;">
                <span class="status-dot" style="--dot:${dot};background:var(--dot);width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:7px;"></span>
                <b>${this._esc(s.id)}</b>
                <span class="dev-chip" style="margin-left:6px;" title="${t('endpoints.srcOrderHint', 'Order of precedence: the first source offering a field supplies it.')}">#${s.rank + 1}</span>
                ${s.enabled === false ? `<span class="sink-pill warn" style="margin-left:6px;">${t('devices.disabled', 'disabled')}</span>` : ''}
              </td>
              <td style="padding:8px 10px 8px 0;">${this._esc(s.protocol)}</td>
              <td style="padding:8px 10px 8px 0;font-size:12px;word-break:break-all;max-width:280px;">${this._esc(s.address || '')}</td>
              <td style="padding:8px 10px 8px 0;font-size:12px;">${this._esc(s.template || '—')}</td>
              <td style="padding:8px 10px 8px 0;font-size:12px;white-space:nowrap;">${groups}</td>
              <td style="padding:8px 10px 8px 0;font-size:12px;white-space:nowrap;"
                  title="${t('endpoints.srcStaleHint', 'How long this source stays authoritative before a later one may fill the field. Never, for a counter.')}">${s.stale_after_s ? s.stale_after_s + ' s' : t('endpoints.never', 'never')}</td>
              <td style="padding:8px 10px 8px 0;font-variant-numeric:tabular-nums;white-space:nowrap;">
                ${s.units_ok}/${s.units_total} ${t('endpoints.answeringShort', 'answering')}${fails}
                ${s.latency_ms != null ? ` · ${s.latency_ms} ms` : ''}
                ${s.wire ? `<div style="font-size:11.5px;color:${(s.wire.crc_errors || s.wire.orphan_frames || s.wire.open_error) ? 'var(--warning-text)' : 'var(--text-secondary)'};"
                    title="${t('endpoints.wireHint', 'The bus as the tap hears it: valid frames, CRC errors, frames with no partner, bytes skipped to realign. On a healthy bus CRC errors and orphans stay at 0.')}">
                    ${s.wire.open_error ? this._esc(s.wire.open_error) : `${t('endpoints.wireFrames', 'frames')} ${s.wire.frames ?? '—'} · CRC ${s.wire.crc_errors ?? 0} · ${t('endpoints.wireOrphans', 'orphans')} ${s.wire.orphan_frames ?? 0} · resync ${s.wire.resync_dropped_bytes ?? 0} B`}</div>` : ''}
              </td>
              <td style="padding:8px 10px 8px 0;font-variant-numeric:tabular-nums;white-space:nowrap;"
                  title="${t('endpoints.srcOwnsHint', 'How many fields this source supplies right now. Zero means an earlier source already supplies everything it offers.')}">
                ${s.fields_owned} ${t('endpoints.srcFields', 'fields')}</td>
              <td style="padding:8px 0;white-space:nowrap;text-align:right;">
                <button data-admin class="btn btn-ghost btn-sm" ${i === 0 ? 'disabled' : ''}
                        ${this._act('moveSource', [p.id, s.id, -1, groupId || ''])}
                        title="${t('endpoints.srcUp', 'Raise precedence')}"><i aria-hidden="true" class="bi bi-arrow-up"></i></button>
                <button data-admin class="btn btn-ghost btn-sm" ${i === srcs.length - 1 ? 'disabled' : ''}
                        ${this._act('moveSource', [p.id, s.id, 1, groupId || ''])}
                        title="${t('endpoints.srcDown', 'Lower precedence')}"><i aria-hidden="true" class="bi bi-arrow-down"></i></button>
                <button data-admin class="btn btn-ghost btn-sm" ${this._act('openSourceModal', [p.id, s.id, groupId || ''])}><i aria-hidden="true" class="bi bi-pencil"></i></button>
                <button data-admin class="btn btn-ghost btn-sm" ${srcs.length < 2 ? 'disabled' : ''}
                        ${this._act('deleteSource', [p.id, s.id, groupId || ''])}
                        title="${srcs.length < 2 ? t('endpoints.srcLast', 'A unit needs at least one source') : t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-trash"></i></button>
              </td>
            </tr>`;
        }).join('');
        return `<div style="overflow-x:auto;"><table style="width:100%;font-size:13px;">
            <tr style="color:var(--text-secondary);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;">
              <td style="padding-right:10px;">${t('endpoints.srcName', 'source')}</td>
              <td style="padding-right:10px;">${t('devices.wizard.protocol', 'protocol')}</td>
              <td style="padding-right:10px;">${t('endpoints.srcAddress', 'address')}</td>
              <td style="padding-right:10px;">${t('devices.wizard.template', 'template')}</td>
              <td style="padding-right:10px;">${t('endpoints.srcGroups', 'intervals')}</td>
              <td style="padding-right:10px;">${t('endpoints.srcStale', 'stale after')}</td>
              <td style="padding-right:10px;">${t('endpoints.srcLive', 'live')}</td>
              <td style="padding-right:10px;">${t('endpoints.srcOwns', 'provides')}</td>
              <td style="text-align:right;"><button data-admin class="btn btn-ghost btn-sm"
                  ${this._act('openSourceModal', [p.id, '', groupId || ''])}
                  title="${t('endpoints.srcAdd', 'Add source')}"><i aria-hidden="true" class="bi bi-plus-lg"></i></button></td></tr>
            ${rows}</table></div>`;
    },

    // Reordering IS the precedence control, so it writes straight through.
    _group(p, groupId) {
        return (p.groups || []).find(g => g.id === groupId) || { sources: p.sources || [] };
    },

    async moveSource(endpointId, sourceId, delta, groupId) {
        const p = this._endpointDetail;
        if (!p || p.id !== endpointId) return;
        const g = this._group(p, groupId);
        const ids = (g.sources || []).map(s => s.id);
        const i = ids.indexOf(sourceId), j = i + delta;
        if (i < 0 || j < 0 || j >= ids.length) return;
        ids.splice(j, 0, ids.splice(i, 1)[0]);
        await this._saveGroupSources(endpointId, groupId,
            ids.map(id => this._rawSource(g, id)));
    },

    async deleteSource(endpointId, sourceId, groupId) {
        const p = this._endpointDetail;
        const g = this._group(p, groupId);
        if ((g.sources || []).length < 2) return;
        if (!confirm(this.t('endpoints.srcDeleteAsk', 'Remove source') + ` "${sourceId}"?\n\n`
                + this.t('endpoints.srcDeleteNote',
                    'Its registers stay on disk. The fields it owned fall to the next source that offers them.'))) return;
        await this._saveGroupSources(endpointId, groupId,
            (g.sources || []).filter(s => s.id !== sourceId)
                .map(s => this._rawSource(g, s.id)));
    },

    // Writing a group's source list back is a groups save with that one group
    // rebuilt — the endpoint is saved whole, so a partial write cannot leave
    // two groups disagreeing about which units they own.
    async _saveGroupSources(endpointId, groupId, sources) {
        const p = this._endpointDetail;
        const groups = (p.groups || []).map(g => {
            const raw = this._rawGroup(p, g.id);
            if (g.id !== groupId) return raw;
            delete raw.connection;
            return { ...raw, sources };
        });
        return this._saveGroups(endpointId, groups);
    },

    // The card renders a MERGED view (config + live); a save must send back only
    // the declared half, or the live counters would be written into config.
    _rawSource(p, id) {
        const s = (p.sources || []).find(x => x.id === id) || {};
        const out = { id: s.id, protocol: s.protocol, enabled: s.enabled !== false };
        if (s.template) out.template = s.template;
        if (s.timeout != null) out.timeout = s.timeout;
        if (s.stale_after_s) out.stale_after_s = s.stale_after_s;
        if (Object.keys(s.poll_groups || {}).length) {
            out.poll_groups = {};
            for (const [k, v] of Object.entries(s.poll_groups)) out.poll_groups[k] = { interval: v };
        }
        const a = s.address || '';
        if (s.protocol === 'http') out.url = a;
        else if (s.protocol === 'rtu') out.serial_port = a;
        else if (s.protocol === 'mqtt') out.topic = a;
        else { const m = a.match(/^(.*):(\d+)$/); out.host = m ? m[1] : a; out.port = m ? +m[2] : 502; }
        return out;
    },



    // Add or edit ONE source. Everything an operator sets about a way of
    // reaching the units lives here — protocol, address, template, how often,
    // and how long it stays authoritative before a lower source may fill in.
    async openSourceModal(endpointId, sourceId, groupId) {
        const p = this._endpointDetail;
        if (!p || p.id !== endpointId) return;
        const t = (k, d) => this.t(k, d);
        this._srcGroupId = groupId || ((p.groups || [])[0] || {}).id || '';
        const g = this._group(p, this._srcGroupId);
        const s = (g.sources || []).find(x => x.id === sourceId) || null;
        let templates = [];
        try { templates = (await (await fetch('/api/device-templates')).json()).templates || []; }
        catch (e) { console.error(e); }
        this._srcEditId = s ? s.id : '';
        const proto = s?.protocol || 'tcp';
        const addr = s?.address || '';
        const hostPort = addr.match(/^(.*):(\d+)$/);
        const pg = s?.poll_groups || {};
        this._srcTemplates = templates;
        this._srcRole = g.role || '';
        const tplOpts = this._srcTplOptions(proto, s?.template || '');
        const PROTO = { tcp: 'Modbus TCP', 'rtu-tcp': 'Modbus RTU over TCP', rtu: 'Modbus RTU', http: 'Solar API / HTTP JSON', mqtt: 'MQTT' };
        const iv = (id, label, hint, val) => `<div class="form-group"><label class="form-label" for="${id}">${label}</label>
                    <div style="display:flex;align-items:center;gap:6px;"><input id="${id}" class="input" type="number" min="1" step="1" value="${val ?? ''}" placeholder="—" style="width:90px;"> s</div>
                    <div class="field-hint">${hint}</div></div>`;
        document.getElementById('endpointModalTitle').textContent = s
            ? t('endpoints.srcEditTitle', 'Edit source') : t('endpoints.srcAddTitle', 'Add source');
        document.getElementById('endpointModalBody').innerHTML = `
            <p class="field-hint" style="margin:0 0 12px;">${t('endpoints.srcIntro',
                'A source is one way of reaching the SAME units. Its position in the list is its precedence: the first source offering a field owns it.')}</p>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="srcId">${t('endpoints.srcName', 'Source ID')}</label>
                    <input id="srcId" class="input" value="${this._esc(s?.id || '')}" ${s ? 'disabled' : ''} placeholder="solar_api"></div>
                <div class="form-group"><label class="form-label" for="srcProto">${t('endpoints.srcWay', 'Read over')}</label>
                    <select id="srcProto" class="input" data-action="_srcProtoChanged" data-on="change">
                        ${['tcp', 'rtu-tcp', 'rtu', 'http', 'mqtt'].map(x =>
                            `<option value="${x}" ${proto === x ? 'selected' : ''}>${PROTO[x]}</option>`).join('')}
                    </select></div>
                <div class="form-group flex-2"><label class="form-label" for="srcTpl">${t('devices.wizard.template', 'Template')}</label>
                    <select id="srcTpl" class="input">${tplOpts}</select>
                    <div class="field-hint">${t('endpoints.srcTplHint', 'Only maps for this kind of unit and this way of reading.')}</div></div>
            </div>
            <div class="form-row" id="srcAddrTcp" ${proto === 'http' || proto === 'mqtt' || proto === 'rtu' ? 'hidden' : ''}>
                <div class="form-group flex-2"><label class="form-label" for="srcHost">${t('endpoints.host', 'Host')}</label>
                    <input id="srcHost" class="input" value="${this._esc(hostPort ? hostPort[1] : (proto === 'tcp' || proto === 'rtu-tcp' ? addr : ''))}" placeholder="192.168.1.50"></div>
                <div class="form-group"><label class="form-label" for="srcPort">${t('endpoints.port', 'Port')}</label>
                    <input id="srcPort" class="input" type="number" value="${hostPort ? hostPort[2] : 502}"></div>
            </div>
            <div class="form-row" id="srcAddrUrl" ${proto === 'http' ? '' : 'hidden'}>
                <div class="form-group flex-2"><label class="form-label" for="srcUrl">URL</label>
                    <input id="srcUrl" class="input" value="${this._esc(proto === 'http' ? addr : '')}" placeholder="http://host/solar_api/v1/...DeviceId=\${unit_id}">
                    <div class="field-hint">${t('endpoints.srcUrlHint', 'Use ${unit_id} — an HTTP master addresses its units by URL, not by a unit id inside a frame.')}</div></div>
            </div>
            <div class="form-row" id="srcAddrSerial" ${proto === 'rtu' ? '' : 'hidden'}>
                <div class="form-group flex-2"><label class="form-label" for="srcSerial">${t('endpoints.srcSerial', 'Serial port')}</label>
                    <input id="srcSerial" class="input" value="${this._esc(proto === 'rtu' ? addr : '')}" placeholder="/dev/ttyUSB0"></div>
            </div>
            <div class="form-row" id="srcAddrTopic" ${proto === 'mqtt' ? '' : 'hidden'}>
                <div class="form-group flex-2"><label class="form-label" for="srcTopic">${t('endpoints.srcTopic', 'Topic')}</label>
                    <input id="srcTopic" class="input" value="${this._esc(proto === 'mqtt' ? addr : '')}" placeholder="sensors/\${unit_id}/state"></div>
            </div>
            <div class="wiz-eyebrow">${t('endpoints.srcHowOften', 'How often this source reads')}</div>
            <div class="form-row">
                ${iv('srcIvRealtime', t('endpoints.iv.realtime', 'Power, voltages, currents'), t('endpoints.iv.realtimeHint', 'the "realtime" fields'), pg.realtime)}
                ${iv('srcIvNormal', t('endpoints.iv.normal', 'Energy counters'), t('endpoints.iv.normalHint', 'the "normal" fields'), pg.normal)}
                ${iv('srcIvSlow', t('endpoints.iv.slow', 'Static data'), t('endpoints.iv.slowHint', 'the "slow" fields'), pg.slow)}
            </div>
            <div class="field-hint" style="margin:-6px 0 10px;">${t('endpoints.srcIvHint', 'Leave a field empty and this source will not read that kind of measurement — another source may.')}</div>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="srcStale">${t('endpoints.srcStaleLabel', 'Stale after (s)')}</label>
                    <input id="srcStale" class="input" type="number" min="0" step="1" value="${s?.stale_after_s || 0}">
                    <div class="field-hint">${t('endpoints.srcStaleHint2', 'How long its values stay authoritative before a later source may fill in. 0 = never — right for a counter.')}</div></div>
                <div class="form-group"><label class="form-label" for="srcTimeout">${t('endpoints.srcTimeout', 'Timeout (s)')}</label>
                    <input id="srcTimeout" class="input" type="number" min="1" value="${s?.timeout ?? 3}"></div>
                <div class="form-group flex-2" style="align-self:flex-end;"><label class="form-label" style="display:flex;align-items:center;gap:8px;">
                    <input type="checkbox" id="srcEnabled" ${s?.enabled !== false ? 'checked' : ''}>
                    ${t('endpoints.srcEnabled', 'Read through this source')}</label></div>
            </div>`;
        document.getElementById('endpointFeedback').textContent = '';
        const save = document.querySelector('#endpointModal [data-endpoint-save]')
            || document.querySelector('#endpointModal .btn-primary');
        if (save) { save.innerHTML = `<i aria-hidden="true" class="bi bi-check-lg"></i> ${this.t('common.save', 'Save')}`; this._setAction(save, 'saveSource', [endpointId]); }
        this.openModal('endpointModal');
    },

    _srcProtoChanged() {
        const v = document.getElementById('srcProto').value;
        const show = (id, on) => { const e = document.getElementById(id); if (e) e.hidden = !on; };
        show('srcAddrTcp', v === 'tcp' || v === 'rtu-tcp');
        show('srcAddrUrl', v === 'http');
        show('srcAddrSerial', v === 'rtu');
        show('srcAddrTopic', v === 'mqtt');
        // the map must match the way of reading: a JSON-path map cannot be read
        // over Modbus, nor a register map over HTTP
        const tpl = document.getElementById('srcTpl');
        if (tpl) tpl.innerHTML = this._srcTplOptions(v, tpl.value);
    },

    // templates for THIS kind of unit read THIS way; every map of the transport
    // when no template is known for the kind
    _tplTransportOf(proto) { return proto === 'http' ? 'http' : proto === 'mqtt' ? '' : 'modbus'; },
    _srcTplOptions(proto, selected, role) {
        const transport = this._tplTransportOf(proto);
        const all = (this._srcTemplates || []).filter(x => !transport || x.transport === transport);
        const fit = this._pwTemplatesFor ? this._pwTemplatesFor(role ?? this._srcRole, transport, this._srcTemplates || []).filter(x => all.includes(x)) : all;
        const list = fit.length ? fit : all;
        if (selected && !list.some(x => x.id === selected)) list.push(...all.filter(x => x.id === selected));
        return ['<option value="">—</option>'].concat(list.map(x =>
            `<option value="${this._esc(x.id)}" ${selected === x.id ? 'selected' : ''}>${this._esc(x.name || x.id)}</option>`)).join('');
    },

    async saveSource(endpointId) {
        const p = this._endpointDetail;
        const fb = document.getElementById('endpointFeedback');
        const v = id => (document.getElementById(id) || {}).value;
        const id = (this._srcEditId || v('srcId') || '').trim().toLowerCase();
        if (!id) { fb.textContent = this.t('endpoints.srcNeedId', 'Source ID is required.'); return; }
        const proto = v('srcProto');
        const out = { id, protocol: proto, enabled: document.getElementById('srcEnabled').checked };
        if (v('srcTpl')) out.template = v('srcTpl');
        const st = parseFloat(v('srcStale')); if (st > 0) out.stale_after_s = st;
        const to = parseInt(v('srcTimeout'), 10); if (to > 0) out.timeout = to;
        if (proto === 'http') out.url = (v('srcUrl') || '').trim();
        else if (proto === 'rtu') out.serial_port = (v('srcSerial') || '').trim();
        else if (proto === 'mqtt') out.topic = (v('srcTopic') || '').trim();
        else { out.host = (v('srcHost') || '').trim(); out.port = parseInt(v('srcPort'), 10) || 502; }
        const groups = {};
        for (const [name, id] of [['realtime', 'srcIvRealtime'], ['normal', 'srcIvNormal'], ['slow', 'srcIvSlow']]) {
            const n = parseFloat(v(id));
            if (n > 0) groups[name] = { interval: n };
        }
        if (Object.keys(groups).length) out.poll_groups = groups;

        const g = this._group(p, this._srcGroupId);
        const cur = g.sources || [];
        const list = this._srcEditId
            ? cur.map(x => x.id === id ? out : this._rawSource(g, x.id))
            : cur.filter(x => x.id !== id).map(x => this._rawSource(g, x.id)).concat([out]);
        if (await this._saveGroupSources(endpointId, this._srcGroupId, list)) {
            this.closeModal('endpointModal');
        }
    },

    // ── commands ────────────────────────────────────────────────────────────
    //
    // A command is WHAT a controller wants ("power limit 60 %"); the device's
    // template says HOW that is said to this device. The gateway applies it
    // safely, verifies and consigns it; it never decides WHEN — that is a
    // controller's policy. This dialog is the operator's hand on the same
    // command the controller uses over MQTT or the API.

    // the form for a command's parameters, from their declaration: bounds,
    // default, unit, allowed values. ids are `${prefix}_${param}`.
    _cmdFormHtml(cmd, prefix) {
        const t = (k, d) => this.t(k, d);
        const params = Object.entries(cmd.params || {});
        if (!params.length) return `<p class="field-hint" style="margin:0 0 8px;">${t('commands.noParams', 'This command takes no parameters.')}</p>`;
        return `<div class="form-row">${params.map(([name, p]) => {
            const id = `${prefix}_${name}`;
            const label = `${this._esc(p.label || name)}${p.unit ? ` (${this._esc(p.unit)})` : ''}`;
            const e = (v) => this._esc(v);
            const bounds = p.min != null && p.max != null ? `${e(p.min)}..${e(p.max)}` : p.min != null ? `≥ ${e(p.min)}` : p.max != null ? `≤ ${e(p.max)}` : '';
            // no default → the field starts empty: a limit of "0" nobody typed must never be one click away
            const dflt = p.default != null ? p.default : '';
            const field = p.allowed
                ? `<select id="${e(id)}" class="input">${p.allowed.map(v => `<option value="${e(v)}" ${v == dflt ? 'selected' : ''}>${e(v)}</option>`).join('')}</select>`
                : `<input id="${e(id)}" class="input" type="number" ${p.min != null ? `min="${e(p.min)}"` : ''} ${p.max != null ? `max="${e(p.max)}"` : ''} step="any" value="${e(dflt)}" placeholder="${bounds}" ${p.required ? 'required aria-required="true"' : ''}>`;
            return `<div class="form-group"><label class="form-label" for="${e(id)}">${label}${p.required ? ' *' : ''}</label>${field}
                ${bounds ? `<div class="field-hint">${bounds}${p.default != null ? ` · ${t('commands.default', 'default')} ${e(p.default)}` : ''}</div>` : ''}</div>`;
        }).join('')}</div>`;
    },

    // the parameters as typed; null + a message when one is out of its bounds
    _cmdReadParams(cmd, prefix) {
        const t = (k, d) => this.t(k, d);
        const out = {};
        for (const [name, p] of Object.entries(cmd.params || {})) {
            const el = document.getElementById(`${prefix}_${name}`);
            if (!el) continue;
            const raw = String(el.value).trim();
            if (raw === '') { if (p.required) return { error: `${p.label || name}: ${t('commands.required', 'required')}` }; continue; }
            const v = Number(raw);
            if (!Number.isFinite(v)) return { error: `${p.label || name}: ${t('commands.notNumber', 'must be a number')}` };
            if (p.min != null && v < p.min) return { error: `${p.label || name}: ${t('commands.below', 'below the minimum')} ${p.min}` };
            if (p.max != null && v > p.max) return { error: `${p.label || name}: ${t('commands.above', 'above the maximum')} ${p.max}` };
            out[name] = v;
        }
        return { params: out };
    },

    // a guard clause in words: `controls_model_id = 123`, `wmaxlimpct_sf in [-2, -1, 0]`
    _cmdGuardText(g) {
        const reg = this._esc(g.read || g.register || '');
        if (g.in) return `${reg} ${this.t('commands.in', 'in')} [${this._esc(g.in.join(', '))}]`;
        if (g.expect != null) return `${reg} = ${this._esc(String(g.expect))}${g.tolerance ? ` ±${this._esc(g.tolerance)}` : ''}`;
        return reg;
    },

    _cmdVerdict(status) {
        const t = (k, d) => this.t(k, d);
        return { success: t('commands.v.success', 'applied and verified'),
                 mismatch: t('commands.v.mismatch', 'written, but the device holds another value'),
                 unverified: t('commands.v.unverified', 'written, read-back silent'),
                 rejected: t('commands.v.rejected', 'refused'),
                 error: t('commands.v.error', 'failed'),
                 dry_run: t('commands.v.dryRun', 'would write') }[status] || status;
    },

    _cmdDot(status) {
        return status === 'success' || status === 'dry_run' ? 'var(--success,#22c55e)'
             : status === 'error' || status === 'rejected' ? 'var(--danger,#ef4444)' : 'var(--warning,#f59e0b)';
    },

    // one result in words: verdict, before → after of the verified fields, ms, reason
    _cmdResultHtml(r, withDevice = true) {
        const t = (k, d) => this.t(k, d);
        const after = r.after || {}, before = r.before || {};
        const delta = Object.keys(after).filter(k => k in before).slice(0, 2)
            .map(k => `${this._esc(k)} ${before[k]} → ${after[k]}`).join(', ');
        const frames = r.status === 'dry_run' && r.frames ? r.frames.map(f =>
            `${t('commands.frame', 'registers')} ${f.address}…${f.address + f.words.length - 1} = [${f.words.join(', ')}]`).join('; ') : '';
        return `<div class="cmd-result" style="display:flex;gap:8px;align-items:baseline;padding:2px 0;">
            <span class="status-dot" style="--dot:${this._cmdDot(r.status)}" aria-hidden="true"></span>
            ${withDevice && r.device ? `<span class="dev-chip">${this._esc(r.device)}</span>` : ''}
            <span><b>${this._cmdVerdict(r.status)}</b>${delta ? ` · ${delta}` : ''}${frames ? ` · ${frames}` : ''}${r.ms != null ? ` · ${Math.round(r.ms)} ms` : ''}${r.reason ? ` · ${this._esc(r.reason)}` : ''}</span></div>`;
    },

    async openCommandModal(endpointId, groupId, deviceId, name) {
        const p = this._endpointDetail;
        if (!p || p.id !== endpointId) return;
        const t = (k, d) => this.t(k, d);
        const g = (p.groups || []).find(x => x.id === groupId) || {};
        const units = (g.units || []);
        const cmds = (g.commands || []).filter(c => c.enabled);
        if (!cmds.length) return;
        const cmd = cmds.find(c => c.name === name) || cmds[0];
        this._cmdCtx = { endpointId, groupId, cmds };
        document.getElementById('endpointModalTitle').textContent = t('commands.title', 'Command');
        document.getElementById('endpointModalBody').innerHTML = `
            <p class="field-hint" style="margin:0 0 12px;">${t('commands.intro',
                'The gateway writes the registers the template declares for this command, reads back and says whether it took. Test shows the exact registers without writing.')}</p>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="cmdName">${t('commands.which', 'Command')}</label>
                    <select id="cmdName" class="input" data-action="_cmdPick" data-on="change">${cmds.map(c =>
                        `<option value="${this._esc(c.name)}" ${c.name === cmd.name ? 'selected' : ''}>${this._esc(c.label || c.name)}</option>`).join('')}</select></div>
                <div class="form-group flex-2"><label class="form-label" for="cmdScope">${t('commands.scope', 'Apply to')}</label>
                    <select id="cmdScope" class="input">
                        <option value="">${t('commands.everyUnit', 'every unit of the group')} (${units.length})</option>
                        ${units.map(u => `<option value="${this._esc(u.device_id)}" ${u.device_id === deviceId ? 'selected' : ''}>${this._esc(u.name || u.device_id)}</option>`).join('')}
                    </select></div>
            </div>
            <div id="cmdParams">${this._cmdFormHtml(cmd, 'cmdP')}</div>
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                <button type="button" class="btn btn-ghost btn-sm" ${this._act('runCommand', [true])}
                        title="${t('commands.testHint', 'Reads the device and shows what would be written — nothing is written.')}"><i aria-hidden="true" class="bi bi-eye"></i> ${t('commands.test', 'Test')}</button>
                <span class="field-hint">${t('commands.mqttAt', 'Over MQTT')}: <code id="cmdTopic">${this._esc(g.cmd_topic_prefix || '')}/cmd/${this._esc(cmd.name)}</code></span>
            </div>
            <div id="cmdOut" role="status" aria-live="polite" style="margin-top:8px;font-size:12.5px;"></div>`;
        document.getElementById('endpointFeedback').textContent = '';
        const save = document.querySelector('#endpointModal [data-endpoint-save]');
        if (save) { this._setAction(save, 'runCommand', [false]); save.innerHTML = `<i aria-hidden="true" class="bi bi-send"></i> ${t('commands.run', 'Run')}`; }
        this.openModal('endpointModal');
    },

    _cmdPick() {
        const ctx = this._cmdCtx || {};
        const cmd = (ctx.cmds || []).find(c => c.name === document.getElementById('cmdName')?.value);
        if (!cmd) return;
        document.getElementById('cmdParams').innerHTML = this._cmdFormHtml(cmd, 'cmdP');
        const tp = document.getElementById('cmdTopic');
        if (tp) tp.textContent = tp.textContent.replace(/\/cmd\/.*$/, `/cmd/${cmd.name}`);
        document.getElementById('cmdOut').innerHTML = '';
    },

    async runCommand(dryRun) {
        const t = (k, d) => this.t(k, d);
        const ctx = this._cmdCtx || {};
        const cmd = (ctx.cmds || []).find(c => c.name === document.getElementById('cmdName')?.value);
        const fb = document.getElementById('endpointFeedback'), out = document.getElementById('cmdOut');
        if (!cmd) return;
        const read = this._cmdReadParams(cmd, 'cmdP');
        if (read.error) { fb.textContent = read.error; return; }
        const one = document.getElementById('cmdScope').value;
        const g = (this._endpointDetail?.groups || []).find(x => x.id === ctx.groupId) || {};
        const targets = one ? [one] : (g.units || []).map(u => u.device_id);
        const said = Object.entries(read.params).map(([k, v]) => `${k} = ${v}`).join(', ') || '—';
        if (!dryRun && cmd.confirm !== false
            && !confirm(`${t('commands.confirm', 'Run')} ${cmd.label || cmd.name} (${said}) ${t('commands.on', 'on')} ${one || t('commands.everyUnit', 'every unit of the group')}?`)) return;
        fb.textContent = ''; out.innerHTML = t('common.loading', 'Loading…');
        const rows = [];
        if (dryRun || one) {
            for (const dev of targets) {
                const url = `/api/devices/${encodeURIComponent(dev)}/commands/${encodeURIComponent(cmd.name)}${dryRun ? '/dry-run' : ''}`;
                rows.push(await this._postCommand(url, read.params, dev));
            }
        } else {
            const url = `/api/endpoints/${encodeURIComponent(ctx.endpointId)}/groups/${encodeURIComponent(ctx.groupId)}/commands/${encodeURIComponent(cmd.name)}`;
            const r = await this._postCommand(url, read.params, '');
            rows.push(...(r.units || [r]));
        }
        const gate = rows.find(r => r._gate);
        if (gate) { fb.textContent = gate._gate; out.innerHTML = ''; return; }
        out.innerHTML = rows.map(r => this._cmdResultHtml(r)).join('');
        if (!dryRun) this._refreshEndpointDetail(ctx.endpointId);
    },

    // POST a command; a gate (403/404) comes back as `_gate` text for the feedback line
    async _postCommand(url, params, device) {
        try {
            const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(params) });
            const d = await r.json().catch(() => ({}));
            if (r.status === 403 || r.status === 404 || r.status === 401)
                return { device, _gate: (d.detail?.errors || [d.detail || d.reason || r.statusText]).join(' · ') };
            return { device, ...d };
        } catch (e) { return { device, status: 'error', reason: e.message }; }
    },

    // ── the group editor ────────────────────────────────────────────────────
    //
    // A group answers one question: WHAT does this installation hold, and which
    // units are it. Everything about HOW they are reached lives in its sources.
    async openGroupModal(endpointId, groupId) {
        const p = this._endpointDetail;
        if (!p || p.id !== endpointId) return;
        const t = (k, d) => this.t(k, d);
        const g = (p.groups || []).find(x => x.id === groupId) || null;
        this._grpEditId = g ? g.id : '';
        let templates = [];
        try { templates = (await (await fetch('/api/device-templates')).json()).templates || []; }
        catch (e) { console.error(e); }
        this._srcTemplates = templates;
        const role0 = g?.role || 'inverter';
        const proto0 = ((g?.sources || [])[0] || {}).protocol || (p.connection || {}).protocol || 'tcp';
        const tplOpts = this._srcTplOptions(proto0, g?.template || '', role0);
        const units = (g?.units || []).map(u => u.unit_id).join(', ');
        const roles = ['inverter', 'site', 'meter', 'battery', 'sensor'];
        const ROLE_LABEL = { inverter: 'Inverters', site: 'Site totals', meter: 'Grid meter', battery: 'Battery', sensor: 'Sensors' };
        const PROTO = { tcp: 'Modbus TCP', 'rtu-tcp': 'Modbus RTU over TCP', http: 'Solar API / HTTP JSON' };
        document.getElementById('endpointModalTitle').textContent = g
            ? t('endpoints.groupEditTitle', 'Edit group') : t('endpoints.groupAddTitle', 'Add group');
        document.getElementById('endpointModalBody').innerHTML = `
            <p class="field-hint" style="margin:0 0 12px;">${t('endpoints.groupIntro',
                'A group is one kind of thing this installation holds — its inverters, or the meter at its grid connection. Groups are totalled separately, because adding a meter\'s power to the inverters\' would describe nothing.')}</p>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="grpId">${t('endpoints.groupId', 'Group ID')}</label>
                    <input id="grpId" class="input" value="${this._esc(g?.id || '')}" ${g ? 'disabled' : ''} placeholder="inverters"></div>
                <div class="form-group"><label class="form-label" for="grpRole">${t('endpoints.groupRole', 'Holds')}</label>
                    <select id="grpRole" class="input" data-action="_grpKindChanged" data-on="change">${roles.map(r =>
                        `<option value="${r}" ${role0 === r ? 'selected' : ''}>${t('plant.role.' + r, ROLE_LABEL[r])}</option>`).join('')}</select></div>
                <div class="form-group flex-2"><label class="form-label" for="grpTpl">${t('devices.wizard.template', 'Template')}</label>
                    <select id="grpTpl" class="input">${tplOpts}</select>
                    <div class="field-hint">${t('endpoints.groupTplHint', 'The map for this kind of unit — used by any source of the group that declares none of its own.')}</div></div>
            </div>
            <div class="form-row">
                <div class="form-group flex-2"><label class="form-label" for="grpUnits">${t('endpoints.unitsLabel', 'Unit IDs')}</label>
                    <input id="grpUnits" class="input" value="${this._esc(units)}" placeholder="1, 2, 3, 4">
                    <div class="field-hint">${t('endpoints.unitsHint', 'Comma-separated, ranges allowed (1-4).')}
                        ${g ? t('endpoints.unitsKeep', 'Per-unit names are kept — rename a unit in its row.') : ''}</div></div>
            </div>
            ${g ? '' : `<div class="form-row">
                <div class="form-group"><label class="form-label" for="grpProto">${t('endpoints.srcWay', 'Read over')}</label>
                    <select id="grpProto" class="input" data-action="_grpKindChanged" data-on="change">${['tcp', 'rtu-tcp', 'http'].map(x =>
                        `<option value="${x}" ${proto0 === x ? 'selected' : ''}>${PROTO[x]}</option>`).join('')}</select></div>
                <div class="form-group flex-2"><label class="form-label" for="grpAddr">${t('endpoints.srcAddress', 'Address')}</label>
                    <input id="grpAddr" class="input" value="${this._esc((p.connection || {}).host || '')}" placeholder="192.168.1.50">
                    <div class="field-hint">${t('endpoints.groupAddrHint', 'Host for Modbus, or a URL with ${unit_id} for HTTP. You can add more sources afterwards.')}</div></div>
                <div class="form-group"><label class="form-label" for="grpPort">${t('endpoints.port', 'Port')}</label>
                    <input id="grpPort" class="input" type="number" value="${(p.connection || {}).port || 502}"></div>
            </div>`}
            <label class="form-label" style="display:flex;align-items:center;gap:8px;">
                <input type="checkbox" id="grpEnabled" ${g?.enabled !== false ? 'checked' : ''}>
                ${t('endpoints.groupOnLong', 'Read this group')}</label>
            ${g && (g.commands || []).length ? `<fieldset class="form-group" style="margin-top:12px;border:0;padding:0;">
                <legend class="form-label">${t('commands.groupAccepts', 'Commands this group accepts')}</legend>
                <div class="field-hint" style="margin:0 0 6px;">${t('commands.groupAcceptsHint', 'Offered by the template of the Modbus source. A ticked command can be run from this page, over MQTT and from Home Assistant; an unticked one is refused everywhere.')}</div>
                ${g.commands.map(c => `<label class="form-label" style="display:flex;align-items:center;gap:8px;font-weight:normal;">
                    <input type="checkbox" data-grp-cmd="${this._esc(c.name)}" ${c.enabled ? 'checked' : ''}>
                    ${this._esc(c.label || c.name)} <span class="dev-chip">${this._esc(c.name)}</span></label>`).join('')}
            </fieldset>` : ''}`;
        document.getElementById('endpointFeedback').textContent = '';
        const save = document.querySelector('#endpointModal [data-endpoint-save]');
        if (save) { save.innerHTML = `<i aria-hidden="true" class="bi bi-check-lg"></i> ${this.t('common.save', 'Save')}`; this._setAction(save, 'saveGroup', [endpointId]); }
        this.openModal('endpointModal');
    },

    // the template list follows the kind of unit and the way it is read
    _grpKindChanged() {
        const role = document.getElementById('grpRole')?.value || '';
        const proto = document.getElementById('grpProto')?.value
            || (((this._endpointDetail?.groups || []).find(x => x.id === this._grpEditId) || {}).sources || [])[0]?.protocol || 'tcp';
        const tpl = document.getElementById('grpTpl');
        if (tpl) tpl.innerHTML = this._srcTplOptions(proto, tpl.value, role);
    },

    async saveGroup(endpointId) {
        const p = this._endpointDetail;
        const fb = document.getElementById('endpointFeedback');
        const v = id => (document.getElementById(id) || {}).value;
        const id = (this._grpEditId || v('grpId') || '').trim().toLowerCase();
        if (!id) { fb.textContent = this.t('endpoints.groupNeedId', 'Group ID is required.'); return; }
        const units = this._parseUnitList(v('grpUnits'));
        if (!units || !units.length) {
            fb.textContent = this.t('endpoints.badUnits',
                'Unit IDs: use numbers, commas and ranges (e.g. 1, 2, 5-8).');
            return;
        }
        const prev = (p.groups || []).find(x => x.id === id);
        const out = {
            id, role: v('grpRole') || '',
            enabled: document.getElementById('grpEnabled').checked,
            units: this._mergeUnitIds(prev, units),
        };
        if (v('grpTpl')) out.template = v('grpTpl');
        if (prev) {
            const raw = this._rawGroup(p, id);
            if (raw.sources) out.sources = raw.sources;
            else if (raw.connection) out.connection = raw.connection;
            // the ticked commands, each keeping its stored binding (faces, lease)
            const ticked = [...document.querySelectorAll('#endpointModal [data-grp-cmd]')];
            if (ticked.length) {
                out.commands = ticked.filter(x => x.checked).map(x => {
                    const c = (prev.commands || []).find(y => y.name === x.dataset.grpCmd) || {};
                    return c.binding ? { ...c.binding, enabled: true } : { name: x.dataset.grpCmd };
                });
            } else if (raw.commands) out.commands = raw.commands;
        } else {
            const proto = v('grpProto') || 'tcp';
            const addr = (v('grpAddr') || '').trim();
            out.connection = proto === 'http'
                ? { protocol: 'http', url: addr }
                : { protocol: proto, host: addr, port: parseInt(v('grpPort'), 10) || 502 };
        }
        const list = prev
            ? (p.groups || []).map(x => x.id === id ? out : this._rawGroup(p, x.id))
            : (p.groups || []).map(x => this._rawGroup(p, x.id)).concat([out]);
        if (await this._saveGroups(endpointId, list)) this.closeModal('endpointModal');
    },

    // keep a unit's hand-written id and name when the operator only edits the
    // id list — otherwise editing a group would rename its devices and orphan
    // their history
    _mergeUnitIds(prevGroup, unitIds) {
        const byId = {};
        for (const u of (prevGroup?.units || [])) byId[u.unit_id] = u;
        return unitIds.map(uid => {
            const o = byId[uid];
            return o ? { unit_id: uid, id: o.device_id, ...(o.name ? { name: o.name } : {}) }
                     : uid;
        });
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
        // a percentage or a temperature reads to one decimal, a voltage to two
        const nd = (unit === '%' || unit === '°C') ? 1 : unit === 'V' ? 2 : null;
        const r = nd != null ? v.toFixed(nd)
            : Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
        return unit ? `${r} ${this._esc(unit)}` : r;
    },

    _endpointAggGridHtml(p) {
        const meta = p.aggregate_fields || {};
        const skip = new Set(['units_online', 'units_total', 'status']);
        const present = Object.keys(p.aggregates || {}).filter(n => !skip.has(n));
        // the order the template declares them (its headline totals first);
        // anything else alphabetically after
        const order = (p.aggregate_order || []).filter(n => present.includes(n));
        const names = order.concat(present.filter(n => !order.includes(n))
            .sort((a, b) => ((meta[a] || {}).topic || a).localeCompare((meta[b] || {}).topic || b)));
        if (!names.length) {
            return `<span class="field-hint">${this.t('endpoints.noAggregates',
                'Nothing is fresh right now — the total publishes its census and status, and resumes when a unit reports.')}</span>`;
        }
        const tile = n => {
            const m = meta[n] || {};
            return `<div>
                <div style="color:var(--text-secondary);font-size:11.5px;" title="${this._esc(n)}">${this._esc(m.label || n)}</div>
                <div style="font-weight:600;font-size:14px;font-variant-numeric:tabular-nums;">${this._endpointValue(p.aggregates[n], m.unit || '')}</div>
            </div>`;
        };
        const grid = list => `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px 22px;">${list.map(tile).join('')}</div>`;
        const FIRST = 12;
        return grid(names.slice(0, FIRST)) + (names.length > FIRST ? `
            <details style="margin-top:10px;"><summary style="cursor:pointer;font-size:12.5px;color:var(--text-secondary);">
              ${this.t('endpoints.aggMore', 'All totals')} (${names.length})</summary>
              <div style="margin-top:10px;">${grid(names.slice(FIRST))}</div></details>` : '');
    },

    _endpointUnitRowsHtml(p, cols = ['power_active_total']) {
        const hc = { ok: 'var(--success,#22c55e)', degraded: 'var(--warning,#f59e0b)',
                     down: 'var(--danger,#ef4444)', idle: 'var(--text-secondary,#8a94a0)' };
        const t = this.t.bind(this);
        const meta = p.fields || {};
        return (p.units || []).map(u => {
            const age = u.staleness_age_s != null ? `${u.staleness_age_s} s` : '—';
            const live = u.live || {};
            const via = Object.entries(u.sources || {}).map(([sid, st]) =>
                `<span class="dev-chip" title="${this._esc(sid)}: ${this._esc(st)}"><span class="status-dot" style="--dot:${hc[st] || hc.idle};width:7px;height:7px;margin-right:4px;" aria-hidden="true"></span>${this._esc(sid)}&nbsp;<span style="color:var(--text-secondary);">${this._esc(st)}</span></span>`).join(' ');
            return `<tr data-unit="${this._esc(u.device_id)}">
                <td style="white-space:nowrap;">${u.unit_id} <span class="dev-chip col-wide">${this._esc(u.device_id)}</span></td>
                <td data-unit-name class="col-wide">${this._esc(u.name || '')}</td>
                ${cols.map((c, i) => `<td ${i > 1 ? 'class="col-wide"' : ''} style="text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;">${
                    live[c] == null ? '<span style="color:var(--text-tertiary,#8a94a0);">—</span>'
                                    : this._endpointValue(live[c], (meta[c] || {}).unit || '')}</td>`).join('')}
                <td style="white-space:nowrap;" title="${this._esc(u.health || 'idle')}"><span class="status-dot" style="--dot:${hc[u.health] || hc.idle}" aria-hidden="true"></span> <span class="col-wide-text">${this._esc(u.health || 'idle')}</span>${this._unitAlarmPill(u.alarms)}</td>
                <td class="col-wide" title="${this._esc(u.last_seen || '')}">${age}</td>
                <td class="col-wide">${via || '—'}</td>
                <td style="text-align:right;white-space:nowrap;">
                    <button data-admin class="btn btn-ghost btn-sm col-wide" ${this._act('endpointRenameUnit', [u.device_id])}
                            title="${t('endpoints.renameUnit', 'Rename this unit')}" aria-label="${t('endpoints.renameUnit', 'Rename this unit')}"><i aria-hidden="true" class="bi bi-pencil"></i></button>
                    <button class="btn btn-ghost btn-sm" ${this._act('openDeviceDetail', [u.device_id])}
                            title="${t('endpoints.openUnit', 'Open this unit')}" aria-label="${t('endpoints.openUnit', 'Open this unit')}"><i aria-hidden="true" class="bi bi-box-arrow-up-right"></i></button>
                </td>
            </tr>`;
        }).join('') || `<tr><td colspan="${5 + cols.length}"><span class="field-hint">${this.t('endpoints.noUnits', 'No units materialized.')}</span></td></tr>`;
    },

    // The page, in the operator's order: is it producing and healthy — every
    // unit — how it is read — where it publishes. Nothing about the fallback
    // connection or the endpoint default topic that no group uses.
    _endpointDetailHtml(p) {
        const t = this.t.bind(this);
        const mq = p.mqtt || {}, ix = p.influxdb || {};
        const groups = p.groups || [];
        const fact = (label, val) => `<div><div style="color:var(--text-secondary);font-size:11.5px;">${label}</div>
            <div style="font-weight:600;font-size:13.5px;word-break:break-all;">${val}</div></div>`;
        const outRows = groups.length ? groups.map(g => `<tr>
                <td style="padding:4px 14px 4px 0;white-space:nowrap;">${this._esc(this._groupLabel(g))}</td>
                <td style="padding:4px 14px 4px 0;"><code style="font-size:11px;">${this._esc(this._topicShown((g.outputs || {}).topic_prefix || mq.topic_prefix || '—', p))}/…</code></td>
                <td style="padding:4px 14px 4px 0;"><code style="font-size:11px;">${this._esc((g.outputs || {}).bucket || ix.bucket || '—')}</code>
                    <span style="color:var(--text-secondary);font-size:11.5px;">· ${this._esc((g.outputs || {}).device_tag || ix.device_tag || '—')}</span></td>
                <td style="padding:4px 0;"><code style="font-size:11px;">${g.topic ? this._esc(g.topic) + '/…' : '<span style="color:var(--text-tertiary);">—</span>'}</code></td>
            </tr>`).join('')
            : `<tr><td style="padding:4px 14px 4px 0;"></td>
                <td style="padding:4px 14px 4px 0;"><code style="font-size:11px;">${this._esc(this._topicShown(mq.topic_prefix || '—', p))}/…</code></td>
                <td style="padding:4px 14px 4px 0;"><code style="font-size:11px;">${this._esc(ix.bucket || '—')}</code> <span style="color:var(--text-secondary);font-size:11.5px;">· ${this._esc(ix.device_tag || '—')}</span></td>
                <td style="padding:4px 0;"><code style="font-size:11px;">mbg/endpoints/${this._esc(p.id)}/…</code></td></tr>`;
        return `
        <div data-endpoint-page="${this._esc(p.id)}">
        <div class="section-header">
            <h2><button class="btn btn-ghost btn-sm" data-action="closeEndpointDetail" aria-label="${t('common.back', 'Back')}"><i aria-hidden="true" class="bi bi-arrow-left"></i></button>
                <i aria-hidden="true" class="bi bi-diagram-3"></i> ${this._esc(p.name || p.id)}
                <span class="dev-chip">${this._esc(p.id)}</span></h2>
            <div class="header-actions">
                <button data-admin class="btn btn-secondary btn-sm" ${this._act('testEndpointUi', [p.id], { el: true })}
                        title="${t('endpoints.testHint', 'Asks every unit over every way it is read — the Solar API by URL, Modbus by one read on its own connection. A Modbus probe opens one more client on the datalogger; they serve only a few at once.')}"><i aria-hidden="true" class="bi bi-activity"></i> ${t('endpoints.test', 'Test units')}</button>
                <button data-admin class="btn btn-secondary btn-sm" ${this._act('openEndpointModal', [p.id])}><i aria-hidden="true" class="bi bi-pencil-square"></i> ${t('common.edit', 'Edit')}</button>
                <button data-admin class="btn btn-ghost btn-sm" ${this._act('refreshEndpointFromTemplate', [p.id], { el: true })}
                        title="${t('endpoints.refreshTplHint', 'Bring the units\' labels, units, categories and totals up to date with their template. Your selection, dashboard choices, outputs and formulas stay as they are. The units restart (a few seconds).')}"><i aria-hidden="true" class="bi bi-arrow-repeat"></i> ${t('endpoints.refreshTpl', 'Update from template')}</button>
                <button data-admin class="btn btn-ghost btn-sm" ${this._act('deleteEndpointUi', [p.id])}><i aria-hidden="true" class="bi bi-trash"></i> ${t('common.delete', 'Delete')}</button>
            </div>
        </div>

        <div class="settings-card">
            <div class="settings-card-body">
                <div style="display:flex;flex-wrap:wrap;gap:18px 36px;align-items:flex-start;">
                    <div style="min-width:170px;">
                        <div style="display:flex;align-items:center;gap:10px;">
                            <span class="status-dot" id="plStatusDot" style="width:12px;height:12px;border-radius:50%;--dot:${this._endpointStatusColor(p)};background:var(--dot);box-shadow:0 0 0 3px color-mix(in srgb, var(--dot) 16%, transparent);" aria-hidden="true"></span>
                            <span style="font-weight:600;font-size:15px;" id="plStatusWord">${this._esc(this._endpointStatusWord(p))}</span>
                            ${p.write_locked ? `<span class="sink-pill warn">${t('devices.writeLock.locked', 'locked')}</span>` : ''}
                        </div>
                        <div class="dev-chip" id="plCensus" style="margin-top:8px;">${this._endpointCensusText(p)}</div>
                    </div>
                    <div id="plHeadline" style="display:flex;gap:18px 36px;flex-wrap:wrap;">${this._endpointHeadlineHtml(p)}</div>
                </div>
                <div id="plReadVia" style="margin-top:16px;">${this._endpointReadViaHtml(p)}</div>
                <div id="plPublish" style="margin-top:12px;display:flex;flex-wrap:wrap;gap:4px 8px;align-items:baseline;">${this._endpointPublishHtml(p)}</div>
            </div>
        </div>
        <div id="plTestOut"></div>

        <div class="section-header" style="margin-top:18px;">
            <h3 style="margin:0;"><i aria-hidden="true" class="bi bi-collection"></i> ${t('endpoints.holds', 'What it holds')}</h3>
            <div class="header-actions">
                <button data-admin class="btn btn-secondary btn-sm" ${this._act('openGroupModal', [p.id, ''])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('endpoints.groupAdd', 'Add group')}</button>
            </div>
        </div>
        <div id="plGroups">${this._endpointGroupsHtml(p)}</div>

        <div class="settings-card">
            <div class="settings-card-header">
                <h3><i aria-hidden="true" class="bi bi-signpost-split"></i> ${t('endpoints.whereItPublishes', 'Where it publishes')}</h3>
                <label class="switch-label">
                    <input data-admin type="checkbox" id="plAggEnabled" ${p.aggregates_enabled !== false ? 'checked' : ''}
                           ${this._act('toggleEndpointAggregates', [p.id], {el: true, on: "change"})}>
                    <span>${t('endpoints.aggEnable', 'Publish group totals')}</span>
                </label>
            </div>
            <div class="settings-card-body">
                <div style="overflow-x:auto;"><table style="font-size:13px;">
                    <tr style="color:var(--text-secondary);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;">
                        <td style="padding-right:14px;">${t('endpoints.group', 'Group')}</td>
                        <td style="padding-right:14px;">${t('endpoints.unitTopics', 'Unit topics')}</td>
                        <td style="padding-right:14px;">InfluxDB · ${t('devices.wizard.deviceTag', 'device tag')}</td>
                        <td>${t('endpoints.totalsTopic', 'Totals')}</td>
                    </tr>
                    ${outRows}
                </table></div>
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px 24px;margin-top:14px;">
                    ${fact(t('endpoints.haDiscovery', 'Home Assistant discovery'), mq.ha_discovery ? t('common.on', 'on') : t('common.off', 'off'))}
                    ${fact(t('devices.sink.httpTitle', 'HTTP / JSON output'), p.http_output_enabled ? t('common.on', 'on') : t('common.off', 'off'))}
                    ${fact(t('devices.sink.restTitle', 'REST push'), (p.rest_push || {}).enabled ? t('common.on', 'on') : t('common.off', 'off'))}
                </div>
                <p class="field-hint" style="margin-top:14px;"><i aria-hidden="true" class="bi bi-lock"></i>
                    ${t('endpoints.routingFixed', 'Topics and buckets are fixed after creation — changing them would re-route every unit and orphan their history and Home Assistant entities. Home Assistant, HTTP and REST outputs are switched on any unit’s Outputs tab and apply to the whole installation.')}</p>
            </div>
        </div>
        <div id="plTotals">${this._endpointTotalsHtml(p)}</div>
        <div id="plInfluxExtras">${this._endpointInfluxExtrasHtml(p)}</div>
        </div>`;
    },

    // ── how the totals are made / where else they are written ─────────────────
    // Everything in the open and the operator's to change: which unit fields a
    // total is built from (the template's declaration is only the default), the
    // static tags on every unit's points, and any extra InfluxDB measurement
    // the totals are written to. Nothing here knows a vendor.
    _endpointTotalsHtml(p) {
        const t = this.t.bind(this);
        const own = p.totals || {}, decl = p.totals_declared || {};
        const names = [...new Set([...Object.keys(decl), ...Object.keys(own)])].sort();
        const rows = names.map(n => {
            const d = own[n] || decl[n];
            const mine = !!own[n];
            return `<tr>
                <td style="padding:4px 14px 4px 0;"><code style="font-size:11.5px;">${this._esc(n)}</code></td>
                <td style="padding:4px 14px 4px 0;">${this._esc(d.op)}</td>
                <td style="padding:4px 14px 4px 0;font-size:12px;">${(d.from || []).map(f => `<code style="font-size:11px;">${this._esc(f)}</code>`).join(' ')}</td>
                <td style="padding:4px 14px 4px 0;"><span class="sink-pill ${mine ? 'ok' : ''}">${mine ? t('endpoints.totals.own', 'yours') : t('endpoints.totals.template', 'template')}</span></td>
                <td style="padding:4px 0;white-space:nowrap;">
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('openTotalModal', [p.id, n])} aria-label="${t('common.edit', 'Edit')}"><i aria-hidden="true" class="bi bi-pencil"></i></button>
                    ${mine ? `<button data-admin class="btn btn-ghost btn-sm" ${this._act('removeEndpointTotal', [p.id, n])} aria-label="${t('endpoints.totals.reset', 'Back to the template')}" title="${t('endpoints.totals.reset', 'Back to the template')}"><i aria-hidden="true" class="bi bi-arrow-counterclockwise"></i></button>` : ''}
                </td></tr>`;
        }).join('');
        return `<div class="settings-card">
            <div class="settings-card-header">
                <h3><i aria-hidden="true" class="bi bi-calculator"></i> ${t('endpoints.totals.title', 'How the totals are made')}</h3>
                <button data-admin class="btn btn-secondary btn-sm" ${this._act('openTotalModal', [p.id, ''])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('endpoints.totals.add', 'Add total')}</button>
            </div>
            <div class="settings-card-body">
                ${rows ? `<details><summary style="cursor:pointer;font-size:12.5px;color:var(--text-secondary);">
                  ${names.length} ${t('endpoints.totals.count', 'totals')} · ${Object.keys(own).length} ${t('endpoints.totals.ownCount', 'yours')}</summary>
                  <div style="overflow-x:auto;margin-top:8px;"><table style="font-size:13px;">
                    <tr style="color:var(--text-secondary);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;">
                        <td style="padding-right:14px;">${t('endpoints.totals.name', 'Total')}</td>
                        <td style="padding-right:14px;">${t('endpoints.totals.op', 'How')}</td>
                        <td style="padding-right:14px;">${t('endpoints.totals.from', 'From the units’ fields')}</td>
                        <td style="padding-right:14px;">${t('endpoints.totals.origin', 'Defined by')}</td><td></td></tr>
                    ${rows}</table></div></details>`
                  : `<span class="field-hint">${t('endpoints.totals.none', 'No totals declared by the template — add your own.')}</span>`}
                <p class="field-hint" style="margin-top:10px;">${t('endpoints.totals.hint', 'A total you define replaces the template’s total of the same name. It is computed over the fresh units and published with the other totals (MQTT and InfluxDB).')}</p>
            </div></div>`;
    },

    _endpointInfluxExtrasHtml(p) {
        const t = this.t.bind(this);
        const ix = p.influxdb || {};
        const tags = Object.entries(ix.tags || {});
        const outs = ix.outputs || [];
        const outRows = outs.map(o => `<tr>
                <td style="padding:4px 14px 4px 0;"><code style="font-size:11.5px;">${this._esc(o.measurement)}</code>
                    ${o.enabled === false ? `<span class="sink-pill">${t('common.off', 'off')}</span>` : ''}</td>
                <td style="padding:4px 14px 4px 0;"><code style="font-size:11px;">${this._esc(o.bucket || ix.bucket || p.id)}</code></td>
                <td style="padding:4px 14px 4px 0;font-size:12px;">${(o.fields || []).length} ${t('endpoints.outputs.fields', 'fields')} · ${this._esc(o.mode || 'changed')}</td>
                <td style="padding:4px 0;white-space:nowrap;">
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('openInfluxOutputModal', [p.id, o.id])} aria-label="${t('common.edit', 'Edit')}"><i aria-hidden="true" class="bi bi-pencil"></i></button>
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('removeInfluxOutput', [p.id, o.id])} aria-label="${t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-trash"></i></button>
                </td></tr>`).join('');
        return `<div class="settings-card">
            <div class="settings-card-header">
                <h3><i aria-hidden="true" class="bi bi-database"></i> ${t('endpoints.influx.title', 'InfluxDB — tags and extra outputs')}</h3>
            </div>
            <div class="settings-card-body">
                <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;">
                    <span style="color:var(--text-secondary);font-size:12px;">${t('endpoints.influx.tags', 'Tags on every unit’s points')}</span>
                    ${tags.length ? tags.map(([k, v]) => `<code style="font-size:11.5px;">${this._esc(k)}=${this._esc(v)}</code>`).join(' ')
                                  : `<span class="field-hint">${t('common.none', 'none')}</span>`}
                    <button data-admin class="btn btn-ghost btn-sm" ${this._act('openInfluxTagsModal', [p.id])}><i aria-hidden="true" class="bi bi-pencil"></i> ${t('common.edit', 'Edit')}</button>
                </div>
                <div style="display:flex;align-items:center;justify-content:space-between;margin-top:14px;">
                    <span style="color:var(--text-secondary);font-size:12px;">${t('endpoints.outputs.title', 'The totals are also written to')}</span>
                    <button data-admin class="btn btn-secondary btn-sm" ${this._act('openInfluxOutputModal', [p.id, ''])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('endpoints.outputs.add', 'Add output')}</button>
                </div>
                ${outRows ? `<div style="overflow-x:auto;margin-top:6px;"><table style="font-size:13px;">
                    <tr style="color:var(--text-secondary);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;">
                        <td style="padding-right:14px;">${t('endpoints.outputs.measurement', 'Measurement')}</td>
                        <td style="padding-right:14px;">${t('devices.wizard.bucket', 'InfluxDB bucket')}</td>
                        <td style="padding-right:14px;">${t('endpoints.outputs.what', 'What')}</td><td></td></tr>
                    ${outRows}</table></div>`
                  : `<p class="field-hint" style="margin:6px 0 0;">${t('endpoints.outputs.none', 'Only the default measurement. Add an output when a consumer reads the totals under a measurement and field names of its own.')}</p>`}
            </div></div>`;
    },

    // a small key=value / multi-row editor reused by the three dialogs below
    _kvRowsHtml(id, rows, cols) {
        const row = (r = {}) => `<div class="form-row" data-kv-row style="align-items:flex-end;margin-bottom:4px;">
            ${cols.map(c => c.options
                ? `<div class="form-group ${c.wide ? 'flex-2' : ''}" style="margin-bottom:0;"><select class="input" data-k="${c.key}" aria-label="${this._esc(c.label)}">
                    ${c.options.map(o => `<option ${String(o) === String(r[c.key] ?? '') ? 'selected' : ''}>${this._esc(o)}</option>`).join('')}</select></div>`
                : `<div class="form-group ${c.wide ? 'flex-2' : ''}" style="margin-bottom:0;"><input class="input" data-k="${c.key}" aria-label="${this._esc(c.label)}"
                    ${c.list ? `list="${c.list}"` : ''} placeholder="${this._esc(c.placeholder || c.label)}" value="${this._esc(r[c.key] ?? '')}"></div>`).join('')}
            <button type="button" class="btn btn-ghost btn-sm" data-kv-del aria-label="${this._esc(this.t('common.delete', 'Delete'))}"><i aria-hidden="true" class="bi bi-x-lg"></i></button></div>`;
        setTimeout(() => {
            const box = document.getElementById(id);
            if (!box || box.dataset.wired) return;
            box.dataset.wired = '1';
            box.addEventListener('click', e => {
                if (e.target.closest('[data-kv-del]')) e.target.closest('[data-kv-row]').remove();
                if (e.target.closest('[data-kv-add]')) {
                    e.target.closest('[data-kv-add]').insertAdjacentHTML('beforebegin', row());
                }
            });
        });
        return `<div id="${id}">${(rows.length ? rows : [{}]).map(row).join('')}
            <button type="button" class="btn btn-ghost btn-sm" data-kv-add><i aria-hidden="true" class="bi bi-plus-lg"></i> ${this.t('common.add', 'Add')}</button></div>`;
    },

    _kvRowsRead(id) {
        return [...document.querySelectorAll(`#${id} [data-kv-row]`)].map(r =>
            Object.fromEntries([...r.querySelectorAll('[data-k]')].map(i => [i.dataset.k, i.value.trim()])))
            .filter(o => Object.values(o).some(v => v !== ''));
    },

    _endpointModalOpen(title, body, action, args) {
        document.getElementById('endpointModalTitle').textContent = title;
        document.getElementById('endpointModalBody').innerHTML = body;
        document.getElementById('endpointFeedback').textContent = '';
        const save = document.querySelector('#endpointModal [data-endpoint-save]')
            || document.querySelector('#endpointModal .btn-primary');
        if (save) { save.innerHTML = `<i aria-hidden="true" class="bi bi-check-lg"></i> ${this.t('common.save', 'Save')}`; this._setAction(save, action, args); }
        this.openModal('endpointModal');
    },

    async _endpointPutPartial(id, extra) {
        const p = this._endpointDetail;
        const fb = document.getElementById('endpointFeedback');
        const rsp = await fetch(`/api/endpoints/${encodeURIComponent(id)}`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(this._endpointPutBody(p, extra)),
        });
        if (!rsp.ok) {
            const d = await rsp.json().catch(() => ({}));
            const errs = d.detail?.errors || [d.detail || rsp.statusText];
            const msg = Array.isArray(errs) ? errs.join(' · ') : String(errs);
            if (fb && document.getElementById('endpointModal')?.classList.contains('active')) {
                fb.textContent = msg; fb.className = 'save-feedback err';
            } else this.showToast('error', this.t('endpoints.saveFail', 'Save failed'), msg);
            return false;
        }
        this.closeModal('endpointModal');
        this.showToast('success', this.t('endpoints.saved', 'Endpoint saved'), '');
        const r = await fetch(`/api/endpoints/${encodeURIComponent(id)}`);
        if (r.ok) {
            const np = await r.json();
            this._endpointDetail = np;
            const a = document.getElementById('plTotals'), b = document.getElementById('plInfluxExtras');
            // the operator was working in the totals list: keep it open
            const wasOpen = !!(a && a.querySelector('details[open]'));
            if (a) a.innerHTML = this._endpointTotalsHtml(np);
            if (wasOpen) a.querySelector('details')?.setAttribute('open', '');
            if (b) b.innerHTML = this._endpointInfluxExtrasHtml(np);
        }
        return true;
    },

    openInfluxTagsModal(id) {
        const p = this._endpointDetail, t = this.t.bind(this);
        const rows = Object.entries((p.influxdb || {}).tags || {}).map(([k, v]) => ({ k, v }));
        this._endpointModalOpen(t('endpoints.influx.tagsTitle', 'Tags on every unit’s points'), `
            <p class="field-hint" style="margin:0 0 10px;">${t('endpoints.influx.tagsHint', 'Written on every point of every unit, raw and calculated. ${unit_id}, ${endpoint_id} and ${device_id} are substituted per unit. A tag is part of a series’ identity: changing one starts a new series.')}</p>
            ${this._kvRowsHtml('plTagRows', rows, [
                { key: 'k', label: t('endpoints.influx.tagName', 'Tag'), placeholder: 'battery_id' },
                { key: 'v', label: t('endpoints.influx.tagValue', 'Value'), placeholder: '${unit_id}', wide: true }])}`,
            'saveInfluxTags', [id]);
    },

    async saveInfluxTags(id) {
        const tags = Object.fromEntries(this._kvRowsRead('plTagRows').map(r => [r.k, r.v]));
        const ix = { ...(this._endpointDetail.influxdb || {}), tags };
        delete ix.outputs;                       // not sent → kept server-side
        await this._endpointPutPartial(id, { influxdb: ix });
    },

    openTotalModal(id, name) {
        const p = this._endpointDetail, t = this.t.bind(this);
        const d = (p.totals || {})[name] || (p.totals_declared || {})[name] || { op: 'sum', from: [] };
        const fields = p.unit_fields || [];
        const from = new Set(d.from || []);
        this._endpointModalOpen(name ? t('endpoints.totals.edit', 'Edit total') : t('endpoints.totals.add', 'Add total'), `
            <div class="form-row">
                <div class="form-group flex-2"><label class="form-label" for="totName">${t('endpoints.totals.name', 'Total')}</label>
                    <input id="totName" class="input" value="${this._esc(name)}" ${name ? 'disabled' : ''} placeholder="pack_max_temp"></div>
                <div class="form-group"><label class="form-label" for="totOp">${t('endpoints.totals.op', 'How')}</label>
                    <select id="totOp" class="input">${['sum', 'avg', 'min', 'max', 'spread', 'mode'].map(o => `<option ${o === d.op ? 'selected' : ''}>${o}</option>`).join('')}</select></div>
            </div>
            <div class="form-group"><label class="form-label" for="totFilter">${t('endpoints.totals.from', 'From the units’ fields')}</label>
                <input id="totFilter" class="input" placeholder="${this._esc(t('common.search', 'Search'))}" style="margin-bottom:6px;">
                <div id="totFrom" style="max-height:240px;overflow:auto;border:1px solid var(--border);border-radius:8px;padding:6px 10px;columns:2;">
                    ${fields.map(f => `<label style="display:flex;gap:6px;align-items:center;font-size:12.5px;" data-f="${this._esc(f)}">
                        <input type="checkbox" value="${this._esc(f)}" ${from.has(f) ? 'checked' : ''}> <code style="font-size:11.5px;">${this._esc(f)}</code></label>`).join('')}
                </div>
                <div class="field-hint">${t('endpoints.totals.fromHint', 'Every checked field of every fresh unit goes into one pool, then the operation runs over it.')}</div></div>`,
            'saveEndpointTotal', [id, name]);
        document.getElementById('totFilter').addEventListener('input', e => {
            const q = e.target.value.toLowerCase();
            document.querySelectorAll('#totFrom [data-f]').forEach(l => { l.style.display = l.dataset.f.toLowerCase().includes(q) ? '' : 'none'; });
        });
    },

    async saveEndpointTotal(id, name) {
        const n = name || document.getElementById('totName').value.trim();
        const from = [...document.querySelectorAll('#totFrom input:checked')].map(i => i.value);
        const totals = { ...(this._endpointDetail.totals || {}), [n]: { op: document.getElementById('totOp').value, from } };
        await this._endpointPutPartial(id, { totals });
    },

    async removeEndpointTotal(id, name) {
        const totals = { ...(this._endpointDetail.totals || {}) };
        delete totals[name];
        await this._endpointPutPartial(id, { totals });
    },

    openInfluxOutputModal(id, oid) {
        const p = this._endpointDetail, t = this.t.bind(this);
        const ix = p.influxdb || {};
        const o = (ix.outputs || []).find(x => x.id === oid) || { enabled: true, mode: 'changed', fields: [], tags: {} };
        const sources = [...new Set([...Object.keys(p.aggregates || {}),
            ...Object.keys(p.totals || {}), ...Object.keys(p.totals_declared || {}),
            'units_online', 'units_total'])].sort();
        const groups = (p.groups || []).map(g => g.id);
        this._endpointModalOpen(oid ? t('endpoints.outputs.edit', 'Edit InfluxDB output') : t('endpoints.outputs.add', 'Add output'), `
            <datalist id="outSources">${sources.map(s => `<option value="${this._esc(s)}">`).join('')}</datalist>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="outId">ID</label>
                    <input id="outId" class="input" value="${this._esc(o.id || '')}" ${oid ? 'disabled' : ''} placeholder="legacy-pack"></div>
                <div class="form-group flex-2"><label class="form-label" for="outMeas">${t('endpoints.outputs.measurement', 'Measurement')}</label>
                    <input id="outMeas" class="input" value="${this._esc(o.measurement || '')}" placeholder="my_measurement"></div>
                <div class="form-group"><label class="form-label" for="outBucket">${t('devices.wizard.bucket', 'InfluxDB bucket')}</label>
                    <input id="outBucket" class="input" value="${this._esc(o.bucket || '')}" placeholder="${this._esc(ix.bucket || p.id)}"></div>
            </div>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="outMode">${t('endpoints.outputs.mode', 'Write')}</label>
                    <select id="outMode" class="input">
                        <option value="changed" ${o.mode !== 'every' ? 'selected' : ''}>${t('endpoints.outputs.changed', 'when a value changes')}</option>
                        <option value="every" ${o.mode === 'every' ? 'selected' : ''}>${t('endpoints.outputs.every', 'every cycle')}</option></select></div>
                ${groups.length > 1 ? `<div class="form-group"><label class="form-label" for="outGroup">${t('endpoints.group', 'Group')}</label>
                    <select id="outGroup" class="input"><option value="">${this._esc(groups[0])}</option>${groups.slice(1).map(g => `<option ${g === o.group ? 'selected' : ''}>${this._esc(g)}</option>`).join('')}</select></div>` : ''}
                <div class="form-group" style="align-self:flex-end;"><label class="form-label" style="display:flex;gap:8px;align-items:center;">
                    <input type="checkbox" id="outEnabled" ${o.enabled !== false ? 'checked' : ''}> ${t('endpoints.outputs.enabled', 'Write this output')}</label></div>
            </div>
            <div class="wiz-eyebrow">${t('endpoints.outputs.fieldsTitle', 'Fields — name ← total × scale')}</div>
            ${this._kvRowsHtml('outFieldRows', (o.fields || []).map(f => ({ name: f.name, source: f.source, scale: f.scale ?? 1 })), [
                { key: 'name', label: t('endpoints.outputs.fieldName', 'Field'), placeholder: 'total_power' },
                { key: 'source', label: t('endpoints.outputs.source', 'From total'), list: 'outSources', wide: true },
                { key: 'scale', label: t('endpoints.outputs.scale', 'Scale'), placeholder: '1' }])}
            <div class="wiz-eyebrow" style="margin-top:10px;">${t('endpoints.outputs.tagsTitle', 'Tags on this output')}</div>
            ${this._kvRowsHtml('outTagRows', Object.entries(o.tags || {}).map(([k, v]) => ({ k, v })), [
                { key: 'k', label: t('endpoints.influx.tagName', 'Tag') },
                { key: 'v', label: t('endpoints.influx.tagValue', 'Value'), wide: true }])}
            <p class="field-hint">${t('endpoints.outputs.hint', 'One point per cycle with every field whose total exists. ${endpoint_id} is substituted in the bucket and tag values. Empty bucket = the endpoint’s.')}</p>`,
            'saveInfluxOutput', [id, oid]);
    },

    async saveInfluxOutput(id, oid) {
        const g = x => document.getElementById(x);
        const o = {
            id: oid || g('outId').value.trim(),
            enabled: !!g('outEnabled').checked,
            measurement: g('outMeas').value.trim(),
            mode: g('outMode').value,
            fields: this._kvRowsRead('outFieldRows').map(r => ({
                name: r.name, source: r.source, scale: r.scale === '' ? 1 : Number(r.scale) })),
            tags: Object.fromEntries(this._kvRowsRead('outTagRows').map(r => [r.k, r.v])),
        };
        if (g('outBucket').value.trim()) o.bucket = g('outBucket').value.trim();
        if (g('outGroup')?.value) o.group = g('outGroup').value;
        const ix = this._endpointDetail.influxdb || {};
        const outs = (ix.outputs || []).filter(x => x.id !== o.id).concat([o]);
        const send = { ...ix, outputs: outs };
        delete send.tags;                        // not sent → kept server-side
        await this._endpointPutPartial(id, { influxdb: send });
    },

    async removeInfluxOutput(id, oid) {
        if (!confirm(this.t('endpoints.outputs.confirmDelete', 'Stop writing this output? Its history stays in InfluxDB.'))) return;
        const ix = this._endpointDetail.influxdb || {};
        const send = { ...ix, outputs: (ix.outputs || []).filter(x => x.id !== oid) };
        delete send.tags;
        await this._endpointPutPartial(id, { influxdb: send });
    },

    // ── endpoint actions ───────────────────────────────────────────────────────

    async refreshEndpointFromTemplate(id, btn) {
        if (!confirm(this.t('endpoints.refreshTplConfirm', 'Update every unit\'s labels, units and totals from its template? The units restart for a few seconds.'))) return;
        // a template may have gained calculated fields since the units were made
        const addNew = confirm(this.t('endpoints.refreshTplAddNew', 'Also add the calculated fields the template has gained since these units were made? (OK = add them, Cancel = only update the existing rows. A calculated field you deleted on purpose would come back.)'));
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="btn-spinner"></span> …'; }
        try {
            const r = await fetch(`/api/endpoints/${encodeURIComponent(id)}/refresh-from-template${addNew ? '?add_new=true' : ''}`, { method: 'POST' });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) {
                // FastAPI answers a string, {errors: [...]}, or a 422 list of {msg}
                const det = d.detail;
                const msgs = Array.isArray(det) ? det.map(x => x?.msg || String(x))
                    : det?.errors || [typeof det === 'string' ? det : r.statusText];
                throw new Error(msgs.join(' · '));
            }
            const regs = (d.units || []).reduce((a, u) => a + u.registers + u.calculated + (u.added || 0), 0);
            this.showToast('success', this.t('endpoints.refreshTplDone', 'Updated from template'),
                this.t('endpoints.refreshTplRows', '{n} rows updated across {u} units', { n: regs, u: (d.units || []).length }));
            this._refreshEndpointDetail(id);
        } catch (e) {
            this.showToast('error', this.t('endpoints.saveFail', 'Save failed'), e.message);
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = orig; }
        }
    },

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
            el.checked ? this.t('endpoints.aggOn', 'Group totals are published')
                       : this.t('endpoints.aggOff', 'Group totals are off'));
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
        const row = document.querySelector(`[data-group-units] tr[data-unit="${CSS.escape(deviceId)}"]`);
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
        const row = document.querySelector(`[data-group-units] tr[data-unit="${CSS.escape(deviceId)}"]`);
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
        const row = document.querySelector(`[data-group-units] tr[data-unit="${CSS.escape(deviceId)}"]`);
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
        const rows = d.units || [];
        if (!rows.length) {
            out.innerHTML = `<span class="field-hint">${this.t('endpoints.testNone', 'No units to probe.')}</span>`;
            return;
        }
        // A verdict is a WORD first and a colour second — the dot alone says
        // nothing to a screen reader or to a colour-blind operator.
        const word = u => u.ok == null ? this.t('endpoints.testSkipped', 'not probed')
            : u.ok ? this.t('endpoints.testOk', 'answered') : this.t('endpoints.testFail', 'no answer');
        const color = u => u.ok == null ? 'var(--text-secondary,#8a94a0)'
            : u.ok ? 'var(--success,#22c55e)' : 'var(--danger,#ef4444)';
        const sources = (d.sources || []).length ? d.sources
            : [{ id: 'default', protocol: '', probed: rows.length, answered: rows.filter(u => u.ok).length }];
        const perSource = sources.map(s => {
            const mine = rows.filter(u => (u.source || 'default') === s.id);
            const census = s.probed
                ? `${s.answered}/${s.probed} ${this.t('endpoints.testAnswered', 'answered')}`
                : this.t('endpoints.testSkipped', 'not probed');
            return `<div style="margin-bottom:10px;" data-test-source="${this._esc(s.id)}">
                <div style="font-weight:600;font-size:13px;margin-bottom:4px;">${this._esc(s.id)}
                    ${s.protocol ? `<span class="dev-chip">${this._esc(s.protocol)}</span>` : ''}
                    <span style="font-weight:400;color:var(--text-secondary);">· ${census}</span></div>
                ${mine.map(u => `<div style="display:flex;gap:8px;align-items:baseline;font-size:12.5px;padding:2px 0 2px 6px;">
                    <span class="status-dot" style="--dot:${color(u)}" aria-hidden="true"></span>
                    <span class="dev-chip">${this._esc(u.device_id)}</span>
                    <span><b>${word(u)}</b>${u.latency_ms != null && !/\d\s*ms/.test(u.message || '') ? ` · ${u.latency_ms} ms` : ''}${u.message ? ` · ${this._esc(u.message)}` : ''}</span>
                </div>`).join('')}
            </div>`;
        }).join('');
        const when = new Date().toLocaleTimeString();
        out.innerHTML = `<div class="settings-card" role="status" aria-live="polite"><div class="settings-card-body">
            <h3 style="margin:0 0 10px;font-size:14px;"><i aria-hidden="true" class="bi bi-activity"></i>
                ${this.t('endpoints.testResult', 'Test result')} <span style="font-weight:400;color:var(--text-secondary);">· ${when}</span></h3>
            ${perSource}
            <button class="btn btn-ghost btn-sm" ${this._act('_clearHtml', ['plTestOut'])}>${this.t('common.close', 'Close')}</button>
        </div></div>`;
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
        // Remember the WHOLE connection block. The form edits four of its keys;
        // a save that rebuilt the block from the form alone would quietly erase
        // every other one an operator put in config.yaml (timeouts, retry
        // budgets, illegal-register lists) on the first unrelated edit.
        this._endpointEditConn = { ...conn };
        // A grouped installation keeps its units, its ways of being read and
        // its topics on the group cards. This dialog then owns the name and the
        // output switches — the flat form, sent as-is, once flattened a plant
        // to one Modbus group and dropped every source on a rename.
        const grouped = !!(p && (p.groups || []).length);
        this._endpointEditGrouped = grouped;
        const units = (p?.units || []).map(u => u.unit_id).join(', ');
        const lock = editId ? 'disabled' : '';
        const tplOptions = ['<option value="">—</option>'].concat(templates.map(t =>
            `<option value="${this._esc(t.id)}" ${p?.template === t.id ? 'selected' : ''}>${this._esc(t.name || t.id)}</option>`)).join('');
        document.getElementById('endpointModalTitle').textContent = grouped
            ? this.t('endpoints.titleEditInstallation', 'Edit installation')
            : editId ? this.t('endpoints.titleEdit', 'Edit Endpoint') : this.t('endpoints.titleAdd', 'Add Endpoint');
        const sinkToggles = `
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
                ${editId ? this.t('endpoints.pollingEnabled', 'Polling enabled')
                         : this.t('devices.wizard.enabled', 'Start polling immediately after saving')}</label>`;
        if (grouped) {
            document.getElementById('endpointModalBody').innerHTML = `
            <p class="field-hint" style="margin:0 0 12px;">${this.t('endpoints.groupedIntro',
                'The name and the outputs of the whole installation. Its units, how they are read and where they publish are edited on each group card.')}</p>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="plId">${this.t('endpoints.id', 'Endpoint ID')}</label>
                    <input id="plId" class="input" value="${this._esc(p.id)}" disabled></div>
                <div class="form-group flex-2"><label class="form-label" for="plName">${this.t('endpoints.name', 'Name')}</label>
                    <input id="plName" class="input" value="${this._esc(p.name || '')}" placeholder="${this._esc(p.id)}"></div>
            </div>
            ${sinkToggles}`;
            document.getElementById('endpointFeedback').textContent = '';
            const _save = document.querySelector('#endpointModal [data-endpoint-save]');
            if (_save) this._setAction(_save, 'saveEndpoint');
            this.openModal('endpointModal');
            return;
        }
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
                        <option value="rtu_tap" ${conn.protocol === 'rtu_tap' ? 'selected' : ''}>${this.t('endpoints.protoTap', 'Modbus RTU tap (listen-only)')}</option>
                    </select></div>
                <div class="form-group flex-2" data-net-only><label class="form-label" for="plHost">${this.t('endpoints.host', 'Host')}</label>
                    <input id="plHost" class="input" value="${this._esc(conn.host || '')}" placeholder="192.168.1.50"></div>
                <div class="form-group" data-net-only><label class="form-label" for="plPort">${this.t('endpoints.port', 'Port')}</label>
                    <input id="plPort" class="input" type="number" value="${conn.port || 502}"></div>
                <div class="form-group flex-2" data-tap-only hidden><label class="form-label" for="plSerial">${this.t('endpoints.serialPort', 'Serial port')}</label>
                    <input id="plSerial" class="input" value="${this._esc(conn.serial_port || '')}" placeholder="/dev/serial/by-id/usb-...">
                    <div class="field-hint">${this.t('endpoints.tapHint', 'Listen-only: the gateway never transmits — the bus\'s own master sets the rhythm. One process per port.')}</div></div>
                <div class="form-group" data-tap-only hidden><label class="form-label" for="plBaud">${this.t('endpoints.baudrate', 'Baud rate')}</label>
                    <input id="plBaud" class="input" type="number" value="${conn.baudrate || 19200}"></div>
                <div class="form-group" data-net-only><label class="form-label" for="plLanes">${this.t('endpoints.lanes', 'Connections')}</label>
                    <input id="plLanes" class="input" type="number" min="1" max="8" value="${conn.max_connections || 1}">
                    <div class="field-hint">${this.t('endpoints.lanesHint',
                        'Sockets to this master, with the units shared out between them. One is right for a master that serializes internally — most dataloggers — and more is right only where a measurement says so: scripts/calibrate_endpoint.py.')}</div></div>
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
            ${sinkToggles}`;
        document.getElementById('endpointFeedback').textContent = '';
        // serial fields for the tap, network fields for the masters
        const _syncProto = () => {
            const tap = document.getElementById('plProto').value === 'rtu_tap';
            document.querySelectorAll('#endpointModalBody [data-tap-only]')
                .forEach(el => { el.hidden = !tap; });
            document.querySelectorAll('#endpointModalBody [data-net-only]')
                .forEach(el => { el.hidden = tap; });
        };
        document.getElementById('plProto').addEventListener('change', _syncProto);
        _syncProto();
        // the modal shell is shared with the source editor, which repoints this
        // button — reclaim it, or Save would still be saving a source
        const _save = document.querySelector('#endpointModal [data-endpoint-save]');
        if (_save) { this._setAction(_save, 'saveEndpoint'); _save.innerHTML = `<i aria-hidden="true" class="bi bi-check-lg"></i> ${this.t('common.save', 'Save')}`; }
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
        const editing = !!this._endpointEditId;
        const grouped = editing && this._endpointEditGrouped;
        let units = [];
        if (!grouped) {
            units = this._parseUnitList(document.getElementById('plUnits').value);
            if (!units || !units.length) {
                fb.textContent = this.t('endpoints.badUnits', 'Unit IDs: use numbers, commas and ranges (e.g. 1, 2, 5-8).');
                return;
            }
        }
        const body = {
            id: (this._endpointEditId || document.getElementById('plId').value || '').trim().toLowerCase(),
            name: document.getElementById('plName').value.trim(),
            enabled: document.getElementById('plEnabled').checked,
            aggregates: !!document.getElementById('plAggregates')?.checked,
        };
        if (grouped) {
            // the stored connection block travels back untouched; units, sources
            // and topics live on the groups, which the server keeps when the
            // payload does not speak of them
            body.connection = { ...(this._endpointEditConn || {}) };
        } else {
            const _proto = document.getElementById('plProto').value;
            if (_proto === 'rtu_tap') {
                body.connection = {
                    ...(this._endpointEditConn || {}),
                    protocol: _proto,
                    serial_port: document.getElementById('plSerial').value.trim(),
                    baudrate: parseInt(document.getElementById('plBaud').value, 10) || 19200,
                };
                delete body.connection.host;
                delete body.connection.max_connections;
            } else {
                body.connection = {
                    ...(this._endpointEditConn || {}),
                    protocol: _proto,
                    host: document.getElementById('plHost').value.trim(),
                    port: parseInt(document.getElementById('plPort').value, 10) || 502,
                    max_connections: Math.min(8, Math.max(1,
                        parseInt(document.getElementById('plLanes')?.value, 10) || 1)),
                };
            }
            body.units = units;
        }
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
