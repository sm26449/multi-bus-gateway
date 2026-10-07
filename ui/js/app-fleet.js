/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// Fleet overview — the dashboard's default face when several devices exist.
// One row per device (health, alarms, hero metrics), grouped by endpoint and
// sorted by severity: problems surface, browsing is optional. Clicking a row
// opens the per-device widget view (the old dashboard), Back returns here.
Object.assign(JanitzaMonitor.prototype, {

    // fleet is only meaningful with 2+ devices; single-device installs keep
    // the classic per-device dashboard with zero fleet chrome
    _fleetCapable() {
        return ((this.fleetData && this.fleetData.devices) || []).length >= 2;
    },

    _fleetVisible() {
        return this.currentPage === 'dashboard' && this._fleetCapable()
            && (this._dashMode || 'fleet') === 'fleet';
    },

    async initFleet() {
        // fleet is the dashboard's DEFAULT face: every page load lands here
        // (drill-in is session state, deliberately not persisted)
        this._dashMode = 'fleet';
        this._fleetQuery = '';
        this._fleetProblems = false;
        this._wireFleetControls();
        await this.refreshFleet();
        this._applyDashMode();
        // alarm counts + health are server-side judgements → refetch; hero
        // values additionally stream in live over the websocket (_fleetIngest)
        this._fleetTimer = setInterval(() => {
            if (this._fleetVisible() && document.visibilityState === 'visible') {
                this.refreshFleet();
            }
        }, 5000);
    },

    _wireFleetControls() {
        const search = document.getElementById('fleetSearch');
        if (search) search.addEventListener('input', () => {
            this._fleetQuery = search.value.trim().toLowerCase();
            this.renderFleet();
        });
        const probs = document.getElementById('fleetProblemsBtn');
        if (probs) probs.addEventListener('click', () => {
            this._fleetProblems = !this._fleetProblems;
            probs.setAttribute('aria-pressed', String(this._fleetProblems));
            probs.classList.toggle('active', this._fleetProblems);
            this.renderFleet();
        });
        const back = document.getElementById('fleetBackBtn');
        if (back) back.addEventListener('click', () => this.showFleetView());
        const view = document.getElementById('fleetView');
        if (view) view.addEventListener('click', (e) => {
            const pin = e.target.closest('.fleet-pin');
            if (pin) { e.stopPropagation(); this._toggleOverviewPin(pin.dataset.pin); return; }
            const row = e.target.closest('.fleet-row');
            if (row && row.dataset.id) this.openFleetDevice(row.dataset.id);
        });
        // the pin is a span inside the row's <button> (nested buttons are
        // invalid HTML) — give it its own keyboard activation
        if (view) view.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter' && e.key !== ' ') return;
            const pin = e.target.closest && e.target.closest('.fleet-pin');
            if (pin) { e.preventDefault(); e.stopPropagation(); this._toggleOverviewPin(pin.dataset.pin); }
        });
    },

    // show/hide the two dashboard faces; the widget toolbar belongs to the
    // per-device face only
    _applyDashMode() {
        const fleet = this._fleetVisible() || (this._fleetCapable()
            && (this._dashMode || 'fleet') === 'fleet');
        const set = (id, hidden) => {
            const el = document.getElementById(id);
            if (el) el.hidden = hidden;
        };
        set('fleetControls', !fleet);
        set('fleetView', !fleet);
        set('siteStrip', !fleet);      // renderSiteStrip re-hides it when empty
        set('dashboardGrid', fleet);
        set('deviceSections', fleet);
        set('deviceSwitcherWrap', fleet);
        set('dashboardDensityToggle', fleet);
        set('customizeDashBtn', fleet);
        set('fleetBackBtn', fleet || !this._fleetCapable());
    },

    async refreshFleet() {
        try {
            const r = await fetch('/api/fleet');
            if (!r.ok) return;
            this.fleetData = await r.json();
        } catch (e) { return; }
        this._applyDashMode();
        await this._refreshPinValues();
        if (this._fleetVisible()) this.renderFleet();
    },

    showFleetView() {
        this._dashMode = 'fleet';
        this._applyDashMode();
        this.renderFleet();
        this.refreshFleet();
    },

    async openFleetDevice(id) {
        this._dashMode = 'device';
        this._applyDashMode();
        await this.switchDashDevice(id);
        this.updateDashboard();
        this.renderDashDeviceChips();
    },

    // live hero values: the websocket already broadcasts every device's
    // updates — harvest the addresses the fleet rows show, re-render at 1Hz
    _fleetIngest(msg) {
        if (!this._fleetVisible() || !this.fleetData || !msg.values) return;
        const devId = msg.device || this._primaryDeviceId();
        const dev = (this.fleetData.devices || []).find(d => d.id === devId);
        if (!dev) return;
        let touched = false;
        // personalized site cards stream too
        this._overviewPins().forEach(p => {
            if (p.id !== devId || p.address == null) return;
            const item = msg.values[String(p.address)];
            if (item) {
                this._pinValues = this._pinValues || {};
                (this._pinValues[devId] = this._pinValues[devId] || {})[String(p.address)] = item.value;
                touched = true;
            }
        });
        (dev.hero || []).forEach(m => {
            const item = msg.values[String(m.address)];
            if (item && item.value !== m.value) { m.value = item.value; touched = true; }
        });
        if (!touched) return;
        const now = Date.now();
        if (now - (this._fleetLastRender || 0) >= 1000) {
            this._fleetLastRender = now;
            this.renderFleet();
        }
    },

    // ── Site strip: the energy story above the fleet ──────────────────────
    // One card per installation (live aggregate power, units online) plus the
    // devices the operator PINNED to the overview (their first hero metric).
    // Pins are a per-browser view preference, like density and collapse state.
    // pins v2: {id, address?, name?, label?, unit?, title?} — address picks
    // WHICH metric the card shows (default: the device's first hero metric),
    // title overrides the label. v1 stored bare id strings; normalize them.
    _overviewPins() {
        try {
            const raw = JSON.parse(localStorage.getItem('mbg-overview-pins') || '[]');
            return raw.map(p => (typeof p === 'string' ? { id: p } : p))
                      .filter(p => p && p.id);
        } catch (e) { return []; }
    },

    _savePins(pins) {
        try { localStorage.setItem('mbg-overview-pins', JSON.stringify(pins)); }
        catch (e) { /* private mode */ }
    },

    _pinIndex(id) { return this._overviewPins().findIndex(p => p.id === id); },

    _toggleOverviewPin(id) {
        const pins = this._overviewPins();
        const i = pins.findIndex(p => p.id === id);
        if (i >= 0) pins.splice(i, 1); else pins.push({ id });
        this._savePins(pins);
        this._refreshPinValues();
        this.renderFleet();
    },

    // live values for pins on a CUSTOM metric (the default metric rides in
    // the fleet hero payload; a chosen address needs the device's own store)
    async _refreshPinValues() {
        if (!this._fleetVisible()) return;
        const custom = this._overviewPins().filter(p => p.address != null);
        const devs = [...new Set(custom.map(p => p.id))];
        this._pinValues = this._pinValues || {};
        await Promise.all(devs.map(async id => {
            try {
                const d = await (await fetch('/api/values?device=' + encodeURIComponent(id))).json();
                const vals = {};
                Object.entries(d.values || {}).forEach(([a, v]) => { vals[a] = v?.value; });
                this._pinValues[id] = vals;
            } catch (e) { /* keep last */ }
        }));
    },

    _powerText(w) {
        if (typeof w !== 'number' || !isFinite(w)) return null;
        const fmt = this.formatValueWithUnit(w, 'W');
        return { v: this._fmtNum(fmt.value, fmt.decimals), u: fmt.unit };
    },

    renderSiteStrip() {
        const strip = document.getElementById('siteStrip');
        if (!strip) return;
        if (!this._fleetVisible()) { strip.hidden = true; return; }
        const data = this.fleetData || {};
        const pins = this._overviewPins();
        const cards = [];
        (data.endpoints || []).forEach(p => {
            if (p.enabled === false) return;
            // the template's headline metric when the units declare one (a
            // battery bank's power), else the installation's AC power
            const c = p.card;
            let pw = null;
            if (c && typeof c.value === 'number') {
                const fmt = this.formatValueWithUnit(c.value, c.unit || '');
                pw = { v: this._fmtNum(fmt.value, fmt.decimals), u: fmt.unit };
            } else if (c && c.value != null) {
                pw = { v: this._esc(String(c.value)), u: '' };
            } else {
                pw = this._powerText(p.power_active_total);
            }
            const ok = (p.units_total || 0) > 0 && p.units_online === p.units_total;
            cards.push(`
                <button type="button" class="site-card" data-endpoint="${this._esc(p.id)}">
                    <div class="site-card-label"><i aria-hidden="true" class="bi bi-diagram-3"></i> ${this._esc(p.name)}</div>
                    <div class="site-card-value" ${c && c.hint ? `title="${this._esc(c.label)} · ${this._esc(c.hint)}"` : ''}>${pw ? `${pw.v}<span class="site-card-unit">${this._esc(pw.u)}</span>` : '--'}</div>
                    <div class="site-card-sub ${ok ? '' : 'site-card-warn'}">${p.units_online}/${p.units_total} ${this._esc(this.t('fleet.online', 'online'))}</div>
                </button>`);
        });
        const byId = {};
        (data.devices || []).forEach(d => { byId[d.id] = d; });
        pins.forEach(pin => {
            const d = byId[pin.id];
            if (!d) return;                       // pinned device no longer exists
            let value, unit, metricLabel;
            if (pin.address != null) {            // personalized metric
                value = (this._pinValues?.[pin.id] || {})[String(pin.address)];
                unit = pin.unit || '';
                metricLabel = pin.label || pin.name || '';
            } else {                              // default: first hero metric
                const m = (d.hero || [])[0];
                value = m?.value;
                unit = m?.unit || '';
                metricLabel = m?.label || d.health || '';
            }
            const disp = this._displayValue(value, { unit });
            cards.push(`
                <button type="button" class="site-card" data-device="${this._esc(pin.id)}">
                    <div class="site-card-label">${this._esc(pin.title || d.name || d.id)}</div>
                    <div class="site-card-value">${this._esc(disp.text)}<span class="site-card-unit">${this._esc(disp.unit)}</span></div>
                    <div class="site-card-sub">${this._esc(pin.title ? (d.name || d.id) : metricLabel)}</div>
                    <span class="site-card-edit" role="button" tabindex="0" data-edit="${this._esc(pin.id)}"
                          title="${this._esc(this.t('fleet.editCard', 'Customize card'))}">
                        <i aria-hidden="true" class="bi bi-pencil"></i>
                    </span>
                </button>`);
        });
        strip.hidden = cards.length === 0;
        strip.innerHTML = cards.join('');
        if (!strip._siteWired) {
            strip._siteWired = true;
            strip.addEventListener('click', (e) => {
                const edit = e.target.closest('.site-card-edit');
                if (edit) { e.stopPropagation(); this.openSiteCardModal(edit.dataset.edit); return; }
                const c = e.target.closest('.site-card');
                if (!c) return;
                if (c.dataset.device) this.openFleetDevice(c.dataset.device);
                else if (c.dataset.endpoint && this.openEndpointDetail) this.openEndpointDetail(c.dataset.endpoint);
            });
            strip.addEventListener('keydown', (e) => {
                if (e.key !== 'Enter' && e.key !== ' ') return;
                const edit = e.target.closest && e.target.closest('.site-card-edit');
                if (edit) { e.preventDefault(); e.stopPropagation(); this.openSiteCardModal(edit.dataset.edit); }
            });
        }
    },

    // ── Site card personalization modal ────────────────────────────────────
    async openSiteCardModal(id) {
        const pins = this._overviewPins();
        const idx = pins.findIndex(p => p.id === id);
        if (idx < 0) return;
        const pin = pins[idx];
        const dev = ((this.fleetData || {}).devices || []).find(d => d.id === id);
        const title = document.getElementById('siteCardModalTitle');
        if (title) title.textContent = (dev && (dev.name || dev.id)) || id;
        // the device's full register list feeds the metric picker (for an
        // endpoint unit this is the union across its sources)
        let regs = [];
        try {
            const d = await (await fetch('/api/registers/selected?device=' + encodeURIComponent(id))).json();
            regs = d.registers || [];
        } catch (e) { /* empty picker, save disabled below */ }
        this._siteCardRegs = regs;
        this._siteCardEditId = id;
        const sel = document.getElementById('siteCardMetric');
        if (sel) {
            const defAddr = pin.address != null ? pin.address : (dev?.hero || [])[0]?.address;
            sel.innerHTML = regs.map(r => `
                <option value="${r.address}" ${String(r.address) === String(defAddr) ? 'selected' : ''}>
                    ${this._esc(r.label || r.name)}${r.unit ? ` (${this._esc(r.unit)})` : ''}
                </option>`).join('');
        }
        const lbl = document.getElementById('siteCardLabel');
        if (lbl) lbl.value = pin.title || '';
        this._wireSiteCardModal();
        this.openModal('siteCardModal');
    },

    closeSiteCardModal() { this.closeModal('siteCardModal'); },

    _wireSiteCardModal() {
        if (this._siteCardWired) return;
        this._siteCardWired = true;
        const save = document.getElementById('siteCardSave');
        if (save) save.addEventListener('click', () => {
            const pins = this._overviewPins();
            const idx = pins.findIndex(p => p.id === this._siteCardEditId);
            if (idx < 0) return this.closeSiteCardModal();
            const sel = document.getElementById('siteCardMetric');
            const reg = (this._siteCardRegs || []).find(r => String(r.address) === String(sel?.value));
            const titleTxt = (document.getElementById('siteCardLabel')?.value || '').trim();
            const pin = { id: this._siteCardEditId };
            if (reg) {
                pin.address = reg.address;
                pin.name = reg.name;
                pin.label = reg.label || reg.name;
                pin.unit = reg.unit || '';
            }
            if (titleTxt) pin.title = titleTxt;
            pins[idx] = pin;
            this._savePins(pins);
            this.closeSiteCardModal();
            this._refreshPinValues().then(() => this.renderSiteStrip());
        });
        const remove = document.getElementById('siteCardRemove');
        if (remove) remove.addEventListener('click', () => {
            const pins = this._overviewPins();
            const idx = pins.findIndex(p => p.id === this._siteCardEditId);
            if (idx >= 0) { pins.splice(idx, 1); this._savePins(pins); }
            this.closeSiteCardModal();
            this.renderFleet();
        });
        const move = (dir) => () => {
            const pins = this._overviewPins();
            const idx = pins.findIndex(p => p.id === this._siteCardEditId);
            const j = idx + dir;
            if (idx < 0 || j < 0 || j >= pins.length) return;
            [pins[idx], pins[j]] = [pins[j], pins[idx]];
            this._savePins(pins);
            this.renderSiteStrip();
        };
        document.getElementById('siteCardMoveLeft')?.addEventListener('click', move(-1));
        document.getElementById('siteCardMoveRight')?.addEventListener('click', move(1));
    },

    // severity: down > danger alarms > no-data (stale/idle/degraded) >
    // warnings > ok; disabled devices sink to the bottom
    _fleetRank(d) {
        if (d.enabled === false) return 90;
        const h = d.health || 'idle';
        if (h === 'down') return 0;
        if ((d.alarms && d.alarms.danger) > 0) return 1;
        if (h !== 'ok') return 2;
        if ((d.alarms && d.alarms.warning) > 0) return 3;
        return 50;
    },

    _fleetAge(s) {
        if (s == null || !isFinite(s)) return '';
        if (s < 90) return Math.round(s) + 's';
        if (s < 5400) return Math.round(s / 60) + 'm';
        return (s / 3600).toFixed(1) + 'h';
    },

    _fleetRow(d) {
        const rank = this._fleetRank(d);
        const hDot = { ok: 'var(--success)', degraded: 'var(--warning)',
                       stale: 'var(--warning)', down: 'var(--danger)' };
        const sev = (rank <= 1) ? 'sev-danger' : (rank < 50 ? 'sev-warning' : '');
        const metrics = (d.hero || []).map(m => {
            const disp = this._displayValue(m.value, { unit: m.unit });
            return `<span class="fleet-metric" title="${this._esc(m.label)}">
                <span class="fm-l">${this._esc(m.label)}</span>
                <span class="fm-v">${this._esc(disp.text)}</span><span class="fm-u">${this._esc(disp.unit)}</span>
            </span>`;
        }).join('');
        const flags = [];
        if (d.alarms && d.alarms.danger) flags.push(`<span class="fleet-badge fb-danger" title="${this._esc(this.t('fleet.dangerAlarms', 'Danger thresholds exceeded'))}">${d.alarms.danger}</span>`);
        if (d.alarms && d.alarms.warning) flags.push(`<span class="fleet-badge fb-warning" title="${this._esc(this.t('fleet.warningAlarms', 'Warning thresholds exceeded'))}">${d.alarms.warning}</span>`);
        const h = d.health || 'idle';
        if (h !== 'ok') {
            const age = this._fleetAge(d.staleness_age_s);
            flags.push(`<span class="fleet-health">${this._esc(h)}${age ? ' ' + age : ''}</span>`);
        }
        const pinned = this._overviewPins().includes(d.id);
        return `<button type="button" class="fleet-row ${sev}" data-id="${this._esc(d.id)}"
                        ${d.enabled === false ? 'data-disabled="1"' : ''}>
            <span class="fleet-dot" style="background:${hDot[h] || 'var(--text-tertiary)'}"></span>
            <span class="fleet-name" title="${this._esc(d.id)}">${this._esc(d.name || d.id)}</span>
            <span class="fleet-metrics">${metrics}</span>
            <span class="fleet-flags">${flags.join('')}</span>
            <span class="fleet-pin ${pinned ? 'active' : ''}" role="button" tabindex="0"
                  data-pin="${this._esc(d.id)}" aria-pressed="${pinned}"
                  title="${this._esc(pinned ? this.t('fleet.unpinOverview', 'Unpin from overview')
                                            : this.t('fleet.pinOverview', 'Pin to overview'))}">
                <i aria-hidden="true" class="bi ${pinned ? 'bi-pin-fill' : 'bi-pin'}"></i>
            </span>
            <i class="bi bi-chevron-right" aria-hidden="true"></i>
        </button>`;
    },

    renderFleet() {
        this.renderSiteStrip();
        const view = document.getElementById('fleetView');
        if (!view || !this.fleetData) return;
        let devices = (this.fleetData.devices || []).slice();
        const total = devices.length;
        const problems = devices.filter(d => this._fleetRank(d) < 50 && d.enabled !== false).length;

        if (this._fleetQuery) {
            devices = devices.filter(d =>
                (d.name || '').toLowerCase().includes(this._fleetQuery)
                || (d.id || '').toLowerCase().includes(this._fleetQuery));
        }
        if (this._fleetProblems) {
            devices = devices.filter(d => this._fleetRank(d) < 50);
        }

        const summary = document.getElementById('fleetSummary');
        if (summary) {
            summary.textContent = problems
                ? this.t('fleet.summaryProblems', '{n} devices · {p} need attention', { n: total, p: problems })
                : this.t('fleet.summaryOk', '{n} devices · all OK', { n: total });
        }

        if (!devices.length) {
            view.innerHTML = `<div class="empty-state">${this._esc(this.t('fleet.noMatch', 'No devices match.'))}</div>`;
            return;
        }

        const bySev = (a, b) => this._fleetRank(a) - this._fleetRank(b)
            || String(a.name || a.id).localeCompare(String(b.name || b.id));
        const epName = {};
        (this.fleetData.endpoints || []).forEach(p => { epName[p.id] = p.name; });

        const standalone = devices.filter(d => !d.endpoint_id).sort(bySev);
        const byEp = new Map();
        devices.filter(d => d.endpoint_id).forEach(d => {
            if (!byEp.has(d.endpoint_id)) byEp.set(d.endpoint_id, []);
            byEp.get(d.endpoint_id).push(d);
        });

        let html = '';
        if (standalone.length) {
            html += `<div class="fleet-group"><div class="fleet-rows">${standalone.map(d => this._fleetRow(d)).join('')}</div></div>`;
        }
        // endpoint groups: the group with the worst unit first
        const groups = [...byEp.entries()].map(([pid, units]) => {
            units.sort(bySev);
            return { pid, units, rank: this._fleetRank(units[0]) };
        }).sort((a, b) => a.rank - b.rank);
        groups.forEach(g => {
            const ok = g.units.filter(d => (d.health || '') === 'ok').length;
            html += `<div class="fleet-group">
                <div class="fleet-group-head"><i aria-hidden="true" class="bi bi-diagram-3"></i>
                    ${this._esc(epName[g.pid] || g.pid)}
                    <span class="fleet-group-count">${ok}/${g.units.length} ${this._esc(this.t('fleet.online', 'online'))}</span>
                </div>
                <div class="fleet-rows">${g.units.map(d => this._fleetRow(d)).join('')}</div>
            </div>`;
        });
        view.innerHTML = html;
    },
});
