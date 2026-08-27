/* Multi-Bus Gateway — Power Quality section (Jasic PQ recorder archive).
 *
 * Reached embedded in the device workspace (data-dtab="pq"), like Monitor/
 * History. Left pane: archived PQ events from InfluxDB (/api/pq/events —
 * the full retained history, not just the meter's 32-entry ring), grouped
 * by day with a severity dot per event. Right pane: event detail (cause
 * badges + bound/min/max/avg) and the half-wave-RMS trace of the selected
 * event (/api/pq/waveform — read-through to the meter for ring events that
 * predate the recorder), drawn with the shared _renderHistory renderer.
 */
Object.assign(JanitzaMonitor.prototype, {

    initPowerQualityPage() {
        this._pqWired = this._pqWired || this._wirePqControls();
        this._pqSelected = null;
        this._pqEvents = [];
        this._pqOverlay(this.t('pq.pickEvent', 'Select a PQ event on the left to view its recorded waveform.'));
        this._loadPqEvents(true);
        this._loadPqStatus();
    },

    _wirePqControls() {
        document.getElementById('pqRange')?.addEventListener('change', () => this._loadPqEvents(true));
        document.getElementById('pqRefresh')?.addEventListener('click', () => {
            this._loadPqEvents(false); this._loadPqStatus();
        });
        document.getElementById('pqChannel')?.addEventListener('change', () => {
            if (this._pqSelected) this._loadPqWaveform();
        });
        return true;
    },

    _pqOverlay(msg) {
        const ov = document.getElementById('pqOverlay');
        const canvas = document.getElementById('pqCanvas');
        if (ov) { ov.textContent = msg || ''; ov.style.display = msg ? 'flex' : 'none'; }
        if (msg && canvas) this._clearCanvas(canvas);
        if (msg) { const leg = document.getElementById('pqLegend'); if (leg) leg.innerHTML = ''; }
    },

    // severity of one cause → list-dot / badge color
    _pqSeverity(cause) {
        if (cause.includes('outage')) return { color: '#e5534b', label: this.t('pq.sev.critical', 'outage') };
        if (/^(over_|under_|dt_)/.test(cause)) return { color: '#d29922', label: this.t('pq.sev.warning', 'excursion') };
        return { color: '#12a3b2', label: this.t('pq.sev.info', 'RVC') };
    },

    _pqEventSeverity(ev) {
        let worst = { color: '#12a3b2' }, rank = 0;
        ev.causes.forEach(c => {
            const r = c.cause.includes('outage') ? 2 : (/^(over_|under_|dt_)/.test(c.cause) ? 1 : 0);
            if (r >= rank) { rank = r; worst = this._pqSeverity(c.cause); }
        });
        return worst;
    },

    async _loadPqStatus() {
        try {
            const r = await fetch('/api/pq/status');
            if (!r.ok) return;
            const data = await r.json();
            const dev = (data.devices || []).find(d => d.device === (this._viewDevice || this._primaryDeviceId()))
                     || (data.devices || [])[0];
            const el = document.getElementById('pqInfo');
            if (!el || !dev) return;
            if (!dev.enabled) {
                el.textContent = this.t('pq.disabled',
                    'PQ recorder is not enabled for this device — enable it in the device Outputs tab.');
                return;
            }
            const c = dev.counters || {};
            const bits = [`${this.t('pq.counters', 'Lifetime counters')}: ` +
                `${c.events ?? '—'} ${this.t('pq.eventsWord', 'events')} · ` +
                `${c.transients ?? '—'} ${this.t('pq.transients', 'transients')}`];
            if (dev.last_poll) bits.push(`${this.t('pq.lastPoll', 'last poll')} ${new Date(dev.last_poll * 1000).toLocaleTimeString()}`);
            if (dev.last_error) bits.push(`${this.t('pq.lastError', 'last error')}: ${dev.last_error}`);
            el.textContent = bits.join(' · ');
        } catch (e) { /* status line is best-effort */ }
    },

    async _loadPqEvents(autoselect) {
        const list = document.getElementById('pqEventList');
        if (!list) return;
        const range = document.getElementById('pqRange')?.value || '-7d';
        const seq = (this._pqSeq = (this._pqSeq || 0) + 1);
        list.innerHTML = `<div class="panel-hint">${this.t('common.loading', 'Loading…')}</div>`;
        let rows = [];
        try {
            const r = await fetch(`/api/pq/events?start=${encodeURIComponent(range)}&limit=500${this._viewDeviceQS('&')}`);
            if (!r.ok) {
                const msg = r.status === 503
                    ? this.t('pq.noInflux', 'InfluxDB is not enabled — PQ history needs the InfluxDB output.')
                    : `${this.t('common.error', 'Error')} ${r.status}`;
                if (seq === this._pqSeq) list.innerHTML = `<div class="panel-hint">${this._esc(msg)}</div>`;
                return;
            }
            rows = (await r.json()).events || [];
        } catch (e) {
            if (seq === this._pqSeq) list.innerHTML = `<div class="panel-hint">${this._esc(String(e))}</div>`;
            return;
        }
        if (seq !== this._pqSeq) return;              // stale response — a newer load won

        // One row per cause+channel — fold rows sharing the event timestamp
        // into one visual event carrying all its causes + the extreme values.
        const byTs = new Map();
        rows.forEach(row => {
            const ev = byTs.get(row.ts_ms) || { ts_ms: row.ts_ms, t: row.t, causes: [],
                                                duration_ms: 0, vmin: null, vmax: null,
                                                vavg: null, bound: null };
            ev.causes.push({ cause: row.cause, channel: row.channel });
            ev.duration_ms = Math.max(ev.duration_ms, row.duration_ms || 0);
            if (row.vmin != null && (ev.vmin == null || row.vmin < ev.vmin)) ev.vmin = row.vmin;
            if (row.vmax != null && (ev.vmax == null || row.vmax > ev.vmax)) ev.vmax = row.vmax;
            if (ev.vavg == null) { ev.vavg = row.vavg; ev.bound = row.bound; }
            byTs.set(row.ts_ms, ev);
        });
        const events = this._pqEvents = [...byTs.values()].sort((a, b) => b.ts_ms - a.ts_ms);
        if (!events.length) {
            list.innerHTML = `<div class="panel-hint">${this.t('pq.noEvents', 'No PQ events in this range — a quiet grid.')}</div>`;
            this._pqOverlay(this.t('pq.noEvents', 'No PQ events in this range — a quiet grid.'));
            return;
        }
        let html = '', lastDay = '';
        events.forEach(ev => {
            const d = new Date(ev.ts_ms);
            const day = d.toLocaleDateString(undefined, { weekday: 'short', day: '2-digit', month: 'short' });
            if (day !== lastDay) {
                html += `<div class="monitor-category-header" style="cursor:default;padding:6px 10px 2px;color:var(--text-tertiary);font-size:11px;text-transform:uppercase;letter-spacing:.4px;">${this._esc(day)}</div>`;
                lastDay = day;
            }
            const sev = this._pqEventSeverity(ev);
            const causes = [...new Set(ev.causes.map(c => this._pqCauseLabel(c.cause)))].join(', ');
            const chans = [...new Set(ev.causes.map(c => c.channel).filter(ch => ch !== 'all'))].join(' ');
            const sel = this._pqSelected === ev.ts_ms ? ' selected' : '';
            const time = d.toLocaleTimeString(undefined, { hour12: false }) + '.' + String(ev.ts_ms % 1000).padStart(3, '0');
            html += `<div class="monitor-item pq-event${sel}" role="button" tabindex="0" data-key-enter
                        style="display:block;padding:6px 10px;"
                        ${this._act('selectPqEvent', [ev.ts_ms, ev.causes[0]?.cause || '', ev.causes[0]?.channel || ''])}>
                <div style="display:flex;align-items:center;gap:7px;">
                    <span style="width:8px;height:8px;border-radius:50%;background:${sev.color};flex:none;"></span>
                    <b style="font-variant-numeric:tabular-nums;">${this._esc(time)}</b>
                    <span style="margin-left:auto;color:var(--text-secondary);font-size:11.5px;">${this._pqDur(ev.duration_ms)}</span>
                </div>
                <div style="font-size:11.5px;color:var(--text-secondary);padding-left:15px;">
                    ${causes}${chans ? ` · ${this._esc(chans)}` : ''}${ev.vmin != null ? ` · min ${Number(ev.vmin).toFixed(1)}` : ''}
                </div>
            </div>`;
        });
        list.innerHTML = html;
        // First open: auto-select the newest event so the pane is never empty.
        if (autoselect && !this._pqSelected && events.length) {
            const ev = events[0];
            this.selectPqEvent(ev.ts_ms, ev.causes[0]?.cause || '', ev.causes[0]?.channel || '');
        }
    },

    _pqDur(ms) {
        if (ms == null) return '';
        if (ms < 1000) return `${ms.toFixed(0)} ms`;
        if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
        return `${(ms / 60000).toFixed(1)} min`;
    },

    _pqCauseLabel(cause) {
        const map = {
            over_voltage_ln: this.t('pq.cause.ovLn', 'Overvoltage L-N'),
            under_voltage_ln: this.t('pq.cause.uvLn', 'Undervoltage L-N'),
            voltage_outage_ln: this.t('pq.cause.outLn', 'Outage L-N'),
            over_current: this.t('pq.cause.oc', 'Overcurrent'),
            over_voltage_ll: this.t('pq.cause.ovLl', 'Overvoltage L-L'),
            under_voltage_ll: this.t('pq.cause.uvLl', 'Undervoltage L-L'),
            voltage_outage_ll: this.t('pq.cause.outLl', 'Outage L-L'),
            over_frequency: this.t('pq.cause.of', 'Overfrequency'),
            under_frequency: this.t('pq.cause.uf', 'Underfrequency'),
            dt_frequency: this.t('pq.cause.dtf', 'df/dt'),
            rapid_voltage_change_ln: this.t('pq.cause.rvcLn', 'RVC L-N'),
            rapid_voltage_change_ll: this.t('pq.cause.rvcLl', 'RVC L-L'),
            rapid_voltage_change_multi: this.t('pq.cause.rvcM', 'RVC multi'),
        };
        return map[cause] || this._esc(cause);
    },

    _renderPqDetail(ev) {
        const el = document.getElementById('pqDetail');
        if (!el || !ev) { if (el) el.innerHTML = ''; return; }
        const badges = ev.causes.map(c => {
            const sev = this._pqSeverity(c.cause);
            const ch = c.channel !== 'all' ? ` ${this._esc(c.channel)}` : '';
            return `<span style="display:inline-flex;align-items:center;gap:5px;border:1px solid ${sev.color};
                        color:${sev.color};border-radius:10px;padding:1px 8px;font-size:11.5px;white-space:nowrap;">
                <span style="width:6px;height:6px;border-radius:50%;background:${sev.color};"></span>
                ${this._pqCauseLabel(c.cause)}${ch}</span>`;
        }).join(' ');
        const num = (v, dec = 1) => v == null ? '—' : Number(v).toFixed(dec);
        const stat = (label, val) => `<span style="color:var(--text-secondary);">${label}</span> <b style="font-variant-numeric:tabular-nums;">${val}</b>`;
        el.innerHTML = `
            <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
                <b style="font-size:13px;">${new Date(ev.ts_ms).toLocaleString()}</b>
                ${badges}
            </div>
            <div style="display:flex;flex-wrap:wrap;gap:14px;margin-top:4px;font-size:12px;">
                ${stat(this.t('pq.duration', 'duration'), this._pqDur(ev.duration_ms))}
                ${stat('min', num(ev.vmin))}
                ${stat('max', num(ev.vmax))}
                ${stat('avg', num(ev.vavg))}
                ${stat(this.t('pq.bound', 'trigger bound'), num(ev.bound))}
            </div>`;
    },

    selectPqEvent(tsMs, cause, channel) {
        this._pqSelected = Number(tsMs);
        // Preselect the trace the event's first cause implicates.
        const chSel = document.getElementById('pqChannel');
        if (chSel && channel && channel !== 'all') {
            const guess = (cause === 'over_current' ? 'I' : 'U') + channel;
            if ([...chSel.options].some(o => o.value === guess)) chSel.value = guess;
        }
        // repaint the selection highlight without refetching
        document.querySelectorAll('#pqEventList .pq-event').forEach(el => {
            const args = JSON.parse(el.dataset.args || '[]');
            el.classList.toggle('selected', Number(args[0]) === this._pqSelected);
        });
        this._renderPqDetail(this._pqEvents.find(e => e.ts_ms === this._pqSelected));
        this._loadPqWaveform();
    },

    async _loadPqWaveform() {
        const ch = document.getElementById('pqChannel')?.value || 'UL1';
        const ts = this._pqSelected;
        if (!ts) return;
        const lbl = document.getElementById('pqEventLabel');
        if (lbl) lbl.textContent = '';
        this._pqOverlay(this.t('pq.loadingWf', 'Loading waveform…'));
        const seq = (this._pqWfSeq = (this._pqWfSeq || 0) + 1);
        let payload = null;
        try {
            const r = await fetch(`/api/pq/waveform?event=${ts}&channel=${encodeURIComponent(ch)}${this._viewDeviceQS('&')}`);
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            payload = await r.json();
        } catch (e) {
            if (seq === this._pqWfSeq)
                this._pqOverlay(`${this.t('pq.wfError', 'Waveform load failed')}: ${e}`);
            return;
        }
        if (seq !== this._pqWfSeq) return;
        const series = payload.series || [];
        if (!series.length) {
            this._pqOverlay(this.t('pq.noWaveform',
                'No waveform available — the capture window has aged out of the meter and was never archived. New events are archived automatically.'));
            return;
        }
        this._pqOverlay('');
        const unit = ch.startsWith('I') ? 'A' : 'V';
        const color = (this._histColors?.() || ['#12a3b2'])[0];
        this._renderHistory(null, {
            canvas: document.getElementById('pqCanvas'),
            legendId: 'pqLegend',
            series: [{
                name: ch, label: `${ch} (${unit})`, unit, color,
                mean: series.map(p => ({ t: p.t, v: p.v })),
            }],
        });
        const src = payload.source === 'device'
            ? ` · ${this.t('pq.fromMeter', 'fetched live from the meter (now archived)')}` : '';
        const info = document.getElementById('pqInfo');
        if (info) info.textContent =
            `${series.length} ${this.t('pq.samples', 'samples')} · 10 ms RMS · ${ch}${src}`;
    },
});
