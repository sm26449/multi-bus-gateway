/* Multi-Bus Gateway — Power Quality section (Jasic PQ recorder archive).
 *
 * Reached embedded in the device workspace (data-dtab="pq"), like Monitor/
 * History. Left pane: archived PQ events from InfluxDB (/api/pq/events —
 * the full retained history, not just the meter's 32-entry ring). Right
 * pane: the archived half-wave-RMS trace of the selected event
 * (/api/pq/waveform), drawn with the shared _renderHistory canvas renderer.
 */
Object.assign(JanitzaMonitor.prototype, {

    initPowerQualityPage() {
        this._pqWired = this._pqWired || this._wirePqControls();
        this._pqSelected = null;
        this._loadPqEvents();
        this._loadPqStatus();
    },

    _wirePqControls() {
        document.getElementById('pqRange')?.addEventListener('change', () => this._loadPqEvents());
        document.getElementById('pqRefresh')?.addEventListener('click', () => {
            this._loadPqEvents(); this._loadPqStatus();
        });
        document.getElementById('pqChannel')?.addEventListener('change', () => {
            if (this._pqSelected) this._loadPqWaveform();
        });
        return true;
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
                    'PQ recorder is not enabled for this device — enable it in the device config (pq_recorder).');
                return;
            }
            const c = dev.counters || {};
            const err = dev.last_error
                ? ` · ${this.t('pq.lastError', 'last error')}: ${dev.last_error}` : '';
            el.textContent = `${this.t('pq.counters', 'Lifetime counters')}: ` +
                `${c.events ?? '—'} ${this.t('pq.eventsWord', 'events')} · ` +
                `${c.transients ?? '—'} ${this.t('pq.transients', 'transients')}` + err;
        } catch (e) { /* status line is best-effort */ }
    },

    async _loadPqEvents() {
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

        // One point per cause+channel — fold rows sharing the same event
        // timestamp into one visual event with all its causes.
        const byTs = new Map();
        rows.forEach(row => {
            const ev = byTs.get(row.ts_ms) || { ts_ms: row.ts_ms, t: row.t, causes: [],
                                                duration_ms: 0, vmin: null };
            ev.causes.push({ cause: row.cause, channel: row.channel });
            ev.duration_ms = Math.max(ev.duration_ms, row.duration_ms || 0);
            if (row.vmin != null && (ev.vmin == null || row.vmin < ev.vmin)) ev.vmin = row.vmin;
            byTs.set(row.ts_ms, ev);
        });
        const events = [...byTs.values()].sort((a, b) => b.ts_ms - a.ts_ms);
        if (!events.length) {
            list.innerHTML = `<div class="panel-hint">${this.t('pq.noEvents', 'No PQ events in this range.')}</div>`;
            return;
        }
        list.innerHTML = events.map(ev => {
            const when = new Date(ev.ts_ms).toLocaleString();
            const causes = ev.causes.map(c => `${this._pqCauseLabel(c.cause)} ${this._esc(c.channel)}`).join(', ');
            const sel = this._pqSelected === ev.ts_ms ? ' selected' : '';
            return `<div class="monitor-item pq-event${sel}" role="button" tabindex="0" style="display:block;padding:6px 10px;"
                        ${this._act('selectPqEvent', [ev.ts_ms, ev.causes[0]?.cause || '', ev.causes[0]?.channel || ''])}>
                <div style="display:flex;justify-content:space-between;gap:8px;">
                    <b>${this._esc(when)}</b>
                    <span style="color:var(--text-secondary);">${(ev.duration_ms || 0).toFixed(0)} ms</span>
                </div>
                <div style="font-size:11.5px;color:var(--text-secondary);">
                    ${causes}${ev.vmin != null ? ` · min ${Number(ev.vmin).toFixed(1)}` : ''}
                </div>
            </div>`;
        }).join('');
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

    selectPqEvent(tsMs, cause, channel) {
        this._pqSelected = Number(tsMs);
        // Preselect the trace the event's first cause implicates.
        const chSel = document.getElementById('pqChannel');
        if (chSel && channel && channel !== 'all') {
            const guess = (cause === 'over_current' ? 'I' : 'U') + channel;
            if ([...chSel.options].some(o => o.value === guess)) chSel.value = guess;
        }
        this._loadPqEvents();                          // repaint selection highlight
        this._loadPqWaveform();
    },

    async _loadPqWaveform() {
        const label = document.getElementById('pqEventLabel');
        const info = document.getElementById('pqInfo');
        const ch = document.getElementById('pqChannel')?.value || 'UL1';
        const ts = this._pqSelected;
        if (!ts) return;
        if (label) label.textContent = new Date(ts).toLocaleString();
        const seq = (this._pqWfSeq = (this._pqWfSeq || 0) + 1);
        let series = [];
        try {
            const r = await fetch(`/api/pq/waveform?event=${ts}&channel=${encodeURIComponent(ch)}${this._viewDeviceQS('&')}`);
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            series = (await r.json()).series || [];
        } catch (e) {
            if (seq === this._pqWfSeq && info)
                info.textContent = `${this.t('pq.wfError', 'Waveform load failed')}: ${e}`;
            return;
        }
        if (seq !== this._pqWfSeq) return;
        if (!series.length) {
            const canvas = document.getElementById('pqCanvas');
            if (canvas) this._clearCanvas?.(canvas);
            if (info) info.textContent = this.t('pq.noWaveform',
                'No archived waveform for this event/channel (only channels implicated in an event are archived, and only for events after the recorder was enabled).');
            return;
        }
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
        if (info) info.textContent =
            `${series.length} ${this.t('pq.samples', 'samples')} · 10 ms RMS · ${ch}`;
    },
});
