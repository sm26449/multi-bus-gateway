/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// registers domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    async loadAllRegisters() {
        try {
            const response = await fetch('/api/registers/all' + this._regDeviceQS());
            this.allRegisters = await response.json();

            // Populate category filter (reset first — device switches re-run this)
            const filter = document.getElementById('categoryFilter');
            const measurements = this.allRegisters.measurements || {};

            while (filter.options.length > 1) filter.remove(1);
            Object.keys(measurements).forEach(cat => {
                const option = document.createElement('option');
                option.value = cat;
                option.textContent = this._regCatLabel(cat);
                filter.appendChild(option);
            });

        } catch (error) {
            console.error('Failed to load all registers:', error);
        }
    },

    async loadSelectedRegisters() {
        try {
            const response = await fetch('/api/registers/selected' + this._regDeviceQS());
            const data = await response.json();
            this.selectedRegisters = data.registers || [];
            this.pollGroups = data.poll_groups || {};
            this._renderRegSourceSelector(data);
            // The first answer for a unit read several ways is the DEVICE-level
            // map (no source asked for yet); the picker has now chosen the first
            // source, so fetch that source's catalog and selection instead of
            // showing an empty list under a picker that says "3/7 ticked".
            if (this._regSource && (data.source || '') !== this._regSource) {
                await this.loadAllRegisters();
                return this.loadSelectedRegisters();
            }

            this.updatePollGroupsStatus();
            // Keep the dashboard's OWN list in sync when both contexts look at
            // the same device (an edit in Measurements shows up on the widgets);
            // otherwise the dashboard keeps its device's list untouched.
            const regDev = this._regDevice === undefined ? this._primaryDeviceId() : this._regDevice;
            if (regDev === this._dashDeviceId()) {
                this.dashRegisters = this.selectedRegisters;
                this.updateDashboard();
            }

        } catch (error) {
            console.error('Failed to load selected registers:', error);
        }
    },

    // What a measurement is, in words — one vocabulary for the picker, the
    // Selected list, the Overview and the Monitor.
    _regCatLabel(cat) {
        const c = String(cat || 'other');
        const fromCatalog = ((this.allRegisters || {}).measurements || {})[c]?.name;
        return this.t(`cat.${c}`, fromCatalog || (c.charAt(0).toUpperCase() + c.slice(1).replace(/_/g, ' ')));
    },

    // Where a value is read from, as an operator reads it: a JSON path, a topic,
    // or a Modbus address with its shape.
    _regWhere(reg) {
        if (reg.json_path && !reg.topic) return `<span class="address" title="JSON">${this._esc(reg.json_path)}</span>`;
        if (reg.topic) return `<span class="address">${this._esc(reg.topic)}</span>${reg.json_path ? ` <span class="reg-name-mono">${this._esc(reg.json_path)}</span>` : ''}`;
        const bits = [String(reg.address)];
        if (reg.register_type && reg.register_type !== 'holding') bits.push(this._esc(reg.register_type));
        if (reg.data_type) bits.push(this._esc(reg.data_type));
        if (reg.scale && reg.scale !== 1) bits.push(`×${reg.scale}`);
        if (reg.scale_from) bits.push(`SF ${this._esc(reg.scale_from)}`);
        return `<span class="address">${bits[0]}</span> <span class="reg-name-mono">${bits.slice(1).join(' · ')}</span>`;
    },

    _regIntervalText(reg) {
        const g = (this.pollGroups || {})[reg.poll_group];
        if (!g || g.interval == null) return this._esc(reg.poll_group || '—');
        const iv = g.interval < 1 ? `${Math.round(g.interval * 1000)} ms` : `${g.interval} s`;
        return `${this.t('registers.every', 'every')} ${iv}`;
    },

    _regValueHtml(name) {
        const v = (this._regLive || {})[name];
        if (!v || v.value == null) return '<span style="color:var(--text-tertiary,#8a94a0);">—</span>';
        const num = typeof v.value === 'number'
            ? (Number.isInteger(v.value) ? String(v.value) : Number(v.value.toFixed(3)).toString())
            : this._esc(String(v.value));
        const age = v.ts ? Math.max(0, Math.round(Date.now() / 1000 - v.ts)) : null;
        return `${num} <span style="color:var(--text-secondary);">${this._esc(v.unit || '')}</span>${age != null ? ` <span class="reg-age">${age} s</span>` : ''}`;
    },

    // template-derived rows are unticked, never deleted; custom ones may go
    _regInCatalog(reg) {
        return this.flattenRegisters().some(r => r.address === reg.address
            && (!reg.json_path || r.json_path === reg.json_path));
    },

    _regCategoryOrder() {
        return Object.keys((this.allRegisters || {}).measurements || {});
    },

    renderRegistersTable() {
        const tbody = document.getElementById('registersTableBody');
        if (!tbody) return;
        const searchQuery = (document.getElementById('registerSearch')?.value || '').toLowerCase();
        const categoryFilter = document.getElementById('categoryFilter')?.value || '';
        const allRegs = this.flattenRegisters();
        const http = this._regDeviceIsHttp(), mqttIn = this._regDeviceIsMqtt();
        const keyHead = document.getElementById('regColKeyHead');
        if (keyHead) keyHead.textContent = this.t('registers.where', 'Where');
        const queryBtn = document.getElementById('queryRegisterBtn');
        if (queryBtn) queryBtn.style.display = (http || mqttIn) ? 'none' : '';
        this._syncWriteMenuItem();

        const filtered = allRegs.filter(reg => {
            const matchesSearch = !searchQuery ||
                reg.name.toLowerCase().includes(searchQuery) ||
                reg.address.toString().includes(searchQuery) ||
                (reg.unit && reg.unit.toLowerCase().includes(searchQuery)) ||
                (reg.json_path && reg.json_path.toLowerCase().includes(searchQuery)) ||
                (reg.description && reg.description.toLowerCase().includes(searchQuery));
            const matchesCategory = !categoryFilter || reg.category === categoryFilter;
            return matchesSearch && matchesCategory;
        });
        // grouped by what they measure, in the catalog's order; by address inside
        const order = this._regCategoryOrder();
        filtered.sort((a, b) => (order.indexOf(a.category) - order.indexOf(b.category)) || (a.address - b.address));

        const start = (this.registerSearchPage - 1) * this.registersPerPage;
        const paginated = filtered.slice(start, start + this.registersPerPage);

        tbody.innerHTML = '';
        let lastCat = null;
        paginated.forEach(reg => {
            if (reg.category !== lastCat) {
                lastCat = reg.category;
                const gh = document.createElement('tr');
                gh.className = 'reg-group';
                gh.innerHTML = `<td colspan="6">${this._esc(this._regCatLabel(reg.category))}</td>`;
                tbody.appendChild(gh);
            }
            const configuredReg = this.selectedRegisters.find(s => s.address === reg.address);
            const isConfigured = !!configuredReg;
            const tr = document.createElement('tr');
            tr.dataset.address = reg.address;
            if (isConfigured) tr.classList.add('configured');
            const label = reg.description || reg.name;
            const queryBtnHtml = (http || mqttIn) ? '' :
                `<button class="btn-action query" data-address="${reg.address}" title="${this.t('registers.queryNow', 'Query now')}" aria-label="${this.t('registers.queryNow', 'Query now')}">&#128269;</button>`;
            tr.innerHTML = `
                <td class="reg-tick"><input type="checkbox" ${isConfigured ? 'checked' : ''}
                        aria-label="${this.t('registers.readCol', 'Read')}: ${this._esc(label)}"></td>
                <td class="description-cell">
                    <div class="reg-description">${this._esc(label)}</div>
                    <span class="reg-name-mono">${this._esc(reg.name)}</span>
                </td>
                <td>${this._regWhere(reg)}</td>
                <td class="value num" data-reg-name="${this._esc(reg.name)}">${this._regValueHtml(reg.name)}</td>
                <td class="reg-interval">${isConfigured ? this._regIntervalText(configuredReg) : `<span style="color:var(--text-tertiary,#8a94a0);">${this.t('registers.notRead', 'not read')}</span>`}</td>
                <td class="actions-cell">${queryBtnHtml}
                    <button class="btn-action edit" data-address="${reg.address}" title="${isConfigured ? this.t('registers.editConfig', 'Edit how it is published') : this.t('registers.configureAdd', 'Configure & read')}"
                            aria-label="${isConfigured ? this.t('registers.editConfig', 'Edit how it is published') : this.t('registers.configureAdd', 'Configure & read')}">&#9998;</button>
                </td>`;
            tr.querySelector('input[type=checkbox]').addEventListener('change', (ev) => {
                if (ev.target.checked) this.quickAddRegister(reg);
                else this.removeRegisterFromTable(reg.address);
            });
            const qb = tr.querySelector('.query');
            if (qb) qb.addEventListener('click', () => this.queryRegisterNow(reg));
            tr.querySelector('.edit').addEventListener('click', () =>
                isConfigured ? this.editRegister(configuredReg) : this.openAddModal(reg));
            tbody.appendChild(tr);
        });
        this.renderPagination(filtered.length);
        this._updateRegTabCounts();
        this._startRegLive();
    },

    _updateRegTabCounts() {
        const sel = document.getElementById('regTabSelCount');
        const all = document.getElementById('regTabAllCount');
        if (sel) sel.textContent = `(${(this.selectedRegisters || []).length})`;
        if (all) all.textContent = `(${this.flattenRegisters().length})`;
    },

    // Write is a Modbus thing, and only where the map declares something
    // writable and the device is not locked — a menu item that opens a form
    // for a device that can never take a write is a trap.
    _regCanWrite() {
        if (this._regDeviceIsHttp() || this._regDeviceIsMqtt()) return false;
        const id = this._regDevice || this._primaryDeviceId();
        const dev = (this._devices || []).find(d => d.id === id);
        if (dev?.write_locked) return false;
        return this.flattenRegisters().some(r => /W/i.test(String(r.access || '')));
    },

    _syncWriteMenuItem() {
        const w = document.getElementById('writeRegBtn');
        if (w) w.hidden = !this._regCanWrite();
    },

    // menu item: close the register menu, then run the named action
    _regMenuPick(method) {
        this.toggleRegMenu(false);
        if (typeof this[method] === 'function') this[method]();
    },

    toggleRegMenu(open) {
        const btn = document.getElementById('regMenuBtn'), menu = document.getElementById('regMenu');
        if (!btn || !menu) return;
        const willOpen = open === undefined ? menu.hidden : !!open;
        menu.hidden = !willOpen;
        btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
        if (willOpen) {
            const items = [...menu.querySelectorAll('[role=menuitem]:not([hidden])')];
            items[0]?.focus();
            if (!menu._keys) {
                menu._keys = true;
                menu.addEventListener('keydown', (e) => {
                    const its = [...menu.querySelectorAll('[role=menuitem]:not([hidden])')];
                    const i = its.indexOf(document.activeElement);
                    if (e.key === 'Escape') { this.toggleRegMenu(false); btn.focus(); }
                    else if (e.key === 'ArrowDown') { e.preventDefault(); its[(i + 1) % its.length]?.focus(); }
                    else if (e.key === 'ArrowUp') { e.preventDefault(); its[(i - 1 + its.length) % its.length]?.focus(); }
                });
                document.addEventListener('click', (e) => {
                    if (!menu.hidden && !menu.contains(e.target) && e.target !== btn && !btn.contains(e.target)) this.toggleRegMenu(false);
                });
            }
        }
    },

    // ── live values for the open map, by NAME: a JSON path has no address ──
    _startRegLive() {
        if (this._regLiveTimer) return;
        const tick = async () => {
            if (!this._registersVisible()) { this._stopRegLive(); return; }
            await this._fetchRegLive();
            this.updateRegistersValues();
        };
        tick();
        this._regLiveTimer = setInterval(tick, 3000);
    },

    _stopRegLive() {
        if (this._regLiveTimer) { clearInterval(this._regLiveTimer); this._regLiveTimer = null; }
    },

    async _fetchRegLive() {
        try {
            const id = this._regDeviceIdOrNull();
            const d = await (await fetch('/api/values' + (id ? '?device=' + encodeURIComponent(id) : ''))).json();
            const out = {};
            Object.values(d.values || {}).forEach(v => { if (v && v.name) out[v.name] = v; });
            this._regLive = out;
        } catch (e) { /* a blip keeps the last values */ }
    },

    flattenRegisters() {
        // Cache key based on allRegisters object reference
        const cacheKey = JSON.stringify(Object.keys(this.allRegisters.measurements || {}));

        if (this._flattenedRegistersCache && this._flattenedRegistersCacheKey === cacheKey) {
            return this._flattenedRegistersCache;
        }

        const result = [];
        const measurements = this.allRegisters.measurements || {};

        for (const [catName, catData] of Object.entries(measurements)) {
            if (catData.entries) {
                catData.entries.forEach(e => {
                    result.push({ ...e, category: catName });
                });
            }
            if (catData.subtypes) {
                for (const [subName, subData] of Object.entries(catData.subtypes)) {
                    (subData.entries || []).forEach(e => {
                        result.push({ ...e, category: catName, subtype: subName });
                    });
                }
            }
        }

        this._flattenedRegistersCache = result.sort((a, b) => a.address - b.address);
        this._flattenedRegistersCacheKey = cacheKey;

        return this._flattenedRegistersCache;
    },

    renderPagination(totalItems) {
        const container = document.getElementById('registersPagination');
        const totalPages = Math.ceil(totalItems / this.registersPerPage);

        container.innerHTML = '';

        if (totalPages <= 1) return;

        // Prev button
        const prevLi = document.createElement('li');
        prevLi.className = `page-item ${this.registerSearchPage === 1 ? 'disabled' : ''}`;
        const prevBtn = document.createElement('a');
        prevBtn.className = 'page-link';
        prevBtn.href = '#';
        prevBtn.textContent = 'Prev';
        prevBtn.addEventListener('click', (e) => {
            e.preventDefault();
            if (this.registerSearchPage > 1) {
                this.registerSearchPage--;
                this.renderRegistersTable();
            }
        });
        prevLi.appendChild(prevBtn);
        container.appendChild(prevLi);

        // Page numbers
        for (let i = 1; i <= Math.min(totalPages, 10); i++) {
            const li = document.createElement('li');
            li.className = `page-item ${i === this.registerSearchPage ? 'active' : ''}`;
            const btn = document.createElement('a');
            btn.className = 'page-link';
            btn.href = '#';
            btn.textContent = i;
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                this.registerSearchPage = i;
                this.renderRegistersTable();
            });
            li.appendChild(btn);
            container.appendChild(li);
        }

        // Next button
        const nextLi = document.createElement('li');
        nextLi.className = `page-item ${this.registerSearchPage === totalPages ? 'disabled' : ''}`;
        const nextBtn = document.createElement('a');
        nextBtn.className = 'page-link';
        nextBtn.href = '#';
        nextBtn.textContent = 'Next';
        nextBtn.addEventListener('click', (e) => {
            e.preventDefault();
            if (this.registerSearchPage < totalPages) {
                this.registerSearchPage++;
                this.renderRegistersTable();
            }
        });
        nextLi.appendChild(nextBtn);
        container.appendChild(nextLi);
    },

    // Measurements lives only inside the device workspace now (no top-nav page),
    // so currentPage is 'devices', never 'registers'. Gate the live value refresh
    // on the register view being laid out (embedded tab open) instead.
    _registersVisible() {
        const el = document.getElementById('deviceRegistersView');
        return !!(el && el.offsetParent !== null);
    },

    updateRegistersValues() {
        document.querySelectorAll('#deviceRegistersView [data-reg-name]').forEach(td => {
            td.innerHTML = this._regValueHtml(td.dataset.regName);
        });
    },

    async queryRegisterNow(reg) {
        try {
            const response = await fetch('/api/query/register', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    address: reg.address,
                    data_type: reg.data_type || 'float',
                    register_type: reg.register_type || 'holding',
                    scale: reg.scale,
                    ...(this._regDeviceIdOrNull() ? { device_id: this._regDeviceIdOrNull() } : {})
                })
            });

            if (!response.ok) {
                throw new Error('Query failed');
            }

            const data = await response.json();
            const displayValue = typeof data.value === 'number'
                ? data.value.toFixed(4)
                : data.value;

            this.showToast('info', reg.name, `${displayValue} ${reg.unit || ''}`);

            // Update value in table
            const tr = document.querySelector(`#registersTableBody tr[data-address="${reg.address}"]`);
            if (tr) {
                const valueCell = tr.querySelector('.value');
                if (valueCell) {
                    valueCell.textContent = displayValue;
                    valueCell.classList.add('flash');
                    setTimeout(() => valueCell.classList.remove('flash'), 1000);
                }
            }

        } catch (error) {
            this.showToast('error', this.t('toast.queryFailed', 'Query Failed'), `${this.t('toast.couldNotRead', 'Could not read measurement')} ${reg.address}`);
        }
    },

    // ============ Smart Defaults Generation ============

    generateRegisterDefaults(reg) {
        const cat = (reg.category || '').toLowerCase();
        const desc = reg.description || reg.name;

        // 1. Determine poll group based on category
        let pollGroup = 'normal';
        if (cat.includes('energy')) {
            pollGroup = 'slow';
        } else if (cat.includes('power') || cat.includes('voltage') || cat.includes('current')) {
            pollGroup = 'realtime';
        }

        // 2. Generate clean label from description
        const label = desc;

        // 3+4. MQTT topic + InfluxDB measurement. Prefer the canonical
        // dictionary when the register name is canonical (uniform output across
        // devices); otherwise fall back to the category/description heuristic.
        const canon = this._canonicalFields && this._canonicalFields[(reg.name || '').toLowerCase()];
        const topicBase = cat.replace(/\s+/g, '_').toLowerCase();
        const topicName = desc
            .toLowerCase()
            .replace(/[,;]/g, '')
            .replace(/\s+/g, '_')
            .replace(/[^a-z0-9_]/g, '')
            .replace(/_+/g, '_')
            .replace(/^_|_$/g, '');
        const mqttTopic = canon ? canon.mqtt_topic : `${topicBase}/${topicName}`;
        const measurement = canon ? canon.measurement : cat.replace(/\s+/g, '_').toLowerCase();

        // 5. Extract tags from description
        const tags = {};

        // Extract phase (L1, L2, L3, N)
        const phaseMatch = desc.match(/L([1-3N])/i);
        if (phaseMatch) {
            tags.phase = 'L' + phaseMatch[1].toUpperCase();
        }

        // Extract type indicators
        if (desc.toLowerCase().includes('active')) tags.type = 'active';
        else if (desc.toLowerCase().includes('reactive')) tags.type = 'reactive';
        else if (desc.toLowerCase().includes('apparent')) tags.type = 'apparent';

        // Line-to-neutral vs line-to-line
        if (desc.match(/L\d-N/i)) tags.connection = 'line_neutral';
        else if (desc.match(/L\d-L\d/i)) tags.connection = 'line_line';

        // Total/Sum indicator
        if (desc.toLowerCase().includes('total') || desc.toLowerCase().includes('sum')) {
            tags.aggregate = 'total';
        }

        return {
            pollGroup,
            label,
            mqttTopic,
            measurement,
            tags
        };
    },

    quickAddRegister(reg) {
        // Ensure we have description from allRegisters
        if (!reg.description) {
            // Try to find description from allRegisters
            const allRegs = this.flattenRegisters();
            const fullReg = allRegs.find(r => r.address === reg.address);
            if (fullReg && fullReg.description) {
                reg.description = fullReg.description;
            }
        }

        const defaults = this.generateRegisterDefaults(reg);

        // Create register config with smart defaults
        const newReg = {
            address: reg.address,
            name: reg.name,
            description: reg.description || '',
            label: defaults.label,
            unit: reg.unit || '',
            data_type: reg.data_type || 'float',
            poll_group: defaults.pollGroup,
            json_path: reg.json_path || '',    // HTTP/JSON devices poll by path
            topic: reg.topic || '',            // MQTT input: subscribe topic
            scale: reg.scale || 1,             // Modbus SunSpec/ratio scaling
            mqtt_enabled: true,
            mqtt_topic: defaults.mqttTopic,
            influxdb_enabled: true,
            influxdb_measurement: defaults.measurement,
            influxdb_tags: defaults.tags,
            ui_show_on_dashboard: true,
            ui_widget: 'value',
            // quick-add inherits the convention default (phase/category) so a
            // one-click widget lands with the right identity color
            ui_config: { color: this._defaultColorFor(reg) }
        };

        this.selectedRegisters.push(newReg);
        this.saveSelectedRegistersQuiet();
        this.showToast('success', this.t('toast.added', 'Added'), `${defaults.label} · ${defaults.pollGroup}`);
        this._afterSelectionChange();
    },

    // a tick or an untick is saved at once; every view of the selection follows
    _afterSelectionChange() {
        this.renderRegistersTable();
        if (this.updateConfigTabs) this.updateConfigTabs();
        this.renderSelectedRegistersList();
    },

    removeRegisterFromTable(address) {
        const reg = this.selectedRegisters.find(r => r.address === address);
        if (reg) {
            this.selectedRegisters = this.selectedRegisters.filter(r => r.address !== address);
            this.saveSelectedRegistersQuiet();
            this.showToast('info', this.t('toast.removed', 'Removed'), `${reg.name} ${this.t('toast.fromConfig', 'removed from configuration')}`);
            this._afterSelectionChange();
        }
    },

    async saveSelectedRegistersQuiet() {
        try {
            const response = await fetch('/api/registers/selected' + this._regDeviceQS(), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(this.selectedRegisters)
            });

            if (!response.ok) {
                throw new Error('Save failed');
            }
            // the selection census on the source picker follows the save
            this.loadSelectedRegisters?.();
        } catch (error) {
            this.showToast('error', this.t('toast.saveFailed', 'Save Failed'), error.message);
        }
    },

    // ============ Add Modal ============

    // Add a register NOT in the catalog: a custom register with a manually
    // entered address. It is added straight to the Selected set (the poller
    // reads by address, so it works whether or not it exists in the map).
    // Is the device currently open in the Measurements view an HTTP/JSON source?
    // HTTP measurements are keyed by json_path, not a Modbus address.
    _regSourceProto() {
        const src = (this._regSources || []).find(x => x.id === this._regSource);
        return src ? String(src.protocol || '').toLowerCase() : null;
    },

    _regDeviceIsHttp() {
        const sp = this._regSourceProto();
        if (sp) return sp === 'http';
        const id = this._regDevice || this._primaryDeviceId();
        const dev = (this._devices || []).find(d => d.id === id);
        return dev?.protocol === 'http';
    },

    // MQTT-input measurements key on a subscribe topic + json_path into the payload.
    _regDeviceIsMqtt() {
        const sp = this._regSourceProto();
        if (sp) return sp === 'mqtt';
        const id = this._regDevice || this._primaryDeviceId();
        const dev = (this._devices || []).find(d => d.id === id);
        return dev?.protocol === 'mqtt';
    },

    // ============ Canonical field guidance ============

    // Load the canonical dictionary once (the single source of truth for field
    // naming) and wire the register editor to it: datalist autocomplete on Name
    // + a hint that flags non-canonical names, and manual-edit tracking so we
    // stop auto-overwriting the topic/measurement once the user touches them.
    async _loadCanonicalFields() {
        if (this._canonicalFields) return this._canonicalFields;
        try {
            const r = await fetch('/api/canonical-fields');
            if (!r.ok) return null;
            const data = await r.json();
            this._canonicalFields = data.fields || {};
            this._canonicalNames = Object.keys(this._canonicalFields);
            const dl = document.getElementById('canonicalNamesList');
            if (dl) {
                dl.innerHTML = this._canonicalNames.map(n => {
                    const f = this._canonicalFields[n];
                    return `<option value="${this._esc(n)}">${this._esc(f.description || '')}</option>`;
                }).join('');
            }
            // once the user edits topic/measurement by hand, stop auto-filling it
            ['addMqttTopic', 'addInfluxMeasurement'].forEach(id => {
                const el = document.getElementById(id);
                if (el && !el._canonWired) {
                    el.addEventListener('input', () => { el.dataset.canonAuto = ''; });
                    el._canonWired = true;
                }
            });
        } catch (e) { /* guidance is best-effort */ }
        return this._canonicalFields;
    },

    _isCanonical(name) {
        return !!(this._canonicalFields && this._canonicalFields[String(name).toLowerCase()]);
    },

    // Closest canonical name (client-side "did you mean"), or null — mirrors the
    // server's difflib cutoff of 0.6 with a normalized Levenshtein ratio.
    _canonicalSuggest(name) {
        if (!this._canonicalNames) return null;
        const a = String(name).toLowerCase();
        let best = null, bestScore = 0;
        for (const c of this._canonicalNames) {
            const d = this._levenshtein(a, c);
            const score = 1 - d / Math.max(a.length, c.length, 1);
            if (score > bestScore) { bestScore = score; best = c; }
        }
        return bestScore >= 0.6 ? best : null;
    },

    _levenshtein(a, b) {
        const m = a.length, n = b.length;
        if (!m) return n; if (!n) return m;
        let prev = Array.from({ length: n + 1 }, (_, i) => i);
        for (let i = 1; i <= m; i++) {
            let cur = [i];
            for (let j = 1; j <= n; j++) {
                cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1,
                    prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
            }
            prev = cur;
        }
        return prev[n];
    },

    // React to typing in the custom-register Name field: show canonical status
    // and auto-fill the hierarchical MQTT topic + InfluxDB measurement from the
    // dictionary (only while the user hasn't overridden them by hand).
    _checkCanonicalName() {
        const nameEl = document.getElementById('addNameInput');
        const hint = document.getElementById('canonicalNameHint');
        if (!nameEl || !hint) return;
        const raw = nameEl.value.trim();
        const name = raw.toLowerCase();
        if (!raw) { hint.textContent = ''; hint.className = 'field-hint'; return; }

        const f = this._canonicalFields && this._canonicalFields[name];
        const topicEl = document.getElementById('addMqttTopic');
        const measEl = document.getElementById('addInfluxMeasurement');
        if (f) {
            hint.className = 'field-hint canon-ok';
            hint.textContent = '✓ ' + this.t('registers.canon.ok', 'Canonical field') +
                (f.description ? ' — ' + f.description : '');
            // auto-fill topic/measurement unless the user typed their own
            if (topicEl && (!topicEl.value || topicEl.dataset.canonAuto === '1')) {
                topicEl.value = f.mqtt_topic; topicEl.dataset.canonAuto = '1';
            }
            if (measEl && (!measEl.value || measEl.dataset.canonAuto === '1')) {
                measEl.value = f.measurement; measEl.dataset.canonAuto = '1';
            }
        } else {
            const s = this._canonicalSuggest(name);
            hint.className = 'field-hint canon-warn';
            if (s) {
                hint.innerHTML = '⚠ ' + this._esc(this.t('registers.canon.non', 'Non-canonical name')) +
                    ' — ' + this._esc(this.t('registers.canon.didYouMean', 'did you mean')) +
                    ` <a href="#" ${this._act('_applyCanonicalSuggestion', [s])}><code>${this._esc(s)}</code></a>?`;
            } else {
                hint.textContent = '⚠ ' + this.t('registers.canon.non', 'Non-canonical name') +
                    ' — ' + this.t('registers.canon.pickFromList', 'pick a canonical name from the list for uniform output.');
            }
        }
    },

    _applyCanonicalSuggestion(name) {
        const nameEl = document.getElementById('addNameInput');
        if (nameEl) { nameEl.value = name; this._checkCanonicalName(); nameEl.focus(); }
    },

    openCustomRegisterModal() {
        this._loadCanonicalFields();
        const modal = document.getElementById('addRegisterModal');
        modal.dataset.mode = 'custom';
        modal.dataset.description = '';
        const http = this._regDeviceIsHttp();
        const mqtt = this._regDeviceIsMqtt();
        modal.dataset.http = http ? '1' : '';
        modal.dataset.mqtt = mqtt ? '1' : '';
        const title = document.getElementById('addModalTitle');
        if (title) title.textContent = this.t('registers.addCustom', 'Add measurement');
        document.getElementById('addInfoBox').style.display = 'none';
        document.getElementById('addCustomFields').style.display = '';
        // clear editable identity
        ['addAddressInput', 'addNameInput', 'addUnitInput', 'addCategoryInput', 'addDescInput', 'addJsonPathInput', 'addScaleInput', 'addTopicInput']
            .forEach(id => { const e = document.getElementById(id); if (e) e.value = ''; });
        // reset canonical guidance for a fresh custom register
        const _ch = document.getElementById('canonicalNameHint');
        if (_ch) { _ch.textContent = ''; _ch.className = 'field-hint'; }
        ['addMqttTopic', 'addInfluxMeasurement'].forEach(id => {
            const e = document.getElementById(id); if (e) e.dataset.canonAuto = '';
        });
        const _jpp = document.getElementById('jsonPathPicker');
        if (_jpp) _jpp.style.display = 'none';
        document.getElementById('addDataTypeInput').value = 'float';
        const _sc = document.getElementById('addScaleInput'); if (_sc) _sc.value = '1';
        // Protocol-aware fields: HTTP keys on json_path (address auto-assigned),
        // MQTT on a subscribe topic + json_path, Modbus on a numeric address.
        const addrGroup = document.getElementById('addAddressGroup');
        const jpGroup = document.getElementById('addJsonPathGroup');
        const rtGroup = document.getElementById('addRegTypeGroup');
        const topicGroup = document.getElementById('addTopicGroup');
        const byPath = http || mqtt;                       // address auto-assigned
        if (addrGroup) addrGroup.style.display = byPath ? 'none' : '';
        if (jpGroup) jpGroup.style.display = byPath ? '' : 'none';
        if (rtGroup) rtGroup.style.display = byPath ? 'none' : '';   // Modbus only (FC3/FC4)
        if (topicGroup) topicGroup.style.display = mqtt ? '' : 'none';
        const rt = document.getElementById('addRegType'); if (rt) rt.value = 'holding';
        // sensible routing defaults
        document.getElementById('addLabel').value = '';
        document.getElementById('addPollGroup').value = 'normal';
        document.getElementById('addWidget').value = 'value';
        document.getElementById('addMqttEnabled').checked = true;
        document.getElementById('addMqttTopic').value = '';
        document.getElementById('addInfluxEnabled').checked = true;
        document.getElementById('addInfluxMeasurement').value = '';
        document.getElementById('addInfluxTags').value = '{}';
        document.getElementById('addThresholdEnabled').checked = false;
        this.openModal('addRegisterModal');
        setTimeout(() => document.getElementById(
            mqtt ? 'addTopicInput' : http ? 'addJsonPathInput' : 'addAddressInput').focus(), 100);
    },

    openAddModal(reg) {
        // catalog mode — driven by an existing register from the map
        const modal0 = document.getElementById('addRegisterModal');
        modal0.dataset.mode = 'catalog';
        const _t = document.getElementById('addModalTitle');
        if (_t) _t.textContent = this.t('registers.configure', 'Configure measurement');
        document.getElementById('addInfoBox').style.display = '';
        document.getElementById('addCustomFields').style.display = 'none';
        // Ensure we have description from allRegisters
        if (!reg.description) {
            const allRegs = this.flattenRegisters();
            const fullReg = allRegs.find(r => r.address === reg.address);
            if (fullReg && fullReg.description) {
                reg.description = fullReg.description;
            }
        }

        // Generate smart defaults
        const defaults = this.generateRegisterDefaults(reg);

        // Default widget color by phase/category convention (Settings→General):
        // the color input needs a literal hex, so var(--phase-lN) is resolved
        // against the current theme here.
        const colEl = document.getElementById('addGaugeColor');
        if (colEl) colEl.value = this._resolveCssColor(this._defaultColorFor(reg)) || '#3b82f6';

        // Store register data in hidden fields
        document.getElementById('addAddress').value = reg.address;
        document.getElementById('addName').value = reg.name;
        document.getElementById('addUnit').value = reg.unit || '';
        document.getElementById('addDataType').value = reg.data_type || 'float';
        document.getElementById('addCategory').value = reg.category || '';

        // Store description + json_path in data attributes on the modal
        const modal = document.getElementById('addRegisterModal');
        modal.dataset.description = reg.description || '';
        modal.dataset.jsonPath = reg.json_path || '';
        modal.dataset.scale = reg.scale != null ? String(reg.scale) : '1';
        modal.dataset.regType = reg.register_type || 'holding';

        // Display info - show description prominently. For HTTP sources the numeric
        // address is just an internal key, so show the json_path instead.
        const _isHttp = this._regDeviceIsHttp();
        const _keyLabel = document.getElementById('addInfoKeyLabel');
        if (_keyLabel) _keyLabel.textContent = _isHttp ? this.t('registers.custom.jsonPath', 'JSON path') + ':' : 'Address:';
        document.getElementById('addAddressDisplay').textContent =
            _isHttp ? (reg.json_path || '—') : reg.address;
        document.getElementById('addNameDisplay').textContent = reg.description || reg.name;

        // Set defaults from smart generation
        document.getElementById('addLabel').value = defaults.label;
        document.getElementById('addPollGroup').value = defaults.pollGroup;

        // Set MQTT defaults
        document.getElementById('addWidget').value = 'value';
        document.getElementById('addMqttEnabled').checked = true;
        document.getElementById('addMqttTopic').value = defaults.mqttTopic;

        // Set InfluxDB defaults
        document.getElementById('addInfluxEnabled').checked = true;
        document.getElementById('addInfluxMeasurement').value = defaults.measurement;
        document.getElementById('addInfluxTags').value = JSON.stringify(defaults.tags, null, 2);

        // Auto-fill thresholds based on detected type
        this.autoFillThresholds('add', reg.unit, reg.name);

        // Show modal
        this.openModal('addRegisterModal');
    },

    closeAddModal() {
        this.closeModal('addRegisterModal');
    },

    openQueryModal() {
        // Reset the form
        document.getElementById('queryAddress').value = '';
        document.getElementById('queryDataType').value = 'float';
        document.getElementById('queryResultContainer').style.display = 'none';
        document.getElementById('queryResult').innerHTML = '';
        this.openModal('queryModal');
        // Focus on address input
        setTimeout(() => document.getElementById('queryAddress').focus(), 100);
    },

    closeQueryModal() {
        this.closeModal('queryModal');
    },

    saveNewRegister() {
        const modal = document.getElementById('addRegisterModal');

        // Custom mode: pull the manually-entered identity into the hidden fields
        // the rest of this function reads from (address accepts 0x… or decimal).
        if (modal.dataset.mode === 'custom') {
            const isHttp = modal.dataset.http === '1';
            const isMqtt = modal.dataset.mqtt === '1';
            const nm = document.getElementById('addNameInput').value.trim();
            let addr;
            if (isHttp || isMqtt) {
                // HTTP/MQTT measurements key on json_path (+ topic for MQTT); the
                // address is just an internal key → auto-assign the next free one.
                const jp = document.getElementById('addJsonPathInput').value.trim();
                if (isHttp && !jp) {
                    this.showToast('error', this.t('registers.custom.noJsonPath', 'JSON path required'),
                                   this.t('registers.custom.noJsonPathMsg', 'Enter where to read this value, e.g. Body.Data.PowerReal_P_Sum.'));
                    return;
                }
                if (isMqtt && !document.getElementById('addTopicInput').value.trim()) {
                    this.showToast('error', this.t('registers.custom.noTopic', 'Topic required'),
                                   this.t('registers.custom.noTopicMsg', 'Enter the MQTT topic this value is published on.'));
                    return;
                }
                const used = new Set([
                    ...this.selectedRegisters.map(r => r.address),
                    ...this.flattenRegisters().map(r => r.address),
                ]);
                addr = 0;
                while (used.has(addr)) addr++;
            } else {
                const raw = document.getElementById('addAddressInput').value.trim();
                addr = /^0x/i.test(raw) ? parseInt(raw, 16) : parseInt(raw, 10);
                if (!Number.isInteger(addr) || addr < 0 || addr > 65535) {
                    this.showToast('error', this.t('registers.custom.badAddr', 'Invalid address'),
                                   this.t('registers.custom.badAddrMsg', 'Use a decimal or 0x… value in 0–65535.'));
                    return;
                }
            }
            if (!nm) {
                this.showToast('error', this.t('registers.custom.noName', 'Name required'), '');
                return;
            }
            document.getElementById('addAddress').value = addr;
            document.getElementById('addName').value = nm;
            document.getElementById('addUnit').value = document.getElementById('addUnitInput').value.trim();
            document.getElementById('addDataType').value = document.getElementById('addDataTypeInput').value;
            document.getElementById('addCategory').value = document.getElementById('addCategoryInput').value.trim();
            modal.dataset.description = document.getElementById('addDescInput').value.trim();
            if (!document.getElementById('addLabel').value.trim())
                document.getElementById('addLabel').value = nm;
        }

        const address = parseInt(document.getElementById('addAddress').value);

        // Check if already monitored
        if (this.selectedRegisters.some(r => r.address === address)) {
            this.showToast('warning', this.t('toast.alreadyMonitored', 'Already Monitored'), this.t('toast.alreadyMonitoredMsg', 'This measurement is already being monitored'));
            return;
        }

        // Build register config
        const newReg = {
            address: address,
            name: document.getElementById('addName').value,
            description: modal.dataset.description || '',
            label: document.getElementById('addLabel').value,
            unit: document.getElementById('addUnit').value,
            data_type: document.getElementById('addDataType').value || 'float',
            poll_group: document.getElementById('addPollGroup').value,
            json_path: (modal.dataset.mode === 'custom'
                ? (document.getElementById('addJsonPathInput')?.value.trim() || '')
                : (modal.dataset.jsonPath || '')),
            topic: (modal.dataset.mode === 'custom'
                ? (document.getElementById('addTopicInput')?.value.trim() || '')
                : (modal.dataset.topic || '')),
            scale: (modal.dataset.mode === 'custom'
                ? (parseFloat(document.getElementById('addScaleInput')?.value) || 1)
                : (parseFloat(modal.dataset.scale) || 1)),
            register_type: (modal.dataset.mode === 'custom'
                ? (document.getElementById('addRegType')?.value || 'holding')
                : (modal.dataset.regType || 'holding')),
            mqtt_enabled: document.getElementById('addMqttEnabled').checked,
            mqtt_topic: document.getElementById('addMqttTopic').value,
            influxdb_enabled: document.getElementById('addInfluxEnabled').checked,
            influxdb_measurement: document.getElementById('addInfluxMeasurement').value,
            influxdb_tags: {},
            ui_show_on_dashboard: true,
            ui_widget: document.getElementById('addWidget').value,
            ui_config: (() => {
                const cfg = {};
                const min = document.getElementById('addGaugeMin').value;
                const max = document.getElementById('addGaugeMax').value;
                if (min !== '') cfg.min = parseFloat(min);
                if (max !== '') cfg.max = parseFloat(max);
                cfg.color = document.getElementById('addGaugeColor').value;
                return cfg;
            })(),
            thresholds: this.readThresholdsFromForm('add')
        };

        // Parse InfluxDB tags
        const tagsStr = document.getElementById('addInfluxTags').value;
        if (tagsStr) {
            try {
                newReg.influxdb_tags = JSON.parse(tagsStr);
            } catch (e) {
                this.showToast('error', this.t('toast.invalidJson', 'Invalid JSON'), this.t('toast.influxTagsJson', 'InfluxDB tags must be valid JSON'));
                return;
            }
        }

        // Add and save
        this.selectedRegisters.push(newReg);
        this.saveSelectedRegistersQuiet();
        this.showToast('success', this.t('toast.added', 'Added'), `${newReg.label} ${this.t('toast.toConfig', 'added to configuration')}`);
        this.closeAddModal();
        this.renderRegistersTable();
        // keep the Selected tab (+ its category tabs) in sync
        this.updateConfigTabs();
        this.renderSelectedRegistersList();
    },

    async queryRegister() {
        const address = parseInt(document.getElementById('queryAddress').value);
        const dataType = document.getElementById('queryDataType').value;

        if (!address) {
            this.showToast('warning', this.t('toast.missingAddress', 'Missing Address'), this.t('toast.enterAddress', 'Please enter an address'));
            return;
        }

        const resultContainer = document.getElementById('queryResultContainer');
        const resultDiv = document.getElementById('queryResult');

        // Show the result container
        resultContainer.style.display = 'block';
        resultDiv.innerHTML = '<div class="loading"><i aria-hidden="true" class="bi bi-arrow-repeat spin"></i> Querying…</div>';

        // Look up register info from our database
        const allRegs = this.flattenRegisters();
        const regInfo = allRegs.find(r => r.address === address);
        const isConfigured = this.selectedRegisters.some(r => r.address === address);

        try {
            const response = await fetch('/api/query/register', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ address, data_type: dataType, scale: regInfo?.scale,
                    ...(this._regDeviceIdOrNull() ? { device_id: this._regDeviceIdOrNull() } : {}) })
            });

            if (!response.ok) {
                throw new Error('Query failed');
            }

            const data = await response.json();

            const displayValue = typeof data.value === 'number' ?
                data.value.toFixed(4) : data.value;

            // Build detailed result HTML
            let html = `
                <div class="query-result-header">
                    <div class="result-value">${this._esc(displayValue)}</div>
                    <div class="result-unit">${this._esc(regInfo?.unit || '')}</div>
                </div>
            `;

            if (regInfo) {
                html += `
                    <div class="result-details">
                        <div class="detail-row">
                            <span class="detail-label">${this.t('lbl.description', "Description")}</span>
                            <span class="detail-value">${this._esc(regInfo.description || '-')}</span>
                        </div>
                        <div class="detail-row">
                            <span class="detail-label">${this.t('lbl.name', "Name")}</span>
                            <span class="detail-value mono">${this._esc(regInfo.name)}</span>
                        </div>
                        <div class="detail-row">
                            <span class="detail-label">${this.t('lbl.address', "Address")}</span>
                            <span class="detail-value mono">${data.address}</span>
                        </div>
                        <div class="detail-row">
                            <span class="detail-label">${this.t('lbl.category', "Category")}</span>
                            <span class="detail-value">${this._esc(regInfo.category)}${regInfo.subtype ? ' / ' + this._esc(regInfo.subtype) : ''}</span>
                        </div>
                        <div class="detail-row">
                            <span class="detail-label">${this.t('lbl.dataType', "Data Type")}</span>
                            <span class="detail-value">${data.data_type}</span>
                        </div>
                        <div class="detail-row">
                            <span class="detail-label">${this.t('lbl.timestamp', "Timestamp")}</span>
                            <span class="detail-value">${new Date(data.timestamp).toLocaleString()}</span>
                        </div>
                    </div>
                    <div class="result-actions">
                        ${isConfigured
                            ? '<span class="badge configured"><i aria-hidden="true" class="bi bi-check-circle"></i> Monitored</span>'
                            : `<button class="btn btn-primary btn-sm" id="queryConfigureBtn">
                                <i aria-hidden="true" class="bi bi-plus-circle"></i> Add to Monitoring
                               </button>
                               <button class="btn btn-ghost btn-sm" id="queryQuickAddBtn">
                                <i aria-hidden="true" class="bi bi-lightning"></i> Quick Add
                               </button>`
                        }
                    </div>
                `;
            } else {
                html += `
                    <div class="result-details">
                        <div class="detail-row">
                            <span class="detail-label">${this.t('lbl.address', "Address")}</span>
                            <span class="detail-value mono">${data.address}</span>
                        </div>
                        <div class="detail-row">
                            <span class="detail-label">${this.t('lbl.dataType', "Data Type")}</span>
                            <span class="detail-value">${data.data_type}</span>
                        </div>
                        <div class="detail-row">
                            <span class="detail-label">${this.t('lbl.timestamp', "Timestamp")}</span>
                            <span class="detail-value">${new Date(data.timestamp).toLocaleString()}</span>
                        </div>
                        <div class="result-note">
                            <i aria-hidden="true" class="bi bi-info-circle"></i> This address is not in the known measurements database.
                        </div>
                    </div>
                `;
            }

            resultDiv.innerHTML = html;

            // Add event listeners for action buttons
            if (regInfo && !isConfigured) {
                document.getElementById('queryConfigureBtn')?.addEventListener('click', () => {
                    this.closeQueryModal();
                    this.openAddModal(regInfo);
                });
                document.getElementById('queryQuickAddBtn')?.addEventListener('click', () => {
                    this.quickAddRegister(regInfo);
                    this.closeQueryModal();
                    this.showToast('success', this.t('toast.added', 'Added'), `${regInfo.name} ${this.t('toast.toMonitoring', 'added to monitoring')}`);
                });
            }

            // Add to history
            this.queryHistory.unshift({
                address: data.address,
                value: displayValue,
                dataType: data.data_type,
                timestamp: data.timestamp,
                description: regInfo?.description
            });

            this.renderQueryHistory();

        } catch (error) {
            resultDiv.innerHTML = `<div class="result-error"><i aria-hidden="true" class="bi bi-exclamation-triangle"></i> Error: ${this._esc(error.message)}</div>`;
        }
    },

    renderQueryHistory() {
        const container = document.getElementById('queryHistory');
        if (!container) return;
        container.innerHTML = '';

        this.queryHistory.slice(0, 20).forEach(item => {
            const div = document.createElement('div');
            div.className = 'history-item';
            div.innerHTML = `
                <span class="address">${this._esc(item.address)}</span>
                <span class="value">${this._esc(item.value)}</span>
                <span class="time">${new Date(item.timestamp).toLocaleTimeString()}</span>
            `;
            container.appendChild(div);
        });
    },

    renderSelectedRegistersList() {
        const container = document.getElementById('selectedRegistersList');
        if (!container) return;
        const t = this.t.bind(this);
        let filtered = this.selectedRegisters.filter(reg => {
            if (this.configTab !== 'all' && (reg.category || reg._category) !== this.configTab) return false;
            if (this.configSearch) {
                const searchStr = `${reg.address} ${reg.label} ${reg.name} ${reg.description || ''} ${reg.unit || ''} ${reg.poll_group} ${reg.json_path || ''}`.toLowerCase();
                if (!searchStr.includes(this.configSearch)) return false;
            }
            return true;
        });
        const countEl = document.getElementById('registerCount');
        if (countEl) {
            const total = this.selectedRegisters.length, shown = filtered.length;
            countEl.textContent = shown === total ? `${total} measurement${total !== 1 ? 's' : ''}` : `${shown} of ${total} measurements`;
        }
        this._updateRegTabCounts();
        if (filtered.length === 0) {
            container.innerHTML = `<div class="empty-state">${this.selectedRegisters.length === 0
                ? t('registers.noneSelected', 'Nothing is read yet — tick measurements under "All available".')
                : t('registers.noneMatch', 'No measurements match the current filter.')}</div>`;
            return;
        }
        // grouped by what they measure, in the catalog's order
        const order = this._regCategoryOrder();
        const catOf = r => r.category || r._category || 'other';
        filtered = [...filtered].sort((a, b) => {
            const ia = order.indexOf(catOf(a)), ib = order.indexOf(catOf(b));
            return ((ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib)) || (a.address - b.address);
        });
        container.innerHTML = `
            <table class="selected-registers-table reg-table">
                <thead><tr>
                    <th class="reg-tick"><span class="sr-only">${t('registers.readCol', 'Read')}</span></th>
                    <th>${t('registers.measurement', 'Measurement')}</th>
                    <th>${t('registers.where', 'Where')}</th>
                    <th class="num">${t('lbl.value', 'Value')}</th>
                    <th>${t('registers.interval', 'Interval')}</th>
                    <th class="center">MQTT</th>
                    <th class="center">${t('lbl.influx', 'Influx')}</th>
                    <th>${t('lbl.actions', 'Actions')}</th>
                </tr></thead>
                <tbody id="selectedRegistersBody"></tbody>
            </table>`;
        const tbody = document.getElementById('selectedRegistersBody');
        let lastCat = null;
        filtered.forEach(reg => {
            const cat = catOf(reg);
            if (cat !== lastCat) {
                lastCat = cat;
                const gh = document.createElement('tr');
                gh.className = 'reg-group';
                gh.innerHTML = `<td colspan="8">${this._esc(this._regCatLabel(cat))}</td>`;
                tbody.appendChild(gh);
            }
            const tr = document.createElement('tr');
            tr.dataset.address = reg.address;
            const mqttTooltip = reg.mqtt_enabled && reg.mqtt_topic ? `Topic: ${reg.mqtt_topic}` : (reg.mqtt_enabled ? 'Enabled' : 'Disabled');
            let influxTooltip = 'Disabled';
            if (reg.influxdb_enabled) {
                influxTooltip = reg.influxdb_measurement || 'Enabled';
                if (reg.influxdb_tags && Object.keys(reg.influxdb_tags).length) {
                    influxTooltip += ` [${Object.entries(reg.influxdb_tags).map(([k, v]) => `${k}=${v}`).join(', ')}]`;
                }
            }
            const custom = !this._regInCatalog(reg);
            tr.innerHTML = `
                <td class="reg-tick"><input type="checkbox" checked aria-label="${t('registers.readCol', 'Read')}: ${this._esc(reg.label || reg.name)}"></td>
                <td class="label-cell">
                    <span class="reg-label">${this._esc(reg.label || reg.description || reg.name)}</span>
                    <span class="reg-name">${this._esc(reg.name)}</span>
                </td>
                <td>${this._regWhere(reg)}</td>
                <td class="value num" data-reg-name="${this._esc(reg.name)}">${this._regValueHtml(reg.name)}</td>
                <td class="reg-interval">${this._regIntervalText(reg)}</td>
                <td class="center"><span class="status-icon ${reg.mqtt_enabled ? 'active' : ''}" title="${this._esc(mqttTooltip)}">${reg.mqtt_enabled ? '&#10003;' : '&#10005;'}</span></td>
                <td class="center"><span class="status-icon ${reg.influxdb_enabled ? 'active' : ''}" title="${this._esc(influxTooltip)}">${reg.influxdb_enabled ? '&#10003;' : '&#10005;'}</span></td>
                <td class="actions-cell">
                    <button class="btn-action edit" title="${t('registers.editConfig', 'Edit how it is published')}" aria-label="${t('registers.editConfig', 'Edit how it is published')}">&#9998;</button>
                    ${custom
                        ? `<button class="btn-action remove" title="${t('common.delete', 'Delete')}" aria-label="${t('common.delete', 'Delete')}">&#10006;</button>`
                        : `<span class="reg-lock" title="${t('registers.fromTemplate', 'From the template — untick it instead of deleting')}" aria-label="${t('registers.fromTemplate', 'From the template — untick it instead of deleting')}"><i aria-hidden="true" class="bi bi-lock"></i></span>`}
                </td>`;
            tr.querySelector('input[type=checkbox]').addEventListener('change', () => this.removeRegisterFromTable(reg.address));
            tr.querySelector('.edit').addEventListener('click', () => this.editRegister(reg));
            tr.querySelector('.remove')?.addEventListener('click', () => this.removeRegisterFromTable(reg.address));
            tbody.appendChild(tr);
        });
        this._startRegLive();
    },

    editRegisterByAddress(address) {
        // dashboard context: the widget list is the DASH device's own — the
        // shared modal must read/save that list, not the Measurements page's.
        const reg = this._dashRegs().find(r => r.address === address);
        this._editFromDash = true;
        if (reg) this.editRegister(reg);
    },

    editRegister(reg) {
        document.getElementById('editAddress').value = reg.address;
        document.getElementById('editLabel').value = reg.label;
        document.getElementById('editPollGroup').value = reg.poll_group;
        document.getElementById('editWidget').value = reg.ui_widget;
        document.getElementById('editMqttEnabled').checked = reg.mqtt_enabled;
        document.getElementById('editMqttTopic').value = reg.mqtt_topic;
        document.getElementById('editInfluxEnabled').checked = reg.influxdb_enabled;
        document.getElementById('editInfluxMeasurement').value = reg.influxdb_measurement;
        document.getElementById('editInfluxTags').value = JSON.stringify(reg.influxdb_tags || {});

        // Gauge options
        document.getElementById('editGaugeMin').value = reg.ui_config?.min ?? '';
        document.getElementById('editGaugeMax').value = reg.ui_config?.max ?? '';
        document.getElementById('editGaugeColor').value =
            this._resolveCssColor(reg.ui_config?.color || this._defaultColorFor(reg)) || '#3b82f6';
        this.toggleGaugeOptions('edit', reg.ui_widget);

        // Fill thresholds - use existing or auto-detect
        this.autoFillThresholds('edit', reg.unit, reg.name, reg.thresholds);

        this.openModal('registerModal');
    },

    toggleGaugeOptions(prefix, widgetType) {
        const el = document.getElementById(`${prefix}GaugeOptions`);
        if (el) {
            el.style.display = (widgetType === 'gauge') ? 'block' : 'none';
        }
    },

    closeRegisterModal() {
        this.closeModal('registerModal');
    },

    saveRegisterEdit() {
        const address = parseInt(document.getElementById('editAddress').value);
        const fromDash = !!this._editFromDash;
        this._editFromDash = false;
        const reg = (fromDash ? this._dashRegs() : this.selectedRegisters)
            .find(r => r.address === address);

        if (reg) {
            reg.label = document.getElementById('editLabel').value;
            reg.poll_group = document.getElementById('editPollGroup').value;
            reg.ui_widget = document.getElementById('editWidget').value;
            reg.mqtt_enabled = document.getElementById('editMqttEnabled').checked;
            reg.mqtt_topic = document.getElementById('editMqttTopic').value;
            reg.influxdb_enabled = document.getElementById('editInfluxEnabled').checked;
            reg.influxdb_measurement = document.getElementById('editInfluxMeasurement').value;

            try {
                reg.influxdb_tags = JSON.parse(document.getElementById('editInfluxTags').value || '{}');
            } catch (e) {
                reg.influxdb_tags = {};
            }

            // Save gauge options
            if (!reg.ui_config) reg.ui_config = {};
            const gaugeMin = document.getElementById('editGaugeMin').value;
            const gaugeMax = document.getElementById('editGaugeMax').value;
            reg.ui_config.min = gaugeMin !== '' ? parseFloat(gaugeMin) : undefined;
            reg.ui_config.max = gaugeMax !== '' ? parseFloat(gaugeMax) : undefined;
            reg.ui_config.color = document.getElementById('editGaugeColor').value;

            // Save thresholds
            reg.thresholds = this.readThresholdsFromForm('edit');
        }

        this.closeModal();
        if (fromDash) {
            this.updateDashboard();
            this._saveDashRegisters();
        } else {
            this.renderSelectedRegistersList();
            this.updateDashboard();
            this.saveSelectedRegisters();
        }
    },

    removeRegister(address) {
        this.selectedRegisters = this.selectedRegisters.filter(r => r.address !== address);
        this.renderSelectedRegistersList();
    },

    async saveSelectedRegisters() {
        const btn = document.getElementById('saveRegistersBtn');
        this.setButtonLoading(btn, true);

        try {
            // Save registers to file
            const response = await fetch('/api/registers/selected' + this._regDeviceQS(), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(this.selectedRegisters)
            });

            if (!response.ok) {
                throw new Error('Save failed');
            }

            // Auto-reload registers in backend (no restart needed). A
            // non-primary device's save endpoint already hot-reloaded ITS
            // pollers; the global reload below is for device #1 only.
            if (this._regDeviceQS()) {
                this.showToast('success', this.t('toast.configSaved', 'Configuration Saved'), this.t('toast.deviceReloaded', 'Device measurements reloaded.'));
            } else {
                const reloadResponse = await fetch('/api/config/reload-registers', {
                    method: 'POST'
                });

                if (reloadResponse.ok) {
                    this.showToast('success', this.t('toast.configSaved', 'Configuration Saved'), this.t('toast.registersReloaded', 'Registers reloaded successfully.'));
                } else {
                    this.showToast('success', this.t('toast.configSaved', 'Configuration Saved'), this.t('toast.applyToReload', 'Saved. Apply configuration to reload.'));
                }
            }

        } catch (error) {
            this.showToast('error', this.t('toast.saveFailed', 'Save Failed'), error.message);
        } finally {
            this.setButtonLoading(btn, false, 'Save');
        }
    },

    _regDeviceQS(sep = '?') {
        // Query-string suffix routing register catalog/selection calls to the
        // currently edited device AND, when it is reached several ways, to the
        // source whose map is being edited.
        //
        // Every source has its own template and therefore its own address
        // space: the SunSpec view of an inverter reads holding registers, the
        // Solar API view reads JSON paths. Editing them as one list would make
        // neither editable, and saving one over the other would have a source
        // polling addresses that mean nothing to it.
        const id = this._regDevice;
        const parts = [];
        if (id && id !== this._primaryDeviceId()) parts.push(`device=${encodeURIComponent(id)}`);
        if (this._regSource) parts.push(`source=${encodeURIComponent(this._regSource)}`);
        return parts.length ? sep + parts.join('&') : '';
    },

    // The sources this device offers, learned from the last selection load —
    // the API returns them alongside, so choosing one costs no extra call.
    _renderRegSourceSelector(data) {
        const host = document.getElementById('regSourceBar');
        if (!host) return;
        const srcs = (data && data.sources) || [];
        this._regSources = srcs;
        // one way of being read is not a choice: say nothing rather than show a
        // dropdown with a single entry
        if (srcs.length < 2) { host.innerHTML = ''; this._regSource = ''; return; }
        if (!srcs.some(s => s.id === this._regSource)) this._regSource = srcs[0].id;
        const t = (k, d) => this.t(k, d);
        const PROTO = { http: 'HTTP', tcp: 'Modbus TCP', 'rtu-tcp': 'Modbus RTU/TCP', rtu: 'Modbus RTU', mqtt: 'MQTT' };
        host.innerHTML = `
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px;" role="group" aria-label="${t('registers.sourceLabel', 'Read from')}">
              <span style="color:var(--text-secondary);font-size:12.5px;">
                <i aria-hidden="true" class="bi bi-diagram-2"></i> ${t('registers.sourceLabel', 'Read from')}</span>
              ${srcs.map(x => `
                <button class="btn btn-sm ${x.id === this._regSource ? 'btn-primary' : 'btn-ghost'}" aria-pressed="${x.id === this._regSource}"
                        ${this._act('setRegSource', [x.id])}>
                  ${this._esc(x.id)} <span style="opacity:.75;">· ${PROTO[x.protocol] || this._esc(x.protocol)}${x.interval_s != null ? ` · ${t('registers.every', 'every')} ${x.interval_s} s` : ''}${x.selected != null ? ` · ${x.selected}${x.catalog != null ? '/' + x.catalog : ''} ${t('registers.ticked', 'ticked')}` : ''}</span>
                </button>`).join('')}
              <span class="field-hint" style="margin:0;">${t('registers.sourceHint',
                'Each source has its own map and its own intervals. Ticking a field here changes only what THIS source reads.')}</span>
            </div>`;
    },

    async setRegSource(sourceId) {
        if (this._regSource === sourceId) return;
        this._regSource = sourceId;
        await this.loadAllRegisters?.();
        await this.loadSelectedRegisters?.();
        // Loading data is not showing it: the two lists render on their own
        // schedule, so without this the operator switches source and keeps
        // looking at the previous source's map — the most misleading thing this
        // screen could do, since ticking a box then edits the wrong one.
        this.currentRegPage = 1;
        try { this.renderRegistersTable?.(); } catch (e) { console.error(e); }
        try { this.renderSelectedRegistersList?.(); } catch (e) { console.error(e); }
    },

    // The currently-scoped device id for POST bodies (on-demand queries), or
    // null for the primary — so 'Query now' reads the RIGHT device's bus.
    _regDeviceIdOrNull() {
        const id = this._regDevice;
        return (id && id !== this._primaryDeviceId()) ? id : null;
    },

    _renderRegDeviceSelectors() {
        const devices = this._devices || [];
        ['regDeviceSel', 'cfgRegDeviceSel'].forEach(id => {
            const sel = document.getElementById(id);
            if (!sel) return;
            if (devices.length <= 1) { sel.style.display = 'none'; return; }
            sel.style.display = '';
            const cur = this._regDevice || this._primaryDeviceId();
            sel.innerHTML = devices.map(d =>
                `<option value="${this._esc(d.id)}" ${d.id === cur ? 'selected' : ''}>${this._esc(d.name || d.id)}</option>`).join('');
            if (!sel._wired) {
                sel._wired = true;
                sel.addEventListener('change', () => this.setRegDevice(sel.value));
            }
        });
    },

    async setRegDevice(id) {
        this._regDevice = id;
        // a source id belongs to ONE device; carrying it across would ask for a
        // map that device has never heard of
        this._regSource = '';
        this._renderRegDeviceSelectors();               // keep both selects in sync
        await this.loadAllRegisters();
        await this.loadSelectedRegisters();
        if (this.currentPage === 'registers') this.renderRegistersTable();
        if (this.currentPage === 'config') {
            this.updateConfigTabs();
            this.renderSelectedRegistersList();
            this.renderPollGroups();
        }
    },

    _maybeResetRegDevice() {
        if (this._regDevice && this._regDevice !== this._primaryDeviceId()) {
            this._regDevice = this._primaryDeviceId();
            this.loadAllRegisters();
            this.loadSelectedRegisters();
        }
    },

    // empty-state shortcut: "primary" = the primary device, "view" = the one on screen
    _jumpToRegisters(which) {
        this.jumpToDeviceRegisters(which === 'view' ? this._viewDeviceId() : this._primaryDeviceId());
    },

    async jumpToDeviceRegisters(id) {
        if (this.currentPage !== 'devices') this.navigateTo('devices');
        await this.setRegDevice(id);
        // Register editor overlays the Devices page (hide list + detail).
        const list = document.getElementById('devicesListView');
        const detail = document.getElementById('deviceDetailView');
        if (list) list.style.display = 'none';
        if (detail) detail.style.display = 'none';
        const dev = (this._devices || []).find(d => d.id === id);
        const titleEl = document.getElementById('deviceRegistersTitle');
        if (titleEl) titleEl.textContent =
            (dev?.name || id) + ' · ' + this.t('registers.configured', 'Measurements');
        const reg = document.getElementById('deviceRegistersView');
        if (reg) reg.style.display = '';
        // Available (catalog) + Selected are both device-scoped via setRegDevice.
        this.registerSearchPage = 1;
        this.renderRegistersTable();
        this.updateConfigTabs();
        this.renderSelectedRegistersList();
        this._wireDeviceRegTabs();
        this.switchDeviceRegTab('selected');    // what is read comes first
    },

    _wireDeviceRegTabs() {
        document.querySelectorAll('#deviceRegTabs .config-main-tab').forEach(t => {
            if (t._wired) return;
            t._wired = true;
            t.addEventListener('click', () => this.switchDeviceRegTab(t.dataset.regtab));
        });
    },

    switchDeviceRegTab(name) {
        document.querySelectorAll('#deviceRegTabs .config-main-tab').forEach(t =>
            t.classList.toggle('active', t.dataset.regtab === name));
        document.querySelectorAll('#deviceRegistersView .reg-pane').forEach(p =>
            p.style.display = p.dataset.regpane === name ? '' : 'none');
        // Save applies to the Selected set only — hide it on the Available tab.
        const save = document.getElementById('saveRegistersBtn');
        if (save) save.style.display = name === 'selected' ? '' : 'none';
    },

    // Download this device's full register map. If the device has a template we
    // export it in the re-uploadable template format (round-trips with Upload
    // map); otherwise we export the live catalog as Janitza-model JSON.
    downloadRegisterMap() {
        const id = this._regDevice || this._primaryDeviceId();
        const dev = (this._devices || []).find(d => d.id === id);
        if (dev?.template) {
            const a = document.createElement('a');
            a.href = `/api/device-templates/${encodeURIComponent(dev.template)}/export`;
            a.download = `${dev.template}.json`;
            document.body.appendChild(a); a.click(); a.remove();
            return;
        }
        const map = { device: id, template: '', registers: this.flattenRegisters() };
        const blob = new Blob([JSON.stringify(map, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = `${id}_register_map.json`;
        document.body.appendChild(a); a.click(); a.remove();
        URL.revokeObjectURL(url);
    },

    // Upload a register map (device-template JSON) and assign it to the current
    // device. The primary keeps its built-in Janitza catalog, so it's blocked
    // there to protect the byte-identical migration.
    uploadDeviceMap() {
        const id = this._regDevice || this._primaryDeviceId();
        if (id === this._primaryDeviceId()) {
            this.showToast('info', this.t('registers.upload.primaryTitle', 'Uses built-in map'),
                           this.t('registers.upload.primaryMsg',
                                  'The primary device keeps its built-in measurement map. Upload applies to other devices.'));
            return;
        }
        const input = document.getElementById('devTplUploadInput');
        input.onchange = async () => {
            const file = input.files[0];
            input.value = '';
            if (!file) return;
            let data;
            try { data = JSON.parse(await file.text()); }
            catch (e) {
                this.showToast('error', this.t('devtpl.uploadFail', 'Upload failed'),
                               this.t('devtpl.badJson', 'Not valid JSON: ') + e.message);
                return;
            }
            await this._uploadMapAndAssign(data, id, false);
        };
        input.click();
    },

    async _uploadMapAndAssign(data, deviceId, overwrite) {
        try {
            const r = await fetch('/api/device-templates/upload', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ template: data, overwrite }),
            });
            const res = await r.json();
            if (r.status === 409 && !res.builtin) {
                if (confirm(this.t('devtpl.overwriteConfirm',
                    'A template with this id already exists.\n\nYes: overwrite it with the uploaded file.\nNo: cancel the upload.')))
                    return this._uploadMapAndAssign(data, deviceId, true);
                return;
            }
            if (!r.ok) {
                const errs = res.detail?.errors || ['upload failed'];
                this.showToast('error', this.t('devtpl.uploadFail', 'Upload failed'), errs.slice(0, 4).join('\n'));
                return;
            }
            const tplId = res.template.id;
            // assign the imported template to the device — send its FULL current
            // config with only the template swapped (the API validates connection).
            const dev = (this._devices || []).find(d => d.id === deviceId);
            const c = dev?.connection || {};
            const payload = {
                id: deviceId, name: dev?.name || deviceId, template: tplId,
                enabled: dev?.enabled !== false,
                connection: (dev?.protocol === 'rtu')
                    ? { protocol: 'rtu', serial_port: c.serial_port, baudrate: c.baudrate,
                        parity: c.parity, stopbits: c.stopbits, unit_id: c.unit_id }
                    : { protocol: 'tcp', host: c.host || dev?.host, port: c.port || dev?.port,
                        unit_id: c.unit_id ?? dev?.unit_id, timeout: c.timeout ?? 3 },
                mqtt: { topic_prefix: dev?.mqtt_topic_prefix },
                influxdb: { bucket: dev?.influxdb_bucket, device_tag: dev?.influxdb_device_tag },
                ha_discovery_enabled: dev?.ha_discovery_enabled !== false,
            };
            const put = await fetch(`/api/devices/${encodeURIComponent(deviceId)}`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (!put.ok) {
                const errs = (await put.json()).detail?.errors || ['assign failed'];
                this.showToast('error', this.t('registers.upload.assignFail', 'Template imported but assign failed'),
                               errs.join(' · '));
                return;
            }
            this.showToast('success', this.t('registers.upload.done', 'Measurement map applied'),
                           `${tplId} · ${res.template.registers} reg`);
            await this._fetchDevices(true);
            await this.setRegDevice(deviceId);      // reload catalog + selected for this device
            this.renderRegistersTable();
        } catch (e) {
            this.showToast('error', this.t('devtpl.uploadFail', 'Upload failed'), e.message);
        }
    },

    // ── Modbus write (FC5/FC6/FC16) — gated, non-primary only ─────────────
    openWriteModal() {
        // F3a: no hardcoded read-only — the per-device write LOCK decides
        // (the primary ships locked by default; the envelope lookup in
        // submitWrite surfaces the lock with a clear message).
        const id = this._regDevice || this._primaryDeviceId();
        this._writeDeviceId = id;
        ['writeAddr', 'writeValue', 'writeLease'].forEach(k => { const e = document.getElementById(k); if (e) e.value = ''; });
        document.getElementById('writeScale').value = '1';
        document.getElementById('writeResult').innerHTML = '';
        document.getElementById('writeRegType').value = 'holding';
        this._syncWriteForm();
        this.openModal('writeModal');
    },

    _syncWriteForm() {
        const coil = document.getElementById('writeRegType').value === 'coil';
        document.getElementById('writeHoldingFields').style.display = coil ? 'none' : '';
        document.getElementById('writeCoilHint').style.display = coil ? '' : 'none';
    },

    async submitWrite(btn) {
        const id = this._writeDeviceId;
        const rtype = document.getElementById('writeRegType').value;
        const address = parseInt(document.getElementById('writeAddr').value, 10);
        if (!Number.isInteger(address)) {
            this.showToast('error', this.t('toast.missingAddress', 'Missing Address'),
                           this.t('toast.enterAddress', 'Please enter an address'));
            return;
        }
        const raw = document.getElementById('writeValue').value.trim();
        const body = { register_type: rtype, address };
        if (rtype === 'coil') {
            body.value = ['1', 'true', 'on'].includes(raw.toLowerCase()) ? 1 : 0;
        } else {
            body.data_type = document.getElementById('writeDataType').value;
            body.scale = parseFloat(document.getElementById('writeScale').value) || 1;
            const num = Number(raw);
            body.value = (raw !== '' && Number.isFinite(num)) ? num : raw;
        }
        const leaseS = parseInt(document.getElementById('writeLease').value, 10);
        if (Number.isInteger(leaseS) && leaseS > 0) body.lease_ms = leaseS * 1000;

        // Envelope lookup (F3a): declared register → its guards enforce and the
        // server owns the encoding; undeclared → raw path with an explicit
        // unguarded acknowledgement in the confirm card.
        let info = null;
        try {
            info = await (await fetch(`/api/devices/${encodeURIComponent(id)}/write-info/${rtype}/${address}`)).json();
        } catch (e) { /* offline lookup → treat as undeclared */ }
        const box = document.getElementById('writeResult');
        if (info?.write_locked) {
            box.innerHTML = `<div class="settings-card" style="padding:8px 12px;color:var(--danger-text,#c0392b);">
                <i aria-hidden="true" class="bi bi-lock"></i> ${this.t('write.locked', 'This device is write-locked. Unlock it in the Outputs tab (write protection) first.')}</div>`;
            return;
        }
        let guardTxt = '';
        if (info?.declared) {
            const g = [];
            if (info.write_min != null || info.write_max != null)
                g.push(`${info.write_min ?? '−∞'} … ${info.write_max ?? '+∞'}`);
            if (info.write_allowed) g.push(`${this.t('write.allowed', 'allowed')}: ${info.write_allowed.join(', ')}`);
            // client-side pre-check so the user gets the verdict before confirming
            const v = Number(body.value);
            if (info.write_min != null && v < info.write_min || info.write_max != null && v > info.write_max
                || (info.write_allowed && !info.write_allowed.includes(v))) {
                box.innerHTML = `<div class="settings-card" style="padding:8px 12px;color:var(--danger-text,#c0392b);">
                    ${this.t('write.guardFail', 'Value is outside this register’s declared envelope')} (${this._esc(g.join(' · '))}).</div>`;
                return;
            }
            guardTxt = `<div style="font-size:12px;margin-top:4px;color:var(--success-text,#1a8f4c);">
                <i aria-hidden="true" class="bi bi-shield-check"></i> ${this.t('write.guarded', 'Declared register')} · ${this._esc(info.data_type || '')}${g.length ? ' · ' + this._esc(g.join(' · ')) : ' · ' + this.t('write.noGuards', 'no guards declared')}</div>`;
        } else {
            body.unguarded = true;
            guardTxt = `<div style="font-size:12px;margin-top:6px;color:var(--warning-text,#c77700);">
                <i aria-hidden="true" class="bi bi-exclamation-octagon"></i> <b>${this.t('write.unguardedTitle', 'UNGUARDED write')}</b> — ${this.t('write.unguardedMsg', 'this register is not declared in the template; the value is sent verbatim with the type/scale above. Nothing is checked or clamped.')}<br>
                <label style="display:flex;align-items:center;gap:6px;margin-top:6px;"><input type="checkbox" id="writeUnguardedAck"> ${this.t('write.unguardedAck', 'I understand — write it anyway')}</label></div>`;
        }
        // Review-and-confirm: a write hits real hardware, so never fire on the
        // first click — show exactly what will be written and require a second,
        // deliberate confirmation.
        this._pendingWrite = { id, body, needsAck: !info?.declared };
        const fc = rtype === 'coil' ? 'FC5' : 'FC6/16';
        const leaseTxt = body.lease_ms ? ` · ${this.t('write.lease', 'Auto-revert (s)')}: ${body.lease_ms / 1000}s` : '';
        box.innerHTML = `
            <div class="settings-card" style="border-left:3px solid #e08e0b;padding:10px 12px;">
              <div style="font-weight:600;margin-bottom:6px;color:var(--warning-text,#c77700);"><i aria-hidden="true" class="bi bi-exclamation-triangle"></i> ${this.t('write.confirmTitle', 'Confirm write to hardware')}</div>
              <div style="font-size:13px;font-variant-numeric:tabular-nums;">
                ${this.t('lbl.name', 'Name')}: <b>${this._esc(id)}</b> · ${this.t('lbl.address', 'Address')}: <b>${address}</b> (${fc}) · ${this.t('lbl.value', 'Value')}: <b>${this._esc(String(body.value))}</b>${leaseTxt}
              </div>
              ${guardTxt}
              <div style="margin-top:8px;display:flex;gap:8px;">
                <button class="btn btn-ghost btn-sm" data-action="_cancelWrite">${this.t('common.cancel', 'Cancel')}</button>
                <button class="btn btn-primary btn-sm" data-action="confirmWrite" data-with-el><i aria-hidden="true" class="bi bi-check-lg"></i> ${this.t('write.confirm', 'Confirm write')}</button>
              </div>
            </div>`;
    },

    _cancelWrite() {
        this._pendingWrite = null;
        document.getElementById('writeResult').innerHTML = '';
    },

    async confirmWrite(btn) {
        if (!this._pendingWrite) return;
        if (this._pendingWrite.needsAck && !document.getElementById('writeUnguardedAck')?.checked) {
            this.showToast('error', this.t('write.unguardedTitle', 'UNGUARDED write'),
                           this.t('write.ackFirst', 'Tick the acknowledgement first.'));
            return;
        }
        const { id, body } = this._pendingWrite;
        const address = body.address;
        const box = document.getElementById('writeResult');
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="btn-spinner"></span>'; }
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(id)}/write`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
            });
            const res = await r.json();
            if (!r.ok) {
                const errs = res.detail?.errors || [res.detail || 'write failed'];
                box.innerHTML = `<div class="settings-card" style="padding:8px 12px;color:var(--danger-text,#c0392b);">${errs.map(e => this._esc(String(e))).join('<br>')}</div>`;
                return;
            }
            const vflag = res.verified === true ? ' ✓' : (res.verified === false ? ` ⚠ ${this.t('write.mismatch', 'mismatch')}` : '');
            const lease = res.lease_ms ? ` · <i aria-hidden="true" class="bi bi-hourglass-split"></i> ${this.t('write.leases', 'auto-revert')} ${res.lease_ms / 1000}s → ${this._esc(String(res.reverts_to))}` : '';
            box.innerHTML = `<div class="settings-card" style="padding:8px 12px;color:var(--success-text,#1a8f4c);">
                <i aria-hidden="true" class="bi bi-check-circle"></i> ${this.t('write.done', 'Written')} · ${this.t('write.readBack', 'read-back')}: <b>${this._esc(String(res.read_back))}</b>${vflag}${lease}</div>`;
            this.showToast('success', this.t('write.done', 'Written'), `${id} @ ${address} = ${String(res.written)}`);
            this._pendingWrite = null;
        } catch (e) {
            box.innerHTML = `<div class="settings-card" style="padding:8px 12px;color:var(--danger-text,#c0392b);">${this._esc(e.message)}</div>`;
        } finally { if (btn) { btn.disabled = false; btn.innerHTML = orig; } }
    }
});

// ── json_path picker: flatten a live payload into clickable leaf paths ──
Object.assign(JanitzaMonitor.prototype, {

    // Flatten parsed JSON into [{path, value}] leaves using the same syntax
    // resolve_json_path() reads back: dots for keys, [i] for array indices.
    // Keys containing '.' or '[' are inexpressible in that syntax — skipped.
    _flattenJson(obj) {
        const out = [];
        const walk = (v, path) => {
            if (out.length >= 500) return;
            if (v === null || typeof v !== 'object') { out.push({ path, value: v }); return; }
            if (Array.isArray(v)) { v.forEach((x, i) => walk(x, `${path}[${i}]`)); return; }
            for (const [k, x] of Object.entries(v)) {
                if (k.includes('.') || k.includes('[')) continue;
                walk(x, path ? `${path}.${k}` : k);
            }
        };
        walk(obj, '');
        return out;
    },

    async pickJsonPath() {
        const panel = document.getElementById('jsonPathPicker');
        const list = document.getElementById('jsonPathPickerList');
        const hint = document.getElementById('jsonPathPickerHint');
        panel.style.display = '';
        hint.textContent = '';
        list.innerHTML = `<div class="field-hint" style="padding:8px;">${this._esc(
            this.t('registers.custom.pickFetching', 'Fetching a live payload…'))}</div>`;
        const device = this._regDevice || this._primaryDeviceId();
        const mqtt = this._regDeviceIsMqtt();
        const topic = mqtt ? (document.getElementById('addTopicInput')?.value || '').trim() : '';
        try {
            const r = await fetch(`/api/devices/${encodeURIComponent(device)}/payload-sample`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(topic ? { topic } : {}),
            });
            const data = await r.json();
            if (!r.ok) throw new Error((data.detail?.errors || [data.detail || r.statusText]).join('; '));
            if (!data.ok) throw new Error(data.error || 'sample failed');
            let doc;
            try {
                doc = JSON.parse(data.payload);
            } catch (_) {
                // not JSON — the whole payload IS the value (json_path stays empty)
                list.innerHTML = `<div class="field-hint" style="padding:8px;">${this._esc(
                    this.t('registers.custom.pickNotJson',
                           'Payload is not JSON — the value is the whole message, leave the path empty.'))}
                    <div class="mqtt-browse-payload" style="max-width:none;margin-top:4px;">${this._esc(String(data.payload).slice(0, 200))}</div></div>`;
                return;
            }
            this._jsonPathLeaves = this._flattenJson(doc);
            this._renderJsonPathPicker();
            hint.textContent = this.t('registers.custom.pickHint', 'Click a field to use its path.')
                + (data.topic ? ` (${data.topic})` : '');
        } catch (e) {
            list.innerHTML = `<div class="err" style="padding:8px;">${this._esc(String(e.message || e))}</div>`;
        }
    },

    _renderJsonPathPicker() {
        const list = document.getElementById('jsonPathPickerList');
        if (!list) return;
        const q = (document.getElementById('jsonPathPickerFilter')?.value || '').toLowerCase();
        const all = this._jsonPathLeaves || [];
        const rows = q ? all.filter(l => l.path.toLowerCase().includes(q)) : all;
        if (!rows.length) {
            list.innerHTML = `<div class="field-hint" style="padding:8px;">${this._esc(
                all.length ? this.t('registers.custom.pickNoMatch', 'No field matches the filter.')
                           : this.t('registers.custom.pickEmpty', 'No usable fields in the payload.'))}</div>`;
            return;
        }
        list.innerHTML = rows.slice(0, 400).map(l => {
            const val = JSON.stringify(l.value);
            const numeric = typeof l.value === 'number' || typeof l.value === 'boolean';
            return `<div class="mqtt-browse-row" role="option" tabindex="0"
                 ${this._act('_pickJsonPathLeaf', [l.path])} data-key-enter>
                <span class="mqtt-browse-topic">${this._esc(l.path)}</span>
                <span class="mqtt-browse-payload" style="${numeric ? '' : 'opacity:.55;'}">${this._esc(String(val).slice(0, 60))}</span>
            </div>`;
        }).join('');
    },

    _pickJsonPathLeaf(path) {
        const inp = document.getElementById('addJsonPathInput');
        if (inp) inp.value = path;
        const panel = document.getElementById('jsonPathPicker');
        if (panel) panel.style.display = 'none';
        inp?.focus();
    },
});
