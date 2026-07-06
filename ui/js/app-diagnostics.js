// diagnostics domain (frame-level bus monitor) — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    _stopDiagPoll() {
        if (this._diagTimer) { clearInterval(this._diagTimer); this._diagTimer = null; }
    },

    initDiagnosticsPage() {
        this._diagRows = this._diagRows || [];       // newest first, capped
        this._diagLastSeq = this._diagLastSeq || 0;
        this._diagPaused = false;
        this._diagFilter = this._diagFilter || '';
        this._diagWired = this._diagWired || this._wireDiagControls();
        this._probeWired = this._probeWired || this._wireProbe();
        this._ssWired = this._ssWired || this._wireSunspec();
        this._loadDiagDevices();
        this._pollBusTrace(true);
        this._stopDiagPoll();
        this._diagTimer = setInterval(() => {
            if (this.currentPage === 'diagnostics' && !document.hidden && !this._diagPaused) {
                this._pollBusTrace();
            }
        }, 1000);
    },

    _wireDiagControls() {
        document.getElementById('diagToggleBtn')?.addEventListener('click', () => this._toggleBusTrace());
        document.getElementById('diagClearBtn')?.addEventListener('click', () => this._clearBusTrace());
        document.getElementById('diagPauseBtn')?.addEventListener('click', (e) => {
            this._diagPaused = !this._diagPaused;
            e.currentTarget.classList.toggle('active', this._diagPaused);
            e.currentTarget.setAttribute('aria-pressed', this._diagPaused ? 'true' : 'false');
            if (!this._diagPaused) this._pollBusTrace();
        });
        document.getElementById('diagDeviceFilter')?.addEventListener('change', (e) => {
            this._diagFilter = e.target.value;
            this._renderDiagTable();
        });
        // expand/collapse hex detail (delegated — rows are re-rendered)
        document.getElementById('diagTableBody')?.addEventListener('click', (e) => {
            const tr = e.target.closest('tr[data-seq]');
            if (!tr) return;
            const open = tr.nextElementSibling?.classList.contains('diag-detail');
            document.querySelectorAll('#diagTableBody .diag-detail').forEach(d => d.remove());
            document.querySelectorAll('#diagTableBody tr.expanded').forEach(d => d.classList.remove('expanded'));
            if (open) return;
            const row = this._diagRows.find(r => r.seq === Number(tr.dataset.seq));
            if (!row) return;
            tr.classList.add('expanded');
            tr.insertAdjacentHTML('afterend', this._diagDetailHtml(row));
        });
        return true;
    },

    async _loadDiagDevices() {
        try {
            const r = await fetch('/api/devices');
            const devs = ((await r.json()).devices || [])
                .filter(d => (d.protocol || 'tcp') === 'tcp' || d.protocol === 'rtu');
            const opts = devs.map(d =>
                `<option value="${this._esc(d.id)}">${this._esc(d.name || d.id)}</option>`).join('');
            const sel = document.getElementById('diagDeviceFilter');
            if (sel) {
                const cur = sel.value;
                sel.innerHTML = `<option value="">${this._esc(this.t('diag.allDevices', 'All devices'))}</option>` + opts;
                sel.value = cur;
            }
            const psel = document.getElementById('probeDevice');
            if (psel) {
                const cur = psel.value;
                psel.innerHTML = opts;
                if (cur) psel.value = cur;
            }
        } catch (_) { /* filter stays generic */ }
    },

    // ── register probe (endianness workbench) ────────────────────────────

    _wireProbe() {
        document.getElementById('probeBtn')?.addEventListener('click', () => this._runProbe());
        document.getElementById('probeAddr')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') this._runProbe();
        });
        return true;
    },

    async _runProbe() {
        const host = document.getElementById('probeResult');
        const btn = document.getElementById('probeBtn');
        const addr = document.getElementById('probeAddr')?.value;
        if (addr === '' || addr == null) {
            host.innerHTML = `<div class="field-hint">${this._esc(this.t('probe.needAddr', 'Enter a register address.'))}</div>`;
            return;
        }
        const body = {
            device: document.getElementById('probeDevice')?.value,
            address: Number(addr),
            register_type: document.getElementById('probeType')?.value || 'holding',
            count: Number(document.getElementById('probeCount')?.value || 2),
        };
        btn.disabled = true;
        host.innerHTML = `<div class="field-hint">${this._esc(this.t('probe.reading', 'Reading… (a failed read retries a few times before giving up)'))}</div>`;
        try {
            const r = await fetch('/api/diagnostics/probe', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await r.json();
            if (!r.ok) throw new Error(data.detail || r.statusText);
            host.innerHTML = this._probeResultHtml(data);
        } catch (e) {
            host.innerHTML = `<div class="diag-exc"><i class="bi bi-exclamation-triangle"></i> ${this._esc(String(e.message || e))}</div>`;
        } finally {
            btn.disabled = false;
        }
    },

    _probeResultHtml(d) {
        if (!d.ok) {
            return `<div class="diag-exc"><i class="bi bi-exclamation-triangle"></i> ${this._esc(this.t('probe.noResponse', 'No response — check the bus monitor above for the frame (exception code, timeout).'))}</div>`;
        }
        if (d.bits) {
            const bits = d.bits.map((b, i) =>
                `<span class="badge ${b ? 'badge-success' : 'badge-secondary'}">${d.address + i}: ${b ? 'ON' : 'OFF'}</span>`).join(' ');
            return `<div class="probe-bits">${bits}</div>`;
        }
        const rawRow = d.hex.map(h => `<span class="diag-seg diag-seg-pdu">${this._esc(h.toUpperCase())}</span>`).join('');
        const orders = ['abcd', 'cdab', 'badc', 'dcba'];
        const fmt = (v) => {
            if (v == null) return '—';
            if (typeof v === 'string') return v;                      // NaN/inf
            if (typeof v === 'number' && !Number.isInteger(v)) {
                const a = Math.abs(v);
                return (a !== 0 && (a >= 1e9 || a < 1e-4)) ? v.toExponential(4) : String(Math.round(v * 1e6) / 1e6);
            }
            return String(v);
        };
        const rows = d.interpretations.map(it => `<tr>
            <td class="mono">${this._esc(it.type)}</td>
            ${orders.map(o => `<td class="mono">${this._esc(fmt(it.orders[o]))}</td>`).join('')}
        </tr>`).join('');
        return `
            <div class="probe-raw mono">
                <span class="diag-dir">${this._esc(this.t('probe.raw', 'Raw'))}</span>${rawRow}
                <span class="probe-ascii" title="ASCII">${this._esc(d.ascii)}</span>
            </div>
            <div class="diag-table-wrap">
                <table class="data-table probe-table">
                    <thead><tr>
                        <th data-i18n="probe.colType">Type</th>
                        <th class="mono">ABCD <small>big</small></th>
                        <th class="mono">CDAB <small>word swap</small></th>
                        <th class="mono">BADC <small>byte swap</small></th>
                        <th class="mono">DCBA <small>little</small></th>
                    </tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
            <p class="field-hint" data-i18n="probe.hint">The column that yields a plausible value is the device's word order — set it as byte_order in the device template.</p>`;
    },

    async _toggleBusTrace() {
        const on = document.getElementById('diagToggleBtn')?.dataset.on === '1';
        try {
            const r = await fetch('/api/bus-trace/config', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: !on }),
            });
            if (!r.ok) throw new Error(await r.text());
            this._applyDiagState(await r.json());
            this._pollBusTrace();
        } catch (e) {
            this.showToast('error', this.t('diag.title', 'Bus Monitor'),
                           this.t('diag.toggleFailed', 'Could not change capture state'));
        }
    },

    async _clearBusTrace() {
        try {
            await fetch('/api/bus-trace/config', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ clear: true }),
            });
        } catch (_) { /* clear locally regardless */ }
        this._diagRows = [];
        this._diagLastSeq = 0;
        this._renderDiagTable();
    },

    _applyDiagState(st) {
        const btn = document.getElementById('diagToggleBtn');
        if (btn) {
            btn.dataset.on = st.enabled ? '1' : '0';
            btn.classList.toggle('btn-danger', st.enabled);
            btn.classList.toggle('btn-primary', !st.enabled);
            btn.innerHTML = st.enabled
                ? `<i class="bi bi-stop-fill"></i> ${this._esc(this.t('diag.stop', 'Stop capture'))}`
                : `<i class="bi bi-record-fill"></i> ${this._esc(this.t('diag.start', 'Start capture'))}`;
        }
        const stats = document.getElementById('diagStats');
        if (stats) {
            stats.textContent = st.enabled
                ? `${this.t('diag.capturing', 'Capturing')} — ${st.captured_total} ${this.t('diag.frames', 'transactions')}`
                : `${this.t('diag.idle', 'Capture off')} — ${st.captured_total} ${this.t('diag.frames', 'transactions')}`;
        }
    },

    async _pollBusTrace(reset) {
        try {
            const r = await fetch(`/api/bus-trace?after=${reset ? 0 : this._diagLastSeq}&limit=500`);
            if (!r.ok) return;
            const data = await r.json();
            this._applyDiagState(data);
            if (reset) { this._diagRows = []; }
            for (const e of data.entries) {
                if (e.seq > this._diagLastSeq) this._diagLastSeq = e.seq;
                this._diagRows.unshift(e);
            }
            if (this._diagRows.length > 600) this._diagRows.length = 600;
            if (data.entries.length || reset) this._renderDiagTable();
        } catch (_) { /* transient — next tick retries */ }
    },

    _diagResultBadge(res, exc) {
        const map = {
            ok: ['badge-success', this.t('diag.ok', 'OK')],
            exception: ['badge-warning', `${this.t('diag.exception', 'Exception')} ${exc ?? ''}`],
            no_response: ['badge-danger', this.t('diag.noResponse', 'No response')],
            crc_error: ['badge-danger', 'CRC'],
            mismatch: ['badge-danger', this.t('diag.mismatch', 'Mismatch')],
            malformed: ['badge-danger', this.t('diag.malformed', 'Malformed')],
        };
        const [cls, label] = map[res] || ['badge-secondary', res];
        return `<span class="badge ${cls}">${this._esc(label)}</span>`;
    },

    _renderDiagTable() {
        const body = document.getElementById('diagTableBody');
        if (!body) return;
        const rows = this._diagFilter
            ? this._diagRows.filter(r => r.device === this._diagFilter)
            : this._diagRows;
        if (!rows.length) {
            body.innerHTML = `<tr><td colspan="8" class="diag-empty">${this._esc(
                this.t('diag.empty', 'No transactions captured. Start the capture and the polled traffic appears here — each retry as its own frame.'))}</td></tr>`;
            return;
        }
        body.innerHTML = rows.map(r => {
            const t = new Date(r.ts * 1000);
            const hh = t.toTimeString().slice(0, 8) + '.' + String(t.getMilliseconds()).padStart(3, '0');
            const req = r.addr != null
                ? `${r.addr}${r.count != null ? ' ×' + r.count : ''}${r.value != null ? ' = ' + r.value : ''}`
                : '—';
            return `<tr data-seq="${r.seq}" title="${this._esc(this.t('diag.rowTip', 'Click for the raw frames'))}">
                <td class="mono">${hh}</td>
                <td>${this._esc(r.device)}</td>
                <td class="mono">${r.unit ?? '—'}</td>
                <td><span class="diag-fc mono" title="${this._esc(r.fc_name || '')}">${r.fc ?? '—'}</span></td>
                <td class="mono">${req}</td>
                <td>${this._diagResultBadge(r.result, r.exc)}</td>
                <td class="mono">${r.latency_ms != null ? r.latency_ms.toFixed(1) : '—'}</td>
                <td class="mono diag-proto">${r.proto}</td>
            </tr>`;
        }).join('');
    },

    _diagHexFmt(hex, proto, isTx) {
        if (!hex) return `<span class="diag-hex-none">${this._esc(this.t('diag.noBytes', '(no bytes)'))}</span>`;
        const bytes = hex.toUpperCase().match(/../g) || [];
        const seg = (arr, cls, tip) =>
            arr.length ? `<span class="diag-seg ${cls}" title="${this._esc(tip)}">${arr.join(' ')}</span>` : '';
        if (proto === 'rtu') {
            return seg(bytes.slice(0, 1), 'diag-seg-unit', 'Unit ID')
                 + seg(bytes.slice(1, -2), 'diag-seg-pdu', 'PDU')
                 + seg(bytes.slice(-2), 'diag-seg-crc', 'CRC-16');
        }
        return seg(bytes.slice(0, 7), 'diag-seg-mbap', 'MBAP header (TID·PID·LEN·Unit)')
             + seg(bytes.slice(7), 'diag-seg-pdu', 'PDU');
    },

    _diagDetailHtml(r) {
        const excLine = r.exc != null
            ? `<div class="diag-exc"><i class="bi bi-exclamation-triangle"></i> ${this._esc(r.exc_name || '')} (${r.exc})</div>` : '';
        return `<tr class="diag-detail"><td colspan="8">
            ${excLine}
            <div class="diag-frame"><span class="diag-dir">TX →</span>${this._diagHexFmt(r.tx, r.proto, true)}</div>
            <div class="diag-frame"><span class="diag-dir">RX ←</span>${this._diagHexFmt(r.rx, r.proto, false)}</div>
            <div class="diag-meta">${this._esc(r.fc_name || '')}${r.tid != null ? ` · TID ${r.tid}` : ''}${r.latency_ms != null ? ` · ${r.latency_ms.toFixed(1)} ms` : ''}</div>
        </td></tr>`;
    },

    // ── SunSpec model walk ────────────────────────────────────────────────

    _wireSunspec() {
        document.getElementById('ssBtn')?.addEventListener('click', () => this._runSunspec());
        document.getElementById('ssHost')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') this._runSunspec();
        });
        return true;
    },

    async _runSunspec() {
        const host = document.getElementById('ssHost')?.value?.trim();
        const out = document.getElementById('ssResult');
        const btn = document.getElementById('ssBtn');
        if (!host) {
            out.innerHTML = `<div class="field-hint">${this._esc(this.t('ss.needHost', 'Enter the device IP or hostname.'))}</div>`;
            return;
        }
        btn.disabled = true;
        out.innerHTML = `<div class="field-hint">${this._esc(this.t('ss.scanning', 'Walking the model chain…'))}</div>`;
        try {
            const r = await fetch('/api/discover/sunspec', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    host,
                    port: Number(document.getElementById('ssPort')?.value || 502),
                    unit_id: Number(document.getElementById('ssUnit')?.value || 1),
                }),
            });
            const data = await r.json();
            if (!r.ok) throw new Error((data.detail?.errors || [r.statusText]).join('; '));
            out.innerHTML = this._ssResultHtml(data);
        } catch (e) {
            out.innerHTML = `<div class="diag-exc"><i class="bi bi-exclamation-triangle"></i> ${this._esc(String(e.message || e))}</div>`;
        } finally {
            btn.disabled = false;
        }
    },

    _ssResultHtml(d) {
        if (!d.ok) {
            return `<div class="diag-exc"><i class="bi bi-exclamation-triangle"></i> ${this._esc(d.error || 'scan failed')}</div>`;
        }
        const id = d.identity;
        const idCard = id ? `
            <div class="ss-identity">
                <span class="badge badge-success">SunSpec @ ${d.base}</span>
                <b>${this._esc(id.manufacturer || '?')}</b> ${this._esc(id.model || '')}
                ${id.version ? `<span class="ss-dim">fw ${this._esc(id.version)}</span>` : ''}
                ${id.serial ? `<span class="ss-dim">SN ${this._esc(id.serial)}</span>` : ''}
            </div>` : `
            <div class="ss-identity"><span class="badge badge-success">SunSpec @ ${d.base}</span>
                <span class="ss-dim">${this._esc(this.t('ss.noIdentity', '(no Common model — identity unavailable)'))}</span></div>`;
        const rows = (d.models || []).map(m => `<tr>
            <td class="mono">${m.id}</td>
            <td>${this._esc(m.name)}</td>
            <td class="mono">${m.addr}</td>
            <td class="mono">${m.length}</td>
        </tr>`).join('');
        return `${idCard}
            <div class="diag-table-wrap">
                <table class="data-table probe-table">
                    <thead><tr>
                        <th>ID</th>
                        <th data-i18n="ss.colModel">Model</th>
                        <th data-i18n="ss.colAddr">Address</th>
                        <th data-i18n="ss.colLen">Length</th>
                    </tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
            <p class="field-hint" data-i18n="ss.hint">Model addresses are live from the device — use them when building a template (the register block of a model starts at its address + 2).</p>`;
    },
});
