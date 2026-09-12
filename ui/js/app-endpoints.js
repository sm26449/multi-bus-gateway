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
        set('#plBus', this._endpointBusHtml(p));
        if (!view.querySelector('#plSources [data-src-busy]')) {
            set('#plSources', this._endpointSourcesHtml(p));
        }
        // never redraw the unit rows while one of them is being renamed
        if (!view.querySelector('#plUnitsBody [data-renaming]')) {
            set('#plUnitsBody', this._endpointUnitRowsHtml(p));
        }
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

    // ── Sources ─────────────────────────────────────────────────────────────
    //
    // The ordered ways of reaching one endpoint's units. Configuration and live
    // state together on purpose: "which source is this value from" and "is that
    // source still alive" are the same question for an operator.

    _endpointSourcesHtml(p) {
        const t = (k, d) => this.t(k, d);
        const srcs = p.sources || [];
        if (!srcs.length) {
            return `<span style="color:var(--text-secondary);">${t('endpoints.srcNone',
                'No source declared yet.')}</span>`;
        }
        const rows = srcs.map((s, i) => {
            const ok = s.units_total ? s.units_ok === s.units_total : null;
            const dot = s.enabled === false ? 'var(--text-secondary,#8a94a0)'
                : ok === null ? 'var(--text-secondary,#8a94a0)'
                : ok ? 'var(--success,#22c55e)'
                : s.units_ok ? 'var(--warning,#f59e0b)' : 'var(--danger,#ef4444)';
            const groups = Object.entries(s.poll_groups || {})
                .map(([k, v]) => `${this._esc(k)} ${v}s`).join(' · ') || '—';
            const fails = s.failed_reads
                ? ` <span style="color:var(--danger,#ef4444);">${s.failed_reads} failed</span>` : '';
            return `
            <tr data-src-row="${this._esc(s.id)}" style="border-top:1px solid var(--border,#2a3038);">
              <td style="padding:8px 10px 8px 0;white-space:nowrap;">
                <span class="status-dot" style="--dot:${dot};background:var(--dot);width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:7px;"></span>
                <b>${this._esc(s.id)}</b>
                <span class="dev-chip" style="margin-left:6px;">${t('endpoints.srcRank', 'rank')} ${s.rank}</span>
                ${s.enabled === false ? `<span class="sink-pill warn" style="margin-left:6px;">${t('devices.disabled', 'disabled')}</span>` : ''}
              </td>
              <td style="padding:8px 10px 8px 0;">${this._esc(s.protocol)}</td>
              <td style="padding:8px 10px 8px 0;font-size:12px;word-break:break-all;max-width:280px;">${this._esc(s.address || '')}</td>
              <td style="padding:8px 10px 8px 0;font-size:12px;">${this._esc(s.template || '—')}</td>
              <td style="padding:8px 10px 8px 0;font-size:12px;white-space:nowrap;">${groups}</td>
              <td style="padding:8px 10px 8px 0;font-size:12px;white-space:nowrap;"
                  title="${t('endpoints.srcStaleHint', 'How long this source stays authoritative before a lower-ranked one may fill the field. 0 never yields.')}">${s.stale_after_s ? s.stale_after_s + 's' : '∞'}</td>
              <td style="padding:8px 10px 8px 0;font-variant-numeric:tabular-nums;white-space:nowrap;">
                ${s.units_ok}/${s.units_total} ${t('endpoints.online', 'online')}${fails}
                ${s.latency_ms != null ? ` · ${s.latency_ms} ms` : ''}
              </td>
              <td style="padding:8px 10px 8px 0;font-variant-numeric:tabular-nums;white-space:nowrap;"
                  title="${t('endpoints.srcOwnsHint', 'How many fields this source is authoritative for right now. Zero means a higher-ranked source already supplies everything it offers.')}">
                ${s.fields_owned} ${t('endpoints.srcFields', 'fields')}</td>
              <td style="padding:8px 0;white-space:nowrap;text-align:right;">
                <button class="btn btn-ghost btn-sm" ${i === 0 ? 'disabled' : ''}
                        ${this._act('moveSource', [p.id, s.id, -1])}
                        title="${t('endpoints.srcUp', 'Raise precedence')}"><i aria-hidden="true" class="bi bi-arrow-up"></i></button>
                <button class="btn btn-ghost btn-sm" ${i === srcs.length - 1 ? 'disabled' : ''}
                        ${this._act('moveSource', [p.id, s.id, 1])}
                        title="${t('endpoints.srcDown', 'Lower precedence')}"><i aria-hidden="true" class="bi bi-arrow-down"></i></button>
                <button class="btn btn-ghost btn-sm" ${this._act('openSourceModal', [p.id, s.id])}><i aria-hidden="true" class="bi bi-pencil"></i></button>
                <button class="btn btn-ghost btn-sm" ${srcs.length < 2 ? 'disabled' : ''}
                        ${this._act('deleteSource', [p.id, s.id])}
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
              <td style="padding-right:10px;">${t('endpoints.srcStale', 'yields after')}</td>
              <td style="padding-right:10px;">${t('endpoints.srcLive', 'live')}</td>
              <td style="padding-right:10px;">${t('endpoints.srcOwns', 'owns')}</td>
              <td></td></tr>
            ${rows}</table></div>`;
    },

    // Reordering IS the precedence control, so it writes straight through.
    async moveSource(endpointId, sourceId, delta) {
        const p = this._endpointDetail;
        if (!p || p.id !== endpointId) return;
        const srcs = (p.sources || []).map(s => s.id);
        const i = srcs.indexOf(sourceId), j = i + delta;
        if (i < 0 || j < 0 || j >= srcs.length) return;
        srcs.splice(j, 0, srcs.splice(i, 1)[0]);
        await this._saveSources(endpointId, srcs.map(id => this._rawSource(p, id)));
    },

    async deleteSource(endpointId, sourceId) {
        const p = this._endpointDetail;
        if (!p || (p.sources || []).length < 2) return;
        if (!confirm(this.t('endpoints.srcDeleteAsk', 'Remove source') + ` "${sourceId}"?\n\n`
                + this.t('endpoints.srcDeleteNote',
                    'Its registers stay on disk. The fields it owned fall to the next source that offers them.'))) return;
        const rest = (p.sources || []).filter(s => s.id !== sourceId)
            .map(s => this._rawSource(p, s.id));
        await this._saveSources(endpointId, rest);
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

    async _saveSources(endpointId, sources) {
        const host = document.querySelector('#plSources');
        if (host) host.dataset.srcBusy = '1';
        try {
            const p = this._endpointDetail;
            const body = {
                id: endpointId, name: p.name, template: p.template,
                enabled: p.enabled, connection: p.connection,
                units: (p.units || []).map(u => ({ unit_id: u.unit_id, id: u.device_id, name: u.name })),
                sources,
            };
            const rsp = await fetch(`/api/endpoints/${encodeURIComponent(endpointId)}`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!rsp.ok) {
                const d = await rsp.json().catch(() => ({}));
                this.showToast((d.detail?.errors || [d.detail || rsp.statusText]).join('\n'), 'error');
                return false;
            }
            this.showToast(this.t('endpoints.srcSaved', 'Sources updated'), 'success');
            return true;
        } finally {
            if (host) delete host.dataset.srcBusy;
            await this._refreshEndpointDetail(endpointId);
        }
    },

    // Add or edit ONE source. Everything an operator sets about a way of
    // reaching the units lives here — protocol, address, template, how often,
    // and how long it stays authoritative before a lower source may fill in.
    async openSourceModal(endpointId, sourceId) {
        const p = this._endpointDetail;
        if (!p || p.id !== endpointId) return;
        const t = (k, d) => this.t(k, d);
        const s = (p.sources || []).find(x => x.id === sourceId) || null;
        let templates = [];
        try { templates = (await (await fetch('/api/device-templates')).json()).templates || []; }
        catch (e) { console.error(e); }
        this._srcEditId = s ? s.id : '';
        const proto = s?.protocol || 'tcp';
        const addr = s?.address || '';
        const hostPort = addr.match(/^(.*):(\d+)$/);
        const groups = Object.entries(s?.poll_groups || { normal: 20 })
            .map(([k, v]) => `${k}=${v}`).join(', ');
        const tplOpts = ['<option value="">—</option>'].concat(templates.map(x =>
            `<option value="${this._esc(x.id)}" ${s?.template === x.id ? 'selected' : ''}>${this._esc(x.name || x.id)}</option>`)).join('');
        document.getElementById('endpointModalTitle').textContent = s
            ? t('endpoints.srcEditTitle', 'Edit source') : t('endpoints.srcAddTitle', 'Add source');
        document.getElementById('endpointModalBody').innerHTML = `
            <p class="field-hint" style="margin:0 0 12px;">${t('endpoints.srcIntro',
                'A source is one way of reaching the SAME units. Its position in the list is its precedence: the first source offering a field owns it.')}</p>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="srcId">${t('endpoints.srcName', 'Source ID')}</label>
                    <input id="srcId" class="input" value="${this._esc(s?.id || '')}" ${s ? 'disabled' : ''} placeholder="solar_api"></div>
                <div class="form-group"><label class="form-label" for="srcProto">${t('devices.wizard.protocol', 'Protocol')}</label>
                    <select id="srcProto" class="input" onchange="app._srcProtoChanged()">
                        ${['tcp', 'rtu-tcp', 'rtu', 'http', 'mqtt'].map(x =>
                            `<option value="${x}" ${proto === x ? 'selected' : ''}>${x}</option>`).join('')}
                    </select></div>
                <div class="form-group flex-2"><label class="form-label" for="srcTpl">${t('devices.wizard.template', 'Template')}</label>
                    <select id="srcTpl" class="input">${tplOpts}</select></div>
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
            <div class="form-row">
                <div class="form-group flex-2"><label class="form-label" for="srcGroups">${t('endpoints.srcGroups', 'Intervals')}</label>
                    <input id="srcGroups" class="input" value="${this._esc(groups)}" placeholder="normal=20, slow=120">
                    <div class="field-hint">${t('endpoints.srcGroupsHint', 'group=seconds, comma separated. This source polls ONLY the groups named here.')}</div></div>
                <div class="form-group"><label class="form-label" for="srcStale">${t('endpoints.srcStale', 'Yields after (s)')}</label>
                    <input id="srcStale" class="input" type="number" min="0" step="1" value="${s?.stale_after_s || 0}">
                    <div class="field-hint">${t('endpoints.srcStaleHint2', '0 = never yields. Right for a counter.')}</div></div>
                <div class="form-group"><label class="form-label" for="srcTimeout">${t('endpoints.srcTimeout', 'Timeout (s)')}</label>
                    <input id="srcTimeout" class="input" type="number" min="1" value="${s?.timeout ?? 3}"></div>
            </div>
            <label class="form-label" style="display:flex;align-items:center;gap:8px;">
                <input type="checkbox" id="srcEnabled" ${s?.enabled !== false ? 'checked' : ''}>
                ${t('endpoints.srcEnabled', 'Poll this source')}</label>`;
        document.getElementById('endpointFeedback').textContent = '';
        const save = document.querySelector('#endpointModal [data-endpoint-save]')
            || document.querySelector('#endpointModal .btn-primary');
        if (save) save.setAttribute('onclick', `app.saveSource('${this._esc(endpointId)}')`);
        this.openModal('endpointModal');
    },

    _srcProtoChanged() {
        const v = document.getElementById('srcProto').value;
        const show = (id, on) => { const e = document.getElementById(id); if (e) e.hidden = !on; };
        show('srcAddrTcp', v === 'tcp' || v === 'rtu-tcp');
        show('srcAddrUrl', v === 'http');
        show('srcAddrSerial', v === 'rtu');
        show('srcAddrTopic', v === 'mqtt');
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
        for (const part of (v('srcGroups') || '').split(',')) {
            const m = part.trim().match(/^([a-z0-9_-]+)\s*=\s*([\d.]+)$/i);
            if (m) groups[m[1]] = { interval: parseFloat(m[2]) };
            else if (part.trim()) {
                fb.textContent = this.t('endpoints.srcBadGroups',
                    'Intervals: use group=seconds, comma separated (e.g. normal=20, slow=120).');
                return;
            }
        }
        if (Object.keys(groups).length) out.poll_groups = groups;

        const keep = (p.sources || []).filter(x => x.id !== id).map(x => this._rawSource(p, x.id));
        const list = this._srcEditId
            ? (p.sources || []).map(x => x.id === id ? out : this._rawSource(p, x.id))
            : keep.concat([out]);
        if (await this._saveSources(endpointId, list)) this.closeModal('endpointModal');
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
                <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:16px;font-size:12px;"
                     id="plBus">${this._endpointBusHtml(p)}</div>
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
                <h3><i aria-hidden="true" class="bi bi-diagram-2"></i> ${t('endpoints.sources', 'Sources')}</h3>
                <button class="btn btn-secondary btn-sm" ${this._act('openSourceModal', [p.id, ''])}><i aria-hidden="true" class="bi bi-plus-lg"></i> ${t('endpoints.srcAdd', 'Add source')}</button>
            </div>
            <div class="settings-card-body">
                <div id="plSources">${this._endpointSourcesHtml(p)}</div>
                <p class="field-hint" style="margin-top:12px;"><i aria-hidden="true" class="bi bi-info-circle"></i>
                    ${t('endpoints.srcNote', 'Ordered ways of reaching the SAME units — a datalogger may answer Modbus and HTTP at once. Order is precedence: the first source offering a field owns it, and a lower one fills in only after the owner has been silent for its window. A window of 0 never yields, which is what an energy counter needs so it cannot walk backwards.')}</p>
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
        // Remember the WHOLE connection block. The form edits four of its keys;
        // a save that rebuilt the block from the form alone would quietly erase
        // every other one an operator put in config.yaml (timeouts, retry
        // budgets, illegal-register lists) on the first unrelated edit.
        this._endpointEditConn = { ...conn };
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
                <div class="form-group"><label class="form-label" for="plLanes">${this.t('endpoints.lanes', 'Connections')}</label>
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
        // the modal shell is shared with the source editor, which repoints this
        // button — reclaim it, or Save would still be saving a source
        const _save = document.querySelector('#endpointModal [data-endpoint-save]');
        if (_save) _save.setAttribute('onclick', 'app.saveEndpoint()');
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
                ...(this._endpointEditConn || {}),
                protocol: document.getElementById('plProto').value,
                host: document.getElementById('plHost').value.trim(),
                port: parseInt(document.getElementById('plPort').value, 10) || 502,
                max_connections: Math.min(8, Math.max(1,
                    parseInt(document.getElementById('plLanes')?.value, 10) || 1)),
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
