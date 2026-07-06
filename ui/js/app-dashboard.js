// dashboard domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    // ============ Value Color Coding ============

    /**
     * Detect measurement type from unit and name for template selection
     * Returns template key: voltage_ln, voltage_ll, frequency, power_factor, thd, current, power
     */
    detectMeasurementType(unit, name) {
        const unitLower = (unit || '').toLowerCase();
        const nameLower = (name || '').toLowerCase();

        // Voltage detection - distinguish L-N from L-L
        if (unitLower === 'v' || nameLower.includes('voltage') || nameLower.match(/u[_]?l/)) {
            // L-L voltage: Ull, U_ll, voltage_l1_l2, etc.
            if (nameLower.includes('ll') || nameLower.match(/l\d[_-]?l\d/) || nameLower.includes('_ll')) {
                return 'voltage_ll';
            }
            // L-N voltage: Uln, U_ln, voltage_l1_n, etc.
            return 'voltage_ln';
        }
        // Frequency detection
        if (unitLower === 'hz' || nameLower.includes('freq')) {
            return 'frequency';
        }
        // Power Factor detection
        if (nameLower.includes('power_factor') || nameLower.includes('cos') || nameLower.includes('pf')) {
            return 'power_factor';
        }
        // THD detection
        if (nameLower.includes('thd') || unitLower === '%thd' || unitLower === '% thd') {
            return 'thd';
        }
        // Current detection
        if (unitLower === 'a' || nameLower.includes('current') || nameLower.match(/i[_]?l/)) {
            return 'current';
        }
        // Power detection
        if (unitLower === 'w' || unitLower === 'kw' || unitLower === 'mw' || unitLower === 'va' || unitLower === 'kva' ||
            nameLower.includes('power') || nameLower.match(/p[_]?l/) || nameLower.match(/s[_]?l/)) {
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
            detectedDiv.innerHTML = `Detected: <span class="detected-type">${typeNames[template.templateType] || template.templateType}</span> - thresholds auto-filled${unitLine}`;
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
    },

    openValueHistory(address) {
        const reg = (this.selectedRegisters || []).find(r => String(r.address) === String(address));
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
        try {
            const r = await fetch(`/api/history?name=${encodeURIComponent(reg.name)}&start=${encodeURIComponent(range)}&every=${every}&fn=all`);
            if (r.status === 503) { if (info) info.textContent = this.t('valhist.needInflux'); return; }
            if (!r.ok) { if (info) info.textContent = `HTTP ${r.status}`; return; }
            const d = await r.json();
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
        const grid = document.getElementById('dashboardGrid');

        // Per-device empty state: distinguish "no widgets picked" from "source
        // is down" so the operator isn't guessing which problem they have.
        const dashRegs = this._dashRegs();
        if (!dashRegs.length && !this._dashIsPrimary()) {
            const dev = this._dashDeviceId();
            const h = (this.status?.devices || []).find(d => d.id === dev)?.data_health;
            if (grid) grid.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">${h === 'down' ? '📵' : '📊'}</div>
                    <div class="empty-state-title">${h === 'down'
                        ? this.t('dash.deviceDown', 'Source is not responding')
                        : this.t('dash.noDeviceWidgets', 'No measurements on this dashboard yet')}</div>
                    <div class="empty-state-desc">${h === 'down'
                        ? this.t('dash.deviceDownDesc', 'The device is unreachable — check Status for details.')
                        : this.t('dash.noDeviceWidgetsDesc', 'Pick measurements for this device to see them here.')}</div>
                    <button class="empty-state-action" ${this._act('jumpToDeviceRegisters', [dev])}>
                        📋 ${this.t('dash.goMeasurements', 'Go to Measurements')}</button>
                </div>`;
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

        // Check if we should render table view or cards view
        if (this.dashboardView === 'table') {
            this.renderDashboardTable(grid, dashboardRegs);
            return;
        }

        // Cards view (default)
        // Get current widget addresses (data-address is on the wrapper div)
        const existingWidgets = new Map();
        grid.querySelectorAll('.widget-card[data-address]').forEach(el => {
            existingWidgets.set(parseInt(el.dataset.address), el);
        });

        // Track which addresses should exist
        const targetAddresses = new Set(dashboardRegs.map(r => r.address));

        // Remove widgets that shouldn't exist anymore
        existingWidgets.forEach((el, addr) => {
            if (!targetAddresses.has(addr)) {
                el.remove();
            }
        });

        // Remove empty state if it exists and we have registers
        const emptyState = grid.querySelector('.empty-state');
        if (emptyState && dashboardRegs.length > 0) {
            emptyState.remove();
        }

        // Dominant poll group of the set — cards badge only the exceptions.
        this._dashDominantGroup = this._dominantPollGroup(dashboardRegs);

        // Update or create widgets in correct order
        dashboardRegs.forEach((reg, index) => {
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

        // Show empty state if no registers
        if (dashboardRegs.length === 0 && !grid.querySelector('.empty-state')) {
            grid.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">📊</div>
                    <div class="empty-state-title">${this.t('msg.noWidgets', "No widgets on dashboard")}</div>
                    <div class="empty-state-desc">
                        Add measurements to your dashboard to monitor values in real-time.
                    </div>
                    <button class="empty-state-action" onclick="app.jumpToDeviceRegisters(app._primaryDeviceId())">
                        📋 Go to Measurements
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

    createWidgetCard(reg, numValue) {
        const fmt = this.formatValueWithUnit(numValue, reg.unit);
        const displayValue = this._fmtNum(fmt.value, fmt.decimals);

        // Widget card (CSS Grid handles responsive layout)
        const card = document.createElement('div');
        card.className = `widget-card widget-${reg.ui_widget || 'value'}`;
        if (reg.ui_config?.wide) card.classList.add('widget-wide');
        card.dataset.address = reg.address;

        // Poll-group badge only when this row DIFFERS from the dashboard's
        // dominant group — the majority chip is noise, the exception is signal.
        const dom = this._dashDominantGroup;
        const pollBadge = (reg.poll_group && reg.poll_group !== dom)
            ? `<span class="badge poll-${this._esc(reg.poll_group)}" title="${this.t('dash.pollGroup', 'Poll group')}">${this._esc(reg.poll_group)}</span>`
            : '';

        // Header with edit button
        const header = `
            <div class="widget-header">
                <div class="widget-header-left">
                    <span class="widget-label">${this._esc(reg.label)}</span>
                </div>
                <div class="widget-header-right">
                    ${pollBadge}
                    <button class="widget-edit-btn" title="Edit widget">
                        <i class="bi bi-pencil"></i>
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
                        <span class="value-number ${colorClass}">${displayValue}</span><span class="widget-unit">${this._esc(fmt.unit)}</span>
                    </div>
                `;
        }

        // Footer with register name
        const footer = `<div class="widget-footer">${this._esc(reg.name)}</div>`;

        card.innerHTML = header + content + footer;

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
                    const fmt = this.formatValueWithUnit(numValue, reg.unit);
                    const displayValue = this._fmtNum(fmt.value, fmt.decimals);
                    if (valueEl.textContent !== displayValue) {
                        valueEl.textContent = displayValue;
                    }
                    // Update unit display (may change with scaling)
                    const unitEl = card.querySelector('.widget-unit');
                    if (unitEl && unitEl.textContent !== fmt.unit) {
                        unitEl.textContent = fmt.unit;
                    }
                    // Update color class
                    const newColorClass = this.getValueColorClass(numValue, reg);
                    valueEl.classList.remove('value-normal', 'value-warning', 'value-danger', 'value-success');
                    valueEl.classList.add(newColorClass);
                }
        }
    },

    // ============ Dashboard Table View ============

    toggleDashboardView() {
        this.dashboardView = this.dashboardView === 'cards' ? 'table' : 'cards';
        localStorage.setItem('janitza-dashboard-view', this.dashboardView);

        // Update toggle button icon
        const toggleBtn = document.getElementById('dashboardViewToggle');
        if (toggleBtn) {
            const icon = toggleBtn.querySelector('i');
            if (icon) {
                icon.className = this.dashboardView === 'table' ? 'bi bi-grid-3x3-gap' : 'bi bi-table';
            }
        }

        // Force full re-render
        const grid = document.getElementById('dashboardGrid');
        grid.innerHTML = '';
        this.updateDashboard();
    },

    renderDashboardTable(container, registers) {
        // Remove any existing cards (switching from cards to table)
        const existingCards = container.querySelectorAll('.widget-card');
        existingCards.forEach(el => el.remove());

        // Check for existing table
        let table = container.querySelector('.dashboard-table');

        if (!table) {
            // Create table structure
            container.innerHTML = `
                <div class="table-container dashboard-table-container">
                    <table class="dashboard-table">
                        <thead>
                            <tr>
                                <th>${this.t('lbl.label', "Label")}</th>
                                <th>${this.t('lbl.value', "Value")}</th>
                                <th>${this.t('lbl.unit', "Unit")}</th>
                                <th>${this.t('lbl.pollGroup', "Poll Group")}</th>
                                <th>${this.t('lbl.actions', "Actions")}</th>
                            </tr>
                        </thead>
                        <tbody id="dashboardTableBody"></tbody>
                    </table>
                </div>
            `;
            table = container.querySelector('.dashboard-table');
        }

        const tbody = container.querySelector('#dashboardTableBody');

        if (registers.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">📊</div>
                    <div class="empty-state-title">${this.t('msg.noWidgets', "No widgets on dashboard")}</div>
                    <div class="empty-state-desc">
                        Add measurements to your dashboard to monitor values in real-time.
                    </div>
                    <button class="empty-state-action" onclick="app.jumpToDeviceRegisters(app._primaryDeviceId())">
                        📋 Go to Measurements
                    </button>
                </div>
            `;
            return;
        }

        // Build table rows
        const dom = this._dominantPollGroup(registers);
        const rows = registers.map(reg => {
            const value = this._dashStore()[reg.address];
            const numValue = value?.value;
            const fmt = this.formatValueWithUnit(numValue, reg.unit);
            const displayValue = this._fmtNum(fmt.value, fmt.decimals);
            const colorClass = this.getValueColorClass(numValue, reg);
            const pollBadge = (reg.poll_group && reg.poll_group !== dom)
                ? `<span class="badge poll-${this._esc(reg.poll_group)}">${this._esc(reg.poll_group)}</span>`
                : '<span class="table-poll-muted">—</span>';

            return `
                <tr data-address="${reg.address}">
                    <td>
                        <div class="table-label">${this._esc(reg.label)}</div>
                        <div class="table-name">${this._esc(reg.name)}</div>
                    </td>
                    <td>
                        <span class="table-value ${colorClass}">${displayValue}</span>
                    </td>
                    <td class="table-unit">${this._esc(fmt.unit)}</td>
                    <td>${pollBadge}</td>
                    <td>
                        <button class="btn-action" title="Edit" ${this._act('editRegisterByAddress', [reg.address])}>
                            <i class="bi bi-pencil"></i>
                        </button>
                    </td>
                </tr>
            `;
        }).join('');

        tbody.innerHTML = rows;
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
                'value-danger': 'var(--color-danger, #ef4444)',
                'value-warning': 'var(--color-warning, #f59e0b)',
                'value-success': 'var(--color-success, #22c55e)',
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
                          style="stroke: ${color}; stroke-dasharray: ${circumference}; stroke-dashoffset: ${offset};" />
                </svg>
                <div class="gauge-reading">
                    <span class="gauge-number">${displayValue}</span>
                    <span class="gauge-unit">${this._esc(displayUnit)}</span>
                </div>
                <div class="gauge-range">
                    <span>${min}</span>
                    <span>${max}</span>
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
            this.showToast('error', this.t('toast.saveFailed', 'Save failed'), String(e));
        } finally { if (btn) btn.disabled = false; }
    },

    // ── Device chips (Phase B): pick which device the dashboard shows ──────
    async renderDashDeviceChips() {
        const box = document.getElementById('dashDeviceChips');
        if (!box) return;
        let devices = [];
        try { devices = (await this._fetchDevices(true)) || []; } catch (e) {}
        this._dashDevList = devices;
        if (devices.length < 2) { box.innerHTML = ''; return; }   // one device → no chips
        const health = {};
        (this.status?.devices || []).forEach(d => { health[d.id] = d.data_health; });
        const hDot = { ok: 'var(--success)', degraded: 'var(--warning)',
                       stale: 'var(--warning)', down: 'var(--danger)' };
        const active = this._dashDeviceId();
        box.innerHTML = devices.filter(d => d.enabled !== false).map(d => `
            <button type="button" class="dash-chip ${d.id === active ? 'active' : ''}"
                    role="tab" aria-selected="${d.id === active}"
                    ${this._act('switchDashDevice', [d.id])}>
                <span class="dot" style="background:${hDot[health[d.id]] || 'var(--text-tertiary)'}"></span>
                ${this._esc(d.name || d.id)}
            </button>`).join('');
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
                    <i class="bi bi-grip-vertical customize-drag-handle"></i>
                    <input type="checkbox" data-address="${reg.address}" ${checked}>
                    <div class="customize-item-info">
                        <div class="customize-item-label">${this._esc(reg.label || reg.name)}</div>
                        <div class="customize-item-details">${this._esc(reg.name)} · ${this._esc(reg.unit || 'N/A')}</div>
                    </div>
                    <div class="customize-item-controls">
                        <select class="customize-select" data-address="${reg.address}" data-field="widget">
                            <option value="value" ${widgetType === 'value' ? 'selected' : ''}>${this.t('lbl.value', "Value")}</option>
                            <option value="gauge" ${widgetType === 'gauge' ? 'selected' : ''}>${this.t('lbl.gauge', "Gauge")}</option>
                            <option value="chart" ${widgetType === 'chart' ? 'selected' : ''}>${this.t('lbl.chart', "Chart")}</option>
                        </select>
                        <button class="customize-size-toggle ${isWide}" data-address="${reg.address}" title="Wide widget">
                            <i class="bi bi-arrows-expand"></i> Wide
                        </button>
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
                btn.classList.toggle('active');
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
