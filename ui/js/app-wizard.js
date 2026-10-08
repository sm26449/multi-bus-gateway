/* Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
 * Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
// wizard domain — augments JanitzaMonitor.prototype
Object.assign(JanitzaMonitor.prototype, {

    // ── Add/Edit Device wizard ──────────────────────────────────────────────
    async openDeviceWizard(editId = null, prefill = null) {
        await this._fetchDevices(true);
        let templates = [];
        try { templates = (await (await fetch('/api/device-templates')).json()).templates || []; }
        catch (e) { console.error(e); }
        const editing = editId ? (this._devices || []).find(d => d.id === editId) : null;
        if (this._loadBridges) await this._loadBridges();
        this._devWiz = {
            step: 1, editId, templates,
            primary: !!editing?.primary,
            data: editing ? {
                id: editing.id, name: editing.name || editing.id,
                template: editing.template || '',
                enabled: editing.enabled !== false,
                // a device on a bridge edits as "over network, this bridge, this bus"
                protocol: editing.protocol || 'tcp',      // resolved: rtu-tcp, tcp (through a gateway), rtu_tap…
                bridge: editing.bridge || '', bridge_port: editing.bridge_port || null,
                host: editing.host || '', port: editing.port || 502,
                unit_id: editing.unit_id ?? 1, timeout: editing.timeout ?? 3,
                serial_port: editing.serial?.serial_port || '', baudrate: editing.serial?.baudrate || 9600,
                parity: editing.serial?.parity || 'N', stopbits: editing.serial?.stopbits || 1,
                url: editing.connection?.url || editing.http?.url || '',
                topic_prefix: editing.mqtt_topic_prefix || '',
                bucket: editing.influxdb_bucket || '',
                device_tag: editing.influxdb_device_tag || editing.id,
                ha_discovery_enabled: editing.ha_discovery_enabled !== false,
            } : {
                id: '', name: '', template: '', enabled: true,
                protocol: 'tcp', host: '', port: 502, unit_id: 1, timeout: 3,
                serial_port: '', baudrate: 9600, parity: 'N', stopbits: 1, url: '',
                topic_prefix: '', bucket: '', device_tag: '', ha_discovery_enabled: true,
            },
        };
        // Prefill a fresh wizard from a discovered device (protocol/url/template/name/id…).
        if (prefill && !editing) Object.assign(this._devWiz.data, prefill);
        // a device on a bus of a Modbus TCP gateway is a Modbus TCP device
        if (this._devWiz.data.bridge && this._bridgeType && this._bridgeType(
                (this._bridges || []).find(b => b.id === this._devWiz.data.bridge)?.type).framing === 'modbus_tcp')
            this._devWiz.data.protocol = 'tcp';
        // what the protocol may become when editing: Modbus variants share a
        // register map (by address); HTTP and MQTT maps read by path
        const _cls = p => (p === 'http' || p === 'mqtt') ? p : 'modbus';
        this._devWiz.lockClass = editing ? _cls(this._devWiz.data.protocol) : null;
        document.getElementById('devWizTitle').textContent =
            editId ? this.t('devices.wizard.titleEdit', 'Edit Device') : this.t('devices.wizard.titleAdd', 'Add Device');
        this._devWizRender();
        this.openModal('deviceWizardModal');
        // Clickable step rail — jump back to an already-seen step (no re-validation
        // needed going backward). onclick (not addEventListener) so re-opening the
        // wizard never stacks duplicate handlers.
        document.querySelectorAll('#devWizSteps .wizard-step').forEach(li => {
            li.tabIndex = 0;
            li.onclick = () => {
                const n = parseInt(li.dataset.step, 10);
                if (n < this._devWiz.step) { this._devWizCollect(); this._devWiz.step = n; this._devWizRender(); }
            };
            li.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); li.click(); } };
        });
    },

    _devWizRender() {
        const w = this._devWiz;
        document.querySelectorAll('#devWizSteps .wizard-step').forEach(s => {
            const n = parseInt(s.dataset.step, 10);
            s.classList.toggle('active', n === w.step);
            s.classList.toggle('done', n < w.step);
            if (n === w.step) s.setAttribute('aria-current', 'step');
            else s.removeAttribute('aria-current');       // SR announces the active step
        });
        document.getElementById('devWizBack').style.visibility = w.step === 1 ? 'hidden' : 'visible';
        document.getElementById('devWizNextLabel').textContent = w.step === 3
            ? (w.editId ? this.t('common.save', 'Save') : this.t('devices.wizard.create', 'Create device'))
            : this.t('common.next', 'Next');
        const fb = document.getElementById('devWizFeedback');
        if (fb) { fb.textContent = ''; fb.className = 'save-feedback'; }
        const body = document.getElementById('devWizBody');
        body.innerHTML = w.step === 1 ? this._devWizStep1Html()
                       : w.step === 2 ? this._devWizStep2Html()
                       : this._devWizStep3Html();
        this._devWizWire();
        // Move focus to the first field of the step (keyboard + screen-reader
        // users land where they can act; setTimeout lets the modal become visible
        // on the initial open before we focus).
        setTimeout(() => body.querySelector(
            'input:not([disabled]), select:not([disabled]), textarea')?.focus(), 0);
    },

    _devWizStep1Html() {
        const d = this._devWiz.data;
        const tcp = d.protocol === 'tcp';
        const http = d.protocol === 'http';
        const isRtu = d.protocol === 'rtu' || d.protocol === 'rtu-tcp' || d.protocol === 'rtu_tap';
        const rtuBridge = d.protocol === 'rtu-tcp';   // over-network (ser2net) vs direct serial
        const tap = d.protocol === 'rtu_tap';         // listen-only: another master owns the bus
        const tapNet = tap && !!d.bridge;             // …heard through a transparent bridge
        // Protocol is fixed after creation: the template's register map is
        // transport-specific (Modbus reads by address, HTTP by json_path), so
        // switching transport would orphan the map.
        // editing: only the variants that keep the register map valid
        const lockClass = this._devWiz.lockClass;
        const cls = p => (p === 'http' || p === 'mqtt') ? p : 'modbus';
        const off = p => lockClass && cls(p) !== lockClass;
        const locked = !!lockClass;
        const lk = p => off(p) ? 'disabled' : '';
        return `
        <div class="wiz-eyebrow">${this.t('devices.wizard.protoQ', 'How is it connected?')}</div>
        <div class="seg" role="radiogroup" aria-label="${this.t('devices.wizard.protocol', 'Protocol')}">
            <label class="seg-btn ${tcp ? 'on' : ''} ${off('tcp') ? 'disabled' : ''}"><input type="radio" name="devWizProto" value="tcp" ${tcp ? 'checked' : ''} ${lk('tcp')}><span class="s"></span> Modbus TCP</label>
            <label class="seg-btn ${isRtu ? 'on' : ''} ${off('rtu') ? 'disabled' : ''}"><input type="radio" name="devWizProto" value="rtu" ${isRtu ? 'checked' : ''} ${lk('rtu')}><span class="s"></span> Modbus RTU</label>
            <label class="seg-btn ${http ? 'on' : ''} ${off('http') ? 'disabled' : ''}"><input type="radio" name="devWizProto" value="http" ${http ? 'checked' : ''} ${lk('http')}><span class="s"></span> HTTP / JSON</label>
            <label class="seg-btn ${d.protocol === 'mqtt' ? 'on' : ''} ${off('mqtt') ? 'disabled' : ''}"><input type="radio" name="devWizProto" value="mqtt" ${d.protocol === 'mqtt' ? 'checked' : ''} ${lk('mqtt')}><span class="s"></span> MQTT</label>
        </div>
        ${locked ? `<div class="field-hint" style="margin:-8px 0 14px;"><i aria-hidden="true" class="bi bi-lock"></i> ${lockClass === 'modbus'
            ? this.t('devices.wizard.protoModbusOnly', 'A Modbus device can move between Modbus TCP, RTU, over the network or listen-only — its register map stays valid. HTTP and MQTT read by path, so they need a new device.')
            : this.t('devices.wizard.protoLocked', 'Fixed after creation — the template map is transport-specific.')}</div>` : ''}
        <div id="devWizHttpFields" style="display:${http ? '' : 'none'}">
            <div class="form-group">
                <label class="form-label" for="devWizUrl">${this.t('devices.wizard.httpUrl', 'JSON endpoint URL')}</label>
                <input type="text" id="devWizUrl" class="input" value="${this._esc(d.url || '')}" placeholder="http://192.168.1.50/rpc/Shelly.GetStatus">
                <div class="field-hint">${this.t('devices.wizard.httpHint', 'The device polls this URL and reads values by the template’s json_path. Values arrive already scaled.')}</div>
            </div>
            <button class="btn btn-secondary btn-sm" data-action="devWizardTest" data-with-el>
                <i aria-hidden="true" class="bi bi-activity"></i> ${this.t('devices.wizard.testConn', 'Test connection')}</button>
            <div class="wiz-test-result" id="devWizTestResult3" role="status"></div>
        </div>
        <div id="devWizTcpFields" style="display:${tcp ? '' : 'none'}">
            ${this._devWizGatewayPickHtml(d)}
            <div class="form-row">
                <div class="form-group flex-2" ${tcp && d.bridge ? 'style="display:none"' : ''}>
                    <label class="form-label" for="devWizHost">${this.t('lbl.hostIp', "Host / IP")}</label>
                    <input type="text" id="devWizHost" class="input" value="${this._esc(d.host)}" placeholder="192.168.1.60">
                </div>
                <div class="form-group" ${tcp && d.bridge ? 'style="display:none"' : ''}>
                    <label class="form-label" for="devWizPort">${this.t('lbl.port', "Port")}</label>
                    <input type="number" id="devWizPort" class="input" aria-label="Port" value="${d.port}" min="1" max="65535">
                    <div class="field-hint">1–65535 · ${this.t('common.default', 'default')} 502</div>
                </div>
                <div class="form-group">
                    <label class="form-label" for="devWizUnit">${this.t('lbl.unitId', "Unit ID")}</label>
                    <input type="number" id="devWizUnit" class="input" aria-label="Unit ID" value="${d.unit_id}" min="0" max="255">
                    <div class="field-hint">0–255 · ${this.t('common.default', 'default')} 1</div>
                </div>
                <div class="form-group">
                    <label class="form-label" for="devWizTimeout">${this.t('lbl.timeoutS', "Timeout (s)")}</label>
                    <input type="number" id="devWizTimeout" class="input" aria-label="Timeout seconds" value="${d.timeout}" min="1" max="30">
                    <div class="field-hint">1–30 · ${this.t('common.default', 'default')} 3</div>
                </div>
            </div>
            <button class="btn btn-secondary btn-sm" id="devWizTestBtn" data-action="devWizardTest" data-with-el>
                <i aria-hidden="true" class="bi bi-activity"></i> ${this.t('devices.wizard.testConn', 'Test connection')}</button>
            <div class="wiz-test-result" id="devWizTestResult" role="status"></div>
        </div>
        <div id="devWizRtuFields" style="display:${isRtu ? '' : 'none'}">
            <div class="seg seg-sm" role="radiogroup" aria-label="${this.t('devices.wizard.rtuMode', 'RTU connection mode')}" style="margin-bottom:12px;">
                <label class="seg-btn ${rtuBridge ? 'on' : ''}"><input type="radio" name="devWizRtuMode" value="rtu-tcp" ${rtuBridge ? 'checked' : ''}><span class="s"></span> ${this.t('devices.wizard.rtuOverNet', 'Over network (auto-detect)')} <span class="seg-reco">${this.t('common.recommended', 'recommended')}</span></label>
                <label class="seg-btn ${d.protocol === 'rtu' ? 'on' : ''}"><input type="radio" name="devWizRtuMode" value="rtu" ${d.protocol === 'rtu' ? 'checked' : ''}><span class="s"></span> ${this.t('devices.wizard.rtuDirect', 'Direct serial')}</label>
                <label class="seg-btn ${tap ? 'on' : ''}"><input type="radio" name="devWizRtuMode" value="rtu_tap" ${tap ? 'checked' : ''}><span class="s"></span> ${this.t('devices.wizard.rtuTap', 'Listen only (tap)')}</label>
            </div>
            <div id="devWizRtuBridge" style="display:${rtuBridge ? '' : 'none'}">
                ${this._devWizBridgePickHtml(d)}
                <div class="form-row" style="align-items:end;${(this._bridges || []).length ? 'display:none;' : ''}" id="devWizLegacyScan">
                    <div class="form-group flex-2">
                        <label class="form-label" for="devWizAdapter">${this.t('devices.wizard.adapter', 'USB serial adapter')}</label>
                        <select id="devWizAdapter" class="input" data-action="_devWizAdapterPick" data-with-value data-on="change">
                            ${this._devWizAdapterOptions(d)}
                        </select>
                    </div>
                    <div class="form-group">
                        <button class="btn btn-secondary btn-sm" id="devWizScanBtn" data-action="devWizScanBridge" data-with-el>
                            <i aria-hidden="true" class="bi bi-arrow-repeat"></i> ${this.t('devices.wizard.scan', 'Scan')}</button>
                    </div>
                    <div class="form-group">
                        <label class="form-label" for="devWizUnitB">${this.t('lbl.unitId', "Unit ID")}</label>
                        <input type="number" id="devWizUnitB" class="input" aria-label="Unit ID" value="${d.unit_id}" min="0" max="255">
                    </div>
                </div>
                <div class="field-hint" id="devWizBridgeHint" style="margin-top:2px;">${
                    d.host && rtuBridge
                        ? this.t('devices.wizard.bridgeBound', 'Bound to {ep} — the adapter keeps this endpoint across replug.').replace('{ep}', `${d.host}:${d.port}`)
                        : this.t('devices.wizard.bridgeScanHint', 'Plug the adapter in and press Scan. Baud rate is set on the bridge (default 9600 8N1).')}</div>
                <button class="btn btn-secondary btn-sm" style="margin-top:8px;" data-action="devWizardTest" data-with-el>
                    <i aria-hidden="true" class="bi bi-activity"></i> ${this.t('devices.wizard.testConn', 'Test connection')}</button>
            </div>
            <div id="devWizRtuDirect" style="display:${rtuBridge ? 'none' : ''}">
                ${tap ? this._devWizTapWhereHtml(d) : ''}
                <div class="form-row">
                    <div class="form-group flex-2" ${tapNet ? 'style="display:none"' : ''}>
                        <label class="form-label" for="devWizSerial">${this.t('devices.wizard.serialPort', 'Serial port')}</label>
                        <input type="text" id="devWizSerial" class="input" value="${this._esc(d.serial_port)}" placeholder="/dev/ttyUSB0">
                    </div>
                    <div class="form-group" ${tapNet ? 'style="display:none"' : ''}>
                        <label class="form-label" for="devWizBaud">${this.t('lbl.baudrate', "Baudrate")}</label>
                        <select id="devWizBaud" class="input">
                            ${[9600, 19200, 38400, 57600, 115200].map(b => `<option ${b === +d.baudrate ? 'selected' : ''}>${b}</option>`).join('')}
                        </select>
                    </div>
                    <div class="form-group" ${tapNet ? 'style="display:none"' : ''}>
                        <label class="form-label" for="devWizParity">${this.t('lbl.parity', "Parity")}</label>
                        <select id="devWizParity" class="input">
                            ${['N', 'E', 'O'].map(x => `<option ${x === d.parity ? 'selected' : ''}>${x}</option>`).join('')}
                        </select>
                    </div>
                    <div class="form-group">
                        <label class="form-label" for="devWizUnitR">${this.t('lbl.unitId', "Unit ID")}</label>
                        <input type="number" id="devWizUnitR" class="input" aria-label="Unit ID" value="${d.unit_id}" min="0" max="255">
                    </div>
                </div>
                ${tapNet ? `<div class="field-hint" style="margin-top:6px;"><i aria-hidden="true" class="bi bi-ear"></i> ${this.t('devices.wizard.rtuTapNetNote', 'Listen-only through the bridge: another master already polls this bus — the gateway never transmits, it decodes the answers that unit gives, at the master’s rhythm. Several taps can listen to one bus, each with its own unit id; a bus is either polled or tapped. After saving, Test on the device shows what the tap hears.')}</div>`
                : tap ? `<div class="field-hint" style="margin-top:6px;"><i aria-hidden="true" class="bi bi-ear"></i> ${this.t('devices.wizard.rtuTapNote', 'Listen-only: another master already polls this bus — the gateway never transmits, it decodes the answers that unit gives. Values arrive at the master’s rhythm; several taps can share one port (same baud/parity). The adapter must be mapped into the container (e.g. devices: /dev/ttyUSB1). After saving, Test on the device shows what the tap hears.')}</div>` : `
                <button class="btn btn-secondary btn-sm" data-action="devWizardTest" data-with-el>
                    <i aria-hidden="true" class="bi bi-activity"></i> ${this.t('devices.wizard.testConn', 'Test connection')}</button>
                <div class="field-hint" style="margin-top:6px;">${this.t('devices.wizard.rtuNote', 'The serial device must be attached to the host and mapped into the container (e.g. devices: /dev/ttyUSB0). It starts polling right after saving.')}</div>`}
            </div>
            <div class="wiz-test-result" id="devWizTestResult2" role="status"></div>
        </div>
        <div id="devWizMqttFields" style="display:${d.protocol === 'mqtt' ? '' : 'none'}">
            <div class="form-row">
                <div class="form-group flex-2">
                    <label class="form-label" for="devWizBroker">${this.t('devices.wizard.broker', 'MQTT broker')}</label>
                    <input type="text" id="devWizBroker" class="input" value="${this._esc(d.broker || '')}" placeholder="192.168.1.100">
                </div>
                <div class="form-group">
                    <label class="form-label" for="devWizMqttPort">${this.t('lbl.port', 'Port')}</label>
                    <input type="number" id="devWizMqttPort" class="input" value="${d.mqtt_port || 1883}" min="1" max="65535">
                </div>
            </div>
            <div class="form-group">
                <label class="form-label" for="devWizMqttTopic">${this.t('devices.wizard.mqttTopic', 'Topic (subscribe)')}</label>
                <input type="text" id="devWizMqttTopic" class="input" value="${this._esc(d.topic || '')}" placeholder="sensors/# or shellies/shelly1/status">
                <div class="field-hint">${this.t('devices.wizard.mqttTopicHint', 'The device subscribes here; values are read from the JSON payload by the template’s json_path. + and # wildcards supported.')}</div>
                <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
                    <span class="field-hint" style="margin:0;">${this.t('devices.wizard.presets', 'Presets:')}</span>
                    <button type="button" class="calc-chip" ${this._act('_devWizMqttPreset', ["zigbee2mqtt_sensor", "zigbee2mqtt/<friendly_name>"])}><i aria-hidden="true" class="bi bi-broadcast-pin"></i> Zigbee (zigbee2mqtt)</button>
                    <button type="button" class="calc-chip" ${this._act('_devWizMqttPreset', ["ble_theengs_sensor", "home/TheengsGateway/BTtoMQTT/<MAC>"])}><i aria-hidden="true" class="bi bi-bluetooth"></i> BLE (Theengs/BTHome)</button>
                </div>
            </div>
            <div class="form-row">
                <div class="form-group"><label class="form-label" for="devWizMqttUser">${this.t('lbl.username', 'Username')}</label>
                    <input type="text" id="devWizMqttUser" class="input" value="${this._esc(d.mqtt_username || '')}" autocomplete="off"></div>
                <div class="form-group"><label class="form-label" for="devWizMqttPass">${this.t('lbl.password', 'Password')}</label>
                    <input type="password" id="devWizMqttPass" class="input" value="${this._esc(d.mqtt_password || '')}" autocomplete="new-password"></div>
                <div class="form-group" style="align-self:end;"><label style="display:flex;align-items:center;gap:8px;">
                    <input type="checkbox" id="devWizMqttTls" ${d.mqtt_tls ? 'checked' : ''}> ${this.t('devices.wizard.mqttTls', 'TLS (8883)')}</label></div>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
                <button class="btn btn-secondary btn-sm" data-action="devWizardTest" data-with-el>
                    <i aria-hidden="true" class="bi bi-activity"></i> ${this.t('devices.wizard.testConn', 'Test connection')}</button>
                <button class="btn btn-secondary btn-sm" id="devWizMqttBrowseBtn" data-action="devWizMqttBrowse" data-with-el>
                    <i aria-hidden="true" class="bi bi-binoculars"></i> ${this.t('devices.wizard.browseTopics', 'Browse topics')}</button>
            </div>
            <div class="wiz-test-result" id="devWizTestResult4" role="status"></div>
            <div id="devWizMqttBrowse" style="display:none;">
                <input type="text" id="devWizMqttBrowseFilter" class="input" style="margin:8px 0 6px;"
                       placeholder="${this._esc(this.t('devices.wizard.browseFilter', 'Filter topics…'))}"
                       data-action="_devWizBrowseRender" data-on="input">
                <div class="mqtt-browse-list" id="devWizMqttBrowseList" role="listbox"
                     aria-label="MQTT topics"></div>
                <div class="field-hint" id="devWizMqttBrowseHint"></div>
            </div>
        </div>`;
    },

    // ── MQTT topic browse: collect the broker's speaking topics, click to fill ──
    async devWizMqttBrowse(btn) {
        const g = id => document.getElementById(id);
        const broker = g('devWizBroker')?.value.trim();
        const out = g('devWizTestResult4');
        if (!broker) {
            out.innerHTML = `<span class="err">${this._esc(this.t('devices.wizard.browseNeedBroker', 'Enter the broker address first.'))}</span>`;
            return;
        }
        btn.disabled = true;
        g('devWizMqttBrowse').style.display = '';
        g('devWizMqttBrowseList').innerHTML =
            `<div class="field-hint" style="padding:8px;">${this._esc(this.t('devices.wizard.browseListening', 'Listening 3s — retained topics appear instantly, live ones as they publish…'))}</div>`;
        g('devWizMqttBrowseHint').textContent = '';
        try {
            const r = await fetch('/api/discover/mqtt/browse', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    broker,
                    port: parseInt(g('devWizMqttPort')?.value, 10) || 1883,
                    username: g('devWizMqttUser')?.value.trim() || '',
                    password: g('devWizMqttPass')?.value || '',
                    tls: !!g('devWizMqttTls')?.checked,
                }),
            });
            const data = await r.json();
            if (!r.ok) throw new Error((data.detail?.errors || [r.statusText]).join('; '));
            if (!data.ok) throw new Error(data.error || 'browse failed');
            this._mqttBrowseTopics = data.topics || [];
            this._devWizBrowseRender();
            g('devWizMqttBrowseHint').textContent = data.truncated
                ? this.t('devices.wizard.browseTruncated', 'Topic list truncated — narrow it with the filter field.')
                : this.t('devices.wizard.browsePick', 'Click a topic to use it as the subscribe topic.');
        } catch (e) {
            g('devWizMqttBrowseList').innerHTML =
                `<div class="err" style="padding:8px;">${this._esc(String(e.message || e))}</div>`;
        } finally {
            btn.disabled = false;
        }
    },

    _devWizBrowseRender() {
        const list = document.getElementById('devWizMqttBrowseList');
        if (!list) return;
        const q = (document.getElementById('devWizMqttBrowseFilter')?.value || '').toLowerCase();
        const all = this._mqttBrowseTopics || [];
        const rows = q ? all.filter(t => t.topic.toLowerCase().includes(q)) : all;
        if (!rows.length) {
            list.innerHTML = `<div class="field-hint" style="padding:8px;">${this._esc(
                all.length ? this.t('devices.wizard.browseNoMatch', 'No topic matches the filter.')
                           : this.t('devices.wizard.browseEmpty', 'No topics heard — the broker may be idle (nothing retained, nobody publishing). Try again while the device is sending.'))}</div>`;
            return;
        }
        list.innerHTML = rows.slice(0, 400).map(t => `
            <div class="mqtt-browse-row" role="option" tabindex="0"
                 ${this._act('_devWizBrowsePick', [t.topic])} data-key-enter>
                <span class="mqtt-browse-topic">${this._esc(t.topic)}</span>
                ${t.retained ? '<span class="badge badge-secondary">retained</span>' : ''}
                ${t.count > 1 ? `<span class="mqtt-browse-count">×${t.count}</span>` : ''}
                <span class="mqtt-browse-payload">${this._esc((t.payload || '').slice(0, 120))}</span>
            </div>`).join('');
    },

    _devWizBrowsePick(topic) {
        const inp = document.getElementById('devWizMqttTopic');
        if (inp) {
            inp.value = topic;
            inp.focus();
        }
        const hint = document.getElementById('devWizMqttBrowseHint');
        if (hint) hint.textContent = this.t('devices.wizard.browsePicked', 'Topic set. Wildcards + and # still work if you generalize it.');
    },

    // Sensor preset chips (step 1, MQTT): fill the subscribe-topic pattern for
    // the bridge and preselect the matching template for step 2. The user
    // replaces the <placeholder> with their device's friendly name / MAC.
    _devWizMqttPreset(templateId, topicPattern) {
        const inp = document.getElementById('devWizMqttTopic');
        if (inp && !inp.value.trim()) inp.value = topicPattern;
        if (this._devWiz) this._devWiz.data.template = templateId;
        inp?.focus();
        inp?.select?.();
    },

    _devWizStep2Html() {
        const w = this._devWiz;
        // Only templates whose transport matches the chosen protocol — a Modbus
        // map can't be read over HTTP (and vice versa). Drop a now-incompatible
        // selection so the picker + validation stay honest.
        const cls = w.data.protocol === 'http' ? 'http'
                  : w.data.protocol === 'mqtt' ? 'mqtt' : 'modbus';
        const compatible = w.templates.filter(t => (t.transport || 'modbus') === cls);
        if (w.data.template && !compatible.some(t => t.id === w.data.template)) w.data.template = '';
        const rows = compatible.map(t => `
            <div class="tpl-pick ${t.id === w.data.template ? 'selected' : ''}" data-tpl="${this._esc(t.id)}"
                 role="radio" aria-checked="${t.id === w.data.template}" tabindex="0">
                <i aria-hidden="true" class="bi ${t.id === w.data.template ? 'bi-check-circle-fill' : 'bi-circle'}"></i>
                <div><strong>${this._esc(t.name)}</strong>
                    <div class="field-hint">${this._esc(t.vendor || '')} ${this._esc(t.model || '')}
                        ${(t.used_by || []).length ? `· ${this.t('devtpl.usedBy', 'used by')} ${t.used_by.map(x => this._esc(x)).join(', ')}` : ''}</div></div>
                <div class="tpl-pick-meta">${t.builtin ? this.t('devices.builtin', 'built-in') : this.t('devices.community', 'user')}<br>${t.registers} measurements · v${this._esc(t.version)}</div>
                <div class="tpl-pick-actions">
                    ${t.builtin
                        ? `<button class="btn btn-ghost btn-sm" ${this._act('openTplEditor', [t.id, true])} title="${this.t('devtpl.duplicate', 'Duplicate to edit')}"><i aria-hidden="true" class="bi bi-copy"></i></button>`
                        : `<button class="btn btn-ghost btn-sm" ${this._act('openTplEditor', [t.id])} title="${this.t('common.edit', 'Edit')}"><i aria-hidden="true" class="bi bi-pencil"></i></button>` +
                          ((t.used_by || []).length ? '' : `<button class="btn btn-ghost btn-sm" ${this._act('tplDelete', [t.id])} title="${this.t('common.delete', 'Delete')}"><i aria-hidden="true" class="bi bi-trash"></i></button>`)}
                    <button class="btn btn-ghost btn-sm" ${this._act('tplExport', [t.id])} title="${this.t('common.export', 'Export')}"><i aria-hidden="true" class="bi bi-download"></i></button>
                </div>
            </div>`).join('');
        return `
        <div class="wiz-eyebrow">${this.t('devices.wizard.tplQ', 'What is it?')}</div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
            <input type="text" id="devWizTplSearch" class="input" placeholder="${this.t('common.search', 'Search')}…" style="max-width:240px;">
            <button class="btn btn-secondary btn-sm" data-action="tplUpload">
                <i aria-hidden="true" class="bi bi-upload"></i> ${this.t('devtpl.upload', 'Upload template')}</button>
            <button class="btn btn-secondary btn-sm" ${this._act('openTplEditor', [null])}>
                <i aria-hidden="true" class="bi bi-plus-lg"></i> ${this.t('devtpl.new', 'New template')}</button>
        </div>
        <p class="field-hint" style="margin:6px 0;">${this.t('devices.wizard.tplIntro2', 'The template is the measurement map of the equipment: pick one, upload a file, or create your own.')}</p>
        <div class="tpl-pick-list" id="devWizTplList" role="radiogroup">${rows ||
            `<span class="field-hint">${this.t('devices.wizard.noTplForProto', 'No {proto} templates yet — upload or create one for this protocol.').replace('{proto}', cls === 'http' ? 'HTTP/JSON' : cls === 'mqtt' ? 'MQTT' : 'Modbus')}</span>`}</div>
        <div class="field-error" id="devWizTplError" style="display:none;">${this.t('devices.wizard.tplRequired', 'Choose a template to continue.')}</div>`;
    },

    _devWizStep3Html() {
        const d = this._devWiz.data;
        const idLocked = !!this._devWiz.editId;
        // The primary device (UMG512) keeps its published identity byte-for-byte:
        // topic, bucket and Influx tag are fixed so existing history/HA entities
        // don't break. Only its connection, name and HA-discovery flag are editable.
        // Routing (topic/bucket/tag) is fixed for the primary AND after creation:
        // changing it re-routes future data and orphans existing history / HA entities.
        const routeLocked = this._devWiz.primary || !!this._devWiz.editId;
        const routeNote = routeLocked
            ? `<div class="field-hint"><i aria-hidden="true" class="bi bi-lock"></i> ${this._devWiz.primary
                ? this.t('devices.wizard.routeLocked', 'Fixed for device #1 (protects existing history &amp; Home Assistant entities).')
                : this.t('devices.wizard.routeLockedEdit', 'Fixed after creation — changing it would orphan existing history &amp; HA entities.')}</div>`
            : '';
        return `
        <div class="wiz-eyebrow">${this.t('devices.wizard.routeQ', 'Where does its data go?')}</div>
        <div class="form-row">
            <div class="form-group flex-2">
                <label class="form-label" for="devWizName">${this.t('devices.wizard.name', 'Device name')}</label>
                <input type="text" id="devWizName" class="input" value="${this._esc(d.name)}" placeholder="Warehouse EM24">
            </div>
            <div class="form-group">
                <label class="form-label" for="devWizId">${this.t('devices.wizard.id', 'Device id')}</label>
                <input type="text" id="devWizId" class="input" value="${this._esc(d.id)}" ${idLocked ? 'disabled' : ''}
                       pattern="[a-z0-9][a-z0-9_-]{1,63}" placeholder="em24-hala">
                <div class="field-hint">${idLocked ? this.t('devices.wizard.idLocked', 'Fixed after creation (used in topics).')
                                                   : this.t('devices.wizard.idHint', 'a-z 0-9 - _ · used in MQTT/InfluxDB routing')}</div>
                <div class="field-error" id="devWizIdError" style="display:none;"></div>
            </div>
        </div>
        <div class="form-row">
            <div class="form-group flex-2">
                <label class="form-label" for="devWizTopic">${this.t('devices.wizard.topicPrefix', 'MQTT topic prefix')}</label>
                <input type="text" id="devWizTopic" class="input" value="${this._esc(d.topic_prefix)}" placeholder="meters/em24-hala" ${routeLocked ? 'disabled' : ''}>
                ${routeNote}
            </div>
            <div class="form-group">
                <label class="form-label" for="devWizBucket">${this.t('devices.wizard.bucket', 'InfluxDB bucket')}</label>
                <input type="text" id="devWizBucket" class="input" value="${this._esc(d.bucket)}" placeholder="meters" ${routeLocked ? 'disabled' : ''}>
                <div class="field-hint">${this.t('devices.wizard.bucketHint', 'Auto-created (90d retention) if missing.')}</div>
            </div>
            <div class="form-group">
                <label class="form-label" for="devWizTag">${this.t('devices.wizard.deviceTag', 'Influx device tag')}</label>
                <input type="text" id="devWizTag" class="input" value="${this._esc(d.device_tag)}" placeholder="(= id)" ${routeLocked ? 'disabled' : ''}>
            </div>
        </div>
        <div class="topic-preview" id="devWizTopicPreview"></div>
        <div style="display:flex;flex-direction:column;gap:2px;margin-top:15px;">
        <label class="wiz-tog">
            <input type="checkbox" id="devWizHaDisc" ${d.ha_discovery_enabled ? 'checked' : ''}><span class="wiz-switch"></span>
            <span>${this.t('devices.wizard.haDiscovery', 'Publish Home Assistant MQTT discovery for this device')}</span>
        </label>
        <label class="wiz-tog">
            <input type="checkbox" id="devWizEnabled" ${d.enabled ? 'checked' : ''}><span class="wiz-switch"></span>
            <span>${this.t('devices.wizard.enabled', 'Start polling immediately after saving')}</span>
        </label></div>`;
    },

    _devWizWire() {
        const w = this._devWiz;
        // Inline validation feedback: clear a field's error state the moment the
        // user starts fixing it, and let Enter advance the wizard (novice-friendly).
        // Attached to the freshly-rendered inputs each render, so no listener leak.
        document.getElementById('devWizBody')?.querySelectorAll('.input').forEach(inp => {
            inp.addEventListener('input', () => {
                inp.classList.remove('invalid');
                inp.removeAttribute('aria-invalid');
                document.getElementById(inp.id + 'Error')?.style.setProperty('display', 'none');
            });
            if (w.step !== 2) inp.addEventListener('keydown', e => {   // step 2 = template pick
                if (e.key === 'Enter') { e.preventDefault(); this.devWizardNext(); }
            });
        });
        if (w.step === 1) {
            document.querySelectorAll('input[name="devWizProto"]').forEach(r =>
                r.addEventListener('change', () => {
                    this._devWizCollect();
                    // RTU defaults to over-network (bridge) — the recommended mode;
                    // the sub-toggle lets the user switch to direct serial.
                    w.data.protocol = r.value === 'rtu' ? 'rtu-tcp' : r.value;
                    this._devWizFitBridge();
                    this._devWizRender();
                }));
            document.querySelectorAll('input[name="devWizRtuMode"]').forEach(r =>
                r.addEventListener('change', () => {
                    this._devWizCollect();
                    w.data.protocol = r.value;          // 'rtu' | 'rtu-tcp' | 'rtu_tap'
                    this._devWizFitBridge();
                    this._devWizRender();
                }));
        } else if (w.step === 2) {
            const list = document.getElementById('devWizTplList');
            // Sensor presets: picking a bridge-backed sensor template prefills
            // the (still empty) subscribe topic with the bridge's documented
            // pattern — the user replaces the <placeholder> with their device.
            const TOPIC_PRESETS = {
                zigbee2mqtt_sensor: 'zigbee2mqtt/<friendly_name>',
                ble_theengs_sensor: 'home/TheengsGateway/BTtoMQTT/<MAC>',
            };
            const pick = (el) => {
                w.data.template = el.dataset.tpl;
                if (w.data.protocol === 'mqtt' && !(w.data.topic || '').trim()
                    && TOPIC_PRESETS[el.dataset.tpl]) {
                    w.data.topic = TOPIC_PRESETS[el.dataset.tpl];
                }
                this._devWizRender();
            };
            list?.querySelectorAll('.tpl-pick').forEach(el => {
                el.addEventListener('click', (e) => { if (!e.target.closest('.tpl-pick-actions')) pick(el); });
                el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(el); } });
            });
            const search = document.getElementById('devWizTplSearch');
            search?.addEventListener('input', () => {
                const q = search.value.toLowerCase();
                list.querySelectorAll('.tpl-pick').forEach(el =>
                    el.style.display = el.textContent.toLowerCase().includes(q) ? '' : 'none');
            });
        } else if (w.step === 3) {
            const name = document.getElementById('devWizName');
            const id = document.getElementById('devWizId');
            const topic = document.getElementById('devWizTopic');
            const preview = () => {
                const did = id.value.trim() || 'device-id';
                const p = topic.value.trim() || `meters/${did}`;
                const tag = document.getElementById('devWizTag')?.value.trim() || did;
                document.getElementById('devWizTopicPreview').innerHTML =
                    `<div class="tp-row"><span class="tp-node">${this._esc(did)}</span>` +
                    `<span class="tp-arrow">→</span>` +
                    `<span class="tp-topic">${this._esc(p)}/voltage/l1_n</span></div>` +
                    `<div class="tp-row tp-sub"><span class="tp-dim">${this.t('devices.wizard.influxTag', 'Influx tag')}</span> ` +
                    `<span class="tp-topic">${this._esc(tag)}</span></div>`;
            };
            name?.addEventListener('input', () => {
                if (!this._devWiz.editId && !id.dataset.touched) {
                    id.value = name.value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32);
                    preview();
                }
            });
            id?.addEventListener('input', () => { id.dataset.touched = '1'; preview(); });
            topic?.addEventListener('input', preview);
            document.getElementById('devWizTag')?.addEventListener('input', preview);
            preview();
        }
    },

    // Build the <option> list for the bridge adapter dropdown from the last
    // scan (cached on w.data._adapters). Keeps the currently-bound endpoint
    // visible even before a scan (e.g. when editing an rtu-tcp device).
    _devWizAdapterOptions(d) {
        const ads = (this._devWiz.data._adapters || []).filter(a => a.available && a.tcp_port);
        const cur = (d.host && d.port) ? `${d.host}::${d.port}` : '';
        const opts = [];
        if (!ads.length && !cur) {
            return `<option value="">${this._esc(this.t('devices.wizard.noAdapters', '— press Scan to detect adapters —'))}</option>`;
        }
        let curSeen = false;
        for (const a of ads) {
            const val = `${a.bridge_host}::${a.tcp_port}`;
            if (val === cur) curSeen = true;
            const label = `${a.model || a.manufacturer || a.vendor_id || 'serial'}`
                        + `${a.serial ? ' (' + a.serial + ')' : ''} → :${a.tcp_port}`;
            opts.push(`<option value="${this._esc(val)}" ${val === cur ? 'selected' : ''}>${this._esc(label)}</option>`);
        }
        if (cur && !curSeen) {   // bound endpoint not in the (possibly empty) scan — keep it selectable
            opts.unshift(`<option value="${this._esc(cur)}" selected>${this._esc(`${d.host}:${d.port}`)}</option>`);
        }
        return opts.join('');
    },

    // A dropdown pick binds the device to that adapter's stable endpoint.
    _devWizAdapterPick(value) {
        const [host, port] = String(value).split('::');
        const d = this._devWiz.data;
        d.host = host || '';
        d.port = parseInt(port, 10) || 0;
        const hint = document.getElementById('devWizBridgeHint');
        if (hint && d.host) {
            hint.textContent = this.t('devices.wizard.bridgeBound',
                'Bound to {ep} — the adapter keeps this endpoint across replug.')
                .replace('{ep}', `${d.host}:${d.port}`);
        }
    },

    // Scan the serial bridge for live adapters and refresh the dropdown.
    async devWizScanBridge(btn) {
        btn.disabled = true;
        const orig = btn.innerHTML;
        btn.innerHTML = `<i aria-hidden="true" class="bi bi-arrow-repeat"></i> ${this._esc(this.t('devices.wizard.scanning', 'Scanning…'))}`;
        let msg = '', ok = false;
        try {
            const r = await fetch('/api/bridge/adapters');
            const data = await r.json();
            if (!r.ok) throw new Error((data.detail?.errors || [data.detail || r.statusText]).join('; '));
            if (!data.available) throw new Error(data.error || this.t('devices.wizard.bridgeDown', 'Serial bridge not reachable.'));
            this._devWiz.data._adapters = data.adapters || [];
            const avail = this._devWiz.data._adapters.filter(a => a.available && a.tcp_port);
            if (avail.length === 1) this._devWizAdapterPick(`${avail[0].bridge_host}::${avail[0].tcp_port}`);
            ok = avail.length > 0;
            msg = avail.length
                ? this.t('devices.wizard.scanFound', 'Found {n} adapter(s).').replace('{n}', avail.length)
                : this.t('devices.wizard.scanNone', 'No available adapters — plug one in and Scan again.');
        } catch (e) {
            msg = String(e.message || e);
        }
        // The dropdown lives inside the re-rendered body, so render first, then
        // report into the freshly-created result element.
        this._devWizRender();
        const out = document.getElementById('devWizTestResult2');
        if (out) { out.className = 'wiz-test-result ' + (ok ? 'ok' : 'err'); out.textContent = (ok ? '✓ ' : '✗ ') + msg; }
    },

    _devWizCollect() {
        const w = this._devWiz, d = w.data;
        const g = id => document.getElementById(id);
        if (w.step === 1) {
            d.protocol = document.querySelector('input[name="devWizProto"]:checked')?.value || 'tcp';
            if (d.protocol === 'http') {
                d.url = g('devWizUrl')?.value.trim() ?? d.url;
            } else if (d.protocol === 'tcp') {
                if (g('devWizGw')) {
                    const [b, p] = (g('devWizGw').value || '').split('::');
                    d.bridge = b || '';
                    d.bridge_port = parseInt(p, 10) || null;
                } else if (d.bridge && this._bridgeType(((this._bridges || []).find(x => x.id === d.bridge) || {}).type).framing !== 'modbus_tcp') {
                    d.bridge = ''; d.bridge_port = null;     // came from an RTU bridge: TCP is direct
                }
                d.host = g('devWizHost')?.value.trim() ?? d.host;
                d.port = parseInt(g('devWizPort')?.value, 10) || 502;
                d.unit_id = parseInt(g('devWizUnit')?.value, 10) ?? 1;
                d.timeout = parseFloat(g('devWizTimeout')?.value) || 3;
            } else if (d.protocol === 'mqtt') {
                d.broker = g('devWizBroker')?.value.trim() ?? d.broker;
                d.mqtt_port = parseInt(g('devWizMqttPort')?.value, 10) || 1883;
                d.topic = g('devWizMqttTopic')?.value.trim() ?? d.topic;
                d.mqtt_username = g('devWizMqttUser')?.value.trim() ?? d.mqtt_username;
                d.mqtt_password = g('devWizMqttPass')?.value ?? d.mqtt_password;
                d.mqtt_tls = !!g('devWizMqttTls')?.checked;
            } else {   // rtu (direct serial), rtu_tap (listening) or rtu-tcp (over the bridge)
                const mode = document.querySelector('input[name="devWizRtuMode"]:checked')?.value;
                if (mode) d.protocol = mode;
                if (d.protocol === 'rtu-tcp') {
                    // through a bridge (its bus picked here), or the legacy scan
                    // of the local bridge (host/port set by _devWizAdapterPick)
                    if (g('devWizBridge')) {
                        d.bridge = g('devWizBridge').value || '';
                        d.bridge_port = parseInt(g('devWizBridgePort')?.value, 10) || null;
                        d.unit_id = parseInt(g('devWizUnitBr')?.value, 10) || 1;
                    } else {
                        d.unit_id = parseInt(g('devWizUnitB')?.value, 10) ?? 1;
                    }
                } else {
                    if (d.protocol === 'rtu_tap' && g('devWizTapWhere')) {
                        const [b, p] = (g('devWizTapWhere').value || '').split('::');
                        d.bridge = b || '';
                        d.bridge_port = parseInt(p, 10) || null;
                    } else if (d.protocol === 'rtu') {
                        d.bridge = ''; d.bridge_port = null;
                    }
                    d.serial_port = g('devWizSerial')?.value.trim() ?? d.serial_port;
                    d.baudrate = parseInt(g('devWizBaud')?.value, 10) || 9600;
                    d.parity = g('devWizParity')?.value || 'N';
                    d.unit_id = parseInt(g('devWizUnitR')?.value, 10) ?? 1;
                }
            }
        } else if (w.step === 3) {
            d.name = g('devWizName')?.value.trim() ?? d.name;
            if (!w.editId) d.id = g('devWizId')?.value.trim() ?? d.id;
            d.topic_prefix = g('devWizTopic')?.value.trim() ?? d.topic_prefix;
            d.bucket = g('devWizBucket')?.value.trim() ?? d.bucket;
            d.device_tag = g('devWizTag')?.value.trim() ?? d.device_tag;
            d.ha_discovery_enabled = !!g('devWizHaDisc')?.checked;
            d.enabled = !!g('devWizEnabled')?.checked;
        }
    },

    _wizInvalid(id) {
        const el = document.getElementById(id);
        if (el) {
            el.classList.add('invalid');
            el.setAttribute('aria-invalid', 'true');
            el.focus();                    // take the user straight to the problem field
        }
        return false;
    },

    _devWizValidate() {
        const w = this._devWiz, d = w.data;
        if (w.step === 1 && d.protocol === 'tcp' && !d.host && !d.bridge) return this._wizInvalid('devWizHost');
        if (w.step === 1 && (d.protocol === 'rtu' || (d.protocol === 'rtu_tap' && !d.bridge)) && !d.serial_port)
            return this._wizInvalid('devWizSerial');
        if (w.step === 1 && d.protocol === 'rtu-tcp' && !d.host && !d.bridge) return this._wizInvalid('devWizAdapter');
        if (w.step === 1 && d.protocol === 'http' && !/^https?:\/\//.test(d.url || ''))
            return this._wizInvalid('devWizUrl');
        if (w.step === 1 && d.protocol === 'mqtt' && !d.broker) return this._wizInvalid('devWizBroker');
        if (w.step === 1 && d.protocol === 'mqtt' && !d.topic) return this._wizInvalid('devWizMqttTopic');
        if (w.step === 2 && !d.template) {
            const err = document.getElementById('devWizTplError');
            if (err) err.style.display = '';
            return false;
        }
        if (w.step === 3 && !w.editId && !/^[a-z0-9][a-z0-9_-]{1,63}$/.test(d.id)) {
            const err = document.getElementById('devWizIdError');
            if (err) {
                err.textContent = this.t('devices.wizard.idInvalid', 'Use a-z 0-9 - _ (2-64 chars).');
                err.style.display = '';
            }
            document.getElementById('devWizId')?.setAttribute('aria-describedby', 'devWizIdError');
            return this._wizInvalid('devWizId');
        }
        return true;
    },

    async devWizardTest(btn) {
        this._devWizCollect();
        const d = this._devWiz.data;
        const out = document.getElementById(
            d.protocol === 'http' ? 'devWizTestResult3'
            : d.protocol === 'mqtt' ? 'devWizTestResult4'
            // both RTU modes (direct serial, over the network bridge) live in the RTU block
            : (d.protocol === 'rtu' || d.protocol === 'rtu-tcp') ? 'devWizTestResult2' : 'devWizTestResult');
        const orig = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="btn-spinner"></span> …';
        try {
            const conn = d.protocol === 'http'
                ? { protocol: 'http', url: d.url }
                : d.protocol === 'mqtt'
                ? { protocol: 'mqtt', broker: d.broker, port: d.mqtt_port || 1883, topic: d.topic,
                    username: d.mqtt_username || '', password: d.mqtt_password || '', tls: !!d.mqtt_tls }
                : d.protocol === 'rtu'
                ? { protocol: 'rtu', serial_port: d.serial_port, baudrate: d.baudrate,
                    parity: d.parity, stopbits: d.stopbits, unit_id: d.unit_id, timeout: d.timeout }
                : d.protocol === 'rtu-tcp' && d.bridge
                ? { bridge: d.bridge, bridge_port: d.bridge_port, unit_id: d.unit_id, timeout: d.timeout }
                : d.protocol === 'rtu-tcp'
                ? { protocol: 'rtu-tcp', host: d.host, port: d.port, unit_id: d.unit_id, timeout: d.timeout }
                : d.bridge
                ? { bridge: d.bridge, bridge_port: d.bridge_port, unit_id: d.unit_id, timeout: d.timeout }
                : { protocol: 'tcp', host: d.host, port: d.port, unit_id: d.unit_id, timeout: d.timeout };
            const r = await fetch('/api/devices/test', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                // include the chosen template so an HTTP test resolves its json_paths
                body: JSON.stringify({ connection: conn, template: d.template || '' }),
            });
            const res = await r.json();
            out.className = 'wiz-test-result ' + (res.ok ? 'ok' : 'err');
            out.textContent = (res.ok ? '✓ ' : '✗ ') + (res.message || '');
        } catch (e) {
            out.className = 'wiz-test-result err';
            out.textContent = '✗ ' + e.message;
        } finally {
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    },

    devWizardBack() {
        this._devWizCollect();
        if (this._devWiz.step > 1) { this._devWiz.step--; this._devWizRender(); }
    },

    async devWizardNext() {
        this._devWizCollect();
        if (!this._devWizValidate()) return;
        const w = this._devWiz;
        if (w.step < 3) { w.step++; this._devWizRender(); return; }

        // step 3 → submit
        const d = w.data;
        const payload = {
            id: d.id, name: d.name || d.id, template: d.template, enabled: d.enabled,
            connection: d.protocol === 'http'
                ? { protocol: 'http', url: d.url }
                : d.protocol === 'mqtt'
                ? { protocol: 'mqtt', broker: d.broker, port: d.mqtt_port || 1883, topic: d.topic,
                    username: d.mqtt_username || '', password: d.mqtt_password || '', tls: !!d.mqtt_tls }
                : d.protocol === 'tcp' && d.bridge
                ? { bridge: d.bridge, bridge_port: d.bridge_port, unit_id: d.unit_id, timeout: d.timeout }
                : d.protocol === 'tcp'
                ? { protocol: 'tcp', host: d.host, port: d.port, unit_id: d.unit_id, timeout: d.timeout }
                : d.protocol === 'rtu-tcp' && d.bridge
                ? { bridge: d.bridge, bridge_port: d.bridge_port, unit_id: d.unit_id, timeout: d.timeout }
                : d.protocol === 'rtu-tcp'
                ? { protocol: 'rtu-tcp', host: d.host, port: d.port, unit_id: d.unit_id, timeout: d.timeout }
                : d.protocol === 'rtu_tap' && d.bridge
                ? { protocol: 'rtu_tap', bridge: d.bridge, bridge_port: d.bridge_port, unit_id: d.unit_id }
                : { protocol: d.protocol === 'rtu_tap' ? 'rtu_tap' : 'rtu', serial_port: d.serial_port,
                    baudrate: d.baudrate, parity: d.parity, stopbits: d.stopbits, unit_id: d.unit_id },
            mqtt: { topic_prefix: d.topic_prefix || `meters/${d.id}` },
            influxdb: { bucket: d.bucket || undefined, device_tag: d.device_tag || undefined },
            ha_discovery_enabled: d.ha_discovery_enabled,
        };
        const fb = document.getElementById('devWizFeedback');
        try {
            const r = await fetch(w.editId ? `/api/devices/${encodeURIComponent(w.editId)}` : '/api/devices', {
                method: w.editId ? 'PUT' : 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (!r.ok) {
                const errs = (await r.json()).detail?.errors || ['save failed'];
                fb.textContent = errs.join(' · ');
                fb.className = 'save-feedback err';
                return;
            }
            this.closeModal('deviceWizardModal');
            this.showToast('success',
                w.editId ? this.t('devices.updated', 'Device updated') : this.t('devices.created', 'Device created'),
                `${d.name || d.id} → MQTT ${payload.mqtt.topic_prefix}/…`);
            await this.renderDevicesList();
            if (!w.editId && d.enabled) this.jumpToDeviceRegisters(d.id);
        } catch (e) {
            fb.textContent = e.message;
            fb.className = 'save-feedback err';
        }
    },

    // the bridge and bus a device on the network side of RS-485 is read through
    // a bridge chosen in one mode that the new mode cannot use is dropped:
    // TCP goes through a gateway, RTU over the network and listening need RTU
    _devWizFitBridge() {
        const d = this._devWiz.data;
        if (!d.bridge) return;
        const gw = this._bridgeType(((this._bridges || []).find(b => b.id === d.bridge) || {}).type).framing === 'modbus_tcp';
        const ok = d.protocol === 'tcp' ? gw : (d.protocol === 'rtu-tcp' || d.protocol === 'rtu_tap') ? !gw : false;
        if (!ok) { d.bridge = ''; d.bridge_port = null; }
    },

    // bridges that pass RTU frames (a Modbus TCP gateway is reached as TCP)
    _rtuBridges() {
        return (this._bridges || []).filter(b => this._bridgeType(b.type).framing !== 'modbus_tcp');
    },

    // Modbus TCP: directly, or through a Modbus TCP gateway's bus
    _devWizGatewayPickHtml(d) {
        const t = (k, def, p) => this.t(k, def, p);
        const buses = [];
        for (const b of this._bridges || []) {
            if (this._bridgeType(b.type).framing !== 'modbus_tcp') continue;
            for (const p of b.ports || []) buses.push({ b, p });
        }
        if (!buses.length) return '';
        const cur = d.protocol === 'tcp' && d.bridge ? `${d.bridge}::${d.bridge_port || (buses.find(x => x.b.id === d.bridge)?.p.port ?? '')}` : '';
        return `<div class="form-row"><div class="form-group flex-2">
            <label class="form-label" for="devWizGw">${t('devices.wizard.gwWhere', 'Reached')}</label>
            <select id="devWizGw" class="input" data-action="_devWizTapWhereChanged" data-on="change">
              <option value="" ${cur ? '' : 'selected'}>${t('devices.wizard.gwDirect', 'Directly — its own IP and port')}</option>
              ${buses.map(({ b, p }) => {
                  const v = `${b.id}::${p.port}`;
                  const n = (p.devices || []).filter(x => x.id !== d.id).length;
                  return `<option value="${this._esc(v)}" ${v === cur ? 'selected' : ''}>${this._esc(t('devices.wizard.gwVia', 'Through the gateway {name} · {host}:{port}', { name: b.name || b.id, host: b.host, port: p.port }))}${n ? ' · ' + this._esc(t('bridges.nDevices', '{n} device(s)', { n })) : ''}</option>`;
              }).join('')}
            </select>
            <div class="field-hint">${t('devices.wizard.gwHint', 'An RS-485 slave behind a converter in “Modbus TCP to RTU” mode: pick the gateway, then the slave’s unit ID. The gateway’s address and connection limit come from the bridge.')}</div>
          </div></div>`;
    },

    _devWizBridgePickHtml(d) {
        const bridges = this._rtuBridges();
        const t = (k, def, p) => this.t(k, def, p);
        if (!bridges.length) {
            return `<div class="field-hint" style="margin-bottom:8px;">${t('devices.wizard.noBridge', 'No bridge yet. A bridge is the box the bus is reached through — our serial bridge on a Raspberry Pi or server, or an RS-485-to-Ethernet converter (Waveshare, USR, Elfin…).')}
                <a href="#" ${this._act('openBridgeModal', [])}>${t('bridges.add', 'Add bridge')}</a> · ${t('devices.wizard.orLocal', 'or scan a bridge running next to the gateway:')}</div>`;
        }
        const cur = bridges.find(b => b.id === d.bridge) || bridges[0];
        const ports = cur.ports || [];
        const port = d.bridge_port || (ports[0] || {}).port;
        const used = (ports.find(p => +p.port === +port)?.devices || []).filter(x => x.id !== d.id);
        return `<div class="form-row" style="align-items:end;" id="devWizBridgeRow">
            <div class="form-group flex-2"><label class="form-label" for="devWizBridge">${t('devices.wizard.bridge', 'Bridge')}</label>
              <select id="devWizBridge" class="input" data-action="_devWizBridgeChanged" data-on="change">
                ${bridges.map(b => `<option value="${this._esc(b.id)}" ${b.id === cur.id ? 'selected' : ''}>${this._esc(b.name || b.id)} · ${this._esc(b.host)}</option>`).join('')}
              </select></div>
            <div class="form-group"><label class="form-label" for="devWizBridgePort">${t('devices.wizard.bus', 'Bus')}</label>
              <select id="devWizBridgePort" class="input" data-action="_devWizBridgeChanged" data-on="change">
                ${ports.map(p => `<option value="${this._esc(p.port)}" ${+p.port === +port ? 'selected' : ''}>:${this._esc(p.port)}${p.label ? ' · ' + this._esc(p.label) : ''}</option>`).join('')
                  || `<option value="">${t('devices.wizard.noBus', 'no bus yet')}</option>`}
              </select></div>
            <div class="form-group"><label class="form-label" for="devWizUnitBr">${t('lbl.unitId', 'Unit ID')}</label>
              <input type="number" id="devWizUnitBr" class="input" min="0" max="255" value="${this._esc(d.unit_id ?? 1)}"></div>
          </div>
          <div class="field-hint" id="devWizBridgeHint2" style="margin-bottom:8px;">${used.length
              ? this._esc(t('devices.wizard.busHas', 'Also on this bus: {list} — each slave needs its own unit id; they take turns on one connection.', { list: used.map(x => `${x.name || x.id} (unit ${x.unit_id})`).join(', ') }))
              : this._esc(t('devices.wizard.busEmpty', 'First device on this bus.'))}
            · <a href="#" ${this._act('_devWizShowLegacyScan', [])}>${t('devices.wizard.legacyScan', 'scan a bridge next to the gateway instead')}</a></div>`;
    },

    // listen-only: where the bus is heard — a serial port on this host, or the
    // bus of a bridge that passes the bytes on (a gateway only answers questions)
    _devWizTapWhereHtml(d) {
        const t = (k, def, p) => this.t(k, def, p);
        const buses = [];
        for (const b of this._bridges || []) {
            if (this._bridgeType(b.type).framing === 'modbus_tcp') continue;
            for (const p of b.ports || []) buses.push({ b, p });
        }
        if (!buses.length && !d.bridge) return '';
        const cur = d.bridge ? `${d.bridge}::${d.bridge_port || ''}` : '';
        return `<div class="form-row"><div class="form-group flex-2">
            <label class="form-label" for="devWizTapWhere">${t('devices.wizard.tapWhere', 'Where the bus is')}</label>
            <select id="devWizTapWhere" class="input" data-action="_devWizTapWhereChanged" data-on="change">
              <option value="" ${cur ? '' : 'selected'}>${t('devices.wizard.tapLocal', 'A serial port on this host')}</option>
              ${buses.map(({ b, p }) => {
                  const v = `${b.id}::${p.port}`;
                  const sel = v === cur || (d.bridge === b.id && !d.bridge_port && p === b.ports[0]);
                  return `<option value="${this._esc(v)}" ${sel ? 'selected' : ''}>${this._esc(b.name || b.id)} · ${this._esc(b.host)}:${this._esc(p.port)}${p.label ? ' · ' + this._esc(p.label) : ''}</option>`;
              }).join('')}
            </select>
            <div class="field-hint">${t('devices.wizard.tapWhereHint', 'Through a bridge, the gateway reads the bridge\u2019s port and never writes to it. Only transparent bridges and the MBG serial bridge pass the bus\u2019s traffic on — a Modbus TCP gateway only answers questions. The line settings are the bus\u2019s, set on the bridge.')}</div>
          </div></div>`;
    },

    _devWizTapWhereChanged() {
        this._devWizCollect();
        this._devWizRender();
    },

    _devWizBridgeChanged() {
        const d = this._devWiz.data;
        const b = document.getElementById('devWizBridge')?.value;
        // the picker shows the first bridge until one is chosen
        const shown = d.bridge || (this._rtuBridges()[0] || {}).id;
        if (b !== shown) { d.bridge = b; d.bridge_port = null; }
        else d.bridge_port = parseInt(document.getElementById('devWizBridgePort')?.value, 10) || null;
        d.unit_id = parseInt(document.getElementById('devWizUnitBr')?.value, 10) || d.unit_id;
        // redraw just the picker
        document.getElementById('devWizBridgeRow')?.remove();
        document.getElementById('devWizBridgeHint2')?.remove();
        document.getElementById('devWizRtuBridge')?.insertAdjacentHTML('afterbegin', this._devWizBridgePickHtml(d));
    },

    _devWizShowLegacyScan() {
        const d = this._devWiz.data;
        d.bridge = ''; d.bridge_port = null;
        document.getElementById('devWizLegacyScan')?.style.removeProperty('display');
        document.getElementById('devWizBridgeRow')?.remove();
        document.getElementById('devWizBridgeHint2')?.remove();
    },
});
