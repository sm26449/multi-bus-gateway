/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// dashboard domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    // ============ Value Color Coding ============

    /**
     * Detect measurement type from unit and name for template selection
     * Returns template key: voltage_ln, voltage_ll, frequency, power_factor, thd, current, power
     */
    detectMeasurementType(unit, name) {
        // These implicit threshold templates encode GRID assumptions (230 V
        // L-N, 50 Hz, breaker-sized currents), so they must fire only on the
        // canonical GRID register names — never on a bare unit or a loose
        // substring. The old heuristics painted a 51 V battery bank
        // danger-red, and /u[_]?l/ even matched the "ul" in energy_to_FULl
        // (audit 2026-10-03, seen live). A register that wants coloring
        // outside these shapes sets its own thresholds.
        const unitLower = (unit || '').toLowerCase();
        const nameLower = (name || '').toLowerCase();

        // Voltage — canonical grid names only, anchored at the start
        if (/^(voltage_l\d_?l\d|voltage_ll|u_?l\d_?l\d|ull)/.test(nameLower)) {
            return 'voltage_ll';
        }
        if (/^(voltage_l\d(_n)?($|_)|voltage_ln|u_?l\d|uln)/.test(nameLower)) {
            return 'voltage_ln';
        }
        // Frequency — the unit is unambiguous
        if (unitLower === 'hz' || /^freq/.test(nameLower)) {
            return 'frequency';
        }
        // Power factor
        if (/^(power_factor|cos_?phi|pf)(_|$)/.test(nameLower)) {
            return 'power_factor';
        }
        // THD
        if (/^thd/.test(nameLower) || unitLower === '%thd' || unitLower === '% thd') {
            return 'thd';
        }
        // Current — per-phase grid names only
        if (/^(current_(l\d|n)($|_)|i_?l\d)/.test(nameLower)) {
            return 'current';
        }
        // Power — canonical grid power names only
        if (/^(power_(active|apparent|reactive)|p_?l\d|s_?l\d)/.test(nameLower)) {
            return 'power';
        }

        return null;
    },

    /**
     * Get threshold template for a measurement type
     */
    getThresholdTemplate(unit, name) {
        const type = this.detectMeasurementType(unit, name);
        if (type && this.thresholdTemplates[type]) {
            return { ...this.thresholdTemplates[type], templateType: type };
        }
        return null;
    },

    /**
     * Format value with automatic unit scaling (Wh→kWh, W→kW, VA→kVA, var→kvar)
     * Returns { value: number, unit: string, decimals: number }
     */
    formatValueWithUnit(value, unit) {
        if (typeof value !== 'number' || isNaN(value)) {
            return { value: value, unit: unit, decimals: 2 };
        }

        const absVal = Math.abs(value);

        // Energy: Wh → kWh → MWh
        if (unit === 'Wh') {
            if (absVal >= 1000000) return { value: value / 1000000, unit: 'MWh', decimals: 2 };
            if (absVal >= 1000) return { value: value / 1000, unit: 'kWh', decimals: 2 };
            return { value, unit, decimals: 1 };
        }
        if (unit === 'varh' || unit === 'VArh') {
            if (absVal >= 1000000) return { value: value / 1000000, unit: 'Mvarh', decimals: 2 };
            if (absVal >= 1000) return { value: value / 1000, unit: 'kvarh', decimals: 2 };
            return { value, unit, decimals: 1 };
        }
        if (unit === 'VAh') {
            if (absVal >= 1000000) return { value: value / 1000000, unit: 'MVAh', decimals: 2 };
            if (absVal >= 1000) return { value: value / 1000, unit: 'kVAh', decimals: 2 };
            return { value, unit, decimals: 1 };
        }

        // Power: W → kW → MW
        if (unit === 'W') {
            if (absVal >= 1000000) return { value: value / 1000000, unit: 'MW', decimals: 2 };
            if (absVal >= 10000) return { value: value / 1000, unit: 'kW', decimals: 2 };
            return { value, unit, decimals: 1 };
        }
        if (unit === 'VA') {
            if (absVal >= 1000000) return { value: value / 1000000, unit: 'MVA', decimals: 2 };
            if (absVal >= 10000) return { value: value / 1000, unit: 'kVA', decimals: 2 };
            return { value, unit, decimals: 1 };
        }
        if (unit === 'var' || unit === 'VAr') {
            if (absVal >= 1000000) return { value: value / 1000000, unit: 'Mvar', decimals: 2 };
            if (absVal >= 10000) return { value: value / 1000, unit: 'kvar', decimals: 2 };
            return { value, unit, decimals: 1 };
        }

        return { value, unit, decimals: 2 };
    },

    /**
     * Get CSS class for value based on register thresholds
     * Uses per-register thresholds if available, otherwise detects from type
     * Returns: 'value-normal', 'value-warning', 'value-danger', or 'value-success'
     */
    getValueColorClass(value, register) {
        if (typeof value !== 'number' || isNaN(value)) {
            return 'value-normal';
        }

        // Get thresholds - prefer per-register, fallback to template
        let thresholds = null;
        let measurementType = null;

        if (register && register.thresholds && register.thresholds.enabled) {
            thresholds = register.thresholds;
            measurementType = register.thresholds.templateType || this.detectMeasurementType(register.unit, register.name);
        } else if (register) {
            // Fallback to template-based detection
            const template = this.getThresholdTemplate(register.unit, register.name);
            if (template) {
                thresholds = template;
                measurementType = template.templateType;
            }
        }

        if (!thresholds) {
            return 'value-normal';
        }

        // Check danger thresholds first (they take priority)
        if (thresholds.dangerLow !== null && thresholds.dangerLow !== undefined && value < thresholds.dangerLow) {
            return 'value-danger';
        }
        if (thresholds.dangerHigh !== null && thresholds.dangerHigh !== undefined && value > thresholds.dangerHigh) {
            return 'value-danger';
        }

        // Check warning thresholds
        if (thresholds.warningLow !== null && thresholds.warningLow !== undefined && value < thresholds.warningLow) {
            return 'value-warning';
        }
        if (thresholds.warningHigh !== null && thresholds.warningHigh !== undefined && value > thresholds.warningHigh) {
            return 'value-warning';
        }

        // For power factor, show success when good (>0.95)
        if (measurementType === 'power_factor' && value >= 0.95) {
            return 'value-success';
        }

        // For THD, show success when very low (<2%)
        if (measurementType === 'thd' && value < 2) {
            return 'value-success';
        }

        return 'value-normal';
    },

    /**
     * Auto-fill threshold fields in Add/Edit modal based on detected type
     * @param {string} prefix - 'add' or 'edit'
     * @param {string} unit - Register unit
     * @param {string} name - Register name
     * @param {object} existingThresholds - Existing thresholds to use (for edit mode)
     */
    autoFillThresholds(prefix, unit, name, existingThresholds = null) {
        const detectedDiv = document.getElementById(`${prefix}ThresholdDetected`);
        const enabledCheckbox = document.getElementById(`${prefix}ThresholdEnabled`);

        // Get template based on detection
        const template = this.getThresholdTemplate(unit, name);
        const typeNames = {
            voltage_ln: 'Voltage L-N',
            voltage_ll: 'Voltage L-L',
            frequency: 'Frequency',
            power_factor: 'Power Factor',
            thd: 'THD',
            current: 'Current',
            power: 'Power'
        };

        // Unit clarity: thresholds compare the RAW value in the register's own
        // unit, while cards may display auto-scaled units (kVA vs VA). Without
        // this line a user types "100" meaning 100 kVA and every reading above
        // 100 VA turns red (the false-alarm we shipped once ourselves).
        const live = Object.values(this._dashStore() || {})
            .find(v => v && v.name === name);
        const liveTxt = (live && typeof live.value === 'number')
            ? ` · ${this.t('thr.currentLive', 'current live value:')} <b>${this._fmtNum(live.value, 2)} ${this._esc(unit || '')}</b>` : '';
        const unitLine = unit
            ? `<div>${this.t('thr.unitHint', 'Thresholds are compared against the RAW value in')} <b>${this._esc(unit)}</b>${liveTxt}</div>` : '';
        if (template) {
            detectedDiv.innerHTML = `Detected: <span class="detected-type">${this._esc(typeNames[template.templateType] || template.templateType)}</span> - thresholds auto-filled${unitLine}`;
            detectedDiv.classList.add('visible');
        } else {
            detectedDiv.innerHTML = unitLine;
            detectedDiv.classList.toggle('visible', !!unitLine);
        }

        // Use existing thresholds if provided, otherwise use template
        const thresholds = existingThresholds || template || {};

        // Enable checkbox
        enabledCheckbox.checked = existingThresholds ? existingThresholds.enabled !== false : !!template;

        // Fill the fields
        document.getElementById(`${prefix}ThreshDangerLow`).value = thresholds.dangerLow ?? '';
        document.getElementById(`${prefix}ThreshWarningLow`).value = thresholds.warningLow ?? '';
        document.getElementById(`${prefix}ThreshWarningHigh`).value = thresholds.warningHigh ?? '';
        document.getElementById(`${prefix}ThreshDangerHigh`).value = thresholds.dangerHigh ?? '';
    },

    /**
     * Read threshold values from modal form
     * @param {string} prefix - 'add' or 'edit'
     * @returns {object|null} Threshold object or null if disabled
     */
    readThresholdsFromForm(prefix) {
        const enabled = document.getElementById(`${prefix}ThresholdEnabled`).checked;

        const parseVal = (id) => {
            const val = document.getElementById(id).value;
            return val === '' ? null : parseFloat(val);
        };

        return {
            enabled,
            dangerLow: parseVal(`${prefix}ThreshDangerLow`),
            warningLow: parseVal(`${prefix}ThreshWarningLow`),
            warningHigh: parseVal(`${prefix}ThreshWarningHigh`),
            dangerHigh: parseVal(`${prefix}ThreshDangerHigh`)
        };
    },

    // ── Dashboard value → recent-history modal ─────────────────────────────
    _wireDashboardClicks() {
        const grid = document.getElementById('dashboardGrid');
        if (!grid || grid._histWired) return;
        grid._histWired = true;
        grid.addEventListener('click', (e) => {
            // Works for both card view (.widget-card) and table view (tr[data-address]).
            const el = e.target.closest('.widget-card, tr[data-address]');
            if (!el || el.dataset.address == null) return;
            this.openValueHistory(el.dataset.address);
        });
        grid.addEventListener('keydown', (e) => this._historyKey(e, '.widget-card, tr[data-address]'));
    },

    // Enter / Space on a focused card or row opens its history — the same
    // thing a click does, for keyboard and switch users.
    _historyKey(e, sel) {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        const el = e.target.closest(sel);
        if (!el || el.dataset.address == null || e.target !== el) return;
        e.preventDefault();
        this.openValueHistory(el.dataset.address);
    },

    openValueHistory(address) {
        // the dashboard device's own list first — selectedRegisters belongs to
        // the Measurements page and misses non-primary devices entirely
        const reg = [...(this.dashRegisters || []), ...(this.selectedRegisters || [])]
            .find(r => String(r.address) === String(address));
        if (!reg || !reg.name) return;
        this._vhReg = reg;
        this._vhRange = this._vhRange || '-1h';
        const title = document.getElementById('valHistTitle');
        if (title) title.textContent = reg.label || reg.name;
        const ranges = document.querySelectorAll('#valueHistoryModal .vh-range');
        if (!this._vhWired) {
            this._vhWired = true;
            ranges.forEach(b => b.addEventListener('click', () => {
                this._vhRange = b.dataset.range;
                ranges.forEach(x => x.classList.toggle('active', x === b));
                this.loadValueHistory();
            }));
        }
        ranges.forEach(x => x.classList.toggle('active', x.dataset.range === this._vhRange));
        this.openModal('valueHistoryModal');
        this.loadValueHistory();
    },

    // The socket badge says "nothing is arriving"; this one says "this DEVICE
    // has stopped" while everything else flows — its last values would
    // otherwise sit on the page looking live.
    _syncDeviceStaleBadge(grid) {
        const dev = this._dashDeviceId();
        const s = (this.status?.devices || []).find(d => d.id === dev) || {};
        const h = s.data_health;
        let b = document.getElementById('devStaleBadge');
        const show = (h === 'stale' || h === 'down') && !document.body.classList.contains('ws-stale');
        if (!show) { if (b) b.remove(); return; }
        if (!b) {
            b = document.createElement('div');
            b.id = 'devStaleBadge';
            b.className = 'stale-badge';
            b.setAttribute('role', 'status');
            (grid?.parentElement || document.body).insertBefore(b, grid || null);
        }
        const age = typeof s.staleness_age_s === 'number' ? s.staleness_age_s : null;
        const ageTxt = age == null ? '' : ' · ' + this.t('dash.devLastUpdate', 'last update') + ' '
            + (age < 120 ? `${Math.round(age)} s` : age < 7200 ? `${Math.round(age / 60)} min` : `${Math.round(age / 3600)} h`)
            + ' ' + this.t('dash.ago', 'ago');
        b.textContent = (h === 'down'
            ? this.t('dash.devDownBanner', 'This device is not responding — the values shown are the last received')
            : this.t('dash.devStaleBanner', 'This device’s values are not updating')) + ageTxt;
    },

    async loadValueHistory() {
        const reg = this._vhReg;
        if (!reg) return;
        const canvas = document.getElementById('valHistCanvas');
        const info = document.getElementById('valHistInfo');
        const leg = document.getElementById('valHistLegend');
        const range = this._vhRange || '-1h';
        const every = range === '-1h' ? '1m' : (range === '-3h' ? '2m' : '5m');
        if (info) info.textContent = this.t('common.loading', 'Loading…');
        if (leg) leg.innerHTML = '';
        this._clearCanvas(canvas);
        // the device on screen, not the primary — a secondary's history lives
        // in its own bucket; and the last request wins over a slower one
        const devQs = this._dashIsPrimary() ? '' : `&device=${encodeURIComponent(this._dashDeviceId())}`;
        const seq = this._vhSeq = (this._vhSeq || 0) + 1;
        try {
            const r = await fetch(`/api/history?name=${encodeURIComponent(reg.name)}&start=${encodeURIComponent(range)}&every=${every}&fn=all${devQs}`);
            if (seq !== this._vhSeq) return;
            if (r.status === 503) { if (info) info.textContent = this.t('valhist.needInflux'); return; }
            if (!r.ok) { if (info) info.textContent = `HTTP ${r.status}`; return; }
            const d = await r.json();
            if (seq !== this._vhSeq) return;
            const mean = d.series_mean || [];
            if (!mean.length) { if (info) info.textContent = this.t('valhist.noData'); return; }
            const series = [{
                name: reg.name, label: reg.label || reg.name, unit: reg.unit || '',
                color: (this._histColors && this._histColors()[0]) || '#2f81f7',
                mean, mins: d.series_min || mean, maxs: d.series_max || mean,
            }];
            if (info) info.textContent = '';
            // Render next frame so the now-visible canvas has a measured width.
            requestAnimationFrame(() => this._renderHistory(null, { canvas, series, legendId: 'valHistLegend' }));
        } catch (e) {
            if (info) info.textContent = this.t('settings.saveFailed', 'Failed');
        }
    },

    updateDashboard() {
        // fleet face active → the widget grid is hidden, don't paint it
        if (this._fleetVisible && this._fleetVisible()) return;
        const grid = document.getElementById('dashboardGrid');
        this._syncDeviceStaleBadge(grid);

        // Per-device empty state: distinguish "no widgets picked" from "source
        // is down" so the operator isn't guessing which problem they have.
        const dashRegs = this._dashRegs();
        if (!dashRegs.length && !this._dashIsPrimary()) {
            const dev = this._dashDeviceId();
            const h = (this.status?.devices || []).find(d => d.id === dev)?.data_health;
            if (grid) grid.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon"><i class="bi ${h === 'down' ? 'bi-wifi-off' : 'bi-bar-chart'}" aria-hidden="true"></i></div>
                    <div class="empty-state-title">${h === 'down'
                        ? this.t('dash.deviceDown', 'Source is not responding')
                        : this.t('dash.noDeviceWidgets', 'No measurements on this dashboard yet')}</div>
                    <div class="empty-state-desc">${h === 'down'
                        ? this.t('dash.deviceDownDesc', 'The device is unreachable — check Status for details.')
                        : this.t('dash.noDeviceWidgetsDesc', 'Pick measurements for this device to see them here.')}</div>
                    <button class="empty-state-action" ${this._act('jumpToDeviceRegisters', [dev])}>
                        📋 ${this.t('dash.goMeasurements', 'Go to Measurements')}</button>
                </div>`;
            const sections = document.getElementById('deviceSections');
            if (sections) { sections.innerHTML = ''; this._sectionsKey = ''; }
            return;
        }

        // Get registers that should be on dashboard and sort by order
        const dashboardRegs = dashRegs
            .filter(r => r.ui_show_on_dashboard)
            .sort((a, b) => {
                const orderA = a.ui_config?.dashboard_order ?? 999;
                const orderB = b.ui_config?.dashboard_order ?? 999;
                return orderA - orderB;
            });

        // Hero strip + sections (the integrator pattern: SolarEdge/VRM put a
        // few curated KPIs up top, HA/GridVis put the volume in grouped dense
        // sections). A card is a spotlight, not an inventory — the legacy
        // defaults flag EVERYTHING on-dashboard (umg512: 39), so the strip
        // takes only the first HERO_CAP by dashboard_order and every value
        // still lives in the categorized table below.
        const heroRegs = dashboardRegs.slice(0, this.HERO_CAP);
        this.renderDeviceSections(dashRegs, heroRegs);
        // Get current widget addresses (data-address is on the wrapper div)
        const existingWidgets = new Map();
        grid.querySelectorAll('.widget-card[data-address]').forEach(el => {
            existingWidgets.set(parseInt(el.dataset.address), el);
        });

        // Track which addresses should exist
        const targetAddresses = new Set(heroRegs.map(r => r.address));

        // Remove widgets that shouldn't exist anymore
        existingWidgets.forEach((el, addr) => {
            if (!targetAddresses.has(addr)) {
                el.remove();
            }
        });

        // Remove empty state if it exists and we have registers
        const emptyState = grid.querySelector('.empty-state');
        if (emptyState && heroRegs.length > 0) {
            emptyState.remove();
        }

        // Dominant poll group of the set — cards badge only the exceptions.
        this._dashDominantGroup = this._dominantPollGroup(heroRegs);

        // Update or create widgets in correct order
        heroRegs.forEach((reg, index) => {
            const value = this._dashStore()[reg.address];
            const numValue = value?.value;
            const existingCard = existingWidgets.get(reg.address);

            if (existingCard) {
                // Check if widget type changed - if so, recreate it
                const currentType = existingCard.classList.contains('widget-gauge') ? 'gauge'
                    : existingCard.classList.contains('widget-chart') ? 'chart' : 'value';
                const targetType = reg.ui_widget || 'value';

                if (currentType !== targetType) {
                    // Widget type changed - replace with new widget
                    const newCard = this.createWidgetCard(reg, numValue);
                    existingCard.replaceWith(newCard);
                } else {
                    // Widget exists - just update the value (incremental update)
                    this.updateWidgetValue(existingCard, reg, numValue);

                    // Check if order is correct
                    const currentIndex = [...grid.querySelectorAll('.widget-card')].indexOf(existingCard);
                    if (currentIndex !== index) {
                        // Move to correct position
                        const children = grid.querySelectorAll('.widget-card');
                        if (index < children.length) {
                            grid.insertBefore(existingCard, children[index]);
                        } else {
                            grid.appendChild(existingCard);
                        }
                    }
                }
            } else {
                // Create new widget
                const card = this.createWidgetCard(reg, numValue);
                const children = grid.querySelectorAll('.widget-card');
                if (index < children.length) {
                    grid.insertBefore(card, children[index]);
                } else {
                    grid.appendChild(card);
                }
            }
        });

        // Show empty state only when the device has NO registers at all —
        // zero heroes with a populated sections table below is a valid state.
        if (dashRegs.length === 0 && !grid.querySelector('.empty-state')) {
            grid.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon"><i class="bi bi-bar-chart" aria-hidden="true"></i></div>
                    <div class="empty-state-title">${this.t('msg.noWidgets', "No widgets on dashboard")}</div>
                    <div class="empty-state-desc">
                        ${this.t('dash.emptyDesc', 'Add measurements to your dashboard to monitor values in real-time.')}
                    </div>
                    <button class="empty-state-action" ${this._act('jumpToDeviceRegisters', [this._dashDeviceId()])}>
                        📋 ${this.t('dash.goMeasurements', 'Go to Measurements')}
                    </button>
                </div>
            `;
        }
    },

    // The MODAL (most frequent) poll group of the dashboard set — used to
    // silence its badge: 15 identical REALTIME chips carry zero information
    // (data-ink); only the exceptions (slow/normal rows) stay labelled.
    _dominantPollGroup(registers) {
        const counts = {};
        (registers || []).forEach(r => { counts[r.poll_group] = (counts[r.poll_group] || 0) + 1; });
        return Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0];
    },

    // One display contract for cards, live updates and the table view:
    // a TEXT value (a decoded enum state like 'Standby', a string register)
    // renders as text — it used to fall through the numeric formatter as '--'.
    _displayValue(numValue, reg) {
        if (typeof numValue === 'string' && numValue !== '') {
            return { text: numValue, unit: '', isText: true };
        }
        const fmt = this.formatValueWithUnit(numValue, reg.unit);
        return { text: this._fmtNum(fmt.value, fmt.decimals), unit: fmt.unit, isText: false };
    },

    createWidgetCard(reg, numValue) {
        const disp = this._displayValue(numValue, reg);
        const displayValue = disp.text;

        // Widget card (CSS Grid handles responsive layout)
        const card = document.createElement('div');
        card.className = `widget-card widget-${reg.ui_widget || 'value'}`;
        if (reg.ui_config?.wide) card.classList.add('widget-wide');
        card.dataset.address = reg.address;
        card.tabIndex = 0;
        card.setAttribute('role', 'button');
        card.setAttribute('aria-label', `${reg.label || reg.name} — ${this.t('valhist.open', 'show history')}`);

        // Poll-group badge only when this row DIFFERS from the dashboard's
        // dominant group — the majority chip is noise, the exception is signal.
        const dom = this._dashDominantGroup;
        const pollBadge = (reg.poll_group && reg.poll_group !== dom)
            ? `<span class="poll-hint" title="${this.t('dash.pollGroup', 'Poll group')}: ${this._esc(reg.poll_group)}"><i aria-hidden="true" class="bi bi-clock"></i> ${this._esc(reg.poll_group)}</span>`
            : '';

        // Header with edit button
        const header = `
            <div class="widget-header">
                <div class="widget-header-left">
                    <span class="widget-label" title="${this._esc(reg.name)}">${this._esc(reg.label)}</span>
                </div>
                <div class="widget-header-right">
                    ${pollBadge}
                    <button class="widget-edit-btn" title="${this._esc(this.t('dash.editWidget', 'Edit widget'))}" aria-label="${this._esc(this.t('dash.editWidget', 'Edit widget'))}">
                        <i aria-hidden="true" class="bi bi-pencil"></i>
                    </button>
                </div>
            </div>
        `;

        // Render based on widget type
        let content = '';
        switch (reg.ui_widget) {
            case 'gauge':
                content = this.renderGaugeWidget(reg, numValue);
                break;
            case 'chart':
                content = this.renderChartWidget(reg);
                break;
            default: // 'value'
                const colorClass = this.getValueColorClass(numValue, reg);
                content = `
                    <div class="widget-value">
                        <span class="value-number ${disp.isText ? 'value-text' : ''} ${colorClass}">${this._esc(displayValue)}</span><span class="widget-unit">${this._esc(disp.unit)}</span>
                    </div>
                `;
        }

        // No footer: the raw register name is a tooltip on the label — it
        // duplicated the label on every card and doubled the chrome-to-data ratio.
        card.innerHTML = header + content;

        // Add edit button click handler
        const editBtn = card.querySelector('.widget-edit-btn');
        if (editBtn) {
            editBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.editRegister(reg);
            });
        }

        return card;
    },

    updateWidgetValue(card, reg, numValue) {
        const widgetType = reg.ui_widget || 'value';

        switch (widgetType) {
            case 'gauge':
                this.updateGaugeWidget(card, reg, numValue);
                break;
            case 'chart':
                this.updateChartWidget(card, reg);
                break;
            default: // 'value'
                const valueEl = card.querySelector('.value-number');
                if (valueEl) {
                    const disp = this._displayValue(numValue, reg);
                    if (valueEl.textContent !== disp.text) {
                        valueEl.textContent = disp.text;
                    }
                    valueEl.classList.toggle('value-text', disp.isText);
                    // Update unit display (may change with scaling)
                    const unitEl = card.querySelector('.widget-unit');
                    if (unitEl && unitEl.textContent !== disp.unit) {
                        unitEl.textContent = disp.unit;
                    }
                    // Update color class
                    const newColorClass = this.getValueColorClass(numValue, reg);
                    valueEl.classList.remove('value-normal', 'value-warning', 'value-danger', 'value-success');
                    valueEl.classList.add(newColorClass);
                }
        }
    },

    // ============ Device value sections (hero strip + grouped inventory) ============
    // The HA/GridVis pattern: a few spotlight cards on top, EVERY value in
    // dense collapsible sections grouped by what it measures. The group id
    // comes from the API's canonical `category`, refined client-side for BMS
    // vocabularies (cells, alarms) the canonical dictionary doesn't cover.

    HERO_CAP: 12,

    _sectionCat(reg) {
        // the template's own filing wins (it knows its cells from its alarms);
        // the name heuristics stay for maps that declare no categories
        if (reg.category_label) return reg.category;
        const n = String(reg.name || '').toLowerCase();
        if (/(^|_)(alarm|protect|fault|warn)(_|$|s_|ing)/.test(n)) return 'alarms';
        if (/^cell_?\d|^balancing/.test(n)) return 'cells';
        return reg.category || 'other';
    },

    // display order + labels; sections absent from a device simply don't render
    _sectionMeta() {
        return [
            ['power', this.t('sec.power', 'Power')],
            ['voltage', this.t('sec.voltage', 'Voltage')],
            ['current', this.t('sec.current', 'Current')],
            ['energy', this.t('sec.energy', 'Energy')],
            ['frequency', this.t('sec.frequency', 'Frequency')],
            ['dc', this.t('sec.dc', 'DC')],
            ['temperature', this.t('sec.temperature', 'Temperature')],
            ['cells', this.t('sec.cells', 'Cells')],
            ['quality', this.t('sec.quality', 'Power quality')],
            ['site', this.t('sec.site', 'Site')],
            ['controls', this.t('sec.controls', 'Controls')],
            ['status', this.t('sec.status', 'Status')],
            ['alarms', this.t('sec.alarms', 'Alarms')],
            ['other', this.t('sec.other', 'Other')],
        ];
    },

    // noise-prone groups fold by default (HA folds Diagnostics the same way);
    // the operator's toggles stick per device+section
    _secOpen(dev, sec) {
        const def = !(sec === 'alarms' || sec === 'status' || sec === 'other' || sec === 'diagnostics');
        try {
            const v = localStorage.getItem(`mbg-sec-${dev}-${sec}`);
            return v == null ? def : v === '1';
        } catch (e) { return def; }
    },

    renderDeviceSections(allRegs, heroRegs) {
        const box = document.getElementById('deviceSections');
        if (!box) return;
        const dev = this._dashDeviceId();
        const heroSet = new Set(heroRegs.map(r => r.address));
        const rest = allRegs.filter(r => !heroSet.has(r.address));
        const key = dev + '|' + rest.map(r => r.address).join(',');

        if (this._sectionsKey !== key) {
            this._sectionsKey = key;
            this._sectionCells = {};
            if (!rest.length) { box.innerHTML = ''; return; }
            const groups = new Map();
            rest.forEach(r => {
                const c = this._sectionCat(r);
                if (!groups.has(c)) groups.set(c, []);
                groups.get(c).push(r);
            });
            // categories the dashboard does not know by name come from the
            // template, labelled as it labels them, before "Other"
            const known = this._sectionMeta();
            const knownIds = new Set(known.map(([id]) => id));
            const extra = [...groups.keys()].filter(id => !knownIds.has(id)).map(id => {
                const r = groups.get(id).find(x => x.category_label) || {};
                return [id, r.category_label || (id.charAt(0).toUpperCase() + id.slice(1))];
            });
            const ordered = known.filter(([id]) => id !== 'other').concat(extra, known.filter(([id]) => id === 'other'));
            const html = ordered
                .filter(([id]) => groups.has(id))
                .map(([id, label]) => {
                    const regs = groups.get(id);
                    const widget = ((this.dashDisplay || {}).sections || {})[id]?.widget || '';
                    const rowHtml = r => `
                        <tr data-address="${r.address}" tabindex="0">
                            <td class="ds-label" title="${this._esc(r.name)}">${this._esc(r.label || r.name)}</td>
                            <td class="ds-value"><span class="table-value value-normal">--</span></td>
                            <td class="ds-unit"></td>
                        </tr>`;
                    let body;
                    if (widget === 'grid') {
                        // the template asks for a grid (a pack's cells): the
                        // fields sharing the section's main unit become tiles,
                        // the rest (a mask, a count) stay as rows below
                        const units = {};
                        regs.forEach(r => { units[r.unit || ''] = (units[r.unit || ''] || 0) + 1; });
                        const main = Object.entries(units).sort((a, b) => b[1] - a[1])[0]?.[0];
                        const tiles = regs.filter(r => (r.unit || '') === main && main);
                        const others = regs.filter(r => !tiles.includes(r));
                        body = `<div class="cell-grid">${tiles.map(r => `
                            <div class="cell-tile" data-address="${r.address}" data-grid="${id}" tabindex="0" role="button" title="${this._esc(r.name)}">
                                <div class="ct-label">${this._esc(r.label || r.name)}</div>
                                <div class="ct-val"><span class="table-value value-normal">--</span> <span class="ds-unit"></span></div>
                            </div>`).join('')}</div>`
                            + (others.length ? `<table class="dev-sec-table"><tbody>${others.map(rowHtml).join('')}</tbody></table>` : '');
                    } else {
                        body = `<table class="dev-sec-table"><tbody>${regs.map(rowHtml).join('')}
                            ${widget === 'active_only' ? `<tr class="ds-none" hidden><td colspan="3">${this._esc(this.t('dash.noActive', 'Nothing active'))}</td></tr>` : ''}</tbody></table>`;
                    }
                    return `
                    <details class="dev-section" data-sec="${id}" data-widget="${widget}" ${this._secOpen(dev, id) ? 'open' : ''}>
                        <summary><i aria-hidden="true" class="bi bi-chevron-right"></i>
                            ${this._esc(label)} <span class="dev-sec-count">${regs.length}</span></summary>
                        ${body}
                    </details>`;
                }).join('');
            box.innerHTML = html;
            // cache the live cells once — the per-tick update touches text only
            box.querySelectorAll('tr[data-address], .cell-tile[data-address]').forEach(el => {
                const sec = el.closest('details.dev-section');
                this._sectionCells[el.dataset.address] = {
                    val: el.querySelector('.table-value'),
                    unit: el.querySelector('.ds-unit'),
                    el, grid: el.classList.contains('cell-tile'),
                    activeOnly: sec?.dataset.widget === 'active_only', sec,
                };
            });
            this._wireDeviceSections(box, dev);
        }

        // value pass (every tick, in place)
        const store = this._dashStore();
        const bitmasks = (this.dashDisplay || {}).bitmasks || {};
        const gridVals = new Map();         // grid section → [[tile, value]]
        const active = new Map();           // active-only section → active rows
        rest.forEach(r => {
            const cell = this._sectionCells[String(r.address)];
            if (!cell || !cell.val) return;
            const numValue = store[r.address]?.value;
            let disp = this._displayValue(numValue, r);
            if (bitmasks[r.name] && typeof numValue === 'number') {
                disp = { text: this._bitList(numValue, bitmasks[r.name]), unit: '' };
            }
            if (cell.val.textContent !== disp.text) cell.val.textContent = disp.text;
            const cls = 'table-value ' + (this.getValueColorClass(numValue, r) || 'value-normal');
            if (cell.val.className !== cls) cell.val.className = cls;
            if (cell.unit.textContent !== disp.unit) cell.unit.textContent = disp.unit;
            if (cell.grid && typeof numValue === 'number') {
                const k = cell.sec; if (!gridVals.has(k)) gridVals.set(k, []);
                gridVals.get(k).push([cell.el, numValue]);
            }
            if (cell.activeOnly) {
                const on = this._isActiveValue(numValue);
                if (cell.el.hidden === on) cell.el.hidden = !on;
                active.set(cell.sec, (active.get(cell.sec) || 0) + (on ? 1 : 0));
            }
        });
        // a grid marks its lowest and highest (the weak and the full cell)
        gridVals.forEach(list => {
            const vs = list.map(x => x[1]);
            const lo = Math.min(...vs), hi = Math.max(...vs), spread = hi > lo;
            list.forEach(([el, v]) => {
                el.classList.toggle('ct-min', spread && v === lo);
                el.classList.toggle('ct-max', spread && v === hi);
            });
        });
        // an active-only section shows what is active, or says nothing is
        active.forEach((n, sec) => {
            const none = sec.querySelector('.ds-none');
            if (none) none.hidden = n > 0;
            const cnt = sec.querySelector('.dev-sec-count');
            if (cnt && cnt.textContent !== String(n)) cnt.textContent = String(n);
            sec.classList.toggle('sec-alert', n > 0);
        });
    },

    // a bitmask as the list of what is set: 9 → "Cell 1, Cell 4"; 0 → "—"
    _bitList(v, pattern) {
        const n = Math.trunc(v);
        if (!n) return '—';
        const out = [];
        for (let i = 0; i < 32; i++) if (n & (2 ** i)) out.push(String(pattern).replace('{n}', i + 1));
        return out.join(', ');
    },

    _isActiveValue(v) {
        if (v == null || typeof v === 'boolean') return !!v;
        if (typeof v === 'number') return v !== 0;
        return !['', '0', 'off', 'none', 'normal', 'ok', 'false', '—'].includes(String(v).trim().toLowerCase());
    },

    _wireDeviceSections(box, dev) {
        if (!box._secWired) {
            box._secWired = true;
            box.addEventListener('click', (e) => {
                const tr = e.target.closest('tr[data-address], .cell-tile[data-address]');
                if (tr) this.openValueHistory(tr.dataset.address);
            });
            box.addEventListener('keydown', (e) => this._historyKey(e, 'tr[data-address], .cell-tile[data-address]'));
        }
        // 'toggle' does not bubble — capture it; persist per device+section
        if (!box._secToggleWired) {
            box._secToggleWired = true;
            box.addEventListener('toggle', (e) => {
                const d = e.target;
                if (!d.matches || !d.matches('.dev-section')) return;
                const sec = d.dataset.sec;
                const devNow = this._dashDeviceId();
                try { localStorage.setItem(`mbg-sec-${devNow}-${sec}`, d.open ? '1' : '0'); }
                catch (err) { /* private mode */ }
            }, true);
        }
    },

    updateGaugeWidget(card, reg, value) {
        const { min, max } = this.getGaugeRange(reg);
        const color = this.getGaugeColor(value, reg);

        // Update gauge arc
        let percent = 0;
        if (typeof value === 'number' && max > min) {
            percent = Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100));
        }

        const radius = 45;
        const circumference = Math.PI * radius;
        const offset = circumference - (percent / 100) * circumference;

        const valuePath = card.querySelector('.gauge-value');
        if (valuePath) {
            valuePath.style.strokeDashoffset = offset;
            valuePath.style.stroke = color;
        }

        // Update number and unit display
        const numberEl = card.querySelector('.gauge-number');
        const unitEl = card.querySelector('.gauge-unit');
        if (numberEl) {
            const fmt = this.formatValueWithUnit(value, reg.unit);
            const displayValue = this._fmtNum(fmt.value, fmt.decimals);
            if (numberEl.textContent !== displayValue) {
                numberEl.textContent = displayValue;
            }
            if (unitEl && unitEl.textContent !== fmt.unit) {
                unitEl.textContent = fmt.unit;
            }
        }
    },

    // Shared sparkline geometry (dashboard SVG widgets). Returns the SVG path
    // string + the value range used for the min/max labels.
    _sparkPath(history, width = 200, height = 60) {
        const values = history.map(h => h.value);
        const minVal = Math.min(...values);
        const maxVal = Math.max(...values);
        const range = maxVal - minVal || 1;
        const pathD = 'M ' + history.map((h, i) =>
            `${(i / (history.length - 1)) * width},${height - ((h.value - minVal) / range) * height}`
        ).join(' L ');
        return { pathD, minVal, maxVal };
    },

    updateChartWidget(card, reg) {
        const history = this.valueHistory[String(reg.address)] || [];

        // Check if we need to replace placeholder with actual chart
        const placeholder = card.querySelector('.chart-placeholder');
        if (placeholder && history.length >= 2) {
            // Replace entire widget content with chart
            const chartContainer = card.querySelector('.widget-chart');
            if (chartContainer) {
                chartContainer.innerHTML = this.getChartContent(reg, history);
            }
            return;
        }

        if (history.length < 2) {
            return; // Not enough data yet
        }

        const { pathD, minVal, maxVal } = this._sparkPath(history);

        // Update path
        const pathEl = card.querySelector('.chart-line');
        if (pathEl) {
            pathEl.setAttribute('d', pathD);
        }

        // Update current value
        const currentEl = card.querySelector('.chart-current');
        if (currentEl) {
            const currentValue = history[history.length - 1]?.value;
            const displayValue = this._fmtNum(currentValue, 2);
            currentEl.innerHTML = `${displayValue} <span>${this._esc(reg.unit)}</span>`;
        }

        // Update range
        const rangeEl = card.querySelector('.chart-range');
        if (rangeEl) {
            rangeEl.innerHTML = `<span>${minVal.toFixed(1)}</span><span>${maxVal.toFixed(1)}</span>`;
        }
    },

    getChartContent(reg, history) {
        const { pathD, minVal, maxVal } = this._sparkPath(history);

        const currentValue = history[history.length - 1]?.value;
        const displayValue = this._fmtNum(currentValue, 2);

        return `
            <div class="chart-current">${displayValue} <span>${this._esc(reg.unit)}</span></div>
            <svg viewBox="0 0 200 60" class="chart-svg" preserveAspectRatio="none">
                <path class="chart-line" d="${pathD}" />
            </svg>
            <div class="chart-range">
                <span>${minVal.toFixed(1)}</span>
                <span>${maxVal.toFixed(1)}</span>
            </div>
        `;
    },

    getGaugeRange(reg) {
        // Derive min/max from thresholds if not set in ui_config
        let min = reg.ui_config?.min;
        let max = reg.ui_config?.max;

        if ((min == null || max == null) && reg.thresholds && reg.thresholds.enabled) {
            const t = reg.thresholds;
            const vals = [t.dangerLow, t.warningLow, t.warningHigh, t.dangerHigh].filter(v => v != null);
            if (vals.length > 0) {
                const tMin = Math.min(...vals);
                const tMax = Math.max(...vals);
                const margin = (tMax - tMin) * 0.15;
                if (min == null) min = Math.floor(tMin - margin);
                if (max == null) max = Math.ceil(tMax + margin);
            }
        }

        return { min: min ?? 0, max: max ?? 100 };
    },

    getGaugeColor(value, reg) {
        // Use thresholds for color if available
        if (typeof value === 'number' && reg.thresholds && reg.thresholds.enabled) {
            const colorClass = this.getValueColorClass(value, reg);
            const colorMap = {
                'value-danger': 'var(--danger-text)',
                'value-warning': 'var(--warning-text)',
                'value-success': 'var(--success-text)',
                'value-normal': reg.ui_config?.color || 'var(--accent)',
            };
            return colorMap[colorClass] || colorMap['value-normal'];
        }
        return reg.ui_config?.color || 'var(--accent)';
    },

    renderGaugeWidget(reg, value) {
        const { min, max } = this.getGaugeRange(reg);
        const color = this.getGaugeColor(value, reg);

        // Calculate percentage (0-100) using raw value against raw range
        let percent = 0;
        if (typeof value === 'number' && max > min) {
            percent = Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100));
        }

        // SVG arc parameters
        const radius = 45;
        const circumference = Math.PI * radius; // Semi-circle
        const offset = circumference - (percent / 100) * circumference;

        // Format display value with unit scaling
        const fmt = this.formatValueWithUnit(value, reg.unit);
        const displayValue = this._fmtNum(fmt.value, fmt.decimals);
        const displayUnit = fmt.unit;

        return `
            <div class="widget-gauge">
                <svg viewBox="0 0 100 60" class="gauge-svg">
                    <!-- Background arc -->
                    <path class="gauge-bg" d="M 5 55 A 45 45 0 0 1 95 55" />
                    <!-- Value arc -->
                    <path class="gauge-value" d="M 5 55 A 45 45 0 0 1 95 55"
                          style="stroke: ${this._esc(color)}; stroke-dasharray: ${circumference}; stroke-dashoffset: ${offset};" />
                </svg>
                <div class="gauge-reading">
                    <span class="gauge-number">${displayValue}</span>
                    <span class="gauge-unit">${this._esc(displayUnit)}</span>
                </div>
                <div class="gauge-range">
                    <span>${this._esc(String(min))}</span>
                    <span>${this._esc(String(max))}</span>
                </div>
            </div>
        `;
    },

    renderChartWidget(reg) {
        const history = this.valueHistory[String(reg.address)] || [];
        const canvasId = `chart-${reg.address}`;

        if (history.length < 2) {
            return `
                <div class="widget-chart">
                    <div class="chart-placeholder">${this.t('msg.collecting', "Collecting data...")}</div>
                </div>
            `;
        }

        const { pathD, minVal, maxVal } = this._sparkPath(history);

        const currentValue = history[history.length - 1]?.value;
        const fmt = this.formatValueWithUnit(currentValue, reg.unit);
        const displayValue = this._fmtNum(fmt.value, fmt.decimals);
        const fmtMin = this.formatValueWithUnit(minVal, reg.unit);
        const fmtMax = this.formatValueWithUnit(maxVal, reg.unit);

        return `
            <div class="widget-chart">
                <div class="chart-current">${displayValue} <span>${this._esc(fmt.unit)}</span></div>
                <svg viewBox="0 0 200 60" class="chart-svg" preserveAspectRatio="none">
                    <path class="chart-line" d="${pathD}" />
                </svg>
                <div class="chart-range">
                    <span>${typeof fmtMin.value === 'number' ? fmtMin.value.toFixed(fmtMin.decimals) : minVal.toFixed(1)} ${fmtMin.unit !== reg.unit ? fmtMin.unit : ''}</span>
                    <span>${typeof fmtMax.value === 'number' ? fmtMax.value.toFixed(fmtMax.decimals) : maxVal.toFixed(1)} ${fmtMax.unit !== reg.unit ? fmtMax.unit : ''}</span>
                </div>
            </div>
        `;
    },

    // ============ Customize Dashboard ============

    // Overwrite every dashboard widget's color with the convention default —
    // the explicit, user-triggered path (defaults otherwise apply only to NEW
    // widgets). Uses the same save pipeline as Customize.
    async reapplyDefaultColors(btn) {
        if (!confirm(this.t('dash.reapplyConfirm',
                'Overwrite ALL dashboard widget colors with the defaults from Settings → General?'))) return;
        if (btn) btn.disabled = true;
        try {
            this._dashRegs().forEach(reg => {
                if (!reg.ui_show_on_dashboard) return;
                reg.ui_config = reg.ui_config || {};
                reg.ui_config.color = this._defaultColorFor(reg);
            });
            await this._saveDashRegisters();
            this.updateDashboard(true);
            this.showToast('success', this.t('dash.reapplyColors', 'Reapply default colors'),
                           this.t('toast.done', 'done'));
        } catch (e) {
            this.showToast('error', this.t('toast.saveFailed', 'Save failed'), this._errMsg(e));
        } finally { if (btn) btn.disabled = false; }
    },

    // ── Device switcher: a button naming the current device, a searchable
    // panel to jump to another (the VRM installation-switcher pattern). The
    // old chips row stopped scaling past a handful of devices.
    async renderDashDeviceChips() {
        const wrap = document.getElementById('deviceSwitcherWrap');
        if (!wrap) return;
        let devices = [];
        try { devices = (await this._fetchDevices(true)) || []; } catch (e) {}
        this._dashDevList = devices;
        if (devices.length < 2) { wrap.hidden = true; return; }   // one device → no switcher
        // button face: where am I, and is it healthy
        const health = {};
        (this.status?.devices || []).forEach(d => { health[d.id] = d.data_health; });
        const active = this._dashDeviceId();
        const cur = devices.find(d => d.id === active);
        const dot = document.getElementById('deviceSwitcherDot');
        const name = document.getElementById('deviceSwitcherName');
        if (dot) {
            const h = health[active] || 'idle';
            dot.style.background = this._healthDot(h);
            dot.title = h;
        }
        if (name) name.textContent = (cur && (cur.name || cur.id)) || active;
        this._wireDeviceSwitcher(wrap);
        this._renderDeviceSwitchList(devices, health, active);
    },

    _healthDot(h) {
        const map = { ok: 'var(--success)', degraded: 'var(--warning)',
                      stale: 'var(--warning)', down: 'var(--danger)' };
        return map[h] || 'var(--text-tertiary)';
    },

    _renderDeviceSwitchList(devices, health, active) {
        const list = document.getElementById('deviceSwitchList');
        if (!list) return;
        const row = (d, label) => `
            <button type="button" class="dsw-row ${d.id === active ? 'active' : ''}"
                    role="option" aria-selected="${d.id === active}" data-id="${this._esc(d.id)}"
                    data-search="${this._esc(((d.name || '') + ' ' + d.id).toLowerCase())}">
                <span class="dot" style="background:${this._healthDot(health[d.id])}"></span>
                <span class="dsw-name">${this._esc(label)}</span>
                ${(health[d.id] && health[d.id] !== 'ok')
                    ? `<span class="dsw-health">${this._esc(health[d.id])}</span>` : ''}
            </button>`;
        const live = devices.filter(d => d.enabled !== false);
        const standalone = live.filter(d => !d.endpoint_id);
        const byEp = new Map();
        live.filter(d => d.endpoint_id).forEach(d => {
            if (!byEp.has(d.endpoint_id)) byEp.set(d.endpoint_id, []);
            byEp.get(d.endpoint_id).push(d);
        });
        list.innerHTML = standalone.map(d => row(d, d.name || d.id)).join('')
            + [...byEp.entries()].map(([pid, units]) => {
                const epName = units[0].endpoint_name || pid;
                return `<div class="dsw-group" data-group>
                    <div class="dsw-group-name"><i aria-hidden="true" class="bi bi-diagram-3"></i> ${this._esc(epName)}</div>
                    ${units.map(d => row(d, d.name || d.id)).join('')}</div>`;
            }).join('');
    },

    _deviceSwitchOpen(open) {
        const panel = document.getElementById('deviceSwitchPanel');
        const btn = document.getElementById('deviceSwitcherBtn');
        if (!panel || !btn) return;
        panel.hidden = !open;
        btn.setAttribute('aria-expanded', String(open));
        if (open) {
            const s = document.getElementById('deviceSwitchSearch');
            if (s) { s.value = ''; this._filterDeviceSwitch(''); s.focus(); }
        }
    },

    _filterDeviceSwitch(q) {
        const list = document.getElementById('deviceSwitchList');
        if (!list) return;
        const needle = q.trim().toLowerCase();
        list.querySelectorAll('.dsw-row').forEach(r => {
            r.hidden = !!needle && !(r.dataset.search || '').includes(needle);
        });
        // a group header with every unit filtered out disappears with them
        list.querySelectorAll('[data-group]').forEach(g => {
            g.hidden = ![...g.querySelectorAll('.dsw-row')].some(r => !r.hidden);
        });
    },

    _wireDeviceSwitcher(wrap) {
        if (wrap._dswWired) return;
        wrap._dswWired = true;
        const btn = document.getElementById('deviceSwitcherBtn');
        const panel = document.getElementById('deviceSwitchPanel');
        const search = document.getElementById('deviceSwitchSearch');
        if (btn) btn.addEventListener('click', () =>
            this._deviceSwitchOpen(panel ? panel.hidden : true));
        if (search) search.addEventListener('input', () =>
            this._filterDeviceSwitch(search.value));
        if (panel) panel.addEventListener('click', (e) => {
            const r = e.target.closest('.dsw-row');
            if (!r) return;
            this._deviceSwitchOpen(false);
            this.switchDashDevice(r.dataset.id);
        });
        document.addEventListener('click', (e) => {
            if (panel && !panel.hidden && !wrap.contains(e.target)) this._deviceSwitchOpen(false);
        });
        wrap.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && panel && !panel.hidden) {
                this._deviceSwitchOpen(false);
                if (btn) btn.focus();
            }
        });
    },

    async switchDashDevice(id) {
        if (id === this._dashDeviceId()) return;
        await this._setDashDevice(id);
        // rebuild the grid from scratch — widgets belong to another device now
        const grid = document.getElementById('dashboardGrid');
        if (grid) grid.innerHTML = '';
        this.updateDashboard();
        this.renderDashDeviceChips();
    },

    // Persist the DASHBOARD device's register list (?device= for non-primary).
    async _saveDashRegisters() {
        const qs = this._dashIsPrimary() ? '' : ('?device=' + encodeURIComponent(this._dashDeviceId()));
        const r = await fetch('/api/registers/selected' + qs, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(this._dashRegs()) });
        if (!r.ok) throw new Error('HTTP ' + r.status);
    },

    openCustomizeDashModal() {
        const modal = document.getElementById('customizeDashModal');
        const list = document.getElementById('customizeList');

        // Sort by dashboard_order if exists
        const sortedRegs = [...this._dashRegs()].sort((a, b) => {
            const orderA = a.ui_config?.dashboard_order ?? 999;
            const orderB = b.ui_config?.dashboard_order ?? 999;
            return orderA - orderB;
        });

        // Build list of all selected registers
        let html = '';
        sortedRegs.forEach((reg, index) => {
            const checked = reg.ui_show_on_dashboard ? 'checked' : '';
            const widgetType = reg.ui_widget || 'value';
            const isWide = reg.ui_config?.wide ? 'active' : '';

            html += `
                <div class="customize-item" data-address="${reg.address}" draggable="true">
                    <i aria-hidden="true" class="bi bi-grip-vertical customize-drag-handle"></i>
                    <input type="checkbox" data-address="${reg.address}" ${checked} aria-label="${this._esc(reg.label || reg.name)}">
                    <div class="customize-item-info">
                        <div class="customize-item-label">${this._esc(reg.label || reg.name)}</div>
                        <div class="customize-item-details">${this._esc(reg.name)} · ${this._esc(reg.unit || 'N/A')}</div>
                    </div>
                    <div class="customize-item-controls">
                        <select class="customize-select" data-address="${reg.address}" data-field="widget" aria-label="${this._esc(this.t('dash.widgetType', 'Widget type'))} — ${this._esc(reg.label || reg.name)}">
                            <option value="value" ${widgetType === 'value' ? 'selected' : ''}>${this.t('lbl.value', "Value")}</option>
                            <option value="gauge" ${widgetType === 'gauge' ? 'selected' : ''}>${this.t('lbl.gauge', "Gauge")}</option>
                            <option value="chart" ${widgetType === 'chart' ? 'selected' : ''}>${this.t('lbl.chart', "Chart")}</option>
                        </select>
                        <button class="customize-size-toggle ${isWide}" data-address="${reg.address}" title="Wide widget" aria-pressed="${isWide ? 'true' : 'false'}">
                            <i aria-hidden="true" class="bi bi-arrows-expand"></i> Wide
                        </button>
                        <button class="btn-action customize-move" data-dir="-1" title="${this._esc(this.t('dash.moveUp', 'Move up'))}" aria-label="${this._esc(this.t('dash.moveUp', 'Move up'))} — ${this._esc(reg.label || reg.name)}"><i aria-hidden="true" class="bi bi-chevron-up"></i></button>
                        <button class="btn-action customize-move" data-dir="1" title="${this._esc(this.t('dash.moveDown', 'Move down'))}" aria-label="${this._esc(this.t('dash.moveDown', 'Move down'))} — ${this._esc(reg.label || reg.name)}"><i aria-hidden="true" class="bi bi-chevron-down"></i></button>
                    </div>
                </div>
            `;
        });

        if (this._dashRegs().length === 0) {
            html = `<div class="empty-state">${this.t('msg.noMonitoredAdd', "No measurements monitored. Add measurements first.")}</div>`;
        }

        list.innerHTML = html;

        // Setup drag-drop
        this.setupCustomizeDragDrop(list);

        // Setup size toggle buttons
        list.querySelectorAll('.customize-size-toggle').forEach(btn => {
            btn.addEventListener('click', () => {
                btn.setAttribute('aria-pressed', String(btn.classList.toggle('active')));
            });
        });

        // keyboard alternative to the drag reorder (the drag was the ONLY way
        // to reorder — unusable without a pointer)
        list.querySelectorAll('.customize-move').forEach(btn => {
            btn.addEventListener('click', () => {
                const item = btn.closest('.customize-item');
                const sib = +btn.dataset.dir < 0 ? item.previousElementSibling : item.nextElementSibling;
                if (!sib || !sib.classList.contains('customize-item')) return;
                if (+btn.dataset.dir < 0) sib.before(item); else sib.after(item);
                btn.focus();
            });
        });

        this.openModal('customizeDashModal');
    },

    setupCustomizeDragDrop(list) {
        let draggedItem = null;

        list.querySelectorAll('.customize-item').forEach(item => {
            item.addEventListener('dragstart', (e) => {
                draggedItem = item;
                item.classList.add('dragging');
                e.dataTransfer.effectAllowed = 'move';
            });

            item.addEventListener('dragend', () => {
                item.classList.remove('dragging');
                list.querySelectorAll('.customize-item').forEach(i => i.classList.remove('drag-over'));
                draggedItem = null;
            });

            item.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                if (item !== draggedItem) {
                    item.classList.add('drag-over');
                }
            });

            item.addEventListener('dragleave', () => {
                item.classList.remove('drag-over');
            });

            item.addEventListener('drop', (e) => {
                e.preventDefault();
                item.classList.remove('drag-over');
                if (draggedItem && draggedItem !== item) {
                    const allItems = [...list.querySelectorAll('.customize-item')];
                    const draggedIdx = allItems.indexOf(draggedItem);
                    const targetIdx = allItems.indexOf(item);

                    if (draggedIdx < targetIdx) {
                        item.parentNode.insertBefore(draggedItem, item.nextSibling);
                    } else {
                        item.parentNode.insertBefore(draggedItem, item);
                    }
                }
            });
        });
    },

    closeCustomizeDashModal() {
        this.closeModal('customizeDashModal');
    },

    async saveCustomizeDash() {
        const list = document.getElementById('customizeList');
        const items = list.querySelectorAll('.customize-item');
        const btn = document.getElementById('customizeDashSave');

        // Update selectedRegisters based on order, visibility, widget type, and size
        items.forEach((item, index) => {
            const address = parseInt(item.dataset.address);
            const reg = this._dashRegs().find(r => r.address === address);
            if (reg) {
                // Visibility
                const checkbox = item.querySelector('input[type="checkbox"]');
                reg.ui_show_on_dashboard = checkbox?.checked ?? false;

                // Widget type
                const widgetSelect = item.querySelector('.customize-select');
                if (widgetSelect) {
                    reg.ui_widget = widgetSelect.value;
                }

                // Size (wide)
                const sizeToggle = item.querySelector('.customize-size-toggle');
                if (!reg.ui_config) reg.ui_config = {};
                reg.ui_config.wide = sizeToggle?.classList.contains('active') ?? false;

                // Order
                reg.ui_config.dashboard_order = index;
            }
        });

        this.setButtonLoading(btn, true);

        // Save to server
        try {
            const qs = this._dashIsPrimary() ? '' : ('?device=' + encodeURIComponent(this._dashDeviceId()));
            const response = await fetch('/api/registers/selected' + qs, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(this._dashRegs())
            });

            if (!response.ok) throw new Error('Failed to save');

            this.closeCustomizeDashModal();
            this.showToast('success', this.t('toast.dashboardUpdated', 'Dashboard Updated'), this.t('toast.layoutSaved', 'Layout and settings saved'));

            // Refresh dashboard - clear and recreate
            document.getElementById('dashboardGrid').innerHTML = '';
            this.updateDashboard();

        } catch (error) {
            this.showToast('error', this.t('toast.saveFailed', 'Save Failed'), error.message);
        } finally {
            this.setButtonLoading(btn, false, 'Save');
        }
    }
});
