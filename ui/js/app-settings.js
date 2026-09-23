// settings domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    updateConfigTabs() {
        const container = document.getElementById('configTabs');
        if (!container) return;

        // Get categories from influxdb_measurement or derive from label/unit
        const categories = new Map();
        categories.set('all', 0);

        this.selectedRegisters.forEach(reg => {
            categories.set('all', categories.get('all') + 1);

            // the server names what a measurement IS (canonical name → category);
            // older selections without it fall back to the old derivation
            let cat = reg.category || '';
            if (!cat) cat = (reg.influxdb_measurement || '').toLowerCase();
            if (!cat) {
                // Fallback: derive from unit
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

            // Store category on register for filtering
            reg._category = cat;
            categories.set(cat, (categories.get(cat) || 0) + 1);
        });

        // Sort categories: all first, then alphabetically
        const sortedCats = ['all', ...Array.from(categories.keys()).filter(k => k !== 'all').sort()];

        // Generate tabs HTML
        container.innerHTML = sortedCats
            .filter(cat => categories.get(cat) > 0)
            .map(cat => {
                const count = categories.get(cat);
                const label = cat === 'all' ? this.t('common.all', 'All') : (this._regCatLabel ? this._regCatLabel(cat) : cat);
                const isActive = this.configTab === cat ? 'active' : '';
                return `<button class="config-tab ${isActive}" data-tab="${this._esc(cat)}">${this._esc(label)} <span class="count">(${count})</span></button>`;
            })
            .join('');

        // Rebind click events
        container.querySelectorAll('.config-tab').forEach(btn => {
            btn.addEventListener('click', () => {
                container.querySelectorAll('.config-tab').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.configTab = btn.dataset.tab;
                this.renderSelectedRegistersList();
            });
        });
    },

    // ============ Settings Config (Modbus, MQTT, InfluxDB) ============

    async loadSettingsConfig() {
        try {
            // Load all configs in parallel
            const [modbusRes, mqttRes, influxRes, envRes] = await Promise.all([
                fetch('/api/config/modbus'),
                fetch('/api/config/mqtt'),
                fetch('/api/config/influxdb'),
                fetch('/api/config/env-overrides')
            ]);

            const modbus = await modbusRes.json();
            const mqtt = await mqttRes.json();
            const influx = await influxRes.json();
            const envOverrides = await envRes.json();

            // Store original values for change detection
            this.originalConfig = { modbus, mqtt, influx };
            this.envOverrides = envOverrides;

            // Guarded setters — the Modbus card and some HA fields are no longer
            // rendered globally (they moved per-device), so tolerate missing nodes.
            const setVal = (id, val) => { const e = document.getElementById(id); if (e) e.value = val; };
            const setChk = (id, val) => { const e = document.getElementById(id); if (e) e.checked = val; };
            const show = (id, on) => { const e = document.getElementById(id); if (e) e.style.display = on ? '' : 'none'; };

            // Modbus fields (only present in the device detail view; primary's
            // connection is edited there — kept guarded for any legacy markup).
            setVal('cfgModbusHost', modbus.host || '');
            setVal('cfgModbusPort', modbus.port || 502);
            setVal('cfgModbusUnitId', modbus.unit_id || 1);
            setVal('cfgModbusTimeout', modbus.timeout || 3);
            setVal('cfgModbusRetryAttempts', modbus.retry_attempts || 3);
            setVal('cfgModbusRetryDelay', modbus.retry_delay || 1.0);

            // MQTT fields (global broker connection)
            setChk('cfgMqttEnabled', mqtt.enabled);
            setVal('cfgMqttBroker', mqtt.broker || '');
            setVal('cfgMqttPort', mqtt.port || 1883);
            setVal('cfgMqttUsername', mqtt.username || '');
            setVal('cfgMqttDefaultTopic', mqtt.default_topic_pattern || 'meters/{device}');
            setVal('cfgMqttPublishMode', mqtt.publish_mode || 'changed');
            setVal('cfgMqttQos', mqtt.qos || 0);
            setChk('cfgMqttRetain', mqtt.retain !== false);
            // HA discovery: per-device enable now lives in the device detail; only
            // the broker-wide discovery prefix stays global here.
            setChk('cfgMqttHaEnabled', mqtt.ha_discovery_enabled !== false);
            setVal('cfgMqttHaPrefix', mqtt.ha_discovery_prefix || 'homeassistant');
            setVal('cfgMqttHaDeviceName', mqtt.ha_device_name || '');
            // MQTT TLS
            setChk('cfgMqttTls', !!mqtt.tls_enabled);
            setVal('cfgMqttTlsCa', mqtt.tls_ca_cert || '');
            setVal('cfgMqttTlsCert', mqtt.tls_client_cert || '');
            setVal('cfgMqttTlsKey', mqtt.tls_client_key || '');
            setChk('cfgMqttTlsInsecure', !!mqtt.tls_insecure);
            show('mqttTlsFields', !!mqtt.tls_enabled);

            // InfluxDB fields (global connection + default bucket pattern)
            setChk('cfgInfluxEnabled', influx.enabled);
            setVal('cfgInfluxUrl', influx.url || '');
            setVal('cfgInfluxOrg', influx.org || '');
            setVal('cfgInfluxBucket', influx.bucket || '');
            setVal('cfgInfluxDefaultBucket', influx.default_bucket_pattern || '{device}');
            setVal('cfgInfluxWriteInterval', influx.write_interval || 5);
            setVal('cfgInfluxPublishMode', influx.publish_mode || 'changed');

            // General settings (report timezone) — separate endpoint, tolerant
            // of failure (an old backend without it must not break the page).
            fetch('/api/config/general').then(r => r.ok ? r.json() : null).then(g => {
                if (!g) return;
                setVal('cfgTimezone', g.timezone || 'Europe/Bucharest');
                const dl = document.getElementById('tzList');
                if (dl && !dl.children.length && Array.isArray(g.timezones)) {
                    dl.innerHTML = g.timezones.map(z => `<option value="${this._esc(z)}">`).join('');
                }
                // default widget colors
                const dc = g.default_colors || {};
                this._defaultColors = dc;
                this._applyDefaultColors();
                setVal('cfgPhaseConv', dc.phase_convention || 'distinct');
                const pc = dc.phase_custom || [];
                if (pc.length === 3) { setVal('cfgPhaseC1', pc[0]); setVal('cfgPhaseC2', pc[1]); setVal('cfgPhaseC3', pc[2]); }
                const cats = dc.categories || {};
                setVal('cfgColTemp', cats.temperature || '#f97316');
                setVal('cfgColHum', cats.humidity || '#06b6d4');
                setVal('cfgColPow', cats.power || '#12a3b2');
                setVal('cfgColEn', cats.energy || '#d29922');
                const conv = document.getElementById('cfgPhaseConv');
                const wrap = document.getElementById('cfgPhaseCustomWrap');
                const updWrap = () => { if (wrap) wrap.style.display = conv.value === 'custom' ? '' : 'none'; };
                if (conv && !conv._wired) { conv._wired = true; conv.addEventListener('change', updWrap); }
                updWrap();
            }).catch(() => {});

            // Show ENV override warnings
            this.showEnvOverrides(envOverrides);

            // Update status dots
            this.updateSettingsStatusDots();

            // Toggle settings body visibility based on enabled
            this.toggleSettingsBody('mqtt', mqtt.enabled);
            this.toggleSettingsBody('influx', influx.enabled);

            // Devices list (Tier 2)
            this.renderDevicesList();

            // Security section
            this.loadSecurity();

        } catch (error) {
            console.error('Failed to load settings:', error);
            this.showToast('error', this.t('toast.loadSettingsFailed', 'Failed to load settings'));
        }
    },

    async loadSecurity() {
        try {
            const [sec, ui] = await Promise.all([
                fetch('/api/config/security').then(r => r.json()),
                fetch('/api/config/ui-security').then(r => r.json()),
            ]);
            const v = id => document.getElementById(id);
            v('cfgAllowlist').value = (sec.allowlist || []).join('\n');
            if (v('cfgAllowWrites')) v('cfgAllowWrites').checked = !!sec.allow_writes;
            if (v('cfgAllowNonlanHttp')) v('cfgAllowNonlanHttp').checked = !!sec.allow_nonlan_http_devices;
            const ipEl = v('allowlistYourIp');
            if (ipEl) ipEl.textContent = sec.your_ip
                ? this.t('security.allowlist.yourIp', 'Your current IP:') + ' ' + sec.your_ip : '';
            // HTTPS
            v('cfgUiTls').checked = !!ui.tls_enabled;
            v('cfgUiTlsCert').value = ui.tls_cert || '';
            v('cfgUiTlsKey').value = ui.tls_key || '';
            v('uiTlsFields').style.display = ui.tls_enabled ? '' : 'none';
            // Auth
            v('cfgAuthEnabled').checked = !!ui.auth_enabled;
            v('cfgAuthUser').value = ui.auth_username || '';
            v('cfgViewerUser').value = ui.viewer_username || '';
            v('cfgOperatorUser').value = ui.operator_username || '';
            if (v('cfgCanonicalUrl')) v('cfgCanonicalUrl').value = ui.canonical_url || '';
            v('cfgLockoutN').value = ui.lockout_threshold || 5;
            v('cfgLockoutMin').value = ui.lockout_minutes || 5;
            v('cfgAuthPass').value = '';
            v('cfgViewerPass').value = '';
            v('cfgOperatorPass').value = '';
            v('authFields').style.display = ui.auth_enabled ? '' : 'none';
            if (!v('cfgUiTls')._wired) {
                v('cfgUiTls')._wired = true;
                v('cfgUiTls').addEventListener('change', e =>
                    v('uiTlsFields').style.display = e.target.checked ? '' : 'none');
                v('cfgAuthEnabled').addEventListener('change', e =>
                    v('authFields').style.display = e.target.checked ? '' : 'none');
            }
        } catch (e) { console.error('security load failed:', e); }
    },

    async saveSecurity(btn) {
        const v = id => document.getElementById(id);
        const fb = v('securitySaveFeedback');
        this.setButtonLoading(btn, true);
        try {
            // 1) IP allowlist
            const allowlist = v('cfgAllowlist').value.split('\n').map(s => s.trim()).filter(Boolean);
            let r = await fetch('/api/config/security', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    allowlist,
                    allow_writes: v('cfgAllowWrites') ? v('cfgAllowWrites').checked : undefined,
                    allow_nonlan_http_devices: v('cfgAllowNonlanHttp') ? v('cfgAllowNonlanHttp').checked : undefined,
                }),
            });
            if (!r.ok) throw new Error(((await r.json()).detail?.errors || ['allowlist error']).join('; '));
            // 2) HTTPS + auth (ui-security)
            const body = {
                tls_enabled: v('cfgUiTls').checked,
                tls_cert: v('cfgUiTlsCert').value.trim(),
                tls_key: v('cfgUiTlsKey').value.trim(),
                auth_enabled: v('cfgAuthEnabled').checked,
                auth_username: v('cfgAuthUser').value.trim(),
                viewer_username: v('cfgViewerUser').value.trim(),
                operator_username: v('cfgOperatorUser').value.trim(),
                canonical_url: (v('cfgCanonicalUrl') ? v('cfgCanonicalUrl').value.trim() : ''),
                lockout_threshold: parseInt(v('cfgLockoutN').value) || 5,
                lockout_minutes: parseInt(v('cfgLockoutMin').value) || 5,
            };
            if (v('cfgAuthPass').value) body.auth_password = v('cfgAuthPass').value;
            if (v('cfgViewerPass').value) body.viewer_password = v('cfgViewerPass').value;
            if (v('cfgOperatorPass').value) body.operator_password = v('cfgOperatorPass').value;
            r = await fetch('/api/config/ui-security', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!r.ok) throw new Error(((await r.json()).detail?.errors || ['auth error']).join('; '));
            const res = await r.json();
            if (fb) { fb.textContent = '✓ ' + this.t('common.saved', 'Saved'); fb.className = 'save-feedback ok'; }
            this.showToast('success', this.t('security.saved', 'Security updated'),
                res.restart_needed ? this.t('security.restartHint', 'Restart the container to apply HTTPS.') : '');
            this.loadSecurity();
        } catch (e) {
            if (fb) { fb.textContent = '✗ ' + e.message; fb.className = 'save-feedback err'; }
            this.showToast('error', this.t('security.saveFail', 'Security save failed'), e.message);
        } finally {
            this.setButtonLoading(btn, false, 'Save & Apply');
        }
    },

    async loadAlerts() {
        try {
            const a = await fetch('/api/config/alerts').then(r => r.json());
            const v = id => document.getElementById(id);
            v('cfgAlertsEnabled').checked = !!a.enabled;
            v('cfgAlertMqtt').checked = a.mqtt !== false;
            v('cfgAlertWebhookUrl').value = a.webhook_url || '';
            v('cfgAlertWebhookHeaders').value = (a.webhook_headers && Object.keys(a.webhook_headers).length)
                ? JSON.stringify(a.webhook_headers) : '';
            v('cfgAlertWebhookBody').value = a.webhook_body ? JSON.stringify(a.webhook_body) : '';
            v('cfgAlertMinInterval').value = a.min_interval_s ?? 300;
            v('cfgAlertLatency').value = a.latency_ms ?? 1000;
            v('cfgAlertBuffer').value = a.buffer_points ?? 1000;
            const sig = a.signals || {};
            v('cfgAlertSigDevice').checked = sig.device !== false;
            v('cfgAlertSigSink').checked = sig.sink !== false;
            v('cfgAlertSigLatency').checked = sig.latency !== false;
            v('cfgAlertSigBuffer').checked = sig.buffer !== false;
            const out = document.getElementById('alertTestResult'); if (out) out.innerHTML = '';
        } catch (e) { console.error('alerts load failed:', e); }
    },

    async saveAlerts(btn) {
        const v = id => document.getElementById(id);
        const fb = v('alertsSaveFeedback');
        this.setButtonLoading(btn, true);
        try {
            let headers = {}, body = null;
            const hRaw = v('cfgAlertWebhookHeaders').value.trim();
            const bRaw = v('cfgAlertWebhookBody').value.trim();
            if (hRaw) { try { headers = JSON.parse(hRaw); } catch (_) { throw new Error(this.t('alerts.badHeaders', 'Webhook headers must be valid JSON')); } }
            if (bRaw) { try { body = JSON.parse(bRaw); } catch (_) { throw new Error(this.t('alerts.badBody', 'Webhook body must be valid JSON')); } }
            const payload = {
                enabled: v('cfgAlertsEnabled').checked,
                mqtt: v('cfgAlertMqtt').checked,
                webhook_url: v('cfgAlertWebhookUrl').value.trim(),
                webhook_headers: headers,
                webhook_body: body,
                min_interval_s: parseFloat(v('cfgAlertMinInterval').value) || 300,
                latency_ms: parseFloat(v('cfgAlertLatency').value) || 1000,
                buffer_points: parseInt(v('cfgAlertBuffer').value) || 1000,
                signals: {
                    device: v('cfgAlertSigDevice').checked, sink: v('cfgAlertSigSink').checked,
                    latency: v('cfgAlertSigLatency').checked, buffer: v('cfgAlertSigBuffer').checked,
                },
            };
            const r = await fetch('/api/config/alerts', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
            });
            if (!r.ok) throw new Error(((await r.json()).detail?.errors || ['save failed']).join('; '));
            const res = await r.json();
            if (fb) { fb.textContent = '✓ ' + this.t('common.saved', 'Saved'); fb.className = 'save-feedback ok'; }
            this.showToast('success', this.t('alerts.saved', 'Alerts updated'),
                           (res.channels || []).join(', ') || this.t('status.noChannel', 'no channel'));
            this.loadAlerts();
        } catch (e) {
            if (fb) { fb.textContent = '✗ ' + e.message; fb.className = 'save-feedback err'; }
            this.showToast('error', this.t('toast.saveFailed', 'Save Failed'), e.message);
        } finally { this.setButtonLoading(btn, false, 'Save & Apply'); }
    },

    showEnvOverrides(overrides) {
        // Map config paths to field IDs
        const mapping = {
            'modbus.host': 'envModbusHost',
            'mqtt.broker': 'envMqttBroker',
            'influxdb.url': 'envInfluxUrl'
        };

        Object.keys(mapping).forEach(path => {
            const el = document.getElementById(mapping[path]);
            if (el) {
                if (overrides[path]) {
                    el.textContent = `ENV: ${path.toUpperCase().replace('.', '_')}=${overrides[path]}`;
                    el.classList.add('visible');
                } else {
                    el.classList.remove('visible');
                }
            }
        });
    },

    updateSettingsStatusDots() {
        const modbusDot = document.getElementById('modbusStatusDot');
        if (modbusDot && this.status) {
            const connected = this.status.modbus?.connected;
            modbusDot.className = `status-dot ${connected ? 'connected' : 'disconnected'}`;
        }
    },

    toggleSettingsBody(service, enabled) {
        const body = document.getElementById(`${service}SettingsBody`);
        if (body) {
            if (enabled) {
                body.classList.remove('disabled');
            } else {
                body.classList.add('disabled');
            }
        }
    },

    setupConfigMainTabs() {
        document.querySelectorAll('#configSubtabs .config-main-tab').forEach(tab => {
            tab.addEventListener('click', () => this.switchConfigTab(tab.dataset.cfgtab));
        });
    },

    // Show one Config sub-tab panel (mqtt|influxdb|backup|security).
    switchConfigTab(name) {
        if (!name) name = 'mqtt';
        document.querySelectorAll('#configSubtabs .config-main-tab').forEach(t =>
            t.classList.toggle('active', t.dataset.cfgtab === name));
        document.querySelectorAll('#configPage .cfg-panel').forEach(p =>
            p.style.display = p.dataset.cfgpanel === name ? '' : 'none');
        if (name === 'alerts') this.loadAlerts();
        if (name === 'backup') this.loadSnapshots();
        if (name === 'security') { this.loadAudit(); this.loadPasskeys(); }
    },

    setupSettingsListeners() {
        // The enable toggles only show/hide the section body. Nothing is persisted
        // until the section's explicit "Save & Apply" button is pressed — a single,
        // clear save path (no silent autosave, no separate apply banner).
        document.getElementById('cfgMqttEnabled')?.addEventListener('change', (e) => {
            this.toggleSettingsBody('mqtt', e.target.checked);
        });
        document.getElementById('cfgInfluxEnabled')?.addEventListener('change', (e) => {
            this.toggleSettingsBody('influx', e.target.checked);
        });
        document.getElementById('cfgMqttTls')?.addEventListener('change', (e) => {
            const f = document.getElementById('mqttTlsFields');
            if (f) f.style.display = e.target.checked ? '' : 'none';
        });
    },

    // Build [endpoint, body] for one settings section from its form fields.
    _gatherSettings(section) {
        const v = id => document.getElementById(id);
        if (section === 'modbus') return ['/api/config/modbus', {
            host: v('cfgModbusHost').value,
            port: parseInt(v('cfgModbusPort').value) || 502,
            unit_id: parseInt(v('cfgModbusUnitId').value) || 1,
            timeout: parseInt(v('cfgModbusTimeout').value) || 3,
            retry_attempts: parseInt(v('cfgModbusRetryAttempts').value) || 3,
            retry_delay: parseFloat(v('cfgModbusRetryDelay').value) || 1.0,
        }];
        const val = (id, d = '') => { const e = v(id); return e ? e.value : d; };
        const chk = (id, d = false) => { const e = v(id); return e ? e.checked : d; };
        if (section === 'mqtt') return ['/api/config/mqtt', {
            enabled: chk('cfgMqttEnabled'), broker: val('cfgMqttBroker'),
            port: parseInt(val('cfgMqttPort')) || 1883, username: val('cfgMqttUsername'),
            password: val('cfgMqttPassword') || undefined,
            default_topic_pattern: val('cfgMqttDefaultTopic') || 'meters/{device}',
            publish_mode: val('cfgMqttPublishMode'), qos: parseInt(val('cfgMqttQos')) || 0,
            retain: chk('cfgMqttRetain', true), ha_discovery_enabled: chk('cfgMqttHaEnabled', true),
            ha_discovery_prefix: val('cfgMqttHaPrefix'), ha_device_name: val('cfgMqttHaDeviceName'),
            tls_enabled: chk('cfgMqttTls'), tls_ca_cert: val('cfgMqttTlsCa'),
            tls_client_cert: val('cfgMqttTlsCert'), tls_client_key: val('cfgMqttTlsKey'),
            tls_insecure: chk('cfgMqttTlsInsecure'),
        }];
        return ['/api/config/influxdb', {
            enabled: chk('cfgInfluxEnabled'), url: val('cfgInfluxUrl'),
            token: val('cfgInfluxToken') || undefined, org: val('cfgInfluxOrg'),
            bucket: val('cfgInfluxBucket'),
            default_bucket_pattern: val('cfgInfluxDefaultBucket') || '{device}',
            write_interval: parseInt(val('cfgInfluxWriteInterval')) || 5,
            publish_mode: val('cfgInfluxPublishMode'),
        }];
    },

    // Explicit per-section Save & Apply: persist that section to config.yaml then
    // reconnect the service, with inline feedback (clearer than the silent autosave).
    async saveAndApply(section, btn) {
        const [url, body] = this._gatherSettings(section);
        const fb = document.getElementById(section + 'SaveFeedback');
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<i aria-hidden="true" class="bi bi-arrow-repeat spin"></i>'; }
        if (fb) { fb.textContent = ''; fb.className = 'save-feedback'; }
        try {
            const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            await fetch('/api/config/apply', { method: 'POST' });
            if (fb) { fb.textContent = '✓ ' + this.t('settings.saved', 'Saved & applied'); fb.className = 'save-feedback ok'; setTimeout(() => { if (fb.classList.contains('ok')) fb.textContent = ''; }, 4000); }
            // Refresh connection state after the reconnect.
            await this.loadStatus();
            this.updateSettingsStatusDots();
        } catch (e) {
            if (fb) { fb.textContent = this.t('settings.saveFailed', 'Save failed'); fb.className = 'save-feedback err'; }
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = orig; }
        }
    },

    // General settings (report timezone): validated server-side against the
    // IANA database; applies live (the Energy report reads it per request).
    async saveGeneralConfig(btn) {
        const fb = document.getElementById('generalSaveFeedback');
        const orig = btn ? btn.innerHTML : '';
        if (btn) { btn.disabled = true; btn.innerHTML = '<i aria-hidden="true" class="bi bi-arrow-repeat spin"></i>'; }
        if (fb) { fb.textContent = ''; fb.className = 'save-feedback'; }
        try {
            const tz = (document.getElementById('cfgTimezone')?.value || '').trim();
            const val = id => document.getElementById(id)?.value;
            const default_colors = {
                phase_convention: val('cfgPhaseConv') || 'distinct',
                phase_custom: [val('cfgPhaseC1'), val('cfgPhaseC2'), val('cfgPhaseC3')].filter(Boolean),
                categories: { temperature: val('cfgColTemp'), humidity: val('cfgColHum'),
                              power: val('cfgColPow'), energy: val('cfgColEn') },
            };
            const r = await fetch('/api/config/general', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ timezone: tz, default_colors }) });
            if (!r.ok) {
                const d = await r.json().catch(() => ({}));
                throw new Error((d.detail?.errors || [])[0] || ('HTTP ' + r.status));
            }
            const saved = await r.json().catch(() => ({}));
            this._defaultColors = saved.default_colors || this._defaultColors;
            this._applyDefaultColors();               // convention applies live
            if (fb) { fb.textContent = '✓ ' + this.t('settings.saved', 'Saved & applied'); fb.className = 'save-feedback ok'; setTimeout(() => { if (fb.classList.contains('ok')) fb.textContent = ''; }, 4000); }
        } catch (e) {
            if (fb) { fb.textContent = e.message || this.t('settings.saveFailed', 'Save failed'); fb.className = 'save-feedback err'; }
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = orig; }
        }
    },

    // ============ Raw Config Modal ============

    openRawConfigModal() {
        const modal = document.getElementById('rawConfigModal');
        const editor = document.getElementById('rawConfigEditor');

        // Prepare config data for display
        const configData = this.selectedRegisters.map(reg => ({
            address: reg.address,
            name: reg.name,
            label: reg.label,
            unit: reg.unit,
            description: reg.description,
            data_type: reg.data_type,
            poll_group: reg.poll_group,
            mqtt_enabled: reg.mqtt_enabled,
            mqtt_topic: reg.mqtt_topic,
            influxdb_enabled: reg.influxdb_enabled,
            influxdb_measurement: reg.influxdb_measurement,
            influxdb_tags: reg.influxdb_tags,
            ui_show_on_dashboard: reg.ui_show_on_dashboard,
            ui_widget: reg.ui_widget,
            ui_config: reg.ui_config
        }));

        editor.value = JSON.stringify(configData, null, 2);
        this.openModal('rawConfigModal');
        this.validateRawConfig();
    },

    closeRawConfigModal() {
        this.closeModal('rawConfigModal');
    },

    formatRawConfig() {
        const editor = document.getElementById('rawConfigEditor');
        try {
            const parsed = JSON.parse(editor.value);
            editor.value = JSON.stringify(parsed, null, 2);
            this.validateRawConfig();
        } catch (e) {
            // Can't format invalid JSON
        }
    },

    validateRawConfig() {
        const editor = document.getElementById('rawConfigEditor');
        const statusEl = document.getElementById('editorStatus');

        try {
            const parsed = JSON.parse(editor.value);

            if (!Array.isArray(parsed)) {
                throw new Error('Configuration must be an array');
            }

            // Validate each register
            for (let i = 0; i < parsed.length; i++) {
                const reg = parsed[i];
                if (!reg.address || typeof reg.address !== 'number') {
                    throw new Error(`Register ${i + 1}: missing or invalid 'address'`);
                }
                if (!reg.name || typeof reg.name !== 'string') {
                    throw new Error(`Register ${i + 1}: missing or invalid 'name'`);
                }
            }

            statusEl.textContent = `Valid JSON - ${parsed.length} measurements`;
            statusEl.className = 'editor-status valid';
            return true;

        } catch (e) {
            statusEl.textContent = `Error: ${e.message}`;
            statusEl.className = 'editor-status invalid';
            return false;
        }
    },

    async saveRawConfig() {
        if (!this.validateRawConfig()) {
            this.showToast('error', this.t('toast.invalidJson', 'Invalid JSON'), this.t('toast.fixErrors', 'Please fix the errors before saving.'));
            return;
        }

        const btn = document.getElementById('rawConfigSave');
        this.setButtonLoading(btn, true);

        try {
            const parsed = JSON.parse(document.getElementById('rawConfigEditor').value);

            const response = await fetch('/api/registers/selected' + this._regDeviceQS(), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(parsed)
            });

            if (!response.ok) {
                throw new Error('Save failed');
            }

            this.closeRawConfigModal();
            await this.loadSelectedRegisters();
            this.updateConfigTabs();
            this.renderSelectedRegistersList();
            this.showToast('success', this.t('toast.configSaved', 'Configuration Saved'), this.t('toast.changesApplied', 'Changes applied successfully.'));

        } catch (error) {
            this.showToast('error', this.t('toast.saveFailed', 'Save Failed'), error.message);
        } finally {
            this.setButtonLoading(btn, false, 'Save & Apply');
        }
    },

    // ── Backup / Restore ────────────────────────────────────────────────────
    exportConfig() {
        const secrets = document.getElementById('backupSecrets')?.checked ? 'true' : 'false';
        const a = document.createElement('a');
        a.href = `/api/config/export?include_secrets=${secrets}`;
        a.download = 'modbus-gateway-config-backup.zip';
        a.click();
        this.showToast('info', this.t('backup.exported', 'Backup downloaded'),
                       this.t('backup.exportedMsg', 'Keep it somewhere safe.'));
    },

    importConfig() {
        const input = document.getElementById('backupFileInput');
        input.onchange = async () => {
            const file = input.files[0];
            input.value = '';
            if (!file) return;
            if (!confirm(this.t('backup.importConfirm',
                'Restore this backup?\n\nYes: overwrites the current devices, measurement selections, templates and virtual meters, then reloads.\nNo: cancel.'))) return;
            const fb = document.getElementById('backupFeedback');
            if (fb) { fb.textContent = this.t('backup.restoring', 'Restoring…'); fb.className = 'save-feedback'; }
            try {
                const r = await fetch('/api/config/import', {
                    method: 'POST', headers: { 'Content-Type': 'application/zip' },
                    body: await file.arrayBuffer(),
                });
                const res = await r.json();
                if (!r.ok) throw new Error((res.detail?.errors || ['import failed']).join('; '));
                if (fb) { fb.textContent = '✓ ' + this.t('backup.restored', 'Restored'); fb.className = 'save-feedback ok'; }
                this.showToast('success', this.t('backup.restored', 'Backup restored'),
                    `${res.device_registers} device selections · ${res.templates} templates` +
                    (res.note && res.note.includes('restart') ? ' · ' + this.t('backup.restartHint', 'restart recommended for new devices') : ''));
                await this.loadSettingsConfig();
                await this.loadSelectedRegisters();
            } catch (e) {
                if (fb) { fb.textContent = '✗ ' + e.message; fb.className = 'save-feedback err'; }
                this.showToast('error', this.t('backup.importFail', 'Restore failed'), e.message);
            }
        };
        input.click();
    }
});

// ── Config snapshots & rollback ──
Object.assign(JanitzaMonitor.prototype, {

    async loadSnapshots() {
        const body = document.getElementById('snapTableBody');
        if (!body) return;
        try {
            const r = await fetch('/api/config/snapshots');
            const data = await r.json();
            const rows = data.snapshots || [];
            if (!rows.length) {
                body.innerHTML = `<tr><td colspan="5" class="diag-empty">${this._esc(
                    this.t('snap.empty', 'No snapshots yet — one is taken automatically after the first config change.'))}</td></tr>`;
                return;
            }
            body.innerHTML = rows.map(s => {
                const when = new Date(s.ts * 1000).toLocaleString();
                const kb = s.size ? (s.size / 1024).toFixed(1) + ' KB' : '—';
                const lkg = s.lkg ? ` <span class="badge badge-success">LKG</span>` : '';
                const trig = { manual: this.t('snap.trigManual', 'manual'),
                               baseline: this.t('snap.trigBaseline', 'baseline'),
                               'pre-import': this.t('snap.trigPreImport', 'before import'),
                               'pre-restore': this.t('snap.trigPreRestore', 'before rollback'),
                               'healthy-boot': this.t('snap.trigLkg', 'healthy boot') }[s.trigger] || s.trigger;
                return `<tr>
                    <td style="white-space:nowrap;">${this._esc(when)}${lkg}</td>
                    <td class="mono" style="font-size:11.5px;">${this._esc(trig)}</td>
                    <td>${this._esc(s.note || '')}</td>
                    <td class="mono">${kb}</td>
                    <td style="white-space:nowrap;text-align:right;">
                        <button class="btn btn-ghost btn-sm" ${this._act('diffSnapshot', [s.id])}
                                title="${this._esc(this.t('snap.diff', 'What changed since'))}"><i aria-hidden="true" class="bi bi-file-diff"></i></button>
                        <button class="btn btn-ghost btn-sm" ${this._act('restoreSnapshot', [s.id])}
                                title="${this._esc(this.t('snap.restore', 'Restore'))}"><i aria-hidden="true" class="bi bi-arrow-counterclockwise"></i></button>
                        <button class="btn btn-ghost btn-sm" ${this._act('_navigate', [`/api/config/snapshots/${encodeURIComponent(s.id)}/download`])}
                                title="${this._esc(this.t('snap.download', 'Download'))}"><i aria-hidden="true" class="bi bi-download"></i></button>
                        ${s.lkg ? '' : `<button class="btn btn-ghost btn-sm" ${this._act('deleteSnapshot', [s.id])}
                                title="${this._esc(this.t('common.delete', 'Delete'))}"><i aria-hidden="true" class="bi bi-trash"></i></button>`}
                    </td></tr>`;
            }).join('');
        } catch (e) {
            body.innerHTML = `<tr><td colspan="5" class="err">${this._esc(String(e.message || e))}</td></tr>`;
        }
    },

    async createSnapshot() {
        const note = prompt(this.t('snap.notePrompt', 'Optional note for this snapshot:'), '') ?? null;
        if (note === null) return;
        try {
            const r = await fetch('/api/config/snapshots', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ note }),
            });
            if (!r.ok) throw new Error(await r.text());
            this.showToast('success', this.t('snap.title', 'Snapshots & Rollback'),
                           this.t('snap.created', 'Snapshot saved.'));
            this.loadSnapshots();
        } catch (e) {
            this.showToast('error', this.t('snap.title', 'Snapshots & Rollback'), String(e.message || e));
        }
    },

    async restoreSnapshot(id) {
        if (!confirm(this.t('snap.restoreConfirm',
            'Roll the configuration back to this snapshot?\n\nDevices, measurements, templates, virtual meters and settings all revert to that moment. The current state is snapshotted first, so you can roll forward again.'))) return;
        const fb = document.getElementById('snapFeedback');
        if (fb) fb.textContent = this.t('snap.restoring', 'Restoring…');
        try {
            const r = await fetch(`/api/config/snapshots/${encodeURIComponent(id)}/restore`, { method: 'POST' });
            const data = await r.json();
            if (!r.ok) throw new Error((data.detail?.errors || [data.detail || r.statusText]).join('; '));
            if (fb) fb.textContent = '';
            this.showToast('success', this.t('snap.title', 'Snapshots & Rollback'),
                           this.t('snap.restored', 'Configuration rolled back. Reloading…'));
            setTimeout(() => window.location.reload(), 1200);
        } catch (e) {
            if (fb) fb.textContent = '';
            this.showToast('error', this.t('snap.title', 'Snapshots & Rollback'), String(e.message || e));
        }
    },

    async deleteSnapshot(id) {
        if (!confirm(this.t('snap.deleteConfirm', 'Delete this snapshot?'))) return;
        try {
            const r = await fetch(`/api/config/snapshots/${encodeURIComponent(id)}`, { method: 'DELETE' });
            if (!r.ok) throw new Error(await r.text());
            this.loadSnapshots();
        } catch (e) {
            this.showToast('error', this.t('snap.title', 'Snapshots & Rollback'), String(e.message || e));
        }
    },
});

// ── Audit trail ──
Object.assign(JanitzaMonitor.prototype, {

    _auditSearchInput() {
        clearTimeout(this._auditT);
        this._auditT = setTimeout(() => this.loadAudit(), 300);
    },

    async loadAudit() {
        const body = document.getElementById('auditTableBody');
        if (!body) return;
        const q = document.getElementById('auditFilter')?.value?.trim() || '';
        try {
            const r = await fetch(`/api/audit?limit=200${q ? '&q=' + encodeURIComponent(q) : ''}`);
            if (r.status === 403) {
                body.innerHTML = `<tr><td colspan="6" class="diag-empty">${this._esc(
                    this.t('audit.adminOnly', 'The audit trail requires the admin role.'))}</td></tr>`;
                return;
            }
            const entries = (await r.json()).entries || [];
            if (!entries.length) {
                body.innerHTML = `<tr><td colspan="6" class="diag-empty">${this._esc(
                    this.t('audit.empty', 'Nothing recorded yet — config changes, logins and device writes will appear here.'))}</td></tr>`;
                return;
            }
            const stColor = s => s === 'ok' ? 'badge-success'
                : s.startsWith('denied') || s.includes('invalid') || s.includes('locked') ? 'badge-danger'
                : 'badge-warning';
            body.innerHTML = entries.map(e => `<tr>
                <td style="white-space:nowrap;">${this._esc(new Date(e.ts * 1000).toLocaleString())}</td>
                <td>${this._esc(e.user || '-')}</td>
                <td class="mono" style="font-size:11.5px;">${this._esc(e.ip || '-')}</td>
                <td class="mono" style="font-size:11.5px;">${this._esc(e.action)}${e.target ? ' · ' + this._esc(e.target) : ''}</td>
                <td><span class="badge ${stColor(e.status)}">${this._esc(e.status)}</span></td>
                <td class="mono" style="font-size:11px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
                    title="${this._esc(e.detail || '')}">${this._esc((e.detail || '').slice(0, 160))}</td>
            </tr>`).join('');
        } catch (e) {
            body.innerHTML = `<tr><td colspan="6" class="err">${this._esc(String(e.message || e))}</td></tr>`;
        }
    },
});

// ── snapshot diff (what changed since) ──
Object.assign(JanitzaMonitor.prototype, {

    async diffSnapshot(id) {
        let host = document.getElementById('snapDiffModal');
        if (!host) {
            host = document.createElement('div');
            host.id = 'snapDiffModal';
            host.className = 'modal';
            host.innerHTML = `
              <div class="modal-content" style="max-width:820px;">
                <div class="modal-header">
                  <h3><i class="bi bi-file-diff" aria-hidden="true"></i> <span id="snapDiffTitle"></span></h3>
                  <button class="modal-close" ${this._act('closeModal', ["snapDiffModal"])} aria-label="Close">&times;</button>
                </div>
                <div class="modal-body" id="snapDiffBody" style="max-height:65vh;overflow-y:auto;"></div>
              </div>`;
            document.body.appendChild(host);
        }
        document.getElementById('snapDiffTitle').textContent =
            this.t('snap.diffTitle', 'Changes since this snapshot');
        const body = document.getElementById('snapDiffBody');
        body.innerHTML = `<div class="field-hint">${this._esc(this.t('common.loading', 'Loading…'))}</div>`;
        // through openModal, not a bare display:flex — it carries the dialog
        // semantics, focus trap, Escape and focus restore (a11y audit: this
        // was the one modal bypassing the shared machinery)
        this.openModal('snapDiffModal');
        try {
            const r = await fetch(`/api/config/snapshots/${encodeURIComponent(id)}/diff`);
            const d = await r.json();
            if (!r.ok) throw new Error(d.detail || r.statusText);
            body.innerHTML = this._snapDiffHtml(d);
        } catch (e) {
            body.innerHTML = `<div class="err">${this._esc(String(e.message || e))}</div>`;
        }
    },

    _snapDiffHtml(d) {
        if (!d.totals.files_changed) {
            return `<div class="field-hint" style="padding:12px;">${this._esc(
                this.t('snap.diffNone', 'Nothing changed — the current configuration is identical to this snapshot.'))}</div>`;
        }
        const KIND = { added: ['badge-success', '+'], removed: ['badge-danger', '−'], changed: ['badge-warning', '~'] };
        const fileBlocks = d.files.filter(f => f.status !== 'unchanged').map(f => {
            if (f.status !== 'changed') {
                const label = f.status === 'added'
                    ? this.t('snap.diffFileAdded', 'file added since the snapshot (a rollback removes it)')
                    : this.t('snap.diffFileRemoved', 'file existed in the snapshot (a rollback brings it back)');
                return `<div class="snap-diff-file"><h4 class="mono">${this._esc(f.name)}</h4>
                        <div class="field-hint">${this._esc(label)}</div></div>`;
            }
            const rows = f.changes.map(c => {
                const [cls, sym] = KIND[c.kind] || ['badge-secondary', '?'];
                const oldv = c.old !== undefined ? `<span class="snap-diff-old">${this._esc(c.old)}</span>` : '';
                const newv = c.new !== undefined ? `<span class="snap-diff-new">${this._esc(c.new)}</span>` : '';
                const arrow = (oldv && newv) ? ' → ' : '';
                return `<tr>
                    <td><span class="badge ${cls}">${sym}</span></td>
                    <td class="mono" style="font-size:11.5px;">${this._esc(c.path)}</td>
                    <td class="mono" style="font-size:11.5px;word-break:break-all;">${oldv}${arrow}${newv}</td>
                </tr>`;
            }).join('');
            return `<div class="snap-diff-file">
                <h4 class="mono">${this._esc(f.name)} <span class="ss-dim">(${f.changes.length})</span></h4>
                ${f.truncated ? `<div class="field-hint">${this._esc(this.t('snap.diffTruncated', 'List truncated.'))}</div>` : ''}
                <table class="data-table"><tbody>${rows}</tbody></table>
            </div>`;
        }).join('');
        return `<p class="field-hint">${this._esc(this.t('snap.diffHint',
            'These changes happened AFTER the snapshot — restoring it would undo them. Secret values are masked but still marked when they changed.'))}</p>${fileBlocks}`;
    },
});
