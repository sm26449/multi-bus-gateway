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
                option.textContent = cat.charAt(0).toUpperCase() + cat.slice(1);
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

    renderRegistersTable() {
        const tbody = document.getElementById('registersTableBody');
        const searchQuery = document.getElementById('registerSearch').value.toLowerCase();
        const categoryFilter = document.getElementById('categoryFilter').value;

        // Flatten all registers
        const allRegs = this.flattenRegisters();

        // HTTP sources key on json_path, not a Modbus address — surface that column
        // (and let search hit it) so the view makes sense for a JSON device.
        const http = this._regDeviceIsHttp();
        const keyHead = document.getElementById('regColKeyHead');
        if (keyHead) keyHead.textContent = http ? this.t('registers.custom.jsonPath', 'JSON path') : 'Address';
        const queryBtn = document.getElementById('queryRegisterBtn');
        if (queryBtn) queryBtn.style.display = http ? 'none' : '';

        // Filter
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

        // Paginate
        const start = (this.registerSearchPage - 1) * this.registersPerPage;
        const paginated = filtered.slice(start, start + this.registersPerPage);

        // Render
        tbody.innerHTML = '';
        paginated.forEach(reg => {
            const configuredReg = this.selectedRegisters.find(s => s.address === reg.address);
            const isConfigured = !!configuredReg;
            const currentValue = this.currentValues[reg.address];

            const tr = document.createElement('tr');
            tr.dataset.address = reg.address;
            if (isConfigured) {
                tr.classList.add('configured');
            }

            // Build badges for monitored registers
            let badges = '';
            if (isConfigured) {
                const pollClass = `poll-${this._esc(configuredReg.poll_group)}`;
                badges = `
                    <span class="badge configured">${this.t('lbl.monitored', "Monitored")}</span>
                    <span class="badge ${pollClass}">${this._esc(configuredReg.poll_group)}</span>
                `;
            }

            // Build action buttons. Query is a direct Modbus read, so it only makes
            // sense for Modbus sources — omit it for HTTP/JSON devices.
            const queryBtn = http ? '' :
                `<button class="btn-action query" data-address="${reg.address}" title="Query Now">&#128269;</button>`;
            let actions = '';
            if (isConfigured) {
                // Configured measurement: Query, Edit, Remove
                actions = `
                    ${queryBtn}
                    <button class="btn-action edit" data-address="${reg.address}" title="Edit Config">&#9998;</button>
                    <button class="btn-action remove" data-address="${reg.address}" title="Remove">&#10005;</button>
                `;
            } else {
                // Not configured: Query, Configure (edit → adds on save), Quick Add.
                // The pencil is on every row so "edit" is reachable everywhere, not
                // only after a measurement is selected.
                actions = `
                    ${queryBtn}
                    <button class="btn-action add" data-address="${reg.address}" title="Configure &amp; add">&#9998;</button>
                    <button class="btn-action quick-add" data-address="${reg.address}" title="Quick Add">&#9889;</button>
                `;
            }

            tr.innerHTML = `
                <td class="address"${http ? ` title="${this._esc(reg.json_path || '')}"` : ''}>${http ? this._esc(reg.json_path || '—') : reg.address}</td>
                <td class="description-cell">
                    <div class="reg-description">${this._esc(reg.description || '-')}</div>
                    ${badges ? `<div class="badges">${badges}</div>` : ''}
                </td>
                <td class="name-cell">
                    <span class="reg-name-mono">${this._esc(reg.name)}</span>
                </td>
                <td>${this._esc(reg.unit || '-')}</td>
                <td>${this._esc(reg.category)}${reg.subtype ? '/' + this._esc(reg.subtype) : ''}</td>
                <td class="value">${currentValue ? currentValue.value?.toFixed(2) : '-'}</td>
                <td class="actions-cell">${actions}</td>
            `;

            // Attach event listeners (Query is absent for HTTP sources)
            const qb = tr.querySelector('.query');
            if (qb) qb.addEventListener('click', () => this.queryRegisterNow(reg));

            if (isConfigured) {
                tr.querySelector('.edit').addEventListener('click', () => this.editRegister(configuredReg));
                tr.querySelector('.remove').addEventListener('click', () => this.removeRegisterFromTable(reg.address));
            } else {
                tr.querySelector('.add').addEventListener('click', () => this.openAddModal(reg));
                tr.querySelector('.quick-add').addEventListener('click', () => this.quickAddRegister(reg));
            }

            tbody.appendChild(tr);
        });

        // Render pagination
        this.renderPagination(filtered.length);
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
        document.querySelectorAll('#registersTableBody tr').forEach(tr => {
            const address = parseInt(tr.dataset.address);
            if (address) {
                const value = this.currentValues[address];
                const valueCell = tr.querySelector('.value');
                if (valueCell && value) {
                    valueCell.textContent = value.value?.toFixed(2) || '-';
                }
            }
        });
    },

    // ============ Register Actions ============

    async queryRegisterNow(reg) {
        try {
            const response = await fetch('/api/query/register', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    address: reg.address,
                    data_type: reg.data_type || 'float'
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

        // 3. Generate MQTT topic: category/cleaned_description
        const topicBase = cat.replace(/\s+/g, '_').toLowerCase();
        const topicName = desc
            .toLowerCase()
            .replace(/[,;]/g, '')
            .replace(/\s+/g, '_')
            .replace(/[^a-z0-9_]/g, '')
            .replace(/_+/g, '_')
            .replace(/^_|_$/g, '');
        const mqttTopic = `${topicBase}/${topicName}`;

        // 4. Generate InfluxDB measurement from category
        const measurement = cat.replace(/\s+/g, '_').toLowerCase();

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
        this.renderRegistersTable();
    },

    removeRegisterFromTable(address) {
        const reg = this.selectedRegisters.find(r => r.address === address);
        if (reg) {
            this.selectedRegisters = this.selectedRegisters.filter(r => r.address !== address);
            this.saveSelectedRegistersQuiet();
            this.showToast('info', this.t('toast.removed', 'Removed'), `${reg.name} ${this.t('toast.fromConfig', 'removed from configuration')}`);
            this.renderRegistersTable();
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
    _regDeviceIsHttp() {
        const id = this._regDevice || this._primaryDeviceId();
        const dev = (this._devices || []).find(d => d.id === id);
        return dev?.protocol === 'http';
    },

    // MQTT-input measurements key on a subscribe topic + json_path into the payload.
    _regDeviceIsMqtt() {
        const id = this._regDevice || this._primaryDeviceId();
        const dev = (this._devices || []).find(d => d.id === id);
        return dev?.protocol === 'mqtt';
    },

    openCustomRegisterModal() {
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
        resultDiv.innerHTML = '<div class="loading"><i class="bi bi-arrow-repeat spin"></i> Querying…</div>';

        // Look up register info from our database
        const allRegs = this.flattenRegisters();
        const regInfo = allRegs.find(r => r.address === address);
        const isConfigured = this.selectedRegisters.some(r => r.address === address);

        try {
            const response = await fetch('/api/query/register', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ address, data_type: dataType })
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
                            ? '<span class="badge configured"><i class="bi bi-check-circle"></i> Monitored</span>'
                            : `<button class="btn btn-primary btn-sm" id="queryConfigureBtn">
                                <i class="bi bi-plus-circle"></i> Add to Monitoring
                               </button>
                               <button class="btn btn-ghost btn-sm" id="queryQuickAddBtn">
                                <i class="bi bi-lightning"></i> Quick Add
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
                            <i class="bi bi-info-circle"></i> This address is not in the known measurements database.
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
            resultDiv.innerHTML = `<div class="result-error"><i class="bi bi-exclamation-triangle"></i> Error: ${error.message}</div>`;
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

        // Filter registers by tab (category) and search
        let filtered = this.selectedRegisters.filter(reg => {
            // Tab filter (by category)
            if (this.configTab !== 'all' && reg._category !== this.configTab) {
                return false;
            }
            // Search filter
            if (this.configSearch) {
                const searchStr = `${reg.address} ${reg.label} ${reg.name} ${reg.description || ''} ${reg.unit || ''} ${reg.poll_group}`.toLowerCase();
                if (!searchStr.includes(this.configSearch)) {
                    return false;
                }
            }
            return true;
        });

        // Update register count
        const countEl = document.getElementById('registerCount');
        if (countEl) {
            const total = this.selectedRegisters.length;
            const shown = filtered.length;
            countEl.textContent = shown === total
                ? `${total} measurement${total !== 1 ? 's' : ''}`
                : `${shown} of ${total} measurements`;
        }

        if (filtered.length === 0) {
            container.innerHTML = `<div class="empty-state">${this.selectedRegisters.length === 0
                ? 'No measurements selected.'
                : 'No measurements match the current filter.'}</div>`;
            return;
        }

        // HTTP sources key on json_path; the numeric address is an internal key,
        // so surface the json_path column instead — clearer for the user.
        const http = this._regDeviceIsHttp();

        // Create compact table
        container.innerHTML = `
            <table class="selected-registers-table">
                <thead>
                    <tr>
                        <th>${http ? 'JSON path' : 'Addr'}</th>
                        <th>${this.t('lbl.label', "Label")}</th>
                        <th>${this.t('lbl.unit', "Unit")}</th>
                        <th>${this.t('lbl.poll', "Poll")}</th>
                        <th class="center">MQTT</th>
                        <th class="center">${this.t('lbl.influx', "Influx")}</th>
                        <th>${this.t('lbl.actions', "Actions")}</th>
                    </tr>
                </thead>
                <tbody id="selectedRegistersBody"></tbody>
            </table>
        `;

        const tbody = document.getElementById('selectedRegistersBody');

        filtered.forEach(reg => {
            const tr = document.createElement('tr');

            // MQTT tooltip
            const mqttTooltip = reg.mqtt_enabled && reg.mqtt_topic
                ? `Topic: ${reg.mqtt_topic}`
                : (reg.mqtt_enabled ? 'Enabled' : 'Disabled');

            // InfluxDB tooltip
            let influxTooltip = 'Disabled';
            if (reg.influxdb_enabled) {
                influxTooltip = reg.influxdb_measurement || 'Enabled';
                if (reg.influxdb_tags && Object.keys(reg.influxdb_tags).length > 0) {
                    influxTooltip += ` [${Object.entries(reg.influxdb_tags).map(([k,v]) => `${k}=${v}`).join(', ')}]`;
                }
            }

            tr.innerHTML = `
                <td class="addr-cell"${http ? ` title="${this._esc(reg.json_path || '')}"` : ''}>${http ? this._esc(reg.json_path || '—') : reg.address}</td>
                <td class="label-cell">
                    <span class="reg-label">${this._esc(reg.label)}</span>
                    <span class="reg-name">${this._esc(reg.name)}</span>
                </td>
                <td class="unit-cell">${this._esc(reg.unit || '-')}</td>
                <td><span class="badge poll-${reg.poll_group}">${this._esc(reg.poll_group)}</span></td>
                <td class="center">
                    <span class="status-icon ${reg.mqtt_enabled ? 'active' : ''}" title="${this._esc(mqttTooltip)}">
                        ${reg.mqtt_enabled ? '&#10003;' : '&#10005;'}
                    </span>
                </td>
                <td class="center">
                    <span class="status-icon ${reg.influxdb_enabled ? 'active' : ''}" title="${this._esc(influxTooltip)}">
                        ${reg.influxdb_enabled ? '&#10003;' : '&#10005;'}
                    </span>
                </td>
                <td class="actions-cell">
                    <button class="btn-action edit" title="Edit">&#9998;</button>
                    <button class="btn-action remove" title="Remove">&#10006;</button>
                </td>
            `;

            tr.querySelector('.edit').addEventListener('click', () => this.editRegister(reg));
            tr.querySelector('.remove').addEventListener('click', () => this.removeRegister(reg.address));

            tbody.appendChild(tr);
        });
    },

    // Delegated-action entry (dashboard table rows): look the register up by
    // address at CLICK time, matching the old inline handler's late binding.
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
        // query-string suffix routing register catalog/selection calls to the
        // currently edited device; empty for device #1 (legacy endpoints).
        const id = this._regDevice;
        return (id && id !== this._primaryDeviceId()) ? `${sep}device=${encodeURIComponent(id)}` : '';
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
        this.switchDeviceRegTab('available');   // land on Available, as requested
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
        const id = this._regDevice || this._primaryDeviceId();
        if (id === this._primaryDeviceId()) {
            this.showToast('info', this.t('write.primaryTitle', 'Read-only'),
                           this.t('write.primaryMsg', 'The primary device is read-only. Writes apply to other devices.'));
            return;
        }
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
        // Review-and-confirm: a write hits real hardware, so never fire on the
        // first click — show exactly what will be written and require a second,
        // deliberate confirmation.
        this._pendingWrite = { id, body };
        const fc = rtype === 'coil' ? 'FC5' : 'FC6/16';
        const leaseTxt = body.lease_ms ? ` · ${this.t('write.lease', 'Auto-revert (s)')}: ${body.lease_ms / 1000}s` : '';
        document.getElementById('writeResult').innerHTML = `
            <div class="settings-card" style="border-left:3px solid #e08e0b;padding:10px 12px;">
              <div style="font-weight:600;margin-bottom:6px;color:var(--warning-text,#c77700);"><i class="bi bi-exclamation-triangle"></i> ${this.t('write.confirmTitle', 'Confirm write to hardware')}</div>
              <div style="font-size:13px;font-variant-numeric:tabular-nums;">
                ${this.t('lbl.name', 'Name')}: <b>${this._esc(id)}</b> · ${this.t('lbl.address', 'Address')}: <b>${address}</b> (${fc}) · ${this.t('lbl.value', 'Value')}: <b>${this._esc(String(body.value))}</b>${leaseTxt}
              </div>
              <div style="margin-top:8px;display:flex;gap:8px;">
                <button class="btn btn-ghost btn-sm" onclick="app._cancelWrite()">${this.t('common.cancel', 'Cancel')}</button>
                <button class="btn btn-primary btn-sm" onclick="app.confirmWrite(this)"><i class="bi bi-check-lg"></i> ${this.t('write.confirm', 'Confirm write')}</button>
              </div>
            </div>`;
    },

    _cancelWrite() {
        this._pendingWrite = null;
        document.getElementById('writeResult').innerHTML = '';
    },

    async confirmWrite(btn) {
        if (!this._pendingWrite) return;
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
            const lease = res.lease_ms ? ` · <i class="bi bi-hourglass-split"></i> ${this.t('write.leases', 'auto-revert')} ${res.lease_ms / 1000}s → ${this._esc(String(res.reverts_to))}` : '';
            box.innerHTML = `<div class="settings-card" style="padding:8px 12px;color:var(--success-text,#1a8f4c);">
                <i class="bi bi-check-circle"></i> ${this.t('write.done', 'Written')} · ${this.t('write.readBack', 'read-back')}: <b>${this._esc(String(res.read_back))}</b>${vflag}${lease}</div>`;
            this.showToast('success', this.t('write.done', 'Written'), `${id} @ ${address} = ${this._esc(String(res.written))}`);
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
                 onclick="app._pickJsonPathLeaf('${this._esc(l.path).replace(/'/g, '&#39;')}')"
                 onkeydown="if(event.key==='Enter')this.click()">
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
