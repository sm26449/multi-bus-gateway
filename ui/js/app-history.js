// history domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    // ── History view (InfluxDB read-back) ─────────────────────────────────
    _histColors() {
        return ['#2f81f7', '#e0a000', '#3fb950', '#db61a2', '#f85149', '#1f9c9c', '#a371f7', '#d29922'];
    },

    // Cap the bucket resolution on long ranges so we never pull ~tens of
    // thousands of points/series (keeps the payload + canvas sane).
    _effectiveEvery(range, sel) {
        const toMin = (s) => { const m = String(s).match(/^(\d+)([mh])$/); return m ? (m[2] === 'h' ? +m[1] * 60 : +m[1]) : 5; };
        const floors = { '-7d': 15, '-30d': 60 };   // minutes
        const eff = Math.max(toMin(sel), floors[range] || 0);
        return (eff >= 60 && eff % 60 === 0) ? `${eff / 60}h` : `${eff}m`;
    },

    _histCategory(reg) {
        if (reg.calculated) return 'calculated';
        const u = (reg.unit || '').toLowerCase();
        if (u === 'v') return 'voltage';
        if (u === 'a') return 'current';
        if (u === 'w' || u === 'kw') return 'power';
        if (u === 'wh' || u === 'kwh') return 'energy';
        if (u === 'hz') return 'frequency';
        if (u === 'var' || u === 'kvar') return 'reactive';
        if (u === 'va' || u === 'kva') return 'apparent';
        if (u === '°c' || u === 'c') return 'temperature';
        if (u === '%') return 'percentage';
        return 'other';
    },

    // ── Energy view (monthly totals + daily import/export, from InfluxDB) ──
    initEnergyPage() {
        const inp = document.getElementById('energyMonth');
        if (!inp) return;
        if (!inp._wired) {
            inp._wired = true;
            if (!inp.value) { const d = new Date(); inp.value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`; }
            inp.addEventListener('change', () => this.loadEnergy());
            document.getElementById('energyRefresh')?.addEventListener('click', () => this.loadEnergy());
        }
        this._renderViewDeviceSelector('energyViewDevice', () => this.loadEnergy());
        this.loadEnergy();
    },

    async loadEnergy() {
        const inp = document.getElementById('energyMonth');
        const info = document.getElementById('energyInfo');
        const totals = document.getElementById('energyTotals');
        const canvas = document.getElementById('energyCanvas');
        if (!inp || !inp.value) return;
        const [y, m] = inp.value.split('-').map(Number);
        const py = m === 1 ? y - 1 : y, pm = m === 1 ? 12 : m - 1;
        if (info) info.textContent = this.t('common.loading');
        try {
            const dq = this._viewDeviceQS('&');
            const [r, rp] = await Promise.all([
                fetch(`/api/energy/monthly?year=${y}&month=${m}${dq}`),
                fetch(`/api/energy/monthly?year=${py}&month=${pm}${dq}`).then(x => x).catch(() => null),
            ]);
            if (!r.ok) {
                const d = await r.json().catch(() => ({}));
                const msg = r.status === 503
                    ? this.t('energy.notConfigured')
                    : (d.detail || `HTTP ${r.status}`);
                if (info) info.textContent = msg;
                if (totals) totals.innerHTML = '';
                this._clearCanvas(canvas);
                return;
            }
            const d = await r.json();
            const prev = {};
            if (rp && rp.ok) { (await rp.json()).totals?.forEach(t => { prev[t.name] = t.delta; }); }
            const EPAL = ['#e0a000', '#3fb950', '#a371f7', '#12a3b2', '#f7768e', '#ff9f0a'];
            if (totals) totals.innerHTML = (d.totals || []).map((t, i) => {
                const pv = prev[t.name];
                let trend = '<div style="font-size:11.5px;color:var(--text-secondary);margin-top:3px;">&nbsp;</div>';
                if (t.delta != null && pv != null) {
                    if (pv !== 0) {
                        const pct = (t.delta - pv) / Math.abs(pv) * 100;
                        const up = pct >= 0, col = up ? 'var(--success-text)' : 'var(--danger-text)';
                        trend = `<div style="font-size:11.5px;color:${col};margin-top:3px;">${up ? '▲' : '▼'} ${Math.abs(pct).toFixed(1)}% <span style="color:var(--text-secondary);">${this._esc(this.t('energy.vsPrev'))}</span></div>`;
                    } else {
                        trend = `<div style="font-size:11.5px;color:var(--text-secondary);margin-top:3px;">${this._esc(this.t('energy.vsPrev'))}</div>`;
                    }
                }
                return `<div class="settings-card" style="padding:14px 18px;min-width:160px;">
                    <div style="color:var(--text-secondary);font-size:12px;"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${EPAL[i % EPAL.length]};margin-right:5px;"></span>${this._esc(t.label)}</div>
                    <div style="font-size:24px;font-weight:700;line-height:1.2;color:var(--text-primary);">${t.delta == null ? '—' : Number(t.delta).toLocaleString()}</div>
                    <div style="color:var(--text-secondary);font-size:12px;">${this._esc(t.unit)}</div>${trend}
                </div>`;
            }).join('');
            if (info) info.textContent = new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
            this.drawEnergyBars(canvas, d);
        } catch (e) { if (info) info.textContent = 'Query failed'; }
    },

    // ── Energy field picker: which cumulative counters to total, per device,
    //    in what order, with editable display labels ──────────────────────────
    async openEnergyFields() {
        const list = document.getElementById('energyFieldsList');
        const fb = document.getElementById('energyFieldsFeedback');
        if (fb) { fb.textContent = ''; fb.className = 'save-feedback'; }
        list.innerHTML = `<span class="field-hint">${this.t('common.loading', 'Loading…')}</span>`;
        this.openModal('energyFieldsModal');
        try {
            const r = await (await fetch('/api/energy/fields' + this._viewDeviceQS())).json();
            const cands = r.candidates || [];
            if (!cands.length) {
                this._efRows = [];
                list.innerHTML = `<span class="field-hint">${this.t('energy.noFields', 'No cumulative energy counters (Wh / varh / VAh) in this device’s selected measurements. Add some in Measurements first.')}</span>`;
                return;
            }
            const byName = {}; cands.forEach(c => { byName[c.name] = c; });
            const sel = r.selected || [];
            const noneYet = sel.length === 0;                   // default: track all, source order
            const rows = [], seen = new Set();
            sel.forEach(f => { const c = byName[f.name]; if (c && !seen.has(f.name)) {   // saved first, saved order + labels
                rows.push({ ...c, label: f.label || c.label, checked: true }); seen.add(f.name); } });
            cands.forEach(c => { if (!seen.has(c.name)) rows.push({ ...c, checked: noneYet }); });
            this._efRows = rows;
            this._renderEnergyRows();
        } catch (e) {
            list.innerHTML = `<span class="field-error">${this._esc(e.message)}</span>`;
        }
    },

    _renderEnergyRows() {
        const list = document.getElementById('energyFieldsList');
        const rows = this._efRows || [];
        list.innerHTML = rows.map((c, i) => `
            <div class="ef-row" data-efi="${i}">
                <input type="checkbox" class="ef-check" ${c.checked ? 'checked' : ''} aria-label="Track ${this._esc(c.label)}">
                <input type="text" class="ef-label input" value="${this._esc(c.label)}" placeholder="${this._esc(c.name)}" aria-label="Label">
                <span class="ef-meta">${this._esc(c.name)} · ${this._esc(c.unit)}</span>
                <span class="ef-move">
                    <button type="button" class="ef-btn" title="${this.t('common.moveUp', 'Move up')}" ${i === 0 ? 'disabled' : ''} ${this._act('energyMoveField', [i, -1])}><i class="bi bi-chevron-up"></i></button>
                    <button type="button" class="ef-btn" title="${this.t('common.moveDown', 'Move down')}" ${i === rows.length - 1 ? 'disabled' : ''} ${this._act('energyMoveField', [i, 1])}><i class="bi bi-chevron-down"></i></button>
                </span>
            </div>`).join('');
    },

    _syncEnergyRows() {   // read the live checkbox + label edits back into _efRows
        document.querySelectorAll('#energyFieldsList .ef-row').forEach(el => {
            const row = this._efRows[+el.dataset.efi];
            if (!row) return;
            row.checked = el.querySelector('.ef-check')?.checked ?? row.checked;
            const lbl = el.querySelector('.ef-label')?.value.trim();
            row.label = lbl || row.name;
        });
    },

    energyMoveField(i, dir) {
        this._syncEnergyRows();
        const j = i + dir;
        if (j < 0 || j >= this._efRows.length) return;
        [this._efRows[i], this._efRows[j]] = [this._efRows[j], this._efRows[i]];
        this._renderEnergyRows();
    },

    async saveEnergyFields() {
        this._syncEnergyRows();
        const fb = document.getElementById('energyFieldsFeedback');
        const picked = (this._efRows || []).filter(r => r.checked)
            .map(r => ({ name: r.name, label: r.label, unit: r.unit, div: r.div }));
        try {
            const r = await fetch('/api/energy/fields' + this._viewDeviceQS(), {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ fields: picked }),
            });
            if (!r.ok) {
                const d = await r.json().catch(() => ({}));
                throw new Error((d.detail?.errors || [d.detail || 'save failed']).join('; '));
            }
            this.closeModal('energyFieldsModal');
            this.showToast('success', this.t('energy.fields', 'Fields'),
                           this.t('energy.fieldsSaved', 'Energy fields saved'));
            this.loadEnergy();
        } catch (e) {
            if (fb) { fb.textContent = e.message; fb.className = 'save-feedback err'; }
        }
    },

    drawEnergyBars(canvas, d) {
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const W = canvas.clientWidth || 800, H = canvas.clientHeight || 340, dpr = window.devicePixelRatio || 1;
        canvas.width = W * dpr; canvas.height = H * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
        const EPAL = ['#e0a000', '#3fb950', '#a371f7', '#12a3b2', '#f7768e', '#ff9f0a'];
        const tlabel = {}; (d.totals || []).forEach(t => { tlabel[t.name] = t.label; });
        const series = (d.daily || []).map((s, i) => ({ label: s.label || tlabel[s.name] || s.name, color: EPAL[i % EPAL.length], days: s.days || [] }));
        const dateSet = new Set(); series.forEach(s => s.days.forEach(x => dateSet.add(x.date)));
        const dates = [...dateSet].sort();
        const css = getComputedStyle(document.body); const muted = (css.getPropertyValue('--text-muted') || 'var(--text-secondary)').trim() || 'var(--text-secondary)';
        ctx.fillStyle = muted; ctx.font = '12px system-ui, sans-serif';
        if (!dates.length) { ctx.fillText(this.t('energy.noDaily'), 20, 28); return; }
        const byDate = series.map(s => { const mm = {}; s.days.forEach(x => mm[x.date] = x.delta); return mm; });
        let vhi = 0; dates.forEach(dt => byDate.forEach(mm => { const v = Math.abs(mm[dt] || 0); if (v > vhi) vhi = v; }));
        if (vhi <= 0) vhi = 1;
        const ml = 52, mr = 12, mt = 22, mb = 30;
        ctx.font = '11px system-ui, sans-serif'; ctx.strokeStyle = 'rgba(128,128,128,0.18)'; ctx.textAlign = 'left';
        for (let i = 0; i <= 4; i++) { const v = vhi * i / 4, y = mt + (1 - i / 4) * (H - mt - mb); ctx.beginPath(); ctx.moveTo(ml, y); ctx.lineTo(W - mr, y); ctx.stroke(); ctx.fillStyle = muted; ctx.fillText(v < 10 ? v.toFixed(2) : v.toFixed(0), 6, y + 3); }
        const ns = Math.max(1, series.length);
        const plotW = W - ml - mr, slot = plotW / dates.length, bw = Math.max(2, Math.min(14, slot / (ns + 1)));
        dates.forEach((dt, i) => { const cx = ml + slot * i + slot / 2;
            byDate.forEach((mm, si) => { const v = Math.abs(mm[dt] || 0); const h = (v / vhi) * (H - mt - mb); const bx = cx - (ns * (bw + 1)) / 2 + si * (bw + 1);
                ctx.fillStyle = series[si].color; ctx.fillRect(bx, H - mb - h, bw, h); }); });
        ctx.fillStyle = muted; ctx.textAlign = 'center';
        const step = Math.max(1, Math.ceil(dates.length / 10));
        dates.forEach((dt, i) => { if (i % step === 0) ctx.fillText(String(Number(dt.slice(8))), ml + slot * i + slot / 2, H - 10); });
        ctx.textAlign = 'left';
        let lx = ml;   // advance by each label's measured width so legends never overlap
        series.forEach((s) => {
            if (lx > W - mr - 60) return;   // don't run off the right edge with many fields
            ctx.fillStyle = s.color; ctx.fillRect(lx, 6, 11, 11);
            ctx.fillStyle = muted; ctx.fillText(s.label, lx + 16, 15);
            lx += 16 + ctx.measureText(s.label).width + 18;
        });
    },

    _renderHistoryDisabled() {
        const l = document.getElementById('histRegList');
        const info = document.getElementById('histInfo');
        const leg = document.getElementById('histLegend');
        const canvas = document.getElementById('historyCanvas');
        if (l) l.innerHTML = '<div style="padding:22px 16px;color:var(--text-secondary);font-size:13px;line-height:1.55;">'
            + '<i class="bi bi-database-x" style="font-size:18px;"></i><br><br>'
            + `<b>${this._esc(this.t('history.notConfigured'))}</b><br>${this._esc(this.t('history.notConfiguredBody'))}</div>`;
        if (info) info.textContent = this.t('history.notConfigured');
        if (leg) leg.innerHTML = '';
        this._histSeries = null;
        if (canvas) this._clearCanvas(canvas);
    },

    initHistoryPage() {
        if (!this._histWired) {
            this._histWired = true;
            document.getElementById('histSearch')?.addEventListener('input', (e) => {
                this.histSearch = e.target.value.toLowerCase();
                this.renderHistoryRegisters();
            });
            ['histRange', 'histEvery'].forEach(id =>
                document.getElementById(id)?.addEventListener('change', () => this.loadHistory()));
            document.getElementById('histRefresh')?.addEventListener('click', () => this.loadHistory());
            document.getElementById('histClear')?.addEventListener('click', () => {
                this.histSelected = []; this.renderHistoryRegisters(); this.loadHistory();
            });
            const canvas = document.getElementById('historyCanvas');
            if (canvas) {
                canvas.addEventListener('mousemove', (e) => {
                    if (!this._histSeries) return;
                    const rect = canvas.getBoundingClientRect();
                    this._histHoverX = e.clientX - rect.left;
                    this._renderHistory(this._histHoverX);
                });
                canvas.addEventListener('mouseleave', () => {
                    this._histHoverX = null;
                    this._renderHistory(null);
                    if (this._histTip) this._histTip.style.display = 'none';
                });
            }
        }
        this._renderViewDeviceSelector('histViewDevice', () => this._reloadHistoryForDevice());
        if (!this.histRegisters || !this.histRegisters.length) {
            this._loadHistoryRegisters();
        } else {
            this.renderHistoryRegisters();
            this.loadHistory();
        }
    },

    _loadHistoryRegisters() {
        fetch('/api/history/registers' + this._viewDeviceQS()).then(r => r.json()).then(d => {
            if (d.influx_enabled === false) { this._renderHistoryDisabled(); return; }
            this.histRegisters = d.registers || [];
            this._histRegMeta = Object.fromEntries(this.histRegisters.map(r => [r.name, { unit: r.unit || '', label: r.label || r.name }]));
            if (!this.histSelected) this.histSelected = [];
            if (!this.histSelected.length && this.histRegisters.length) this.histSelected = [this.histRegisters[0].name];
            this.renderHistoryRegisters();
            this.loadHistory();
        }).catch(() => {
            const l = document.getElementById('histRegList');
            if (l) l.innerHTML = '<div style="padding:20px;color:var(--text-secondary);">Could not load measurements.</div>';
        });
    },

    // Device changed → its register set (and history) differ; reload both.
    _reloadHistoryForDevice() {
        this.histRegisters = [];
        this.histSelected = [];
        this._loadHistoryRegisters();
    },

    renderHistoryRegisters() {
        const container = document.getElementById('histRegList');
        if (!container) return;
        const sel = new Set(this.histSelected || []);
        const palette = this._histColors();
        const colorOf = (name) => palette[(this.histSelected || []).indexOf(name) % palette.length];
        const cats = new Map();
        (this.histRegisters || []).forEach(reg => {
            const cat = this._histCategory(reg);
            if (!cats.has(cat)) cats.set(cat, []);
            cats.get(cat).push(reg);
        });
        let html = '';
        for (const catName of Array.from(cats.keys()).sort()) {
            const items = cats.get(catName).filter(it =>
                !this.histSearch || `${it.name} ${it.label || ''} ${it.unit || ''}`.toLowerCase().includes(this.histSearch));
            if (!items.length) continue;
            const disp = catName.charAt(0).toUpperCase() + catName.slice(1);
            html += `<div class="monitor-category expanded" data-category="${catName}">
                <div class="monitor-category-header"><span class="arrow">&#9654;</span><span>${disp}</span><span style="margin-left:auto;color:var(--text-tertiary);">(${items.length})</span></div>
                <div class="monitor-category-items">
                ${items.map(it => {
                    const on = sel.has(it.name);
                    const dot = on ? `<span class="hist-dot" style="background:${colorOf(it.name)};"></span>`
                                   : '<span class="hist-dot hist-dot-empty"></span>';
                    return `<div class="monitor-item hist-item ${on ? 'selected' : ''}" data-name="${this._esc(it.name)}" title="${this._esc(it.name)}">${dot}<span class="item-name">${this._esc(it.label || it.name)}</span><span class="item-unit">${this._esc(it.unit || '')}</span></div>`;
                }).join('')}
                </div></div>`;
        }
        container.innerHTML = html || '<div style="padding:20px;color:var(--text-secondary);">No measurements match.</div>';
        container.querySelectorAll('.monitor-category-header').forEach(h =>
            h.addEventListener('click', () => h.parentElement.classList.toggle('expanded')));
        container.querySelectorAll('.hist-item').forEach(item =>
            item.addEventListener('click', () => this.toggleHistRegister(item.dataset.name)));
    },

    toggleHistRegister(name) {
        if (!this.histSelected) this.histSelected = [];
        const i = this.histSelected.indexOf(name);
        if (i >= 0) this.histSelected.splice(i, 1);
        else this.histSelected.push(name);
        this.renderHistoryRegisters();
        this.loadHistory();
    },

    async loadHistory() {
        const names = (this.histSelected || []).slice();
        const range = document.getElementById('histRange')?.value || '-6h';
        const selEvery = document.getElementById('histEvery')?.value || '5m';
        const every = this._effectiveEvery(range, selEvery);   // cap resolution on large ranges
        const canvas = document.getElementById('historyCanvas');
        const info = document.getElementById('histInfo');
        const leg = document.getElementById('histLegend');
        if (!canvas) return;
        if (!names.length) {
            if (info) info.textContent = 'Click measurements in the list to add them to the chart.';
            this._histSeries = null; this._clearCanvas(canvas); if (leg) leg.innerHTML = '';
            return;
        }
        if (info) info.textContent = 'Loading…';
        const single = names.length === 1;
        const colors = this._histColors();
        // Sequence guard: rapid range/register changes fire overlapping queries;
        // a slow earlier one must not overwrite a newer one's result (last-wins).
        const seq = (this._histSeq = (this._histSeq || 0) + 1);
        try {
            const results = await Promise.all(names.map(n =>
                fetch(`/api/history?name=${encodeURIComponent(n)}&start=${encodeURIComponent(range)}&every=${encodeURIComponent(every)}&fn=all${this._viewDeviceQS('&')}`)
                    .then(async r => r.ok ? r.json() : Promise.reject((await r.json().catch(() => ({}))).detail || ('HTTP ' + r.status)))));
            if (seq !== this._histSeq) return;         // superseded by a newer load
            const series = [];
            results.forEach((d, i) => {
                const mean = d.series_mean || [];
                if (!mean.length) return;
                const meta = (this._histRegMeta || {})[names[i]] || {};
                series.push({
                    name: names[i], label: meta.label || names[i], unit: meta.unit || '',
                    color: colors[i % colors.length], mean,
                    mins: single ? (d.series_min || mean) : null,
                    maxs: single ? (d.series_max || mean) : null,
                });
            });
            if (!series.length) {
                if (info) info.textContent = 'No data in range';
                this._histSeries = null; this._clearCanvas(canvas); if (leg) leg.innerHTML = '';
                return;
            }
            this._histSeries = series;
            const total = series.reduce((a, s) => a + s.mean.length, 0);
            const res = every !== selEvery ? ` · ${every} (raised for ${range.replace('-', '')})` : ` · ${every}`;
            if (info) {
                let axisNote = '';
                if (series.length > 1) {
                    // tell the truth about the shared Y axis: only "same-unit"
                    // when every series really shares a unit; otherwise warn
                    // that a small-magnitude series may be dwarfed.
                    const units = [...new Set(series.map(s => (s.unit || '').trim()).filter(Boolean))];
                    if (units.length <= 1) {
                        axisNote = ' · shared Y axis (same-unit)';
                        info.classList.remove('history-mixed-units');
                    } else {
                        axisNote = ` · ⚠ shared Y axis · mixed units (${units.join(', ')}) — not directly comparable`;
                        info.classList.add('history-mixed-units');
                    }
                } else {
                    info.classList.remove('history-mixed-units');
                }
                info.textContent = `${series.length} series · ${total} pts${res}${axisNote}`;
            }
            this._renderHistory(this._histHoverX || null);
        } catch (e) {
            if (seq !== this._histSeq) return;         // a newer load owns the UI now
            if (info) info.textContent = typeof e === 'string' ? e : 'Query failed';
        }
    },

    _clearCanvas(canvas) {
        try { canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height); } catch (e) {}
    },

    _renderHistory(hoverX, opts = {}) {
        // Reusable: History page (defaults) or the dashboard value-history modal
        // (opts.canvas / opts.series / opts.legendId).
        const canvas = opts.canvas || document.getElementById('historyCanvas');
        const series = opts.series || this._histSeries;
        if (!canvas || !series || !series.length) return;
        const ctx = canvas.getContext('2d');
        const W = canvas.clientWidth || 800, H = canvas.clientHeight || 400;
        const dpr = window.devicePixelRatio || 1;
        canvas.width = W * dpr; canvas.height = H * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, W, H);
        const X = p => new Date(p.t).getTime();
        let t0 = Infinity, t1 = -Infinity, vlo = Infinity, vhi = -Infinity;
        series.forEach(s => {
            s.mean.forEach(p => { const t = X(p); if (t < t0) t0 = t; if (t > t1) t1 = t; });
            (s.mins || s.mean).forEach(p => { if (p.v < vlo) vlo = p.v; });
            (s.maxs || s.mean).forEach(p => { if (p.v > vhi) vhi = p.v; });
        });
        if (!isFinite(t0)) return;
        if (t1 <= t0) t1 = t0 + 1;
        if (vlo === vhi) { vlo -= 1; vhi += 1; }
        const pad = (vhi - vlo) * 0.08; vlo -= pad; vhi += pad;
        const ml = 58, mr = 14, mt = 12, mb = 26;
        const px = t => ml + (t - t0) / ((t1 - t0) || 1) * (W - ml - mr);
        const py = v => mt + (1 - (v - vlo) / ((vhi - vlo) || 1)) * (H - mt - mb);
        const css = getComputedStyle(document.body);
        const muted = (css.getPropertyValue('--text-muted') || 'var(--text-secondary)').trim() || 'var(--text-secondary)';
        const units = new Set(series.map(s => s.unit || '').filter(Boolean));
        const commonUnit = units.size === 1 ? [...units][0] : '';
        ctx.font = '11px system-ui, sans-serif';
        ctx.strokeStyle = 'rgba(128,128,128,0.18)'; ctx.lineWidth = 1; ctx.fillStyle = muted; ctx.textAlign = 'left';
        for (let i = 0; i <= 4; i++) {
            const v = vlo + (vhi - vlo) * i / 4, y = py(v);
            ctx.beginPath(); ctx.moveTo(ml, y); ctx.lineTo(W - mr, y); ctx.stroke();
            ctx.fillText(v.toFixed(1), 6, y + 3);
        }
        // unit label on the Y axis (meaningful because we only mix same-unit series)
        if (commonUnit) { ctx.font = '600 11px system-ui, sans-serif'; ctx.fillText(commonUnit, 6, mt - 2); ctx.font = '11px system-ui, sans-serif'; }
        ctx.textAlign = 'center';
        const span = t1 - t0;
        for (let i = 0; i <= 4; i++) {
            const t = t0 + span * i / 4, x = px(t), dt = new Date(t);
            const lbl = span > 2 * 864e5 ? dt.toLocaleDateString() : dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            ctx.fillText(lbl, x, H - 8);
        }
        ctx.textAlign = 'left';
        // min/max band only when a single register is shown
        if (series.length === 1 && series[0].mins) {
            const s = series[0];
            ctx.beginPath();
            s.maxs.forEach((p, i) => { const x = px(X(p)), y = py(p.v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
            for (let i = s.mins.length - 1; i >= 0; i--) ctx.lineTo(px(X(s.mins[i])), py(s.mins[i].v));
            ctx.closePath(); ctx.fillStyle = this._hexA(s.color, 0.13); ctx.fill();
        }
        series.forEach(s => {
            s._pts = s.mean.map(p => ({ x: px(X(p)), y: py(p.v), t: X(p), v: p.v }));
            ctx.beginPath();
            s._pts.forEach((p, i) => { i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y); });
            ctx.strokeStyle = s.color; ctx.lineWidth = 1.6; ctx.stroke();
        });
        const leg = document.getElementById(opts.legendId || 'histLegend');
        if (leg) leg.innerHTML = series.map(s =>
            `<span style="display:inline-flex;align-items:center;gap:5px;margin-right:14px;font-size:12px;"><span style="width:11px;height:11px;border-radius:2px;background:${s.color};display:inline-block;"></span>${this._esc(s.label)}${s.unit ? ' <span style="color:var(--text-secondary);">(' + this._esc(s.unit) + ')</span>' : ''}</span>`).join('');
        if (hoverX != null) this._histHover(ctx, series, hoverX, { mt, mb, H }, canvas);
    },

    _histHover(ctx, series, hoverX, g, hoverCanvas) {
        let anchor = null, bd = Infinity;
        (series[0]._pts || []).forEach(p => { const dx = Math.abs(p.x - hoverX); if (dx < bd) { bd = dx; anchor = p; } });
        if (!anchor) return;
        const x = anchor.x;
        ctx.strokeStyle = 'rgba(128,128,128,0.55)'; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
        ctx.beginPath(); ctx.moveTo(x, g.mt); ctx.lineTo(x, g.H - g.mb); ctx.stroke(); ctx.setLineDash([]);
        const rows = [];
        series.forEach(s => {
            let bp = null, bbd = Infinity;
            (s._pts || []).forEach(p => { const dx = Math.abs(p.x - x); if (dx < bbd) { bbd = dx; bp = p; } });
            if (bp) {
                ctx.fillStyle = s.color; ctx.beginPath(); ctx.arc(bp.x, bp.y, 3.2, 0, 2 * Math.PI); ctx.fill();
                ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.4; ctx.stroke();
                rows.push(`<div style="display:flex;align-items:center;gap:6px;white-space:nowrap;"><span style="width:9px;height:9px;border-radius:2px;background:${s.color};display:inline-block;"></span><b>${bp.v.toFixed(2)}</b>${s.unit ? ' ' + this._esc(s.unit) : ''} <span style="opacity:.6;">${this._esc(s.label)}</span></div>`);
            }
        });
        const canvas = hoverCanvas || document.getElementById('historyCanvas');
        if (!this._histTip) {
            const tip = document.createElement('div');
            tip.style.cssText = 'position:absolute;pointer-events:none;background:rgba(18,22,27,0.94);color:#fff;padding:6px 9px;border-radius:5px;font-size:12px;z-index:6;display:none;box-shadow:0 2px 8px rgba(0,0,0,.3);';
            canvas.parentElement.style.position = 'relative';
            canvas.parentElement.appendChild(tip);
            this._histTip = tip;
        }
        const tip = this._histTip;
        tip.style.display = 'block';
        tip.innerHTML = `<div style="opacity:.7;margin-bottom:3px;">${new Date(anchor.t).toLocaleString()}</div>${rows.join('')}`;
        const cw = canvas.parentElement.clientWidth;
        const tw = tip.offsetWidth || 170;
        let left = x + 14; if (left + tw > cw) left = x - 14 - tw;
        tip.style.left = Math.max(2, left) + 'px';
        tip.style.top = (g.mt + 4) + 'px';
    }
});
