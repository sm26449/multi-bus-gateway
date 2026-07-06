// monitor domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    // ============ Monitor Page ============

    initMonitorPage() {
        // Initialize canvas
        this.monitorCanvas = document.getElementById('monitorCanvas');
        this.monitorCtx = this.monitorCanvas.getContext('2d');

        // Device selector (Tier 2): pick which device the Monitor shows.
        this._renderViewDeviceSelector('monViewDevice', () => this._reloadMonitorForDevice());
        // If a non-primary device is being viewed, load its registers + start its
        // value poll (WS broadcasts the primary only). Renders when regs arrive.
        if (this._viewDeviceId() !== this._primaryDeviceId()) this._reloadMonitorForDevice();

        // Render categories sidebar
        this.renderMonitorCategories();

        // Drag/drop, button + canvas listeners, and the window resize handler must
        // bind ONCE — initMonitorPage re-runs on every Monitor visit and on each
        // language switch (setLanguage → navigateTo), so re-binding would leak.
        if (!this._monitorWired) {
            this._monitorWired = true;
            this.setupMonitorDragDrop();
            this.setupMonitorEventListeners();
            window.addEventListener('resize', () => this.resizeMonitorCanvas());
        }

        // Update table and legend
        this.updateMonitorTable();
        this.updateMonitorLegend();

        // Delay canvas resize to ensure layout is complete
        requestAnimationFrame(() => {
            this.resizeMonitorCanvas();
        });

        // Show onboarding hint on first visit
        if (!localStorage.getItem('janitza-monitor-visited')) {
            this.showMonitorOnboarding();
        }
    },

    showMonitorOnboarding() {
        const main = document.querySelector('.monitor-main');
        if (!main || document.getElementById('monitorHint')) return;

        const hint = document.createElement('div');
        hint.className = 'hint-banner';
        hint.id = 'monitorHint';
        // device-appropriate copy: touch users tap, mouse users can also drag/zoom
        const touch = window.matchMedia('(pointer: coarse)').matches;
        const body = touch
            ? this.t('monitor.hintTouch', 'Tap a value in the list above to add it to the live graph. Tap again in the list to highlight it.')
            : this.t('monitor.hintMouse', 'Click or drag measurements from the left sidebar onto the graph to monitor them in real-time. Use mouse wheel to zoom and drag to pan when zoomed.');
        hint.innerHTML = `
            <span class="hint-banner-icon">💡</span>
            <span class="hint-banner-text">
                <strong>${this.t('monitor.hintTitle', 'Getting started:')}</strong> ${body}
            </span>
            <button class="hint-banner-dismiss" onclick="app.dismissMonitorHint()" aria-label="Dismiss">✕</button>
        `;

        main.insertBefore(hint, main.firstChild);
    },

    dismissMonitorHint() {
        localStorage.setItem('janitza-monitor-visited', 'true');
        document.getElementById('monitorHint')?.remove();
    },

    resizeMonitorCanvas() {
        if (!this.monitorCanvas) return;
        const container = document.getElementById('monitorDropzone');
        if (!container) return;

        const rect = container.getBoundingClientRect();
        // Account for padding/border
        const width = Math.floor(rect.width);
        const height = Math.floor(rect.height);

        if (width > 0 && height > 0) {
            this.monitorCanvas.width = width;
            this.monitorCanvas.height = height;
            this.drawMonitorGraph();
        }
    },

    renderMonitorCategories() {
        const container = document.getElementById('monitorCategories');

        // Use only selected registers (the ones being polled by Modbus)
        // Group them by category — for the viewing device.
        const categories = new Map();

        this._monRegList().concat(this._monCalcRegs()).forEach(reg => {
            // Derive category from influxdb_measurement or unit
            let cat = (reg.influxdb_measurement || '').toLowerCase();
            if (!cat) {
                const unit = (reg.unit || '').toLowerCase();
                if (unit === 'v') cat = 'voltage';
                else if (unit === 'a') cat = 'current';
                else if (unit === 'w' || unit === 'kw') cat = 'power';
                else if (unit === 'wh' || unit === 'kwh') cat = 'energy';
                else if (unit === 'hz') cat = 'frequency';
                else if (unit === 'var' || unit === 'kvar') cat = 'reactive';
                else if (unit === 'va' || unit === 'kva') cat = 'apparent';
                else cat = 'other';
            }

            if (!categories.has(cat)) {
                categories.set(cat, []);
            }
            categories.get(cat).push(reg);
        });

        let html = '';

        // Sort categories alphabetically
        const sortedCats = Array.from(categories.keys()).sort();

        for (const catName of sortedCats) {
            const items = categories.get(catName);

            // Filter by search
            const filteredItems = items.filter(item => {
                if (!this.monitorSearch) return true;
                const searchStr = `${item.name} ${item.label || ''} ${item.description || ''} ${item.unit || ''}`.toLowerCase();
                return searchStr.includes(this.monitorSearch);
            });

            if (filteredItems.length === 0) continue;

            const displayName = catName.charAt(0).toUpperCase() + catName.slice(1);
            html += `
                <div class="monitor-category expanded" data-category="${this._esc(catName)}">
                    <div class="monitor-category-header">
                        <span class="arrow">&#9654;</span>
                        <span>${this._esc(displayName)}</span>
                        <span style="margin-left: auto; color: var(--text-tertiary);">(${filteredItems.length})</span>
                    </div>
                    <div class="monitor-category-items">
                        ${filteredItems.map(item => {
                            const onGraph = this.monitorData[item.address] ? 'on-graph' : '';
                            return `
                                <div class="monitor-item ${onGraph}"
                                     draggable="true"
                                     role="button"
                                     tabindex="0"
                                     data-address="${item.address}"
                                     data-name="${this._esc(item.name)}"
                                     data-description="${this._esc(item.label || item.description || item.name)}"
                                     data-unit="${this._esc(item.unit || '')}"
                                     data-datatype="${this._esc(item.data_type || 'float')}">
                                    <span class="item-name">${this._esc(item.label || item.description || item.name)}</span>
                                    <span class="item-unit">${this._esc(item.unit || '')}</span>
                                </div>
                            `;
                        }).join('')}
                    </div>
                </div>
            `;
        }

        if (!html) {
            container.innerHTML = `
                <div class="empty-state" style="padding: 40px 20px;">
                    <div class="empty-state-icon">📈</div>
                    <div class="empty-state-title">${this.t('msg.noMonitored', "No measurements monitored")}</div>
                    <div class="empty-state-desc">
                        Add measurements to monitoring to see real-time data.
                    </div>
                    <button class="empty-state-action" onclick="app.jumpToDeviceRegisters(app._viewDeviceId())">
                        📋 Browse Registers
                    </button>
                </div>
            `;
            return;
        }

        container.innerHTML = html;

        // Setup category toggle
        container.querySelectorAll('.monitor-category-header').forEach(header => {
            header.addEventListener('click', () => {
                header.parentElement.classList.toggle('expanded');
            });
        });

        // Setup draggable items and click handlers
        container.querySelectorAll('.monitor-item').forEach(item => {
            // Tap / click / keyboard to add
            const addItem = () => {
                if (item.classList.contains('on-graph')) {
                    this.showToast('info', this.t('toast.alreadyAdded', 'Already Added'), this.t('toast.alreadyOnGraph', 'This measurement is already on the graph'));
                    return;
                }
                this.addToMonitor({
                    address: parseInt(item.dataset.address),
                    name: item.dataset.name,
                    description: item.dataset.description,
                    unit: item.dataset.unit,
                    dataType: item.dataset.datatype
                });
            };
            item.addEventListener('click', addItem);
            // Keyboard activation (role="button")
            item.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    addItem();
                }
            });

            // Drag to add
            item.addEventListener('dragstart', (e) => {
                if (item.classList.contains('on-graph')) {
                    e.preventDefault();
                    return;
                }
                e.dataTransfer.setData('text/plain', JSON.stringify({
                    address: parseInt(item.dataset.address),
                    name: item.dataset.name,
                    description: item.dataset.description,
                    unit: item.dataset.unit,
                    dataType: item.dataset.datatype
                }));
                item.classList.add('dragging');
            });

            item.addEventListener('dragend', () => {
                item.classList.remove('dragging');
            });
        });
    },

    setupMonitorDragDrop() {
        const dropzone = document.getElementById('monitorDropzone');

        dropzone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropzone.classList.add('drag-over');
        });

        dropzone.addEventListener('dragleave', () => {
            dropzone.classList.remove('drag-over');
        });

        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('drag-over');

            try {
                const data = JSON.parse(e.dataTransfer.getData('text/plain'));
                this.addToMonitor(data);
            } catch (err) {
                console.error('Drop error:', err);
            }
        });
    },

    setupMonitorEventListeners() {
        // Search
        const searchInput = document.getElementById('monitorSearch');
        if (searchInput) {
            searchInput.addEventListener('input', (e) => {
                this.monitorSearch = e.target.value.toLowerCase();
                this.renderMonitorCategories();
            });
        }

        // Pause button
        const pauseBtn = document.getElementById('monitorPause');
        if (pauseBtn) {
            pauseBtn.addEventListener('click', () => {
                this.monitorPaused = !this.monitorPaused;
                pauseBtn.innerHTML = this.monitorPaused ? '&#9654;' : '&#9208;';
                pauseBtn.title = this.monitorPaused ? 'Resume' : 'Pause';
            });
        }

        // Clear button
        const clearBtn = document.getElementById('monitorClear');
        if (clearBtn) {
            clearBtn.addEventListener('click', () => {
                // Remove on-graph class from all items first
                document.querySelectorAll('.monitor-item.on-graph').forEach(item => {
                    item.classList.remove('on-graph');
                });

                this.monitorData = {};
                this.monitorColorIndex = 0;
                this.monitorZoom = 1;
                this.monitorPanX = 0;
                this.updateMonitorTable();
                this.updateMonitorLegend();
                this.updateDropzoneHint();
                this.drawMonitorGraph();
            });
        }

        // Zoom buttons
        const zoomInBtn = document.getElementById('zoomIn');
        if (zoomInBtn) {
            zoomInBtn.addEventListener('click', () => {
                this.monitorZoom = Math.min(10, this.monitorZoom * 1.2);
                this.drawMonitorGraph();
            });
        }

        const zoomOutBtn = document.getElementById('zoomOut');
        if (zoomOutBtn) {
            zoomOutBtn.addEventListener('click', () => {
                this.monitorZoom = Math.max(1, this.monitorZoom / 1.2);
                if (this.monitorZoom === 1) this.monitorPanX = 0;
                this.drawMonitorGraph();
            });
        }

        const zoomResetBtn = document.getElementById('zoomReset');
        if (zoomResetBtn) {
            zoomResetBtn.addEventListener('click', () => {
                this.monitorZoom = 1;
                this.monitorPanX = 0;
                this.drawMonitorGraph();
            });
        }

        // Zoom and Pan on canvas
        const canvas = this.monitorCanvas;
        if (canvas) {
            // Mouse wheel zoom
            canvas.addEventListener('wheel', (e) => {
                e.preventDefault();
                const zoomFactor = e.deltaY > 0 ? 0.9 : 1.1;
                const newZoom = Math.max(1, Math.min(10, this.monitorZoom * zoomFactor));

                // Zoom towards mouse position
                const rect = canvas.getBoundingClientRect();
                const mouseX = e.clientX - rect.left;
                const graphCenter = canvas.width / 2;

                // Adjust pan to keep mouse position stable
                if (newZoom !== this.monitorZoom) {
                    const zoomChange = newZoom / this.monitorZoom;
                    this.monitorPanX = (this.monitorPanX - mouseX) * zoomChange + mouseX;
                    this.monitorZoom = newZoom;
                    this.drawMonitorGraph();
                }
            });

            // Mouse drag for pan
            canvas.addEventListener('mousedown', (e) => {
                if (this.monitorZoom > 1) {
                    this.monitorIsDragging = true;
                    this.monitorDragStart = { x: e.clientX, y: e.clientY };
                    this.monitorLastPanX = this.monitorPanX;
                    canvas.style.cursor = 'grabbing';
                }
            });

            canvas.addEventListener('mousemove', (e) => {
                if (this.monitorIsDragging) {
                    const dx = e.clientX - this.monitorDragStart.x;
                    this.monitorPanX = this.monitorLastPanX + dx;
                    this.drawMonitorGraph();
                }
            });

            canvas.addEventListener('mouseup', () => {
                this.monitorIsDragging = false;
                canvas.style.cursor = this.monitorZoom > 1 ? 'grab' : 'default';
            });

            canvas.addEventListener('mouseleave', () => {
                this.monitorIsDragging = false;
                canvas.style.cursor = 'default';
            });

            // Double-click to reset zoom
            canvas.addEventListener('dblclick', () => {
                this.monitorZoom = 1;
                this.monitorPanX = 0;
                this.drawMonitorGraph();
            });

            // Tooltip on hover
            canvas.addEventListener('mousemove', (e) => this.showMonitorTooltip(e));
            canvas.addEventListener('mouseleave', () => this.hideMonitorTooltip());
        }
    },

    showMonitorTooltip(e) {
        const canvas = this.monitorCanvas;
        const params = this.monitorGraphParams;

        // Don't show tooltip while dragging or if no data
        if (!canvas || !params || this.monitorIsDragging || Object.keys(this.monitorData).length === 0) {
            this.hideMonitorTooltip();
            return;
        }

        const rect = canvas.getBoundingClientRect();
        // Scale mouse position to canvas internal coordinates
        const scaleX = canvas.width / rect.width;
        const scaleY = canvas.height / rect.height;
        const mouseX = (e.clientX - rect.left) * scaleX;
        const mouseY = (e.clientY - rect.top) * scaleY;

        // Check if mouse is in graph area
        if (mouseX < params.marginLeft || mouseX > params.width - params.marginRight ||
            mouseY < params.marginTop || mouseY > params.marginTop + params.graphHeight) {
            this.hideMonitorTooltip();
            return;
        }

        // Convert mouse X to time (accounting for zoom and pan)
        const centerX = params.marginLeft + params.graphWidth / 2;
        const baseX = (mouseX - this.monitorPanX - centerX) / this.monitorZoom + centerX;
        const normalizedX = (baseX - params.marginLeft) / params.graphWidth;
        const mouseTime = params.minTime + normalizedX * params.timeRange;

        // Find values closest to this time for each monitored variable
        let tooltipLines = [];
        const timeStr = new Date(mouseTime).toLocaleTimeString();
        tooltipLines.push(`<div class="tooltip-time">${timeStr}</div>`);

        for (const [address, info] of Object.entries(this.monitorData)) {
            if (info.data.length === 0) continue;

            // Find closest point
            let closest = null;
            let minDiff = Infinity;
            for (const point of info.data) {
                const diff = Math.abs(point.time - mouseTime);
                if (diff < minDiff) {
                    minDiff = diff;
                    closest = point;
                }
            }

            if (closest && minDiff < params.timeRange * 0.1) {  // Within 10% of time range
                tooltipLines.push(`
                    <div class="tooltip-row">
                        <span class="tooltip-color" style="background:${info.color}"></span>
                        <span class="tooltip-name">${this._esc(info.name)}</span>
                        <span class="tooltip-value">${closest.value.toFixed(2)} ${this._esc(info.unit)}</span>
                    </div>
                `);
            }
        }

        if (tooltipLines.length <= 1) {
            this.hideMonitorTooltip();
            return;
        }

        // Get or create tooltip element
        let tooltip = document.getElementById('monitorTooltip');
        if (!tooltip) {
            tooltip = document.createElement('div');
            tooltip.id = 'monitorTooltip';
            tooltip.className = 'monitor-tooltip';
            document.body.appendChild(tooltip);
        }

        tooltip.innerHTML = tooltipLines.join('');
        tooltip.style.display = 'block';

        // Position tooltip
        const tooltipX = e.clientX + 15;
        const tooltipY = e.clientY - 10;
        tooltip.style.left = tooltipX + 'px';
        tooltip.style.top = tooltipY + 'px';

        // Keep tooltip in viewport
        const tooltipRect = tooltip.getBoundingClientRect();
        if (tooltipRect.right > window.innerWidth) {
            tooltip.style.left = (e.clientX - tooltipRect.width - 15) + 'px';
        }
        if (tooltipRect.bottom > window.innerHeight) {
            tooltip.style.top = (e.clientY - tooltipRect.height - 10) + 'px';
        }

        // Draw crosshair on canvas
        this.drawMonitorCrosshair(mouseX, mouseY);
    },

    hideMonitorTooltip() {
        const tooltip = document.getElementById('monitorTooltip');
        if (tooltip) {
            tooltip.style.display = 'none';
        }
        // Clear crosshair
        if (this._showCrosshair) {
            this._showCrosshair = false;
            this.drawMonitorGraph();
        }
    },

    drawMonitorCrosshair(x, y) {
        const ctx = this.monitorCtx;
        const params = this.monitorGraphParams;
        if (!ctx || !params) return;

        // Store crosshair position for next redraw
        this._crosshairX = x;
        this._crosshairY = y;
        this._showCrosshair = true;

        // Redraw graph (will include crosshair)
        this.drawMonitorGraph();
    },

    drawCrosshairOverlay() {
        if (!this._showCrosshair) return;

        const ctx = this.monitorCtx;
        const params = this.monitorGraphParams;
        if (!ctx || !params) return;

        const x = this._crosshairX;
        const y = this._crosshairY;

        ctx.save();
        ctx.strokeStyle = this._monChartTheme().text;   // theme-aware (was hardcoded white)
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);

        // Vertical line
        ctx.beginPath();
        ctx.moveTo(x, params.marginTop);
        ctx.lineTo(x, params.marginTop + params.graphHeight);
        ctx.stroke();

        // Horizontal line
        ctx.beginPath();
        ctx.moveTo(params.marginLeft, y);
        ctx.lineTo(params.width - params.marginRight, y);
        ctx.stroke();

        ctx.restore();
    },

    addToMonitor(data) {
        // Check if max 6 variables
        if (Object.keys(this.monitorData).length >= 6) {
            this.showToast('warning', this.t('toast.maxReached', 'Maximum Reached'), this.t('toast.max6', 'You can monitor up to 6 values at a time'));
            return;
        }

        // Check if already on graph
        if (this.monitorData[data.address]) {
            this.showToast('info', this.t('toast.alreadyAdded', 'Already Added'), this.t('toast.alreadyMonitoredMsg', 'This measurement is already being monitored'));
            return;
        }

        // Get next color
        const color = this.monitorColors[this.monitorColorIndex % this.monitorColors.length];
        this.monitorColorIndex++;

        // Add to monitor data
        this.monitorData[data.address] = {
            name: data.description || data.name,
            unit: data.unit,
            color: color,
            data: [],
            min: null,
            max: null
        };

        // Mark item in sidebar as on-graph
        const item = document.querySelector(`.monitor-item[data-address="${data.address}"]`);
        if (item) {
            item.classList.add('on-graph');
        }

        // Update UI
        this.updateMonitorTable();
        this.updateMonitorLegend();
        this.updateDropzoneHint();

        this.showToast('success', this.t('toast.added', 'Added'), `${data.description || data.name} ${this.t('toast.toMonitor', 'added to monitor')}`);
    },

    removeFromMonitor(address) {
        if (this.monitorData[address]) {
            const name = this.monitorData[address].name;
            delete this.monitorData[address];

            // Reset color index if all items removed
            if (Object.keys(this.monitorData).length === 0) {
                this.monitorColorIndex = 0;
            }

            // Unmark item in sidebar
            const item = document.querySelector(`.monitor-item[data-address="${address}"]`);
            if (item) {
                item.classList.remove('on-graph');
            }

            // Update UI
            this.updateMonitorTable();
            this.updateMonitorLegend();
            this.updateDropzoneHint();
            this.drawMonitorGraph();

            this.showToast('info', this.t('toast.removed', 'Removed'), `${name} ${this.t('toast.fromMonitor', 'removed from monitor')}`);
        }
    },

    updateDropzoneHint() {
        const hint = document.getElementById('dropzoneHint');
        const dropzone = document.getElementById('monitorDropzone');
        const hasData = Object.keys(this.monitorData).length > 0;

        if (hint) {
            hint.classList.toggle('hidden', hasData);
        }
        if (dropzone) {
            dropzone.classList.toggle('has-data', hasData);
        }
    },

    updateMonitorTable() {
        const tbody = document.getElementById('monitorTableBody');
        const emptyMsg = document.getElementById('monitorTableEmpty');
        const hasData = Object.keys(this.monitorData).length > 0;

        if (emptyMsg) {
            emptyMsg.classList.toggle('hidden', hasData);
        }

        if (!tbody) return;

        if (!hasData) {
            tbody.innerHTML = '';
            return;
        }

        let html = '';
        const vals = this._monValMap();
        for (const [address, info] of Object.entries(this.monitorData)) {
            const current = vals[address]?.value;
            const currentDisplay = typeof current === 'number' ? current.toFixed(3) : '--';
            const minDisplay = info.min !== null ? info.min.toFixed(3) : '--';
            const maxDisplay = info.max !== null ? info.max.toFixed(3) : '--';

            html += `
                <tr data-address="${address}">
                    <td class="color-cell">
                        <div class="color-dot" style="background: ${info.color}"></div>
                    </td>
                    <td>${this._esc(info.name)}</td>
                    <td class="value-cell">${currentDisplay}</td>
                    <td class="min-cell">${minDisplay}</td>
                    <td class="max-cell">${maxDisplay}</td>
                    <td class="unit-cell">${this._esc(info.unit)}</td>
                    <td class="actions-cell">
                        <button class="btn-remove" data-address="${address}" title="Remove">&#10005;</button>
                    </td>
                </tr>
            `;
        }

        tbody.innerHTML = html;

        // Attach remove handlers
        tbody.querySelectorAll('.btn-remove').forEach(btn => {
            btn.addEventListener('click', () => {
                this.removeFromMonitor(parseInt(btn.dataset.address));
            });
        });
    },

    updateMonitorLegend() {
        const legend = document.getElementById('monitorLegend');
        if (!legend) return;

        let html = '';
        for (const [address, info] of Object.entries(this.monitorData)) {
            html += `
                <div class="legend-item">
                    <span class="legend-color" style="background: ${info.color}"></span>
                    <span>${this._esc(info.name)}</span>
                </div>
            `;
        }

        legend.innerHTML = html;
    },

    updateMonitorData() {
        if (this.monitorPaused) return;

        const now = Date.now();
        const vals = this._monValMap();

        for (const [address, info] of Object.entries(this.monitorData)) {
            const value = vals[address]?.value;

            if (typeof value === 'number') {
                // Add data point
                info.data.push({ time: now, value: value });

                // Keep max points
                if (info.data.length > this.monitorMaxPoints) {
                    info.data.shift();
                }

                // Update min/max
                if (info.min === null || value < info.min) {
                    info.min = value;
                }
                if (info.max === null || value > info.max) {
                    info.max = value;
                }
            }
        }
    },

    _monChartTheme() {
        // Derive chart colours from the active theme's CSS variables so the graph
        // is legible in BOTH light and dark themes (was hardcoded dark).
        const cs = getComputedStyle(document.body);
        const v = (n, fb) => (cs.getPropertyValue(n) || '').trim() || fb;
        return {
            bg: v('--bg-secondary', '#1a1a1e'),
            grid: v('--border-light', 'rgba(128,128,128,0.15)'),
            gridFaint: v('--border-light', 'rgba(128,128,128,0.08)'),
            text: v('--text-secondary', 'rgba(128,128,128,0.7)'),
            textFaint: v('--text-tertiary', 'rgba(128,128,128,0.5)'),
        };
    },

    drawMonitorGraph() {
        if (!this.monitorCtx || !this.monitorCanvas) return;

        const ctx = this.monitorCtx;
        const width = this.monitorCanvas.width;
        const height = this.monitorCanvas.height;
        const theme = this._monChartTheme();

        // Clear canvas
        ctx.fillStyle = theme.bg;
        ctx.fillRect(0, 0, width, height);

        const hasData = Object.keys(this.monitorData).length > 0;
        if (!hasData) {
            // Empty state: a first-time visitor otherwise faces a large blank
            // rectangle with zero guidance (History has its banner; Monitor
            // needs the equivalent). Theme-aware, redrawn on resize.
            ctx.textAlign = 'center';
            ctx.fillStyle = theme.text;
            ctx.font = '600 15px system-ui, sans-serif';
            ctx.fillText(this.t('monitor.emptyTitle', 'Nothing monitored yet'),
                         width / 2, height / 2 - 14);
            ctx.fillStyle = theme.textFaint;
            ctx.font = '13px system-ui, sans-serif';
            ctx.fillText(this.t('monitor.emptyHint', 'Click a value in the list on the left to start charting it live.'),
                         width / 2, height / 2 + 10);
            ctx.textAlign = 'left';
            return;
        }

        // Collect all data points
        let allPoints = [];
        for (const info of Object.values(this.monitorData)) {
            if (info.data.length === 0) continue;
            allPoints = allPoints.concat(info.data);
        }

        if (allPoints.length === 0) return;

        // Calculate ranges
        const times = allPoints.map(p => p.time);
        const values = allPoints.map(p => p.value);

        const minTime = Math.min(...times);
        const maxTime = Math.max(...times);
        const minValue = Math.min(...values);
        const maxValue = Math.max(...values);

        const timeRange = maxTime - minTime || 1000; // At least 1 second
        const valueRange = maxValue - minValue || 1;
        const padding = valueRange * 0.1;

        const yMin = minValue - padding;
        const yMax = maxValue + padding;
        const yRange = yMax - yMin;

        // Graph area
        const marginLeft = 70;
        const marginRight = 20;
        const marginTop = 20;
        const marginBottom = 35;

        const graphWidth = width - marginLeft - marginRight;
        const graphHeight = height - marginTop - marginBottom;

        if (graphWidth <= 0 || graphHeight <= 0) return;

        // Helper: convert value to Y pixel (higher value = lower Y in canvas)
        const valueToY = (val) => {
            const normalized = (val - yMin) / yRange; // 0 to 1
            return marginTop + graphHeight * (1 - normalized); // Flip for canvas
        };

        // Helper: convert time to X pixel (with zoom and pan)
        const timeToX = (t) => {
            const normalized = (t - minTime) / timeRange;
            const baseX = marginLeft + graphWidth * normalized;
            // Apply zoom and pan
            const centerX = marginLeft + graphWidth / 2;
            return centerX + (baseX - centerX) * this.monitorZoom + this.monitorPanX;
        };

        // Draw grid lines
        ctx.strokeStyle = theme.gridFaint;
        ctx.lineWidth = 1;

        // Horizontal grid lines (5 lines)
        for (let i = 0; i <= 4; i++) {
            const val = yMin + (yRange / 4) * i;
            const y = valueToY(val);

            ctx.beginPath();
            ctx.moveTo(marginLeft, y);
            ctx.lineTo(width - marginRight, y);
            ctx.stroke();

            // Y-axis labels
            ctx.fillStyle = theme.textFaint;
            ctx.font = '11px Inter, sans-serif';
            ctx.textAlign = 'right';
            ctx.fillText(val.toFixed(1), marginLeft - 8, y + 4);
        }

        // Vertical grid lines (zoomed)
        for (let i = 0; i <= 4; i++) {
            const t = minTime + (timeRange / 4) * i;
            const x = timeToX(t);
            if (x >= marginLeft && x <= width - marginRight) {
                ctx.beginPath();
                ctx.moveTo(x, marginTop);
                ctx.lineTo(x, marginTop + graphHeight);
                ctx.stroke();
            }
        }

        // Set clipping region for graph area
        ctx.save();
        ctx.beginPath();
        ctx.rect(marginLeft, marginTop, graphWidth, graphHeight);
        ctx.clip();

        // Draw lines for each variable
        for (const info of Object.values(this.monitorData)) {
            if (info.data.length < 2) continue;

            // Sort data by time to ensure correct line drawing
            const sortedData = [...info.data].sort((a, b) => a.time - b.time);

            ctx.strokeStyle = info.color;
            ctx.lineWidth = 2;
            ctx.lineCap = 'round';
            ctx.lineJoin = 'round';
            ctx.beginPath();

            sortedData.forEach((point, idx) => {
                const x = timeToX(point.time);
                const y = valueToY(point.value);

                if (idx === 0) {
                    ctx.moveTo(x, y);
                } else {
                    ctx.lineTo(x, y);
                }
            });

            ctx.stroke();
        }

        ctx.restore(); // Remove clipping

        // Draw time axis labels
        ctx.fillStyle = theme.textFaint;
        ctx.font = '11px Inter, sans-serif';
        ctx.textAlign = 'center';

        for (let i = 0; i <= 4; i++) {
            const t = minTime + (timeRange / 4) * i;
            const x = timeToX(t);
            if (x >= marginLeft - 30 && x <= width - marginRight + 30) {
                const timeStr = new Date(t).toLocaleTimeString();
                ctx.fillText(timeStr, Math.max(marginLeft, Math.min(width - marginRight, x)), height - 12);
            }
        }

        // Draw border around graph area
        ctx.strokeStyle = theme.grid;
        ctx.lineWidth = 1;
        ctx.strokeRect(marginLeft, marginTop, graphWidth, graphHeight);

        // Show zoom indicator if zoomed
        if (this.monitorZoom > 1) {
            ctx.fillStyle = theme.text;
            ctx.font = '12px Inter, sans-serif';
            ctx.textAlign = 'right';
            ctx.fillText(`${this.monitorZoom.toFixed(1)}x (scroll to zoom, drag to pan, dblclick to reset)`, width - marginRight, marginTop - 5);
        }

        // Store graph params for tooltip
        this.monitorGraphParams = {
            marginLeft, marginRight, marginTop, marginBottom,
            graphWidth, graphHeight,
            minTime, maxTime, timeRange,
            yMin, yMax, yRange,
            width, height
        };

        // Draw crosshair overlay if active
        this.drawCrosshairOverlay();
    },

    // Update monitor when WebSocket data arrives
    // Device changed on the Monitor: reload its register sidebar + value source.
    async _reloadMonitorForDevice() {
        this._stopMonitorPoll();
        if (this._viewDeviceId() === this._primaryDeviceId()) {
            this._monRegs = null; this._monValues = null;   // fall back to WS-fed globals
            this.renderMonitorCategories();
            this.updateMonitorTable();
            this.drawMonitorGraph();
            return;
        }
        // non-primary: fetch its selected registers, then poll its live values
        try {
            const r = await fetch('/api/registers/selected?device=' + encodeURIComponent(this._viewDeviceId()));
            this._monRegs = (await r.json()).registers || [];
        } catch (e) { this._monRegs = []; }
        this.renderMonitorCategories();
        this._startMonitorPoll();
    },

    // The Monitor lives ONLY inside the device workspace now (no top-nav page),
    // so `currentPage` is 'devices', never 'monitor'. Gate the data feed on the
    // monitor page actually being laid out (embedded tab open) instead — otherwise
    // updateMonitorData() never runs and the chart stays blank with min/max "--".
    _monitorVisible() {
        const el = document.getElementById('monitorPage');
        return !!(el && el.offsetParent !== null);
    },

    _startMonitorPoll() {
        this._stopMonitorPoll();
        const poll = async () => {
            if (document.hidden || !this._monitorVisible()) return;
            const dev = this._viewDeviceId();
            if (dev === this._primaryDeviceId()) return;      // primary uses WS
            try {
                const d = await (await fetch('/api/values?device=' + encodeURIComponent(dev))).json();
                this._monValues = d.values || {};
                this.updateMonitorData();
                this.updateMonitorTableValues();
                this.drawMonitorGraph();
            } catch (e) { /* transient */ }
        };
        poll();
        this._monitorTimer = setInterval(poll, 1000);
    },

    _stopMonitorPoll() {
        if (this._monitorTimer) { clearInterval(this._monitorTimer); this._monitorTimer = null; }
    },

    onMonitorDataUpdate() {
        if (!this._monitorVisible()) return;
        // Non-primary device is driven by its own poll, not the primary WS feed.
        if (this._viewDeviceId() !== this._primaryDeviceId()) return;
        if (Object.keys(this.monitorData).length === 0) return;

        this.updateMonitorData();
        this.updateMonitorTableValues();

        // Use RAF to prevent too many redraws
        if (!this._monitorRAFPending) {
            this._monitorRAFPending = true;
            requestAnimationFrame(() => {
                this.drawMonitorGraph();
                this._monitorRAFPending = false;
            });
        }
    },

    updateMonitorTableValues() {
        const tbody = document.getElementById('monitorTableBody');
        if (!tbody) return;

        for (const [address, info] of Object.entries(this.monitorData)) {
            const tr = tbody.querySelector(`tr[data-address="${address}"]`);
            if (!tr) continue;

            const current = this.currentValues[address]?.value;
            const currentCell = tr.querySelector('.value-cell');
            const minCell = tr.querySelector('.min-cell');
            const maxCell = tr.querySelector('.max-cell');

            if (currentCell && typeof current === 'number') {
                currentCell.textContent = current.toFixed(3);
            }
            if (minCell && info.min !== null) {
                minCell.textContent = info.min.toFixed(3);
            }
            if (maxCell && info.max !== null) {
                maxCell.textContent = info.max.toFixed(3);
            }
        }
    },

    // Monitor data source: the viewing device's registers + live values. For
    // the primary this is the WS-fed global cache (unchanged); for another
    // device it is a polled snapshot.
    _monRegList() {
        return (this._viewDeviceId() === this._primaryDeviceId())
            ? (this.selectedRegisters || []) : (this._monRegs || []);
    },
    // Calculated registers aren't in the device catalog — they only exist in the
    // live value store (flagged `calculated`). Surface them in the picker so they
    // can be charted like any measurement; they carry their own name/unit/label.
    _monCalcRegs() {
        const out = [];
        for (const [addr, info] of Object.entries(this._monValMap())) {
            if (!info || !info.calculated) continue;
            out.push({ address: parseInt(addr), name: info.name || ('calc_' + addr),
                       label: info.label || info.name, unit: info.unit || '',
                       data_type: 'float', influxdb_measurement: 'calculated' });
        }
        return out;
    },
    _monValMap() {
        return (this._viewDeviceId() === this._primaryDeviceId())
            ? this.currentValues : (this._monValues || {});
    }
});
